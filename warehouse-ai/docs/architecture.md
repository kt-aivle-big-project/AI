# Architecture

```text
Command
  → Conversation Context Load
  → Deterministic/LLM Command Interpretation
  → Conversation Inheritance & Override
  → Supervisor
  → read-only Snapshot
      ├─ Clarification → Report
      └─ Query → Report
  → Scope → Tasks → Optimizer → Routing
  → Validation / Simulation → Verification
  → bounded Replan loop
  → Persist → optional Execute
  → Evidence Report
  → Conversation Context Update → Audit
```

SQL은 확정 운영 상태와 명령/대화/시뮬레이션 이력, Neo4j는 고정 지도, Redis는 실시간 상태·예약·활성 계획을 담당한다.

PHASE 8은 자연어 분류와 결정적 fallback, PHASE 9는 Clarification 안전 종료, PHASE 10은 대화 상속과 API를 제공한다. PHASE 11의 What-if 실제 비교 실행과 PHASE 12의 운영 이벤트 기반 자동 재계획은 범위 밖이며 시작하지 않았다.

신규 Migration:

- `005_clarification_requests.sql`
- `006_conversation_sessions.sql`

두 Migration은 기존 데이터 backfill이나 삭제를 수행하지 않는다. 운영 적용 전 백업 후 번호 순서대로 실행한다. rollback SQL은 각 파일 하단에 문서화되어 있다.
# PHASE 11·12 확장

What-if 비교는 기존 Planning Graph를 시나리오별 독립 `SIMULATE_ONLY`
세션으로 재사용한다. 비교 오케스트레이터는 실행 결과의 compact metric만
`scenario_comparison*`에 저장한다.

Execution Graph는 선택적으로 `impact` 노드를 거친다. 이벤트 API는 DB
idempotency를 먼저 확인한 뒤 Graph를 호출하고, 영향 범위가 있을 때에만
별도 Planning Graph를 `SIMULATE_ONLY`로 실행한다. 실제 활성화와 dispatch는
승인 API가 최신 계획 버전을 재확인한 뒤 새 EXECUTE Graph를 호출할 때만
발생한다.

## 마이그레이션 적용 순서

현재 연결된 개발 DB에서는 `005_clarification_requests.sql`과
`006_conversation_sessions.sql`이 아직 적용되지 않은 상태로 확인됐다.
`command_history.command_id`는 text이며 관련 JSONB/timestamptz 컬럼도 호환된다.
운영 DB 여부를 확인한 후 DB 운영자가 다음 순서로 적용한다.

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_clarification_requests.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_conversation_sessions.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_scenario_comparisons.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/008_event_replan.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/009_daily_scheduling.sql
```

애플리케이션은 이 파일들을 자동 적용하지 않는다. 적용 전 백업을 만들고,
롤백이 필요하면 008부터 역순으로 각 파일 하단의 명령을 사용한다.

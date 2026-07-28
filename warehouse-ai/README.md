# Warehouse Planning Supervisor

> 현재 코드 기준: **P16.5.14** — ROBOT_FAILED 이벤트에서 PICK 전 고장과 적재 중 고장을 구분하고, 안전 정지·적재물 고정이 확인된 경우 고장 정지 노드에서 대체 로봇 인계 PICK/DROP 체인을 생성합니다.
> 동일 이벤트 멱등성, 서버 권위 상태, 저배터리 충전 재계획, 실패 계획 복구 계약을 유지하며 전체 회귀 기준은 `756 passed, 0 failed`입니다.

자연어 창고 명령을 해석하고 PostgreSQL의 확정 운영 상태, Neo4j의 고정 지도,
Redis의 실시간·예약 상태를 결합해 계획·시뮬레이션·실행·재계획을 수행하는
FastAPI + LangGraph 프로젝트입니다.

기본 최적화는 로컬 CPU deterministic optimizer를 사용하고, 시간 기반 경로는
Prioritized Time A*로 충돌을 피합니다. `SIMULATE_ONLY`는 simulation 전용 가상
상태만 변경하고, `EXECUTE`만 활성 계획을 Redis에 저장한 뒤 Robot Gateway로
READY 작업을 전송합니다.

## 주요 기능

- `QUERY`, `PLAN_ONLY`, `SIMULATE_ONLY`, `EXECUTE`, `RESET`
- 명시적 LLM Supervisor와 deterministic fallback
- PostgreSQL·Neo4j·Redis 통합 Snapshot
- 작업 배정, 실제 지도 기반 경로, 노드·간선 예약, WAIT 충돌 회피
- 독립 Verification Agent와 제한된 자동 재계획 루프
- Conversation 조건 상속·명시적 override
- What-if 비교, 이벤트 기반 재계획, simulation session
- 일일 다중 작업 계획, FINISH_TO_START 선후관계, 시간창, 긴급 작업 삽입
- 명령·단계·계획·시뮬레이션·초기화 이력 보존

`warehouse_graph.json`과 `rack_inventory.json` Importer는 구현하지 않았습니다.

## 개발 환경

- Python 3.11+
- PostgreSQL: 재고, 로봇, 작업, 확정 결과, 감사 이력
- Neo4j: `CONNECTED_TO` 기반 노드·통로 그래프
- Redis: 실시간 로봇 상태, 활성 계획, 예약, simulation 가상 상태
- OpenAI API: 선택적 Supervisor·Verification·보고서 생성
- Mock Robot Gateway: 실제 로봇 없이 `EXECUTE` 전송 검증

OpenAI 키가 없거나 structured output 호출이 실패하면 deterministic fallback이
동작합니다. 실제 secret은 `.env`에만 보관하고 커밋하지 마세요.

## 설치

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`에 PostgreSQL, Neo4j, Redis 접속 정보와 필요할 때만 OpenAI 키를 입력합니다.

```powershell
python main.py health
```

## PostgreSQL 마이그레이션

기본 ERD의 `warehouse_items`, `robot`, `works`, `simulation_run`이 먼저 있어야
합니다. 마이그레이션은 번호 순서대로 검토 후 수동 적용합니다.

```text
002_simulation_sessions.sql
003_command_history.sql
004_simulation_history_and_reset.sql
005_clarification_requests.sql
006_conversation_sessions.sql
007_scenario_comparisons.sql
008_event_replan.sql
009_daily_scheduling.sql
010_time_indexed_inventory.sql
013_p16_5_15_execution_delivery.sql
```

`009_daily_scheduling.sql`은 다음을 추가합니다.

- `works.actual_started_at`, `works.actual_completed_at`
- `work_dependencies`: FINISH_TO_START와 `lag_seconds`
- `work_schedule_constraints`: 입력 시간창·동일 로봇·순서 제약


`013_p16_5_15_execution_delivery.sql`은 다음을 추가합니다.

- `execution_plan_approval`: 검증을 통과한 정확한 계획 버전과 fingerprint 승인
- `robot_execution_dispatch`: dispatch, 명령 상태, ACK, retry, cancel, rollback 감사 기록

계산 결과인 `works.scheduled_start/scheduled_end`와 사용자 입력 제약을 분리합니다.
Neo4j에는 작업 일정이나 선후관계를 중복 저장하지 않습니다.

```powershell
$env:PSQL_DATABASE_URL="postgresql://warehouse_app:YOUR_PASSWORD@127.0.0.1:5432/warehouse"
Get-ChildItem .\migrations\*.sql | Sort-Object Name | ForEach-Object {
    psql $env:PSQL_DATABASE_URL -v ON_ERROR_STOP=1 -f $_.FullName
}
```

이 명령은 자동 실행되지 않습니다. 운영 적용 전 백업과 각 SQL의 rollback 주석을
확인하세요.

## 서버 실행

Supervisor API:

```powershell
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```

- Health: `GET http://127.0.0.1:8000/health`
- Swagger: `http://127.0.0.1:8000/docs`

Mock Robot Gateway는 별도 터미널에서 실행합니다.

```powershell
$env:MOCK_GATEWAY_AUTO_EXECUTE="false"
python -m uvicorn mock_robot_gateway:app --host 127.0.0.1 --port 9000
```

Supervisor의 `.env`에는 다음을 설정합니다.

```env
ROBOT_GATEWAY_URL=http://127.0.0.1:9000
WAREHOUSE_TIMEZONE=Asia/Seoul
```

Mock Gateway는 기본적으로 계획을 수신·기록할 뿐 실제 로봇을 움직이지 않습니다.

## 자연어 명령 예시

모든 명령은 `POST /v1/planning/commands`의 `text`로 전달합니다.

일일 계획:

```text
오늘 오전 9시부터 10시까지 W-001 작업을 처리하고,
완료하면 W-002 작업을 처리해줘.
오후 1시부터 2시까지 W-003 작업을 실행해줘.
전체 계획을 시뮬레이션해줘.
```

긴급 삽입:

```text
그 일정 그대로 두고 W-004를 지금 먼저 처리해줘.
진행 중인 작업은 중단하지 말고 이후 일정은 다시 맞춰줘.
```

결정 규칙:

- `W-001 완료 후 W-002`, `W-001 → W-002`: FINISH_TO_START
- `09:00~10:00`: HARD_WINDOW
- `10시까지`: DEADLINE
- `지금 먼저`, `급하게`, `최우선`: INSERT_TASK + URGENT
- 긴급 삽입 기본값: NON_PREEMPTIVE
- 명시적 중단 요구: safe-stop 확인 전 `CLARIFICATION_REQUIRED`
- warehouse timezone 미설정: `Asia/Seoul` + 경고 기록

후속 명령은 같은 `conversation_id`에서 일정·선후관계·로봇 제한·최적화 조건을
상속할 수 있습니다. 다른 warehouse 또는 conversation의 조건은 상속하지 않습니다.

## 일일 계획 실행 방식

1. 자연어에서 작업 ID, 시간창, 선후관계, 동일 로봇 조건을 typed model로 해석
2. 의존 그래프를 위상 정렬하고 cycle이면 Optimizer 전에 안전 종료
3. Local Optimizer가 다음 하한으로 작업 시작 시각 계산

```text
max(robot_available_time, earliest_start,
    predecessor_end + lag, preserved_route_end_time)
```

4. Routing이 시간 포함 경로와 노드·간선 예약 생성
5. Simulation과 Verification 수행
6. `EXECUTE`에서만 계획·제약을 저장하고 현재 READY 작업만 Gateway로 전송
7. 완료 이벤트가 successor를 READY로 전환하고 실패 이벤트는 직접 successor만 BLOCKED

실행 중 작업과 freeze horizon은 긴급 삽입 때 고정됩니다. 시작 전 미래 작업만
재배치하며, hard window를 위반해야만 가능한 `EXECUTE`는 자동 반영하지 않고
clarification으로 종료합니다.

## 실행 모드

- `QUERY`: 조회만 수행
- `PLAN_ONLY`: 배정·경로·검증까지만 수행
- `SIMULATE_ONLY`: `sim:{simulation_id}:*` 가상 상태만 재생
- `EXECUTE`: 최종 Verification PASS 계열에서만 활성화·전송
- `RESET`: 기존 감사·명령·완료 이력은 보존하고 지정 상태만 초기화

실행 이벤트는 `POST /v1/execution/events`, What-if는
`POST /v1/scenario-comparisons`를 사용합니다.

## 로그 상관관계

다음 식별자는 API 응답과 command/stage 로그에서 함께 추적됩니다.

```text
command_id
conversation_id
parent_command_id
plan_version
simulation_id
```

내부 chain-of-thought, 전체 프롬프트, secret은 저장하지 않습니다.

주요 조회 API:

```text
GET /v1/commands/{command_id}
GET /v1/commands/{command_id}/stages
GET /v1/commands/{command_id}/plan-evidence
GET /v1/conversations/{conversation_id}
GET /v1/simulations/{simulation_id}/logs
```

## 초기화

운영 RESET API:

```text
POST /v1/simulations/{simulation_id}/reset
POST /v1/warehouses/{warehouse_id}/simulations/reset-all
GET  /v1/warehouses/{warehouse_id}/simulation-reset-logs
```

개발 데이터 초기화는 production에서 거부되며 두 인자가 모두 필수입니다.

```powershell
$env:APP_ENV="development"
python scripts/reset_demo_data.py --warehouse-id 1 --confirm
```

## 테스트

단위·회귀 테스트:

```powershell
python -m compileall -q app tests scripts
python -m pytest -q
```

실제 외부 서비스를 수동 실행한 뒤 공개 API smoke test:

```powershell
python scripts/smoke_test.py `
  --warehouse-id 1 `
  --base-url http://127.0.0.1:8000 `
  --mock-gateway-url http://127.0.0.1:9000 `
  --inventory-smoke
```

`--inventory-smoke`는 Migration 010과 개발용 inventory seed를 적용한 경우에만
추가합니다. 이 옵션이 없으면 기존 대표 API 회귀 흐름만 실행합니다.

pytest는 실제 PostgreSQL·Redis·Neo4j·Gateway 통합을 자동 실행하지 않습니다.

## 시간축 재고·입출고 계획

재고 기능은 다음 소유권을 유지합니다.

- PostgreSQL: 품목, 확정 AVAILABLE Lot, 입출고 주문, 완료 movement
- Redis: `ACTIVE_PLAN` 전역 예약과 `simulation_id`별 가상 상태
- Neo4j: 노드·통로 토폴로지만 저장하며 재고와 예약은 저장하지 않음

입고는 `SCHEDULED → ARRIVED → UNLOADING → INSPECTING → AVAILABLE` 순서로
진행되며 출고 계산에는 `expected_available_at` 또는 확정된
`actual_available_at`만 사용합니다. 도착 시각만으로 재고를 늘리지 않습니다.
수량 단위는 `BOX`, `BOXES`, `박스`만 `BOX`로 정규화합니다. EA·개·PALLET
등은 자동 환산하지 않고 clarification으로 종료합니다.

계획 파이프라인은 Optimizer 전 `inventory_precheck`, Optimizer 후
`inventory_timeline_validation`을 수행합니다. 부분 출고는 사용자가 명시적으로
승인한 경우에만 허용하며, 부족 작업과 그 종속 작업만 차단하고 독립 작업은
계속 계산합니다.

예약 정책:

- `PLAN_ONLY`: 예약 및 실제 DB 변경 없음
- `SIMULATE_ONLY`: 응답과 `sim:{simulation_id}:*` 안에서만 가상 차감
- `EXECUTE`: Verification PASS 후 Redis `ACTIVE_PLAN` 예약 생성
- `TASK_COMPLETED`: PostgreSQL 트랜잭션 성공 후 예약 `CONSUMED`
- `TASK_FAILED`/취소: 실제 재고를 바꾸지 않고 예약 해제
- PostgreSQL 반영 실패: Redis 예약을 유지해 안전하게 재시도

Migration 010은 자동 실행되지 않습니다. 002~009 적용 후 검토하여 수동
적용합니다.

```powershell
psql $env:PSQL_DATABASE_URL -v ON_ERROR_STOP=1 `
  -f .\migrations\010_time_indexed_inventory.sql
```

개발 재현 데이터도 production에서 거부되며 명시적 승인이 필요합니다.

```powershell
$env:APP_ENV="development"
python scripts/seed_inventory_demo_data.py `
  --warehouse-id 1 --storage-node-id 2 --outbound-node-id 4 --confirm
```

이 seed는 A/B/C/D/E/F의 AVAILABLE 재고를 각각 40/20/60/15/120/30 BOX로
맞추고, A 50·B 100·F 20 BOX 입고를 `INSPECTING` 상태(다음 창고 현지
07:00 도착, 07:10 사용 가능)로 등록합니다. A 30 BOX(01:30)와 F 50
BOX(07:00) 출고 주문은
승인 가능한 demo work에 연결됩니다. `--inventory-smoke`는 실제 완료 event까지
전송해 이 demo 재고를 변경하므로 매 반복 실행 전에 `reset_demo_data.py`를
실행하거나 seed를 다시 적용하세요. 두 스크립트 모두 audit/movement 이력은
삭제하지 않습니다.

대표 명령:

```text
오전 1시 30분까지 A 30박스를 출고하는 가상 시뮬레이션을 실행해줘.
오전 7시에 A 50박스와 B 100박스가 입고되고, 검수 완료 예정은 오전 7시 15분이야. 시뮬레이션해줘.
오늘 주문과 입고 예정 데이터를 기준으로 계획해줘.
F 50박스를 출고하되 재고가 있는 만큼만 우선 처리해줘.
```

`CAPACITY_DATA_NOT_CONFIGURED`는 입고 용량 검증이 필요한데 capacity 계약이
없는 경우에만 경고하며 재고 계산 자체를 실패시키지 않습니다.

## 주요 경로

```text
app/api.py                         FastAPI API
app/models.py                      요청·응답·structured output 모델
app/planning/graph.py              Planning LangGraph
app/planning/nodes.py              Supervisor와 계획 단계 노드
app/services/scheduling.py         일정 해석·의존성·READY 계산
app/services/local_optimizer.py    로컬 CPU 스케줄 최적화
app/services/routing.py            충돌 방지 시간 경로
app/services/simulation.py         계획 시뮬레이션
app/services/scheduler_tick.py     polling 없는 deterministic tick
app/services/schedule_dispatcher.py READY 전용 Gateway payload
app/execution/graph.py             완료·실패·후속 작업 이벤트 흐름
app/repositories/postgres.py       확정 상태와 일정 영속화
app/repositories/neo4j.py          고정 지도 조회
app/repositories/redis_store.py    실시간·예약·가상 상태
migrations/009_daily_scheduling.sql 일일 일정 DB 확장
migrations/010_time_indexed_inventory.sql 시간축 재고·입출고 DB 확장
app/services/inventory_projection.py 시간대별 재고 예측·부족·Lot 할당
app/services/inventory_reservations.py SIMULATION/ACTIVE_PLAN 예약
scripts/seed_inventory_demo_data.py 개발용 재고 fixture
```

## P13: 충전 설명 가능성과 안전성

P13에서는 충전 체류를 `CHARGING` 근거로 기록하고, Optimizer와 Routing
거리 차이에 구체적인 원인 코드를 제공합니다. 자동 생성된 충전 작업의
실행 의존성은 `execution_task_dependencies`에서 확인할 수 있습니다.

충전소별 비용은 임의 기본값을 사용하지 않습니다. 실제 기준값은 아래
스크립트로 Neo4j active CHARGER 노드에 입력합니다.

```powershell
python -m scripts.set_charger_costs --warehouse-id 2 --cost 2152=1.5
```

여러 노드는 `--cost NODE_ID=COST`를 반복합니다.

## P14 deterministic scope and execution dependency validation

P14 fixes bounded single-robot hypothetical commands to `LOCAL_REPLAN` when an
active/base plan exists and validates generated `CHARGE -> PICK -> DROP`
dependencies against routing-reconciled task times. See
`P14_SCOPE_DEPENDENCY_VALIDATION_NOTES.md`.

## P16 최종 통합

P16은 최종 라우팅 거리와 배터리 계산을 일치시키고, API 응답을 사용자용과
개발자용으로 분리합니다.

### 최종 경로 기반 배터리

시간 기반 라우팅에서 WAIT·우회가 발생해 Optimizer 예상거리보다 실제 경로가
길어지면 기존 CHARGE 작업의 충전량을 자동 보정합니다.

```text
energy_source = ROUTING_FINAL_DISTANCE
```

충전 시간의 time step 수가 바뀌면 충전소 예약과 이후 경로도 다시 계산합니다.
최종 경로 기준으로 최소 배터리를 보장하지 못하면 Verification을 통과하지 않습니다.

### 응답 크기 선택

요청 JSON에 다음 필드를 사용할 수 있습니다.

```json
{
  "report_detail_level": "STANDARD",
  "response_view": "AUTO"
}
```

- `AUTO`: 일반 보고서는 COMPACT, DEBUG는 FULL
- `COMPACT`: 사용자 화면에 필요한 핵심 결과만 반환
- `FULL`: 기존 전체 계획·경로·후보·trace 반환

자세한 내용은 `docs/response_views.md`를 확인합니다.

### 최종 로컬 검사

```powershell
python -m scripts.run_p16_release_checks
python -m pytest -q
```

최종 데모 순서는 `docs/final_demo_guide.md`, 요구사항별 구현 위치는
`docs/requirements_traceability.md`에 정리돼 있습니다.

## P16.2.3 미등록 실행 요청 정책

- 등록 품목 후보가 전혀 없으면 추가 질문 없이 계획을 생성하지 않습니다.
- 결과는 `EMERGENCY_REVIEW_REQUIRED`, `earliest_full_fulfillment_at: null`,
  `task_count: 0`, `gateway_dispatched: false`로 종료됩니다.
- 공백·하이픈 차이처럼 유사한 등록 품목 후보가 있을 때만 선택 확인을 요청합니다.
- 최종 응답의 동일 경고 문자열은 한 번만 반환합니다.

검사:

```powershell
python -m scripts.run_p16_2_3_unknown_item_checks
```

## P16.3 최종 복합 일일 계획 통합

P16.3은 마지막 핵심 기능 통합 버전입니다.

- A/B 출고와 C 입고를 한 자연어 명령에서 `DAILY_PLAN`으로 처리합니다.
- `A 재고가 부족하면 A 작업만 제외`는 가상 재고 변경이 아니라 실제 SQL 재고 정책으로 처리합니다.
- 재고가 부족한 작업과 그 종속 작업만 차단하고, 독립적인 B 출고와 C 입고는 계속 진행합니다.
- 한 명령 안에서도 출고 목적지와 입고 저장 목적지가 서로 덮어쓰이지 않습니다.
- `최소 운용 배터리 20%를 유지`를 `MINIMUM_REQUIRED_CHARGE` 정책으로 인식합니다.
- 배터리가 충분하면 불필요한 충전 작업을 만들지 않습니다.
- 일부 작업만 성공하면 최종 보고서는 `PARTIAL_SUCCESS_WITH_EMERGENCY`로 표시합니다.

검사:

```powershell
python -m scripts.run_p16_3_final_integration_checks
```

Swagger 예시는 `examples/p16_3_final_daily_plan_request.json`을 사용합니다.

## P16.3.3 배터리 안전 충전소 선택

- 로봇의 최소 운용 배터리 20%는 이동·작업·충전소 접근 중 항상 유지합니다.
- 예측 오차를 고려해 기본 안전 여유 0.5%를 적용합니다.
- 충전이 필요하면 다음 작업 투입 기준인 80%까지 충전합니다.
- 먼저 안전하게 도달 가능한 active CHARGER만 남기고, 그중 설정 비용이 가장 낮은 충전소를 선택합니다.
- 안전 후보에 비용 정보가 없으면 가장 가까운 안전 충전소를 명시적 fallback으로 선택합니다.
- 안전하게 도달 가능한 충전소가 없으면 위험 계획을 승인하지 않고 `LOCAL_REPLAN` 대상으로 처리합니다.
- FULL 응답에서는 연속 CHARGE 이벤트도 WAIT와 같이 범위 압축합니다.

검사:

```powershell
python -m scripts.run_p16_3_3_final_checks
```

Swagger 예시는 `examples/p16_3_3_battery_safe_charger_request.json`을 사용합니다.

## P16.3.4 충전 목표·재계획 상태 Hotfix

P16.3.4는 최종 라우팅 후 충전 종료 배터리가 79.82%로 낮아지거나,
LOCAL_REPLAN에서 자동 CHARGE 작업이 사라지는 문제를 수정합니다.

```powershell
python -m scripts.run_p16_3_4_final_checks
```

Swagger 예시는 `examples/p16_3_4_charge_replan_state_request.json`을 사용합니다.
현재 응답 스키마 버전은 `p16.5.7`입니다.




## P16.5.2 NVIDIA 라이브 REST 스키마 자동 호환 Hotfix

P16.5.2는 실제 NVIDIA API Catalog가 더 이상 허용하지 않는
`solver_config.drop_infeasible_tasks` 때문에 발생한 `CUOPT_SUBMIT_HTTP_422`를 수정합니다.

현재 REST 요청의 `solver_config`에는 `time_limit`만 전송합니다.
모든 작업의 필수 수행 여부는 cuOpt 응답에서 dropped task와 누락된 작업을 검사하여
엄격하게 보장합니다.

또한 향후 NVIDIA REST 스키마가 바뀌어 HTTP 422 응답에
`Extra inputs are not permitted`가 반환되면, 해당 선택 필드 경로를 자동으로 제거한 뒤
최대 2회 다시 제출합니다. 제거된 필드와 재시도 횟수는 계획 metadata의
`cuopt_schema_removed_fields`, `cuopt_schema_retry_count`에 기록됩니다.

이전 P16.5.1에서 제거한 다음 `task_data` 선택 필드도 계속 제외합니다.

```text
task_ids
priorities
mandatory_task_ids
```

내부 작업 ID는 요청 순서의 task index로 유지하고, cuOpt 응답의 숫자 task index를
기존 `AtomicTask.task_id`로 다시 매핑합니다.

검사:

```powershell
python -m scripts.run_p16_5_2_final_checks
python -m scripts.run_p16_5_3_final_checks
python -m pytest -q
```

## P16.5 NVIDIA cuOpt REST Primary + CPU Fallback

P16.5는 `cuopt-thin-client`, `cuopt-lp`, CUDA와 로컬 GPU 의존성을 제거했습니다.
Windows PowerShell에서도 기존 `requirements.txt`만 설치하면 됩니다.

`.env`에는 NVIDIA API Catalog에서 발급한 키 한 줄만 추가합니다.

```env
CUOPT_API_KEY=nvapi-YOUR_KEY
```

기본 호출 경로:

```text
POST https://optimize.api.nvidia.com/v1/nvidia/cuopt
GET  https://optimize.api.nvidia.com/v1/status/{request_id}
```

동작 순서:

1. `CUOPT_API_KEY`가 있으면 `httpx`로 NVIDIA managed cuOpt REST API를 호출합니다.
2. HTTP 200이면 즉시 결과를 사용하고, HTTP 202이면 `request_id`로 상태 API를 polling합니다.
3. cuOpt가 로봇 배정과 방문 순서를 결정합니다.
4. 기존 CPU warehouse normalizer가 배터리, CHARGE, 세부 작업 시간과 내부 계약을 보정합니다.
5. 인증 실패, 네트워크 오류, 타임아웃, 비정상 응답, 해 미생성 시 CPU optimizer로 자동 전환합니다.
6. 이후 Prioritized Time A*, Simulation, Verification 흐름은 동일합니다.

P16.4 환경변수 `CUOPT_CLIENT_SAK`도 호환 별칭으로 읽지만, 새 설정에는 `CUOPT_API_KEY` 사용을 권장합니다. Function ID는 필요하지 않습니다.

이전 `.env`에 `OPTIMIZER_BACKEND=local`이 있어도 `CUOPT_AUTO_ENABLE=true`이고 API 키가 있으면 cuOpt가 우선 활성화됩니다. CPU만 강제로 사용하려면:

```env
OPTIMIZER_BACKEND=local
CUOPT_AUTO_ENABLE=false
```

응답 확인:

```json
{
  "optimizer_execution": {
    "requested_provider": "CUOPT",
    "used_provider": "CUOPT",
    "transport": "HTTPS_REST",
    "fallback_used": false,
    "fallback_reason": null
  }
}
```

CPU fallback이 발생하면:

```json
{
  "optimizer_execution": {
    "requested_provider": "CUOPT",
    "used_provider": "CPU",
    "fallback_used": true,
    "fallback_reason": "...",
    "attempts": [
      {"provider": "CUOPT_REST", "status": "FAILED"},
      {"provider": "CPU", "status": "SUCCESS"}
    ]
  }
}
```

검사:

```powershell
python -m scripts.run_p16_5_final_checks
python -m pytest -q
```



## P16.5.3 Time Monotonicity Hotfix

장시간 WAIT 뒤 같은 노드 PICK/DROP에서 발생하던 동일 시각 waypoint 중복과 0-step 작업을 수정했습니다.

핵심 불변식:

```text
route[i+1].time_step > route[i].time_step
PICK/DROP/CHARGE end_time_step > start_time_step
```

검사:

```powershell
python -m scripts.run_p16_5_3_final_checks
pytest -q tests/test_routing.py tests/test_p16_5_3_time_monotonicity.py
```


## P16.5.4 입출고 방향·재고 검증·정오 시간창 Hotfix

복합 일일 계획에서 이전 문장의 `입고`가 다음 문장의 E/F `출고`를 덮어쓰던 문제를 수정했습니다.
각 상품 수량은 먼저 같은 문장 안의 입고·출고 동사를 사용하고, 같은 문장에 동사가 없을 때만 인접 정책 문장을 제한적으로 참고합니다.

추가 수정:

- `오전 10시 30분부터 12시까지`의 12시를 다음 날 자정이 아닌 당일 정오로 처리
- INBOUND PICK을 기존 창고 재고 소비에서 제외
- 현재 lot와 `FUTURE_INBOUND` 할당의 합이 수량을 충족하면 시뮬레이션 재고 부족을 발생시키지 않음
- 최적화 전에 제외된 출고 부족 작업도 최종 Verification evidence에 명시

검사:

```powershell
python -m scripts.run_p16_5_4_final_checks
pytest -q tests/test_p16_5_4_inventory_direction_hotfix.py
```

## P16.5.5 다중 로봇 재분배·혼잡 노드 우회 Hotfix

복합 일일 일정에서 cuOpt가 모든 작업을 한 로봇에 배정하더라도, 사용자가 로봇을 명시적으로 고정하지 않았다면 로컬 창고 후처리가 독립 작업 묶음을 여러 로봇에 재분배합니다. PICK과 DROP은 동일한 `same_robot_group`으로 유지됩니다.

명령에 `노드 2013에 로봇이 몰리지 않도록`처럼 혼잡 회피가 포함되면 해당 노드에 소프트 패널티를 적용합니다. 가능한 대체 경로가 있을 때만 우회하며, 실제 폐쇄 노드로 처리하지는 않습니다.

확인 필드:

```json
{
  "response_schema_version": "p16.5.5",
  "data": {
    "robot_count": 3,
    "optimizer_postprocessing": {
      "cuopt_assignment_application": {
        "mode": "GLOBAL_ORDER_LOCAL_MULTI_ROBOT_REBALANCE"
      },
      "parallel_robot_rebalance": {
        "enabled": true
      }
    },
    "congestion_avoidance": {
      "node_ids": [2013]
    }
  }
}
```


## P16.5.6 공유 작업 노드 장기 대기·경로 실패 Hotfix

P16.5.5에서 작업은 3대 로봇으로 분산됐지만, 먼저 경로가 생성된 로봇이
STORAGE `2088`에서 다음 작업까지 장시간 대기하며 노드를 계속 예약해
두 번째 입고 로봇의 DROP 경로가 `ROUTE_FAILED`로 종료될 수 있었습니다.

P16.5.6은 장기 공백 동안 로봇을 일반 ROUTE holding node로 이동시킨 뒤
다음 작업 시간에 복귀시킵니다. holding node는 서비스 노드, 혼잡 회피 노드,
그리고 OUTBOUND `2146`의 유일 진입점 `2044` 같은 articulation node를 제외합니다.
동일 snapshot 노드에서 시작하는 여러 로봇도 첫 free time step부터 순차 활성화합니다.

확인 필드:

```json
{
  "response_schema_version": "p16.5.6",
  "data": {
    "robot_count": 3,
    "congestion_avoidance": {
      "node_ids": [2013]
    }
  },
  "collision_plan": {
    "metadata": {
      "idle_relocation_count": 3,
      "idle_relocations": [
        {
          "resolution": "IDLE_RELOCATION",
          "reason": "RELEASE_SHARED_SERVICE_NODE_DURING_LONG_IDLE"
        }
      ]
    }
  }
}
```

검사:

```powershell
python -m scripts.run_p16_5_6_final_checks
pytest -q tests/test_p16_5_6_idle_holding_routing.py
```

## P16.5.7 길막 대기 금지·Idle Whitelist Safety

P16.5.7부터 일일 일정의 장기 대기는 일반 ROUTE 노드에서 허용되지 않습니다. `PARKING`, `STAGING`, `HOLDING`, `CHARGER_WAITING_AREA` 또는 `idle_allowed=true` 노드에서만 대기합니다.

창고 2 데모 지도에 전용 PARKING 2160~2162를 먼저 추가합니다.

```powershell
python -m scripts.seed_p16_5_7_idle_nodes --warehouse-id 2
```

서버 실행 후 Swagger에서는 다음 예제를 사용합니다.

```text
examples/p16_5_7_idle_whitelist_request.json
```

정상 확인값:

```text
response_schema_version = p16.5.7
status = SIMULATION_SUCCESS
idle_policy.strict = true
idle_policy.violation_count = 0
idle_action_task_count > 0
모든 holding_node_type = PARKING/STAGING/HOLDING/CHARGER_WAITING_AREA
conflict_count = 0
```

전용 대기 노드가 없으면 `IDLE_NODE_NOT_CONFIGURED`로 계획을 승인하지 않습니다.

## P16.5.8 장기 공백 충전소 복귀·기회 충전

P16.5.8은 P16.5.7의 길막 대기 금지 정책 위에서 장기 공백을 배터리 운영에 활용합니다.
충전소까지의 이동과 실제 충전을 정식 작업으로 삽입하고, 원래 업무 작업의 시작 시각을 늦추지 않는 idle gap 안에서만 기회 충전을 수행합니다.

창고 2의 2160~2162 노드는 각 충전소에 연결된 `CHARGER_WAITING_AREA`로 갱신합니다.

```powershell
python -m scripts.seed_p16_5_8_charger_waiting_nodes --warehouse-id 2
```

Swagger 예제:

```text
examples/p16_5_8_opportunity_charging_request.json
```

정상 확인값:

```text
response_schema_version = p16.5.8
status = SIMULATION_SUCCESS
conflict_count = 0
idle_energy_planning.enabled = true
opportunity_charging.policy = LONG_IDLE_CHARGER_AREA_FIRST
충전 완료 후 CHARGER 슬롯 장기 대기 없음
충전 후 대기 위치 = 2160 / 2161 / 2162
```

기본 정책:

```text
장기 공백 → 다른 작업 가능 여부 확인
→ 충전 가능한 공백이면 충전소 MOVE + 필요한 만큼 CHARGE
→ 충전 종료 즉시 연결 CHARGER_WAITING_AREA로 이탈
→ 충전이 불필요하면 충전소 주변 대기 구역 또는 whitelist idle node 사용
```

기본 설정:

```dotenv
OPPORTUNITY_CHARGING_ENABLED=true
OPPORTUNITY_CHARGE_TARGET_BATTERY=95
OPPORTUNITY_CHARGE_MIN_IDLE_MINUTES=15
OPPORTUNITY_CHARGE_MIN_GAIN_PERCENT=2
```

검사:

```powershell
python -m scripts.run_p16_5_8_final_checks
pytest -q tests/test_p16_5_8_opportunity_charging.py
```

## P16.5.8.1 기회 충전 재계획 안정성 Hotfix

P16.5.8.1은 첫 충전 포함 경로가 성공한 뒤 충전소 비용 검증 차이로
불필요한 재계획이 발생하고, 재계획 경로 prefix와 새 CHARGE 구간의 시작점이
겹칠 때 `list index out of range`가 발생하던 문제를 수정합니다.

- 계획기와 검증기가 동일한 충전소 후보 순위 및 비용 모드를 사용합니다.
- 일부 후보에만 비용이 있으면 누락 비용을 0으로 취급하지 않고 모든 후보를
  거리 기준으로 비교합니다.
- 재계획마다 `opportunity:*` 충전 작업과 충전 슬롯 예약을 다시 생성합니다.
- 재계획이 실패해도 직전 경로·시뮬레이션 성공 후보를 증거로 보존합니다.

```text
response_schema_version = p16.5.8.1
```


## P16.5.8.2 충전 검증 및 재계획 시뮬레이션 재생 Hotfix

실제 Swagger 재현에서 경로·충전·충돌 회피가 성공한 뒤 다음 두 상태 일관성
문제가 최종 검증을 실패시키는 현상을 수정합니다.

- 검증기는 경로 보정 후 변경된 충전시간으로 후보를 새로 최적화하지 않고,
  계획 시점에 저장된 immutable `selection_key`를 재생합니다.
- 에너지 보정값은 `reconciled_*` 필드에 기록하고 기존 후보 순위 입력값은
  덮어쓰지 않습니다.
- LOCAL_REPLAN의 전체 계획 재생 전 기존 Redis 가상 상태를 제거하고 SQL
  snapshot에서 다시 초기화하여 동일 출고 lot의 이중 차감을 막습니다.
- 중복된 충전 대기 구역 ID를 제거합니다.

```text
response_schema_version = p16.5.8.2
```

검사:

```powershell
python -m scripts.run_p16_5_8_2_final_checks
pytest -q tests/test_p16_5_8_2_verification_replay_hotfix.py
```


## P16.5.9 공유 자원 용량 스케줄링

P16.5.9는 cuOpt 기본 배정 뒤, Prioritized Time A* 경로 생성 전에 충전기와
공유 작업 노드의 서비스 시간창을 확정합니다. 라우팅에서 생성된 장기 대기 작업은
대기 구역·parking·staging 용량으로 다시 검증합니다.

- `service_capacity`: PICK/DROP 공유 작업 노드 동시 처리 수
- `service_duration_seconds`: 작업 노드 점유 시간
- `charger_capacity`: 충전기 동시 충전 슬롯 수
- `waiting_capacity` / `parking_capacity`: 대기 공간 동시 점유 수
- `maximum_idle_duration`: 해당 공간의 최대 연속 대기 시간

기존 Neo4j 지도를 삭제하지 않고 자원 속성만 추가합니다.

```powershell
python -m scripts.seed_p16_5_9_resource_capacities --warehouse-id 2
python -m scripts.run_p16_5_9_final_checks
```

Swagger는 `SUMMARY + COMPACT`로 실행하고 다음을 확인합니다.

```text
response_schema_version = p16.5.9
status = SIMULATION_SUCCESS
verification.decision = PASS 또는 PASS_WITH_WARNING
result.resources.valid = true
result.resources.reservation_count > 0
result.schedule_validation.resource_capacity_valid = true
errors = []
```

자세한 데이터 계약과 차단 오류는
`P16_5_9_SHARED_RESOURCE_CAPACITY_NOTES.md`를 참고하세요.

## P16.5.10 — cuOpt explicit charge visits and operational objective

P16.5.10 converts selected mandatory/opportunity charger visits into explicit,
robot-bound `CHARGE` tasks and runs a bounded second optimizer pass. A synthetic
`MOVE` task prepositions the robot from the charger to the next service area before
its hard time window. The final operational objective includes charge time, charger
waiting, congestion, shared-resource occupancy, and unnecessary charger round trips.

```powershell
python -m scripts.run_p16_5_10_final_checks
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```

Expected compact response fields:

```text
response_schema_version = p16.5.10
result.optimizer_roles.mode = TWO_PASS_EXPLICIT_CHARGE_VISITS
result.objective.status = PASS
result.resources.valid = true
```

See `P16_5_10_CUOPT_CHARGE_VISIT_OBJECTIVE_NOTES.md` for the responsibility and
objective contracts.

## P16.5.10.1 — second-pass robot binding hotfix

This hotfix keeps the first-pass business-task robot assignments authoritative
when explicit `CHARGE` and `MOVE` visits are added. The second pass is now a
robot-bound visit-order refinement. It also omits `pickup_and_delivery_pairs`
from the mixed standalone-task second-pass managed cuOpt payload and uses
`order_vehicle_match` for every task.

```powershell
python -m scripts.run_p16_5_10_1_final_checks
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```

Expected compact response:

```text
response_schema_version = p16.5.10.1
status = SIMULATION_SUCCESS
result.optimizer_roles.second_pass_role = ROBOT_BOUND_BUSINESS_AND_CHARGE_VISIT_ORDER
result.resources.valid = true
result.objective.status = PASS
errors = []
```

See `P16_5_10_1_SECOND_PASS_BINDING_HOTFIX_NOTES.md`.

## P16.5.10.2 — route-order convergence hotfix

P16.5.10.2 fixes a routing/resource fixed-point failure caused by synthetic
CHARGE/MOVE priority values overriding their scheduled start times. Internal
routing and shared-resource scheduling now use one dependency-aware per-robot
order with start time as the primary signal and priority only as a tie-breaker.

Run the focused release gate:

```powershell
python -m scripts.run_p16_5_10_2_final_checks
```

Expected response markers:

```text
response_schema_version = p16.5.10.2
status = SIMULATION_SUCCESS
result.resources.valid = true
result.objective.status = PASS
errors = []
```

See `P16_5_10_2_ROUTE_ORDER_CONVERGENCE_HOTFIX_NOTES.md`.

## P16.5.10.3 — charge-command boundary hotfix

P16.5.10.3 fixes a one-time-step CHARGE command truncation at the exact boundary
where an explicit CHARGE visit ends and its following MOVE task begins. The
physical waypoint action now owns the command mapping, so the final CHARGE
waypoint remains attached to the CHARGE task rather than the MOVE task.

Run the focused release gate:

```powershell
python -m scripts.run_p16_5_10_3_final_checks
```

Expected response markers:

```text
response_schema_version = p16.5.10.3
status = SIMULATION_SUCCESS
result.resources.valid = true
result.objective.status = PASS
errors = []
```

See `P16_5_10_3_CHARGE_COMMAND_BOUNDARY_HOTFIX_NOTES.md`.

## P16.5.11 — MAPF failure automatic replanning

P16.5.11 classifies routing failures instead of converting every failure into a
terminal `PIPELINE_ERROR`. Concrete reservation/resource conflicts trigger a
bounded `LOCAL_REPLAN` for the affected robot and work only. If the identical
local MAPF signature repeats, the second and final configured attempt widens to
`GLOBAL_REPLAN`, rotates robot routing priority, and applies a small bounded
activation stagger.

Configuration/backend contract failures remain non-retryable, and topology
failures widen directly to global replanning.

Run the release gate:

```powershell
python -m scripts.run_p16_5_11_final_checks
```

Expected recovered response markers:

```text
response_schema_version = p16.5.11
status = SIMULATION_SUCCESS
verification.decision = PASS or PASS_WITH_WARNING
result.mapf_replan.enabled = true
result.mapf_replan.strategy = AFFECTED_ROBOTS_FIRST
  or ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER
result.resources.valid = true
result.objective.status = PASS
errors = []
```

See `P16_5_11_MAPF_AUTO_REPLAN_NOTES.md`.

## P16.5.11.1 event escalation hotfix

The event-driven replanning endpoint now allows one bounded escalation for an
identical local route failure:

```text
LOCAL_REPLAN -> GLOBAL_REPLAN once -> repeated-failure stop
```

Additional response consistency fixes preserve the injected active plan
version, align `final_status` with `status`, and keep
`result.mapf_replan.version = p16.5.11.1` even when automatic MAPF replanning is
inactive.

Release gate:

```powershell
python -m scripts.run_p16_5_11_1_final_checks
```

## P16.5.12 runtime robot-state partial replanning

P16.5.12 turns verified execution telemetry into an explicit partial-replan
contract. `LOW_BATTERY`, position deviation, delay, task failure, robot failure,
and path anomalies now identify completed, protected, and changeable tasks
before a new candidate is generated.

The active plan Snapshot is replayed as `EVENT_SOURCE_PLAN`, and runtime
`node_id`, `battery`, and `status` values are applied directly to the optimizer.
A local replan therefore changes only mutable affected work while completed,
freeze-horizon, and unaffected tasks remain fixed.

Release gate:

```powershell
python -m scripts.run_p16_5_12_final_checks
```

Expected result:

```text
111 passed
```

Swagger example:

```text
examples/p16_5_12_low_battery_event.json
```

See `P16_5_12_RUNTIME_ROBOT_STATE_PARTIAL_REPLAN_NOTES.md`.


## P16.5.13 Gate 2 — server-authoritative runtime state

Gate 2 prevents execution-event clients from selecting the active plan or
runtime clock. `REAL` events resolve the plan from warehouse Redis;
`SIMULATION` events resolve it by `simulation_id` from the server simulation
session. Client `active_plan`, `active_plan_version`, and `current_time_step`
values are stripped and reported as ignored fields.

`POSITION_UPDATED` now updates telemetry only. The server independently derives
`LOW_BATTERY` when the reported battery cannot safely cover the configured
minimum, safety margin, and remaining planned energy.

Release gate:

```powershell
python -m scripts.run_p16_5_13_gate2_checks
# optional complete project regression
python -m scripts.run_p16_5_13_gate2_checks --full
```

Expected result:

```text
Gate 2 focused regression: 39 passed
Gate 2 full result: 731 passed / 0 failed
```

Swagger event examples:

```text
examples/p16_5_13_gate2_position_safe_event.json
examples/p16_5_13_gate2_position_low_event.json
```

See `P16_5_13_GATE2_SERVER_AUTHORITY_NOTES.md`.

## P16.5.13 Gate 2.1 — low-battery safety charge

Gate 2.1 closes the live low-battery partial-replan gap found in Swagger.
Server-side remaining-energy detection was already correct, but the affected
currently executing task stayed frozen and prevented a CHARGE visit from being
inserted before it.

For a server-derived LOW_BATTERY event, only the affected chain is released
from the freeze horizon. When the battery remains above the hard minimum, that
chain keeps its current robot assignment while timing is rescheduled, allowing:

```text
current position -> safe charger -> CHARGE -> PICK -> DROP
```

Release gate:

```powershell
python -m scripts.run_p16_5_13_gate2_1_checks
# optional complete project regression
python -m scripts.run_p16_5_13_gate2_1_checks --full
```

Expected result:

```text
Gate 2.1 focused result: 40 passed / 0 failed
Gate 2.1 full result: 732 passed / 0 failed
```

See `P16_5_13_GATE2_1_LOW_BATTERY_SAFETY_CHARGE_NOTES.md`.

## P16.5.13 Gate 2.2 — changeable cuOpt freeze hotfix

Gate 2.2 fixes the remaining live LOW_BATTERY partial-replan failure.
The event impact layer correctly released the affected C PICK/DROP chain, but
managed cuOpt postprocessing converted its robot binding back into
`frozen=true`. That prevented the local optimizer from inserting a safety
CHARGE visit.

The corrected contract is:

```text
LOCAL_REPLAN changeable task
assigned_robot_id = reporting robot
frozen = false
```

Protected or fixed tasks remain frozen. The resulting affected-chain order is:

```text
current position -> safe charger -> CHARGE -> PICK -> DROP
```

Release gate:

```powershell
python -m scripts.run_p16_5_13_gate2_2_checks
# optional complete project regression
python -m scripts.run_p16_5_13_gate2_2_checks --full
```

Expected result:

```text
Gate 2.2 focused result: 47 passed / 0 failed
Gate 2.2 full result: 734 passed / 0 failed
```

See `P16_5_13_GATE2_2_CHANGEABLE_CUOPT_FREEZE_HOTFIX_NOTES.md`.


## P16.5.13 Gate 2.3 — low-battery E2E charge retention hotfix

Gate 2.3 reproduces the live Gate 2.2 Swagger failure through the complete
optimizer-to-routing path. The affected C window had already opened when the
LOW_BATTERY event arrived. During the explicit charger-visit second pass, that
historical `earliest_start` was incorrectly promoted into the new CHARGE
`latest_finish`, producing an impossible interval and allowing the affected
charge chain to disappear before final route-energy validation.

The corrected window contract is:

```text
successor earliest_start <= first-pass CHARGE target end
-> opened/historical lower bound
-> CHARGE latest_finish = null

successor earliest_start > first-pass CHARGE target end
-> genuinely future start boundary
-> may constrain CHARGE completion
```

A bounded safety guard also verifies that server-derived LOW_BATTERY work still
assigned to the reporting robot retains a CHARGE task. When the task is absent,
the deterministic local optimizer runs once against the same problem and must
restore a complete safe chain:

```text
current position -> safe charger -> CHARGE -> MOVE_TO_NEXT -> PICK -> DROP
```

Failed automatic replans now include bounded optimizer and route-energy debug
evidence instead of returning only the final battery error.

Release gate:

```powershell
python -m scripts.run_p16_5_13_gate2_3_checks
# optional complete project regression
python -m scripts.run_p16_5_13_gate2_3_checks --full
```

Expected result:

```text
Gate 2.3 focused result: 30 passed / 0 failed
Gate 2.3 full result: 738 passed / 0 failed
```

See `P16_5_13_GATE2_3_LOW_BATTERY_E2E_CHARGE_RETENTION_HOTFIX_NOTES.md`.

## P16.5.14 — robot failure and carried-load recovery

`ROBOT_FAILED` 이벤트는 서버 계획 시계와 로봇 적재 상태를 사용해 다음 두 경로로 분기합니다.

```text
PICK 이전 고장
-> failed robot 제외
-> 원래 PICK/DROP 체인을 대체 로봇에 재배정

PICK 이후 적재 중 고장
-> safe_stop_confirmed=true
-> load_secured=true
-> 고장 노드에서 synthetic handover PICK
-> 동일 대체 로봇이 기존 목적지 DROP
```

적재 여부가 불명확하거나 안전 정지·적재물 고정이 확인되지 않으면 자동 재계획을 수행하지 않습니다.

```text
status = MANUAL_RECOVERY_REQUIRED
recovery_required = true
auto_replan_requested = false
```

Release gate:

```powershell
python -m scripts.run_p16_5_14_checks
# optional complete project regression
python -m scripts.run_p16_5_14_checks --full
```

Expected result:

```text
P16.5.14 focused result: 75 passed / 0 failed
P16.5.14 full result: 756 passed / 0 failed
```

See `P16_5_14_ROBOT_FAILURE_LOAD_RECOVERY_NOTES.md`.



## P16.5.14.1 — failed robot stale-route eviction hotfix

A robot-failure handover must remove the failed robot's previous active route from MAPF reservations. The routing change set is now the union of cuOpt changes, event impact robots, and failed/excluded robots.

```text
failed/excluded active robot
-> no preserved prefix
-> no task ownership
-> no vertex/edge reservation
-> synthetic handover chain only
```

Routing metadata and event-replan responses expose `stale_route_eviction` evidence.

Release gate:

```powershell
python -m scripts.run_p16_5_14_1_checks
# optional complete project regression
python -m scripts.run_p16_5_14_1_checks --full
```

Expected focused result: `78 passed / 0 failed`.

See `P16_5_14_1_ROBOT_FAILURE_STALE_ROUTE_EVICTION_HOTFIX_NOTES.md`.


## P16.5.15 — approved plan and reliable robot command delivery

Verified plans are durably approved before activation and dispatch. Robot command batches
use deterministic idempotency identities, strict per-robot ACK sequence, bounded retry,
cancel, and safe rollback policies.

```text
verified plan approval
-> active plan fingerprint match
-> durable dispatch before network send
-> strict sequence ACK
-> timeout retry with same dispatch_id
-> cancel remaining commands on failure
-> logical rollback only before physical progress
```

Apply the new SQL migration first:

```powershell
psql $env:PSQL_DATABASE_URL -v ON_ERROR_STOP=1 `
  -f .\migrations\013_p16_5_15_execution_delivery.sql
```

Release gate:

```powershell
python -m scripts.run_p16_5_15_checks
python -m scripts.run_p16_5_15_checks --full
```

Expected results:

```text
P16.5.15 focused result: 79 passed / 0 failed
P16.5.15 full result: 772 passed / 0 failed
```

See `P16_5_15_EXECUTION_DELIVERY_SAFETY_NOTES.md`.

## P16.5.15.1 — approved PostgreSQL work target verification hotfix

Clarification-bound PostgreSQL outbound works use a legacy `<work_id>:move` task. The
verifier now recognizes that task as destination evidence only when it is explicitly
linked to the requested outbound inventory operation and targets the requested node.
Unrelated relocation MOVE rows remain invalid evidence.

```powershell
python -m scripts.run_p16_5_15_1_checks
python -m scripts.run_p16_5_15_1_checks --full
```

Expected results:

```text
P16.5.15.1 focused result: 87 passed / 0 failed
P16.5.15.1 full result: 777 passed / 0 failed
```

See `P16_5_15_1_APPROVED_SQL_WORK_TARGET_VERIFICATION_HOTFIX_NOTES.md`.

## P16.5.15.2 — gateway cancel confirmation and rollback safety hotfix

The server now restores a previous active plan only after the Robot Gateway explicitly
confirms cancellation. A 404, timeout, transport error, unsupported endpoint, rejected
response, or ambiguous response keeps the current plan active and places commands in
`CANCEL_PENDING` with manual recovery and retry enabled.

```text
confirmed gateway cancel + no physical progress -> ROLLED_BACK
unconfirmed gateway cancel -> CANCELED_PARTIAL_EXECUTION / retryable
confirmed cancel + physical progress -> manual recovery, no rollback
```

ACKs arriving while cancellation is unconfirmed update physical-progress evidence but
cannot downgrade the dispatch to a normal partial/completed state.

```powershell
python -m scripts.run_p16_5_15_2_checks
python -m scripts.run_p16_5_15_2_checks --full
```

Expected focused result: `97 passed / 0 failed`.

See `P16_5_15_2_GATEWAY_CANCEL_CONFIRMATION_ROLLBACK_SAFETY_HOTFIX_NOTES.md`.

## P16.5.15.3 — terminal command state and reservation cleanup hotfix

P16.5.15.3 rejects ACK timestamps that predate the durable send time, blocks ACKs for
terminal or non-delivered dispatches, terminalizes retry-exhausted commands as
`DISPATCH_FAILED`, persists rollback evidence, and releases inventory reservations after
a successful pre-physical rollback.

```text
ACK before sent_at - 5s -> HTTP 409 ACK_BEFORE_COMMAND_SENT
ACK after RETRY_EXHAUSTED -> HTTP 409 ACK_DISPATCH_NOT_ACTIVE
retry exhausted command -> DISPATCH_FAILED / DISPATCH_RETRY_EXHAUSTED
safe rollback -> ACTIVE_PLAN reservation RELEASED
```

```powershell
python -m scripts.run_p16_5_15_3_checks
python -m scripts.run_p16_5_15_3_checks --full
```

Expected focused result: `100 passed / 0 failed`.

See `P16_5_15_3_TERMINAL_STATE_RESERVATION_CLEANUP_HOTFIX_NOTES.md`.

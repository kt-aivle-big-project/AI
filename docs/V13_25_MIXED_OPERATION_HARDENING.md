> **Legacy 문서:** 이 문서는 과거 Native `orders`/`handling_units` Fixture 경로를 설명합니다. v13.27 공유 BE 운영 경로는 `BE_CENTERED_STRUCTURED_INPUT_ARCHITECTURE.md`를 기준으로 하며, 이 문서의 Native Schema 실행 절차를 사용하지 않습니다.

# v13.25 Mixed Operation Hardening

## 1. 수정 목적

자연어 요청:

```text
ORD-001을 출고하고 IN-001도 입고해. 전체 완료시간을 최소화해.
```

에서 Router는 두 Operation을 정상적으로 정규화했지만 Agent 정식화 결과가 다음처럼 만들어질 수 있었습니다.

```json
{
  "formulation_mode": "GOODS_TO_PERSON",
  "g2p_order_ids": ["ORD-001"],
  "tasks": []
}
```

결과적으로 `ORD-001`만 Solver·MAPF에 들어가고 `IN-001`은 `logical_operations`에 빈 항목으로 남거나 실행 계획에서 사라졌는데도 `plan_validated`가 반환될 수 있었습니다.

v13.25의 목표는 다음입니다.

```text
요청에 포함된 모든 actionable operation을 정확히 한 번 보존
→ Solver 전 검증
→ Solver/Compiler/MAPF 후 최종 Plan에서 재검증
→ 누락된 Plan은 저장·반환하지 않음
```

---

## 2. 원인 분석

### 2.1 프롬프트 문제

이전 프롬프트는 G2P 모드에서 사실상 다음을 지시했습니다.

```text
Set formulation_mode=GOODS_TO_PERSON.
Copy outbound order IDs to g2p_order_ids.
Keep tasks empty.
```

하지만 G2P는 **출고 Order를 물리 Handling Unit Cycle로 변환하는 방식**일 뿐, 입고·복구 Task를 제거하는 전역 모드가 아닙니다.

### 2.2 후처리 문제

LLM이 입고 Task를 올바르게 만들어도 후처리가 `tasks=[]`로 덮어쓸 수 있었습니다.

### 2.3 Agent 조회 문제

Agent의 Canonical 조회 계획은 출고 Order/Inventory 중심이었고, `INBOUND_ITEM`에 필요한 다음 사실을 완전하게 수집하지 못했습니다.

```text
Inbound Receipt
Handling Unit
Inbound Handoff Access
Putaway Rack Slot
Robot → Pickup path
Pickup → Delivery path
```

### 2.4 검증 문제

Rule과 Agent가 서로 다른 Coverage 검사를 사용했고, Agent의 G2P 검증은 outbound order만 검사했습니다. `IN-001`이 어느 집합에도 없더라도 valid가 될 수 있었습니다.

### 2.5 최종 Plan 검증 부재

Solver 전 Draft가 정상이어도 Compiler·Enricher·MAPF·Projection 중 작업이 사라질 수 있는데, 최종 `SimulationPlan`의 Operation→Task→Robot→SERVICE 연결을 독립 검사하지 않았습니다.

---

## 3. 수정된 Operation 계약

모든 actionable operation은 아래 중 **정확히 한 곳**에 있어야 합니다.

```text
1. G2P outbound order
2. Direct task
3. Explicitly deferred operation
```

```python
requested_actionable_ids
==
set(g2p_order_ids)
| set(task.source_operation_id for task in tasks)
| set(deferred_operation_ids)
```

중복도 허용하지 않습니다.

```text
ORD-001이 g2p_order_ids와 tasks에 동시에 존재
→ OPERATION_MULTIPLE_REPRESENTATIONS

IN-001이 어디에도 없음
→ OPERATION_COVERAGE_MISMATCH:IN-001
```

현재 스키마의 `deferred_order_ids` 필드명은 하위 호환을 위해 유지하지만, 의미는 모든 Canonical Operation ID입니다.

---

## 4. 프롬프트 수정

파일:

```text
app/prompts/cuopt_formulator.py
```

핵심 규칙:

```text
GOODS_TO_PERSON applies only to outbound operations.
It never forbids direct non-outbound tasks.

Every requested INBOUND_ITEM and RECOVERY operation must remain in tasks.
Never silently omit, rename, duplicate, or reinterpret an actionable operation.
```

혼합 예시도 Prompt에 포함했습니다.

```json
{
  "normalized_operations": [
    {"operation_id": "ORD-001", "operation_type": "OUTBOUND_ORDER"},
    {"operation_id": "IN-001", "operation_type": "INBOUND_ITEM"}
  ],
  "expected": {
    "g2p_order_ids": ["ORD-001"],
    "tasks": [
      {"order_id": "IN-001", "operation_type": "INBOUND_ITEM"}
    ]
  }
}
```

LLM 호출 Payload에도 다음을 명시적으로 전달합니다.

```json
{
  "required_operation_coverage": {
    "actionable_operation_ids": ["ORD-001", "IN-001"],
    "outbound_g2p_operation_ids": ["ORD-001"],
    "direct_task_operation_ids": ["IN-001"]
  }
}
```

---

## 5. 후처리 수정

파일:

```text
app/graph/cuopt_formulation.py
```

이전:

```python
if goods_to_person:
    tasks = []
```

수정:

```python
if goods_to_person:
    g2p_order_ids = canonical_outbound_ids
    tasks = [task for task in tasks if task.operation_type != "OUTBOUND_ORDER"]
```

즉 outbound order-level Task만 제거하며 다음 Task는 유지합니다.

```text
INBOUND_ITEM
RECOVERY
향후 CHARGE / PARK 등 direct operation
```

---

## 6. Agent 조회 수정

### 6.1 새 조회 Tool

```text
get_inbound_facts
```

조회 결과:

```text
inbound receipt
handling unit
item / quantity
source port
inbound handoff
target rack / level
putaway candidate
```

### 6.2 혼합 Canonical Retrieval Plan

`ORD-001 + IN-001`의 조회 DAG:

```text
ORDER_FACTS ──────────────┐
                          ├─ CONNECTING_SUBGRAPH ─ PATH_RUNTIME
INBOUND_FACTS ────────────┤
ROBOT_RUNTIME ────────────┘
INVENTORY_CANDIDATES (outbound stock)
```

### 6.3 Situation Graph 추가 관계

```text
USES_HANDLING_UNIT
PICKUP_FROM
PUTAWAY_TO
HAS_ACCESS_POINT
```

Path Evidence:

```text
ROBOT_TO_PICKUP
PICKUP_TO_DELIVERY
```

혼합 요청에서 outbound G2P path와 inbound putaway path가 같은 Warehouse Situation Graph에 존재합니다.

---

## 7. Solver 전 검증

파일:

```text
app/services/cuopt_formulation_service.py
```

공통 검증 함수가 Rule과 Agent 모두에 적용됩니다.

```text
OPERATION_COVERAGE_MISMATCH
UNKNOWN_OPERATION_COVERAGE
OPERATION_MULTIPLE_REPRESENTATIONS
OPERATION_TYPE_MISMATCH
DUPLICATE_TASK_ID
```

Inbound Task는 다음도 검증합니다.

```text
Inbound Receipt ID
Item / Quantity
Handling Unit
Pickup Handoff Access
Putaway Rack Slot
Delivery Access
Robot-to-Pickup path
Pickup-to-Delivery path
Evidence IDs
```

### 7.1 1회 Repair

Agent Draft가 Coverage 검증에 실패하면 기존 Route를 바꾸지 않고 LLM Formulator를 한 번만 재호출합니다.

```text
첫 Draft 누락
→ validation_errors에 OPERATION_COVERAGE_MISMATCH 전달
→ formulation_retry_count=1
→ LLM repair
```

재시도 후에도 실패하면 Solver로 진행하지 않습니다.

---

## 8. 최종 Plan 검증

신규 파일:

```text
app/services/logical_operation_validation_service.py
app/graph/logical_operation_validation.py
```

실행 위치:

```text
simulation_plan_builder
→ logical_operation_coverage_validator
→ frontend_explanation
```

검증 내용:

```text
요청 Operation이 logical_operations에 모두 존재
중복·Unknown Operation 없음
실행 Operation은 task_ids 보유
실행 Operation은 assigned_robot_id 보유
logical task가 실제 SERVICE Step에 존재
deferred Operation이 실행 Task를 가지지 않음
```

오류 예:

```text
PLAN_OPERATION_COVERAGE_MISSING:IN-001
PLAN_OPERATION_HAS_NO_TASKS:IN-001
PLAN_OPERATION_HAS_NO_ROBOT:IN-001
LOGICAL_TASK_MISSING_FROM_EXECUTION:IN-001:TASK-001
```

Guard 실패 시:

```text
simulation_plan = null
workflow status = failed
Plan Store 저장 안 함
```

---

## 9. Live Repository 수정

### 9.1 JSON 의존 제거

이전 `LiveWarehouseRepository`는 `JsonWarehouseRepository.__init__()`을 먼저 호출해서 live 모드도 `data/*.json` 파일 존재에 의존했습니다.

수정 후:

```text
LiveWarehouseRepository
→ JSON constructor 호출 안 함
→ PostgreSQL / Redis / Neo4j만 조회
→ DB 불완전 시 fail closed
```

### 9.2 요청 단위 Snapshot

```text
Plan 요청 시작
→ PostgreSQL·Redis·Neo4j 병렬 조회
→ request-scoped repository 생성
→ 모든 LangGraph 노드가 같은 Snapshot 사용
→ 요청 종료 후 폐기
```

`node()`나 `edge()` 호출마다 Neo4j에 Round-trip하지 않습니다. 한 Plan 내부에서 지도 버전이 바뀌지 않도록 요청 단위 Snapshot을 사용합니다.

### 9.3 Source Manifest

Trace 응답:

```json
{
  "repository": {
    "repository_type": "LiveWarehouseRepository",
    "source_manifest": {
      "route_nodes": "neo4j_snapshot",
      "route_edges": "neo4j_snapshot",
      "racks": "postgres_snapshot",
      "handling_units": "postgres_live",
      "orders": "postgres_live",
      "inbound_receipts": "postgres_live",
      "facilities": "postgres_snapshot",
      "robots": "redis_live",
      "edge_runtime": "redis_live",
      "reservations": "redis_live"
    }
  }
}
```

---

## 10. Router 보정

정확한 Canonical ID와 단순 목적만 포함한 자연어 요청은 불필요하게 Agent로 강제하지 않습니다.

```text
ORD-001을 출고하고 IN-001도 입고해.
→ Rule로 처리 가능
```

다음은 Agent 대상입니다.

```text
조건부 Edge 정책
정책 우선순위 충돌
모호한 Entity 참조
Rule Schema 밖의 판단
```

Router를 Rule로 보내는 것만으로 Agent 버그를 숨기지 않았습니다. `force_agent`나 복합 정책으로 Agent가 선택돼도 동일 Coverage 검증을 통과해야 합니다.

---

## 11. Trace 성공 기준

```json
{
  "checks": {
    "structured_keys_valid": true,
    "dynamic_input_valid": true,
    "payload_valid": true,
    "candidate_space_valid": true,
    "assignment_valid": true,
    "route_valid": true,
    "mapf_valid": true,
    "logical_operation_coverage_valid": true
  }
}
```

`ORD-001 + IN-001` Plan은 다음을 만족해야 합니다.

```text
logical_operations IDs = {ORD-001, IN-001}
ORD-001.task_ids non-empty
IN-001.task_ids non-empty
ORD-001.assigned_robot_id non-empty
IN-001.assigned_robot_id non-empty
```

---

## 12. 테스트 방법

### 12.1 단위·회귀 테스트

```powershell
python -m pytest -q
```

현재 회귀 묶음은 다음을 함께 확인한다.

```text
BE-centered structured input contract
입출고 혼합 작업 보존
통합 G2P compiler와 최종 Plan 검증
저배터리 rolling-horizon 재계획
Human Review gate와 재개 계약
30개 운영 시나리오와 Rule/Agent 비교
```

### 12.2 BE 중심 HTTP E2E

```powershell
python -m scripts.run_be_centered_plan_probe `
  --base-url http://localhost:8000
```

### 12.3 운영 시나리오 + cuOpt 평가

```powershell
python -m scripts.run_planning_operational_scenario_suite `
  --base-url http://localhost:8000 `
  --backend cuopt `
  --strict `
  --archive
```

---

## 13. 현재 범위

완료:

```text
Initial Plan
Mixed outbound/inbound
Rule/Agent common validation
LLM repair
OR-Tools/cuOpt
MAPF
SimulationPlan
Repository source observability
```

이번 릴리스에서 변경하지 않음:

```text
기존 BE-main Java 소스
POST /optimize
POST /reoptimize
Spring Client를 Native Plan API로 교체하는 작업
Replan 통합 고도화
```

# BE 중심 Structured Input 계획 아키텍처

## 1. 목표

기존 Spring BE의 데이터 구조를 원본으로 두고 LARO에 필요한 기능만 추가한다.

```text
기존 BE를 LARO Native DB로 복제
→ 사용하지 않음

기존 BE public.* + 부족한 속성 laro_ext.*
→ 사용

업무 Order/HU 마스터를 LARO에 다시 저장
→ 사용하지 않음

이번 계획의 업무를 structured_input으로 전달
→ 사용
```

## 2. 전체 흐름

```text
프론트 또는 BE 업무 생성부
        │
        │ simulationRunId + structuredInput + userCommand
        ▼
Spring POST /api/laro/simulation-runs/{id}/plan
        │
        ▼
FastAPI POST /api/v1/simulation-runs/{id}/missions/plan
        │
        ├─ PostgreSQL public.simulation_runs
        │      └─ 창고 식별
        ├─ 요청 structured_input
        │      └─ 출고·입고·이동 업무
        ├─ PostgreSQL public.warehouse_items
        │      └─ 출고 가능한 재고 단위
        ├─ Spring Redis simulation:run:{id}:*
        │      └─ 현재 로봇·차단 Edge 상태
        ├─ Neo4j RouteNode/TRAVERSES
        │      └─ BE 지도 기반 이동 그래프
        ▼
Rule 또는 Agent
→ cuOpt/OR-Tools
→ MAPF
→ MOVE/WAIT/SERVICE SimulationPlan
        │
        ├─ laro_ext.simulation_plan 저장
        ├─ laro_ext.inventory_reservation 저장
        └─ laro_ext.request_log 저장
```

## 3. 요청이 업무 원본인 이유

`structured_input.operations`는 이번 계획에서 처리할 업무를 완전하게 표현한다.

- `operation_id`: 요청 안에서 업무를 추적하는 유일 ID
- `operation_type`: OUTBOUND, INBOUND 등
- `item_id` 또는 `product_code`: 기존 BE Product 식별
- `quantity`: 처리 수량
- `source_*`: 실제 출발 재고·Node·시설
- `destination_*`: 업무 목적지
- `priority`, `release_at_ms`, Service Time: 계획 조건

이 값은 Request Log와 Plan JSON에 보관되지만 별도의 Order 마스터로 복제하지 않는다.

## 4. user_command 역할

`user_command`는 다음처럼 업무 사실이 아닌 운영 의도를 전달한다.

```text
전체 완료시간 최소화
R003 제외
H3_7을 조건부 회피
예비 로봇 한 대 유지
배터리 위험 고려
```

구조화 입력에 없는 수량·품목·출발지·목적지를 LLM이 새로 만들어서는 안 된다. Operation Coverage Guard가 각 구조화 업무가 최종 Plan에 한 번씩 남았는지 검증한다.

## 5. orders와 handling_units를 사용하지 않는 방식

### orders

출고·입고 업무는 Request Overlay가 기존 Repository의 `get_order()`와 `get_inbound_receipt()` 계약으로 변환한다. 이 데이터는 요청 동안만 존재한다.

### handling_units

기존 G2P 코드가 사용하는 명칭은 호환을 위해 일부 남아 있지만, 원본은 `public.warehouse_items`다.

```text
warehouse_item_id=17
→ 요청 내 inventory_unit_id="WI-17"
→ 물리 재고 선택·반환 판단에 사용
```

`WI-17`은 내부 호환 ID이며 `handling_units` 테이블 행이 아니다. 요청이 `source_warehouse_item_id`를 지정하면 해당 BE 재고 행을 Hard Constraint로 사용한다.

## 6. 기존 BE 무수정 범위

기존 파일은 수정하지 않는다.

```text
기존 OptimizationClient
기존 /api/optimizations
기존 SimulationRun
기존 RobotState Redis Writer
기존 Warehouse/Node/Edge/Item Entity
```

새 파일만 추가한다.

```text
com.aivle.be.laro.client.LaroPlanClient
com.aivle.be.laro.controller.LaroPlanController
com.aivle.be.laro.dto.*
com.aivle.be.laro.service.LaroPlanService
```

모든 LARO 화면은 Spring의 `/api/laro/**`를 통해 Native Mission Plan API를 사용한다.

## 7. 지도 통합

`prepare_be_centered_data.py`는 LARO의 Access Node 지도를 기존 BE 테이블에 병합한다.

```text
public.warehouse_node
→ Route/Access/Charging/Facility Node 저장

public.warehouse_edge
→ Directed 이동 Edge 저장

laro_ext.node_profile
→ RACK_ACCESS, SAFE_HOLD, SERVICE_ONLY 등 의미 보충

laro_ext.edge_profile
→ 속도·이동시간·물리 Resource·통행 가능 여부 보충
```

Rack 본체 Node는 BE에 남지만 `transit_allowed=false`로 표시되어 Neo4j Route Projection에서는 제외된다.

## 8. Runtime 통합

LARO는 Spring BE가 이미 쓰는 Redis를 읽는다.

```text
simulation:run:{runId}:robots
simulation:run:{runId}:robot:{robotId}:state
```

정밀 Replan용 필드만 별도 Companion Key에 보충할 수 있다.

```text
simulation:run:{runId}:robot:{robotId}:laro
simulation:run:{runId}:meta
simulation:run:{runId}:edge:{edgeId}:state
```

초기 Plan은 기존 RobotState만으로 가능하다. 진행 중 Replan은 currentStep, currentEdge, load 등의 확장 상태가 추가로 필요하다.

## 9. 권위 데이터

| 데이터 | 권위 Writer | LARO 역할 |
|---|---|---|
| 창고·노드·엣지 | Spring BE | 읽기 |
| 품목·재고 | Spring BE | 읽기·예약안 생성 |
| 업무 Operation | 요청을 만든 BE | 계획 동안 읽기 |
| 로봇 Runtime | Spring 시뮬레이터 | 읽기 |
| 지도 Projection | BE 지도 동기화 도구 | 읽기 |
| Plan | LARO 생성, Spring 적용 | 생성·저장 |
| 실제 재고 차감 | Spring BE | LARO 결과를 검증 후 Commit |

## 10. Plan Version·재전송·재고 예약

- `simulationRunId`별 Plan Version은 `laro_ext.simulation_plan`의 최신 Version 다음 값으로 저장한다.
- 동일 `structured_input.request_id`가 재전송되면 `laro_ext.request_log`의 기존 응답을 반환한다.
- 검증된 출고 Plan은 선택된 `public.warehouse_items` 행을 `laro_ext.inventory_reservation`에 예약한다.
- 하나의 `warehouse_items` 행은 이 통합에서 하나의 물리 Inventory Unit으로 취급하므로 ACTIVE 예약 중에는 다른 신규 Plan에서 다시 선택하지 않는다.
- 실제 수량 차감·예약 Commit/Release는 Spring BE 실행 결과 확인 후 수행한다.

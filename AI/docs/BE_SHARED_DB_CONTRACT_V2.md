# BE-main 기준 PostgreSQL·Redis 확장 계약 v2

## 1. 목표

기존 Spring `BE-main` Java 소스를 수정하지 않고 Spring과 LARO가 같은 PostgreSQL·Redis·Neo4j 서버를 사용합니다.

```text
public.*
→ Spring JPA가 소유하고 쓰는 운영 테이블

laro_contract.*
→ LARO 호환용 Additive Schema

Spring Redis RobotState Key
→ 기존 Spring이 계속 Writer

Optional LARO Redis Key
→ 기존 RobotState DTO와 충돌하지 않는 별도 Namespace
```

운영 데이터의 권위 Writer는 Spring입니다. LARO는 읽고 계산한 경로·배정만 응답합니다.

---

## 2. 기존 구조에서 바뀐 점

### v1 호환 방식

```text
/optimize Body Graph
→ public.be_compat_graph_snapshots에 JSON 전체 저장
→ Redis에도 Graph 전체 저장
→ Neo4j Projection
```

### v2 권장 방식

```text
Spring public.warehouse_node / warehouse_edge
→ 정적 Graph의 우선 원본

요청 Graph가 Spring DB와 다를 때만
→ laro_contract.route_node / route_edge에 정규화된 Fallback 저장

Redis
→ 기본은 Graph Metadata만 Cache

Neo4j
→ 재생성 가능한 Route Projection
```

신규 v2 Stack은 `002_be_compat_schema.sql`을 실행하지 않으며 구버전 public Graph Snapshot 테이블을 새로 만들지 않습니다. 기존 DB에 이미 있으면 Migration Fallback으로만 읽습니다.

---

## 3. PostgreSQL 역할

### 3.1 `public.*` — 기존 Spring 영역

현재 Spring JPA가 생성·관리하는 주요 테이블을 그대로 둡니다.

```text
warehouse_layout
warehouse_node
warehouse_edge
robot
robot_specs
task
simulation_runs
warehouse_items
storage_location
charging_station
...
```

v2 SQL은 다음을 하지 않습니다.

```text
ALTER TABLE public.*
DROP TABLE public.*
DELETE FROM public.*
```

Spring의 Hibernate `ddl-auto=update`와 직접 충돌하지 않도록 설계했습니다.

### 3.2 `laro_contract.contract_meta`

| 컬럼 | 용도 |
|---|---|
| `contract_name` | 계약 식별자 |
| `contract_version` | 현재 DB 계약 버전 |
| `updated_at` | 적용 시각 |

### 3.3 `warehouse_binding`

Spring 숫자 Warehouse와 현재 LARO Graph를 연결합니다.

| 컬럼 | 용도 |
|---|---|
| `warehouse_id` | Spring Warehouse PK |
| `warehouse_code` | `WH-001` 형태의 안정 코드 |
| `graph_version` | Node/Edge 전체 Hash |
| `graph_source` | `spring_db`, `contract` |
| `map_version` | 지도 버전 확장 |
| `inventory_version` | 재고 Snapshot 확장 |
| `facility_version` | 시설 Snapshot 확장 |

중요한 역할:

```text
/optimize에서 Spring Graph와 요청 Graph가 달랐음
→ graph_source=contract로 고정
→ 이후 /reoptimize가 Spring Graph로 몰래 전환하지 않음
```

Contract Graph 갱신은 Warehouse ID 기준 PostgreSQL advisory transaction lock을 사용하고, Node·Edge·Binding을 한 트랜잭션에서 확정합니다. 동시에 두 `/optimize` 요청이 들어와 Graph Version과 Binding이 엇갈리는 것을 방지합니다.

### 3.4 `route_node`

Spring Graph가 없거나 요청 Graph가 다를 때만 쓰는 정규화된 Fallback Node입니다.

| 컬럼 | 용도 |
|---|---|
| `warehouse_id`, `node_id` | Spring 숫자 ID 계약 |
| `node_code` | 향후 문자열 Node Code |
| `x`, `y` | 프론트 좌표 전달 정보 |
| `semantic_type` | `ROUTE`, 향후 `RACK_ACCESS` 등 |
| `service_only` | 작업 전용 접근점 여부 |
| `transit_allowed` | 통과 가능 여부 |
| `resource_type/code` | Rack·Station·Handoff 연결 |
| `adjacent_route_node_id` | 접근점과 통로 연결 |
| `graph_version` | Graph 일관성 |

### 3.5 `route_edge`

| 컬럼 | 용도 |
|---|---|
| `edge_id` | Spring 숫자 Edge ID |
| `from_node_id`, `to_node_id` | 연결 Node |
| `direction_type` | `BOTH`, `A_TO_B`, `B_TO_A` |
| `distance_m` | 현재 Spring `distance` 변환값 |
| `speed_limit_mps` | 이동속도 확장 |
| `nominal_travel_time_ms` | 권위 이동시간 확장 |
| `base_cost` | Solver 기본 비용 |
| `physical_resource_code` | 양방향 Edge의 실제 통로 공유 ID |
| `mobile_robot_traversable` | AMR 이동 가능 여부 |
| `graph_version` | Graph 일관성 |

### 3.6 Native G2P 확장 테이블

기존 Spring 코드가 읽지 않으므로 BE 무수정 상태에서도 안전하게 추가할 수 있습니다.

```text
rack
rack_access
rack_slot
handling_unit
outbound_order
outbound_order_line
inbound_receipt
facility
inventory_reservation
```

현재 `/optimize`·`/reoptimize` 호환 경로는 숫자 Node/Task 계약만 사용합니다. 이 확장 테이블은 추후 Handling Unit·G2P·입고 업무를 Native LARO에 연결할 때 사용합니다.

### 3.7 `request_log`

```text
/optimize와 /reoptimize 요청·응답 감사 로그
Graph Source
Runtime Source
상태
```

운영 경로 계산 실패와 감사 로그 저장 실패는 분리되어 있습니다. 감사 로그 저장 실패가 이미 계산된 유효 경로를 무효화하지 않습니다.

---

## 4. Spring 읽기 View

Spring Hibernate가 public 테이블을 만든 뒤 실행합니다.

```sql
SELECT laro_contract.refresh_spring_views();
```

생성 View:

```text
laro_contract.spring_warehouses_v
laro_contract.spring_route_nodes_v
laro_contract.spring_route_edges_v
```

View는 데이터를 복사하지 않고 public 테이블을 읽기 전용으로 변환합니다.

실행 스크립트:

```powershell
python .\scripts\refresh_spring_contract_views.py
```

Spring 시작 전 실행하면 오류가 아니라 `WAITING_FOR_SPRING_TABLES`를 반환합니다.

---

## 5. Graph Source 설정

### `auto` — 권장

```text
Spring Graph가 있고 요청 Hash와 같음
→ spring_db

Spring Graph가 없거나 요청과 다름
→ contract
```

### `spring_db`

```text
Spring public Graph를 강제 사용
Spring Graph가 없으면 실패
요청 Graph와 달라도 Spring DB를 권위값으로 사용
```

### `contract` 또는 `request_snapshot`

```text
Spring public Graph를 읽지 않고 Additive Contract Graph 사용
```

### Redis Graph Cache

```dotenv
BE_COMPAT_GRAPH_CACHE_MODE=metadata
```

| 값 | 동작 |
|---|---|
| `off` | Redis Graph Cache 없음 |
| `metadata` | Version·Node/Edge 개수·Source만 저장 |
| `full` | 장애 대비 전체 Graph JSON 저장. 중복이므로 명시적 선택일 때만 사용 |

---

## 6. Redis — 기존 Spring 원본 Key

Spring이 이미 쓰는 Key를 그대로 읽습니다.

```text
simulation:run:{runId}:robots
simulation:run:{runId}:robot:{robotId}:state
```

RobotState 예:

```json
{
  "robotId": 101,
  "warehouseId": 1,
  "currentNodeId": 17,
  "currentNodeCode": "R1_5",
  "nextNodeId": 18,
  "nextNodeCode": "R1_6",
  "arrivalInSeconds": 1.5,
  "batteryLevel": 78,
  "status": "MOVING",
  "currentTaskId": 5001,
  "updatedAt": "2026-07-29T10:00:00"
}
```

LARO Pydantic 모델은 Spring이 향후 필드를 추가해도 깨지지 않도록 알 수 없는 필드를 무시합니다.

---

## 7. Optional LARO Runtime Extension

기존 Spring `RobotState` JSON에 필드를 섞지 않고 별도 Key를 사용합니다.

```text
simulation:run:{runId}:meta
simulation:run:{runId}:robot:{robotId}:laro
```

Robot Extension 예:

```json
{
  "schemaVersion": 1,
  "simTimeMs": 18000,
  "stateVersion": 51,
  "activePlanId": "PLAN-001",
  "activePlanVersion": 2,
  "currentStepId": "R002-0005",
  "currentStepType": "MOVE",
  "stepStartAtMs": 17000,
  "stepEndAtMs": 19000,
  "currentEdgeCode": "H3_8",
  "fromNodeCode": "R3_8",
  "toNodeCode": "R3_9",
  "capacityUnits": 8,
  "currentLoadUnits": 1,
  "handlingUnitCode": "HU-001"
}
```

기존 Spring Java Record가 이 Key를 읽지 않으므로 역직렬화 충돌이 없습니다.

---

## 8. Edge Runtime

```text
simulation:run:{runId}:edges
simulation:run:{runId}:edge:{edgeId}:state
```

```json
{
  "schemaVersion": 1,
  "edgeId": 11,
  "edgeCode": "H3_8",
  "status": "BLOCKED",
  "costMultiplier": 1.0,
  "travelTimeMultiplier": 1.0,
  "stateVersion": 4
}
```

차단 판정:

```text
BLOCKED
CLOSED
MAINTENANCE
```

Edge Key가 없으면 기본값 `OPEN`, 배율 `1.0`입니다.

---

## 9. Key 누락 의미

| 상태 | 처리 |
|---|---|
| Robot Set 없음 | Runtime 미초기화 (`NOT_INITIALIZED`) |
| Set에는 Robot ID가 있으나 State 없음 | 해당 Robot만 비가용 |
| Robot `:laro` 없음 | `COMPATIBILITY` 모드, Node 기준 재최적화 |
| Edge State 없음 | `OPEN`, 배율 `1.0` |
| Run Meta 없음 | 정확한 `simTimeMs`·Plan Version 없음 |
| Reservation 없음 | 예약 없음 |

필드 누락을 임의 값으로 채워 실행하지 않고, 지원 범위를 명시적으로 낮춥니다.

---

## 10. Runtime Source 설정

```dotenv
BE_COMPAT_RUNTIME_SOURCE=request_then_redis
```

| 값 | 동작 |
|---|---|
| `request_only` | Spring이 LARO Body에 넣은 Robot·blockedEdgeIds만 사용 |
| `request_then_redis` | Robot Body가 있으면 권위값, 없으면 Redis. Edge 차단은 Redis Overlay |
| `redis_only` | Spring Redis를 권위값으로 사용 |

기존 Spring `ReoptimizationService`는 Robot 목록을 Body에 넣으므로 기본 동작은 요청 Robot이 권위값입니다.

---

## 11. Neo4j Projection

정적 Graph의 권위 저장소가 아니라 재생성 가능한 읽기 Projection입니다.

```text
(:RouteNode:BECompatNode)-[:TRAVERSES]->(:RouteNode:BECompatNode)
```

Node에는 Spring 숫자 ID와 Graph Version을 저장하고, `BOTH` Edge는 두 개의 Directed `TRAVERSES` 관계로 펼칩니다.

```text
PostgreSQL 또는 Contract Graph
→ 권위 Graph

Neo4j
→ 경로·관계 조회 Projection
```

Projection 실패는 현재 호환 경로의 최단 경로 계산 결과를 무효화하지 않습니다.

---

## 12. 적용 순서

```powershell
# 1. LARO DB/API Stack
Copy-Item .env.docker.example .env.docker
.\scripts\start_be_compat_docker.ps1 -ResetData

# 2. 기존 Spring BE 실행
cd ..\BE-main
.\gradlew.bat bootRun

# 3. Spring public 테이블 View 생성
cd ..\LARO-fastapi
python .\scripts\refresh_spring_contract_views.py

# 4. DB·Runtime 계약 확인
python .\scripts\check_be_shared_db_contract.py `
  --warehouse-id 1 `
  --simulation-run-id 77
```

---

## 13. 더 좋은 장기 구조

BE를 최소 수정할 수 있을 때는 다음이 더 좋습니다.

```text
Spring RobotState v2에 simTimeMs/currentStep/load 통합
Spring이 Active Plan과 Runtime의 유일한 Writer
Handling Unit·Order를 Spring public 업무 테이블로 승격
Plan JSONB와 Version Chain을 Spring PostgreSQL에 저장
LARO는 완전 Read-only Planner
```

현재 v2는 그 이전 단계로, **BE-main 무수정 조건에서 데이터 중복을 줄이고 Key 누락 의미를 명확히 한 Additive 호환 구조**입니다.

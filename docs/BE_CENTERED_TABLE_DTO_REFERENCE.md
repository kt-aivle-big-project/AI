# BE 중심 테이블·DTO 상세 설명

## 1. 기존 BE 권위 테이블

### public.warehouse_layout

창고 자체를 나타낸다. 숫자 `id`는 Spring의 FK이고, `laro_ext.warehouse_profile.warehouse_code`가 `WH-001` 형식의 LARO 외부 코드를 보충한다.

주요 필드:
- `id`: 창고 숫자 PK
- `name`, `width`, `height`: 화면과 지도 기본 정보
- `status`: ACTIVE/MAINTENANCE/INACTIVE
- `user_id`: 소유자

### public.warehouse_node

BE와 LARO가 공유하는 모든 물리 Node 원본이다.

- `node_id`: 숫자 PK
- `warehouse_id`: 소속 창고
- `node_code`: `R1_5`, `K1_7_ACCESS_A`처럼 API·Neo4j에서 쓰는 코드
- `node_type`: 기존 BE가 이해하는 큰 분류
- `x`, `y`: 화면 좌표

LARO 전용 의미는 `laro_ext.node_profile`에 둔다.

### public.warehouse_edge

Node 사이 연결 원본이다.

- `edge_id`: 숫자 PK
- `edge_code`: `H2_6`, `RA_K1_7_A_IN` 같은 외부 코드
- `from_node_id`, `to_node_id`: 연결 Node
- `distance`: 물리 거리
- `direction_type`: BOTH/A_TO_B/B_TO_A

LARO 속도·시간·물리 Resource는 `laro_ext.edge_profile`에 둔다.

### public.storage_location

기존 BE의 저장 위치다. 현재 BE는 Node와 1:1이므로 선반 층은 `laro_ext.rack_slot`으로 보충한다.

### public.product

품목 마스터다.

- `product_id`: 숫자 Item ID
- `product_code`: `ITEM-001` 같은 API 코드
- `product_name`: 화면 이름

Structured Operation은 `itemId` 또는 `productCode` 중 하나를 사용한다.

### public.warehouse_items

실제 출고 가능한 재고 원본이다.

- `warehouse_item_id`: 계획에서 정확한 출발 재고를 지정할 때 사용
- `warehouse_id`, `storage_location_id`, `node_id`: 위치
- `item_id`: Product FK
- `quantity`: 현재 수량
- `inbound_quantity`, `outbound_quantity`: BE 집계
- `expiry_date`, `received_at`: 재고 정책 근거

LARO는 이 행을 요청 동안 `WI-{warehouse_item_id}` Inventory Unit으로 취급한다. 별도 Handling Unit 테이블은 없다.

### public.robot_specs / public.robot

`robot_specs`는 모델 능력, `robot`은 창고에 배치된 로봇이다. 현재 위치·배터리의 권위값은 Redis다. `laro_ext.robot_profile`은 Solver에 필요한 용량·속도·최저 배터리를 보충한다.

### public.simulation_runs

숫자 `simulationRunId`가 선택하는 실행 원본이다. 이 행에서 창고 ID와 실행 상태를 찾고, 동일 ID의 Redis Runtime을 읽는다.

### public.task

기존 BE가 생성·실행하는 물리 업무 기록이다. 새 계획 API는 Task 테이블을 Order 마스터로 재해석하지 않는다. BE가 Task를 구조화 Operation으로 변환해 요청에 넣을 수 있으며 `taskId`로 추적 관계를 보존한다.

### public.charging_station

기존 충전 시설은 그대로 사용한다. 별도 LARO 충전소 테이블을 만들지 않는다.

## 2. 필수 LARO 확장 테이블

### laro_ext.warehouse_profile

BE 창고에 LARO 코드와 버전을 붙인다.

```text
warehouse_id     PK/FK → warehouse_layout.id
warehouse_code   WH-001 형식, Unique
map_version      지도 변경 버전
inventory_version 재고 계약 버전
facility_version 시설 계약 버전
active           Planner 사용 여부
```

### laro_ext.node_profile

기존 Node에 Planner 의미를 보충한다.

```text
node_id          PK/FK
semantic_type    RACK_ACCESS, OUTBOUND_STATION_ACCESS 등
service_only     Service 접근점 여부
transit_allowed  통과 가능 여부
holding_allowed  WAIT 가능 여부
node_capacity    동시 점유 수
resource_type/code 실제 Rack·Station 연결
side             A/B 접근 방향
```

### laro_ext.edge_profile

기존 Edge에 Solver·MAPF 속성을 보충한다.

```text
edge_id                       PK/FK
speed_limit_mps               속도 제한
nominal_travel_time_ms        기본 이동시간
base_cost                     Solver 비용
physical_resource_code        반대 방향 Edge를 같은 물리 통로로 묶는 코드
service_only                  Service Spur 여부
mobile_robot_traversable      AMR 통행 가능 여부
version                       변경 버전
```

### laro_ext.rack_slot

기존 StorageLocation만으로 표현하지 못하는 Rack 층을 나타낸다.

```text
rack_slot_id       PK
warehouse_id       창고 FK
rack_node_id       Rack 본체 Node FK
rack_level         층
storage_location_id 기존 BE 위치와 연결
capacity/status/version 계획용 상태
```

### laro_ext.warehouse_item_profile

기존 `warehouse_items` 행에만 필요한 계획 속성이다.

```text
warehouse_item_id  PK/FK
rack_level         해당 재고가 놓인 층
capacity           Inventory Unit 최대 수량
planning_status    STORED/RESERVED 등
version            예약 충돌 검사용 버전
```

### laro_ext.robot_profile

```text
robot_spec_id                    PK/FK
capacity_units                   Solver 용량
nominal_speed_mps                이동 속도
minimum_operating_battery_pct    후보 제외 기준
max_load_weight                  선택적 중량 한계
```

### laro_ext.facility

BE에 없는 입고 Handoff, 출고 Station, 빈 Tote Buffer, Parking 등의 마스터다. 모든 이동 위치는 기존 `warehouse_node` 또는 `access_node_codes`로 연결한다.

### laro_ext.inventory_reservation

계획 중 같은 `warehouse_items` 수량을 중복 선택하지 않도록 예약한다.

```text
reservation_id
simulation_run_id
warehouse_item_id
operation_id
reserved_quantity
expected_item_version
status ACTIVE/COMMITTED/RELEASED/CANCELLED
```

실제 수량 차감은 Spring BE가 Commit한다. 계획이 검증되면 LARO는 선택된
`warehouse_items` 행을 `ACTIVE`로 예약하며, 같은 물리 재고 행은 다른 신규
Plan 후보에서 제외한다. 동일 `requestId` 재전송은 기존 응답을 반환하여 예약을
중복 생성하지 않는다.

### laro_ext.simulation_plan

LARO의 MOVE/WAIT/SERVICE 결과를 SimulationRun에 연결해 저장한다.

```text
plan_id
simulation_run_id / warehouse_id
plan_version / base_plan_id / supersedes_plan_id
status / plan_kind
planning_mode / optimization_backend
map_version / runtime_version / makespan_ms
request_json / plan_json / trace_json
```

### laro_ext.request_log

구조화 입력과 응답을 감사·재현 목적으로 저장한다. 이것은 Order 마스터가 아니라 API 호출 로그다.

### laro_ext.contract_meta

현재 계약이 `REQUEST_STRUCTURED_INPUT`, `PUBLIC_WAREHOUSE_ITEMS`, `orders_table_used=false`, `handling_units_table_used=false`임을 기록한다.

## 3. 요청 DTO

### Spring 공개 DTO

`LaroPlanRequest`

```text
structuredInput    필수 업무 데이터
userCommand        선택 자연어 운영 명령
optimizationBackend ortools/cuopt/cuopt_payload_only
runtimeSnapshot    테스트에서만 선택
```

### StructuredOperation

```text
operationId            업무 추적 ID
operationType          OUTBOUND/INBOUND/TRANSFER/CHARGE/PARK
taskId                 기존 BE Task와 연결할 때 사용
itemId/productCode     Product 식별
quantity/priority      업무 수량·우선순위
sourceWarehouseItemId  특정 BE 재고 행을 강제할 때 사용
source*                출발 위치·시설
destination*           도착 위치·시설
targetRackLevel        입고 층
releaseAtMs            업무 Release 시간
pickup/dropServiceTimeMs 작업 시간
attributes             추가 설명
```

## 4. 별도로 만들지 않는 테이블

```text
orders
order_lines
handling_units
inbound_receipts 업무 마스터
```

업무는 Request가 제공하고, 재고는 `warehouse_items`, 입고 목적지는 Structured Operation이 제공한다. Batch·Task·Inventory Unit은 계획 과정에서 계산되는 파생 데이터이며 Plan JSON과 Trace에 남는다.

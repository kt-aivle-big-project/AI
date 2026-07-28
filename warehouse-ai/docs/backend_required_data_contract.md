# AI 물류 계획 시스템 필수 데이터 계약

## 1. 공통 원칙

AI 계획 시스템은 PostgreSQL, Neo4j, Redis의 원본 구조를 직접 기준으로 삼지 않고,
한 시점의 공통 `PlanningSnapshot`을 입력으로 사용합니다.

```text
PostgreSQL + Neo4j + Redis
              ↓
       PlanningSnapshot
              ↓
최적화 → 경로 계산 → 시뮬레이션 → 검증
```

모든 저장소에서 의미가 일치해야 하는 공통 식별값:

- `warehouse_id`
- `robot_id`
- `node_id`
- `work_id`
- `task_id`
- `item_id`
- `plan_version`
- `simulation_id`

## 2. PostgreSQL 필수 데이터

PostgreSQL은 주문, 작업, 실제 재고, 완료 이력의 기준 저장소입니다.

### 로봇 기본정보

| 필드 | 필수 | 설명 |
|---|---:|---|
| robot_id | O | 로봇 고유 번호 |
| warehouse_id | O | 소속 창고 |
| node_id | O | 마지막 확정 위치 |
| battery | O | 배터리 잔량 |
| status | O | AVAILABLE, BUSY, FAILED 등 |
| max_load | O | 최대 적재량 |
| current_load | O | 현재 적재량 |
| version | O | 동시 변경 확인용 |

### 작업

| 필드 | 필수 | 설명 |
|---|---:|---|
| work_id | O | 업무 고유 번호 |
| warehouse_id | O | 창고 번호 |
| item_id | 조건부 | 품목 작업이면 필수 |
| quantity_boxes | 조건부 | 재고 작업이면 필수 |
| source_node_id | O | 출발 위치 |
| target_node_id | O | 목적 위치 |
| operation_type | O | INBOUND, OUTBOUND, MOVE, CHARGE |
| priority | O | 작업 우선순위 |
| status | O | 현재 작업 상태 |
| required_at | 조건부 | 마감시간이 있으면 필수 |
| assigned_robot_id | 선택 | 기존 배정 로봇 |
| version | O | 동시 변경 확인용 |

### 재고와 lot

| 필드 | 필수 | 설명 |
|---|---:|---|
| warehouse_item_id | O | 창고 재고 고유 번호 |
| warehouse_id | O | 창고 번호 |
| item_id | O | 품목 번호 |
| lot_id | O | lot 번호 |
| storage_node_id | O | 저장 위치 |
| quantity_boxes | O | 현재 수량 |
| reserved_quantity_boxes | O | 다른 계획 예약 수량 |
| status | O | 사용 가능 상태 |
| available_at | O | 사용 가능 시각 |
| expiration_at | 선택 | 유효기간 |
| version | O | 동시 변경 확인용 |

### 품목

- `item_id`
- `item_name`
- `base_unit`
- `active`

### 기능에 따라 추가로 필요한 데이터

- 입고 예정: 품목, 수량, 도착 예정 시각, 사용 가능 예정 시각, 저장 위치
- 출고 주문: 품목, 요청 수량, 마감 시각, 우선순위, 부분 출고 허용 여부
- 작업 선후관계: 선행 작업과 후행 작업
- 작업 시간 제한: 시작 가능 시각, 완료 제한 시각, 고정 로봇

## 3. Neo4j 필수 데이터

Neo4j는 창고 공간과 이동 가능 관계의 기준 저장소입니다.

### 노드

- `warehouse_id`
- `node_id`
- `external_node_id` 선택
- `node_type`
- `x`
- `y`
- `zone_id`
- `active`

최소 노드 종류:

- ROUTE
- INTERSECTION
- STORAGE
- INBOUND
- OUTBOUND
- CHARGER
- CHARGER_WAITING_AREA

### 통로

- `edge_id`
- `from_node_id`
- `to_node_id`
- `distance`
- `travel_seconds`
- `direction`
- `active`
- `width` 또는 동시 사용 가능 수

### 충전·공동 공간 기능 사용 시

- 충전기 수용 수
- 충전 속도
- 대기 공간 수용 수
- 허용 대기 시간
- 연결된 충전기 노드

Neo4j에는 실제 재고 수량, 주문 상태, 현재 로봇 상태를 기준 데이터로 저장하지 않습니다.

## 4. Redis 필수 데이터

Redis는 현재 순간의 빠르게 변하는 운영 상태를 담당합니다.

### 운영 상태

- 로봇 현재 위치
- 로봇 현재 배터리
- 로봇 현재 상태
- 실행 중 작업
- 계획된 작업
- 현재 활성 계획
- 일시 통행 제한
- 재고 예약
- 실시간 이벤트

### 시뮬레이션 상태

실제 운영 key와 분리합니다.

```text
sim:{simulation_id}:robots
sim:{simulation_id}:inventory
sim:{simulation_id}:works
sim:{simulation_id}:plan
sim:{simulation_id}:events
```

Redis가 초기화돼도 PostgreSQL과 실행 이력으로 복구할 수 있어야 합니다.

## 5. PlanningSnapshot 최소 필드

```json
{
  "snapshot_id": "SNAP-001",
  "captured_at": "2026-07-27T07:20:00Z",
  "warehouse_id": 2,
  "map_version": "MAP-001",
  "sql_data_version": "SQL-152",
  "runtime_state_version": "REDIS-338",
  "robots": [],
  "works": [],
  "inventory": [],
  "items": [],
  "nodes": [],
  "edges": [],
  "active_plan": {},
  "temporary_closures": [],
  "inventory_reservations": []
}
```

## 6. 회사 SQL 구조가 다를 때

MVP에서는 회사별 Repository 또는 SQL View가 회사 데이터를 위 공통 형식으로
변환합니다. 어떤 구조든 자동 추론하여 변환하는 범용 매핑 기능은 고도화 범위입니다.

```text
회사 DB 구조
   ↓ 회사별 Adapter / SQL View
공통 PlanningSnapshot
   ↓
AI 계획 시스템
```

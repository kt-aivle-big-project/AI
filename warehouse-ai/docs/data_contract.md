# 데이터 계약

실제 ERD가 다르면 `app/repositories/postgres.py`의 쿼리만 맞춥니다.

## PostgreSQL

- `warehouse_items(warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version)`
- Migration 010 이후 `warehouse_items`는 `status`, `received_at`,
  `available_at`, `expiration_at`, `base_unit`을 추가로 가진다. 현재 확정 Lot은
  `status='AVAILABLE'`이면서 `available_at <= now()`인 행만 사용한다.
- `inventory_item(item_id, item_name, base_unit, active)`의 MVP `base_unit`은
  `BOX`뿐이다.
- `inbound_order_line`은 도착 시각과 가용 시각을 분리한다. 재고 계산은
  `actual_available_at`을 우선하고 없으면 `expected_available_at`을 사용한다.
- `outbound_order_line`은 요청 BOX 수량, required_by, priority,
  allow_partial_fulfillment를 보존한다.
- `inventory_movement`는 완료 입출고를 idempotency_key로 중복 방지하는
  append-only 감사 원장이다. 현재 재고를 만들 때 movement를 다시 replay하지
  않는다.
- Redis `wh:{warehouse_id}:inventory:reservations`는 ACTIVE_PLAN 예약의 전역
  가용 수량을 차단한다. SIMULATION 예약은 SQL이나 이 전역 합계에 포함하지
  않고 `simulation_id` 결과와 `sim:{simulation_id}:*`에만 남는다.
- `robot(robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version)`
- `works(work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version)`
- `simulation_run(run_id, simulation_id, command_id, warehouse_id, plan_version, status, input_payload, output_payload, current_state, checkpoint, created_at)`
- `work_event(event_id, work_id, robot_id, event_type, payload, occurred_at)`

실제 실행 승인 시 재고를 영구 예약하려면 `inventory_reservation`과 outbox 테이블을 추가하고, PostgreSQL 예약 트랜잭션과 Redis 계획 활성화를 Saga/outbox 방식으로 연결하는 것을 권장합니다.

## Neo4j

```text
(Warehouse)-[:HAS_ZONE]->(Zone)-[:HAS_NODE]->(MapNode)
(MapNode)-[:CONNECTED_TO]->(MapNode)
```

`MapNode`:

- `warehouse_id`, `node_id`, `node_type`, `x`, `y`, `active`
- `charging_cost` (선택): `node_type=CHARGER` 후보 간 비교에 사용하는 숫자 비용.
  낮을수록 우선하며 단위는 창고 운영 정책에서 동일하게 정의해야 한다. 입력 JSON은
  `charging_cost`, `charge_cost`, `charger_cost`, `price_per_percent`, `cost` 별칭을
  허용하지만 Neo4j에는 `charging_cost`로 저장한다. 후보 전체에 값이 없으면 거리 기준
  fallback을 사용하고 결과에 `CHARGER_COST_DATA_MISSING` 경고를 남긴다.

`CONNECTED_TO`:

- `distance`, `travel_seconds`, `direction`, `width`, `active`

로봇 현재 위치와 재고 수량은 Neo4j에 중복 저장하지 않습니다.

## 지도 JSON

노드:

```json
{
  "node_id": 1,
  "zone_id": "A",
  "node_type": "AISLE",
  "x": 0.0,
  "y": 0.0,
  "active": true,
  "charging_cost": 1.0
}
```

간선:

```json
{
  "from_node": 1,
  "to_node": 2,
  "distance": 5.0,
  "travel_seconds": 4.0,
  "direction": "BOTH",
  "width": 1.8,
  "active": true
}
```

## Optimizer 계약

`OPTIMIZER_BACKEND=local`은 `app.services.local_optimizer.LocalOptimizer`가 동일한 `CuOptPlan` 계약을 반환합니다. 외부 서비스나 NVIDIA GPU가 필요하지 않습니다.

`OPTIMIZER_BACKEND=cuopt`일 때만 아래 HTTP 어댑터를 호출합니다.

`POST {CUOPT_URL}/optimize`

응답은 `app.models.CuOptPlan` 형식이어야 합니다.

```json
{
  "scheduled_tasks": [
    {
      "task_id": "TASK-1",
      "work_id": "WORK-1",
      "robot_id": "R1",
      "source_node": 10,
      "target_node": 20,
      "start_time_step": 0,
      "end_time_step": 6,
      "priority": 1
    }
  ],
  "unassigned_task_ids": [],
  "changed_robot_ids": ["R1"],
  "objective_value": 12.5,
  "metadata": {}
}
```

## Routing 계약

- `ROUTING_BACKEND=internal`: Neo4j Snapshot 그래프를 사용하는 Prioritized Space-Time 경로기
- `ROUTING_BACKEND=mapf`: `POST {MAPF_URL}/plan` 외부 서비스

두 백엔드는 모두 `CollisionFreePlan`을 반환합니다. 내부 경로기는 WAIT, vertex conflict, edge-swap conflict, 폐쇄 노드·간선, freeze horizon 예약을 처리합니다.

## Redis

- `wh:{warehouse_id}:robots`
- `wh:{warehouse_id}:robot:{robot_id}`
- `wh:{warehouse_id}:tasks:executing`
- `wh:{warehouse_id}:tasks:planned`
- `wh:{warehouse_id}:task:{task_id}`
- `wh:{warehouse_id}:active_plan_version`
- `wh:{warehouse_id}:plan:{version}`
- `wh:{warehouse_id}:temporary_closures`
- `wh:{warehouse_id}:events` Redis Stream

실제 실행 상태와 분리된 시뮬레이션 세션:

- `sim:{simulation_id}:inventory` 가상 재고 JSON
- `sim:{simulation_id}:robots` 가상 로봇 JSON
- `sim:{simulation_id}:works` 가상 작업 JSON
- `sim:{simulation_id}:events` 가상 이벤트 Redis Stream 및 checkpoint

`REAL` 이벤트만 `wh:{warehouse_id}:*`와 실제 PostgreSQL `works`, `warehouse_items`를 변경할 수 있습니다. `SIMULATION` 이벤트는 해당 `sim:{simulation_id}:*`와 PostgreSQL `simulation_run.current_state/checkpoint`만 변경할 수 있습니다. 재고 수량 전이 규칙은 두 컨텍스트 모두 `calculate_inventory_transition()`을 사용합니다.

계획 활성화 Lua 스크립트는 Snapshot에서 읽은 기존 `active_plan_version`과 현재 값을 비교합니다. 값이 달라진 오래된 계획은 `STALE_PLAN_VERSION`으로 거절합니다.

## P16.5.7 idle node fields

장기 대기를 허용하는 지도 노드는 다음 필드를 사용할 수 있습니다.

```json
{
  "node_id": 2160,
  "node_type": "PARKING",
  "idle_allowed": true,
  "idle_capacity": 1,
  "max_idle_seconds": null,
  "linked_charger_node_id": null,
  "parking_priority": 1
}
```

`node_type`이 `PARKING`, `STAGING`, `HOLDING`, `CHARGER_WAITING_AREA`, `ROBOT_PARKING` 중 하나이면 기본적으로 장기 대기가 허용됩니다. 다른 타입은 `idle_allowed=true`가 명시된 경우에만 허용됩니다.

`CHARGER`는 충전 슬롯이며 일반 대기 노드가 아닙니다.

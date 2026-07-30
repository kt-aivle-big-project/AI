# backend_laro PostgreSQL Adapter

The Spring HTTP compatibility endpoints and startup order are documented in
`docs/be_main_http_integration.md`.

## Profile selection

The default preserves the existing AI-owned schema:

```dotenv
POSTGRES_SCHEMA_PROFILE=legacy_ai
```

Select the backend read model explicitly:

```dotenv
POSTGRES_SCHEMA_PROFILE=backend_laro
```

Only `legacy_ai` and `backend_laro` are accepted. Pydantic settings validation
rejects any other value during application startup.

## Mapping

| Backend source | Planning snapshot field | Policy |
| --- | --- | --- |
| `robot.robot_id` | `robots[].robot_id` | Convert to `str` |
| `robot_specs.robot_code` | `robots[].robot_code` | Required join |
| `robot.warehouse_id` | `robots[].warehouse_id` | Direct |
| `robot.node_id` | `robots[].node_id` | Direct |
| `robot.battery` | `robots[].battery` | Convert to `float` |
| `robot.status` | `robots[].status` | Direct |
| No backend column | `max_load`, `current_load`, `version` | `0`, `0`, `1` |
| `warehouse_items.warehouse_item_id` | `inventory[].warehouse_item_id` | Convert to `str` |
| `product.product_code` | `inventory[].item_id` | Required `product` join |
| `warehouse_items.node_id` | `inventory[].node_id` | Direct |
| `warehouse_items.quantity` | `quantity`, `available_quantity` | Direct |
| No backend column | `reserved_quantity` | `0` |
| No backend column | `lot_id` | `BACKEND-{warehouse_item_id}` |
| No backend column | status/unit/version | `AVAILABLE`, `BOX`, `1` |
| `warehouse_items.received_at` | `received_at`, `available_at` | Direct |
| `warehouse_items.expiry_date` | `expiry_date` | Direct |
| No backend column | `expiration_at` | `null` |
| `storage_location.max_quantity` | `capacity_value` | Sum across locations |
| Computed location remainder | `usable_capacity_value` | Sum across locations |

`storage_capacity.locations` also retains each location's `node_id`,
`max_quantity`, `occupied_quantity`, `available_quantity`, and `status`.

The map Adapter uses the backend Entity contracts directly:

| Backend node/edge | AI Neo4j contract |
| --- | --- |
| `ROUTE` | `ROUTE` |
| `ROUTE_CHARGE_JUNCTION` | `INTERSECTION` |
| `RACK_STORAGE` | `STORAGE` |
| `CHARGING_SLOT` | `CHARGER` |
| `A_TO_B` | One-way `from_node_id → to_node_id` |
| `B_TO_A` | One-way `to_node_id → from_node_id` |
| `BOTH` | Bidirectional edge contract |
| `charging_station.charging_power` | `charger_power_kw` |

The backend profile does not infer mappings for orders, works, dependencies, or
schedule constraints. Those lists are empty, and the SQL snapshot includes:

```json
{
  "code": "BACKEND_LARO_TASK_MAPPING_NOT_CONFIGURED",
  "profile": "backend_laro",
  "persistence": "READ_ONLY_NOT_CONFIGURED"
}
```

The adapter is read-only. It does not expose legacy completion/inventory
mutation methods. AI audit/simulation persistence is a no-op in this profile,
and `EXECUTE` approval is blocked until a backend-owned persistence contract is
defined. `PLAN_ONLY` remains available for natural-language planning.

## Validation and Redis bootstrap

Read-only ID validation:

```powershell
python -m scripts.validate_backend_laro_snapshot --warehouse-id 1
```

Neo4j map and Redis robot upserts both default to dry-run:

```powershell
python -m scripts.sync_backend_map_to_neo4j --warehouse-id 1
python -m scripts.sync_backend_map_to_neo4j --warehouse-id 1 --apply
python -m scripts.seed_backend_robots_to_redis --warehouse-id 1
python -m scripts.seed_backend_robots_to_redis --warehouse-id 1 --apply
```

The Neo4j sync creates or updates the AI-owned
`Warehouse → Zone → MapNode`/`CONNECTED_TO` view. The backend's existing
`GraphSyncService` writes `WarehouseNode` with camelCase properties, which is
not the graph shape consumed by `Neo4jRepository`.

The apply mode only uses the existing `wh:{warehouse_id}:robots` set and
`wh:{warehouse_id}:robot:{robot_id}` hashes. It does not delete keys or call
`FLUSHDB`.

The supplied backend Seed was inspected but not executed. It contains 159 map
nodes, 218 edges, 10 charging stations, 6 robots, 5 products, 48 storage
locations, and 10 initial inventory rows. It contains no task rows, so task,
order, dependency, and scheduling mappings remain intentionally disabled.

## Swagger PLAN_ONLY request

`POST /v1/planning/commands`

```json
{
  "warehouse_id": 1,
  "text": "C 상품 1 BOX를 재고 노드 90에서 출고 노드 143으로 이동하는 계획을 만들어줘.",
  "requested_execution_mode": "PLAN_ONLY",
  "source": "USER",
  "report_detail_level": "DEBUG",
  "response_view": "FULL"
}
```

## Rollback

Remove the Adapter/factory, validation/bootstrap scripts, tests, and this
document; restore `ServiceContainer` to instantiate `PostgresRepository`
directly; remove `postgres_schema_profile` from settings and
`POSTGRES_SCHEMA_PROFILE` from `.env.example`. No database rollback is needed
because this change contains no migrations or destructive data operations.

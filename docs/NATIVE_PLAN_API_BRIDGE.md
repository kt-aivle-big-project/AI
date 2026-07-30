# Native LARO Plan API Bridge

## 1. Purpose

This build keeps the current Spring BE compatibility endpoint exactly as-is:

```text
POST /optimize
POST /reoptimize
```

At the same time it enables the LARO-native planning endpoint that is intended to replace `/optimize` later:

```text
POST /api/v1/warehouses/{warehouse_id}/missions/plan
```

The current goal is **communication and planning-pipeline verification**, not BE migration. `BE-main` source code is unchanged.

```text
Existing Spring path
Spring OptimizationClient -> POST /optimize -> legacy nodePath response

Future native path under test
caller -> POST /api/v1/warehouses/WH-001/missions/plan
       -> PostgreSQL orders/inventory/facilities
       -> Redis robot/runtime state
       -> Neo4j RouteNode/TRAVERSES graph
       -> Rule or Agent formulation
       -> OR-Tools or NVIDIA cuOpt
       -> MAPF
       -> MOVE / WAIT / SERVICE SimulationPlan
```

`replan` remains in the codebase but is not part of this bridge verification.

---

## 2. Shared Docker DB layout

The same PostgreSQL, Redis, and Neo4j servers can contain the current Spring data and the native LARO demo without namespace collisions.

### PostgreSQL

Spring continues to use its public tables, for example:

```text
public.warehouse_layout
public.warehouse_node
public.warehouse_edge
public.robot
public.task
```

The existing compatibility layer uses:

```text
laro_contract.*
```

The native plan API uses the existing LARO tables:

```text
warehouses
warehouse_meta
racks
rack_slots
handling_units
orders
order_lines
inbound_receipts
inbound_handoffs
inbound_ports
outbound_chutes
outbound_stations
station_robots
empty_tote_buffers
```

The data represents different API contracts; no Spring source file is changed.

### Redis

Spring runtime keys remain:

```text
simulation:run:{runId}:*
```

The native plan demo uses a separate namespace:

```text
laro:warehouse:WH-001:sim:SIM-V18-MIXED:*
```

### Neo4j

Spring graph projection may use:

```text
(:WarehouseNode)-[:CONNECTED_TO]->(:WarehouseNode)
```

The native plan API uses:

```text
(:RouteNode)-[:TRAVERSES]->(:RouteNode)
```

The native demo is the 220-node / 356-directed-edge Access-Node graph. Rack entities such as `K1_7` are not traversable nodes; robots use `K1_7_ACCESS_A` or `K1_7_ACCESS_B`.

---

## 3. First execution

From `LARO-fastapi`:

```powershell
Copy-Item .env.docker.example .env.docker

.\scripts\start_be_compat_docker.ps1 `
  -ResetData `
  -StopLegacy
```

The script performs the following steps:

```text
1. Start PostgreSQL, Redis, Neo4j, and FastAPI.
2. Apply db/postgres/001_schema.sql for the native planner.
3. Apply db/postgres/003_be_shared_contract.sql for /optimize compatibility.
4. Seed scenarios/fixtures/V18_mixed_inbound_outbound.
5. Load native robot runtime into Redis SIM-V18-MIXED.
6. Load the 220/356 RouteNode/TRAVERSES graph into Neo4j.
7. Run the read-only native-plan preflight.
```

The Spring seed is optional and independent. The native plan check does not require Spring to be running.

---

## 4. Preflight

```powershell
Invoke-RestMethod `
  "http://localhost:8000/api/v1/warehouses/WH-001/missions/plan/preflight?simulation_id=SIM-V18-MIXED" |
  ConvertTo-Json -Depth 20
```

Expected core values:

```json
{
  "status": "READY",
  "ready": true,
  "warehouse_id": "WH-001",
  "simulation_id": "SIM-V18-MIXED",
  "postgres": {
    "ok": true,
    "counts": {
      "racks": 48,
      "handling_units": 8,
      "orders": 5,
      "inbound_receipts": 2,
      "outbound_stations": 2,
      "empty_tote_buffers": 1
    }
  },
  "redis": {
    "ok": true,
    "robot_count": 3
  },
  "neo4j": {
    "ok": true,
    "node_count": 220,
    "edge_count": 356,
    "node_label": "RouteNode",
    "relationship_type": "TRAVERSES"
  },
  "problems": []
}
```

`READY` means the plan endpoint has all three required data sources. It does not create a plan.

---

## 5. One-command request/response inspection

```powershell
.\examples\powershell\call_native_plan.ps1 -Backend ortools
```

This calls preflight, POSTs the plan request, prints the full response, calls the compact trace endpoint, and prints a final status/plan/step summary.

---

## 6. Structured plan request

The initial communication check uses `force_rule + ortools`, so no OpenAI or NVIDIA key is required.

```powershell
$body = @{
  warehouse_id = "WH-001"
  simulation_id = "SIM-V18-MIXED"
  optimization_backend = "ortools"
  events = @(
    @{
      type = "new_order"
      order_id = "ORD-001"
    },
    @{
      type = "inbound_item_arrived"
      inbound_id = "IN-001"
    }
  )
} | ConvertTo-Json -Depth 20

$response = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/warehouses/WH-001/missions/plan" `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

Check the compact response:

```powershell
$response.status
$response.final_route
$response.effective_planning_mode
$response.router_llm_executed
$response.plan.plan_id
$response.plan.plan_version
$response.plan.makespan_ms
$response.plan.robots | ConvertTo-Json -Depth 30
```

Expected values for the keyless communication check:

```text
status                    plan_validated
final_route               RULE_FORMULATION
effective_planning_mode   force_rule
router_llm_executed       False
plan.plan_version         1
plan.robots               one or more robot plans
```

The response schema is:

```json
{
  "api_version": "v1",
  "status": "plan_validated",
  "warehouse_id": "WH-001",
  "simulation_id": "SIM-V18-MIXED",
  "request_mode": "event_driven",
  "final_route": "RULE_FORMULATION",
  "effective_planning_mode": "force_rule",
  "planning_mode_source": "environment",
  "router_llm_executed": false,
  "plan": {
    "plan_id": "PLAN-WH-001-SIM-V18-MIXED-1-...",
    "plan_version": 1,
    "map_version": "...",
    "sim_tick_ms": 100,
    "makespan_ms": 64670,
    "robots": [
      {
        "robot_id": "R002",
        "initial_node": "R1_5",
        "available_at_ms": 0,
        "finish_at_ms": 64670,
        "steps": [
          {
            "step_id": "R002-0001",
            "sequence": 1,
            "step_type": "MOVE",
            "start_at_ms": 0,
            "end_at_ms": 1925,
            "edge_id": "...",
            "from_node": "...",
            "to_node": "..."
          },
          {
            "step_id": "R002-0002",
            "sequence": 2,
            "step_type": "SERVICE",
            "start_at_ms": 1925,
            "end_at_ms": 3125,
            "node_id": "...",
            "task_id": "...",
            "service_kind": "PICKUP"
          }
        ]
      }
    ],
    "station_reservations": [],
    "logical_operations": [],
    "handover_points": []
  },
  "errors": []
}
```

Times and assignments depend on the solver result; the field structure is stable.

---

## 7. Plan-stage trace

After creating a plan:

```powershell
$planId = $response.plan.plan_id

$trace = Invoke-RestMethod `
  "http://localhost:8000/api/v1/warehouses/WH-001/missions/plans/$planId/trace"

$trace.workflow_trace
$trace.checks | ConvertTo-Json -Depth 10
$trace.nodes | Format-Table node_name, status, duration_ms, llm_used, error_code
```

Expected checks:

```json
{
  "structured_keys_valid": true,
  "dynamic_input_valid": true,
  "payload_valid": true,
  "candidate_space_valid": true,
  "assignment_valid": true,
  "route_valid": true,
  "mapf_valid": true
}
```

The full persisted orchestration result is still available at:

```text
GET /api/v1/warehouses/WH-001/missions/plans/{plan_id}/debug
```

The compact trace endpoint is recommended for BE integration checks because it avoids returning the full cuOpt payload and context snapshot.

---

## 8. Automated native plan check

```powershell
.\scripts\run_native_plan_api_check.ps1 `
  -Backend ortools `
  -Repeat 3
```

The script verifies:

```text
preflight READY
PostgreSQL native data present
Redis native robot runtime present
Neo4j RouteNode/TRAVERSES = 220/356
HTTP 200
status = plan_validated
MOVE step exists
SERVICE step exists
payload/candidate/assignment/route/MAPF checks are true
repeated structural signatures match
```

Artifacts are stored under:

```text
runtime_outputs/native_plan_api_checks/{UTC_TIMESTAMP}/
├─ request.json
├─ preflight.json
├─ response_1.json
├─ trace_1.json
└─ summary.json
```

---

## 9. Natural-language plan request

After structured transport succeeds, edit `.env.docker`:

```dotenv
DEFAULT_PLANNING_MODE=llm_router
OPENAI_API_KEY=actual_key
OPENAI_MODEL=gpt-5-mini
```

Recreate only the API container; do not reset the DB:

```powershell
docker compose --env-file .env.docker up -d --build --force-recreate laro-api
```

Request:

```powershell
$body = @{
  warehouse_id = "WH-001"
  simulation_id = "SIM-V18-MIXED"
  optimization_backend = "ortools"
  events = @()
  user_command = "ORD-001을 출고하고 IN-001도 입고해. 전체 완료시간을 최소화해."
} | ConvertTo-Json -Depth 20
```

Now `router_llm_executed` should be `True`. The Router may select Rule or Agent; `final_route` records the locked branch.

---

## 10. NVIDIA cuOpt later

After OR-Tools transport works:

```dotenv
OPTIMIZATION_BACKEND=cuopt
NVIDIA_API_KEY=actual_key
CUOPT_TRANSPORT=nvidia_api
CUOPT_PAYLOAD_FORMAT=native
```

The request can omit `optimization_backend` to use the server default, or explicitly send `"optimization_backend": "cuopt"`.

---

## 11. What is not changed

```text
BE-main source code                         unchanged
POST /optimize request/response             unchanged
POST /reoptimize request/response           unchanged
Spring authentication and public APIs       unchanged
Replan migration to native API              deferred
```

The native plan API is tested directly at port 8000 first. When the team decides to replace `/optimize`, the Spring client can be changed separately to call this endpoint and consume `plan.robots[].steps[]`.

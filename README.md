# LARO v13.24 — Spring Compatibility + Native Plan Bridge

이 디렉터리는 두 HTTP 계약을 동시에 제공합니다.

```text
기존 BE-main 호환
POST /optimize
POST /reoptimize

향후 교체 대상 Native LARO
POST /api/v1/warehouses/{warehouse_id}/missions/plan
GET  /api/v1/warehouses/{warehouse_id}/missions/plan/preflight
GET  /api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/trace
```

기존 `/optimize` 구현과 `BE-main` 소스는 변경하지 않았습니다. 이번 버전의 목표는 Native Plan API가 PostgreSQL·Redis·Neo4j, Rule/Agent, OR-Tools/cuOpt, MAPF, `SimulationPlan`까지 연결될 수 있는지 별도로 검증하는 것입니다. Replan 연동은 다음 단계로 미룹니다.

## 실행

```powershell
Copy-Item .env.docker.example .env.docker

.\scripts\start_be_compat_docker.ps1 `
  -ResetData `
  -StopLegacy
```

시작 스크립트가 같은 Docker DB 서버에 Native V18 데모를 적재합니다.

```text
PostgreSQL  48 racks / 8 handling units / 5 orders / 2 inbound receipts
Redis       3 native robot runtime records
Neo4j       RouteNode 220 / TRAVERSES 356
```

## 한 번의 Plan 요청과 Trace 확인

```powershell
.\examples\powershell\call_native_plan.ps1 -Backend ortools
```

반복 점검:

```powershell
.\scripts\run_native_plan_api_check.ps1 `
  -Backend ortools `
  -Repeat 3
```

## 직접 입력

```json
{
  "warehouse_id": "WH-001",
  "simulation_id": "SIM-V18-MIXED",
  "optimization_backend": "ortools",
  "events": [
    {"type": "new_order", "order_id": "ORD-001"},
    {"type": "inbound_item_arrived", "inbound_id": "IN-001"}
  ]
}
```

Endpoint:

```text
POST http://localhost:8000/api/v1/warehouses/WH-001/missions/plan
```

정상 핵심 출력:

```text
status                    plan_validated
final_route               RULE_FORMULATION
effective_planning_mode   force_rule
router_llm_executed       false
plan.plan_id               PLAN-...
plan.robots[].steps[]      MOVE / WAIT / SERVICE
```

## 데이터 범위

Spring 호환 데이터와 Native Plan 데모는 같은 DB **서버**를 사용하지만 별도 계약으로 분리됩니다.

```text
Spring compatibility
- public.warehouse_layout / warehouse_node / warehouse_edge
- simulation:run:*
- WarehouseNode / CONNECTED_TO

Native plan demo
- warehouses / orders / handling_units / facility tables
- laro:warehouse:WH-001:sim:SIM-V18-MIXED:*
- RouteNode / TRAVERSES
```

즉 Spring 159-node 데이터를 자동으로 220-node G2P 모델로 바꾸는 버전은 아닙니다. 지금은 Native Plan 통신과 전체 계획 파이프라인을 검증하고, 실제 교체 단계에서 Spring 업무 데이터를 Native 계약으로 변환하는 Adapter/View를 추가합니다.

## OpenAI와 NVIDIA 전환

초기 통신 점검은 Key 없이 실행됩니다.

```dotenv
DEFAULT_PLANNING_MODE=force_rule
OPTIMIZATION_BACKEND=ortools
FRONTEND_EXPLANATION_MODE=deterministic
```

LLM Router 확인:

```dotenv
DEFAULT_PLANNING_MODE=llm_router
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5-mini
```

실제 NVIDIA cuOpt 확인:

```dotenv
OPTIMIZATION_BACKEND=cuopt
CUOPT_TRANSPORT=nvidia_api
NVIDIA_API_KEY=...
```

환경만 바꿀 때는 DB를 Reset하지 않습니다.

```powershell
docker compose --env-file .env.docker up -d --build --force-recreate laro-api
```

## 문서

- [Native Plan API 실행·입출력·Trace](docs/NATIVE_PLAN_API_BRIDGE.md)
- [기존 BE-main `/optimize` 계약](docs/BE_MAIN_COMPAT_API.md)
- [PostgreSQL·Redis 공유 계약](docs/BE_SHARED_DB_CONTRACT_V2.md)
- [기존 BE 호환 빠른 안내](README_BE_COMPAT.md)

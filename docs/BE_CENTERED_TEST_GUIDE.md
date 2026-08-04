# BE 중심 Structured Input 테스트 가이드

## 1. 오프라인 계약 검사

```powershell
cd .\AI
python -m scripts.check_v13_27_be_centered_contract
pytest -q tests/test_v13_27_be_centered_contract.py
```

검사 내용:
- 활성 Compose가 `001_schema.sql`을 Mount하지 않음
- `orders`, `handling_units` 테이블을 생성하지 않음
- Structured Input Pydantic 계약
- Request Overlay 변환
- 특정 `source_warehouse_item_id` Hard Constraint
- BE 신규 패키지가 기존 파일과 분리됨

## 2. 공유 DB 시작

```powershell
Copy-Item .env.docker.example .env.docker
.\scripts\start_be_compat_docker.ps1 -ResetData -StopLegacy
```

## 3. Spring BE 시작

기존 `local` 프로필을 그대로 사용한다. AI와 BE의 PostgreSQL·Redis·Neo4j 접속 값이 같아야 한다.

Spring 시작 후 창고, 노드, Edge, Product, WarehouseItem, Robot, SimulationRun 데이터를 준비한다.

## 4. 계획 속성·Neo4j Projection 준비

```powershell
.\scripts\prepare_be_centered_stack.ps1 `
  -WarehouseId 1 `
  -SimulationRunId 1
```

기대 Preflight:

```text
ready=true
orders_table=not_used
handling_units_table=not_used
be_route_nodes > 0
be_route_edges > 0
be_inventory_rows > 0
redis_robot_runtime_rows > 0
neo4j_route_nodes > 0
```

## 5. FastAPI 직접 호출

```powershell
python -m scripts.run_be_centered_plan_probe `
  --simulation-run-id 1
```

결과는 다음에 저장한다.

```text
runtime_outputs/be_centered_plan_probe/{UTC}/
├─ request.json
├─ response.json
└─ summary.json
```

## 6. Spring을 통한 호출

```powershell
.\examples\powershell\call_be_centered_plan.ps1 `
  -SimulationRunId 1 `
  -AccessToken "..."
```

성공 기준:

```text
response.result.status = plan_validated
response.result.plan.planId 존재
logicalOperations에 모든 structuredInput operationId 존재
MOVE/WAIT/SERVICE Step 생성
traceUrl/debugUrl 생성
```

## 7. LLM + NVIDIA cuOpt

`.env.docker`:

```dotenv
DEFAULT_PLANNING_MODE=llm_router
OPENAI_API_KEY=...
OPTIMIZATION_BACKEND=cuopt
NVIDIA_API_KEY=...
CUOPT_TRANSPORT=nvidia_api
```

API만 재생성한다.

```powershell
docker compose --env-file .env.docker up -d --build --force-recreate laro-api
```

## 8. DB 확인

```sql
SELECT * FROM laro_ext.contract_meta ORDER BY contract_key;
SELECT * FROM laro_ext.request_log ORDER BY created_at DESC;
SELECT * FROM laro_ext.inventory_reservation ORDER BY created_at DESC;
SELECT plan_id, simulation_run_id, plan_version, status, makespan_ms
FROM laro_ext.simulation_plan
ORDER BY created_at DESC;
```

다음 테이블은 존재하지 않아야 한다.

```sql
SELECT to_regclass('public.orders');
SELECT to_regclass('public.handling_units');
```

단, 과거 버전의 Volume을 재사용하면 Legacy 테이블이 남아 있을 수 있다. 새 구조 검증은 새 Volume 또는 해당 Legacy 테이블을 제거한 DB에서 수행한다.

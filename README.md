# LARO v13.25 — Native Plan Bridge Mixed-Operation Hardening

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

기존 `/optimize` 구현과 `BE-main` 소스는 변경하지 않았습니다. v13.25는 자연어 Agent의 혼합 출고·입고 Operation 누락을 프롬프트부터 최종 Plan까지 fail-closed로 수정한 버전입니다.

## 핵심 보장

```text
OUTBOUND_ORDER → G2P order 또는 direct task 또는 명시적 defer 중 정확히 하나
INBOUND_ITEM   → direct task 또는 명시적 defer 중 정확히 하나
RECOVERY       → direct task 또는 명시적 defer 중 정확히 하나
```

`GOODS_TO_PERSON`은 출고 방식만 제어하며, 입고·복구 Task를 비우지 않습니다.

## 데이터 출처

한 Plan 요청은 요청 시작 시 하나의 Live Snapshot을 구성합니다.

```text
Route nodes/edges → Neo4j snapshot
Rack/facility     → PostgreSQL snapshot
Order/HU/inbound  → PostgreSQL live lookup
Robot/runtime     → Redis live lookup
```

Live repository는 로컬 JSON을 읽지 않습니다. Trace의 `repository.source_manifest`에서 실제 출처를 확인할 수 있습니다.

## 실행

```powershell
Copy-Item .env.docker.example .env.docker

.\scripts\start_be_compat_docker.ps1 `
  -ResetData `
  -StopLegacy
```

## 구조화 Rule + OR-Tools 점검

```powershell
.\examples\powershell\call_native_plan.ps1 `
  -Backend ortools `
  -InputMode structured
```

```powershell
.\scripts\run_native_plan_api_check.ps1 `
  -Backend ortools `
  -InputMode structured `
  -Repeat 3
```

## 자연어 LLM Router + NVIDIA cuOpt 점검

`.env.docker`:

```dotenv
DEFAULT_PLANNING_MODE=llm_router
OPENAI_API_KEY=실제_Key
OPENAI_MODEL=gpt-5-mini
OPTIMIZATION_BACKEND=cuopt
CUOPT_TRANSPORT=nvidia_api
NVIDIA_API_KEY=실제_Key
```

API 컨테이너만 재생성:

```powershell
docker compose --env-file .env.docker up -d --build --force-recreate laro-api
```

실행:

```powershell
.\examples\powershell\call_native_llm_cuopt_plan.ps1
```

성공 Trace는 다음을 포함합니다.

```text
dynamic_input_valid                    true
payload_valid                          true
candidate_space_valid                  true
assignment_valid                       true
route_valid                            true
mapf_valid                             true
logical_operation_coverage_valid       true
```

그리고 `plan.logical_operations`에서 `ORD-001`, `IN-001` 모두 `task_ids`와 `assigned_robot_id`를 가져야 합니다.

## 문서

- [Native Plan API 실행·입출력·Trace](docs/NATIVE_PLAN_API_BRIDGE.md)
- [v13.25 혼합 Operation Hardening](docs/V13_25_MIXED_OPERATION_HARDENING.md)
- [기존 BE-main `/optimize` 계약](docs/BE_MAIN_COMPAT_API.md)
- [PostgreSQL·Redis 공유 계약](docs/BE_SHARED_DB_CONTRACT_V2.md)

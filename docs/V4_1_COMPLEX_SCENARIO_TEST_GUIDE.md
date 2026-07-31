# LARO Native Plan Bridge v4.1 — Complex Scenario Pack

대상 프로젝트:

```text
LARO_BE_UNMODIFIED_BACKEND_FASTAPI_PLAN_BRIDGE_v4_1
LARO version 13.25.1
```

이 패치는 오케스트레이션 코어, 프롬프트, DB 스키마, Docker Compose, 기존 `/optimize`를 변경하지 않습니다. 아래 항목만 추가합니다.

```text
scenarios/native_plan_complex_v4_1/
scripts/*_v41.py
scripts/*_v41.ps1
```

## 1. 설치

v4.1 프로젝트의 `LARO-fastapi` 폴더에서 압축을 풉니다.

```powershell
cd C:\...\LARO_BE_UNMODIFIED_BACKEND_FASTAPI_PLAN_BRIDGE_v4_1\LARO-fastapi

Expand-Archive `
  .\LARO_v4_1_COMPLEX_SCENARIO_PATCH.zip `
  -DestinationPath . `
  -Force
```

기존 파일을 덮어쓰지 않도록 모든 실행 파일 이름에 `_v41`을 붙였습니다.

## 2. 서버 준비 확인

Docker와 LARO API가 이미 실행 중이어야 합니다.

```powershell
Invoke-RestMethod http://localhost:8000/health |
  ConvertTo-Json -Depth 10
```

기본 확인값:

```text
version                       13.25.1
warehouse_repository_backend  live
map_repository_backend        neo4j
default_planning_mode         llm_router 또는 force_rule
```

Native Plan 데이터 확인:

```powershell
Invoke-RestMethod `
  "http://localhost:8000/api/v1/warehouses/WH-001/missions/plan/preflight?simulation_id=SIM-V18-MIXED" |
  ConvertTo-Json -Depth 20
```

기대값:

```text
ready             true
PostgreSQL racks  48
Redis robots      3
Neo4j nodes       220
Neo4j edges       356
```

## 3. 시나리오 정의 검증

API를 호출하지 않고 JSON 입력 형식만 검사합니다.

```powershell
python -m scripts.validate_native_plan_complex_scenarios_v41
```

기대:

```text
status          PASS
scenario_count  16
```

목록:

```powershell
python -m scripts.list_native_plan_complex_scenarios_v41
```

## 4. 권장 테스트 순서

### 4.1 빠른 기준선

```powershell
.\scripts\run_native_plan_complex_scenario_v41.ps1 `
  -Scenario P01_STRUCTURED_MIXED_BASELINE `
  -Backend ortools `
  -Repeat 1 `
  -Strict
```

검증 범위:

```text
ORD-001 + IN-001 보존
LiveWarehouseRepository
Neo4j route graph
PostgreSQL inventory/order
Redis robot runtime
OR-Tools
MAPF
logical operation coverage
```

### 4.2 전체 Wave

```powershell
.\scripts\run_native_plan_complex_scenario_v41.ps1 `
  -Scenario P03_STRUCTURED_FULL_WAVE `
  -Backend ortools `
  -Repeat 1 `
  -TimeoutSeconds 1200 `
  -Archive `
  -Strict
```

출고 5건과 입고 2건을 함께 처리합니다.

### 4.3 자연어 + LLM Router

```powershell
.\scripts\run_native_plan_complex_scenario_v41.ps1 `
  -Scenario P04_NATURAL_MIXED_EXCLUDE_R003 `
  -Backend ortools `
  -Repeat 1 `
  -TimeoutSeconds 1200 `
  -Strict
```

### 4.4 복합 Agent 정책

```powershell
.\scripts\run_native_plan_complex_scenario_v41.ps1 `
  -Scenario P06_NATURAL_AGENT_POLICY_STACK `
  -Backend cuopt `
  -Repeat 1 `
  -TimeoutSeconds 1200 `
  -Archive
```

`P06`은 정책 근거가 부족하다고 판단하면 `human_review`나 승인 대기 상태로 종료될 수 있으며, 이는 시나리오의 허용 결과입니다. Plan이 생성되면 `ORD-001`, `ORD-002`, `IN-001`이 모두 보존되어야 합니다.

### 4.5 NVIDIA cuOpt 전체 Wave

```powershell
.\scripts\run_native_plan_complex_scenario_v41.ps1 `
  -Scenario P12_NATURAL_FULL_WAVE `
  -Backend cuopt `
  -Repeat 1 `
  -TimeoutSeconds 1200 `
  -Archive `
  -Strict
```

성공 Plan의 Trace에는 다음이 필요합니다.

```text
optimizer.backend    cuopt
optimizer.optimizer  nvidia-cuopt
optimizer.status     success
```

## 5. Suite 실행

### 5.1 OR-Tools 전체 16개

현재 서버가 `llm_router`이면 구조화 입력도 Router LLM을 호출합니다. OpenAI 비용 없이 Rule만 시험하려면 서버를 `force_rule`로 실행하거나 아래의 `-SkipOpenAI`를 사용하십시오. `-SkipOpenAI`는 `/health`의 실제 `default_planning_mode`를 확인해 필요한 시나리오를 자동 제외합니다.

```powershell
$ReviewDir = ".\runtime_outputs\reviews\v41-ortools"

.\scripts\run_native_plan_complex_suite_v41.ps1 `
  -Backend ortools `
  -Repeat 1 `
  -MaxWorkers 1 `
  -TimeoutSeconds 1200 `
  -OutputDir $ReviewDir `
  -Archive `
  -Strict
```

### 5.2 부정·Runtime 시나리오만

```powershell
.\scripts\run_native_plan_complex_suite_v41.ps1 `
  -Backend ortools `
  -Include @(
    "P07_RUNTIME_LOW_BATTERY_FILTER",
    "P08_ALL_ROBOTS_UNAVAILABLE",
    "P09_DUPLICATE_EVENT_REPLAY",
    "P10_UNKNOWN_ORDER_ID",
    "P16_RUNTIME_SINGLE_ELIGIBLE_R003"
  ) `
  -Repeat 1 `
  -OutputDir ".\runtime_outputs\reviews\v41-runtime-negative" `
  -Strict
```

### 5.3 LLM + cuOpt만

```powershell
.\scripts\run_native_plan_complex_suite_v41.ps1 `
  -Backend cuopt `
  -Include @(
    "P04_NATURAL_MIXED_EXCLUDE_R003",
    "P05_NATURAL_TYPED_EDGE_POLICY",
    "P06_NATURAL_AGENT_POLICY_STACK",
    "P12_NATURAL_FULL_WAVE",
    "P13_MIXED_EVENT_AND_COMMAND",
    "P15_ITEM_NAME_ONLY_REJECTION"
  ) `
  -Repeat 1 `
  -MaxWorkers 1 `
  -TimeoutSeconds 1200 `
  -OutputDir ".\runtime_outputs\reviews\v41-llm-cuopt" `
  -Archive
```

OpenAI와 NVIDIA API를 동시에 여러 개 호출하면 Rate Limit과 Queue 지연이 발생할 수 있으므로 `MaxWorkers=1`을 권장합니다.

## 6. `Repeat`와 병렬 실행

```text
-Repeat 3
```

은 한 요청 내부 LLM 호출을 병렬화하는 옵션이 아닙니다. 동일 Plan API를 처음부터 끝까지 3회 순차 반복해 결과 안정성을 검사합니다.

```text
-MaxWorkers 2
```

는 서로 다른 시나리오 요청을 2개 동시에 실행합니다. OR-Tools 로컬 테스트에서만 권장하고, LLM/cuOpt 테스트는 1로 유지하십시오.

## 7. 저장 결과

한 시나리오:

```text
runtime_outputs/native_plan_complex_v4_1/{timestamp}/{scenario_id}/
├─ scenario.json
├─ server_health.json
├─ preflight.json
├─ scenario_summary.json
├─ scenario_report.md
└─ run_01/
   ├─ request.json
   ├─ response.json
   ├─ trace.json
   ├─ debug.json
   ├─ metrics.json
   ├─ node_timings.csv
   ├─ logical_operations.csv
   ├─ robot_steps.csv
   └─ station_reservations.csv
```

Suite:

```text
suite_summary.json
suite_summary.csv
suite_summary.md
server_health.json
```

`-Archive`를 사용하면 같은 폴더 옆에 ZIP도 생성됩니다.

## 8. 결과 검토 명령

요약:

```powershell
Get-Content "$ReviewDir\suite_summary.md"
```

LLM 시간이 긴 순서:

```powershell
Import-Csv "$ReviewDir\suite_summary.csv" |
  Sort-Object { [double]$_.llm_ms } -Descending |
  Format-Table `
    scenario_id,
    status,
    final_route,
    provider,
    http_total_ms,
    llm_ms,
    llm_call_count,
    solver_ms,
    mapf_and_plan_ms
```

한 실행의 노드별 시간:

```powershell
Import-Csv `
  "$ReviewDir\P06_NATURAL_AGENT_POLICY_STACK\run_01\node_timings.csv" |
  Sort-Object { [double]$_.duration_ms } -Descending |
  Format-Table
```

Operation 누락 확인:

```powershell
Import-Csv `
  "$ReviewDir\P06_NATURAL_AGENT_POLICY_STACK\run_01\logical_operations.csv" |
  Format-Table
```

프론트 재생 Step:

```powershell
Import-Csv `
  "$ReviewDir\P03_STRUCTURED_FULL_WAVE\run_01\robot_steps.csv" |
  Format-Table `
    robot_id,
    sequence,
    step_type,
    start_at_ms,
    end_at_ms,
    from_node,
    to_node,
    service_kind
```

## 9. 16개 시나리오

| ID | 목적 |
|---|---|
| P01 | 구조화 출고·입고 기준선 |
| P02 | 출고 2건 + 입고 1건 |
| P03 | 출고 5건 + 입고 2건 전체 Wave |
| P04 | 자연어 Operation + R003 제외 |
| P05 | 조건부 hard/soft Edge 정책 |
| P06 | Agent 복합 목적·예비 로봇 정책 |
| P07 | 저배터리 R002 제외 |
| P08 | 전체 로봇 비가용 시 빈 성공 차단 |
| P09 | 중복 Event deduplication |
| P10 | 존재하지 않는 ORD-999 거절 |
| P11 | 구조화 payload Prompt Injection 방어 |
| P12 | 자연어 7개 Operation 전체 Wave |
| P13 | 구조화 Event + 자연어 제약 mixed 입력 |
| P14 | 혼잡 Edge Runtime Event |
| P15 | 품목명만 있는 요청 거절 |
| P16 | R003만 존재하는 COMPLETE Runtime Snapshot |

## 10. 이 패치가 변경하지 않는 것

```text
app/ 이하 코어 코드
프롬프트
LangGraph 노드·엣지
PostgreSQL·Redis·Neo4j 스키마
Docker Compose
기존 /optimize와 /reoptimize
Native Plan API 입력·출력
```

따라서 v4.1 동작은 그대로 유지되고, 테스트 정의·실행·결과 저장 기능만 추가됩니다.

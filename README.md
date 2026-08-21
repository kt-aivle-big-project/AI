# LARO v13.27 — BE-Centered Structured Input Planner

이 디렉터리는 **기존 Spring BE의 PostgreSQL·Redis 구조를 권위값으로 유지하면서** LARO의 Rule/Agent, cuOpt/OR-Tools, MAPF, MOVE/WAIT/SERVICE 계획을 추가합니다.

```text
Spring public.*           창고·지도·Product·WarehouseItem·Robot·SimulationRun 원본
laro_ext.*                BE에 없는 계획 속성·예약·Plan·감사 로그만 추가
Spring Redis              현재 Robot·Edge Runtime 원본
Neo4j RouteNode           BE 지도에서 만든 경로 Projection
request.structured_input  이번 계획에서 처리할 업무 원본
user_command              운영 정책·목적·제약의 자연어 보충
```

## 핵심 계약

- 활성 DB 초기화 경로는 `db/postgres/004_be_centered_extensions.sql`만 사용합니다.
- 활성 경로는 LARO 전용 `orders` 테이블을 만들거나 읽지 않습니다.
- 활성 경로는 LARO 전용 `handling_units` 테이블을 만들거나 읽지 않습니다.
- 출고 후보 재고는 기존 `public.warehouse_items`에서 읽습니다.
- 업무는 `structured_input.operations`가 전부 전달합니다.
- `user_command`는 목적함수·로봇 제외·통로 정책 등을 추가할 수 있지만 구조화 업무를 추가·삭제·변경할 수 없습니다.
- 숫자 `simulationRunId`로 기존 `public.simulation_runs`와 Spring Redis Namespace를 선택합니다.
- 공개 실행 API는 `/api/v1/simulation-runs/{simulationRunId}/missions/plan`과 `/missions/replan`입니다.

`db/postgres/001_schema.sql`은 과거 Native Fixture 회귀 테스트용 **Legacy 파일**이며 현재 Compose에서는 Mount하지 않습니다.

## 신규 API

Spring 공개 API:

```http
GET  /api/laro/simulation-runs/{simulationRunId}/plan/preflight
POST /api/laro/simulation-runs/{simulationRunId}/plan
```

Spring이 호출하는 FastAPI:

```http
GET  /api/v1/simulation-runs/{simulationRunId}/missions/plan/preflight
POST /api/v1/simulation-runs/{simulationRunId}/missions/plan
```

FastAPI Body 예:

```json
{
  "structured_input": {
    "request_id": "REQ-SIM-1-001",
    "operations": [
      {
        "operation_id": "OUT-REQ-001",
        "operation_type": "OUTBOUND",
        "product_code": "ITEM-001",
        "quantity": 5,
        "destination_facility_code": "O_D"
      },
      {
        "operation_id": "IN-REQ-001",
        "operation_type": "INBOUND",
        "product_code": "ITEM-002",
        "quantity": 3,
        "source_facility_code": "I_a",
        "destination_node_code": "K3_3_ACCESS_A",
        "target_rack_level": 3
      }
    ]
  },
  "user_command": "전체 완료시간을 최소화하고 배터리가 낮은 로봇은 제외해.",
  "optimization_backend": "ortools"
}
```

## 실행 순서

### 1. 공유 DB와 LARO API 시작

```powershell
Copy-Item .env.docker.example .env.docker
.\scripts\start_local_stack.ps1 -ResetData -StopLegacy
```

### 2. 기존 Spring BE 실행

기존 `local` Profile로 실행하여 같은 PostgreSQL·Redis·Neo4j에 접속합니다. 기존 BE 소스는 수정하지 않았고 `com.aivle.be.laro` 패키지만 추가했습니다.

### 3. BE 테이블 확장 속성·Access Map·Neo4j Projection 준비

```powershell
.\scripts\prepare_be_centered_stack.ps1 `
  -WarehouseId 1 `
  -SimulationRunId 1
```

### 4. 오프라인 계약 검사

```powershell
python -m scripts.check_v13_27_be_centered_contract
python -m pytest -q
```

### 5. FastAPI 직접 Plan Probe

```powershell
python -m scripts.run_be_centered_plan_probe --simulation-run-id 1
```

### 6. Spring API를 통한 호출

```powershell
.\examples\powershell\call_be_centered_plan.ps1 `
  -SimulationRunId 1 `
  -AccessToken "발급받은_토큰"
```

## 데이터 권위 원칙

| 데이터 | 권위 원본 | LARO 역할 |
|---|---|---|
| 창고·Node·Edge | Spring PostgreSQL | 읽기 |
| 품목·재고 | Spring `product`, `warehouse_items` | 읽기·예약안 생성 |
| 이번 업무 | `structured_input.operations` | 요청 범위 계획 |
| 현재 Robot 상태 | Spring Redis | 읽기 |
| Route Graph | BE 지도 기반 Neo4j Projection | 읽기 |
| Plan 결과 | `laro_ext.simulation_plan` | 생성·저장 |
| 실제 재고 차감·업무 Commit | Spring BE | 검증 후 쓰기 |

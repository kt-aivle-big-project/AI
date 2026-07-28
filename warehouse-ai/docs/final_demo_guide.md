# P16 최종 데모 가이드

## 1. 준비

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`에 PostgreSQL, Neo4j Aura, Redis 접속 정보를 입력합니다. 충전소 비용이
이미 Neo4j에 저장돼 있다면 다시 입력할 필요가 없습니다.

```powershell
python -m scripts.set_charger_costs `
  --warehouse-id 2 `
  --cost 2150=1.2 `
  --cost 2151=1.0 `
  --cost 2152=1.5
```

## 2. 오프라인 최종 검사

DB와 OpenAI 없이 실행할 수 있습니다.

```powershell
python -m scripts.run_p16_release_checks
```

정상 기준:

```json
{
  "all_passed": true
}
```

검사 대상:

- 중앙 노드 동시 진입 WAIT
- 반대 방향 Edge 충돌 REROUTE
- 충전소 순차 점유
- 긴급 작업 우선 처리
- COMPACT/FULL 응답 분리
- Python compile
- 최종 문서 존재 여부

## 3. 서버 실행

```powershell
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

## 4. 대표 데모 A — 배터리·충전·출고

`POST /v1/planning/commands`

```json
{
  "warehouse_id": 2,
  "text": "R2-03의 배터리가 현재 21%라고 가정하고 E상품 30 BOX를 R2-03에 고정 배정해. 최소 배터리를 유지하지 못하면 active CHARGER 노드 중 비용이 가장 낮은 충전소에서 필요한 만큼 충전한 뒤 출고 노드 2146으로 이동해. 실제 Redis 배터리는 변경하지 말고 시뮬레이션만 해.",
  "requested_execution_mode": "SIMULATE_ONLY",
  "report_detail_level": "STANDARD",
  "response_view": "COMPACT"
}
```

필수 확인:

```text
status = SIMULATION_SUCCESS
plan_mode = LOCAL_REPLAN
verification.decision = PASS
result.charging[0].selected_charger_node = 2151
result.charging[0].selection_policy = MIN_CONFIGURED_CHARGER_COST
result.schedule_validation.dependency_count = 2
result.schedule_validation.validated_after_routing = true
result.metrics.battery_by_robot.R2-03.energy_source = ROUTING_FINAL_DISTANCE
result.metrics.battery_by_robot.R2-03.final_battery >= 20
result.dispatch.gateway_dispatched = false
```

P16부터 충전량은 Optimizer 예상거리가 아니라 최종 라우팅 거리로 다시 계산합니다.
경로가 30.84에서 34.44로 늘어난 기존 사례에서는 충전량도 0.542%가 아니라
약 0.722%로 보정돼 최종 배터리 20%를 유지해야 합니다.

## 5. 대표 데모 B — 개발자용 전체 근거

동일 요청에서 다음만 변경합니다.

```json
{
  "report_detail_level": "DEBUG",
  "response_view": "FULL"
}
```

확인 항목:

- `route_energy_reconciliation.energy_source=ROUTING_FINAL_DISTANCE`
- `distance_comparison`
- `reservation_evidence`
- `execution_task_dependencies`
- `robot_command_batches`
- `trace`의 `route_energy_reconciled`

## 6. 대표 데모 C — 다중 로봇 충돌

```powershell
python -m scripts.run_p15_multi_robot_checks
```

정상 기준:

```text
VERTEX_WAIT: conflict_count 0, resolution WAIT
EDGE_SWAP_REROUTE: conflict_count 0, resolution REROUTE
SHARED_CHARGER: 두 CHARGE 구간 미중첩
EMERGENCY_PRIORITY: EMERGENCY 우선 예약
```

## 7. 운영 실행 데모

Mock Gateway를 별도 터미널에서 실행합니다.

```powershell
$env:MOCK_GATEWAY_AUTO_EXECUTE="false"
python -m uvicorn mock_robot_gateway:app --host 127.0.0.1 --port 9000
```

실제 실행 요청은 `requested_execution_mode=EXECUTE`로 전송합니다. Verification
PASS 계열에서만 활성 계획 저장과 Gateway 전송이 허용됩니다.

# P16.5 업그레이드 가이드

## 1. 기존 `.env` 복사

P16.4에서 사용하던 `.env`를 P16.5 프로젝트 루트에 복사합니다.

## 2. API 키 설정

```env
CUOPT_API_KEY=nvapi-YOUR_KEY
```

다음 값은 더 이상 필요하지 않습니다.

```text
CUOPT_FUNCTION_ID
cuopt-thin-client
cuopt-lp
requirements-cuopt.txt
CUDA
로컬 NVIDIA GPU
```

## 3. 패키지 설치

```powershell
python -m pip install -r requirements.txt
```

## 4. 서버 실행

```powershell
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```

## 5. 결과 확인

cuOpt REST 성공:

```json
{
  "used_provider": "CUOPT",
  "transport": "HTTPS_REST",
  "credentials_detected": true,
  "credential_source": "CUOPT_API_KEY",
  "fallback_used": false
}
```

cuOpt 실패 후 CPU 전환:

```json
{
  "used_provider": "CPU",
  "fallback_used": true,
  "attempts": [
    {"provider": "CUOPT_REST", "status": "FAILED"},
    {"provider": "CPU", "status": "SUCCESS"}
  ]
}
```

## 6. P16.5.4 복합 일정 테스트

P16.5.3과 의존성이 동일하므로 새 가상환경이나 패키지 재설치는 필요하지 않습니다.
기존 `.env`를 복사한 뒤 다음 Swagger 예시를 사용합니다.

```text
examples/p16_5_4_complex_daily_request.json
```

P16.5.4 확인 항목:

```text
response_schema_version = p16.5.4
E/F = OUTBOUND
C/D = INBOUND
오전 10:30~12:00 = 당일 정오 종료
INBOUND PICK = 기존 저장 재고 검사 제외
CURRENT LOT + FUTURE_INBOUND = 출고 수량으로 합산
```

참고: 예시 날짜는 데모 재고의 사용 가능 시점 이후인 `2026-07-25`입니다. 기존 `2026-07-24 오전 7:15` 명령은 데모 데이터 기준 A/B가 아직 사용 가능하지 않아 정상적으로 재고 부족이 발생할 수 있습니다.

## 7. P16.5.5 다중 로봇 재분배 확인

P16.5.4와 동일한 복합 일일 요청을 실행합니다.

확인 항목:

```text
response_schema_version = p16.5.5
optimizer_execution.used_provider = CUOPT
optimizer_postprocessing.cuopt_assignment_application.mode = GLOBAL_ORDER_LOCAL_MULTI_ROBOT_REBALANCE
optimizer_postprocessing.parallel_robot_rebalance.enabled = true
robot_count >= 2
FROZEN_ASSIGNMENT_MISMATCH가 명시적 고정 배정이 아닌 작업을 막지 않음
자기 예약에 의한 blocked_by_robot_id = null 충돌 대기 없음
congestion_avoidance.node_ids에 2013 포함
```


## 8. P16.5.6 공유 노드 장기 대기 해제 확인

P16.5.5와 같은 복합 일일 요청을 실행합니다.

확인 항목:

```text
response_schema_version = p16.5.6
robot_count = 3
optimizer_execution.used_provider = CUOPT
routes가 비어 있지 않음
simulation.success = true
conflict_count = 0
idle_relocation_count >= 1
holding_node_id에 2013 및 2044가 포함되지 않음
ROUTE_FAILED 없음
동일 초기 노드의 R2-01/R2-02 첫 waypoint time_step이 서로 다름
```

Swagger 예시는 `examples/p16_5_6_shared_node_idle_request.json`을 사용합니다.

## 9. P16.5.7 Idle Whitelist 적용

1. `.env`를 기존 버전에서 복사합니다.
2. 전용 대기 노드를 한 번 등록합니다.

```powershell
python -m scripts.seed_p16_5_7_idle_nodes --warehouse-id 2
```

3. 서버를 실행합니다.
4. `examples/p16_5_7_idle_whitelist_request.json`을 Swagger에 입력합니다.
5. 다음을 확인합니다.

```text
response_schema_version = p16.5.7
idle_policy.strict = true
idle_policy.violation_count = 0
idle_action_task_count > 0
holding_node_id = 2160/2161/2162 중 하나
일반 ROUTE 노드의 장시간 WAIT 없음
```

# P16.5.9 Shared Resource Capacity Scheduling

## 목적

P16.5.8까지는 충전·대기 작업과 충돌 없는 경로는 생성했지만, 충전기 슬롯과
공유 작업 노드의 서비스 용량을 일정 단계에서 먼저 확정하지 않았습니다.
P16.5.9는 cuOpt 결과와 Prioritized Time A* 사이에 로컬 공유 자원 스케줄러를
추가해 실제 수행 가능한 서비스 시간창을 먼저 만듭니다.

## 적용 자원

- `SERVICE_NODE`: PICK/DROP이 수행되는 INBOUND, OUTBOUND, STORAGE 노드
- `CHARGER_SLOT`: 실제 CHARGE 시간에만 점유하는 충전기 슬롯
- `IDLE_SPACE`: CHARGER_WAITING_AREA, PARKING, STAGING, HOLDING

각 자원은 `capacity`와 반개구간 `[start_step, end_step)` 예약을 사용합니다.
용량 1이면 직렬화하고, 용량 N이면 최대 N개까지 동시 점유를 허용합니다.

## 주요 변경

1. cuOpt의 로봇 배정·방문 순서는 유지하고 공유 자원 충돌만 지연 조정합니다.
2. 동일 로봇의 후속 작업과 FINISH_TO_START 의존 작업에 지연을 전파합니다.
3. 고정·동결 작업은 이동시키지 않으며 충돌하면 차단 오류를 반환합니다.
4. `service_duration_seconds`가 기존 작업 길이보다 길면 서비스 시간을 확장합니다.
5. 충전기 `charger_capacity`에 따라 CHARGE 슬롯을 순차 예약합니다.
6. 라우팅 후 생성된 대기 작업도 `waiting_capacity` 또는 `parking_capacity`로 검증합니다.
7. 공유 자원 조정, 예약, 슬롯 번호, 용량 출처를 응답에 남깁니다.
8. 자원 시간 조정과 경로 기반 충전시간 보정을 제한된 고정점 반복으로 일치시킵니다.

## Neo4j 속성

```text
service_capacity
service_duration_seconds
charger_capacity
charger_power_kw
charging_rate_percent_per_minute
supported_robot_types
waiting_capacity
parking_capacity
idle_capacity
allowed_robot_types
maximum_idle_duration
nearby_service_nodes
```

기존 지도 노드를 삭제하거나 다시 생성하지 않고 속성만 추가합니다.

## 시드

P16.5.8의 충전 대기 구역 시드가 이미 적용된 창고에서 실행합니다.

```powershell
python -m scripts.seed_p16_5_9_resource_capacities --warehouse-id 2
```

기본 데모 설정은 작업 노드 2088·2139·2146, 충전기 2150~2159,
대기 구역 2160~2162의 용량을 각각 1로 설정합니다.

## 검증 응답

COMPACT 응답의 `result.resources`에서 확인합니다.

```text
response_schema_version = p16.5.9
result.resources.valid = true
result.resources.reservation_count > 0
result.schedule_validation.resource_capacity_valid = true
errors = []
```

동시에 같은 작업 노드를 쓰는 두 작업은 예를 들어 다음처럼 분리되어야 합니다.

```text
R2-01 PICK node 2139: 2340~2341
R2-02 PICK node 2139: 2341~2342
```

## 차단 오류

- `SHARED_RESOURCE_CAPACITY_EXCEEDED`
- `RESOURCE_CAPACITY_CONFLICT_WITH_FROZEN_TASK`
- `HARD_WINDOW_RESOURCE_DELAY_VIOLATION`
- `MAXIMUM_IDLE_DURATION_EXCEEDED`

하드 제약 위반은 목적함수의 큰 비용으로 남기지 않고 계획을 실패시킵니다.

## 검사

```powershell
python -m scripts.run_p16_5_9_final_checks
pytest -q tests/test_p16_5_9_shared_resource_capacity.py
```

신규 공유 자원 테스트 8개와 관련 충전·재계획·라우팅·검증 회귀 테스트 67개를
통과했습니다. 전체 테스트 수집은 이 아티팩트 실행 환경에 `openai`와 실제
LangGraph 런타임이 없어 별도로 완료하지 못했으며, 프로젝트 가상환경에서 위
release gate를 다시 실행해야 합니다.

## 다음 단계

P16.5.10에서 충전 방문 후보를 cuOpt 입력에 포함하고, 공유 자원 예약 비용과
충전 대기시간을 전체 목적함수에 연결합니다. MAPF 실패에 따른 다단계 자동
재계획은 P16.5.11 범위입니다.

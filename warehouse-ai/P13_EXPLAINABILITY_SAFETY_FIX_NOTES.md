# P13 Explainability and Safety Fix

기준 버전: `P12 charge execution fix`

## 반영 내용

1. 충전 체류 근거
   - 충전소 체류 waypoint의 예약 근거를 `CHARGING`으로 저장합니다.
   - 충전 작업 ID와 충전 노드, time step을 함께 기록합니다.
   - 예약 충돌로 인한 대기는 `RESERVATION_CONFLICT_WAIT`으로 구분합니다.

2. 거리 차이 설명
   - Optimizer 예상 거리와 시간 확장 Routing 최종 거리가 다를 때
     `TIME_OPTIMAL_ROUTE_DISTANCE_VARIANCE`를 기록합니다.
   - 예약 충돌이 원인이면 `CONFLICT_AVOIDANCE_DETOUR` 또는
     `RESERVATION_WAIT`으로 기록합니다.

3. 실행 단계 작업 의존성
   - 사용자 업무 단위 `task_dependencies`와 별도로
     `execution_task_dependencies`를 저장합니다.
   - 자동 충전 작업이 생성되면 `CHARGE -> 대상 작업` 관계를
     `AUTO_CHARGING` 근거로 저장합니다.
   - 기존 PICK -> DROP 관계는 `PLANNER_PREDECESSOR`로 저장합니다.

4. 시뮬레이션 안전성 회귀 테스트
   - 가상 배터리 override, 최적화, 경로 생성, 시뮬레이션 및 RobotAdapter
     변환 후에도 원본 Redis Snapshot이 변경되지 않는지 검사합니다.

5. Neo4j 충전 비용 입력
   - 실제 비용값을 임의 생성하지 않습니다.
   - `scripts/set_charger_costs.py`로 사용자가 명시한 값만 active CHARGER
     노드의 `charging_cost`에 반영합니다.

## 충전 비용 입력 예시

아래 숫자는 명령 형식 예시이며 실제 프로젝트 정책값으로 바꿔야 합니다.

```powershell
python -m scripts.set_charger_costs `
  --warehouse-id 2 `
  --cost 2150=1.2 `
  --cost 2151=1.0 `
  --cost 2152=1.5
```

스크립트는 업데이트된 노드, 찾지 못했거나 비활성인 노드, 현재 active
CHARGER 목록을 JSON으로 출력합니다.

## 검증

- P13 신규 테스트 포함 전체 회귀 테스트: `557 passed`
- 전체 Python compile 검사: 통과
- 전체 회귀 테스트는 외부 패키지가 없는 작업 환경에서 임시 테스트
  스텁을 사용했습니다. 스텁은 배포 ZIP에 포함하지 않습니다.

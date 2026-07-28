# 이벤트 기반 자동 재계획

PHASE 12는 기존 `RobotEvent`의 `ROBOT_FAILED`, `ROBOT_DELAYED`,
`PATH_BLOCKED`, `PATH_DEVIATED`만 자동 재계획 대상으로 지원한다.
`POSITION_UPDATED`, `TASK_STARTED`, `TASK_COMPLETED`의 기존 처리 의미는
변경하지 않았다.

## 안전 흐름

1. `event_id` 중복 확인
2. REAL 또는 SIMULATION 상태 반영
3. SQL/Redis/Neo4j/활성 계획 기반 결정론적 영향 분석
4. `NO_REPLAN`, `LOCAL_REPLAN`, `GLOBAL_REPLAN` 결정
5. 별도 `SIMULATE_ONLY` 명령으로 Optimizer, Routing, Simulation,
   deterministic validation, Verification Agent 실행
6. REAL MEDIUM/HIGH 이벤트는 `APPROVAL_REQUIRED`
7. 승인 시 활성 계획 버전을 다시 확인하고 기존 EXECUTE Graph를 새로 실행

이벤트 수신만으로 Robot Gateway를 호출하지 않는다. SIMULATION 이벤트는
실제 실행 승인을 할 수 없다. `ROBOT_FAILED`와 `PATH_BLOCKED`는 기본적으로
운영자 승인이 필요하다.

## 영향 분석

- 고장: 고장 로봇의 실제 활성 배정과 미완료 작업, 대체 로봇 수
- 지연: payload `delay_seconds`와 freeze horizon
- 경로 차단: 실제 활성 route의 waypoint 또는 edge 사용 여부
- 경로 이탈: 현재 노드의 지도 존재 및 남은 경로 복귀 가능성

결과 ID는 Snapshot과 활성 계획에 존재하는 값만 사용한다. 동일 failure
signature가 1시간 안에 두 번째로 들어오면 추가 재계획을 만들지 않고
`REPEATED_FAILURE_DETECTED`로 종료한다.

## API

- 기존 `POST /v1/execution/events`
- `GET /v1/execution/events/{event_id}`
- `GET /v1/event-replans/{request_id}`
- `GET /v1/warehouses/{warehouse_id}/event-replans`
- `POST /v1/event-replans/{request_id}/approve`
- `POST /v1/event-replans/{request_id}/reject`

승인 시 `expected_active_plan_version`과 최신 Redis 활성 버전이 다르면
`STALE_PLAN`으로 중단한다. 승인 후에도 새 Snapshot, Simulation,
Verification을 통과해야 기존 활성화/Gateway 단계로 진행한다.

## 저장 및 복구

`migrations/008_event_replan.sql`은 이벤트 idempotency와 승인 감사 정보를
보존한다. 롤백 전 내보내기를 수행하고 `automatic_replan_request`,
`execution_event_processing` 순으로 삭제한다.


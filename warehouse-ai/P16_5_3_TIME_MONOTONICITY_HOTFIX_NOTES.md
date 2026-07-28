# P16.5.3 Time Monotonicity Hotfix

## 문제

장시간 WAIT 뒤 같은 노드에서 PICK/DROP을 수행하면 다음 두 문제가 겹칠 수 있었습니다.

1. `wait_path()`가 현재 경계 waypoint를 다시 포함하고, 기존 waypoint와 action이 다르면 동일 노드·동일 시간이 중복으로 남았습니다.
2. 라우팅 스케줄 보정이 optimizer의 최소 작업 처리시간을 보존하지 않아 같은 노드 작업이 `start_time_step == end_time_step`으로 축소될 수 있었습니다.

이 조합은 `NON_MONOTONIC_TIME` 검증 실패를 만들었습니다.

## 수정

- WAIT 경계 중복 제거를 Pydantic 객체 전체 비교에서 `node_id + time_step` 비교로 변경
- PICK/DROP에 optimizer가 계산한 최소 작업시간을 실제 경로 waypoint로 반영
- 라우팅 스케줄 보정 시 기존 작업 duration을 보존하고 PICK/DROP/CHARGE는 최소 1 step 보장
- NVIDIA cuOpt REST 입력의 `service_times`에 PICK/DROP 1 local time step 반영
- 중복된 `routing_final_start_time_steps` metadata 대입 제거
- 응답 스키마 버전 `p16.5.3`

## 기대 불변식

- 한 로봇 경로의 모든 waypoint는 이전 waypoint보다 큰 `time_step`을 가집니다.
- PICK/DROP/CHARGE는 `end_time_step > start_time_step`을 유지합니다.
- routing 결과가 같은 노드·같은 시각을 반환해도 optimizer 작업시간이 0으로 축소되지 않습니다.

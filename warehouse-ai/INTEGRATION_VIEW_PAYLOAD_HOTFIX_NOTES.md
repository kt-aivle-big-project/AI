# Integration View Payload Hotfix

## 확인된 문제

- `GET /v1/simulations/{simulation_id}/view`가 실행 메타데이터만 조회하고
  `simulation_run.output_payload`를 불러오지 않아 `robots`, `tasks`, `routes`,
  `timeline`, `metrics`가 빈 값으로 반환됐다.
- `GET /v1/commands/{command_id}/result`의 `summary`가 기존 COMPACT 응답 전체를
  복사해 `answer`와 내부 상태가 중복됐다.
- 사용자 화면에 cuOpt 공급자 원문 오류가 그대로 노출됐다.

## 수정

- `PostgresRepository.get_latest_simulation_run()`이 최신 실행의
  `output_payload`까지 조회하도록 수정했다.
- 시뮬레이션 화면용 변환은 저장된 `simulation.robot_routes`,
  `simulation.task_assignments`, `simulation.timeline`, `simulation.metrics`를
  우선 사용하고 기존 계획 경로를 대체 데이터로 사용한다.
- 사용자 화면용 `summary`는 핵심 수치만 반환한다.
- 공급자 원문 오류는 사용자 화면에서는 일반 안내로 바꾸고, 원문은
  `/debug`와 `/plan-evidence`에 유지한다.
- 시뮬레이션 전용 계획의 실행 상태는 `execution_requested=false`,
  `execution_state=NOT_REQUESTED`로 명시한다.

## 실제 결과 재검증

사용자가 제공한 실행 결과를 수정된 변환기에 다시 넣어 확인했다.

- robots: 2
- tasks: 3
- routes: 2
- timeline: 55
- total_distance: 72.2 m
- execution_mode: SIMULATE_ONLY
- execution_state: NOT_REQUESTED

## 자동 테스트

- `794 passed`

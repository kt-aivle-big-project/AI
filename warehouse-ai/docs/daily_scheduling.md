# 일일 다중 작업 스케줄링

## 데이터 책임

- PostgreSQL: 작업, 사용자 시간 제약, 선후관계, 계산 일정, 실제 시작·완료 시각
- Neo4j: 고정 노드와 `CONNECTED_TO` 통로
- Redis: 활성 plan version, 현재 로봇 상태, 시간대별 예약, simulation 가상 상태

## 명령 해석

`CommandInterpretation`은 `TaskScheduleConstraint`, `TaskDependency`,
`SameRobotGroup`을 사용합니다. OpenAI structured output 대상 모델은
`extra="forbid"`이며 자유 형식 object를 추가하지 않습니다. deterministic parser는
명시적인 작업 ID와 시간만 만들고 모호한 시간은 clarification으로 보냅니다.

## 시간 기준

창고 현지 시각은 `WAREHOUSE_TIMEZONE`에서 해석하고 UTC로 저장합니다. 값이 없거나
유효하지 않으면 `Asia/Seoul`을 사용하고 `DEFAULT_WAREHOUSE_TIMEZONE_USED`를 남깁니다.
Optimizer, Routing, Simulation은 하나의 Snapshot `reference_time`과 동일한
`TIME_STEP_SECONDS`를 공유합니다.

## 제약

- FINISH_TO_START: `successor.start >= predecessor.end + lag`
- HARD_WINDOW: earliest 이전 시작과 latest 이후 완료를 금지
- DEADLINE/SOFT_WINDOW: 지연을 tardiness 목적함수와 경고에 반영
- ASAP: 현재 계획 기준에서 가능한 가장 빠른 시각
- same_robot_group: 그룹의 모든 작업을 같은 로봇에 배정

의존 그래프 cycle은 `CYCLIC_TASK_DEPENDENCY`로 Optimizer 전에 종료합니다.

## Dispatcher와 이벤트

Gateway에는 READY task의 경로 prefix만 전달합니다. 미래 예약 작업은 SCHEDULED,
선행 작업 대기는 WAITING_FOR_PREDECESSOR로 유지합니다. scheduler tick은 실제 무한
polling loop가 아니라 입력 시각에 대한 deterministic 평가 함수입니다.

`TASK_COMPLETED`는 event ID로 멱등 처리한 뒤 모든 선행 작업이 끝난 successor를
READY로 바꿉니다. `TASK_FAILED`는 직접 successor만 BLOCKED로 바꾸고 독립 READY
작업은 계속 전송 가능하게 유지한 뒤 재계획 이벤트를 만듭니다.

## 긴급 삽입

긴급 명령은 `INSERT_TASK`로 해석합니다. 활성 계획에서 COMPLETED, EXECUTING,
freeze horizon prefix는 고정하고 그 이후 작업만 재최적화합니다. 새 계획은 새
`plan_version`을 사용하며 이전·신규 시작/종료, 지연, window 위반, 보존·이동 작업을
evidence로 비교합니다. 실행 중 작업 중단은 기본적으로 허용하지 않습니다.

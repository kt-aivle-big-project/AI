# AI 물류 계획 결과 연동 계약 v1

## 목적

내부 LangGraph 결과 전체를 프론트엔드, 시뮬레이터, 백엔드, Robot Gateway가
각자 해석하지 않도록 용도별 공개 계약을 분리합니다. 내부 FULL 응답은 개발·감사
목적으로 유지하고 일반 연동에서는 사용하지 않습니다.

## 담당 경계

### AI 서비스 담당

- 계획 결과의 기준 의미 정의
- 사용자 화면용 결과 생성
- 시뮬레이션 화면용 결과 생성
- 실행 상태용 결과 생성
- Robot Gateway 명령 형식 생성
- 계약 버전과 예시 JSON 관리

### 백엔드 담당

- AI API 호출과 결과 저장
- 사용자·회사·창고 권한 확인
- 프론트엔드에 공개 API 전달
- 회사 SQL 데이터를 공통 PlanningSnapshot으로 변환
- 재시도, 장애 응답, 운영 로그 관리

### 프론트엔드 담당

- 공개 계약에 있는 필드만 사용
- FULL 응답 내부 구조를 직접 참조하지 않음
- 사용자 결과와 실행 상태를 화면에 표시

### 시뮬레이션 담당

- `simulation-view.v1`의 routes, timeline, metrics 사용
- 실제 운영 Redis를 직접 변경하지 않음

## 공개 API

### 사용자 결과

```text
GET /v1/commands/{command_id}/result
```

계약: `planning-ui.v1`

주요 필드:

- `command_id`
- `status`
- `plan_version`
- `simulation_id`
- `execution_mode`
- `summary`
- `verification`
- `warnings`
- `errors`
- `resources`

### 시뮬레이션 화면

```text
GET /v1/simulations/{simulation_id}/view
```

계약: `simulation-view.v1`

주요 필드:

- `simulation_id`
- `plan_version`
- `time_step_seconds`
- `robots`
- `tasks`
- `routes`
- `timeline`
- `metrics`

### 실행 상태

```text
GET /v1/execution/plans/{plan_version}/status
```

계약: `execution-status.v1`

주요 필드:

- 계획 상태
- 승인 상태
- 최신 작업 전달 상태
- Gateway 작업 번호
- 최종 검증 결과
- 재고 예약 또는 해제 결과

### 개발자 상세 정보

```text
GET /v1/commands/{command_id}/debug
```

계약: `planning-debug.v1`

개발·원인 분석 전용입니다. 프론트엔드의 일반 화면은 이 API에 의존하지 않습니다.

### Robot Gateway

기존 RobotAdapter와 ExecutionDeliveryService가 `robot-command.v1` 역할을 담당합니다.
일반 프론트엔드 API로 로봇 명령 전체를 노출하지 않습니다.

## 변경 원칙

- 내부 PlanningState 변경은 공개 계약 변경을 의미하지 않습니다.
- 공개 필드 삭제·의미 변경 시 새 계약 버전을 만듭니다.
- 필드 추가는 기존 소비자가 영향을 받지 않는 범위에서만 허용합니다.
- 동일 정보의 기준 위치는 공개 계약 안에서 하나만 둡니다.

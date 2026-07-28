# P16.5.16 Integration Contract Notes

## 변경 내용

- 내부 FULL 계획 결과와 외부 소비자 계약을 분리했습니다.
- 사용자 결과, 시뮬레이션 화면, 실행 상태, 개발자 상세 조회 API를 추가했습니다.
- 기존 `POST /v1/planning/commands`와 기존 LLM 사용 범위는 변경하지 않았습니다.
- Supervisor, Verification, 보고서의 기존 규칙 기반 fallback도 변경하지 않았습니다.

## 추가 API

- `GET /v1/commands/{command_id}/result`
- `GET /v1/commands/{command_id}/debug`
- `GET /v1/simulations/{simulation_id}/view`
- `GET /v1/execution/plans/{plan_version}/status`

## 호환성

기존 응답과 내부 저장 구조는 유지됩니다. COMPACT 응답의 `details`에는 새 조회 API
주소만 추가됩니다.

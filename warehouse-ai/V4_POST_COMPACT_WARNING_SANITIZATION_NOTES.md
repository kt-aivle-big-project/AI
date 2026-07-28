# V4 POST COMPACT 사용자 경고 통일

## 수정 목적

`POST /v1/planning/commands`에서 `response_view`를 생략하면 기본적으로
COMPACT 응답을 반환합니다. 기존 V3에서는 `/result` 조회 응답만
cuOpt 제공자 내부 오류를 사용자용 문구로 변환했고, 기본 COMPACT 응답에는
일부 내부 오류 원문이 남을 수 있었습니다.

V4에서는 사용자용 공개 응답이 공통 정리 함수를 사용합니다.

## 적용 대상

- `POST /v1/planning/commands`의 AUTO -> COMPACT 응답
- `POST /v1/planning/commands`의 명시적 COMPACT 응답
- `GET /v1/commands/{command_id}/result`
- `GET /v1/simulations/{simulation_id}/view`
- `GET /v1/execution/plans/{plan_version}/status`

다음 위치의 제공자 내부 경고를 사용자용 문구로 변환합니다.

- `answer`
- 최상위 `warnings`
- 최상위 `errors` 중 동일 제공자 진단
- `verification.warning_findings`
- `verification.user_visible_warnings`
- 중첩된 재고/공유자원 경고

사용자용 문구:

> 기본 최적화 엔진을 사용할 수 없어 대체 최적화 엔진으로 계획했습니다.

## 원문 유지 대상

다음 개발자용 응답에는 제공자 오류 원문을 그대로 유지합니다.

- 명시적 `response_view: "FULL"`
- `GET /v1/commands/{command_id}/debug`
- `GET /v1/commands/{command_id}/plan-evidence`

## 검증

- 관련 응답 계약 테스트: 11개 통과
- 전체 자동 테스트: 797개 통과

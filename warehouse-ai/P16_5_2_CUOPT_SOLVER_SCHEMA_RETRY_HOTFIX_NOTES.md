# P16.5.2 cuOpt Solver Schema Retry Hotfix

## 변경 사항

- NVIDIA managed cuOpt REST 요청에서 더 이상 허용되지 않는 `solver_config.drop_infeasible_tasks` 제거
- HTTP 422 응답의 `Extra inputs are not permitted` 필드 경로를 파싱
- 거부된 선택 필드를 요청에서 자동 제거한 뒤 최대 2회 재제출
- 재시도 횟수와 제거 필드를 계획 metadata에 기록
- cuOpt 응답에서 작업 누락 또는 dropped task가 있으면 기존처럼 실패 처리 후 CPU fallback
- 응답 스키마 버전 `p16.5.2`

## 실행

기존 P16.5 가상환경과 `.env`를 그대로 사용할 수 있습니다.

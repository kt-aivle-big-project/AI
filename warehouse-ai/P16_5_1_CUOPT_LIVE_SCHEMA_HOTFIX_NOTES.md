# P16.5.1 cuOpt Live REST Schema Hotfix

## 문제
실제 NVIDIA API Catalog가 `task_data.priorities`, `task_data.mandatory_task_ids`,
`task_data.task_ids`를 extra field로 거부하여 HTTP 422를 반환했습니다.

## 수정
- `task_data`를 `task_locations`, `task_time_windows`, `service_times` 중심의 최소 스키마로 변경
- 내부 task ID는 context에서 유지
- cuOpt 숫자 task index를 내부 task ID로 역매핑
- 모든 작업은 `drop_infeasible_tasks=false`로 필수 처리
- NVIDIA 오류 응답 excerpt를 500자에서 2,000자로 확대
- 응답 스키마 `p16.5.1`

## 기대 결과
`optimizer_execution.used_provider`가 `CUOPT`, `fallback_used`가 `false`로 반환됩니다.

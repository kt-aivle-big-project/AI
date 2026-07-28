# PHASE 5–7 계획 근거와 상세 보고

## 저장 흐름

최적화·경로 생성 결과는 기존 계획을 변경하지 않고 다음 근거 필드로
`PlanningState`와 `simulation_run.output_payload`에 저장한다.

- `optimization_evidence`: task별 실제 평가 로봇, 실행 가능 여부, 비용 구성,
  선택 여부와 결정적 tie-break 규칙
- `objective_breakdown`: 거리, makespan, tardiness, 에너지, 로봇 활성화,
  계획 변경 component와 합계
- `routing_evidence`: 최종 waypoint 인접 쌍으로 만든 route segment
- `reservation_evidence`: vertex/edge 예약 수, 실제 WAIT, 최종 검증 충돌 수
- `distance_comparison`: Optimizer 예상 거리와 Routing 최종 거리의 차이
- `report_evidence`: 최종 보고에 전달되는 정제된 근거 묶음

외부 cuOpt가 후보별 근거를 반환하지 않으면 후보 근거를 만들어 내지 않고 빈
목록으로 저장한다. 경로의 MOVE segment가 Neo4j Snapshot 간선에서 확인되지
않으면 거리와 edge identifier를 `null`로 두고 evidence를 불완전 상태로 표시한다.

## 상세 보고 원칙

상세 보고는 `report_evidence`만 입력으로 사용한다. 값이나 원인을 확인할 수
없으면 `확인되지 않음`으로 표시한다. 최종 충돌 수가 0이어도 중간 충돌 후보를
수집하지 않은 경우에는 “충돌 회피 성공”으로 해석하지 않는다.

LLM 보고가 활성화되어 있고 호출이 성공하면 `report_source=llm`이다. 호출 또는
structured output이 실패하면 같은 evidence를 사용하는 deterministic template로
전환하고 `REPORT_TEMPLATE_FALLBACK_USED` 단계를 기록한다. 내부 추론과 전체
프롬프트는 저장하지 않는다.

## 계획 근거 API

```text
GET /v1/commands/{command_id}/plan-evidence
```

Query parameters:

- `include_candidates=false`: 기본 응답에서 후보 상세를 제외한다.
- `include_routes=true`: route segment 상세를 포함한다.
- `include_reservations=true`: 예약과 WAIT 상세를 포함한다.

계획 근거가 없는 QUERY 또는 RESET 명령은 다음을 반환한다.

```json
{
  "status": "NO_PLAN_EVIDENCE",
  "command_id": "..."
}
```

존재하지 않는 `command_id`는 HTTP 404다. 기존 계획 명령 응답에는 전체 evidence를
반복하지 않고 `evidence_summary`만 추가한다.

## 스키마와 Migration

기존 `simulation_run.output_payload` JSONB와
`command_history.result_summary` JSONB를 사용하므로 신규 Migration은 필요하지
않다. 큰 evidence는 `planning_stage_log`에 반복 저장하지 않으며 개수와 식별 가능한
요약만 기록한다.

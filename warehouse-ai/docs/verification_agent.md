# Phase 3 Verification Agent

## 범위

Phase 3는 기존 결정론적 계획·시뮬레이션 검증 결과를 종합하는 독립
Verification Agent의 안전 경계를 설명한다. 현재 Graph의 자동 재계획 연결은
Phase 4에서 별도로 구현되며 `docs/replanning.md`에 설명한다.

그래프 순서는 다음과 같다.

```text
PLAN_ONLY      validate_plan ───────┐
                                    ├─ verification ─ persist ─ report
SIMULATE_ONLY  validate_simulation ─┘
EXECUTE        validate_simulation ─ verification ─ persist
                                                   ├─ PASS 계열 → execution_precheck
                                                   └─ 그 외     → report
```

Verification Agent 자체는 재계획을 수행하지 않는다. Phase 4 Graph가
`REPLAN_LOCAL`과 `REPLAN_GLOBAL` 결과만 제한적으로 재계획 준비 노드로 보낸다.

## 역할 분리

- `validate_plan_node`: 계획과 경로를 `simulate_plan(..., include_timeline=False)`로
  결정론적으로 검증한다.
- `validate_simulation_node`: 생성된 시뮬레이션 결과의 유효성을 결정론적으로
  확정한다.
- `verification_agent_node`: 이미 계산된 결과를 evidence로 변환하고 최종 검증
  분류만 수행한다. 배정, 경로, 충돌, 거리, 시간은 다시 계산하지 않는다.

## 허용 결정

- `PASS`
- `PASS_WITH_WARNING`
- `REPLAN_LOCAL`
- `REPLAN_GLOBAL`
- `CLARIFICATION_REQUIRED`
- `FAIL`

결정론적 blocking evidence가 있으면 LLM은 `PASS` 또는
`PASS_WITH_WARNING`으로 변경할 수 없다. 반대로 결정론적 결과가 유효하면 LLM이
존재하지 않는 오류를 만들어 실패시킬 수 없다. affected robot/task와 finding,
evidence ID는 항상 결정론적 evidence에서 다시 채운다.

## Fallback

다음 경우에는 같은 evidence를 사용하는 deterministic 분류 결과를 반환한다.

- `OPENAI_API_KEY`가 없음
- LLM 호출 실패
- structured output 생성 또는 Pydantic 검증 실패

Fallback 여부와 짧은 실패 이유는 trace에 남지만 시스템 프롬프트, 전체 LLM
입출력, 내부 chain-of-thought는 저장하지 않는다.

## 저장과 API

- LangGraph State: `verification_decision`, `verification_evidence`,
  `verification_source`, `verification_prompt_version`, `verification_warnings`
- API 응답: 기존 필드는 유지하며 위 Verification 필드를 추가한다.
- `simulation_run.output_payload`: 전체 Verification 결과와 compact evidence
- `command_history.result_summary`: 결정, 재계획 범위, 영향 ID, evidence ID,
  source와 prompt version의 요약
- `planning_stage_log`: `VERIFICATION_STARTED`,
  `VERIFICATION_FALLBACK_USED`, `VERIFICATION_COMPLETED`

기존 `simulation_run.output_payload`, `command_history.result_summary`,
`planning_stage_log.details`가 JSONB이므로 Phase 3 전용 migration은 필요하지 않다.

## 실행 안전 경계

EXECUTE는 결정론적 시뮬레이션이 유효하고 Verification 결정이 `PASS` 또는
`PASS_WITH_WARNING`일 때만 `execution_precheck`로 이동한다. 동일 조건을
`execution_precheck_node`에서도 다시 확인한다. 그 외 결정은 상태와 보고서에
보존되고 실행 계획은 활성화하거나 전송하지 않는다.

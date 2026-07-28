# Phase 4 자동 재계획 Graph loop

## 구현 범위

Phase 4는 Verification 결과에 따른 제한된 Graph loop만 구현한다. 대화 메모리,
고급 보고서, 별도 이벤트 오케스트레이션 등 Phase 5 이후 기능은 포함하지 않는다.

```text
deterministic validation
        ↓
verification
  ├─ PASS / PASS_WITH_WARNING ───────────────→ persist
  ├─ CLARIFICATION_REQUIRED / FAIL ──────────→ persist → report
  ├─ REPLAN_LOCAL
  │       ↓
  │   prepare_replan(LOCAL)
  │       ↓
  └─ REPLAN_GLOBAL
          ↓
      prepare_replan(GLOBAL)
          ↓
build_problem → optimize → routing → simulation
          → deterministic validation → verification
```

재계획 후 PLAN_ONLY도 내부 안전 확인을 위해 timeline simulation과
`validate_simulation_node`를 거친다. 실제 simulation session을 변경하는 재생은
SIMULATE_ONLY 최종 성공에서만 수행한다.

## LOCAL_REPLAN

Verification evidence에 존재하는 affected task ID를 현재 `required_tasks`와
대조한다. affected robot ID만 있는 경우에는 현재 계획에서 그 로봇에 배정된
작업을 대상에 포함한다.

- 영향 작업: `changeable_task_ids`
- 나머지 작업: `fixed_task_ids` 및 `frozen=true`
- 존재하지 않는 task/robot ID: 대상에서 제거
- 보호 대상을 제외하고 변경할 작업이 없으면 FAIL

Optimizer는 영향 작업만 다시 삽입한다. 영향 작업은 다른 가용 로봇으로
재배정될 수 있지만 영향받지 않은 작업의 기존 배정은 보존한다.

## GLOBAL_REPLAN

현재 `required_tasks` 중 보호되지 않은 전체 작업을 `changeable_task_ids`로
설정한다. 실행 중 작업과 freeze horizon 작업은 전역 재계획에서도 고정한다.

## 실행 중 작업과 freeze horizon

보호 대상은 다음 자료로 계산한다.

1. SQL `works.status=EXECUTING`
2. Redis `executing_task_ids`
3. 실제 Redis 활성 계획의 scheduled task가 현재 step부터 freeze horizon
   종료 step 사이에 겹치는 경우

활성 계획에 scheduled task 정보가 없는 이전 형식에서는 같은 시간 구간의
route 전체를 보수적으로 보호한다. 재계획 중 생성된 후보 계획은 아직 실행된
계획이 아니므로 후보 자체의 미래 구간에는 freeze horizon을 다시 적용하지
않는다.

## 무한 loop 방지

- 횟수 기준: `SupervisorDecision.max_replan_attempts`
- 시스템 절대 상한: 3회
- 동일 failure signature 2회: 즉시 FAIL
- 재계획 대상 없음: 즉시 FAIL
- LangGraph recursion limit: 50

failure signature는 다음 결정론적 값의 canonical JSON을 SHA-256으로 계산한다.

- blocking evidence source와 code
- evidence에 포함된 robot/task ID
- evidence에 포함된 node ID와 time-step
- Verification의 affected robot/task ID

메시지 문장이나 LLM summary는 signature에 넣지 않는다.

## plan version과 이력

첫 Routing 성공 시 최초 후보에 `original_plan_version`과
`current_plan_version`을 생성한다. 매 재계획 준비마다 새 UUID를 발급하고
이전·신규 버전을 `replan_history`에 함께 저장한다. `command_id`는 전체 loop에서
변경하지 않는다.

각 이력에는 attempt, scope, reason, 영향·보호 대상, 이전·신규 plan version,
재계획 전후 Verification, failure signature와 상태가 포함된다.

## 저장과 단계 로그

다음 stage가 기존 `planning_stage_log.details` JSONB에 저장된다.

- `REPLAN_REQUESTED`
- `LOCAL_REPLAN_STARTED`
- `GLOBAL_REPLAN_STARTED`
- `REPLAN_COMPLETED`
- `REPLAN_FAILED`
- `REPLAN_LIMIT_REACHED`
- `REPEATED_FAILURE_DETECTED`

최종 `simulation_run.output_payload`와 `command_history.result_summary`에도 compact
재계획 이력이 저장된다. 기존 JSONB 컬럼을 사용하므로 신규 migration은 없다.

## 실행 안전성

재계획 loop에서는 Redis 활성 계획 교체, Robot Gateway dispatch, 실제 재고·작업
갱신을 수행하지 않는다. 최종 Verification이 PASS 계열이고 실행 모드가
EXECUTE일 때만 기존 `execution_precheck → activate → dispatch` 경로로 진입한다.

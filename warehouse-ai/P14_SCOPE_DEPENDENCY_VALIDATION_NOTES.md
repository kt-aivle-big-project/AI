# P14 Scope and Dependency Validation Fix

## Purpose

P13 completed cost-based charger selection and explainability, but the real
Swagger run exposed two remaining consistency gaps:

1. The same bounded single-robot what-if command could be classified as
   `LOCAL_REPLAN` or `GLOBAL_REPLAN` depending on the LLM response.
2. `execution_task_dependencies` existed in optimizer metadata, while
   `schedule_validation` still reported `dependency_count: 0` and did not
   prove that CHARGE/PICK/DROP ordering was respected after routing.

## Changes

### Deterministic supervisor scope

A bounded `HYPOTHETICAL_SCENARIO` is fixed to `LOCAL_REPLAN` when it contains:

- exactly one referenced robot;
- at most one inventory operation or target task;
- no broad daily schedule;
- at most one temporary node/edge assumption.

Broader hypothetical scenarios are fixed to `GLOBAL_REPLAN`. The LLM still
provides the explanation, but cannot widen or shrink the deterministic scope.
If an otherwise valid planning command is allowed to replan by policy, an LLM
response can no longer silently disable the configured recovery path.

`decide_scope_node` still forces `INITIAL_PLAN` when no base/active plan exists.

### Post-routing execution dependency validation

After routing schedule reconciliation, P14 validates optimizer-generated task
relationships against actual start/end time steps:

- referenced tasks must exist;
- FINISH_TO_START timing and lag must be satisfied;
- the generated task dependency graph must be acyclic.

The merged `schedule_validation` now includes:

- `business_dependency_count`;
- `execution_dependency_count`;
- total `dependency_count`;
- `execution_dependency_order`;
- `execution_dependency_violations`;
- `validated_after_routing: true`.

Violations become blocking Verification evidence using:

- `EXECUTION_DEPENDENCY_TASK_MISSING`;
- `EXECUTION_DEPENDENCY_ORDER_VIOLATION`;
- `EXECUTION_DEPENDENCY_CYCLE`.

## Expected result for the battery scenario

With an active plan and one fixed robot (`R2-03`):

- Supervisor/Scope: `LOCAL_REPLAN`;
- `allow_replan: true`;
- `execution_dependency_count: 2`;
- order: `CHARGE -> PICK -> DROP`;
- `schedule_validation.valid: true`;
- Verification: `PASS` when all other checks pass.

## Test result

- Focused P12-P14 and supervisor/planning-mode tests: 29 passed.
- Full regression suite: 561 passed.
- Python compile check: passed.

The container test run used temporary dependency stubs because external runtime
packages are not installed there. Stub files are not included in the package.

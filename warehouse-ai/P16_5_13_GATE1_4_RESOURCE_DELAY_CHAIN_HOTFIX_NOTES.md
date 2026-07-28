# P16.5.13 Gate 1.4 — Resource Delay Chain Hotfix

## Problems reproduced from Swagger

The low-battery scenario completed the managed cuOpt two-pass optimization and
created the complete R2-02 chain, but routing failed after charger-slot
serialization:

```text
RESOURCE_DELAY_HARD_WINDOW_VIOLATION
CHARGE end=150 latest=146
```

The same run also reported `ROBOT_STATE_OVERRIDE_NOT_APPLIED` even though the
optimization problem and charger calculation both used the requested 21%.

A full regression pass additionally exposed two far-future routing regressions
that materialized every pre-activation step as WAIT waypoints.

## Root causes

1. The first-pass planned CHARGE end was copied into the explicit second-pass
   task as a user-equivalent `HARD_WINDOW`.
2. Shared-resource delays correctly shifted the robot chain, but the synthetic
   hard latest-finish rejected that shift.
3. Verification required simulation battery metrics even when routing failed
   before simulation could produce them.
4. Standalone future routes without an activation flag defaulted to dense WAIT
   materialization, while strict idle-whitelist plans need the opposite policy.

## Fixes

- Treat the first-pass CHARGE end as an auditable optimizer target only.
- Inherit a hard latest-finish only from a successor that explicitly carries a
  user `HARD_WINDOW`.
- Allow charger/service serialization to shift CHARGE and every downstream task
  on the assigned robot while preserving dependencies and robot binding.
- Validate the battery override against the optimization problem first; compare
  simulation metrics only when those metrics exist.
- For a far-future first task beyond the bounded MAPF horizon:
  - default to sparse activation when no strict idle-whitelist policy exists;
  - keep explicit holding relocation for strict idle-whitelist plans;
  - honor an explicit `defer_initial_pre_activation` override from the planner.
- API version: `2.5.13.4`.
- Response schema remains `p16.5.12.1`.

## Regression coverage

Added tests for:

- optimizer target versus user hard-window separation;
- four-step charger-slot delay propagation through
  `CHARGE -> MOVE_TO_NEXT -> PICK -> DROP`;
- removal of false battery-override evidence after routing failure;
- far-future sparse route activation and strict idle-whitelist compatibility.

Final regression result, executed in two bounded test batches:

```text
391 passed
331 passed
722 passed / 0 failed
```

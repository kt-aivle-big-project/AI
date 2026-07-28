# P16.5.12 Runtime Robot-State Partial Replanning

## Goal

Use verified execution telemetry to replan only the unfinished, mutable portion
of an active warehouse plan while preserving completed work, current safe work,
the freeze horizon, and unaffected robots.

## New execution events

- `LOW_BATTERY`
- `POSITION_UPDATED` now participates in deterministic deviation analysis

Existing runtime anomalies remain supported:

- `ROBOT_DELAYED`
- `ROBOT_FAILED`
- `TASK_FAILED`
- `PATH_BLOCKED`
- `PATH_DEVIATED`

## Runtime partial-replan contract

`impact_analysis` now reports:

- `completed_task_ids`
- `frozen_task_ids`
- `changeable_task_ids`
- `freeze_horizon_seconds`
- `partial_replan_policy`
- `robot_state_overrides`

The event response mirrors the contract under `partial_replan`.

## Safety policy

1. Completed tasks are immutable.
2. Executing and near-term tasks inside the freeze horizon are protected.
3. Unaffected tasks remain fixed during `LOCAL_REPLAN`.
4. A blocked/deviated path, failed task, or failed robot may unfreeze only its
   directly affected unfinished work.
5. A critical low-battery event can release the affected robot's current task
   after a safe stop or when battery is already at/below the minimum reserve.
6. A moderate low-battery event preserves the current execution and replans
   future work to insert charging or reassign later tasks.

## Base-plan replay

The event's injected or live active plan is carried in
`ScenarioDefinition.source_plan_snapshot`. The planning graph selects it as
`EVENT_SOURCE_PLAN`; it is no longer forced to reconstruct a new plan from only
natural-language text.

## Robot-state propagation

Runtime `node_id`, `battery`, and `status` values are copied into
`robot_state_overrides` and applied to the optimization problem. Low-battery
telemetry is also represented as a deterministic `LOW_BATTERY` hypothetical
event so charging verification remains active.

## Release gate

```powershell
python -m scripts.run_p16_5_12_final_checks
```

Expected result:

```text
111 passed
```

The full repository test suite currently reports `699 passed, 10 failed`. The
10 failures are pre-existing legacy insert-time rebase, P11 adapter, and P15
priority-order tests and are not used as this release gate.

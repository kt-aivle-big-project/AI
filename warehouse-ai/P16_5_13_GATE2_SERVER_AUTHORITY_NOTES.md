# P16.5.13 Gate 2 — Server-authoritative runtime state

API version: `2.5.13.5`  
Response schema compatibility: `p16.5.12.1`

## Goal

Execution-event clients report telemetry and operational facts. They do not own
or select the active plan, plan version, runtime clock, or simulation source
state. Gate 2 moves those values behind a server-authoritative boundary.

## Runtime source policy

| Context | Active-plan source | Runtime clock |
|---|---|---|
| `REAL` | warehouse Redis `active_plan` | server plan `activated_at` / `reference_time` |
| `SIMULATION` | `simulation_id` Redis session, PostgreSQL session fallback, latest run fallback | server simulation plan `reference_time` |

The following client `payload` fields are removed before internal event
processing:

```text
active_plan
active_plan_version
current_time_step
reference_time
activated_at
time_step_seconds
server_runtime
_server_runtime
```

The response exposes them under `runtime_context.ignored_client_fields` so the
boundary is auditable.

## POSITION_UPDATED contract

`POSITION_UPDATED` performs telemetry mutation only. It never invalidates the
route by itself. Route divergence must be reported as `PATH_DEVIATED`.

After telemetry is stored, the server may derive an internal `LOW_BATTERY`
event using:

```text
minimum battery
+ configured safety margin
+ remaining scheduled energy for that robot
```

The derivation is visible in `server_derived_event_evidence`. A safe telemetry
update returns `TELEMETRY_UPDATED` without invoking the planner. A derived
low-battery anomaly follows the existing verified partial-replan pipeline.

## Simulation plan persistence

A successful verified simulation stores a compact authoritative plan in the
simulation Redis namespace. The same plan is copied into the PostgreSQL
simulation session state for recovery. If Redis plan state is missing, PostgreSQL session state or the latest
simulation run reconstructs the plan without trusting client payload.

Stored contract includes:

```text
plan_version
reference_time
required_tasks
cuopt_plan
collision_plan
ready/waiting/blocked task ids
resource reservations
robot command batches
```

## Time-relative simulation status

Simulation Redis contains a fully replayed final state for audit. When an event
is injected at an earlier `occurred_at`, task completion/freeze status is
therefore derived from the authoritative plan clock rather than the final
replayed work rows. This prevents all tasks from being incorrectly classified
as already completed.

## Event response fields

```text
reported_event_type
effective_event_type
server_derived_event
server_derived_event_evidence
runtime_context.source
runtime_context.active_plan_version
runtime_context.current_time_step
runtime_context.ignored_client_fields
partial_replan.runtime_source
```

## Regression gate

```powershell
python -m scripts.run_p16_5_13_gate2_checks
python -m scripts.run_p16_5_13_gate2_checks --full  # optional
```

Validated in the artifact environment:

```text
Gate 2 focused regression: 39 passed
Full project regression: 731 passed / 0 failed
compileall: PASS
```

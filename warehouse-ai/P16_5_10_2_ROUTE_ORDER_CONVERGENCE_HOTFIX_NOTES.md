# P16.5.10.2 — Route-order and shared-resource convergence hotfix

## Live failure reproduced

P16.5.10.1 successfully preserved first-pass robot bindings and removed the
managed cuOpt mixed-PDP HTTP 400. The remaining live failure occurred after
routing:

```text
RESOURCE_SCHEDULER_DID_NOT_CONVERGE
RESOURCE_DELAY_HARD_WINDOW_VIOLATION ... end=19033 latest=5580
```

The second `CHARGE` task had a numerically smaller priority than the earlier
business `PICK` and `DROP`. Internal MAPF sorted each robot's tasks by
`priority` before `start_time_step`, so it routed the later charge before its
predecessor. Routing reconciliation therefore produced this contradictory
order:

```text
CHARGE-2 -> PICK-1 -> MOVE-2 -> DROP-1
```

while the execution dependency remained:

```text
DROP-1 -> CHARGE-2
```

The shared-resource scheduler attempted to satisfy both orders. Shifting
`CHARGE-2` also shifted `DROP-1`, preserving the same violation and adding the
same delay on every iteration. The schedule grew from roughly 4,000 steps to
more than 19,000 steps before the bounded loop stopped.

## Fix

1. **Start time is authoritative for routing order**
   - Robot tasks are no longer routed with priority as the primary key.
   - The deterministic tie-break is now:
     `start_time_step -> end_time_step -> priority -> task_id`.

2. **Execution dependencies override conflicting synthetic priorities**
   - A shared dependency-aware topological ordering utility is used by both
     internal MAPF and shared-resource scheduling.
   - Same-robot `CHARGE -> MOVE -> PICK -> DROP` chains remain ordered even when
     generated priority values are not monotonic.

3. **Fail-fast cycle protection**
   - Same-robot dependency order conflicts are reported as
     `RESOURCE_DEPENDENCY_ORDER_CONFLICT`.
   - Explicit dependency cycles are reported as
     `ROBOT_TASK_DEPENDENCY_CYCLE` instead of repeatedly inflating times.

4. **Observability**
   - Collision-plan metadata now records:

```text
task_ordering_policy = START_TIME_DEPENDENCY_AWARE_PRIORITY_TIEBREAK
```

## Regression coverage

The added regression recreates the live priority pattern where the later charge
has priority 9 and the earlier pick has priority 10. It verifies that:

- MAPF routes the dependency chain in chronological order.
- Post-routing shared-resource reconciliation converges.
- `RESOURCE_SCHEDULER_DID_NOT_CONVERGE` is absent.
- The final makespan does not grow into the 19,000-step range.

## Expected response

```text
response_schema_version = p16.5.10.2
status = SIMULATION_SUCCESS
result.valid = true
result.resources.valid = true
result.objective.status = PASS
result.collision_resolution.final_conflict_count = 0
errors = []
```

# P16.5.10.1 — Second-pass robot binding and mixed PDP hotfix

## Live failure reproduced

P16.5.10 generated explicit `CHARGE` and `MOVE` tasks correctly, but the second
optimizer pass was still allowed to reassign ordinary business tasks. A charger
visit selected for `R2-02` was built around a later business task, while CPU
fallback reassigned an earlier outbound task to the same robot. Idle planning
then reserved the robot until the stale later successor and pushed the earlier
outbound past its hard window.

The managed cuOpt request also mixed standalone `CHARGE`/`MOVE` tasks with
`pickup_and_delivery_pairs`. The managed schema requires all task-location
indices to participate when that field is present, so the request returned HTTP
400 and fell back to CPU.

## Fix

1. **First-pass robot assignment is authoritative**
   - Every business task is copied into the second-pass problem with
     `assigned_robot_id` from the first-pass plan.
   - Exact times remain changeable (`frozen=false`).
   - The second pass optimizes robot-bound visit order, not robot reassignment.

2. **Mixed PDP field is disabled only in the second pass**
   - `cuopt_disable_pickup_delivery_pairs=true` is set on the enriched problem.
   - All business, charge, and relocation tasks are represented in
     `order_vehicle_match`.
   - PICK→DROP and CHARGE→MOVE→business dependencies remain hard-validated by
     the local warehouse normalizer after cuOpt returns.

3. **Robot-chain safety invariant**
   - The second-pass result is checked against the first-pass business bindings,
     explicit charger bindings, and relocation bindings.
   - Missing or reassigned tasks raise structured
     `SECOND_PASS_ROBOT_BINDING_VIOLATION` errors before routing.

## Expected response

```text
response_schema_version = p16.5.10.1
status = SIMULATION_SUCCESS
result.optimizer_roles.mode = TWO_PASS_EXPLICIT_CHARGE_VISITS
result.optimizer_roles.second_pass_role = ROBOT_BOUND_BUSINESS_AND_CHARGE_VISIT_ORDER
result.resources.valid = true
result.objective.status = PASS
errors = []
```

The cuOpt warning containing
`pickup_and_delivery_pairs assignments must be ... and all task location indices must be used`
should no longer appear.

# P16.5.10.3 Charge Command Boundary Hotfix

## Symptom

The routed plan, shared-resource reservations, collision plan, battery
reconciliation, and operational objective were valid, but deterministic
verification rejected the plan because robot CHARGE commands were one time step
shorter than the final reconciled plan:

- 60 seconds planned, 55 seconds emitted
- 35 seconds planned, 30 seconds emitted

The repeated signature guard then stopped LOCAL_REPLAN after two identical
verification failures.

## Root cause

The last CHARGE waypoint occurs at the exact time step where the following
explicit MOVE assignment begins. Both assignments therefore matched the same
node/time boundary. `RobotAdapter` selected the final generic match, which was
MOVE, and attributed the last physical CHARGE waypoint to the MOVE task ID.

The route itself still reserved the full charging duration. Only command task
ownership and duration aggregation were wrong.

## Fix

For waypoints whose physical action is `CHARGE`, `RobotAdapter` now prefers the
matching scheduled CHARGE assignment at the current charger node. Generic
matching remains the fallback for legacy plans.

This preserves all charging time steps under the original CHARGE task ID and
prevents a synthetic MOVE task from receiving a CHARGE command at the boundary.

## Regression coverage

- 60-second charge followed by MOVE at the same boundary
- 35-second reconciled charge followed by MOVE at the same boundary
- no CHARGE command emitted under the following MOVE task ID
- P12 charging execution and P16.5.10 routing/resource regressions

## Expected response

```text
response_schema_version = p16.5.10.3
status = SIMULATION_SUCCESS
result.resources.valid = true
result.objective.status = PASS
errors = []
```

The following findings must be absent:

```text
CHARGE_DURATION_NOT_ROUTED
CHARGE_DURATION_MISMATCH
동일한 검증 실패 signature가 2회 반복되었습니다.
```

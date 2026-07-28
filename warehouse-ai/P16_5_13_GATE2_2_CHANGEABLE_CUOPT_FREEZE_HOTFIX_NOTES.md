# P16.5.13 Gate 2.2 — Changeable cuOpt freeze hotfix

## Live failure

The server correctly converted the reported `POSITION_UPDATED` event into a
server-derived `LOW_BATTERY` event and correctly marked the affected C PICK and
DROP tasks as changeable. The managed cuOpt assignment application then treated
the existing `assigned_robot_id` as an automatic timing freeze.

That produced this invalid contract:

```text
assigned_robot_id = R2-03
frozen = true
changeable = true
```

The local optimizer therefore preserved PICK and DROP unchanged and could not
insert a CHARGE visit before them. Final route-energy reconciliation rejected
the plan.

## Corrected contract

For `LOCAL_REPLAN`, a task in `changeable_task_ids` retains its robot identity
without becoming timing-frozen:

```text
assigned_robot_id = R2-03
frozen = false
changeable = true
```

Tasks in `fixed_task_ids` still take precedence and remain frozen. Unaffected
protected work is unchanged.

## Expected recovery

```text
R2-03 at node 2080, battery 21%
-> move to safe charger 2152
-> CHARGE
-> C PICK at 2088
-> C DROP at 2146
-> final battery >= 20%
```

## Regression coverage

- cuOpt assignment application keeps changeable LOCAL_REPLAN tasks robot-bound
  and unfrozen.
- fixed scope remains protected.
- the real warehouse graph with R2-03 at node 2080 and 21% battery produces
  `CHARGE -> PICK -> DROP`.
- existing multi-robot rebalance and Gate 2 server-authority regressions remain
  green.

## Validation

```text
compileall: PASS
focused regression: 47 passed / 0 failed
full regression: 734 passed / 0 failed
```

## Environment impact

```text
PostgreSQL migration: none
PostgreSQL reset: none
Redis reset: none
Neo4j seed: none
Mock Robot Gateway change: none
Mock Robot Gateway required for SIMULATE_ONLY: no
API restart: required after changing package
new simulation_id and event_id: required for live revalidation
```

# P16.5.13 Gate 2.3 — Low-battery E2E charge retention hotfix

## Swagger failure reproduced

The Gate 2.2 live response proved that server authority, low-battery detection,
and changeable-task freeze release were correct, but the final routed plan was
still rejected for R2-03 battery safety.

The affected task window had already opened before the event:

```text
plan/event reference: 2026-07-27T00:05:22Z
C PICK earliest_start: 2026-07-27T00:00:00Z
```

During the explicit charge second pass, the historical successor
`earliest_start` was promoted to the newly inserted CHARGE task's
`latest_finish`. This created an impossible interval:

```text
CHARGE earliest_start = 00:05:22
CHARGE latest_finish  = 00:00:00
```

The second-pass normalizer could then lose the affected CHARGE/PICK/DROP chain,
leaving final route-energy validation to fail closed.

## Corrected contracts

1. A successor HARD_WINDOW start constrains CHARGE completion only when it is
   genuinely later than the first-pass charge target end.
2. A historical/opened earliest-start remains a lower bound that has passed;
   it is never promoted into a new CHARGE deadline.
3. Server-derived LOW_BATTERY plans receive a bounded post-optimizer guard.
   When business work remains on the reporting robot but its CHARGE task is
   absent, the deterministic local optimizer runs once against the same
   problem and must restore the charge visit without unassigned tasks.
4. Failed automatic replans now expose bounded debug evidence:
   `charge_visit_two_pass`, scheduled charge rows,
   `changeable_robot_bound_task_ids`, charge-retention recovery,
   route-energy reconciliation, schedule validation, and the final trace tail.

Expected affected-chain order:

```text
R2-03 current node 2080
-> safe active charger
-> CHARGE
-> MOVE_TO_NEXT
-> C PICK
-> C DROP
```

## Environment impact

```text
PostgreSQL migration: none
PostgreSQL reset: none
Redis reset: none
Neo4j change/seed: none
Mock Robot Gateway change: none
Mock Robot Gateway required for SIMULATE_ONLY: no
API restart after package switch: yes
New simulation_id and event_id for live verification: yes
```

## Verification

```text
Focused Gate 2.3 regression: 30 passed, 0 failed
Full project regression: 738 passed, 0 failed
compileall: PASS
```

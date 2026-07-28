# P16.5.8.2 Verification & Simulation Replay Hotfix

## Live failure reproduced

The P16.5.8.1 Swagger run produced valid routes and charging tasks, but final
verification failed for two independent state-consistency reasons.

1. The charger planner selected node 2150 using optimizer-time evidence. Route
   energy reconciliation then increased only the selected candidate's charge
   duration. Verification ranked the mutated candidates again and incorrectly
   expected node 2151.
2. LOCAL_REPLAN reused the same Redis simulation session after the first
   candidate had already applied outbound inventory deltas. Replaying the full
   plan applied the same lot deduction twice (`20 -> 0 -> -20`).

## Changes

- Charger verification prefers immutable planner `selection_key` evidence.
- Route energy reconciliation keeps planner ranking inputs unchanged and writes
  operational values to `reconciled_*` fields.
- A replan resets the candidate Redis simulation state before replaying the full
  plan from the SQL snapshot.
- Linked charger waiting-area IDs are deduplicated.
- Added regression tests for the live 2150/2151 tie and duplicated inventory
  replay.

## Expected Swagger result

- no `OPPORTUNITY_CHARGER_POLICY_SELECTION_INVALID`
- no `simulation session 재생 실패: 재고 음수 방지`
- no repeated failure signature
- `verification.decision` is `PASS` or `PASS_WITH_WARNING`
- `result.metrics.total_distance > 0`
- charging tasks remain present
- `final_conflict_count = 0`

`CAPACITY_DATA_NOT_CONFIGURED` remains a non-blocking P16.5.9 warning.

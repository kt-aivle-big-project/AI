# P16.5.8.1 Opportunity-Charging Replan Stability Hotfix

## Fixed failures

P16.5.8 could build and simulate the first charger-aware route successfully, but
verification could reject it because the opportunity-charge planner and the
verification layer applied different charger cost rules.  The subsequent local
replan could then fail with `list index out of range` when a regenerated CHARGE
segment began exactly at the preserved candidate-route endpoint.

## Changes

- Shared opportunity-charger ranking policy is used by planning and verification.
- Configured cost is compared only when every feasible candidate has cost data.
- Incomplete cost data uses one uniform distance fallback; missing cost is never
  treated as zero.
- Candidate evidence records `cost_mode`, `total_selection_cost`,
  `selection_key`, `selection_rank`, and exactly one `selected` charger.
- Synthetic `opportunity:*` CHARGE tasks, dependencies, and selection evidence
  are removed and regenerated on every replan pass.
- Replanned route prefix deduplication no longer indexes an emptied waypoint list.
- Unexpected routing `IndexError` is converted to
  `INTERNAL_ROUTING_STATE_ERROR` instead of leaking a Python exception.
- The last route/simulation-successful candidate is preserved in
  `previous_successful_candidate` when a later verification/replan fails.

## Response versions

```text
API version: 2.5.8.1
response_schema_version: p16.5.8.1
```

## Focused checks

```powershell
python -m scripts.run_p16_5_8_1_final_checks
```

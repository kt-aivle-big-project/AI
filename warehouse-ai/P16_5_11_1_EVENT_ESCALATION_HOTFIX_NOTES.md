# P16.5.11.1 Event Replan Escalation Hotfix

## Observed live failure

A first `PATH_BLOCKED` event correctly produced `LOCAL_REPLAN` and
`REPLAN_VERIFIED`. Sending the same route-failure signature a second time was
stopped by the event service with:

```text
status = FAILED
failure_reason = REPEATED_FAILURE_DETECTED
auto_replan_requested = false
```

This happened before the P16.5.11 planning-level MAPF policy could apply its
single LOCAL-to-GLOBAL escalation.

## Root cause

There were two independent repeated-failure guards:

1. `EventReplanService` counted recent event signatures and stopped the second
   event immediately.
2. The planning graph allowed a retryable MAPF local failure to expand once to
   `GLOBAL_REPLAN`.

The event guard ran first, so Swagger-driven event tests could never reach the
second policy.

## Hotfix policy

For route failures only (`PATH_BLOCKED`, `PATH_DEVIATED`):

```text
first identical signature  -> LOCAL_REPLAN
second identical signature -> GLOBAL_REPLAN once
third identical signature  -> FAILED / REPEATED_FAILURE_DETECTED
```

Other repeated operational events keep the original stop guard.

The second event response exposes:

```text
original_scope = LOCAL_REPLAN
scope = GLOBAL_REPLAN
escalated_from_local = true
repeat_count = 1
auto_replan_requested = true
```

## Consistency fixes

- `payload.active_plan.plan_version` is copied into
  `impact_analysis.active_plan_version` when Redis has no active version.
- `final_status` now matches `status` for `REPLAN_NOT_REQUIRED`,
  `REPLAN_VERIFIED`, `APPROVAL_REQUIRED`, and `FAILED` event responses.
- Inactive `result.mapf_replan.version` is fixed to `p16.5.11.1` instead of
  `null`.

## Validation

```text
Event + MAPF focused tests: 32 passed
Focused release gate: 105 passed
compileall: PASS
```

Run:

```powershell
python -m scripts.run_p16_5_11_1_final_checks
```

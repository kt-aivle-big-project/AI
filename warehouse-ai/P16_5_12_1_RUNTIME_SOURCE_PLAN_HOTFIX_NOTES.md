# P16.5.12.1 Runtime Source Plan Hotfix

## Observed Swagger failure

A `LOW_BATTERY` event correctly produced a local partial-replan contract:

- current and unaffected tasks were protected;
- the affected robot's future task was changeable;
- the runtime battery override was captured.

The generated planning command nevertheless returned `FAILED` with no generated plan version, simulation id, verification decision, or diagnostic error.

## Root cause

The runtime plan contained temporary task IDs that were intentionally not persisted as SQL work rows. Three downstream stages still assumed every planning target must exist in SQL:

1. Snapshot validation rejected source-plan robot/task identifiers.
2. Inventory precheck compared full runtime task IDs with SQL work IDs.
3. Task selection rebuilt only persisted SQL works and dropped `source_plan_snapshot.required_tasks`.

As a result, the planning graph stopped before a valid optimization/verification response was produced.

## Fix

`EVENT_SOURCE_PLAN` is now treated as an explicit planning contract for temporary runtime tasks:

- runtime task IDs are resolved through their source-plan `work_id`;
- source-plan robots, assignments, nodes, and task identifiers participate in Snapshot validation;
- source-plan `required_tasks` are materialized into `AtomicTask` records;
- missing candidates and assigned robots may be recovered from source-plan scheduled tasks;
- protected/changeable status is reapplied while materializing runtime tasks;
- planner failures without a verification decision now expose a deterministic diagnostic error.

SQL remains authoritative for persisted warehouse works and inventory.

## Expected LOW_BATTERY flow

```text
LOW_BATTERY event
→ partial impact contract
→ EVENT_SOURCE_PLAN selected
→ temporary runtime tasks accepted
→ protected tasks frozen
→ changeable tasks materialized
→ optimize / route / simulate / verify
→ REPLAN_VERIFIED or an explicit diagnostic failure
```

## Version

- API: `2.5.12.1`
- Response schema: `p16.5.12.1`

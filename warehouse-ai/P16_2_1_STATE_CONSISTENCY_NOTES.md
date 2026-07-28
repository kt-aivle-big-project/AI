# P16.2.1 State Consistency Hotfix

## Problem

Repeated development experiments could leave PostgreSQL work status and the Redis active plan out of sync. A completed work could disappear from `open_works` while its old tasks remained in the Redis active plan and were preserved as frozen tasks in later simulations.

## Fix

- Snapshot now loads every persisted work ID and status in addition to open works.
- Active-plan tasks are removed only when their matching persisted work is explicitly terminal: `COMPLETED`, `CANCELLED`, or `FAILED`.
- Candidate tasks without a persisted works row are preserved.
- If every task in the active plan is terminal, the stale active plan is ignored for scope selection.
- A `STALE_ACTIVE_PLAN_TASKS_DROPPED` warning and trace event are emitted when repair is applied.

## Demo state reset

For repeatable local experiments, restore seed rows and clear Redis live/simulation state before a regression run:

```powershell
python -m scripts.seed_inventory_demo_data --warehouse-id 2 --storage-node-id 2088 --outbound-node-id 2146 --confirm
python -m scripts.reset_demo_data --warehouse-id 2 --confirm
```

The reset preserves append-only command and simulation audit history.

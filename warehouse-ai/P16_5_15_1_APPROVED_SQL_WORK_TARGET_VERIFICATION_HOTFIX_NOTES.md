# P16.5.15.1 — Approved SQL work target verification hotfix

## Problem

A clarification-bound PostgreSQL outbound work is represented by one legacy task:

```text
<work_id>:move
```

The execution adapter expands that task into `PICKUP -> MOVE -> DROPOFF`, but final
verification accepted only an outbound `DROP` row as evidence that the user-requested
destination had been applied. A valid plan such as:

```text
P16-W-OUT-2-C-001:move
2088 -> 2146
```

was therefore rejected with `TARGET_NODE_NOT_APPLIED` before approval and gateway
dispatch.

## Fix

The target verifier now accepts a legacy `MOVE` only when all conditions hold:

1. Its `target_node` equals a requested target.
2. Its `work_id` is explicitly linked to a requested outbound inventory operation.
3. The inventory-feasibility result planned a positive quantity for that work.
4. Its task identity is exactly `<work_id>:move`.

This keeps ordinary relocation, parking, and unrelated MOVE rows from satisfying an
outbound command constraint. Existing PICK/DROP verification behavior is unchanged.

`operation_id="work:<work_id>"` is also normalized when a clarification-bound SQL work
is surfaced without a separate `work_id` field.

## Regression coverage

- approved SQL outbound MOVE to the requested destination: PASS
- `work:<work_id>` operation namespace binding: PASS
- wrong target: blocked
- unrelated relocation MOVE: blocked
- matching work with noncanonical MOVE task identity: blocked
- previous P16.3.1 partial-target cases: preserved

## Release checks

```powershell
python -m scripts.run_p16_5_15_1_checks
python -m scripts.run_p16_5_15_1_checks --full
```

The existing P16.5.15 database migration remains required. No new migration, reset,
seed, or environment variable is introduced by this hotfix.

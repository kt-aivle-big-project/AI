# P16.1 Daily Schedule & Outbound Hotfix

## Scope

P16.1 fixes the daily-schedule and multi-item outbound defects found after the
P16 final integration.  INBOUND robot task generation remains intentionally
separated into P16.2.

## Fixed

1. Natural-language windows such as `오전 9시부터 오전 11시` are bound to
   command-created inventory operation IDs.
2. An explicit planning reference date is resolved first and reused by the
   schedule and inventory parsers.
3. Warehouse-local windows are converted deterministically to UTC.
4. Outbound inventory feasibility is checked at the earliest PICK time instead
   of only at the window deadline.
5. Multiple FEFO lots at the same storage node are packed into one physical
   PICK/DROP transport pair when robot capacity permits; lot identities remain
   attached for reservations and inventory commit.
6. A PLAN/EXECUTE request with inventory operations but zero scheduled tasks is
   blocked with `EMPTY_EXECUTION_PLAN`.

## Expected example

- Planning reference: `2026-07-24 07:15 Asia/Seoul`
- Requested window: `2026-07-24 09:00-11:00 Asia/Seoul`
- UTC window: `2026-07-24T00:00:00Z-2026-07-24T02:00:00Z`
- A and B each receive one operation-level HARD_WINDOW constraint.

## Validation

- Focused P16.1 tests: `5 passed`
- Related regression tests: `247 passed`
- Full regression: `579 passed`
- Existing P16 release check: `all_passed: true`
- P16.1 hotfix check: `all_passed: true`

## Deferred to P16.2

- INBOUND source-node selection
- Storage node 2088 extraction and application
- PICKUP/MOVE/DROPOFF generation
- INBOUND capacity-aware execution planning

# P16.3.1 Final Verification Hotfix

## Why the real Swagger run failed

The uploaded integration result used `2026-07-24 07:15 Asia/Seoul` as the
planning reference.  The demo A/B lots had `available_at` around
`2026-07-24 15:35 Asia/Seoul`, so both lots were correctly unavailable at the
requested reference time.  Therefore A and B were inventory-blocked and only
the independent C inbound operation was planned.

The previous verifier then required the global outbound destination 2146 even
though no outbound operation survived inventory feasibility.  This converted a
valid inventory partial result into `VERIFICATION_FAILED` with
`TARGET_NODE_NOT_APPLIED`.

## Fix

- Validate an outbound target only against outbound operations whose
  `planned_quantity_boxes` is positive.
- If every outbound operation is inventory-blocked, skip the outbound target
  application check.
- An independent inbound DROP can no longer be mistaken for outbound target
  evidence.
- If a planned outbound operation exists and uses the wrong target, verification
  still blocks the plan.

## Correct real-data acceptance command

The demo lots shown in the real response become available before 16:00 KST.
Use a 16:00 planning reference and later windows so A=40 remains short for a
50 BOX request while B=20 is available.

- Planning reference: 2026-07-24 16:00 KST
- Outbound window: 17:00-19:00 KST
- Inbound window: 19:00-21:00 KST

Expected outcome:

- A OUTBOUND: blocked, planned 0, shortage 10
- B OUTBOUND: planned 20, destination 2146
- C INBOUND: planned 50, destination 2088
- Four tasks total
- No CHARGE when selected robots remain above 20%
- `PARTIAL_SUCCESS_WITH_EMERGENCY`
- `gateway_dispatched=false`

# P16.5.13 Gate 2.1 — Low-battery safety charge hotfix

## Observed Swagger failure

A server-authoritative `POSITION_UPDATED` event for `R2-03` at node `2080`
with battery `21%` was correctly converted to `LOW_BATTERY` because the
minimum reserve, safety margin, and remaining planned energy required more than
21%. The event scope, however, kept the currently executing C-item PICK task
inside the freeze horizon and allowed only its DROP task to change.

The optimizer therefore could not insert a CHARGE visit before the current PICK.
The resulting candidate contained no CHARGE task and final route-energy
verification rejected `R2-03` below the 20% reserve.

## Fix

- A server-derived LOW_BATTERY event now releases only its affected task chain
  from the freeze horizon.
- Unrelated and future work remains protected.
- When the reported battery is still above the hard minimum, the affected
  PICK/DROP chain remains bound to the same robot while exact timing is
  rescheduled.
- This allows the local warehouse normalizer and cuOpt two-pass contract to
  generate `current position -> safe charger -> CHARGE -> PICK -> DROP`.
- Battery at or below the hard minimum retains the existing emergency
  stop/reassignment behavior instead of forcing same-robot continuation.

## Regression fixture

The regression uses the real warehouse-2 example topology and the live failure
conditions:

- robot: `R2-03`
- current node: `2080`
- battery: `21%`
- pickup node: `2088`
- outbound node: `2146`
- selected safe charger: `2152`

The deterministic optimizer must return CHARGE, PICK, and DROP with no
unassigned tasks and with safe arrival/final battery evidence.

## Validation

- Gate 2.1 focused regression: 40 passed
- Full project regression: 732 passed, 0 failed
- `compileall`: PASS

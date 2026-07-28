# P16.3.3 Battery-Safe Charger Hotfix

## Why this patch exists

The P16.3.2 real Swagger battery scenario correctly inserted a CHARGE task and
selected charger 2151 by configured cost. Verification then blocked the plan
because R2-03 was projected to arrive with 19.994%, below the 20% reserve.

P16.3.3 fixes the planning rule rather than weakening Verification.

## Battery policy

The battery model now separates three values.

- `MIN_ROBOT_BATTERY=20`: path-wide reserve. The robot must never cross it.
- `BATTERY_SAFETY_MARGIN_PERCENT=0.5`: prediction and telemetry margin.
- `CHARGE_TARGET_BATTERY=80`: operation-ready target after charging.

A charger is eligible only when:

```text
battery_at_charger >= MIN_ROBOT_BATTERY + BATTERY_SAFETY_MARGIN_PERCENT
```

When charging is required, the robot charges to 80% by default. If the
remaining mission itself needs more than 80% to preserve the reserve, the
higher calculated target is used, capped at 100%.

## Charger selection order

1. Read active CHARGER nodes.
2. Calculate route distance and battery at each charger.
3. Reject candidates below the safe-arrival threshold.
4. Among safe candidates with configured cost, choose the lowest cost.
5. If no safe candidate has cost data, choose the closest safe candidate and
   report `SAFE_DISTANCE_FALLBACK_NO_COST_DATA`.
6. If no charger is safely reachable, leave the affected task unassigned so
   Verification requests `LOCAL_REPLAN`.

Candidate evidence includes:

- `battery_at_charger`
- `minimum_arrival_battery`
- `safe_reachable`
- `rejection_reason`
- `selected`

## Verification behavior

Battery and charger failures remain blocking, but they are treated as locally
recoverable when replanning is allowed. Examples include:

- missing required charge
- final battery below reserve
- charger arrival below safe threshold
- invalid charger or charge duration
- wrong charger cost selection
- charge target below the operation-ready target

The expected decision is `REPLAN_LOCAL`, not an immediate unrecoverable FAIL.

## Response compression

P16.3.2 compressed repeated WAIT rows. P16.3.3 also compresses consecutive
CHARGE waypoints and timeline events while keeping the internal time-expanded
route unchanged for reservation and collision validation.

## Acceptance command

```powershell
python -m scripts.run_p16_3_3_final_checks
python -m pytest -q
```

Expected response schema: `p16.3.3`.

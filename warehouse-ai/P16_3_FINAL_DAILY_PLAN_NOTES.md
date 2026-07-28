# P16.3 Final Daily Plan Integration

P16.3 is the final major functional integration release.

## Combined command behavior

- Parse explicit A/B outbound and C inbound operations as `DAILY_PLAN`.
- A conditional phrase such as `A 재고가 부족하면 A 작업만 제외` is treated as a real inventory handling policy, not a hypothetical inventory mutation.
- Inventory shortage blocks only the affected operation and its dependents.
- Independent B outbound and C inbound operations continue.
- Outbound and inbound destinations remain operation-specific in one command.
- `MINIMUM_REQUIRED_CHARGE` is recognized from `최소 운용 배터리 ... 유지`.
- Charging is inserted only when route energy would otherwise violate the minimum battery threshold.
- `SIMULATE_ONLY` never dispatches commands to the robot gateway.

## Acceptance scenario

The packaged in-memory acceptance fixture uses:

- A request 30 BOX, available 10 BOX -> blocked.
- B request 20 BOX, available 20 BOX -> PICK/DROP generated.
- C inbound 50 BOX -> PICK/DROP generated.
- B outbound destination 30 and C storage destination 20 remain separate.
- High battery -> no unnecessary CHARGE task.
- Final user report outcome -> `PARTIAL_SUCCESS_WITH_EMERGENCY`.

For the connected demo database, the Swagger example requests A 50 BOX because the current demo A stock is 40 BOX.

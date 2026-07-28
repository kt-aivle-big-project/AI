# P16.5.15.3 — Terminal command state and reservation cleanup hotfix

## Live findings

The P16.5.15.2 Swagger gate passed, but two non-blocking consistency defects remained:

1. a robot ACK could report `occurred_at` earlier than the durable command `sent_at`;
2. a retry-exhausted dispatch was terminal at the dispatch level while every command
   still appeared as `PENDING`, and the rolled-back plan reservation required manual
   release.

P16.5.15.3 closes those gaps without changing the PostgreSQL schema.

## ACK time and lifecycle guard

ACKs are accepted only while the dispatch is actively awaiting command completion:

```text
AWAITING_ACK / PARTIAL_ACK
or
cancel-unconfirmed recovery window
```

A late ACK for `RETRY_EXHAUSTED`, `ROLLED_BACK`, `COMPLETED`, or another terminal/non-
delivered dispatch is rejected with:

```text
ACK_DISPATCH_NOT_ACTIVE:<status>
```

The server also compares the robot event time with the durable send time. A 5-second
clock-skew allowance is permitted; an older ACK is rejected with:

```text
ACK_BEFORE_COMMAND_SENT
```

## Retry-exhausted terminalization

When the final logical gateway attempt fails, unfinished command states now become:

```text
status = DISPATCH_FAILED
error_code = DISPATCH_RETRY_EXHAUSTED
```

The dispatch remains:

```text
status = RETRY_EXHAUSTED
attempt_count = max_attempts
result_summary.retryable = false
```

The rollback result is durably persisted in `result_summary.rollback` instead of being
visible only in the HTTP 422 exception text.

## Rolled-back plan reservation cleanup

A successful pre-physical logical rollback now releases the rolled-back plan's
`ACTIVE_PLAN / RESERVED` inventory reservations in the same service workflow.
The rollback evidence includes:

```text
rollback.inventory_reservation_release.status
rollback.inventory_reservation_release.released_count
```

Physical-progress and unconfirmed-gateway-cancel paths still do not release the
reservation automatically because the real load location may require manual recovery.

## Data and environment

- No new PostgreSQL migration
- Existing `migrations/013_p16_5_15_execution_delivery.sql` remains valid
- No PostgreSQL reset
- No Redis reset
- No Neo4j change
- No Mock Robot Gateway change

## Verification

```powershell
python -m scripts.run_p16_5_15_3_checks
python -m scripts.run_p16_5_15_3_checks --full
```

Expected results:

```text
P16.5.15.3 focused result: 100 passed / 0 failed
P16.5.15.3 full result: 790 passed / 0 failed
```

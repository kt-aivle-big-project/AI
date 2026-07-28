# P16.5.15.2 — Gateway cancel confirmation and rollback safety hotfix

## Problem reproduced in live Swagger

The application accepted an operator cancel, marked all remaining commands canceled,
and restored the previous Redis active plan even when the Robot Gateway cancel request
returned HTTP 404. That could split server and robot authority:

```text
robot gateway: original command batch may still be active
application: dispatch reported ROLLED_BACK and previous plan restored
```

A logical rollback must never imply physical cancellation without explicit gateway
confirmation.

## Safety policy

### Confirmed cancel before physical progress

```text
gateway accepted=true and status=CANCELED
+ no ACKed physical command
-> remaining commands CANCELED
-> previous active plan restored
-> dispatch ROLLED_BACK
```

### Unconfirmed cancel

The following are not cancellation confirmation:

- HTTP 404/409/5xx
- timeout or connection failure
- gateway without cancel support
- `accepted=false`
- accepted response with a non-canceled status

```text
-> remaining commands CANCEL_PENDING
-> active plan is not rolled back
-> status CANCELED_PARTIAL_EXECUTION
-> manual_recovery_required=true
-> retryable=true
-> reason_code=GATEWAY_CANCEL_UNCONFIRMED
```

Repeating the same cancel request after the gateway becomes available retries the
physical cancellation. Only a confirmed cancellation can then restore the previous
active plan.

### Physical progress already ACKed

An ACKed MOVE, PICKUP, DROPOFF, or CHARGE is treated as physical progress.

```text
gateway cancel confirmed
+ physical progress exists
-> no logical rollback
-> CANCELED_PARTIAL_EXECUTION
-> manual recovery required
```

### ACKs during unconfirmed cancellation

The robot may still emit ACKs while cancellation remains unconfirmed. Those ACKs are
accepted in sequence, but the dispatch cannot regress to `PARTIAL_ACK` or `COMPLETED`.
The recovery-required state is retained and the physical-progress evidence is updated.

### Command failure path

Automatic cancellation triggered by a FAILED command uses the same confirmation gate.
A gateway cancellation failure cannot trigger a logical rollback.

## Gateway dispatch identity correction

The durable application `dispatch_id` and the Robot Gateway `dispatch_id` are different
idempotency namespaces. Cancellation now uses the gateway identity returned by dispatch.
The gateway identity is also deterministically precomputed and stored before the network
send so a response timeout cannot erase the identifier needed for later cancellation.

```text
service dispatch_id -> PostgreSQL audit/API identity
gateway dispatch_id -> Robot Gateway cancel identity
```

## Data and environment

- No new PostgreSQL migration
- No PostgreSQL reset
- No Redis reset
- No Neo4j change
- Existing `migrations/013_p16_5_15_execution_delivery.sql` remains valid
- Mock Robot Gateway already exposes `POST /dispatches/{dispatch_id}/cancel`

## Verification

```powershell
python -m scripts.run_p16_5_15_2_checks
python -m scripts.run_p16_5_15_2_checks --full
```

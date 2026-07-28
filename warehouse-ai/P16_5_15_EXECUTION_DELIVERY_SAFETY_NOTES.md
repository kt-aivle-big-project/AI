# P16.5.15 — Approved Plan and Reliable Robot Command Delivery

## Goal

P16.5.15 closes the gap between a verified warehouse plan and reliable robot execution.
A robot gateway request is no longer treated as a one-shot fire-and-forget operation.
The exact verified plan version, dispatch identity, command order, acknowledgements,
retries, cancellation, and rollback evidence are persisted.

## Execution contract

```text
Verification PASS/PASS_WITH_WARNING
-> durable execution_plan_approval
-> Redis active plan version and approved plan fingerprint must match
-> durable dispatch and command states are created before network send
-> gateway send uses a deterministic idempotency key
-> robot ACKs must follow strict sequence per robot
-> all commands ACKED -> COMPLETED
```

## Approval policy

Only `PASS` and `PASS_WITH_WARNING` plans may receive `APPROVED` status.
The approval fingerprint covers immutable operational fields:

- plan version, command, warehouse and scope
- required tasks and cuOpt assignments
- collision-free routes
- inventory operations
- charger nodes
- execution dependencies and schedule constraints
- ready/waiting/blocked task sets

Volatile values such as `activated_at` are not part of the fingerprint.
After activation, the Redis active plan is fingerprinted again. A matching plan version
with modified tasks or routes is rejected as `ACTIVE_PLAN_PAYLOAD_NOT_APPROVED`.

## Dispatch idempotency

`dispatch_id` and gateway `idempotency_key` are deterministic from:

```text
warehouse_id + plan_version + canonical command batches
```

The same payload returns the existing dispatch without sending commands twice.
The same idempotency identity with a different fingerprint is rejected.

## Command sequence and ACK

Each robot batch must satisfy:

```text
sequence = 1, 2, 3, ... without gaps
unique command_id
command.plan_version = approved plan_version
command.robot_id = batch.robot_id
```

ACK endpoint:

```text
POST /v1/execution/dispatches/{dispatch_id}/acks
```

ACKs are accepted only for the next unfinished command of that robot.
A repeated identical ACK is idempotent. A changed ACK body for an already terminal
command is rejected.

## Timeout and retry

The durable delivery layer counts logical attempts. The transport layer performs one
network send per logical attempt. A timeout produces:

```text
status = DISPATCH_TIMEOUT
retryable = true
```

Retrying uses the same dispatch and idempotency identity:

```text
POST /v1/execution/dispatches/{dispatch_id}/retry
```

When `max_attempts` is exhausted, the dispatch becomes `RETRY_EXHAUSTED` and a safe
logical rollback is attempted.

## Partial failure, cancel and rollback

When a command fails, all unfinished commands are canceled and the gateway receives a
cancel request.

```text
No ACKed physical progress
-> Redis active plan may be restored to previous_active_plan_version
-> status = ROLLED_BACK

MOVE/PICKUP/DROPOFF/CHARGE already ACKed
-> no unsafe automatic reverse command is fabricated
-> status = PARTIAL_FAILURE or CANCELED_PARTIAL_EXECUTION
-> manual_recovery_required = true
```

This policy avoids pretending that a database rollback can physically undo an already
executed robot movement or load transfer.

## API

- `POST /v1/execution/plans/{plan_version}/approve`
- `GET /v1/execution/plans/{plan_version}/approval`
- `POST /v1/execution/plans/{plan_version}/dispatch`
- `GET /v1/execution/dispatches/{dispatch_id}`
- `POST /v1/execution/dispatches/{dispatch_id}/acks`
- `POST /v1/execution/dispatches/{dispatch_id}/retry`
- `POST /v1/execution/dispatches/{dispatch_id}/cancel`

Normal `EXECUTE` planning also uses this lifecycle automatically after final verification.

## Database migration

Apply `migrations/013_p16_5_15_execution_delivery.sql` after the existing SQL migrations.
It adds:

- `execution_plan_approval`
- `robot_execution_dispatch`

The dispatch row stores command batches and per-command states as JSONB so retries and
ACK processing survive API restarts.

## Mock Robot Gateway

Mock gateway version 1.1.0 supports:

- deterministic dispatch identity fields
- duplicate dispatch replay without recording or auto-executing twice
- idempotency payload conflict rejection
- `POST /dispatches/{dispatch_id}/cancel`

## Release checks

```powershell
python -m scripts.run_p16_5_15_checks
python -m scripts.run_p16_5_15_checks --full
```

Expected results:

```text
P16.5.15 focused result: 79 passed / 0 failed
P16.5.15 full result: 772 passed / 0 failed
```

# P16.5.13 Gate 3 — Event Safety and Recovery

API version: `2.5.13.9`

## Scope

Gate 3 closes the event-ingestion safety boundary without adding robot handover,
command ACK/retry, or physical dispatch rollback. Those remain P16.5.14 and
P16.5.15 work.

## Contracts

### Immutable event identity

- `event_id` is idempotency identity.
- The operational body is fingerprinted without server runtime fields.
- A retry may receive a newly defaulted `occurred_at`; event ordering handles time.
- The same `event_id` with different robot, task, event type, position, battery,
  inventory delta, execution context, simulation, or client payload is rejected as
  `EVENT_ID_PAYLOAD_CONFLICT`.
- Legacy rows without `event_payload` remain replayable but are marked as
  `LEGACY_EVENT_PAYLOAD_UNAVAILABLE` in evidence.

### Deterministic event ordering

External API events use a Redis watermark per robot and execution context.
Simulation timeline replay does not populate this external-event watermark.

Ordering is decided by:

1. `occurred_at`
2. event precedence when timestamps are equal
3. `event_id` lexical tie-break when both are equal

Older events return `STALE_EVENT_IGNORED` and do not mutate state or trigger a
replan.

### PostgreSQL before Redis for durable lifecycle events

For `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, and `INBOUND_AVAILABLE` in
REAL execution:

1. validate event order
2. commit PostgreSQL transaction
3. update Redis robot/task state and reservation state

A PostgreSQL failure therefore leaves Redis unchanged. An idempotent SQL replay
may repair a missing Redis update.

### Simulation checkpoint rollback

A simulation event captures the pre-event Redis snapshot. If the PostgreSQL
simulation checkpoint write fails after Redis mutation, inventory, robots, works,
active plan, and the external-event watermark are restored. The response includes
`simulation_state_rollback` and is retryable.

### Failed replan plan retention

Before automatic simulation replanning, the last verified simulation state is
captured. If planning raises or verification fails after replacing the simulation
plan, that snapshot is restored to Redis and persisted back to PostgreSQL.
The response includes `plan_recovery`.

### Recoverable duplicate replay

A duplicate event normally returns the stored result. A stored lifecycle event
with `recovery_required=true` and `retryable=true` is resumed through the
idempotent commit path so missing Redis state can be reconciled without applying
the SQL transaction twice.

## Out of scope

- failed robot carried-load handover
- replacement robot selection
- command sequence ACK and timeout retry
- partial dispatch cancellation and compensation
- physical gateway rollback

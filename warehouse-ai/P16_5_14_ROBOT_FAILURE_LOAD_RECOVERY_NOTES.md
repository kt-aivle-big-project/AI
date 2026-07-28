# P16.5.14 — Robot Failure and Carried-Load Recovery

API version: `2.5.14.0`

## Scope

P16.5.14 adds server-authoritative recovery for `ROBOT_FAILED` events. It
distinguishes failure before pickup from failure while carrying inventory and
prevents the original storage pickup from being replayed after a confirmed
pickup.

## Recovery contracts

### Failure before pickup

- The failed robot is excluded from the candidate set.
- The original unfinished PICK/DROP chain remains changeable.
- The optimizer assigns the chain to an available replacement robot.
- Unrelated robots and protected tasks remain fixed.

### Failure while carrying a secured load

Automatic handover is allowed only when all of the following are true:

- the server plan clock or event confirms that PICK has completed and DROP has
  not completed;
- the failed robot's stop node is known;
- `safe_stop_confirmed=true`;
- `load_secured=true`;
- a replacement robot has sufficient residual load capacity.

The original PICK/DROP pair is replaced by a synthetic handover chain:

```text
replacement robot -> failed robot stop node
-> HANDOVER PICK (represented as PICK with source_type=ROBOT_HANDOVER)
-> original outbound destination
-> DROP
```

Both recovery tasks must be assigned to the same non-failed replacement robot.
The event service rejects a planner result that drops either recovery task,
assigns one back to the failed robot, splits the pair across robots, or reverses
the PICK/DROP order.

### Unsafe or unknown carried load

Automatic planning is blocked when the load state is unknown, the stop node is
missing, or safe stop/load securing is not confirmed. The event response is:

```text
status = MANUAL_RECOVERY_REQUIRED
recovery_required = true
auto_replan_requested = false
```

## Server-generated recovery evidence

The response and scenario include:

- `robot_failure_recovery.strategy`
- `robot_failure_recovery.load_state`
- `robot_failure_recovery.carried_load`
- `robot_failure_recovery.failed_node_id`
- `robot_failure_recovery.replacement_candidate_ids`
- `robot_failure_recovery.replace_task_ids`
- `robot_failure_recovery.recovery_task_ids`
- `robot_failure_recovery_result.replacement_robot_ids`
- `robot_failure_recovery_result.handover_order_valid`

## Out of scope

- physical gripper-to-gripper transfer protocol;
- gateway command ACK, sequence fencing, timeout and retry;
- dispatch cancellation and compensation after partial gateway acceptance;
- human confirmation workflow UI;
- permanent maintenance work-order creation.

These remain P16.5.15 and later work.

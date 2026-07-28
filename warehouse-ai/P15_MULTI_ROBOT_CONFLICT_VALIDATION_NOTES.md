# P15 Multi-Robot Conflict Validation

## Goal

P15 validates and explains deterministic multi-robot collision avoidance while preserving the P12-P14 charging, cost selection, local replan, and dependency-validation behavior.

## Changes

### 1. Capacity-one directed edges

The internal time-expanded router now treats a directed aisle edge as a capacity-one resource. It blocks both:

- reverse-direction edge swaps, and
- overlapping use of the same directed edge.

### 2. Attributed conflict resolution evidence

Reservation ownership is tracked for vertices and edges. Conflict-avoidance evidence can now include:

- `conflict_type`
  - `VERTEX_OCCUPANCY`
  - `EDGE_SWAP`
  - `EDGE_CAPACITY`
  - `CHARGER_OCCUPANCY`
- `blocked_resource`
- `blocked_by_robot_id`
- `blocked_by_task_id`

The existing `reason: RESERVATION_CONFLICT_WAIT` remains for compatibility.

### 3. WAIT and REROUTE distinction

`collision_plan.metadata.resolution_events` records whether a conflict was resolved by:

- `WAIT`, or
- `REROUTE`.

`reservation_evidence.reroute_count` is now populated, and the evidence report displays the reroute count and detailed blocker information.

### 4. Shared charger serialization

CHARGE dwell continues to reserve the charger vertex. When multiple robots target the same charger at overlapping times, the lower-priority robot waits outside the charger and the evidence identifies the occupying robot and CHARGE task.

### 5. Emergency priority validation

Lower numeric priority is routed first. A priority-1 emergency task reserves shared resources before a priority-50 normal task even when the normal task appears first in the input list.

## Added tests

`tests/test_p15_multi_robot_conflicts.py` verifies:

1. shared-node conflict -> attributed WAIT,
2. reverse-edge swap -> attributed REROUTE,
3. shared charger -> serialized CHARGE windows,
4. emergency task -> priority-first reservation,
5. long same-direction edge -> capacity-one enforcement.

## Local deterministic check

Run without LLM, PostgreSQL, Neo4j, or Redis:

```powershell
python -m scripts.run_p15_multi_robot_checks
```

Expected top-level result:

```json
{
  "all_passed": true
}
```

## Verification performed

- P15 focused tests: passed
- Full regression suite: `566 passed`
- Python compile check: passed
- ZIP integrity check: passed

The full regression suite in the build container used temporary external-dependency stubs for packages unavailable in that container. The stubs are outside the project and are not included in the ZIP.

# P16.5.14.1 Robot Failure Stale-Route Eviction Hotfix

API version: `2.5.14.1`

## Live failure reproduced

A carried-load `ROBOT_FAILED` event correctly generated a handover PICK/DROP pair and assigned both tasks to `R2-02`, but the previous `R2-03` route remained in the final collision plan.

Root cause:

```python
changed = set(cuopt_plan.changed_robot_ids) or set(affected_robot_ids)
```

When cuOpt changed replacement robots, the `or` expression discarded the event-affected failed robot. The old active route was then preserved even though the failed robot had been excluded from the optimization snapshot.

## Fix

```text
changed robots
= cuOpt changed robots
∪ event affected robots
∪ failed/excluded robots
```

Active routes are now fully evicted when the robot is:

```text
excluded_robot_ids
or status in FAILED / ROBOT_FAILED / OFFLINE / MAINTENANCE
```

The failed robot contributes no future waypoint, task ownership, vertex reservation, or edge reservation. Its physical stop remains represented by the server-authoritative runtime override and the synthetic handover task.

## Evidence

Collision-plan metadata now exposes:

```json
{
  "stale_route_eviction": {
    "version": "p16.5.14.1",
    "policy": "EVICT_EXCLUDED_OR_FAILED_ACTIVE_ROUTES",
    "changed_robot_ids": [],
    "evicted_robot_ids": [],
    "preserved_robot_ids": []
  }
}
```

Automatic event-replan responses also surface the same object as `stale_route_eviction`.

## Expected handover result

```text
R2-03: FAILED, no route
R2-02: handover PICK at failed node -> DROP at original destination
R2-01: unrelated D chain preserved/replanned without ownership change
```

## Release checks

```powershell
python -m scripts.run_p16_5_14_1_checks
python -m scripts.run_p16_5_14_1_checks --full
```

Validation: focused `78 passed / 0 failed`; full project `759 passed / 0 failed`; compileall PASS.

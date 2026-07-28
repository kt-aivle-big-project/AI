# P16.5.6 Idle Holding and Shared-Node Routing Hotfix

## Problem reproduced from the live Swagger result

P16.5.5 successfully rebalanced the daily workload across `R2-01`, `R2-02`,
and `R2-03`, but routing failed on the second simultaneous inbound operation:

```text
작업 ...:drop 목적지 경로 없음
ROUTE_FAILED
```

The first routed robot completed an inbound DROP at STORAGE node `2088` and
then reserved that node for the entire idle interval until its afternoon PICK.
Because routes were built robot by robot, the next robot could not reach `2088`
within the MAPF search horizon even though the business schedule was feasible.

A second latent issue was also exposed: `R2-01` and `R2-02` were both recorded
at OUTBOUND node `2146` before their first task and could emit the same initial
vertex/time waypoint.

## Fix

- Long inter-task idle gaps no longer occupy STORAGE/INBOUND/OUTBOUND/CHARGER
  service nodes.
- The robot relocates to a deterministic non-service holding node, waits there,
  and returns when the next task window opens.
- Holding candidates exclude:
  - command-specified congestion nodes,
  - service nodes,
  - graph articulation/cut vertices such as `2044`, the sole gateway to
    OUTBOUND `2146`.
- Multiple sparse routes starting from the same snapshot node are activated on
  successive free time steps.
- Routing metadata now includes:

```json
{
  "idle_relocation_count": 3,
  "idle_relocations": [
    {
      "resolution": "IDLE_RELOCATION",
      "reason": "RELEASE_SHARED_SERVICE_NODE_DURING_LONG_IDLE"
    }
  ]
}
```

## Version

- API version: `2.5.6`
- Response schema: `p16.5.6`

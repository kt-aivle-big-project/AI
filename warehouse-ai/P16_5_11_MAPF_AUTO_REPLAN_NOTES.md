# P16.5.11 MAPF Failure Automatic Replanning

## Goal

A MAPF or internal prioritized-routing failure must not always terminate the
planning command. P16.5.11 classifies the failure and applies a bounded,
deterministic recovery policy.

## Failure classification

| Category | Verification code | Action |
|---|---|---|
| Concrete reservation/resource conflict with affected robot or task | `MAPF_LOCAL_CONFLICT` | `LOCAL_REPLAN` |
| Conflict without a safe bounded target | `MAPF_GLOBAL_CONFLICT` | `GLOBAL_REPLAN` |
| Disconnected/invalid topology | `MAPF_TOPOLOGY_FAILURE` | `GLOBAL_REPLAN` |
| Missing backend URL or routing contract/configuration error | `MAPF_CONFIGURATION_FAILURE` | Fail without retry |
| External MAPF backend unavailable when fallback is disabled | `MAPF_BACKEND_UNAVAILABLE` | Fail without retry |
| Unclassified exception | `MAPF_UNCLASSIFIED_FAILURE` | Fail without retry |

The classifier records affected robot, task, and node IDs when they are present
in deterministic routing evidence. A local scope is never fabricated when no
concrete target can be identified.

## Recovery sequence

### First retry

```text
MAPF_LOCAL_CONFLICT
→ REPLAN_LOCAL
→ freeze unaffected work
→ route affected robots first
→ rerun optimization, shared-resource scheduling, routing, simulation, verification
```

Policy marker:

```text
strategy = AFFECTED_ROBOTS_FIRST
```

### Repeated identical local failure

The existing repeated-signature guard remains active, but one controlled
escalation is allowed for retryable MAPF failures:

```text
same local signature on second attempt
→ GLOBAL_REPLAN once
→ rotate all robot routing priorities
→ apply bounded activation stagger
```

Policy marker:

```text
strategy = ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER
escalated_from_local = true
```

A subsequent failure is stopped by the normal repeated-signature or maximum
attempt guard. The maximum remains capped at three and the default supervisor
limit remains two.

## State safety

Retryable route failures are stored in `route_failure` and verification
evidence instead of the additive pipeline `errors` list. This is required
because LangGraph accumulates `errors`; otherwise a successful retry would
still be rejected due to the first attempt's stale `ROUTE_FAILED` message.

The following fields are cleared before rebuilding a candidate:

- `route_failure`
- collision plan
- simulation and plan validation
- robot command batches
- adapter validation
- route/reservation evidence
- route energy reconciliation

The applied routing perturbation is retained in `mapf_replan_policy` and copied
into the next optimization problem.

## Routing behavior

The internal prioritized time-expanded planner reads `mapf_replan_policy`.
Normal plans keep the original deterministic robot order. Replanned candidates
may change only robot processing order and a bounded initial activation delay;
per-robot task dependency order remains unchanged.

Observability fields:

```text
collision_plan.metadata.mapf_replan_policy
collision_plan.metadata.robot_processing_order
result.mapf_replan
replan_history
```

## Expected successful response after automatic recovery

```text
response_schema_version = p16.5.11
status = SIMULATION_SUCCESS
verification.decision = PASS or PASS_WITH_WARNING
plan_mode = LOCAL_REPLAN or GLOBAL_REPLAN
result.valid = true
result.mapf_replan.enabled = true
result.mapf_replan.attempt >= 1
result.resources.valid = true
result.objective.status = PASS
result.collision_resolution.final_conflict_count = 0
errors = []
```

## Regression gate

```powershell
python -m scripts.run_p16_5_11_final_checks
```

Focused gate result in the artifact environment:

```text
84 passed
compileall PASS
```

The focused gate includes MAPF classification, local retry, repeated-signature
global escalation, routing-order policy application, compact response fields,
and P16.5.7 through P16.5.10.3 charging/resource/routing regressions.

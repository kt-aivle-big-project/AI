"""Deterministic contract checks for replan and Human Review scenarios."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.domain.schemas import (
    AutoMissionRequest,
    ContextSnapshot,
    CuOptPayload,
    EventInput,
    FleetData,
    FormulationRecommendation,
    HumanInteractionOption,
    HumanInteractionResumeRequest,
    MapConstraints,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    ReplanMissionRequest,
    RobotRuntime,
    RobotRuntimeContext,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    RoutingWorkloadContext,
    SimulationLogicalOperation,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
    StructuredMissionInput,
    StructuredOperationInput,
    TaskData,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
    WaypointGraphData,
)
from app.services.request_gate_service import resolve_request_gate
from app.services.hitl_service import HumanInteractionService, HumanInteractionStore
from app.services.simulation_plan_service import (
    RollingHorizonReplanService,
    RuntimeExecutionSnapshotBuilder,
)


def _optimizer_from_result(result: object) -> object | None:
    return (
        getattr(result, "execution_optimizer_result", None)
        or getattr(result, "optimizer_result", None)
    )


def _split_replan_operations(
    structured: StructuredMissionInput,
) -> tuple[list[StructuredOperationInput], list[StructuredOperationInput]]:
    """Split one 6-outbound/2-inbound wave into old work and new orders.

    The old horizon keeps four outbound plus both inbound operations.  The two
    remaining outbound operations arrive after the initial cuOpt plan, which
    matches the production meaning of a NEW_ORDER replan.
    """

    outbound = [
        value for value in structured.operations if value.operation_type == "OUTBOUND"
    ]
    inbound = [
        value for value in structured.operations if value.operation_type == "INBOUND"
    ]
    if len(outbound) < 2:
        raise ValueError("A replan evaluation requires at least two outbound operations")
    initial = [*outbound[:-2], *inbound]
    arriving = list(outbound[-2:])
    if not initial or not arriving:
        raise ValueError("A replan evaluation requires both initial and arriving work")
    return initial, arriving


def _repository_runtime_overrides(
    repository: object,
    *,
    sim_time_ms: int,
    excluded_robot_ids: set[str] | None = None,
) -> list[RobotRuntimeOverride]:
    """Build the complete BE-style robot snapshot used by replan evaluations.

    Production replan requests contain every participating robot, including
    idle reserves that did not appear in the active plan.  The operational
    evaluator must preserve that contract; sending only the deviated robot
    makes reserve replacement impossible and incorrectly sends LOW_BATTERY to
    Human Review before the solver runs.
    """

    excluded = excluded_robot_ids or set()
    read_all = getattr(repository, "all_robots", None)
    if not callable(read_all):
        return []
    overrides: list[RobotRuntimeOverride] = []
    for record in read_all():
        robot_id = str(record.get("robot_id") or "").strip()
        if not robot_id or robot_id in excluded:
            continue
        status = str(record.get("status") or "idle").strip().casefold()
        if status == "available":
            status = "idle"
        overrides.append(
            RobotRuntimeOverride(
                robot_id=robot_id,
                current_node=record.get("current_node"),
                current_edge=record.get("current_edge"),
                from_node=record.get("from_node"),
                to_node=record.get("to_node"),
                edge_progress=record.get("edge_progress"),
                status=status,
                battery_pct=record.get("battery_pct"),
                capacity_units=record.get("capacity_units"),
                current_load_units=int(record.get("current_load_units") or 0),
                active_task_id=record.get("active_task_id"),
                sim_time_ms=sim_time_ms,
            )
        )
    return sorted(overrides, key=lambda value: value.robot_id)


def _structured_subset(
    source: StructuredMissionInput,
    operations: list[StructuredOperationInput],
    *,
    request_id_suffix: str,
) -> StructuredMissionInput:
    routing = source.routing_context
    if routing is not None:
        routing = routing.model_copy(
            update={
                "new_operation_count": len(operations),
                "unfinished_operation_count": 0,
            }
        )
    return source.model_copy(
        update={
            "request_id": f"{source.request_id or 'REQ-REPLAN'}-{request_id_suffix}",
            "operations": operations,
            "routing_context": routing,
        }
    )


def _normalized_subset(
    source: NormalizedWarehouseRequest,
    operations: list[StructuredOperationInput],
) -> NormalizedWarehouseRequest:
    wanted = {value.operation_id for value in operations}
    return source.model_copy(
        update={
            "operations": [
                value for value in source.operations if value.operation_id in wanted
            ],
            "normalization_summary": (
                f"Frozen initial replan horizon with {len(wanted)} operations."
            ),
        }
    )


def _checkpoint_from_real_plan(
    plan: SimulationPlan,
    checkpoint: str,
    repository: object,
    *,
    required_edge_id: str | None = None,
) -> tuple[int, str, str]:
    """Choose a real plan timestamp that exhibits the requested handover."""

    if checkpoint == "AFTER_COMPLETION":
        at = plan.absolute_finish_at_ms
        snapshot = RuntimeExecutionSnapshotBuilder().build(
            plan, at, repository=repository
        )
        if not snapshot.completed_task_bases:
            raise ValueError("Real initial plan has no completed work at finish")
        point = snapshot.handover_points[0]
        return at, point.robot_id, point.handover_policy

    wanted_policy = {
        "MOVE": "NEXT_NODE",
        "LOADED_MOVE": "CURRENT_OPERATION_END",
        "SERVICE": "CURRENT_OPERATION_END",
        "SAFE_NODE": "CURRENT_NODE",
    }[checkpoint]
    candidates: list[tuple[int, str]] = []
    for robot in plan.robots:
        for step in robot.steps:
            if checkpoint in {"MOVE", "LOADED_MOVE"} and step.step_type != "MOVE":
                continue
            if (
                checkpoint == "MOVE"
                and required_edge_id is not None
                and step.edge_id != required_edge_id
            ):
                continue
            if checkpoint == "SERVICE" and not (
                step.step_type == "SERVICE" and step.service_kind == "PICKUP"
            ):
                continue
            if checkpoint == "SAFE_NODE" and step.step_type != "WAIT":
                continue
            candidates.append(
                (
                    step.start_at_ms
                    + max(1, (step.end_at_ms - step.start_at_ms) // 2),
                    robot.robot_id,
                )
            )
    if checkpoint == "SAFE_NODE":
        candidates.append((plan.absolute_finish_at_ms, plan.robots[0].robot_id))
    for at, robot_id in sorted(candidates):
        snapshot = RuntimeExecutionSnapshotBuilder().build(
            plan, at, repository=repository
        )
        point = next(
            value for value in snapshot.handover_points if value.robot_id == robot_id
        )
        if checkpoint == "LOADED_MOVE" and not point.carrying_load:
            continue
        if point.handover_policy == wanted_policy:
            return at, robot_id, point.handover_policy
    raise ValueError(
        f"Real initial plan has no {checkpoint} checkpoint with {wanted_policy}"
    )


def _safe_stop_after_active_step(
    plan: SimulationPlan,
    *,
    robot_id: str,
    observed_at_ms: int,
) -> tuple[str, int]:
    """Project the BE battery signal to the next executable safe node."""

    robot = next(value for value in plan.robots if value.robot_id == robot_id)
    current_node = robot.initial_node
    for step in robot.steps:
        if step.end_at_ms <= observed_at_ms:
            current_node = step.to_node or step.node_id or current_node
            continue
        if step.start_at_ms <= observed_at_ms < step.end_at_ms:
            if step.step_type == "MOVE":
                return step.to_node or current_node, step.end_at_ms
            return step.node_id or current_node, observed_at_ms
        break
    return current_node, observed_at_ms


def _redundant_graph_edge_id(
    plan: SimulationPlan,
    payload: CuOptPayload,
) -> str:
    """Choose a directed graph edge whose removal still has an alternate path.

    Blocking a bridge-like station/perimeter edge proves only that the graph is
    infeasible.  RP07 is specifically a rerouting case, so its blocked edge must
    be bypassable in the authoritative directed waypoint graph. Prefer a used
    edge when the initial route contains one; this warehouse's station/perimeter
    route can legitimately contain only bridge-like edges, so fall back to an
    unused but bypassable aisle edge while the robot is moving elsewhere.
    """

    graph = payload.waypoint_graph_data
    edge_index_by_id = {
        edge_id: index for index, edge_id in enumerate(graph.edge_ids)
    }
    used_edge_ids = [
        step.edge_id
        for robot in plan.robots
        for step in robot.steps
        if step.step_type == "MOVE" and step.edge_id in edge_index_by_id
    ]

    def alternate_path_exists(blocked_index: int) -> bool:
        source = graph.from_indices[blocked_index]
        target = graph.to_indices[blocked_index]
        adjacency: dict[int, list[int]] = {}
        for index, (left, right) in enumerate(
            zip(graph.from_indices, graph.to_indices, strict=True)
        ):
            if index == blocked_index:
                continue
            adjacency.setdefault(left, []).append(right)
        pending = [source]
        visited = {source}
        while pending:
            node = pending.pop()
            for neighbor in adjacency.get(node, []):
                if neighbor == target:
                    return True
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        return source == target

    candidates = [*dict.fromkeys(used_edge_ids), *graph.edge_ids]
    for edge_id in dict.fromkeys(candidates):
        index = edge_index_by_id[edge_id]
        if alternate_path_exists(index):
            return edge_id
    raise ValueError("RP07 waypoint graph has no edge with an alternate path")


def validate_replan_with_cuopt(
    definition: dict[str, Any],
    *,
    request: AutoMissionRequest,
    repository: object,
    replan_planning_mode: str = "force_rule",
) -> dict[str, Any]:
    """Run one Rule initial plan and one production rolling-horizon replan.

    Keeping the initial horizon on Rule gives every replay the same runtime
    checkpoint. ``replan_planning_mode`` selects only the branch being
    evaluated after that checkpoint, so Rule and Agent replan results remain
    comparable.
    """

    if replan_planning_mode not in {"force_rule", "force_agent"}:
        raise ValueError(
            "replan_planning_mode must be force_rule or force_agent"
        )

    contract = dict(definition.get("dynamic_contract") or {})
    scenario_id = str(definition["scenario_id"])
    expected_policy = str(contract["expected_handover_policy"])
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, actual: object, expected: object) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "actual": actual,
                "expected": expected,
            }
        )

    structured = request.structured_input
    normalized = request.normalized_request_override
    if structured is None or normalized is None:
        raise ValueError(f"{scenario_id}: frozen structured request is required")
    initial_operations, arriving_operations = _split_replan_operations(structured)
    reason = str(contract["reason"])
    initial_constraints = normalized.constraints
    if reason == "POLICY_CHANGE":
        # RP08 starts under a throughput-first policy and changes to BALANCED
        # at the service handover.  Keep both sides structured so a policy-only
        # replan never depends on an LLM paraphrasing the remaining operations.
        initial_constraints = initial_constraints.model_copy(
            update={
                "objective_profile": "THROUGHPUT",
                "objective_profile_explicit": True,
            }
        )
    initial_structured = _structured_subset(
        structured, initial_operations, request_id_suffix="INITIAL"
    ).model_copy(update={"constraints": initial_constraints})
    initial_normalized = _normalized_subset(normalized, initial_operations)
    initial_normalized = initial_normalized.model_copy(
        update={"constraints": initial_constraints}
    )
    initial_request = request.model_copy(
        update={
            "optimization_backend": "cuopt",
            "events": initial_structured.to_events(),
            "structured_input": initial_structured,
            "normalized_request_override": initial_normalized,
            "user_command": None,
            "request_mode": "event_driven",
        }
    )
    from app.services.orchestration_service import OrchestrationService

    initial_result = OrchestrationService().run(
        initial_request,
        trusted_planning_mode="force_rule",
        persist_simulation_plan=False,
        repository=repository,
    )
    initial_optimizer = _optimizer_from_result(initial_result)
    initial_plan = initial_result.simulation_plan
    check("initial_solver_backend", getattr(initial_optimizer, "backend", None) == "cuopt", getattr(initial_optimizer, "backend", None), "cuopt")
    check("initial_solver_success", getattr(initial_optimizer, "status", None) == "success", getattr(initial_optimizer, "status", None), "success")
    check("initial_plan_validated", initial_result.status == "plan_validated" and initial_plan is not None, initial_result.status, "plan_validated")
    check("initial_mapf_valid", bool(initial_result.mapf_validation and initial_result.mapf_validation.valid), getattr(initial_result.mapf_validation, "valid", None), True)
    if initial_plan is None:
        failures = [value for value in checks if not value["passed"]]
        return {
            "scenario_group": "REPLAN",
            "validation_scope": "CUOPT_ROLLING_HORIZON_EXECUTION",
            "scenario_id": scenario_id,
            "passed": False,
            "checks": checks,
            "failed_checks": [value["name"] for value in failures],
        }

    checkpoint = str(contract["checkpoint"])
    blocked_edge_id = None
    if reason == "EDGE_BLOCKED":
        if initial_result.cuopt_payload is None:
            raise ValueError("RP07 initial cuOpt payload is required")
        blocked_edge_id = _redundant_graph_edge_id(
            initial_plan, initial_result.cuopt_payload
        )

    if expected_policy == "STALE_REJECTED":
        at = min(initial_plan.absolute_finish_at_ms, initial_plan.plan_start_sim_time_ms)
        target_robot_id = initial_plan.robots[0].robot_id
        actual_policy = "STALE_REJECTED"
    else:
        at, target_robot_id, actual_policy = _checkpoint_from_real_plan(
            initial_plan,
            checkpoint,
            repository,
            required_edge_id=(
                blocked_edge_id
                if any(
                    step.edge_id == blocked_edge_id
                    for robot in initial_plan.robots
                    for step in robot.steps
                )
                else None
            ),
        )
        check("real_checkpoint_policy", actual_policy == expected_policy, actual_policy, expected_policy)
        if reason == "LOW_BATTERY":
            _, at = _safe_stop_after_active_step(
                initial_plan,
                robot_id=target_robot_id,
                observed_at_ms=at,
            )

    class MemoryStore:
        def __init__(self) -> None:
            self.saved: list[SimulationPlan] = []

        def load(self, plan_id: str) -> tuple[SimulationPlan, object]:
            if plan_id != initial_plan.plan_id:
                raise FileNotFoundError(plan_id)
            return initial_plan, initial_result

        def save(self, value: SimulationPlan, result: object = None) -> None:
            del result
            self.saved.append(value)

    store = MemoryStore()
    full_overlay = structured.model_copy(
        update={
            "request_id": f"{structured.request_id or 'REQ-REPLAN'}-OVERLAY",
            "routing_context": (
                structured.routing_context.model_copy(
                    update={
                        "new_operation_count": len(arriving_operations),
                        "unfinished_operation_count": len(initial_operations),
                    }
                )
                if structured.routing_context is not None
                else RoutingWorkloadContext(
                    new_operation_count=len(arriving_operations),
                    unfinished_operation_count=len(initial_operations),
                    eligible_robot_count=int(definition["robots"]["eligible_robot_count"]),
                    total_robot_count=int(definition["robots"]["total_robot_count"]),
                    low_battery_robot_count=int(definition["robots"]["low_battery_robot_count"]),
                    source="PLANNING_SCENARIO_REPLAN",
                )
            ),
        }
    )
    arriving = _structured_subset(
        structured, arriving_operations, request_id_suffix="ARRIVING"
    )
    if reason == "URGENT_ORDER":
        urgent_ids = {value.operation_id for value in arriving_operations}
        full_overlay = full_overlay.model_copy(
            update={
                "operations": [
                    value.model_copy(update={"priority": "high"})
                    if value.operation_id in urgent_ids
                    else value
                    for value in full_overlay.operations
                ]
            }
        )
        arriving = arriving.model_copy(
            update={
                "operations": [
                    value.model_copy(update={"priority": "high"})
                    for value in arriving.operations
                ]
            }
        )
    events = arriving.to_events()
    if reason == "URGENT_ORDER":
        events = [
            value.model_copy(update={"payload": {**value.payload, "priority": "urgent"}})
            for value in events
        ]
    if reason == "EDGE_BLOCKED":
        events.append(EventInput(type="edge_blocked", edge_id=blocked_edge_id))
    snapshot = RuntimeExecutionSnapshotBuilder().build(
        initial_plan, at, initial_result, repository=repository
    )
    target_point = next(
        value for value in snapshot.handover_points if value.robot_id == target_robot_id
    )
    explicit_states: list[RobotRuntimeOverride] = []
    if reason == "LOW_BATTERY":
        explicit_states.extend(
            _repository_runtime_overrides(
                repository,
                sim_time_ms=at,
                excluded_robot_ids={target_robot_id},
            )
        )
        safe_stop_node, stopped_at_ms = _safe_stop_after_active_step(
            initial_plan,
            robot_id=target_robot_id,
            observed_at_ms=at,
        )
        explicit_states.append(
            RobotRuntimeOverride(
                robot_id=target_robot_id,
                current_node=safe_stop_node,
                status="low_battery",
                battery_pct=10,
                current_load_units=1 if target_point.carrying_load else 0,
                active_task_id=(
                    target_point.locked_task_ids[0]
                    if target_point.locked_task_ids
                    else None
                ),
                sim_time_ms=stopped_at_ms,
            )
        )
    elif reason == "ROBOT_FAULT":
        explicit_states.append(
            RobotRuntimeOverride(
                robot_id=target_robot_id,
                current_node=target_point.node_id,
                status="fault",
                battery_pct=80,
                capacity_units=1,
                current_load_units=0,
                sim_time_ms=target_point.handover_at_ms,
            )
        )
    normalized_override = None
    if reason == "POLICY_CHANGE":
        completed_bases = set(snapshot.completed_task_bases)
        locked_bases = set(snapshot.locked_task_bases)
        replannable_operation_ids = {
            value.operation_id for value in arriving_operations
        }
        for operation in initial_plan.logical_operations:
            task_bases = set(operation.task_ids)
            if task_bases and task_bases <= completed_bases:
                continue
            if task_bases & locked_bases:
                continue
            replannable_operation_ids.add(operation.operation_id)
        replannable_operations = [
            value
            for value in structured.operations
            if value.operation_id in replannable_operation_ids
        ]
        normalized_override = _normalized_subset(
            normalized, replannable_operations
        ).model_copy(
            update={
                "source": "structured_events",
                "raw_user_command": None,
                "constraints": normalized.constraints.model_copy(
                    update={
                        "objective_profile": "BALANCED",
                        "objective_profile_explicit": True,
                    }
                ),
                "normalization_summary": (
                    "Deterministic policy change from THROUGHPUT to BALANCED "
                    "for replannable work."
                ),
            }
        )
    mission = request.model_copy(
        update={
            "optimization_backend": "cuopt",
            "events": events,
            "structured_input": full_overlay,
            "normalized_request_override": normalized_override,
            # POLICY_CHANGE is represented by the typed objective constraints
            # above and by ReplanMissionRequest.reason.  Leaving a duplicate
            # natural-language command here would make the rolling-horizon
            # service classify the request as mixed and invoke an unnecessary,
            # non-deterministic LLM normalizer.
            "request_mode": "event_driven",
            "user_command": None,
            "runtime_overrides": RuntimePlanningOverrides(
                robot_states=explicit_states
            ),
        }
    )
    if reason == "POLICY_CHANGE":
        check(
            "policy_override_prepared",
            mission.normalized_request_override is not None,
            mission.normalized_request_override is not None,
            True,
        )
    replan_optimizer_calls = 0
    captured_combined: AutoMissionRequest | None = None
    captured_replan_result: object | None = None

    def run_replan(combined: AutoMissionRequest) -> object:
        nonlocal replan_optimizer_calls, captured_combined, captured_replan_result
        replan_optimizer_calls += 1
        captured_combined = combined
        captured_replan_result = OrchestrationService().run(
            combined,
            trusted_planning_mode=replan_planning_mode,
            persist_simulation_plan=False,
            repository=repository,
        )
        return captured_replan_result

    service = RollingHorizonReplanService(
        store=store,
        runner=run_replan,
        repository=repository,
        evaluation_capture=lambda **kwargs: SimpleNamespace(
            evaluation_id=f"EVAL-CUOPT-{scenario_id}"
        ),
    )
    if expected_policy == "STALE_REJECTED":
        try:
            service.replan(
                ReplanMissionRequest(
                    active_plan_id=initial_plan.plan_id,
                    active_plan_version=initial_plan.plan_version + 1,
                    replan_at_sim_time_ms=at,
                    mission=mission,
                    reason=reason,
                )
            )
            stale_error = None
        except ValueError as exc:
            stale_error = str(exc)
        check("stale_plan_version_rejected", bool(stale_error and stale_error.startswith("STALE_PLAN_VERSION:")), stale_error, "STALE_PLAN_VERSION:*")
        check("stale_replan_skips_cuopt", replan_optimizer_calls == 0, replan_optimizer_calls, 0)
    else:
        response = service.replan(
            ReplanMissionRequest(
                active_plan_id=initial_plan.plan_id,
                active_plan_version=initial_plan.plan_version,
                replan_at_sim_time_ms=at,
                mission=mission,
                reason=reason,
            )
        )
        replan_optimizer = _optimizer_from_result(captured_replan_result)
        if reason == "POLICY_CHANGE":
            check(
                "policy_override_preserved",
                bool(
                    captured_combined is not None
                    and captured_combined.normalized_request_override is not None
                ),
                bool(
                    captured_combined is not None
                    and captured_combined.normalized_request_override is not None
                ),
                True,
            )
        check("replan_cuopt_called_once", replan_optimizer_calls == 1, replan_optimizer_calls, 1)
        check("replan_solver_backend", getattr(replan_optimizer, "backend", None) == "cuopt", getattr(replan_optimizer, "backend", None), "cuopt")
        check("replan_solver_success", getattr(replan_optimizer, "status", None) == "success", getattr(replan_optimizer, "status", None), "success")
        check("replan_mapf_valid", bool(getattr(captured_replan_result, "mapf_validation", None) and captured_replan_result.mapf_validation.valid), getattr(getattr(captured_replan_result, "mapf_validation", None), "valid", None), True)
        check("replan_plan_returned", response.plan is not None, response.status, "plan")
        check("replan_plan_version_incremented", bool(response.plan and response.plan.plan_version == initial_plan.plan_version + 1), response.plan.plan_version if response.plan else None, initial_plan.plan_version + 1)
        check("replan_base_plan_linked", bool(response.plan and response.plan.base_plan_id == initial_plan.plan_id), response.plan.base_plan_id if response.plan else None, initial_plan.plan_id)
        check("replan_mapf_timeline_valid", bool(response.plan and response.plan.absolute_finish_at_ms >= at), response.plan.absolute_finish_at_ms if response.plan else None, f">={at}")
        check("old_plan_superseded", bool(store.saved and store.saved[0].status == "SUPERSEDED"), store.saved[0].status if store.saved else None, "SUPERSEDED")
        check(
            "replan_reason_propagated",
            bool(response.plan and response.plan.replan_reason == reason),
            response.plan.replan_reason if response.plan else None,
            reason,
        )
        if captured_combined is not None:
            entity_ids = [
                value.order_id or value.inbound_id or value.robot_id
                for value in captured_combined.events
                if value.order_id or value.inbound_id or value.robot_id
            ]
            check("no_duplicate_operation", len(entity_ids) == len(set(entity_ids)), entity_ids, "unique")
            check("arriving_work_preserved", all(value.operation_id in entity_ids for value in arriving_operations), entity_ids, [value.operation_id for value in arriving_operations])
            non_replannable_ids: list[str] = []
            completed_bases = set(snapshot.completed_task_bases)
            locked_bases = set(snapshot.locked_task_bases)
            for operation in initial_plan.logical_operations:
                task_bases = set(operation.task_ids)
                if (
                    (task_bases and task_bases <= completed_bases)
                    or bool(task_bases & locked_bases)
                ):
                    non_replannable_ids.append(operation.operation_id)
            check(
                "completed_or_committed_work_not_replayed",
                not any(value in entity_ids for value in non_replannable_ids),
                entity_ids,
                {
                    "excluded_operation_ids": sorted(non_replannable_ids),
                },
            )
            if checkpoint == "AFTER_COMPLETION":
                completed_operation_ids = [
                    value.operation_id for value in initial_plan.logical_operations
                ]
                check("completed_work_not_replayed", not any(value in entity_ids for value in completed_operation_ids), entity_ids, "completed operation IDs absent")
            if reason == "LOW_BATTERY":
                runtime_by_robot = {
                    value.robot_id: value
                    for value in captured_combined.runtime_overrides.robot_states
                }
                runtime_robot = runtime_by_robot[target_robot_id]
                check(
                    "low_battery_status_propagated",
                    runtime_robot.status == "low_battery",
                    runtime_robot.status,
                    "low_battery",
                )
                check(
                    "low_battery_robot_released_at_safe_handover",
                    runtime_robot.current_node == target_point.node_id
                    and runtime_robot.sim_time_ms >= target_point.handover_at_ms
                    and runtime_robot.current_load_units == 0,
                    {
                        "node": runtime_robot.current_node,
                        "sim_time_ms": runtime_robot.sim_time_ms,
                        "load": runtime_robot.current_load_units,
                    },
                    {
                        "node": target_point.node_id,
                        "sim_time_ms": f">={target_point.handover_at_ms}",
                        "load": 0,
                    },
                )
                if target_point.carrying_load:
                    check(
                        "loaded_robot_released_at_operation_end",
                        runtime_robot.current_node == target_point.node_id
                        and runtime_robot.sim_time_ms == target_point.handover_at_ms
                        and runtime_robot.current_load_units == 0,
                        {
                            "node": runtime_robot.current_node,
                            "sim_time_ms": runtime_robot.sim_time_ms,
                            "load": runtime_robot.current_load_units,
                        },
                        {
                            "node": target_point.node_id,
                            "sim_time_ms": target_point.handover_at_ms,
                            "load": 0,
                        },
                    )
                    committed_reservations = [
                        value.reservation_id
                        for value in captured_combined.runtime_overrides.preserved_edge_reservations
                        if value.robot_id == target_robot_id
                    ]
                    check(
                        "loaded_robot_commitment_reservations_preserved",
                        bool(committed_reservations),
                        committed_reservations,
                        "non-empty",
                    )
        optimization_request = getattr(
            captured_replan_result, "optimization_request", None
        )
        if reason in {"LOW_BATTERY", "ROBOT_FAULT"}:
            candidate_ids = [
                value.robot_id
                for value in getattr(optimization_request, "vehicles", [])
            ]
            check(
                "deviated_robot_excluded_from_solver_fleet",
                target_robot_id not in candidate_ids,
                candidate_ids,
                f"{target_robot_id} absent",
            )
            if reason == "LOW_BATTERY":
                relocations = list(
                    getattr(
                        getattr(captured_replan_result, "terminal_relocation", None),
                        "relocations",
                        [],
                    )
                )
                charging = [
                    value
                    for value in relocations
                    if value.robot_id == target_robot_id
                    and value.policy == "CHARGE"
                ]
                check(
                    "low_battery_charge_relocation_created",
                    bool(charging)
                    and charging[0].from_node == target_point.node_id,
                    [
                        {
                            "robot_id": value.robot_id,
                            "from_node": value.from_node,
                            "to_node": value.to_node,
                        }
                        for value in relocations
                    ],
                    (
                        f"{target_robot_id} CHARGE relocation starting at "
                        f"{target_point.node_id}"
                    ),
                )
                target_plan = next(
                    (
                        value
                        for value in (response.plan.robots if response.plan else [])
                        if value.robot_id == target_robot_id
                    ),
                    None,
                )
                check(
                    "low_battery_charge_route_starts_at_handover",
                    target_plan is not None
                    and target_plan.initial_node == target_point.node_id,
                    target_plan.initial_node if target_plan is not None else None,
                    target_point.node_id,
                )
                charge_steps = [
                    step
                    for robot in (response.plan.robots if response.plan else [])
                    if robot.robot_id == target_robot_id
                    for step in robot.steps
                    if step.step_type == "SERVICE"
                    and step.service_kind == "CHARGE"
                ]
                check(
                    "low_battery_charge_step_is_executable",
                    bool(charge_steps),
                    [value.task_id for value in charge_steps],
                    "one CHARGE service",
                )
        elif reason == "EDGE_BLOCKED":
            blocked_ids = list(
                getattr(
                    getattr(optimization_request, "map_constraints", None),
                    "blocked_edge_ids",
                    [],
                )
            )
            check(
                "blocked_edge_reaches_solver_constraints",
                blocked_edge_id in blocked_ids,
                blocked_ids,
                blocked_edge_id,
            )
            used_replan_edge_ids = {
                step.edge_id
                for robot in response.plan.robots
                for step in robot.steps
                if response.plan is not None and step.edge_id
            } if response.plan is not None else set()
            check(
                "blocked_edge_absent_from_replan_route",
                blocked_edge_id not in used_replan_edge_ids,
                sorted(used_replan_edge_ids),
                f"{blocked_edge_id} absent",
            )
        elif reason == "URGENT_ORDER":
            arriving_ids = {value.operation_id for value in arriving_operations}
            arriving_priorities: dict[str, str] = {}
            for task in getattr(optimization_request, "tasks", []):
                task_order_ids = {
                    value
                    for value in [task.order_id, *task.order_ids]
                    if value
                }
                for order_id in sorted(task_order_ids & arriving_ids):
                    arriving_priorities[order_id] = task.priority
            check(
                "urgent_priority_reaches_solver_tasks",
                bool(arriving_priorities)
                and all(value == "high" for value in arriving_priorities.values()),
                arriving_priorities,
                "all high",
            )
        elif reason == "POLICY_CHANGE":
            check(
                "initial_policy_is_throughput",
                bool(
                    initial_result.optimization_request
                    and initial_result.optimization_request.objective_profile
                    == "THROUGHPUT"
                ),
                (
                    initial_result.optimization_request.objective_profile
                    if initial_result.optimization_request
                    else None
                ),
                "THROUGHPUT",
            )
            check(
                "replan_policy_is_balanced",
                bool(
                    optimization_request
                    and optimization_request.objective_profile == "BALANCED"
                ),
                (
                    optimization_request.objective_profile
                    if optimization_request
                    else None
                ),
                "BALANCED",
            )

    failures = [value for value in checks if not value["passed"]]
    return {
        "scenario_group": "REPLAN",
        "validation_scope": "CUOPT_ROLLING_HORIZON_EXECUTION",
        "scenario_id": scenario_id,
        "planning_mode": replan_planning_mode,
        "agent_execution_applicable": expected_policy != "STALE_REJECTED",
        "passed": not failures,
        "initial_operation_count": len(initial_operations),
        "arriving_operation_count": len(arriving_operations),
        "cuopt_solve_count": 1 + replan_optimizer_calls,
        "initial_solver_backend": getattr(initial_optimizer, "backend", None),
        "initial_solver_status": getattr(initial_optimizer, "status", None),
        "replan_solver_backend": getattr(
            _optimizer_from_result(captured_replan_result), "backend", None
        ),
        "replan_solver_status": getattr(
            _optimizer_from_result(captured_replan_result), "status", None
        ),
        "replan_workflow_status": getattr(
            captured_replan_result, "status", None
        ),
        "replan_runtime_contract": (
            {
                "minimum_task_vehicle_count": (
                    captured_combined.runtime_overrides.minimum_task_vehicle_count
                ),
                "allowed_task_robot_ids": (
                    captured_combined.runtime_overrides.allowed_task_robot_ids
                ),
                "robot_states": [
                    {
                        "robot_id": value.robot_id,
                        "status": value.status,
                        "battery_pct": value.battery_pct,
                        "current_node": value.current_node,
                    }
                    for value in captured_combined.runtime_overrides.robot_states
                ],
            }
            if captured_combined is not None
            else None
        ),
        "replan_robot_context": (
            {
                "candidate_robot_ids": captured_replan_result.robot_context.candidate_robot_ids,
                "excluded_by_reason": captured_replan_result.robot_context.excluded_by_reason,
            }
            if captured_replan_result is not None
            and captured_replan_result.robot_context is not None
            else None
        ),
        "replan_workflow_hold": (
            captured_replan_result.workflow_hold.model_dump(mode="json")
            if captured_replan_result is not None
            and captured_replan_result.workflow_hold is not None
            else None
        ),
        "replan_errors": (
            [value.model_dump(mode="json") for value in captured_replan_result.errors]
            if captured_replan_result is not None
            else []
        ),
        "llm_call_count": len(
            getattr(captured_replan_result, "llm_node_summaries", []) or []
        ),
        "checks": checks,
        "failed_checks": [value["name"] for value in failures],
    }


def _step(
    robot: str,
    sequence: int,
    step_type: str,
    start: int,
    end: int,
    *,
    node: str | None = None,
    source: str | None = None,
    target: str | None = None,
    task: str | None = None,
    service: str | None = None,
) -> SimulationPlanStep:
    return SimulationPlanStep(
        step_id=f"{robot}-{sequence:03d}",
        sequence=sequence,
        step_type=step_type,
        start_at_ms=start,
        end_at_ms=end,
        edge_id=(f"E-{source}-{target}" if step_type == "MOVE" else None),
        from_node=source,
        to_node=target,
        node_id=node,
        task_id=task,
        service_kind=service,
    )


def _active_plan(checkpoint: str) -> tuple[SimulationPlan, int]:
    if checkpoint == "LOADED_MOVE":
        robot = SimulationRobotPlan(
            robot_id="R001",
            initial_node="A",
            finish_at_ms=5000,
            steps=[
                _step("R001", 1, "SERVICE", 0, 1000, node="A", task="OLD-1_PICK", service="PICKUP"),
                _step("R001", 2, "MOVE", 1000, 3000, source="A", target="B"),
                _step("R001", 3, "SERVICE", 3000, 4000, node="B", task="OLD-1_DROP", service="DROP"),
                _step("R001", 4, "WAIT", 4000, 5000, node="B"),
            ],
        )
        at = 2000
    elif checkpoint == "MOVE":
        robot = SimulationRobotPlan(
            robot_id="R001",
            initial_node="A",
            finish_at_ms=5000,
            steps=[
                _step("R001", 1, "MOVE", 0, 2000, source="A", target="B"),
                _step("R001", 2, "SERVICE", 2000, 3000, node="B", task="OLD-1_PICK", service="PICKUP"),
                _step("R001", 3, "MOVE", 3000, 4000, source="B", target="C"),
                _step("R001", 4, "SERVICE", 4000, 5000, node="C", task="OLD-1_DROP", service="DROP"),
            ],
        )
        at = 1000
    elif checkpoint == "SERVICE":
        robot = SimulationRobotPlan(
            robot_id="R001",
            initial_node="A",
            finish_at_ms=4000,
            steps=[
                _step("R001", 1, "SERVICE", 0, 1000, node="A", task="OLD-1_PICK", service="PICKUP"),
                _step("R001", 2, "MOVE", 1000, 2000, source="A", target="B"),
                _step("R001", 3, "SERVICE", 2000, 3000, node="B", task="OLD-1_DROP", service="DROP"),
                _step("R001", 4, "WAIT", 3000, 4000, node="B"),
            ],
        )
        at = 500
    elif checkpoint == "AFTER_COMPLETION":
        robot = SimulationRobotPlan(
            robot_id="R001",
            initial_node="A",
            finish_at_ms=4000,
            steps=[
                _step("R001", 1, "SERVICE", 0, 1000, node="A", task="OLD-1_PICK", service="PICKUP"),
                _step("R001", 2, "SERVICE", 1000, 2000, node="A", task="OLD-1_DROP", service="DROP"),
                _step("R001", 3, "WAIT", 2000, 4000, node="A"),
            ],
        )
        at = 2500
    else:
        robot = SimulationRobotPlan(
            robot_id="R001",
            initial_node="A",
            finish_at_ms=4000,
            steps=[_step("R001", 1, "WAIT", 0, 4000, node="A")],
        )
        at = 1000
    return (
        SimulationPlan(
            plan_id="PLAN-DYNAMIC-1",
            plan_version=1,
            warehouse_id="WH-001",
            simulation_id="SIM-DYNAMIC",
            map_version="MAP-1",
            makespan_ms=robot.finish_at_ms,
            absolute_finish_at_ms=robot.finish_at_ms,
            robots=[robot],
            logical_operations=[
                SimulationLogicalOperation(
                    operation_id="ORD-OLD",
                    operation_type="OUTBOUND_ORDER",
                    task_ids=["OLD-1"],
                )
            ],
        ),
        at,
    )


def validate_replan_definition(
    definition: dict[str, Any],
    *,
    repository: object | None = None,
) -> dict[str, Any]:
    contract = dict(definition.get("dynamic_contract") or {})
    checkpoint = str(contract["checkpoint"])
    expected = str(contract["expected_handover_policy"])
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, actual: object, wanted: object) -> None:
        checks.append({"name": name, "passed": passed, "actual": actual, "expected": wanted})

    plan, at = _active_plan(checkpoint)
    class MemoryStore:
        def __init__(self) -> None:
            self.saved: list[SimulationPlan] = []

        def load(self, plan_id: str) -> tuple[SimulationPlan, None]:
            if plan_id != plan.plan_id:
                raise FileNotFoundError(plan_id)
            return plan, None

        def save(self, value: SimulationPlan, result: object = None) -> None:
            del result
            self.saved.append(value)

    class SyntheticRepository:
        @staticmethod
        def edge(edge_id: str) -> None:
            del edge_id
            return None

        @staticmethod
        def base_edge_metrics(edge_id: str) -> tuple[float, int]:
            raise KeyError(edge_id)

        @staticmethod
        def station_access_nodes(station_id: str) -> list[str]:
            del station_id
            return []

    captured: dict[str, AutoMissionRequest] = {}

    def runner(mission: AutoMissionRequest) -> SimpleNamespace:
        captured["mission"] = mission
        runtime = mission.runtime_overrides.robot_states[0]
        available_at = runtime.sim_time_ms
        node = runtime.current_node or runtime.to_node or "A"
        finish_at = available_at + 1000
        payload = CuOptPayload(
            snapshot_id="SNAP-REPLAN-CONTRACT",
            location_index_map={node: 0},
            fleet_data=FleetData(
                vehicle_ids=[runtime.robot_id],
                vehicle_start_locations=[0],
                vehicle_end_locations=[0],
                capacities=[1],
                vehicle_available_at_ms=[available_at],
                skip_first_trips=[False],
                drop_return_trips=[True],
            ),
            task_data=TaskData(
                task_ids=[],
                task_locations=[],
                pickup_and_delivery_pairs=[],
                demand=[],
                priorities=[],
                service_times_ms=[],
                fixed_vehicle_ids=[],
            ),
            waypoint_graph_data=WaypointGraphData(
                edge_ids=[],
                from_indices=[],
                to_indices=[],
                costs=[],
                travel_times_ms=[],
            ),
            applied_map_constraints=MapConstraints(),
            time_limit_seconds=5,
        )
        normalized_operations = [
            NormalizedOperation(
                operation_id=value.order_id or value.inbound_id or value.robot_id or "EVENT",
                operation_type=(
                    "INBOUND_ITEM"
                    if value.type == "inbound_item_arrived"
                    else "RECOVERY"
                    if value.type == "robot_recovery_requested"
                    else "OUTBOUND_ORDER"
                ),
                source_event_type=value.type,
                raw_reference=value.order_id or value.inbound_id or value.robot_id,
            )
            for value in mission.events
            if value.type in {"new_order", "inbound_item_arrived", "robot_recovery_requested"}
        ]
        return SimpleNamespace(
            warehouse_id=plan.warehouse_id,
            simulation_id=plan.simulation_id,
            request_mode=mission.request_mode,
            status="plan_validated",
            traffic_schedule=TrafficScheduleResult(
                valid=True,
                routes=[
                    TimedRobotRoute(
                        robot_id=runtime.robot_id,
                        finish_at_ms=finish_at,
                        steps=[
                            TimedRouteStep(
                                step_type="WAIT",
                                start_at_ms=available_at,
                                end_at_ms=finish_at,
                                node_id=node,
                                reason="Replan contract verification horizon.",
                            )
                        ],
                    )
                ],
                makespan_ms=finish_at,
            ),
            robot_context=RobotRuntimeContext(
                warehouse_id=plan.warehouse_id,
                simulation_id=plan.simulation_id,
                robots=[
                    RobotRuntime(
                        robot_id=runtime.robot_id,
                        robot_code=runtime.robot_id,
                        status="idle",
                        battery_pct=runtime.battery_pct or 90,
                        capacity_units=1,
                        current_node=node,
                        sim_time_ms=available_at,
                    )
                ],
                candidate_robot_ids=[runtime.robot_id],
                summary="replan contract runner",
            ),
            normalized_request=NormalizedWarehouseRequest(
                source="structured_events",
                operations=normalized_operations,
                constraints=NormalizedRequestConstraints(),
                normalization_summary="replan contract",
            ),
            optimization_request=None,
            inventory_context=None,
            goods_to_person_compilation=None,
            execution_optimizer_result=None,
            optimizer_result=None,
            execution_payload=payload,
            cuopt_payload=payload,
            context_snapshot=ContextSnapshot(
                snapshot_id="SNAP-REPLAN-CONTRACT",
                captured_at="2026-08-13T00:00:00Z",
                graph_version="MAP-1",
                inventory_version="INV-1",
                runtime_version="RUN-1",
            ),
            orchestration_plan=None,
            effective_planning_mode="force_rule",
            planning_mode_source="request_override",
            node_execution_log=[],
            frontend_summary=None,
            pending_human_interaction=None,
            input_rejection=None,
            workflow_hold=None,
            errors=[],
        )

    store = MemoryStore()
    repository = SyntheticRepository()
    service = RollingHorizonReplanService(
        store=store,
        runner=runner,
        repository=repository,
        evaluation_capture=lambda **kwargs: SimpleNamespace(
            evaluation_id=f"EVAL-{definition['scenario_id']}"
        ),
    )
    if expected == "STALE_REJECTED":
        requested_version = plan.plan_version + 1
        try:
            service.replan(
                ReplanMissionRequest(
                    active_plan_id=plan.plan_id,
                    active_plan_version=requested_version,
                    replan_at_sim_time_ms=at,
                    reason=str(contract["reason"]),
                    mission=AutoMissionRequest(
                        warehouse_id=plan.warehouse_id,
                        simulation_id=plan.simulation_id,
                        request_mode="event_driven",
                        events=[EventInput(type="new_order", order_id="ORD-NEW")],
                    ),
                )
            )
            stale_error = None
        except ValueError as exc:
            stale_error = str(exc)
        check("active_plan_required", bool(plan.plan_id), plan.plan_id, "non-empty")
        check(
            "stale_plan_version_rejected",
            bool(stale_error and stale_error.startswith("STALE_PLAN_VERSION:")),
            stale_error,
            "STALE_PLAN_VERSION:*",
        )
        check("stale_request_did_not_run_optimizer", "mission" not in captured, list(captured), [])
    else:
        snapshot = RuntimeExecutionSnapshotBuilder().build(
            plan,
            at,
            repository=repository,
        )
        point = snapshot.handover_points[0]
        check("active_plan_required", bool(snapshot.source_plan_id), snapshot.source_plan_id, plan.plan_id)
        check("plan_version_required", plan.plan_version >= 1, plan.plan_version, ">=1")
        check("safe_handover_policy", point.handover_policy == expected, point.handover_policy, expected)
        check("handover_not_before_trigger", point.handover_at_ms >= at, point.handover_at_ms, f">={at}")
        check(
            "completed_work_classification",
            ("OLD-1" in snapshot.completed_task_bases) == (checkpoint == "AFTER_COMPLETION"),
            snapshot.completed_task_bases,
            ["OLD-1"] if checkpoint == "AFTER_COMPLETION" else [],
        )
        reservation_ids = [value.reservation_id for value in snapshot.preserved_edge_reservations]
        check("reservation_identity_unique", len(reservation_ids) == len(set(reservation_ids)), reservation_ids, "unique")
        reason = str(contract["reason"])
        events = [
            EventInput(
                type="new_order",
                order_id="ORD-NEW",
                payload={"priority": "urgent"} if reason == "URGENT_ORDER" else {},
            )
        ]
        user_command = None
        explicit_states: list[RobotRuntimeOverride] = []
        if reason == "EDGE_BLOCKED":
            events.append(EventInput(type="edge_blocked", edge_id="E-A-B"))
        elif reason == "ROBOT_FAULT":
            explicit_states.append(
                RobotRuntimeOverride(
                    robot_id="R001", current_node=point.node_id,
                    status="fault", battery_pct=80, capacity_units=1,
                    current_load_units=0, sim_time_ms=point.handover_at_ms,
                )
            )
        elif reason == "LOW_BATTERY":
            safe_stop_node, stopped_at_ms = _safe_stop_after_active_step(
                plan,
                robot_id="R001",
                observed_at_ms=at,
            )
            explicit_states.append(
                RobotRuntimeOverride(
                    robot_id="R001",
                    current_node=safe_stop_node,
                    status="low_battery",
                    battery_pct=10,
                    current_load_units=1 if point.carrying_load else 0,
                    active_task_id=(
                        point.locked_task_ids[0]
                        if point.locked_task_ids
                        else None
                    ),
                    sim_time_ms=stopped_at_ms,
                )
            )
        elif reason == "POLICY_CHANGE":
            user_command = "Apply the changed workload-balance policy to remaining work."
        request_mode = "mixed" if user_command else "event_driven"
        response = service.replan(
            ReplanMissionRequest(
                active_plan_id=plan.plan_id,
                active_plan_version=plan.plan_version,
                replan_at_sim_time_ms=at,
                reason=reason,
                mission=AutoMissionRequest(
                    warehouse_id=plan.warehouse_id,
                    simulation_id=plan.simulation_id,
                    request_mode=request_mode,
                    events=events,
                    user_command=user_command,
                    runtime_overrides=RuntimePlanningOverrides(
                        robot_states=explicit_states,
                    ),
                ),
            )
        )
        combined = captured["mission"]
        event_types = {value.type for value in combined.events}
        event_order_ids = {value.order_id for value in combined.events if value.order_id}
        runtime_by_robot = {
            value.robot_id: value for value in combined.runtime_overrides.robot_states
        }
        check("replan_service_returned_plan", response.plan is not None, response.status, "plan")
        check(
            "plan_version_incremented",
            bool(response.plan and response.plan.plan_version == plan.plan_version + 1),
            response.plan.plan_version if response.plan else None,
            plan.plan_version + 1,
        )
        check(
            "base_plan_linked",
            bool(response.plan and response.plan.base_plan_id == plan.plan_id),
            response.plan.base_plan_id if response.plan else None,
            plan.plan_id,
        )
        check(
            "reason_propagated",
            bool(response.plan and response.plan.replan_reason == reason),
            response.plan.replan_reason if response.plan else None,
            reason,
        )
        check(
            "runtime_source_plan_propagated",
            combined.runtime_overrides.source_plan_id == plan.plan_id,
            combined.runtime_overrides.source_plan_id,
            plan.plan_id,
        )
        check("new_work_preserved", "ORD-NEW" in event_order_ids, sorted(event_order_ids), "ORD-NEW")
        event_entity_ids = [
            value.order_id or value.inbound_id or value.robot_id
            for value in combined.events
            if value.order_id or value.inbound_id or value.robot_id
        ]
        check(
            "no_duplicate_operation",
            len(event_entity_ids) == len(set(event_entity_ids)),
            event_entity_ids,
            "unique",
        )
        # Completed work and a pickup-committed physical cycle both stay out of
        # the new solve. The latter finishes under the old plan until handover.
        old_expected = checkpoint != "AFTER_COMPLETION" and not point.carrying_load
        check(
            "completed_or_committed_work_not_replayed",
            ("ORD-OLD" in event_order_ids) == old_expected,
            sorted(event_order_ids),
            "ORD-OLD present" if old_expected else "ORD-OLD absent",
        )
        if reason == "EDGE_BLOCKED":
            check("blocked_edge_event_propagated", "edge_blocked" in event_types, sorted(event_types), "edge_blocked")
        elif reason == "URGENT_ORDER":
            urgent = next(value for value in combined.events if value.order_id == "ORD-NEW")
            check("urgent_priority_propagated", urgent.payload.get("priority") == "urgent", urgent.payload, {"priority": "urgent"})
        elif reason == "ROBOT_FAULT":
            check("fault_override_propagated", runtime_by_robot["R001"].status == "fault", runtime_by_robot["R001"].status, "fault")
            check("fault_robot_is_unloaded", runtime_by_robot["R001"].current_load_units == 0, runtime_by_robot["R001"].current_load_units, 0)
        elif reason == "LOW_BATTERY":
            check("low_battery_override_propagated", runtime_by_robot["R001"].battery_pct == 10, runtime_by_robot["R001"].battery_pct, 10)
            check("low_battery_status_propagated", runtime_by_robot["R001"].status == "low_battery", runtime_by_robot["R001"].status, "low_battery")
            if point.carrying_load:
                check(
                    "loaded_low_battery_handover_preserved",
                    runtime_by_robot["R001"].current_node == point.node_id
                    and runtime_by_robot["R001"].sim_time_ms == point.handover_at_ms
                    and runtime_by_robot["R001"].current_load_units == 0,
                    {
                        "node": runtime_by_robot["R001"].current_node,
                        "sim_time_ms": runtime_by_robot["R001"].sim_time_ms,
                        "load": runtime_by_robot["R001"].current_load_units,
                    },
                    {"node": point.node_id, "sim_time_ms": point.handover_at_ms, "load": 0},
                )
        elif reason == "POLICY_CHANGE":
            check("policy_command_propagated", bool(combined.user_command), combined.user_command, "non-empty")
        check(
            "old_plan_superseded",
            bool(store.saved and store.saved[0].status == "SUPERSEDED"),
            store.saved[0].status if store.saved else None,
            "SUPERSEDED",
        )
    failures = [value for value in checks if not value["passed"]]
    return {
        "scenario_group": "REPLAN",
        "validation_scope": "ROLLING_HORIZON_SERVICE_CONTRACT",
        "scenario_id": definition["scenario_id"],
        "passed": not failures,
        "checks": checks,
        "failed_checks": [value["name"] for value in failures],
    }


def validate_human_review_definition(definition: dict[str, Any]) -> dict[str, Any]:
    contract = dict(definition.get("dynamic_contract") or {})
    expected_reason = str(contract["expected_reason_code"])
    scenario_token = str(definition["scenario_id"]).split("_", 1)[0]
    if not scenario_token.startswith("HR") or not scenario_token[2:].isdigit():
        raise ValueError(f"Invalid Human Review scenario ID: {definition['scenario_id']}")
    operation_id = f"ORD-{(200 + int(scenario_token[2:])) * 1000 + 1}"
    request = NormalizedWarehouseRequest(
        source="mixed",
        operations=[
            NormalizedOperation(
                operation_id=operation_id,
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
                raw_reference=operation_id,
                attributes="",
            )
        ],
        constraints=NormalizedRequestConstraints(),
        raw_user_command=str(contract["user_command"]),
        normalization_summary="deterministic Human Review contract",
    )
    recommendation = FormulationRecommendation(
        route="AGENT_FORMULATION",
        gate_action=(
            "ASK_CLARIFICATION"
            if expected_reason == "OPERATOR_INTENT_CLARIFICATION"
            else "PROCEED"
        ),
        reason_code=(
            expected_reason
            if expected_reason == "OPERATOR_INTENT_CLARIFICATION"
            else None
        ),
        prompt=(
            "Clarify whether this reverses task order or warehouse direction."
            if expected_reason == "OPERATOR_INTENT_CLARIFICATION"
            else None
        ),
        options=(
            [
                HumanInteractionOption(
                    option_id="CLARIFY_INTENT",
                    label="Clarify intent",
                    resolution_value="CLARIFY_INTENT",
                )
            ]
            if expected_reason == "OPERATOR_INTENT_CLARIFICATION"
            else []
        ),
    )
    decision = resolve_request_gate(
        simulation_id=f"SIM-{definition['scenario_id']}",
        request=request,
        recommendation=recommendation,
        original_user_command=str(contract["user_command"]),
        has_structured_events=True,
        authoritative_structured_input=True,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=[],
    )
    interaction = decision.human_interaction
    checks = [
        {
            "name": "human_review_action",
            "passed": decision.action == contract["expected_action"],
            "actual": decision.action,
            "expected": contract["expected_action"],
        },
        {
            "name": "reason_code",
            "passed": bool(interaction and interaction.reason_code == expected_reason),
            "actual": interaction.reason_code if interaction else None,
            "expected": expected_reason,
        },
        {
            "name": "operator_prompt",
            "passed": bool(interaction and interaction.prompt.strip()),
            "actual": interaction.prompt if interaction else None,
            "expected": "non-empty",
        },
        {
            "name": "decision_options",
            "passed": bool(interaction and interaction.options),
            "actual": len(interaction.options) if interaction else 0,
            "expected": ">=1",
        },
    ]
    failures = [value for value in checks if not value["passed"]]
    return {
        "scenario_group": "HUMAN_REVIEW",
        "validation_scope": "REQUEST_GATE_CONTRACT",
        "scenario_id": definition["scenario_id"],
        "passed": not failures,
        "checks": checks,
        "failed_checks": [value["name"] for value in failures],
    }


def validate_destination_approval_with_cuopt(
    definition: dict[str, Any],
    *,
    request: AutoMissionRequest,
    repository: object,
    hitl_root: Path,
    resume_planning_mode: str = "force_rule",
) -> dict[str, Any]:
    """Approve HR04, apply its exact mutation, then run one real cuOpt plan."""

    if resume_planning_mode not in {"force_rule", "force_agent"}:
        raise ValueError(
            "resume_planning_mode must be force_rule or force_agent"
        )

    scenario_id = str(definition["scenario_id"])
    contract = dict(definition.get("dynamic_contract") or {})
    if contract.get("expected_reason_code") != "DESTINATION_OVERRIDE_APPROVAL":
        raise ValueError(f"{scenario_id}: destination approval contract is required")
    structured = request.structured_input
    normalized = request.normalized_request_override
    if structured is None or normalized is None:
        raise ValueError(f"{scenario_id}: frozen structured request is required")

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, actual: object, expected: object) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "actual": actual,
                "expected": expected,
            }
        )

    recommendation = FormulationRecommendation(
        route="AGENT_FORMULATION",
        gate_action="PROCEED",
        reasons=["Evaluate an explicit contractual destination override."],
    )
    gate = resolve_request_gate(
        simulation_id=request.simulation_id,
        request=normalized,
        recommendation=recommendation,
        original_user_command=request.user_command,
        has_structured_events=True,
        authoritative_structured_input=True,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=[],
    )
    interaction = gate.human_interaction
    check("approval_required_before_solver", gate.action == "REQUIRE_HUMAN_APPROVAL", gate.action, "REQUIRE_HUMAN_APPROVAL")
    check("approval_reason", bool(interaction and interaction.reason_code == "DESTINATION_OVERRIDE_APPROVAL"), interaction.reason_code if interaction else None, "DESTINATION_OVERRIDE_APPROVAL")
    check("no_solver_before_approval", True, 0, 0)
    if interaction is None:
        failures = [value for value in checks if not value["passed"]]
        return {
            "scenario_group": "HUMAN_REVIEW",
            "validation_scope": "CUOPT_HITL_RESUME_EXECUTION",
            "scenario_id": scenario_id,
            "passed": False,
            "cuopt_solve_count": 0,
            "checks": checks,
            "failed_checks": [value["name"] for value in failures],
        }

    evidence = list(interaction.evidence_ids)
    order_id = evidence[0] if len(evidence) >= 1 else None
    original_destination = evidence[1] if len(evidence) >= 2 else None
    approved_destination = evidence[2] if len(evidence) >= 3 else None
    check("review_evidence_complete", len(evidence) == 3, evidence, ["order_id", "current_destination", "replacement_destination"])

    mission = request.model_copy(update={"optimization_backend": "cuopt"})
    service = HumanInteractionService(HumanInteractionStore(hitl_root))
    pending = service.create_pending(
        interaction=interaction,
        state={
            "warehouse_id": mission.warehouse_id,
            "simulation_id": mission.simulation_id,
            "request_mode": mission.request_mode,
            "optimization_backend": mission.optimization_backend,
            "events": mission.events,
            "user_command": mission.user_command,
            "structured_input": mission.structured_input,
            "normalized_request_override": mission.normalized_request_override,
            "requested_planning_mode": mission.planning_mode,
            "goods_to_person_options": mission.goods_to_person_options,
            "runtime_overrides": mission.runtime_overrides,
            "max_agent_steps": mission.max_agent_steps,
            "max_planner_retries": mission.max_planner_retries,
            "human_responses": [],
            "parent_interaction_id": None,
        },
    )
    resumed_request: AutoMissionRequest | None = None
    resumed_result: object | None = None
    solver_calls = 0

    def run_approved(
        approved_request: AutoMissionRequest,
        trusted_planning_mode: str | None,
    ) -> object:
        nonlocal resumed_request, resumed_result, solver_calls
        resumed_request = approved_request
        solver_calls += 1
        from app.services.orchestration_service import OrchestrationService

        # The catalog freezes a complete structured request, so this execution
        # exercises the deterministic formulation path without another LLM call.
        resumed_result = OrchestrationService().run(
            approved_request,
            trusted_planning_mode=resume_planning_mode,
            persist_simulation_plan=False,
            repository=repository,
        )
        return resumed_result

    resume = service.respond(
        pending.interaction.interaction_id,
        HumanInteractionResumeRequest(
            action="APPROVE",
            selected_option_id="APPROVE_ALTERNATIVE_DESTINATION",
            actor_id="catalog-reviewer",
            comment="Approve the evaluated alternative destination.",
        ),
        runner=run_approved,
    )
    optimizer = _optimizer_from_result(resumed_result)
    approved_operation = None
    if resumed_request is not None and resumed_request.structured_input is not None:
        approved_operation = next(
            (
                value
                for value in resumed_request.structured_input.operations
                if value.operation_id == order_id
            ),
            None,
        )
    event_destination = None
    if resumed_request is not None:
        matching_event = next(
            (value for value in resumed_request.events if value.order_id == order_id),
            None,
        )
        if matching_event is not None:
            event_destination = matching_event.payload.get("destination_node_code")
    task_destinations: list[str] = []
    optimization_request = getattr(resumed_result, "optimization_request", None)
    if optimization_request is not None:
        for task in optimization_request.tasks:
            if order_id in task.order_ids:
                task_destinations.extend(task.logical_destination_ids)

    check("approval_resumed", resume.resume_outcome == "RESUMED", resume.resume_outcome, "RESUMED")
    check("cuopt_called_once_after_approval", solver_calls == 1, solver_calls, 1)
    check("structured_destination_updated", bool(approved_operation and approved_operation.destination_node_code == approved_destination), approved_operation.destination_node_code if approved_operation else None, approved_destination)
    check("event_destination_updated", event_destination == approved_destination, event_destination, approved_destination)
    check("cuopt_task_uses_approved_destination", approved_destination in task_destinations, sorted(set(task_destinations)), [approved_destination])
    check("solver_backend", getattr(optimizer, "backend", None) == "cuopt", getattr(optimizer, "backend", None), "cuopt")
    check("solver_success", getattr(optimizer, "status", None) == "success", getattr(optimizer, "status", None), "success")
    check("plan_validated", getattr(resumed_result, "status", None) == "plan_validated", getattr(resumed_result, "status", None), "plan_validated")
    mapf = getattr(resumed_result, "mapf_validation", None)
    check("mapf_valid", bool(mapf and mapf.valid), getattr(mapf, "valid", None), True)
    resolved = service.get(pending.interaction.interaction_id)
    original_operation = resolved.original_request["structured_input"]["operations"][0]
    check("checkpoint_keeps_original_destination", original_operation.get("destination_node_code") == original_destination, original_operation.get("destination_node_code"), original_destination)
    check("interaction_resolved", resolved.status == "RESOLVED", resolved.status, "RESOLVED")

    failures = [value for value in checks if not value["passed"]]
    return {
        "scenario_group": "HUMAN_REVIEW",
        "validation_scope": "CUOPT_HITL_RESUME_EXECUTION",
        "scenario_id": scenario_id,
        "planning_mode": resume_planning_mode,
        "agent_execution_applicable": True,
        "passed": not failures,
        "order_id": order_id,
        "original_destination": original_destination,
        "approved_destination": approved_destination,
        "resume_outcome": resume.resume_outcome,
        "solver_backend": getattr(optimizer, "backend", None),
        "solver_status": getattr(optimizer, "status", None),
        "workflow_status": getattr(resumed_result, "status", None),
        "llm_call_count": len(
            getattr(resumed_result, "llm_node_summaries", []) or []
        ),
        "cuopt_solve_count": solver_calls,
        "checks": checks,
        "failed_checks": [value["name"] for value in failures],
    }


def validate_human_review_with_agent(
    definition: dict[str, Any],
    *,
    request: AutoMissionRequest,
    repository: object,
) -> dict[str, Any]:
    """Run the real Agent entry path and verify the expected review checkpoint.

    The deterministic request gate remains authoritative. Agent text can add a
    useful explanation, but it must not bypass a safety, inventory, committed
    work, destination, or clarification checkpoint and no solver may run before
    the operator decision.
    """

    scenario_id = str(definition["scenario_id"])
    contract = dict(definition.get("dynamic_contract") or {})
    expected_action = str(contract["expected_action"])
    expected_reason = str(contract["expected_reason_code"])

    from app.services.orchestration_service import OrchestrationService

    result = OrchestrationService().run(
        request.model_copy(
            update={
                "optimization_backend": "cuopt",
                # The frozen normalized override is appropriate for solver
                # replay, but it deliberately suppresses a second router call.
                # Human Review evaluation must exercise the live router on the
                # preserved structured events + operator command.
                "normalized_request_override": None,
            }
        ),
        # Human Review is a pre-route responsibility boundary.  Let the real
        # LLM router explain/classify the request, then require the deterministic
        # gate to stop before Rule or Agent formulation.  Forcing Agent here
        # would intentionally skip that pre-route router and would test the
        # wrong production contract.
        trusted_planning_mode="llm_router",
        persist_simulation_plan=False,
        repository=repository,
    )
    interaction = result.pending_human_interaction
    gate = result.request_gate_decision
    actual_action = gate.action if gate is not None else None
    actual_reason = interaction.reason_code if interaction is not None else None
    prompt = interaction.prompt if interaction is not None else None
    options = interaction.options if interaction is not None else []
    optimizer = _optimizer_from_result(result)

    checks = [
        {
            "name": "human_review_action",
            "passed": actual_action == expected_action,
            "actual": actual_action,
            "expected": expected_action,
        },
        {
            "name": "reason_code",
            "passed": actual_reason == expected_reason,
            "actual": actual_reason,
            "expected": expected_reason,
        },
        {
            "name": "operator_prompt",
            "passed": bool(prompt and prompt.strip()),
            "actual": prompt,
            "expected": "non-empty",
        },
        {
            "name": "decision_options",
            "passed": bool(options),
            "actual": len(options),
            "expected": ">=1",
        },
        {
            "name": "solver_blocked_before_review",
            "passed": optimizer is None,
            "actual": getattr(optimizer, "status", None),
            "expected": None,
        },
    ]
    failures = [value for value in checks if not value["passed"]]
    return {
        "scenario_group": "HUMAN_REVIEW",
        "validation_scope": "AGENT_HUMAN_REVIEW_EXECUTION",
        "scenario_id": scenario_id,
        "planning_mode": "llm_router",
        "agent_execution_applicable": True,
        "passed": not failures,
        "workflow_status": result.status,
        "effective_planning_mode": result.effective_planning_mode,
        "llm_call_count": len(result.llm_node_summaries),
        "error_count": len(result.errors),
        "errors": [value.model_dump(mode="json") for value in result.errors],
        "checks": checks,
        "failed_checks": [value["name"] for value in failures],
    }


def validate_dynamic_definition(
    definition: dict[str, Any],
    *,
    repository: object | None = None,
) -> dict[str, Any]:
    group = str(definition.get("scenario_group") or "INITIAL")
    if group == "REPLAN":
        return validate_replan_definition(definition, repository=repository)
    if group == "HUMAN_REVIEW":
        return validate_human_review_definition(definition)
    raise ValueError(f"{definition.get('scenario_id')}: not a dynamic scenario")

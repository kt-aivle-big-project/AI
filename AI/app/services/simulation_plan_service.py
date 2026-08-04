"""Front-end simulation-plan projection and conservative rolling-horizon replanning."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Any
from uuid import uuid4

from app.core.config import get_settings
from app.repositories.json_repository import get_repository
from app.domain.schemas import (
    AutoMissionRequest,
    EdgeReservation,
    EventInput,
    NodeReservation,
    OrchestrationResult,
    PlanHandoverPoint,
    ReplanExecutionSnapshot,
    ReplanMissionRequest,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    SimulationLogicalOperation,
    SimulationPlan,
    SimulationPlanResponse,
    SimulationPlanStep,
    SimulationRobotPlan,
    StationServiceReservation,
)


_TASK_PHASE_SUFFIXES = ("_EMPTY_TOTE", "_RETURN", "_DROP", "_PICK")


def canonical_task_id(task_id: str | None) -> str | None:
    """Expose optimizer pickup/drop phases as one logical execution task."""

    if not task_id:
        return None
    for suffix in _TASK_PHASE_SUFFIXES:
        if task_id.endswith(suffix):
            return task_id[: -len(suffix)]
    return task_id


class SimulationPlanStore:
    """Small file-backed plan store used by the front-end PoC and replan API."""

    def __init__(self, root: Path | None = None) -> None:
        settings = get_settings()
        self.root = root or (settings.output_dir / "simulation_plans")
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, plan: SimulationPlan, result: OrchestrationResult | None = None) -> None:
        payload = {
            "plan": plan.model_dump(mode="json"),
            "orchestration_result": result.model_dump(mode="json") if result else None,
        }
        (self.root / f"{plan.plan_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self, plan_id: str) -> tuple[SimulationPlan, OrchestrationResult | None]:
        path = self.root / f"{plan_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Unknown simulation plan {plan_id}.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        plan = SimulationPlan.model_validate(payload["plan"])
        result = (
            OrchestrationResult.model_validate(payload["orchestration_result"])
            if payload.get("orchestration_result")
            else None
        )
        return plan, result


class SimulationPlanBuilder:
    """Project the validated MAPF schedule into a compact front-end contract."""

    def build(
        self,
        result: OrchestrationResult,
        *,
        plan_version: int = 1,
        base_plan_id: str | None = None,
        effective_from_sim_time_ms: int = 0,
        plan_start_sim_time_ms: int | None = None,
        schedule_times_are_absolute: bool = False,
        plan_kind: str = "INITIAL",
        handover_points: list[PlanHandoverPoint] | None = None,
        replan_reason: str | None = None,
        replan_requested_at_ms: int | None = None,
        repository: Any | None = None,
    ) -> SimulationPlan | None:
        schedule = result.traffic_schedule
        if result.status != "plan_validated" or schedule is None or not schedule.valid:
            return None
        settings = get_settings()
        warehouse_id = getattr(result, "warehouse_id", settings.default_warehouse_id)
        repository = repository or get_repository(warehouse_id, result.simulation_id)
        robot_start = {
            value.robot_id: value.current_node
            for value in (result.robot_context.robots if result.robot_context else [])
        }
        operation_tasks: dict[str, list[str]] = {}
        operation_robot: dict[str, str] = {}
        optimizer = result.execution_optimizer_result or result.optimizer_result
        if optimizer:
            for route in optimizer.routes:
                for task_id in route.task_sequence:
                    operation_robot[task_id] = route.vehicle_id

        batches = result.goods_to_person_compilation.batches if result.goods_to_person_compilation else []
        batch_by_order = {
            order_id: batch
            for batch in batches
            for order_id in batch.order_ids
        }
        logical: list[SimulationLogicalOperation] = []
        normalized = result.normalized_request
        if normalized:
            for operation in normalized.operations:
                if operation.operation_type not in {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}:
                    continue
                matching_tasks = []
                matching_task_records = []
                if result.optimization_request:
                    for task in result.optimization_request.tasks:
                        if operation.operation_type == "OUTBOUND_ORDER" and operation.operation_id in task.order_ids:
                            matching_tasks.append(task.task_id)
                            matching_task_records.append(task)
                        elif operation.operation_type == "INBOUND_ITEM" and task.operation_type == "INBOUND_ITEM":
                            if task.order_id == operation.operation_id:
                                matching_tasks.append(task.task_id)
                                matching_task_records.append(task)
                batch = batch_by_order.get(operation.operation_id)
                assigned = None
                if batch:
                    assigned = batch.mobile_robot_id or next(
                        (robot for task, robot in operation_robot.items() if task.startswith(batch.batch_id)), None
                    )
                    matching_tasks = [batch.batch_id]
                elif matching_tasks:
                    assigned = next(
                        (robot for task, robot in operation_robot.items() if any(task.startswith(value) for value in matching_tasks)),
                        None,
                    )
                inbound_need = None
                if result.inventory_context:
                    inbound_need = next(
                        (value for value in result.inventory_context.inbound_needs if value.inbound_id == operation.operation_id),
                        None,
                    )
                order_need = None
                if result.inventory_context:
                    order_need = next(
                        (value for value in result.inventory_context.task_needs if value.order_id == operation.operation_id),
                        None,
                    )
                # An inbound operation is compiled into one physical BOX task,
                # so its selected rack and level are authoritative.  Outbound
                # orders can expand into multiple rack tasks; only expose the
                # singular physical rack when the operation has exactly one.
                physical_task = (
                    matching_task_records[0]
                    if len(matching_task_records) == 1
                    else None
                )
                rack_id = (
                    physical_task.rack_id
                    if physical_task and physical_task.rack_id
                    else inbound_need.target_rack_id
                    if inbound_need
                    else None
                )
                rack_level = (
                    physical_task.rack_level
                    if physical_task and physical_task.rack_level
                    else inbound_need.target_rack_level
                    if inbound_need
                    else None
                )
                logical.append(
                    SimulationLogicalOperation(
                        operation_id=operation.operation_id,
                        operation_type=operation.operation_type,
                        item_id=(batch.item_id if batch else inbound_need.item_id if inbound_need else order_need.item_id if order_need else None),
                        quantity=(batch.requested_quantity if batch else inbound_need.quantity if inbound_need else order_need.required_qty if order_need else None),
                        logical_destination_id=(
                            ",".join(batch.logical_destination_ids)
                            if batch
                            else rack_id
                            if operation.operation_type == "INBOUND_ITEM"
                            else order_need.delivery_node
                            if order_need
                            else None
                        ),
                        rack_id=rack_id,
                        rack_level=rack_level,
                        source_port_id=inbound_need.source_port_id if inbound_need else None,
                        handling_unit_id=batch.handling_unit_id if batch else inbound_need.handling_unit_id if inbound_need else None,
                        assigned_robot_id=assigned,
                        task_ids=matching_tasks,
                    )
                )

        payload = getattr(result, "execution_payload", None) or getattr(
            result, "cuopt_payload", None
        )
        raw_available_by_robot: dict[str, int] = {}
        if payload is not None:
            available_values = list(payload.fleet_data.vehicle_available_at_ms) or [
                0 for _ in payload.fleet_data.vehicle_ids
            ]
            raw_available_by_robot = {
                robot_id: int(available_at_ms)
                for robot_id, available_at_ms in zip(
                    payload.fleet_data.vehicle_ids,
                    available_values,
                    strict=True,
                )
            }

        robot_plans: list[SimulationRobotPlan] = []
        offset = 0 if schedule_times_are_absolute else int(effective_from_sim_time_ms)
        for route in schedule.routes:
            route_steps = list(route.steps)
            initial = robot_start.get(route.robot_id)
            if initial is None and route_steps:
                initial = route_steps[0].from_node or route_steps[0].node_id
            current_node = initial
            raw_available_at_ms = raw_available_by_robot.get(
                route.robot_id,
                int(plan_start_sim_time_ms or effective_from_sim_time_ms)
                if schedule_times_are_absolute
                else 0,
            )
            cursor_ms = raw_available_at_ms
            steps: list[SimulationPlanStep] = []

            def append_step(
                *,
                step_type: str,
                start_at_ms: int,
                end_at_ms: int,
                node_id: str | None = None,
                edge_id: str | None = None,
                from_node: str | None = None,
                to_node: str | None = None,
                task_id: str | None = None,
                service_kind: str | None = None,
                reason: str | None = None,
            ) -> None:
                distance_m = None
                nominal_speed_mps = None
                nominal_travel_time_ms = None
                if step_type == "MOVE" and edge_id:
                    edge = repository.edge(edge_id) or {}
                    try:
                        distance_m, nominal_travel_time_ms = repository.base_edge_metrics(edge_id)
                    except (KeyError, ValueError):
                        # Synthetic unit-test schedules and external planners may
                        # use an edge ID that is not in the active map projection.
                        # Keep the executable time contract while leaving physical
                        # metrics unset instead of fabricating distance.
                        distance_m = None
                        nominal_travel_time_ms = None
                    nominal_speed_mps = (
                        float(edge.get("speed_limit_mps") or settings.robot_nominal_speed_mps)
                        if edge
                        else None
                    )
                sequence = len(steps) + 1
                steps.append(
                    SimulationPlanStep(
                        step_id=f"{route.robot_id}-{sequence:04d}",
                        sequence=sequence,
                        step_type=step_type,
                        start_at_ms=offset + int(start_at_ms),
                        end_at_ms=offset + int(end_at_ms),
                        node_id=node_id,
                        edge_id=edge_id,
                        from_node=from_node,
                        to_node=to_node,
                        task_id=canonical_task_id(task_id),
                        service_kind=service_kind,
                        reason=reason,
                        distance_m=distance_m,
                        nominal_speed_mps=nominal_speed_mps,
                        nominal_travel_time_ms=nominal_travel_time_ms,
                    )
                )

            for raw in route_steps:
                # A front-end plan never leaves implicit time gaps.  If MAPF or
                # a future planner returns a delayed first move/task, materialize
                # the interval as WAIT so browser playback remains deterministic.
                if raw.start_at_ms > cursor_ms:
                    wait_node = current_node or raw.from_node or raw.node_id
                    if wait_node:
                        append_step(
                            step_type="WAIT",
                            start_at_ms=cursor_ms,
                            end_at_ms=raw.start_at_ms,
                            node_id=wait_node,
                            reason="Explicit idle interval inserted by SimulationPlanBuilder.",
                        )
                append_step(
                    step_type=raw.step_type,
                    start_at_ms=raw.start_at_ms,
                    end_at_ms=raw.end_at_ms,
                    node_id=raw.node_id,
                    edge_id=raw.edge_id,
                    from_node=raw.from_node,
                    to_node=raw.to_node,
                    task_id=raw.task_id,
                    service_kind=raw.service_kind,
                    reason=raw.reason,
                )
                cursor_ms = raw.end_at_ms
                if raw.step_type == "MOVE":
                    current_node = raw.to_node or current_node
                elif raw.node_id:
                    current_node = raw.node_id

            if route.finish_at_ms > cursor_ms and current_node:
                append_step(
                    step_type="WAIT",
                    start_at_ms=cursor_ms,
                    end_at_ms=route.finish_at_ms,
                    node_id=current_node,
                    reason="Explicit final idle interval inserted by SimulationPlanBuilder.",
                )
            robot_plans.append(
                SimulationRobotPlan(
                    robot_id=route.robot_id,
                    initial_node=initial or "UNKNOWN",
                    available_at_ms=offset + raw_available_at_ms,
                    finish_at_ms=offset + route.finish_at_ms,
                    steps=steps,
                )
            )

        plan_id = (
            f"PLAN-{warehouse_id}-{result.simulation_id}-"
            f"{plan_version}-{uuid4().hex[:10].upper()}"
        )
        map_version = result.context_snapshot.graph_version if result.context_snapshot else "UNKNOWN"
        requested_start = (
            int(plan_start_sim_time_ms)
            if plan_start_sim_time_ms is not None
            else int(effective_from_sim_time_ms)
        )
        absolute_finish_at_ms = max(
            [offset + schedule.makespan_ms]
            + [value.handover_at_ms for value in (handover_points or [])]
        )
        earliest_robot_start = min(
            (value.available_at_ms for value in robot_plans),
            default=requested_start,
        )
        resolved_effective_from = max(requested_start, earliest_robot_start)
        return SimulationPlan(
            plan_id=plan_id,
            plan_version=plan_version,
            base_plan_id=base_plan_id,
            warehouse_id=warehouse_id,
            simulation_id=result.simulation_id,
            plan_kind=plan_kind,
            replan_reason=replan_reason,
            replan_requested_at_ms=replan_requested_at_ms,
            map_version=map_version,
            source_snapshot_id=(result.context_snapshot.snapshot_id if result.context_snapshot else None),
            plan_start_sim_time_ms=requested_start,
            effective_from_sim_time_ms=resolved_effective_from,
            sim_tick_ms=settings.simulation_tick_ms,
            makespan_ms=max(0, absolute_finish_at_ms - requested_start),
            absolute_finish_at_ms=absolute_finish_at_ms,
            robots=robot_plans,
            station_reservations=[
                value.model_copy(update={
                    "start_at_ms": offset + value.start_at_ms,
                    "end_at_ms": offset + value.end_at_ms,
                })
                for value in schedule.station_reservations
            ],
            logical_operations=logical,
            handover_points=list(handover_points or []),
            supersedes_plan_id=base_plan_id,
        )


class ReplanTimelineValidator:
    """Reject any absolute-time replan that schedules work before its horizon."""

    @staticmethod
    def validate(plan: SimulationPlan, horizon_start_ms: int) -> None:
        if plan.plan_kind != "REPLAN":
            return
        errors: list[str] = []
        for robot in plan.robots:
            if robot.available_at_ms < horizon_start_ms:
                errors.append(
                    "REPLAN_VEHICLE_BEFORE_HORIZON:"
                    f"{robot.robot_id}:available_at_ms={robot.available_at_ms}<horizon={horizon_start_ms}"
                )
            for step in robot.steps:
                if step.start_at_ms < horizon_start_ms:
                    errors.append(
                        "REPLAN_STEP_BEFORE_HORIZON:"
                        f"{robot.robot_id}:{step.step_id}:start_at_ms={step.start_at_ms}<horizon={horizon_start_ms}"
                    )
                if step.start_at_ms < robot.available_at_ms:
                    errors.append(
                        "REPLAN_STEP_BEFORE_VEHICLE_AVAILABLE:"
                        f"{robot.robot_id}:{step.step_id}:start_at_ms={step.start_at_ms}"
                        f"<available_at_ms={robot.available_at_ms}"
                    )
        expected_effective = min(
            (robot.available_at_ms for robot in plan.robots),
            default=horizon_start_ms,
        )
        if plan.effective_from_sim_time_ms != expected_effective:
            errors.append(
                "REPLAN_EFFECTIVE_TIME_MISMATCH:"
                "effective_from_sim_time_ms="
                f"{plan.effective_from_sim_time_ms};expected={expected_effective}"
            )
        if errors:
            raise ValueError(
                "REPLAN_TIMELINE_INVALID: " + "; ".join(errors)
            )


class RuntimeExecutionSnapshotBuilder:
    """Project one active plan into safe, per-robot rolling-horizon handovers.

    The browser is assumed to replay the validated plan exactly unless an
    explicit runtime snapshot says otherwise.  A replan therefore needs only
    the active plan and one simulation timestamp.  Robots without a load stop
    at the next safe boundary; robots that have started a physical handling
    cycle finish that entire cycle before becoming available to the new solve.
    """

    @staticmethod
    def _task_base(task_id: str | None) -> str | None:
        return canonical_task_id(task_id)

    @staticmethod
    def _resource_id(source: str, target: str) -> str:
        left, right = sorted((source, target))
        return f"CORRIDOR:{left}<->{right}"

    @staticmethod
    def _node_after_completed_steps(
        robot: SimulationRobotPlan, sim_time_ms: int
    ) -> str:
        node = robot.initial_node
        for step in robot.steps:
            if step.end_at_ms > sim_time_ms:
                break
            if step.step_type == "MOVE" and step.to_node:
                node = step.to_node
            elif step.node_id:
                node = step.node_id
        return node

    @staticmethod
    def _active_step(
        robot: SimulationRobotPlan, sim_time_ms: int
    ) -> SimulationPlanStep | None:
        return next(
            (
                step
                for step in robot.steps
                if step.start_at_ms <= sim_time_ms < step.end_at_ms
            ),
            None,
        )

    @staticmethod
    def _overlapping_steps(
        robot: SimulationRobotPlan, start_at_ms: int, end_at_ms: int
    ) -> list[SimulationPlanStep]:
        return [
            step
            for step in robot.steps
            if step.end_at_ms > start_at_ms and step.start_at_ms < end_at_ms
        ]

    def build(
        self,
        plan: SimulationPlan,
        replan_at_sim_time_ms: int,
        prior_result: OrchestrationResult | None = None,
        repository: Any | None = None,
    ) -> ReplanExecutionSnapshot:
        del prior_result  # The compact SimulationPlan contains the executable truth.
        if replan_at_sim_time_ms < plan.plan_start_sim_time_ms:
            raise ValueError("replan time cannot precede the active plan start")
        if replan_at_sim_time_ms > plan.absolute_finish_at_ms:
            raise ValueError("replan time cannot exceed the active plan finish")

        repository = repository or get_repository(
            plan.warehouse_id, plan.simulation_id
        )
        points: list[PlanHandoverPoint] = []
        overrides: list[RobotRuntimeOverride] = []
        preserved_edges: list[EdgeReservation] = []
        preserved_nodes: list[NodeReservation] = []
        preserved_stations: list[StationServiceReservation] = []
        completed_bases: set[str] = set()
        locked_bases: set[str] = set()
        preserve_until_by_robot: dict[str, int] = {}

        for robot in plan.robots:
            service_by_base: dict[str, list[SimulationPlanStep]] = {}
            for step in robot.steps:
                if step.step_type != "SERVICE" or not step.task_id:
                    continue
                base = self._task_base(step.task_id)
                if base:
                    service_by_base.setdefault(base, []).append(step)

            robot_locked_bases: list[str] = []
            locked_task_ids: list[str] = []
            commitment_end = -1
            commitment_node: str | None = None
            for base, raw_steps in service_by_base.items():
                ordered = sorted(
                    raw_steps, key=lambda value: (value.start_at_ms, value.end_at_ms)
                )
                final_step = ordered[-1]
                pickup_started = any(
                    step.service_kind == "PICKUP"
                    and step.start_at_ms <= replan_at_sim_time_ms
                    for step in ordered
                )
                if final_step.end_at_ms <= replan_at_sim_time_ms:
                    completed_bases.add(base)
                    continue
                if pickup_started:
                    locked_bases.add(base)
                    robot_locked_bases.append(base)
                    locked_task_ids.extend(
                        step.task_id for step in ordered if step.task_id
                    )
                    if final_step.end_at_ms > commitment_end:
                        commitment_end = final_step.end_at_ms
                        commitment_node = final_step.node_id

            active = self._active_step(robot, replan_at_sim_time_ms)
            current_node = self._node_after_completed_steps(
                robot, replan_at_sim_time_ms
            )
            carrying_load = bool(robot_locked_bases)

            if commitment_end >= 0:
                handover_at = commitment_end
                handover_node = commitment_node or current_node
                policy = "CURRENT_OPERATION_END"
                reason = (
                    "Pickup has started; finish the committed inbound/G2P physical "
                    "cycle before replacing future work."
                )
                preserve_until_by_robot[robot.robot_id] = handover_at
            elif active is None:
                handover_at = replan_at_sim_time_ms
                handover_node = current_node
                policy = "CURRENT_NODE"
                reason = "Robot is already at a safe node and may join the new horizon immediately."
            elif active.step_type == "MOVE":
                handover_at = active.end_at_ms
                handover_node = active.to_node or current_node
                policy = "NEXT_NODE"
                reason = "Finish the current edge and switch plans at its destination node."
                preserve_until_by_robot[robot.robot_id] = handover_at
            elif active.step_type == "WAIT":
                handover_at = replan_at_sim_time_ms
                handover_node = active.node_id or current_node
                policy = "CURRENT_NODE"
                reason = "A waiting robot without a committed load may switch at the current node."
            else:
                handover_at = active.end_at_ms
                handover_node = active.node_id or current_node
                policy = "CURRENT_SERVICE_END"
                reason = "Finish the current service interval before replacing future work."
                preserve_until_by_robot[robot.robot_id] = handover_at

            point = PlanHandoverPoint(
                robot_id=robot.robot_id,
                node_id=handover_node,
                handover_at_ms=handover_at,
                reason=reason,
                handover_policy=policy,
                current_step_id=active.step_id if active else None,
                locked_task_ids=sorted(set(locked_task_ids)),
                carrying_load=carrying_load,
            )
            points.append(point)
            overrides.append(
                RobotRuntimeOverride(
                    robot_id=robot.robot_id,
                    current_node=handover_node,
                    status="idle",
                    current_load_units=0,
                    sim_time_ms=handover_at,
                )
            )

            preserve_until = preserve_until_by_robot.get(robot.robot_id)
            if preserve_until is None or preserve_until <= replan_at_sim_time_ms:
                continue
            for step in self._overlapping_steps(
                robot, replan_at_sim_time_ms, preserve_until
            ):
                start_at = max(replan_at_sim_time_ms, step.start_at_ms)
                end_at = min(preserve_until, step.end_at_ms)
                if end_at <= start_at:
                    continue
                if step.step_type == "MOVE" and step.edge_id and step.from_node and step.to_node:
                    preserved_edges.append(
                        EdgeReservation(
                            reservation_id=(
                                f"REPLAN-{plan.plan_id}-{robot.robot_id}-"
                                f"EDGE-{step.sequence:04d}"
                            ),
                            edge_id=step.edge_id,
                            robot_id=robot.robot_id,
                            direction=f"{step.from_node}_TO_{step.to_node}",
                            start_at_ms=start_at,
                            end_at_ms=end_at,
                            from_node=step.from_node,
                            to_node=step.to_node,
                            physical_resource_id=self._resource_id(
                                step.from_node, step.to_node
                            ),
                        )
                    )
                elif step.step_type in {"WAIT", "SERVICE"} and step.node_id:
                    preserved_nodes.append(
                        NodeReservation(
                            reservation_id=(
                                f"REPLAN-{plan.plan_id}-{robot.robot_id}-"
                                f"NODE-{step.sequence:04d}"
                            ),
                            node_id=step.node_id,
                            robot_id=robot.robot_id,
                            start_at_ms=start_at,
                            end_at_ms=end_at,
                            reason=(
                                "Committed old-plan service interval"
                                if step.step_type == "SERVICE"
                                else "Committed old-plan node wait"
                            ),
                        )
                    )

        # A station robot and all of its access spurs remain occupied while a
        # committed G2P cycle finishes.  This keeps old-plan capacity visible to
        # the new MAPF solve without requiring continuous telemetry.
        for reservation in plan.station_reservations:
            preserve_until = preserve_until_by_robot.get(
                reservation.mobile_robot_id
            )
            if preserve_until is None:
                continue
            start_at = max(replan_at_sim_time_ms, reservation.start_at_ms)
            end_at = min(preserve_until, reservation.end_at_ms)
            if end_at <= start_at:
                continue
            preserved = reservation.model_copy(
                update={
                    "reservation_id": f"REPLAN-{plan.plan_id}-{reservation.reservation_id}",
                    "start_at_ms": start_at,
                    "end_at_ms": end_at,
                }
            )
            preserved_stations.append(preserved)
            for access_node in repository.station_access_nodes(
                reservation.station_id
            ):
                preserved_nodes.append(
                    NodeReservation(
                        reservation_id=(
                            f"REPLAN-{plan.plan_id}-{reservation.reservation_id}-"
                            f"{access_node}"
                        ),
                        node_id=access_node,
                        robot_id=reservation.mobile_robot_id,
                        start_at_ms=start_at,
                        end_at_ms=end_at,
                        reason=(
                            f"Station {reservation.station_id} is committed to "
                            f"{reservation.mobile_robot_id}."
                        ),
                    )
                )

        points.sort(key=lambda value: value.robot_id)
        overrides.sort(key=lambda value: value.robot_id)
        handover_times = [value.handover_at_ms for value in points]
        return ReplanExecutionSnapshot(
            source_plan_id=plan.plan_id,
            replan_at_sim_time_ms=replan_at_sim_time_ms,
            earliest_handover_at_ms=min(
                handover_times, default=replan_at_sim_time_ms
            ),
            latest_handover_at_ms=max(
                handover_times, default=replan_at_sim_time_ms
            ),
            handover_points=points,
            robot_overrides=overrides,
            preserved_edge_reservations=preserved_edges,
            preserved_node_reservations=preserved_nodes,
            preserved_station_reservations=preserved_stations,
            completed_task_bases=sorted(completed_bases),
            locked_task_bases=sorted(locked_bases),
        )


class RollingHorizonReplanService:
    """Conservative replan: freeze current steps, replan remaining logical work plus new work."""

    def __init__(
        self,
        *,
        store: SimulationPlanStore | None = None,
        runner: Callable[[AutoMissionRequest], Any] | None = None,
        repository: Any | None = None,
    ) -> None:
        self.store = store or SimulationPlanStore()
        self.builder = SimulationPlanBuilder()
        self.runner = runner
        self.repository = repository

    @staticmethod
    def _merge_runtime_overrides(
        snapshot: ReplanExecutionSnapshot,
        explicit: RuntimePlanningOverrides,
        activation_at_ms: int | None = None,
    ) -> RuntimePlanningOverrides:
        """Merge plan-derived availability with an optional deviation snapshot.

        Plan projection is authoritative for normal browser playback.  Explicit
        request overrides win only for robots the caller says have deviated
        (fault, manual relocation, delayed service, and similar cases).
        """

        activation_at_ms = max(
            snapshot.replan_at_sim_time_ms,
            activation_at_ms or snapshot.replan_at_sim_time_ms,
        )
        robot_by_id = {
            value.robot_id: value.model_copy(
                update={"sim_time_ms": max(value.sim_time_ms, activation_at_ms)}
            )
            for value in snapshot.robot_overrides
        }
        robot_by_id.update(
            {
                value.robot_id: value.model_copy(
                    update={"sim_time_ms": max(value.sim_time_ms, activation_at_ms)}
                )
                for value in explicit.robot_states
            }
        )
        deviated_robot_ids = {
            value.robot_id
            for value in explicit.robot_states
            if value.status in {"fault", "offline"}
        }
        preserved_edges = [
            value
            for value in snapshot.preserved_edge_reservations
            if value.robot_id not in deviated_robot_ids
        ]
        preserved_nodes = [
            value
            for value in snapshot.preserved_node_reservations
            if value.robot_id not in deviated_robot_ids
        ]
        preserved_stations = [
            value
            for value in snapshot.preserved_station_reservations
            if value.mobile_robot_id not in deviated_robot_ids
        ]
        # Explicit reservations are useful for simulator fault/obstacle tests
        # and are appended after plan-derived reservations.
        preserved_edges.extend(explicit.preserved_edge_reservations)
        preserved_nodes.extend(explicit.preserved_node_reservations)
        preserved_stations.extend(explicit.preserved_station_reservations)
        relocate_ids = sorted(
            set(snapshot_robot.robot_id for snapshot_robot in snapshot.robot_overrides)
            | set(explicit.relocate_idle_robot_ids)
        )
        return RuntimePlanningOverrides(
            robot_states=sorted(robot_by_id.values(), key=lambda value: value.robot_id),
            preserved_edge_reservations=preserved_edges,
            preserved_node_reservations=preserved_nodes,
            preserved_station_reservations=preserved_stations,
            source_plan_id=snapshot.source_plan_id,
            planning_horizon_start_ms=max(
                activation_at_ms,
                explicit.planning_horizon_start_ms,
            ),
            relocate_idle_robot_ids=relocate_ids,
        )

    def replan(self, request: ReplanMissionRequest) -> SimulationPlanResponse:
        active, prior_result = self.store.load(request.active_plan_id)
        if (
            request.active_plan_version is not None
            and request.active_plan_version != active.plan_version
        ):
            raise ValueError(
                "STALE_PLAN_VERSION: "
                f"requested={request.active_plan_version};active={active.plan_version}"
            )
        if active.status != "READY":
            raise ValueError(
                f"ACTIVE_PLAN_NOT_READY: {active.plan_id} status={active.status}"
            )
        if request.mission.warehouse_id != active.warehouse_id:
            raise ValueError(
                "Replan mission warehouse_id must match the active plan warehouse_id."
            )
        snapshot = RuntimeExecutionSnapshotBuilder().build(
            active,
            request.replan_at_sim_time_ms,
            prior_result,
            repository=self.repository,
        )
        completed_bases = set(snapshot.completed_task_bases)
        locked_bases = set(snapshot.locked_task_bases)
        new_mission = request.mission
        events = list(new_mission.events)
        known = {
            value.order_id or value.inbound_id or value.robot_id
            for value in events
            if value.order_id or value.inbound_id or value.robot_id
        }
        known.update(
            re.findall(
                r"\b(?:ORD|IN)-[A-Za-z0-9_-]+\b",
                new_mission.user_command or "",
            )
        )
        for operation in active.logical_operations:
            operation_bases = set(operation.task_ids)
            if operation_bases and operation_bases <= completed_bases:
                continue
            if operation_bases & locked_bases:
                # Pickup has started.  That physical cycle remains committed to
                # the old robot until the per-robot handover point.
                continue
            if operation.operation_id in known:
                continue
            event_type = {
                "OUTBOUND_ORDER": "new_order",
                "INBOUND_ITEM": "inbound_item_arrived",
                "RECOVERY": "robot_recovery_requested",
            }[operation.operation_type]
            events.append(
                EventInput(
                    type=event_type,
                    order_id=(
                        operation.operation_id
                        if event_type == "new_order"
                        else None
                    ),
                    inbound_id=(
                        operation.operation_id
                        if event_type == "inbound_item_arrived"
                        else None
                    ),
                    robot_id=(
                        operation.operation_id
                        if event_type == "robot_recovery_requested"
                        else None
                    ),
                    payload={"replan_from_plan_id": active.plan_id},
                )
            )
        has_command = bool((new_mission.user_command or "").strip())
        if events and has_command:
            mode = "mixed"
        elif has_command:
            mode = "human_command"
        else:
            mode = "event_driven"

        activation_at_ms = (
            snapshot.latest_handover_at_ms
            if request.activation_policy == "ALL_ROBOTS_READY"
            else request.replan_at_sim_time_ms
        )
        runtime_overrides = self._merge_runtime_overrides(
            snapshot,
            new_mission.runtime_overrides,
            activation_at_ms,
        )
        combined = new_mission.model_copy(
            update={
                "warehouse_id": active.warehouse_id,
                "simulation_id": active.simulation_id,
                "request_mode": mode,
                "events": events,
                "runtime_overrides": runtime_overrides,
            }
        )
        if self.runner is not None:
            result = self.runner(combined)
        else:
            from app.services.orchestration_service import OrchestrationService

            result = OrchestrationService().run(
                combined,
                repository=self.repository,
            )

        plan = self.builder.build(
            result,
            plan_version=active.plan_version + 1,
            base_plan_id=active.plan_id,
            plan_start_sim_time_ms=activation_at_ms,
            effective_from_sim_time_ms=activation_at_ms,
            schedule_times_are_absolute=True,
            plan_kind="REPLAN",
            handover_points=snapshot.handover_points,
            replan_reason=request.reason,
            replan_requested_at_ms=request.replan_at_sim_time_ms,
            repository=self.repository,
        )
        evaluation = None
        if plan:
            ReplanTimelineValidator.validate(
                plan, activation_at_ms
            )
            if request.activation_policy == "ALL_ROBOTS_READY":
                # The Spring simulator owns the activation barrier. Keep the
                # old plan eligible until BE verifies the real handover state.
                self.store.save(active, prior_result)
            else:
                superseded = active.model_copy(update={"status": "SUPERSEDED"})
                self.store.save(superseded, prior_result)
            self.store.save(plan, result)
        from app.services.planning_evaluation_service import (
            PlanningEvaluationCaptureService,
        )
        evaluation = PlanningEvaluationCaptureService().capture(
            raw_request=request,
            internal_request=combined,
            result=result,
            request_kind="REPLAN",
            plan=plan,
            source_plan_id=active.plan_id,
        )
        return SimulationPlanResponse(
            status=result.status,
            warehouse_id=getattr(result, "warehouse_id", active.warehouse_id),
            simulation_id=result.simulation_id,
            request_mode=getattr(result, "request_mode", combined.request_mode),
            final_route=(
                result.orchestration_plan.formulation_route
                if getattr(result, "orchestration_plan", None)
                else None
            ),
            effective_planning_mode=getattr(
                result, "effective_planning_mode", combined.planning_mode
            ),
            planning_mode_source=getattr(result, "planning_mode_source", None),
            router_llm_executed=any(
                value.node_name == "request_router_llm" and value.llm_used
                for value in getattr(result, "node_execution_log", [])
            ),
            plan=plan,
            evaluation_id=evaluation.evaluation_id if evaluation else None,
            frontend_summary=getattr(result, "frontend_summary", None),
            pending_human_interaction=result.pending_human_interaction,
            input_rejection=getattr(result, "input_rejection", None),
            workflow_hold=getattr(result, "workflow_hold", None),
            errors=result.errors,
        )

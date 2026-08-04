"""Pure mission validation from one immutable warehouse snapshot.

The policy stage validates business facts and preserves all feasible rack and
robot candidates.  It deliberately does not choose the final rack, robot, or
route; the traffic-aware option evaluator performs that numerical decision.
"""
from __future__ import annotations

from app.domain.schemas import (
    CandidateRobot,
    ContextSnapshot,
    EventDisposition,
    EventInput,
    FulfillmentCandidate,
    InventoryContext,
    MapContext,
    MissionSpec,
    PolicyCheck,
    PolicyValidationResult,
    PolicyViolation,
    RobotRuntimeContext,
    ValidatedTask,
)
from app.services.graph_service import DirectedGraphService
from app.services.rack_access_service import reachable_access_nodes


class MissionPolicyService:
    """Validate one mission without re-reading any repository."""

    def validate(
        self,
        *,
        mission: MissionSpec,
        snapshot: ContextSnapshot,
        inventory: InventoryContext,
        map_context: MapContext,
        robots: RobotRuntimeContext,
        graph_arcs: list[dict],
        events: list[EventInput],
    ) -> PolicyValidationResult:
        """Return business-valid candidates and directly materialized recovery tasks."""

        graph = DirectedGraphService(graph_arcs)
        checks: list[PolicyCheck] = []
        violations: list[PolicyViolation] = []
        fulfillment_candidates: list[FulfillmentCandidate] = []
        validated_tasks: list[ValidatedTask] = []
        excluded = set(mission.excluded_robot_ids)
        eligible_runtime = {
            robot.robot_id: robot
            for robot in robots.robots
            if robot.robot_id in robots.candidate_robot_ids and robot.robot_id not in excluded
        }
        if excluded:
            checks.append(
                PolicyCheck(
                    check_type="ROBOT_EXCLUSION_APPLIED",
                    status="pass",
                    target=",".join(sorted(excluded)),
                    detail={"excluded_robot_ids": sorted(excluded)},
                )
            )

        for index, request in enumerate(mission.task_requests, start=1):
            if request.request_type == "loaded_transfer":
                robot = next((value for value in robots.robots if value.robot_id == request.fixed_robot_id), None)
                if robot is None:
                    violations.append(
                        PolicyViolation(
                            code="fixed_robot_missing",
                            message="Recovery robot is not present in the snapshot.",
                        )
                    )
                    continue
                if not graph.reachable(robot.current_node, request.delivery_node):
                    violations.append(
                        PolicyViolation(
                            code="recovery_target_unreachable",
                            message="Recovery destination is unreachable.",
                        )
                    )
                    continue
                validated_tasks.append(
                    ValidatedTask(
                        task_id=f"RECOVERY-{index:03d}",
                        task_type="loaded_transfer",
                        pickup_node=robot.current_node,
                        delivery_node=request.delivery_node,
                        demand=request.requested_qty,
                        priority=request.priority,
                        fixed_robot_id=robot.robot_id,
                    )
                )
                checks.append(
                    PolicyCheck(
                        check_type="RECOVERY_ROUTE_AVAILABLE",
                        status="pass",
                        target=robot.robot_id,
                        detail={"destination": request.delivery_node},
                    )
                )
                continue

            need = next((value for value in inventory.task_needs if value.order_id == request.order_id), None)
            if need is None:
                violations.append(
                    PolicyViolation(
                        code="order_context_missing",
                        message=f"Order {request.order_id} is absent from InventoryContext.",
                    )
                )
                continue
            order_ok = (
                need.order_status == "pending"
                and need.item_id == request.item_id
                and need.required_qty == request.requested_qty
                and need.delivery_node == request.delivery_node
            )
            checks.append(
                PolicyCheck(
                    check_type="ORDER_CONTEXT_MATCH",
                    status="pass" if order_ok else "fail",
                    target=need.order_id,
                    detail={
                        "status": need.order_status,
                        "item_id": need.item_id,
                        "required_qty": need.required_qty,
                        "delivery_node": need.delivery_node,
                    },
                )
            )
            if not order_ok:
                violations.append(
                    PolicyViolation(
                        code="mission_order_mismatch",
                        message=f"Mission task does not match order {need.order_id} in the snapshot.",
                        repairable=True,
                    )
                )
                continue

            stock_candidates = [
                stock
                for stock in inventory.candidate_stocks
                if stock.item_id == need.item_id and stock.available_qty >= need.required_qty
            ]
            accepted = 0
            for stock in stock_candidates:
                feasible_access_nodes = reachable_access_nodes(
                    graph,
                    access_node_ids=stock.access_node_ids,
                    source_nodes=[
                        robot.current_node
                        for robot in eligible_runtime.values()
                        if robot.capacity_units >= need.required_qty
                    ],
                    target_node=need.delivery_node,
                )
                if not feasible_access_nodes:
                    continue
                fulfillment_candidates.append(
                    FulfillmentCandidate(
                        order_id=need.order_id,
                        item_id=need.item_id,
                        required_qty=need.required_qty,
                        delivery_node=need.delivery_node,
                        priority=need.priority,
                        stock_id=stock.stock_id,
                        rack_id=stock.rack_id,
                        access_node_ids=feasible_access_nodes,
                        rack_level=stock.rack_level,
                        available_qty=stock.available_qty,
                    )
                )
                accepted += 1
            checks.append(
                PolicyCheck(
                    check_type="AVAILABLE_RACK_CANDIDATES",
                    status="pass" if accepted else "fail",
                    target=need.order_id,
                    detail={
                        "required_qty": need.required_qty,
                        "candidate_count": accepted,
                        "candidate_rack_ids": [
                            value.rack_id
                            for value in fulfillment_candidates
                            if value.order_id == need.order_id
                        ],
                    },
                )
            )
            if accepted == 0:
                violations.append(
                    PolicyViolation(
                        code="no_reachable_stock_location",
                        message=(
                            f"No reachable rack level can supply {need.required_qty} unit(s) "
                            f"of {need.item_id}."
                        ),
                    )
                )

        task_demands = [candidate.required_qty for candidate in fulfillment_candidates] + [
            task.demand for task in validated_tasks
        ]
        required_capacity = max(task_demands + [1])
        fixed_robot_ids = {task.fixed_robot_id for task in validated_tasks if task.fixed_robot_id}
        robot_pool = dict(eligible_runtime)
        for robot in robots.robots:
            if robot.robot_id in fixed_robot_ids:
                robot_pool[robot.robot_id] = robot
        candidate_robots: list[CandidateRobot] = []
        for robot in robot_pool.values():
            can_serve_candidate = any(
                robot.capacity_units >= candidate.required_qty
                and any(
                    graph.reachable(robot.current_node, access_node_id)
                    for access_node_id in candidate.access_node_ids
                )
                for candidate in fulfillment_candidates
            )
            can_serve_fixed = any(
                task.fixed_robot_id == robot.robot_id and robot.capacity_units >= task.demand
                for task in validated_tasks
            )
            if fulfillment_candidates or validated_tasks:
                if not (can_serve_candidate or can_serve_fixed):
                    continue
            candidate_robots.append(
                CandidateRobot(
                    robot_id=robot.robot_id,
                    start_node=robot.current_node,
                    home_node=robot.home_node,
                    capacity_units=robot.capacity_units,
                    battery_pct=robot.battery_pct,
                    available_at_ms=robot.sim_time_ms,
                )
            )
        if (fulfillment_candidates or validated_tasks) and not candidate_robots:
            violations.append(
                PolicyViolation(
                    code="no_eligible_robot",
                    message="No eligible robot can reach and carry the policy-approved work.",
                )
            )
        checks.append(
            PolicyCheck(
                check_type="ROBOT_ELIGIBILITY_AND_CAPACITY",
                status="pass" if candidate_robots else "fail",
                target="candidate_robots",
                detail={
                    "max_task_capacity": required_capacity,
                    "candidate_robot_ids": [robot.robot_id for robot in candidate_robots],
                },
            )
        )

        dispositions: list[EventDisposition] = []
        for event in events:
            entity = event.order_id or event.edge_id or event.robot_id or event.node_id
            if event.type == "new_order":
                resolution = "TASK_CREATED" if fulfillment_candidates else "OBSERVATION_ONLY"
                reason = "The order produced one or more policy-approved fulfillment candidates."
            elif event.type in {"edge_congested", "edge_occupied", "edge_reserved", "edge_blocked"}:
                resolution = "CONSTRAINT_APPLIED"
                reason = "The edge runtime state was applied to cost or traffic scheduling."
            else:
                resolution = "OBSERVATION_ONLY"
                reason = "The event was retained as context without creating a physical task."
            dispositions.append(
                EventDisposition(
                    event_type=event.type,
                    entity_id=entity,
                    resolution=resolution,
                    reason=reason,
                )
            )

        if violations:
            status = "repairable" if all(value.repairable for value in violations) else "fail"
        elif (fulfillment_candidates or validated_tasks) and candidate_robots:
            status = "pass"
        else:
            status = "fail"
            violations.append(
                PolicyViolation(
                    code="empty_materialization",
                    message="Mission validation produced no executable or candidate work.",
                )
            )
        return PolicyValidationResult(
            status=status,
            snapshot_id=snapshot.snapshot_id,
            map_constraints=map_context.map_constraints,
            validated_tasks=validated_tasks,
            fulfillment_candidates=fulfillment_candidates,
            candidate_robots=candidate_robots,
            stock_allocations=[],
            event_dispositions=dispositions,
            checks=checks,
            violations=violations,
        )

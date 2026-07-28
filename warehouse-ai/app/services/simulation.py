import math
from typing import Any

from app.models import (
    AtomicTask,
    CollisionFreePlan,
    CuOptPlan,
    SimulationIssue,
    SimulationResult,
)
from app.services.routing import active_edges, active_node_ids
from app.services.energy_reconciliation import calculate_route_battery_metrics
from app.time_utils import planning_reference_time, task_tardiness_steps
from app.services.scheduling import planned_at, relative_time_step


def _issue(
    issues: list[SimulationIssue],
    code: str,
    message: str,
    **details: Any,
) -> None:
    issues.append(SimulationIssue(code=code, message=message, **details))


def _task_route_completion_steps(
    collision_plan: CollisionFreePlan,
) -> dict[str, int]:
    """Get task-level completion steps emitted by routing when available."""

    metadata_values = collision_plan.metadata.get("task_completion_steps", {})
    completion_steps = (
        {str(task_id): int(step) for task_id, step in metadata_values.items()}
        if isinstance(metadata_values, dict)
        else {}
    )
    for route in collision_plan.routes:
        if len(route.task_ids) == 1 and route.waypoints:
            completion_steps.setdefault(
                route.task_ids[0], route.waypoints[-1].time_step
            )
    return completion_steps


def simulate_plan(
    collision_plan: CollisionFreePlan,
    cuopt_plan: CuOptPlan,
    problem: dict[str, Any] | None = None,
    *,
    include_timeline: bool = True,
) -> SimulationResult:
    problem = problem or {}
    issues: list[SimulationIssue] = []
    warnings: list[str] = []
    vertex_seen: dict[tuple[int, int], str] = {}
    edge_seen: dict[tuple[int, int, int], str] = {}
    routed_tasks: set[str] = set()
    route_by_robot = {route.robot_id: route for route in collision_plan.routes}
    valid_nodes = active_node_ids(problem) if problem.get("nodes") else set()
    has_topology = bool(problem.get("edges"))
    valid_edges: set[tuple[int, int]] = set()
    for edge in active_edges(problem) if problem.get("edges") else []:
        start = int(edge["from_node"])
        target = int(edge["to_node"])
        valid_edges.add((start, target))
        if str(edge.get("direction", "ONE_WAY")).upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            valid_edges.add((target, start))

    timeline: list[dict[str, Any]] = []
    for route in collision_plan.routes:
        routed_tasks.update(route.task_ids)
        if not route.waypoints:
            _issue(
                issues,
                "EMPTY_ROUTE",
                f"{route.robot_id} 경로가 비었습니다.",
                robot_ids=[route.robot_id],
                task_ids=route.task_ids,
            )
            continue

        if len(route.waypoints) == 1:
            waypoint = route.waypoints[0]
            key = (waypoint.node_id, waypoint.time_step)
            other = vertex_seen.get(key)
            if other and other != route.robot_id:
                _issue(
                    issues,
                    "VERTEX_CONFLICT",
                    f"노드 {waypoint.node_id}, t={waypoint.time_step} 충돌",
                    robot_ids=[other, route.robot_id],
                    task_ids=route.task_ids,
                    node_ids=[waypoint.node_id],
                    time_steps=[waypoint.time_step],
                )
            vertex_seen[key] = route.robot_id

        for waypoint in route.waypoints:
            if valid_nodes and waypoint.node_id not in valid_nodes:
                _issue(
                    issues,
                    "INVALID_OR_CLOSED_NODE",
                    f"{route.robot_id}가 사용할 수 없는 노드 {waypoint.node_id}를 사용합니다.",
                    robot_ids=[route.robot_id],
                    task_ids=route.task_ids,
                    node_ids=[waypoint.node_id],
                    time_steps=[waypoint.time_step],
                )
            if include_timeline:
                timeline.append(
                    {
                        "time_step": waypoint.time_step,
                        "robot_id": route.robot_id,
                        "event": waypoint.action,
                        "node_id": waypoint.node_id,
                    }
                )

        for left, right in zip(route.waypoints, route.waypoints[1:]):
            if right.time_step <= left.time_step:
                _issue(
                    issues,
                    "NON_MONOTONIC_TIME",
                    f"{route.robot_id} 경로 시간이 증가하지 않습니다.",
                    robot_ids=[route.robot_id],
                    time_steps=[left.time_step, right.time_step],
                )
                continue
            if left.node_id != right.node_id and has_topology and (
                left.node_id,
                right.node_id,
            ) not in valid_edges:
                _issue(
                    issues,
                    "DISCONNECTED_OR_CLOSED_EDGE",
                    f"연결되지 않았거나 폐쇄된 간선 {left.node_id}→{right.node_id}를 사용합니다.",
                    robot_ids=[route.robot_id],
                    task_ids=route.task_ids,
                    node_ids=[left.node_id, right.node_id],
                    time_steps=[left.time_step],
                )

            occupied_vertices = (
                range(left.time_step, right.time_step + 1)
                if left.node_id == right.node_id
                else (left.time_step, right.time_step)
            )
            for time_step in occupied_vertices:
                node_id = left.node_id if time_step < right.time_step else right.node_id
                key = (node_id, time_step)
                other = vertex_seen.get(key)
                if other and other != route.robot_id:
                    _issue(
                        issues,
                        "VERTEX_CONFLICT",
                        f"노드 {node_id}, t={time_step} 충돌",
                        robot_ids=[other, route.robot_id],
                        task_ids=route.task_ids,
                        node_ids=[node_id],
                        time_steps=[time_step],
                    )
                vertex_seen[key] = route.robot_id

            if left.node_id != right.node_id:
                for time_step in range(left.time_step, right.time_step):
                    reverse = (right.node_id, left.node_id, time_step)
                    other = edge_seen.get(reverse)
                    if other and other != route.robot_id:
                        _issue(
                            issues,
                            "EDGE_SWAP_CONFLICT",
                            f"{left.node_id}↔{right.node_id}, t={time_step} 반대 방향 충돌",
                            robot_ids=[other, route.robot_id],
                            task_ids=route.task_ids,
                            node_ids=[left.node_id, right.node_id],
                            time_steps=[time_step],
                        )
                    edge_seen[(left.node_id, right.node_id, time_step)] = route.robot_id

    assignments_by_task: dict[str, list[Any]] = {}
    for assignment in cuopt_plan.scheduled_tasks:
        assignments_by_task.setdefault(assignment.task_id, []).append(assignment)
    for task_id, rows in assignments_by_task.items():
        if len(rows) > 1:
            _issue(
                issues,
                "DUPLICATE_TASK_ASSIGNMENT",
                f"작업 {task_id}가 여러 번 배정되었습니다.",
                robot_ids=sorted({row.robot_id for row in rows}),
                task_ids=[task_id],
            )

    expected_tasks = {task.task_id for task in cuopt_plan.scheduled_tasks}
    missing = sorted(expected_tasks - routed_tasks)
    if missing:
        _issue(
            issues,
            "TASK_ROUTE_MISSING",
            f"경로가 없는 작업: {missing}",
            task_ids=missing,
        )
    if cuopt_plan.unassigned_task_ids:
        _issue(
            issues,
            "UNASSIGNED_TASKS",
            f"미배정 작업: {cuopt_plan.unassigned_task_ids}",
            task_ids=cuopt_plan.unassigned_task_ids,
        )

    task_models = {
        task.task_id: task
        for task in (
            AtomicTask.model_validate(row) for row in problem.get("tasks", [])
        )
    }
    schedule = {task.task_id: task for task in cuopt_plan.scheduled_tasks}
    route_completion_steps = _task_route_completion_steps(collision_plan)
    tardiness_steps = 0
    tardiness_by_task: dict[str, int] = {}
    tardiness_by_task_seconds: dict[str, int] = {}
    reference_time = (
        planning_reference_time(problem)
        if problem.get("reference_time") or problem.get("captured_at")
        else None
    )
    same_robot_assignments: dict[str, str] = {}

    for task_id, assignment in schedule.items():
        route_end_step = route_completion_steps.get(task_id)
        if route_end_step is None:
            continue
        if assignment.end_time_step < route_end_step:
            _issue(
                issues,
                "ROUTE_SCHEDULE_TIME_MISMATCH",
                (
                    f"{task_id} schedule ends at step {assignment.end_time_step}, "
                    f"before the routed arrival at step {route_end_step}."
                ),
                robot_ids=[assignment.robot_id],
                task_ids=[task_id],
                time_steps=[assignment.end_time_step, route_end_step],
            )
        if reference_time and assignment.planned_end_at is not None:
            route_completion_at = planned_at(
                reference_time,
                route_end_step,
                collision_plan.time_step_seconds,
            )
            if assignment.planned_end_at < route_completion_at:
                _issue(
                    issues,
                    "ROUTE_COMPLETION_TIME_MISMATCH",
                    (
                        f"{task_id} planned_end_at is before its routed "
                        "completion time."
                    ),
                    robot_ids=[assignment.robot_id],
                    task_ids=[task_id],
                    time_steps=[assignment.end_time_step, route_end_step],
                )

    for task_id, task in task_models.items():
        assignment = schedule.get(task_id)
        if assignment is None:
            continue
        route = route_by_robot.get(assignment.robot_id)
        route_nodes = {waypoint.node_id for waypoint in route.waypoints} if route else set()
        if assignment.source_node not in route_nodes or assignment.target_node not in route_nodes:
            _issue(
                issues,
                "TASK_ENDPOINT_NOT_REACHED",
                f"작업 {task_id}의 출발지 또는 목적지에 도달하지 못했습니다.",
                robot_ids=[assignment.robot_id],
                task_ids=[task_id],
                node_ids=[assignment.source_node, assignment.target_node],
            )
        dependency_lag_steps = {
            f"{row.predecessor_work_id}:move": math.ceil(
                row.lag_seconds / collision_plan.time_step_seconds
            )
            for row in task.dependencies
            if row.successor_work_id == task.work_id
        }
        for predecessor in task.predecessors:
            predecessor_assignment = schedule.get(predecessor)
            if predecessor_assignment and (
                predecessor_assignment.end_time_step
                + dependency_lag_steps.get(predecessor, 0)
                > assignment.start_time_step
            ):
                _issue(
                    issues,
                    "PRECEDENCE_VIOLATION",
                    f"{predecessor} 완료 전에 {task_id}가 시작됩니다.",
                    task_ids=[predecessor, task_id],
                )
        if reference_time and task.earliest_start is not None:
            earliest_step = relative_time_step(
                task.earliest_start,
                reference_time,
                collision_plan.time_step_seconds,
                round_up=True,
            )
            if assignment.start_time_step < earliest_step:
                _issue(
                    issues,
                    "EARLIEST_START_VIOLATION",
                    f"작업 {task_id}가 earliest_start 전에 시작됩니다.",
                    task_ids=[task_id],
                    time_steps=[assignment.start_time_step, earliest_step],
                )
        if reference_time and task.latest_finish is not None:
            latest_step = relative_time_step(
                task.latest_finish,
                reference_time,
                collision_plan.time_step_seconds,
                round_up=False,
            )
            if (
                task.time_constraint_type == "HARD_WINDOW"
                and assignment.end_time_step > latest_step
            ):
                _issue(
                    issues,
                    "HARD_WINDOW_VIOLATION",
                    f"작업 {task_id}가 hard window 종료 후 완료됩니다.",
                    task_ids=[task_id],
                    time_steps=[assignment.end_time_step, latest_step],
                )
        if task.same_robot_group:
            group_robot = same_robot_assignments.setdefault(
                task.same_robot_group, assignment.robot_id
            )
            if group_robot != assignment.robot_id:
                _issue(
                    issues,
                    "SAME_ROBOT_GROUP_VIOLATION",
                    f"동일 로봇 그룹 {task.same_robot_group}의 배정이 다릅니다.",
                    robot_ids=[group_robot, assignment.robot_id],
                    task_ids=[task_id],
                )
        if task.deadline and reference_time:
            final_end_step = route_completion_steps.get(
                task_id,
                assignment.end_time_step,
            )
            late = task_tardiness_steps(
                deadline=task.deadline,
                reference_time=reference_time,
                task_end_time_step=final_end_step,
                time_step_seconds=collision_plan.time_step_seconds,
            )
            if late:
                tardiness_steps += late
                tardiness_by_task[task_id] = late
                late_seconds = late * collision_plan.time_step_seconds
                tardiness_by_task_seconds[task_id] = late_seconds
                warnings.append(
                    f"작업 {task_id}의 예상 납기 지연은 {late_seconds}초입니다."
                )

    robots = {str(row["robot_id"]): row for row in problem.get("robots", [])}
    min_battery = float(problem.get("min_robot_battery") or 0.0)
    battery_by_robot = calculate_route_battery_metrics(
        cuopt_plan,
        collision_plan,
        problem,
    )
    for route in collision_plan.routes:
        robot = robots.get(route.robot_id)
        if robots and not robot:
            _issue(
                issues,
                "UNKNOWN_ROBOT",
                f"Snapshot에 없는 로봇 {route.robot_id}가 배정되었습니다.",
                robot_ids=[route.robot_id],
            )
            continue
        metric = battery_by_robot.get(route.robot_id)
        if metric is None:
            continue
        for index, battery_at_charger in enumerate(
            metric.get("battery_at_chargers", [])
        ):
            if float(battery_at_charger) < 0:
                task_ids = metric.get("charge_task_ids", [])
                task_id = task_ids[index] if index < len(task_ids) else None
                _issue(
                    issues,
                    "BATTERY_UNREACHABLE",
                    (
                        f"{route.robot_id} battery becomes negative before "
                        f"{task_id or 'CHARGE'}"
                    ),
                    robot_ids=[route.robot_id],
                    task_ids=[task_id] if task_id else [],
                )
        remaining = float(metric.get("final_battery") or 0.0)
        if remaining < min_battery - 1e-6:
            _issue(
                issues,
                "BATTERY_CONSTRAINT_VIOLATION",
                (
                    f"{route.robot_id}의 최종 라우팅 거리 기준 예상 잔여 "
                    f"배터리 {remaining:.2f}%가 기준보다 낮습니다."
                ),
                robot_ids=[route.robot_id],
                task_ids=route.task_ids,
            )

    operation_type_by_work: dict[str, str] = {}
    for row in problem.get("inventory_operations", []):
        operation_ref = str(row.get("work_id") or row.get("operation_id") or "")
        operation_type = str(row.get("operation_type") or "").upper()
        if operation_ref and operation_type in {"INBOUND", "OUTBOUND"}:
            operation_type_by_work[operation_ref] = operation_type

    requested_inventory: dict[str, int] = {}
    requested_task_ids: dict[str, list[str]] = {}
    for task in task_models.values():
        if (
            task.action != "PICK"
            or not task.item_id
            or task.inventory_transition_policy == "NO_STOCK_DELTA"
        ):
            continue
        operation_type = operation_type_by_work.get(str(task.work_id or ""))
        if operation_type == "INBOUND":
            # Inbound PICK means collecting goods from an inbound dock.  It
            # does not consume stock that already exists in a storage lot.
            continue
        allocated_quantity = sum(
            int(
                allocation.get("quantity_boxes")
                or allocation.get("quantity")
                or 0
            )
            for allocation in task.inventory_allocations
        )
        if allocated_quantity >= task.quantity:
            # FEFO allocations can include FUTURE_INBOUND lots.  Inventory
            # projection already proved they are available before this task.
            continue
        requested_inventory[task.item_id] = (
            requested_inventory.get(task.item_id, 0) + task.quantity
        )
        requested_task_ids.setdefault(task.item_id, []).append(task.task_id)

    available_inventory: dict[str, int] = {}
    for row in problem.get("inventory", []):
        item_id = str(row.get("item_id"))
        available_inventory[item_id] = available_inventory.get(item_id, 0) + int(
            row.get("available_quantity") or 0
        )
    for item_id, quantity in requested_inventory.items():
        if available_inventory and available_inventory.get(item_id, 0) < quantity:
            _issue(
                issues,
                "INSUFFICIENT_INVENTORY",
                f"{item_id} 재고가 {quantity}개 작업에 부족합니다.",
                task_ids=requested_task_ids.get(item_id, []),
            )

    conflict_codes = {"VERTEX_CONFLICT", "EDGE_SWAP_CONFLICT"}
    conflict_count = sum(issue.code in conflict_codes for issue in issues)
    makespan = max(
        (
            route.waypoints[-1].time_step
            for route in collision_plan.routes
            if route.waypoints
        ),
        default=0,
    )
    task_duration_steps = sum(
        max(0, task.end_time_step - task.start_time_step)
        for task in cuopt_plan.scheduled_tasks
    )
    schedule_completion_at = (
        planned_at(
            reference_time,
            makespan,
            collision_plan.time_step_seconds,
        ).isoformat()
        if reference_time
        else None
    )
    timeline.sort(
        key=lambda event: (
            event["time_step"],
            event["robot_id"],
            event["node_id"],
        )
    )
    valid = not issues
    error_messages = [issue.message for issue in issues]
    return SimulationResult(
        success=valid,
        valid=valid,
        status="SUCCESS" if valid else "FAILED",
        issues=issues,
        errors=error_messages,
        warnings=warnings,
        total_distance=collision_plan.total_distance,
        makespan=makespan,
        tardiness=tardiness_steps * collision_plan.time_step_seconds,
        robot_routes=[route.model_dump(mode="json") for route in collision_plan.routes],
        task_assignments=[
            task.model_dump(mode="json") for task in cuopt_plan.scheduled_tasks
        ],
        conflict_count=conflict_count,
        timeline=timeline,
        metrics={
            "robot_count": len(collision_plan.routes),
            "task_count": len(expected_tasks),
            "total_distance": collision_plan.total_distance,
            "makespan_time_steps": makespan,
            "makespan_seconds": makespan * collision_plan.time_step_seconds,
            "schedule_completion_at": schedule_completion_at,
            "active_work_duration_seconds": (
                task_duration_steps * collision_plan.time_step_seconds
            ),
            "elapsed_until_completion_seconds": (
                makespan * collision_plan.time_step_seconds
            ),
            "tardiness_seconds": tardiness_steps * collision_plan.time_step_seconds,
            "tardiness_by_task": tardiness_by_task,
            "tardiness_by_task_unit": "time_step",
            "tardiness_by_task_seconds": tardiness_by_task_seconds,
            "conflict_count": conflict_count,
            "time_step_seconds": collision_plan.time_step_seconds,
            "battery_by_robot": battery_by_robot,
        },
    )

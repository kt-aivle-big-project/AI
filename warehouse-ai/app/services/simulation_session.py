from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.models import AtomicTask, LotAllocation, RobotEvent, SimulationResult


def _event_time(
    captured_at: datetime,
    time_step: int,
    time_step_seconds: int,
) -> datetime:
    return captured_at + timedelta(seconds=time_step * time_step_seconds)


def _inventory_deltas(task: AtomicTask | None) -> list[dict[str, Any]]:
    if (
        task is None
        or task.action not in {"PICK", "MOVE"}
        or task.inventory_transition_policy == "NO_STOCK_DELTA"
    ):
        return []
    return [
        {
            "warehouse_item_id": str(allocation["warehouse_item_id"]),
            "quantity_delta": -int(allocation["quantity"]),
        }
        for allocation in task.inventory_allocations
    ]


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _future_inbound_lots(state: dict[str, Any]) -> list[LotAllocation]:
    """Return structured virtual lots selected by inventory feasibility.

    The projection owns the distinction between a current lot and a planned
    inbound lot.  Replay deliberately consumes that metadata rather than
    inferring it from an identifier prefix.
    """

    validation = (
        state.get("inventory_timeline_validation", {}).get("item_results")
        or state.get("inventory_feasibility", {}).get("item_results")
        or []
    )
    lots: list[LotAllocation] = []
    for item_result in validation:
        for raw in item_result.get("lot_allocations", []):
            allocation = LotAllocation.model_validate(raw)
            if allocation.source_type == "FUTURE_INBOUND":
                if allocation.available_at is None:
                    raise ValueError("예정 입고 allocation에 available_at이 필요합니다.")
                lots.append(allocation)
    return lots


def _simulation_plan_payload(
    state: dict[str, Any],
    simulation_id: str,
) -> dict[str, Any]:
    plan_version = str(state.get("plan_version") or "")
    reference_time = state.get("optimization_problem", {}).get("reference_time")
    return {
        "plan_version": plan_version,
        "command_id": state.get("command", {}).get("command_id"),
        "warehouse_id": state.get("command", {}).get("warehouse_id"),
        "simulation_id": simulation_id,
        "scope": state.get("scope") or {},
        "required_tasks": state.get("required_tasks") or [],
        "cuopt_plan": state.get("cuopt_plan") or {},
        "collision_plan": state.get("collision_plan") or {},
        "inventory_operations": state.get("inventory_operations") or [],
        "task_dependencies": (
            state.get("interpretation", {}).get("task_dependencies") or []
        ),
        "execution_task_dependencies": (
            state.get("cuopt_plan", {}).get("metadata", {}).get(
                "execution_task_dependencies", []
            )
        ),
        "scheduled_task_constraints": (
            state.get("interpretation", {}).get("scheduled_task_constraints") or []
        ),
        "ready_task_ids": state.get("ready_task_ids") or [],
        "waiting_task_ids": state.get("waiting_task_ids") or [],
        "blocked_task_ids": state.get("blocked_task_ids") or [],
        "resource_reservation_plan": state.get("resource_reservation_plan") or {},
        "robot_command_batches": state.get("robot_command_batches") or [],
        "reference_time": reference_time,
        "time_step_seconds": int(
            state.get("collision_plan", {}).get("time_step_seconds") or 1
        ),
        "execution_mode": "SIMULATE_ONLY",
        "base_plan_is_simulated": True,
        "candidate_plan": True,
    }


def replay_simulation_session(
    state: dict[str, Any],
    result: SimulationResult,
    redis_repository: Any,
) -> dict[str, Any]:
    """검증된 timeline을 simulation_id 전용 가상 상태에만 재생한다."""

    simulation_id = (
        state.get("simulation_id")
        or state.get("command", {}).get("simulation_id")
        or str(uuid4())
    )
    replan_attempt = max(
        int(state.get("replan_attempt") or 0),
        int(state.get("replan_count") or 0),
    )
    session_reset_for_replan = False
    if replan_attempt > 0:
        remover = getattr(redis_repository, "remove_simulation_state", None)
        if callable(remover):
            warehouse_id = int(
                state.get("command", {}).get("warehouse_id")
                or state.get("optimization_problem", {}).get("warehouse_id")
                or state.get("snapshot", {}).get("warehouse_id")
                or 0
            )
            # A replan replays the complete candidate plan. Reusing the prior
            # candidate's mutated virtual inventory would apply outbound deltas
            # twice (for example 20 -> 0 -> -20).
            remover(warehouse_id, simulation_id)
            session_reset_for_replan = True

    base_state = redis_repository.initialize_simulation_session(
        simulation_id,
        state["snapshot"],
    )
    plan_payload = _simulation_plan_payload(state, simulation_id)
    saver = getattr(redis_repository, "save_simulation_plan", None)
    if callable(saver):
        saver(simulation_id, plan_payload)
        # Persist the plan with both the immutable base and the mutable current
        # simulation state so PostgreSQL can later serve as a recovery source.
        base_state = redis_repository.simulation_snapshot(simulation_id)
    else:
        base_state = {**base_state, "active_plan": plan_payload}
        base_state["active_plan_version"] = plan_payload.get("plan_version")
        base_state["reference_time"] = plan_payload.get("reference_time")

    captured_at = _as_datetime(
        state.get("optimization_problem", {}).get("reference_time")
        or state["snapshot"]["captured_at"]
    )
    time_step_seconds = int(
        state.get("collision_plan", {}).get("time_step_seconds") or 1
    )
    tasks = {
        task.task_id: task
        for task in (
            AtomicTask.model_validate(raw)
            for raw in state.get("required_tasks", [])
        )
    }
    schedules = state.get("cuopt_plan", {}).get("scheduled_tasks", [])
    scheduled_by_robot: dict[str, list[dict[str, Any]]] = {}
    replay_queue: list[tuple[int, int, str, RobotEvent]] = []

    for schedule in schedules:
        robot_id = str(schedule["robot_id"])
        task_id = str(schedule["task_id"])
        work_id = schedule.get("work_id")
        scheduled_by_robot.setdefault(robot_id, []).append(schedule)
        start_step = int(schedule.get("start_time_step") or 0)
        end_step = int(schedule.get("end_time_step") or start_step)
        replay_queue.append(
            (
                start_step,
                0,
                task_id,
                RobotEvent(
                    event_id=f"{simulation_id}:{task_id}:started",
                    warehouse_id=int(state["command"]["warehouse_id"]),
                    robot_id=robot_id,
                    work_id=str(work_id) if work_id is not None else None,
                    task_id=task_id,
                    event_type="TASK_STARTED",
                    node_id=int(schedule["source_node"]),
                    occurred_at=_event_time(
                        captured_at,
                        start_step,
                        time_step_seconds,
                    ),
                    execution_context="SIMULATION",
                    simulation_id=simulation_id,
                ),
            )
        )
        replay_queue.append(
            (
                end_step,
                2,
                task_id,
                RobotEvent(
                    event_id=f"{simulation_id}:{task_id}:completed",
                    warehouse_id=int(state["command"]["warehouse_id"]),
                    robot_id=robot_id,
                    work_id=str(work_id) if work_id is not None else None,
                    task_id=task_id,
                    event_type="TASK_COMPLETED",
                    node_id=int(schedule["target_node"]),
                    occurred_at=_event_time(
                        captured_at,
                        end_step,
                        time_step_seconds,
                    ),
                    inventory_deltas=_inventory_deltas(tasks.get(task_id)),
                    execution_context="SIMULATION",
                    simulation_id=simulation_id,
                ),
            )
        )

    for route in state.get("collision_plan", {}).get("routes", []):
        robot_id = str(route["robot_id"])
        schedules_for_robot = scheduled_by_robot.get(robot_id, [])
        for waypoint in route.get("waypoints", []):
            time_step = int(waypoint["time_step"])
            matching = next(
                (
                    schedule
                    for schedule in schedules_for_robot
                    if int(schedule.get("start_time_step") or 0)
                    <= time_step
                    <= int(schedule.get("end_time_step") or 0)
                ),
                None,
            )
            replay_queue.append(
                (
                    time_step,
                    1,
                    f"{robot_id}:{time_step}",
                    RobotEvent(
                        event_id=(
                            f"{simulation_id}:{robot_id}:{time_step}:position"
                        ),
                        warehouse_id=int(state["command"]["warehouse_id"]),
                        robot_id=robot_id,
                        work_id=(
                            str(matching["work_id"])
                            if matching and matching.get("work_id") is not None
                            else None
                        ),
                        task_id=(
                            str(matching["task_id"]) if matching else None
                        ),
                        event_type="POSITION_UPDATED",
                        node_id=int(waypoint["node_id"]),
                        occurred_at=_event_time(
                            captured_at,
                            time_step,
                            time_step_seconds,
                        ),
                        execution_context="SIMULATION",
                        simulation_id=simulation_id,
                    ),
                )
            )

    future_lots: dict[str, dict[str, Any]] = {}
    for allocation in _future_inbound_lots(state):
        row = future_lots.setdefault(
            allocation.warehouse_item_id,
            {
                "warehouse_item_id": allocation.warehouse_item_id,
                "item_id": allocation.item_id,
                "lot_id": allocation.lot_id,
                "storage_node_id": allocation.storage_node_id,
                "available_at": allocation.available_at,
                "inbound_source_id": allocation.inbound_source_id,
                "quantity_boxes": 0,
            },
        )
        row["quantity_boxes"] += allocation.quantity_boxes

    inbound_event_keys: set[str] = set()
    for lot in future_lots.values():
        if not lot["item_id"]:
            raise ValueError("예정 입고 allocation에 item_id가 필요합니다.")
        available_time = lot["available_at"]
        available_step = max(
            0,
            int((available_time - captured_at).total_seconds() // time_step_seconds),
        )
        warehouse_item_id = str(lot["warehouse_item_id"])
        inbound_event_keys.add(warehouse_item_id)
        replay_queue.append(
            (
                available_step,
                -1,
                warehouse_item_id,
                RobotEvent(
                    event_id=f"{simulation_id}:{warehouse_item_id}:available",
                    warehouse_id=int(state["command"]["warehouse_id"]),
                    robot_id="INBOUND-SYSTEM",
                    task_id=warehouse_item_id,
                    event_type="INBOUND_AVAILABLE",
                    node_id=lot["storage_node_id"],
                    occurred_at=available_time,
                    payload={
                        "inbound_id": lot["inbound_source_id"],
                        "item_id": lot["item_id"],
                        "quantity_boxes": lot["quantity_boxes"],
                        "lot_id": lot["lot_id"],
                        "warehouse_item_id": warehouse_item_id,
                        "source_type": "FUTURE_INBOUND",
                    },
                    execution_context="SIMULATION",
                    simulation_id=simulation_id,
                ),
            )
        )

    for operation in state.get("inventory_operations", []):
        if str(operation.get("operation_type")) != "INBOUND":
            continue
        available_at = (
            operation.get("actual_available_at")
            or operation.get("expected_available_at")
        )
        if not available_at:
            continue
        available_time = _as_datetime(available_at)
        available_step = max(
            0,
            int((available_time - captured_at).total_seconds() // time_step_seconds),
        )
        operation_id = str(operation["operation_id"])
        warehouse_item_id = operation.get("warehouse_item_id")
        if warehouse_item_id and str(warehouse_item_id) in inbound_event_keys:
            continue
        replay_queue.append(
            (
                available_step,
                -1,
                operation_id,
                RobotEvent(
                    event_id=f"{simulation_id}:{operation_id}:available",
                    warehouse_id=int(state["command"]["warehouse_id"]),
                    robot_id="INBOUND-SYSTEM",
                    work_id=operation.get("work_id"),
                    task_id=operation_id,
                    event_type="INBOUND_AVAILABLE",
                    node_id=operation.get("storage_node_id"),
                    occurred_at=available_time,
                    payload={
                        "inbound_id": operation.get("order_id"),
                        "item_id": operation["item_id"],
                        "quantity_boxes": operation["quantity_boxes"],
                        "lot_id": operation.get("lot_id"),
                        "warehouse_item_id": warehouse_item_id,
                        "source_type": "FUTURE_INBOUND",
                    },
                    execution_context="SIMULATION",
                    simulation_id=simulation_id,
                ),
            )
        )
    current_state: dict[str, Any] | None = None
    for _, _, _, event in sorted(replay_queue, key=lambda row: row[:3]):
        current_state = redis_repository.update_simulation_from_event(event)
    if current_state is None:
        current_state = redis_repository.simulation_snapshot(simulation_id)

    return {
        "simulation_id": simulation_id,
        "base_state": base_state,
        "current_state": current_state,
        "checkpoint": str(current_state["checkpoint"]),
        "event_count": len(replay_queue),
        "session_reset_for_replan": session_reset_for_replan,
    }

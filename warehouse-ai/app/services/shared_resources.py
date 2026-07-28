from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.models import CollisionFreePlan, CuOptPlan, ScheduledTask
from app.time_utils import as_utc_datetime
from app.services.task_ordering import dependency_aware_robot_task_ids


RESOURCE_SCHEDULER_VERSION = "p16.5.9"
SERVICE_ACTIONS = {"PICK", "DROP"}
IDLE_NODE_TYPES = {
    "PARKING",
    "STAGING",
    "HOLDING",
    "CHARGER_WAITING_AREA",
    "ROBOT_PARKING",
}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _node_index(problem: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        int(row["node_id"]): dict(row)
        for row in problem.get("nodes", [])
        if row.get("node_id") is not None
    }


def _node_type(row: dict[str, Any]) -> str:
    return str(row.get("node_type") or row.get("type") or "UNKNOWN").upper()


def _capacity_descriptor(
    row: dict[str, Any],
    resource_type: str,
) -> tuple[int, str | None]:
    if resource_type == "CHARGER_SLOT":
        for key in ("charger_capacity", "service_capacity", "capacity"):
            if row.get(key) is not None:
                return _positive_int(row.get(key), 1), key
        return 1, None
    if resource_type == "SERVICE_NODE":
        for key in ("service_capacity", "capacity"):
            if row.get(key) is not None:
                return _positive_int(row.get(key), 1), key
        return 1, None
    node_type = _node_type(row)
    if node_type == "CHARGER_WAITING_AREA":
        for key in ("waiting_capacity", "idle_capacity", "parking_capacity"):
            if row.get(key) is not None:
                return _positive_int(row.get(key), 1), key
        return 1, None
    for key in ("parking_capacity", "idle_capacity", "waiting_capacity"):
        if row.get(key) is not None:
            return _positive_int(row.get(key), 1), key
    return 1, None


def _service_steps(
    task: ScheduledTask,
    row: dict[str, Any],
    *,
    time_step_seconds: int,
) -> int:
    if task.action == "CHARGE":
        seconds = int(task.charge_duration_seconds or 0)
        return max(1, math.ceil(seconds / time_step_seconds))
    seconds = _positive_int(row.get("service_duration_seconds"), time_step_seconds)
    return max(1, math.ceil(seconds / time_step_seconds))


def _resource_spec(
    task: ScheduledTask,
    nodes: dict[int, dict[str, Any]],
    *,
    time_step_seconds: int,
) -> dict[str, Any] | None:
    action = str(task.action).upper()
    if action == "CHARGE":
        resource_type = "CHARGER_SLOT"
    elif action in SERVICE_ACTIONS:
        resource_type = "SERVICE_NODE"
    else:
        return None
    node_id = int(task.target_node)
    row = nodes.get(node_id, {"node_id": node_id})
    capacity, capacity_source = _capacity_descriptor(row, resource_type)
    duration_steps = _service_steps(
        task,
        row,
        time_step_seconds=time_step_seconds,
    )
    return {
        "resource_type": resource_type,
        "node_id": node_id,
        "node_type": _node_type(row),
        "capacity": capacity,
        "capacity_source": capacity_source,
        "duration_steps": duration_steps,
        "service_duration_seconds": duration_steps * time_step_seconds,
    }


def _parse_reference_time(problem: dict[str, Any]) -> datetime | None:
    raw = problem.get("reference_time") or problem.get("captured_at")
    if not raw:
        return None
    try:
        return as_utc_datetime(raw, field_name="resource_reference_time")
    except (TypeError, ValueError):
        return None


def _reservation_id(
    resource_type: str,
    node_id: int,
    task_id: str,
    start_step: int,
    end_step: int,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"warehouse-resource:{resource_type}:{node_id}:{task_id}:{start_step}:{end_step}",
        )
    )


def _interval_overlaps(
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
) -> bool:
    return left_start < right_end and right_start < left_end


def _assign_slots(
    rows: list[dict[str, Any]],
    capacity: int,
) -> tuple[list[dict[str, Any]], bool]:
    lanes: list[list[tuple[int, int]]] = [[] for _ in range(max(1, capacity))]
    assigned: list[dict[str, Any]] = []
    for raw in sorted(
        rows,
        key=lambda row: (
            int(row["start_time_step"]),
            int(row["end_time_step"]),
            str(row["task_id"]),
        ),
    ):
        chosen: int | None = None
        for lane_index, intervals in enumerate(lanes):
            if all(
                not _interval_overlaps(
                    int(raw["start_time_step"]),
                    int(raw["end_time_step"]),
                    start,
                    end,
                )
                for start, end in intervals
            ):
                chosen = lane_index
                break
        if chosen is None:
            return assigned, False
        lanes[chosen].append(
            (int(raw["start_time_step"]), int(raw["end_time_step"]))
        )
        row = dict(raw)
        row["slot_index"] = chosen + 1
        assigned.append(row)
    return assigned, True


def _occupancy_is_free(
    occupancy: dict[int, list[str]],
    start_step: int,
    end_step: int,
    capacity: int,
) -> bool:
    return all(
        len(set(occupancy.get(step, []))) < capacity
        for step in range(start_step, end_step)
    )


def _reserve_occupancy(
    occupancy: dict[int, list[str]],
    *,
    start_step: int,
    end_step: int,
    task_id: str,
) -> None:
    for step in range(start_step, end_step):
        if task_id not in occupancy[step]:
            occupancy[step].append(task_id)


def _latest_finish_steps(problem: dict[str, Any], time_step_seconds: int) -> dict[str, int]:
    reference = _parse_reference_time(problem)
    if reference is None:
        return {}
    result: dict[str, int] = {}
    for row in problem.get("tasks", []):
        if str(row.get("time_constraint_type") or "").upper() != "HARD_WINDOW":
            continue
        raw = row.get("latest_finish") or row.get("deadline")
        if not raw:
            continue
        try:
            latest = as_utc_datetime(raw, field_name="resource_latest_finish")
        except (TypeError, ValueError):
            continue
        seconds = max(0.0, (latest - reference).total_seconds())
        result[str(row.get("task_id"))] = math.floor(seconds / time_step_seconds)
    return result


def schedule_shared_resources(
    problem: dict[str, Any],
    plan: CuOptPlan,
) -> tuple[CuOptPlan, dict[str, Any]]:
    """Serialize charger and service-node use before MAPF routing.

    cuOpt remains responsible for robot assignment and visit order. This local
    scheduler only delays already-assigned tasks so capacity-one or capacity-N
    shared resources have executable, non-overlapping service windows.
    """

    time_step_seconds = max(1, int(problem.get("time_step_seconds") or 5))
    nodes = _node_index(problem)
    tasks = [task.model_copy(deep=True) for task in plan.scheduled_tasks]
    by_id = {task.task_id: task for task in tasks}
    original_times = {
        task.task_id: (int(task.start_time_step), int(task.end_time_step))
        for task in tasks
    }
    fixed_task_ids = {str(value) for value in problem.get("fixed_task_ids", [])}
    frozen_by_task = {
        str(row.get("task_id")): bool(row.get("frozen"))
        for row in problem.get("tasks", [])
        if row.get("task_id")
    }
    frozen_task_ids = fixed_task_ids | {
        task_id for task_id, frozen in frozen_by_task.items() if frozen
    }
    hard_latest = _latest_finish_steps(problem, time_step_seconds)
    reference = _parse_reference_time(problem)
    dependencies = list(plan.metadata.get("execution_task_dependencies") or [])

    robot_order, order_errors = dependency_aware_robot_task_ids(
        tasks,
        dependencies,
    )
    robot_position = {
        task_id: index
        for _robot_id, rows in robot_order.items()
        for index, task_id in enumerate(rows)
    }

    adjustments: list[dict[str, Any]] = []
    errors: list[str] = list(order_errors)
    warning_codes: set[str] = set()

    def refresh_planned_times(task: ScheduledTask) -> None:
        if reference is None:
            return
        task.planned_start_at = reference + timedelta(
            seconds=int(task.start_time_step) * time_step_seconds
        )
        task.planned_end_at = reference + timedelta(
            seconds=int(task.end_time_step) * time_step_seconds
        )

    def shift_chain(task_id: str, delta: int, reason: str, node_id: int | None = None) -> bool:
        if delta <= 0:
            return True
        task = by_id[task_id]
        ordered = robot_order[str(task.robot_id)]
        start_index = robot_position[task_id]
        affected = ordered[start_index:]
        blocked = [value for value in affected if value in frozen_task_ids]
        if blocked:
            errors.append(
                "RESOURCE_CAPACITY_CONFLICT_WITH_FROZEN_TASK: "
                + ", ".join(blocked)
            )
            return False
        old_start = int(task.start_time_step)
        for affected_id in affected:
            row = by_id[affected_id]
            row.start_time_step = int(row.start_time_step) + delta
            row.end_time_step = int(row.end_time_step) + delta
            refresh_planned_times(row)
        adjustments.append(
            {
                "task_id": task_id,
                "robot_id": task.robot_id,
                "old_start_time_step": old_start,
                "new_start_time_step": int(task.start_time_step),
                "delay_steps": delta,
                "delay_seconds": delta * time_step_seconds,
                "reason": reason,
                "node_id": node_id,
                "affected_task_ids": list(affected),
            }
        )
        return True

    # Ensure each task contains enough dwell time for its configured resource
    # service duration. Extending a task shifts all downstream tasks on the
    # same robot, preserving the optimizer's visit order.
    for robot_id, ordered in robot_order.items():
        for task_id in ordered:
            task = by_id[task_id]
            spec = _resource_spec(
                task,
                nodes,
                time_step_seconds=time_step_seconds,
            )
            if spec is None:
                continue
            duration = int(task.end_time_step) - int(task.start_time_step)
            missing = max(0, int(spec["duration_steps"]) - duration)
            if missing <= 0:
                continue
            if task_id in frozen_task_ids:
                errors.append(
                    f"RESOURCE_SERVICE_DURATION_EXCEEDS_FROZEN_TASK: {task_id}"
                )
                continue
            task.end_time_step = int(task.end_time_step) + missing
            refresh_planned_times(task)
            later = ordered[robot_position[task_id] + 1 :]
            for later_id in later:
                later_task = by_id[later_id]
                if later_id in frozen_task_ids:
                    errors.append(
                        f"RESOURCE_SERVICE_DURATION_BLOCKED_BY_FROZEN_TASK: {later_id}"
                    )
                    break
                later_task.start_time_step = int(later_task.start_time_step) + missing
                later_task.end_time_step = int(later_task.end_time_step) + missing
                refresh_planned_times(later_task)
            adjustments.append(
                {
                    "task_id": task_id,
                    "robot_id": robot_id,
                    "old_end_time_step": int(task.end_time_step) - missing,
                    "new_end_time_step": int(task.end_time_step),
                    "delay_steps": missing,
                    "delay_seconds": missing * time_step_seconds,
                    "reason": "CONFIGURED_SERVICE_DURATION_EXTENSION",
                    "node_id": int(task.target_node),
                    "affected_task_ids": [task_id, *later],
                }
            )

    max_iterations = max(20, len(tasks) * 12)
    final_rows: list[dict[str, Any]] = []
    iterations = 0
    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        changed = False

        # Preserve the assigned robot's task order.
        for robot_id, ordered in robot_order.items():
            previous: ScheduledTask | None = None
            for task_id in ordered:
                task = by_id[task_id]
                if previous is not None and int(task.start_time_step) < int(previous.end_time_step):
                    delta = int(previous.end_time_step) - int(task.start_time_step)
                    if not shift_chain(
                        task_id,
                        delta,
                        "ROBOT_TASK_SEQUENCE",
                    ):
                        changed = False
                        break
                    changed = True
                    break
                previous = task
            if changed or errors:
                break
        if errors:
            break
        if changed:
            continue

        # Preserve explicit execution dependencies after any resource delay.
        for dependency in dependencies:
            predecessor_id = str(dependency.get("predecessor_task_id") or "")
            successor_id = str(dependency.get("successor_task_id") or "")
            if predecessor_id not in by_id or successor_id not in by_id:
                continue
            lag_seconds = int(dependency.get("lag_seconds") or 0)
            lag_steps = math.ceil(lag_seconds / time_step_seconds)
            predecessor = by_id[predecessor_id]
            successor = by_id[successor_id]
            if str(predecessor.robot_id) == str(successor.robot_id):
                ordered = robot_order[str(successor.robot_id)]
                if robot_position[predecessor_id] >= robot_position[successor_id]:
                    errors.append(
                        "RESOURCE_DEPENDENCY_ORDER_CONFLICT: "
                        f"robot={successor.robot_id} "
                        f"predecessor={predecessor_id} successor={successor_id}"
                    )
                    break
            required_start = int(predecessor.end_time_step) + lag_steps
            if int(successor.start_time_step) >= required_start:
                continue
            if not shift_chain(
                successor_id,
                required_start - int(successor.start_time_step),
                "EXECUTION_DEPENDENCY",
            ):
                break
            changed = True
            break
        if errors:
            break
        if changed:
            continue

        resource_tasks: list[tuple[ScheduledTask, dict[str, Any]]] = []
        for task in tasks:
            spec = _resource_spec(
                task,
                nodes,
                time_step_seconds=time_step_seconds,
            )
            if spec is not None:
                resource_tasks.append((task, spec))
                if spec["capacity_source"] is None:
                    warning_codes.add(
                        f"RESOURCE_CAPACITY_DEFAULTED_TO_ONE:{spec['resource_type']}:{spec['node_id']}"
                    )

        occupancy_by_resource: dict[
            tuple[str, int], dict[int, list[str]]
        ] = defaultdict(lambda: defaultdict(list))

        # Fixed/frozen tasks reserve first and are never moved by this stage.
        fixed_rows = [
            (task, spec)
            for task, spec in resource_tasks
            if task.task_id in frozen_task_ids
        ]
        for task, spec in sorted(
            fixed_rows,
            key=lambda pair: (
                int(pair[0].end_time_step) - int(pair[1]["duration_steps"]),
                str(pair[0].task_id),
            ),
        ):
            start = int(task.end_time_step) - int(spec["duration_steps"])
            end = int(task.end_time_step)
            occupancy = occupancy_by_resource[
                (str(spec["resource_type"]), int(spec["node_id"]))
            ]
            if not _occupancy_is_free(
                occupancy,
                start,
                end,
                int(spec["capacity"]),
            ):
                errors.append(
                    "RESOURCE_CAPACITY_EXCEEDED_BY_FROZEN_TASK: "
                    f"{spec['resource_type']} node={spec['node_id']} task={task.task_id}"
                )
                break
            _reserve_occupancy(
                occupancy,
                start_step=start,
                end_step=end,
                task_id=task.task_id,
            )
        if errors:
            break

        flexible_rows = [
            (task, spec)
            for task, spec in resource_tasks
            if task.task_id not in frozen_task_ids
        ]
        for task, spec in sorted(
            flexible_rows,
            key=lambda pair: (
                int(pair[0].end_time_step) - int(pair[1]["duration_steps"]),
                int(pair[0].priority),
                str(pair[0].robot_id),
                str(pair[0].task_id),
            ),
        ):
            duration = int(spec["duration_steps"])
            desired_start = int(task.end_time_step) - duration
            candidate_start = desired_start
            occupancy = occupancy_by_resource[
                (str(spec["resource_type"]), int(spec["node_id"]))
            ]
            delay_limit = desired_start + max_iterations * max(1, duration)
            while not _occupancy_is_free(
                occupancy,
                candidate_start,
                candidate_start + duration,
                int(spec["capacity"]),
            ):
                candidate_start += 1
                if candidate_start > delay_limit:
                    errors.append(
                        "RESOURCE_SLOT_SEARCH_EXHAUSTED: "
                        f"{spec['resource_type']} node={spec['node_id']} task={task.task_id}"
                    )
                    break
            if errors:
                break
            if candidate_start > desired_start:
                if not shift_chain(
                    task.task_id,
                    candidate_start - desired_start,
                    "SHARED_RESOURCE_CAPACITY",
                    int(spec["node_id"]),
                ):
                    break
                changed = True
                break
            _reserve_occupancy(
                occupancy,
                start_step=candidate_start,
                end_step=candidate_start + duration,
                task_id=task.task_id,
            )
        if errors:
            break
        if changed:
            continue

        # Stable schedule: emit auditable reservation rows and slot numbers.
        grouped_rows: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for task, spec in resource_tasks:
            end = int(task.end_time_step)
            start = end - int(spec["duration_steps"])
            key = (str(spec["resource_type"]), int(spec["node_id"]))
            grouped_rows[key].append(
                {
                    "reservation_id": _reservation_id(
                        key[0], key[1], task.task_id, start, end
                    ),
                    "resource_type": key[0],
                    "node_id": key[1],
                    "node_type": spec["node_type"],
                    "capacity": int(spec["capacity"]),
                    "capacity_source": spec["capacity_source"] or "DEFAULT_ONE",
                    "task_id": task.task_id,
                    "work_id": task.work_id,
                    "robot_id": task.robot_id,
                    "action": task.action,
                    "start_time_step": start,
                    "end_time_step": end,
                    "duration_steps": int(spec["duration_steps"]),
                    "duration_seconds": int(spec["duration_steps"])
                    * time_step_seconds,
                    "original_task_start_time_step": original_times[task.task_id][0],
                    "original_task_end_time_step": original_times[task.task_id][1],
                    "task_start_time_step": int(task.start_time_step),
                    "task_end_time_step": int(task.end_time_step),
                    "shifted_steps": int(task.start_time_step)
                    - original_times[task.task_id][0],
                    "fixed": task.task_id in frozen_task_ids,
                }
            )
        final_rows = []
        for key, rows in sorted(grouped_rows.items()):
            capacity = int(rows[0]["capacity"])
            assigned, valid = _assign_slots(rows, capacity)
            if not valid:
                errors.append(
                    f"RESOURCE_CAPACITY_EXCEEDED: {key[0]} node={key[1]}"
                )
                break
            final_rows.extend(assigned)
        break
    else:
        errors.append("RESOURCE_SCHEDULER_DID_NOT_CONVERGE")

    # Hard-window violations are a blocking outcome, never a soft penalty.
    for task_id, latest_step in hard_latest.items():
        task = by_id.get(task_id)
        if task is None:
            continue
        if int(task.end_time_step) > int(latest_step):
            errors.append(
                f"RESOURCE_DELAY_HARD_WINDOW_VIOLATION: {task_id} "
                f"end={task.end_time_step} latest={latest_step}"
            )

    tasks.sort(
        key=lambda task: (
            int(task.start_time_step),
            int(task.priority),
            str(task.robot_id),
            str(task.task_id),
        )
    )
    new_makespan = max((int(task.end_time_step) for task in tasks), default=0)
    old_makespan = max((value[1] for value in original_times.values()), default=0)
    weights = problem.get("weights") or {}
    makespan_weight = float(weights.get("makespan", 1.0))
    objective_delta = max(0, new_makespan - old_makespan) * makespan_weight

    summary: dict[str, dict[str, Any]] = {}
    for row in final_rows:
        key = f"{row['resource_type']}:{row['node_id']}"
        item = summary.setdefault(
            key,
            {
                "resource_type": row["resource_type"],
                "node_id": row["node_id"],
                "node_type": row["node_type"],
                "capacity": row["capacity"],
                "reservation_count": 0,
                "slot_indexes": [],
            },
        )
        item["reservation_count"] += 1
        item["slot_indexes"].append(int(row["slot_index"]))
    for item in summary.values():
        item["slot_indexes"] = sorted(set(item["slot_indexes"]))

    warnings = sorted(warning_codes)
    result = {
        "version": RESOURCE_SCHEDULER_VERSION,
        "valid": not errors,
        "status": "PASS" if not errors else "FAILED",
        "time_step_seconds": time_step_seconds,
        "reservation_count": len(final_rows),
        "reservations": sorted(
            final_rows,
            key=lambda row: (
                int(row["start_time_step"]),
                str(row["resource_type"]),
                int(row["node_id"]),
                int(row["slot_index"]),
                str(row["task_id"]),
            ),
        ),
        "resource_summary": list(summary.values()),
        "adjustment_count": len(adjustments),
        "adjustments": adjustments,
        "old_makespan_time_steps": old_makespan,
        "new_makespan_time_steps": new_makespan,
        "added_makespan_time_steps": max(0, new_makespan - old_makespan),
        "objective_delta": round(objective_delta, 6),
        "iterations": iterations,
        "warnings": warnings,
        "errors": list(dict.fromkeys(errors)),
        "policies": {
            "service_node_capacity": "HARD_CONSTRAINT",
            "charger_slot_capacity": "HARD_CONSTRAINT",
            "frozen_task_shift": "PROHIBITED",
            "default_capacity": 1,
            "mapf_idle_capacity": "VALIDATED_AFTER_ROUTING",
        },
    }

    metadata = deepcopy(plan.metadata)
    metadata["shared_resource_scheduling"] = result
    metadata["resource_reservations"] = result["reservations"]
    metadata["makespan_time_steps"] = new_makespan
    metadata["shared_resource_objective_delta"] = round(objective_delta, 6)
    updated = plan.model_copy(
        update={
            "scheduled_tasks": tasks,
            "objective_value": round(float(plan.objective_value) + objective_delta, 6),
            "metadata": metadata,
        }
    )
    return updated, result


def finalize_idle_resource_reservations(
    problem: dict[str, Any],
    collision_plan: CollisionFreePlan,
    base_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate routed PARKING/STAGING/WAITING occupancy against node capacity."""

    result = deepcopy(base_result or {})
    nodes = _node_index(problem)
    time_step_seconds = max(1, int(problem.get("time_step_seconds") or 5))
    idle_rows: list[dict[str, Any]] = []
    errors = list(result.get("errors") or [])
    warnings = set(result.get("warnings") or [])

    for raw in collision_plan.metadata.get("idle_action_tasks", []) or []:
        action = str(raw.get("action") or "").upper()
        if action != "WAIT_AT_IDLE_NODE":
            continue
        node_id = int(raw.get("target_node") or raw.get("source_node"))
        node = nodes.get(node_id, {"node_id": node_id})
        node_type = _node_type(node)
        if node_type not in IDLE_NODE_TYPES and not bool(node.get("idle_allowed")):
            continue
        capacity, source = _capacity_descriptor(node, "IDLE_SPACE")
        if source is None:
            warnings.add(f"RESOURCE_CAPACITY_DEFAULTED_TO_ONE:IDLE_SPACE:{node_id}")
        start = int(raw.get("start_time_step") or 0)
        end = int(raw.get("end_time_step") or start)
        max_idle_seconds = node.get("maximum_idle_duration")
        if max_idle_seconds is None:
            max_idle_seconds = node.get("max_idle_seconds")
        if max_idle_seconds is not None:
            try:
                maximum = int(max_idle_seconds)
            except (TypeError, ValueError):
                maximum = 0
            if maximum > 0 and (end - start) * time_step_seconds > maximum:
                errors.append(
                    f"MAXIMUM_IDLE_DURATION_EXCEEDED: node={node_id} "
                    f"robot={raw.get('robot_id')}"
                )
        idle_rows.append(
            {
                "reservation_id": _reservation_id(
                    "IDLE_SPACE",
                    node_id,
                    str(raw.get("idle_task_id") or raw.get("next_task_id") or raw.get("robot_id")),
                    start,
                    end,
                ),
                "resource_type": "IDLE_SPACE",
                "node_id": node_id,
                "node_type": node_type,
                "capacity": capacity,
                "capacity_source": source or "DEFAULT_ONE",
                "task_id": raw.get("idle_task_id") or raw.get("next_task_id"),
                "work_id": None,
                "robot_id": raw.get("robot_id"),
                "action": action,
                "start_time_step": start,
                "end_time_step": end,
                "duration_steps": max(0, end - start),
                "duration_seconds": max(0, end - start) * time_step_seconds,
                "fixed": False,
            }
        )

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in idle_rows:
        grouped[int(row["node_id"])].append(row)
    assigned_idle: list[dict[str, Any]] = []
    for node_id, rows in sorted(grouped.items()):
        capacity = int(rows[0]["capacity"])
        assigned, valid = _assign_slots(rows, capacity)
        if not valid:
            errors.append(f"IDLE_SPACE_CAPACITY_EXCEEDED: node={node_id}")
            continue
        assigned_idle.extend(assigned)

    all_rows = [
        row
        for row in result.get("reservations", []) or []
        if str(row.get("resource_type")) != "IDLE_SPACE"
    ] + assigned_idle
    result["reservations"] = sorted(
        all_rows,
        key=lambda row: (
            int(row.get("start_time_step") or 0),
            str(row.get("resource_type") or ""),
            int(row.get("node_id") or 0),
            int(row.get("slot_index") or 0),
        ),
    )
    result["reservation_count"] = len(result["reservations"])
    result["idle_reservation_count"] = len(assigned_idle)
    result["idle_reservations"] = assigned_idle
    result["warnings"] = sorted(warnings)
    result["errors"] = list(dict.fromkeys(errors))
    result["valid"] = not result["errors"]
    result["status"] = "PASS" if result["valid"] else "FAILED"
    return result

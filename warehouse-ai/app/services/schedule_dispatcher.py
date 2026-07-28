"""Build a gateway payload containing only tasks that are READY now."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def ready_only_plan_payload(
    plan: dict[str, Any],
    ready_task_ids: list[str],
) -> dict[str, Any]:
    payload = deepcopy(plan)
    ready = set(ready_task_ids)
    payload["dispatch_policy"] = "READY_ONLY"
    payload["ready_task_ids"] = sorted(ready)
    payload["required_tasks"] = [
        row
        for row in payload.get("required_tasks", [])
        if str(row.get("task_id")) in ready
    ]
    scheduled = [
        row
        for row in payload.get("cuopt_plan", {}).get("scheduled_tasks", [])
        if str(row.get("task_id")) in ready
    ]
    payload.setdefault("cuopt_plan", {})["scheduled_tasks"] = scheduled
    ready_by_robot: dict[str, list[dict[str, Any]]] = {}
    for row in scheduled:
        ready_by_robot.setdefault(str(row.get("robot_id")), []).append(row)

    filtered_routes: list[dict[str, Any]] = []
    for raw_route in payload.get("collision_plan", {}).get("routes", []):
        robot_id = str(raw_route.get("robot_id"))
        robot_tasks = ready_by_robot.get(robot_id, [])
        if not robot_tasks:
            continue
        route = deepcopy(raw_route)
        route["task_ids"] = sorted(
            set(str(value) for value in route.get("task_ids", [])) & ready
        )
        cutoff = max(int(row.get("end_time_step") or 0) for row in robot_tasks)
        route["waypoints"] = [
            row
            for row in route.get("waypoints", [])
            if int(row.get("time_step") or 0) <= cutoff
        ]
        filtered_routes.append(route)
    payload.setdefault("collision_plan", {})["routes"] = filtered_routes
    payload["received_robot_count"] = len(ready_by_robot)
    return payload

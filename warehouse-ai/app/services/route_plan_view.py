"""Public millisecond route-plan projection for backend and UI consumers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.integration_models import (
    RoutePlanRobotRoute,
    RoutePlanStep,
    SimulationViewResponse,
)
from app.services.public_output import sanitize_public_warnings


SERVICE_KIND_BY_ACTION = {
    "PICK": "PICKUP",
    "DROP": "DROPOFF",
    "CHARGE": "CHARGE",
}

PLANNER_NAME_BY_ENGINE = {
    "PRIORITIZED_TIME_ASTAR": "prioritized_time_astar",
    "EXTERNAL_CBS": "external_cbs",
}

STATION_RESOURCE_MARKERS = {
    "CHARGER",
    "CHARGING",
    "INBOUND",
    "OUTBOUND",
    "STATION",
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _explicit_bool(*values: Any) -> bool:
    for value in values:
        if value is not None:
            return bool(value)
    return False


def _route_context(
    output: dict[str, Any],
) -> tuple[dict[int, str], dict[tuple[int, int], str]]:
    data = _as_dict(output.get("data"))
    context = _as_dict(
        output.get("route_view_context")
        or data.get("route_view_context")
    )

    node_codes: dict[int, str] = {}
    for raw in _as_list(context.get("nodes")):
        row = _as_dict(raw)
        if row.get("node_id") is None:
            continue
        node_id = int(row["node_id"])
        node_codes[node_id] = str(row.get("node_code") or node_id)

    edge_ids: dict[tuple[int, int], str] = {}
    for raw in _as_list(context.get("edges")):
        row = _as_dict(raw)
        if row.get("from_node") is None or row.get("to_node") is None:
            continue
        start = int(row["from_node"])
        target = int(row["to_node"])
        edge_id = str(row.get("edge_id") or f"{start}->{target}")
        edge_ids[(start, target)] = edge_id
        if str(row.get("direction") or "").upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            edge_ids[(target, start)] = edge_id
    return node_codes, edge_ids


def _node_code(node_codes: dict[int, str], value: Any) -> str:
    node_id = int(value)
    return node_codes.get(node_id, str(node_id))


def _planner_name(collision_plan: dict[str, Any]) -> str:
    metadata = _as_dict(collision_plan.get("metadata"))
    explicit = metadata.get("planner")
    if explicit:
        return str(explicit).lower()
    engine = str(collision_plan.get("engine") or "UNKNOWN").upper()
    return PLANNER_NAME_BY_ENGINE.get(engine, engine.lower())


def _wait_reason_index(
    collision_plan: dict[str, Any],
) -> dict[tuple[str, int, int], str]:
    result: dict[tuple[str, int, int], str] = {}
    metadata = _as_dict(collision_plan.get("metadata"))
    for raw in _as_list(metadata.get("wait_evidence")):
        row = _as_dict(raw)
        if (
            row.get("robot_id") is None
            or row.get("node_id") is None
            or row.get("time_step") is None
        ):
            continue
        result[
            (
                str(row["robot_id"]),
                int(row["node_id"]),
                int(row["time_step"]),
            )
        ] = str(row.get("reason") or "WAIT")
    return result


def _assignment_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    simulation = _as_dict(output.get("simulation"))
    optimization_plan = _as_dict(output.get("optimization_plan"))
    data = _as_dict(output.get("data"))
    values = (
        _as_list(simulation.get("task_assignments"))
        or _as_list(optimization_plan.get("scheduled_tasks"))
        or _as_list(data.get("task_assignments"))
    )
    return [_as_dict(value) for value in values]


def _service_task_id(
    assignments: list[dict[str, Any]],
    completion_steps: dict[str, int],
    *,
    robot_id: str,
    action: str,
    node_id: int,
    end_step: int,
    used_task_ids: set[str],
) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for row in assignments:
        task_id = str(row.get("task_id") or "")
        if not task_id or task_id in used_task_ids:
            continue
        if str(row.get("robot_id") or "") != robot_id:
            continue
        if str(row.get("action") or "").upper() != action:
            continue
        if row.get("target_node") is not None and int(row["target_node"]) != node_id:
            continue
        completion = completion_steps.get(
            task_id,
            int(row.get("end_time_step") or end_step),
        )
        candidates.append(
            (
                0 if completion == end_step else 1,
                abs(completion - end_step),
                task_id,
            )
        )
    if not candidates:
        return None
    task_id = min(candidates)[2]
    used_task_ids.add(task_id)
    return task_id


def _merge_wait_or_service_steps(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row in rows:
        if not merged or row["step_type"] == "MOVE":
            merged.append(row)
            continue
        previous = merged[-1]
        compatible = (
            previous["step_type"] == row["step_type"]
            and previous["end_at_ms"] == row["start_at_ms"]
            and previous.get("_node_numeric") == row.get("_node_numeric")
            and previous.get("_action") == row.get("_action")
            and (
                row["step_type"] != "WAIT"
                or previous.get("reason") == row.get("reason")
            )
        )
        if not compatible:
            merged.append(row)
            continue
        previous["end_at_ms"] = row["end_at_ms"]
        if row.get("_end_step") is not None:
            previous["_end_step"] = row["_end_step"]
    return merged


def _route_steps(
    route: dict[str, Any],
    *,
    milliseconds_per_step: int,
    node_codes: dict[int, str],
    edge_ids: dict[tuple[int, int], str],
    wait_reasons: dict[tuple[str, int, int], str],
    assignments: list[dict[str, Any]],
    completion_steps: dict[str, int],
) -> list[RoutePlanStep]:
    robot_id = str(route.get("robot_id") or "")
    waypoints = [_as_dict(value) for value in _as_list(route.get("waypoints"))]
    raw_steps: list[dict[str, Any]] = []

    for left, right in zip(waypoints, waypoints[1:]):
        if (
            left.get("node_id") is None
            or right.get("node_id") is None
            or left.get("time_step") is None
            or right.get("time_step") is None
        ):
            continue
        start_node = int(left["node_id"])
        end_node = int(right["node_id"])
        start_step = int(left["time_step"])
        end_step = int(right["time_step"])
        if end_step <= start_step:
            continue
        start_at_ms = start_step * milliseconds_per_step
        end_at_ms = end_step * milliseconds_per_step
        action = str(right.get("action") or "MOVE").upper()

        if start_node != end_node:
            raw_steps.append(
                {
                    "step_type": "MOVE",
                    "start_at_ms": start_at_ms,
                    "end_at_ms": end_at_ms,
                    "edge_id": edge_ids.get(
                        (start_node, end_node),
                        f"{start_node}->{end_node}",
                    ),
                    "from_node": _node_code(node_codes, start_node),
                    "to_node": _node_code(node_codes, end_node),
                }
            )
            continue

        if action in SERVICE_KIND_BY_ACTION:
            raw_steps.append(
                {
                    "step_type": "SERVICE",
                    "start_at_ms": start_at_ms,
                    "end_at_ms": end_at_ms,
                    "node_id": _node_code(node_codes, end_node),
                    "service_kind": SERVICE_KIND_BY_ACTION[action],
                    "_node_numeric": end_node,
                    "_action": action,
                    "_end_step": end_step,
                }
            )
            continue

        raw_steps.append(
            {
                "step_type": "WAIT",
                "start_at_ms": start_at_ms,
                "end_at_ms": end_at_ms,
                "node_id": _node_code(node_codes, end_node),
                "reason": wait_reasons.get(
                    (robot_id, end_node, end_step),
                    "WAIT",
                ),
                "_node_numeric": end_node,
                "_action": "WAIT",
            }
        )

    merged = _merge_wait_or_service_steps(raw_steps)
    used_task_ids: set[str] = set()
    result: list[RoutePlanStep] = []
    for row in merged:
        if row["step_type"] == "SERVICE":
            action = str(row.pop("_action"))
            node_id = int(row.pop("_node_numeric"))
            end_step = int(row.pop("_end_step"))
            task_id = _service_task_id(
                assignments,
                completion_steps,
                robot_id=robot_id,
                action=action,
                node_id=node_id,
                end_step=end_step,
                used_task_ids=used_task_ids,
            )
            if task_id:
                row["task_id"] = task_id
        else:
            row.pop("_action", None)
            row.pop("_node_numeric", None)
            row.pop("_end_step", None)
        result.append(RoutePlanStep.model_validate(row))
    return result


def _resource_reservations(
    output: dict[str, Any],
    *,
    milliseconds_per_step: int,
    node_codes: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = _as_dict(output.get("data"))
    resource_plan = _as_dict(
        output.get("resource_reservation_plan")
        or data.get("resource_reservation_plan")
    )
    reservations: list[dict[str, Any]] = []
    station_reservations: list[dict[str, Any]] = []

    for raw in _as_list(resource_plan.get("reservations")):
        source = _as_dict(raw)
        row = {
            key: source.get(key)
            for key in (
                "reservation_id",
                "resource_type",
                "robot_id",
                "task_id",
                "slot_index",
            )
            if source.get(key) is not None
        }
        if source.get("node_id") is not None:
            row["node_id"] = _node_code(node_codes, source["node_id"])
        if source.get("start_time_step") is not None:
            row["start_at_ms"] = (
                int(source["start_time_step"]) * milliseconds_per_step
            )
        if source.get("end_time_step") is not None:
            row["end_at_ms"] = (
                int(source["end_time_step"]) * milliseconds_per_step
            )
        resource_type = str(source.get("resource_type") or "").upper()
        target = (
            station_reservations
            if any(marker in resource_type for marker in STATION_RESOURCE_MARKERS)
            else reservations
        )
        target.append(row)
    return reservations, station_reservations


def _conflicts(output: dict[str, Any]) -> list[dict[str, Any]]:
    simulation = _as_dict(output.get("simulation"))
    result: list[dict[str, Any]] = []
    for raw in _as_list(simulation.get("issues")):
        row = _as_dict(raw)
        if "CONFLICT" not in str(row.get("code") or "").upper():
            continue
        result.append(
            {
                key: row.get(key)
                for key in (
                    "code",
                    "message",
                    "robot_ids",
                    "task_ids",
                    "node_ids",
                    "time_steps",
                )
                if row.get(key) not in (None, [], {})
            }
        )
    return result


def build_route_plan_view(output: dict[str, Any]) -> SimulationViewResponse:
    """Project a full planning response into the stable route-plan contract."""

    output = _as_dict(output)
    data = _as_dict(output.get("data"))
    collision_plan = _as_dict(output.get("collision_plan"))
    simulation = _as_dict(output.get("simulation"))
    plan_validation = _as_dict(output.get("plan_validation"))
    metadata = _as_dict(collision_plan.get("metadata"))
    milliseconds_per_step = max(
        1,
        int(
            collision_plan.get("time_step_seconds")
            or _as_dict(simulation.get("metrics")).get("time_step_seconds")
            or 1
        ),
    ) * 1000

    node_codes, edge_ids = _route_context(output)
    wait_reasons = _wait_reason_index(collision_plan)
    assignments = _assignment_rows(output)
    completion_steps = {
        str(task_id): int(step)
        for task_id, step in _as_dict(
            metadata.get("task_completion_steps")
        ).items()
    }

    routes: list[RoutePlanRobotRoute] = []
    wait_by_robot: dict[str, int] = defaultdict(int)
    total_wait_ms = 0
    total_service_ms = 0
    makespan_ms = 0
    route_values = (
        _as_list(collision_plan.get("routes"))
        or _as_list(simulation.get("robot_routes"))
    )
    for raw_route in route_values:
        route = _as_dict(raw_route)
        robot_id = str(route.get("robot_id") or "")
        if not robot_id:
            continue
        steps = _route_steps(
            route,
            milliseconds_per_step=milliseconds_per_step,
            node_codes=node_codes,
            edge_ids=edge_ids,
            wait_reasons=wait_reasons,
            assignments=assignments,
            completion_steps=completion_steps,
        )
        for step in steps:
            duration = step.end_at_ms - step.start_at_ms
            if step.step_type == "WAIT":
                total_wait_ms += duration
                wait_by_robot[robot_id] += duration
            elif step.step_type == "SERVICE":
                total_service_ms += duration
        waypoints = _as_list(route.get("waypoints"))
        finish_at_ms = (
            int(_as_dict(waypoints[-1]).get("time_step") or 0)
            * milliseconds_per_step
            if waypoints
            else 0
        )
        makespan_ms = max(makespan_ms, finish_at_ms)
        routes.append(
            RoutePlanRobotRoute(
                robot_id=robot_id,
                steps=steps,
                finish_at_ms=finish_at_ms,
            )
        )

    warning_values = sanitize_public_warnings(
        _as_list(output.get("warnings"))
        + _as_list(simulation.get("warnings"))
    )
    warnings = [str(value) for value in warning_values]
    for robot_id in sorted(wait_by_robot):
        message = (
            f"{robot_id} accumulates {wait_by_robot[robot_id]} ms of MAPF wait."
        )
        if message not in warnings:
            warnings.append(message)

    reservations, station_reservations = _resource_reservations(
        output,
        milliseconds_per_step=milliseconds_per_step,
        node_codes=node_codes,
    )
    return SimulationViewResponse(
        valid=_explicit_bool(
            simulation.get("valid"),
            data.get("valid"),
            plan_validation.get("valid"),
        ),
        planner=_planner_name(collision_plan),
        routes=routes,
        reservations=reservations,
        station_reservations=station_reservations,
        conflicts=_conflicts(output),
        warnings=warnings,
        total_wait_ms=total_wait_ms,
        total_service_ms=total_service_ms,
        makespan_ms=makespan_ms,
    )

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.models import ResponseView
from app.services.public_output import (
    sanitize_public_answer,
    sanitize_public_verification,
    sanitize_public_warnings,
)
from app.services.wait_compression import (
    compress_resolution_events,
    compress_timeline,
    compress_wait_rows,
    compress_waypoints,
)


RESPONSE_SCHEMA_VERSION = "p16.5.12.1"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if source.get(key) is not None}


def _compact_assignment(row: Any) -> dict[str, Any]:
    source = _as_dict(row)
    return _pick(
        source,
        "task_id",
        "work_id",
        "action",
        "robot_id",
        "source_node",
        "target_node",
        "start_time_step",
        "end_time_step",
        "schedule_status",
        "priority",
    )


def _compact_charger(row: Any) -> dict[str, Any]:
    source = _as_dict(row)
    return _pick(
        source,
        "task_id",
        "robot_id",
        "selected_charger_node",
        "charger_node",
        "charger_cost",
        "selection_policy",
        "selection_reason",
        "battery_before_travel",
        "battery_at_charger",
        "charged_percent",
        "target_battery",
        "projected_final_battery",
        "charge_duration_seconds",
    )


def _compact_resources(value: Any) -> dict[str, Any]:
    source = _as_dict(value)
    reservations = [
        _pick(
            _as_dict(row),
            "reservation_id",
            "resource_type",
            "node_id",
            "node_type",
            "capacity",
            "slot_index",
            "task_id",
            "work_id",
            "robot_id",
            "action",
            "start_time_step",
            "end_time_step",
            "duration_seconds",
            "shifted_steps",
        )
        for row in _as_list(source.get("reservations"))[:50]
    ]
    return {
        "status": source.get("status"),
        "valid": source.get("valid"),
        "reservation_count": source.get("reservation_count", len(reservations)),
        "adjustment_count": source.get("adjustment_count", 0),
        "idle_reservation_count": source.get("idle_reservation_count", 0),
        "added_makespan_time_steps": source.get("added_makespan_time_steps", 0),
        "resource_summary": _as_list(source.get("resource_summary")),
        "reservations": reservations,
        "warnings": sanitize_public_warnings(source.get("warnings")),
        "errors": sanitize_public_warnings(source.get("errors")),
    }


def _compact_mapf_replan(response: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    policy = _as_dict(response.get("mapf_replan_policy") or data.get("mapf_replan_policy"))
    route_failure = _as_dict(response.get("route_failure") or data.get("route_failure"))
    history = _as_list(response.get("replan_history") or data.get("replan_history"))
    return {
        "version": (
            policy.get("version")
            or route_failure.get("version")
            or RESPONSE_SCHEMA_VERSION
        ),
        "enabled": policy.get("enabled", False),
        "attempt": policy.get("attempt", response.get("replan_attempt", 0)),
        "scope": policy.get("scope"),
        "strategy": policy.get("strategy"),
        "affected_robot_ids": _as_list(policy.get("affected_robot_ids")),
        "escalated_from_local": policy.get("escalated_from_local", False),
        "last_failure_code": route_failure.get("code"),
        "last_failure_category": route_failure.get("category"),
        "retryable": route_failure.get("retryable"),
        "history": [
            _pick(
                _as_dict(row),
                "attempt",
                "scope",
                "status",
                "verification_before",
                "verification_after",
                "affected_robot_ids",
                "affected_task_ids",
                "failure_signature",
            )
            for row in history[-3:]
        ],
    }


def _compact_operational_objective(value: Any) -> dict[str, Any]:
    source = _as_dict(value)
    return {
        "version": source.get("version"),
        "status": source.get("status"),
        "objective_scope": source.get("objective_scope"),
        "hard_constraint_policy": source.get("hard_constraint_policy"),
        "total": source.get("total"),
        "metrics": _as_dict(source.get("metrics")),
        "components": _as_dict(source.get("components")),
        "weights": _as_dict(source.get("weights")),
        "role_contract": _as_dict(source.get("role_contract")),
    }


def _compact_inventory(feasibility: Any) -> dict[str, Any]:
    source = _as_dict(feasibility)
    items: list[dict[str, Any]] = []
    for raw in _as_list(source.get("item_results")):
        row = _as_dict(raw)
        items.append(
            _pick(
                row,
                "operation_id",
                "work_id",
                "operation_type",
                "item_id",
                "requested_quantity_boxes",
                "planned_quantity_boxes",
                "available_quantity_boxes",
                "shortage_quantity_boxes",
                "status",
                "required_at",
                "earliest_full_fulfillment_at",
            )
        )
    return {
        "status": source.get("status"),
        "valid": source.get("valid"),
        "partial_success": source.get("partial_success", False),
        "items": items,
        "warnings": sanitize_public_warnings(source.get("warnings")),
    }


def _compact_verification(response: dict[str, Any]) -> dict[str, Any]:
    decision = sanitize_public_verification(response.get("verification_decision"))
    return {
        "decision": decision.get("decision"),
        "summary": decision.get("summary"),
        "requires_replan": decision.get("requires_replan", False),
        "replan_scope": decision.get("replan_scope"),
        "blocking_findings": decision.get("blocking_findings", []),
        "warning_findings": decision.get("warning_findings", []),
        "user_visible_warnings": decision.get("user_visible_warnings", []),
        "confidence": decision.get("confidence"),
    }


def _compact_collision(response: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    collision = _as_dict(response.get("collision_plan"))
    metadata = _as_dict(collision.get("metadata"))
    simulation = _as_dict(response.get("simulation"))
    wait_evidence = _as_list(metadata.get("wait_evidence"))
    events = _as_list(metadata.get("resolution_events"))
    if not events:
        reservations = _as_dict(data.get("reservation_evidence"))
        events = _as_list(reservations.get("resolution_events"))
    return {
        "final_conflict_count": simulation.get(
            "conflict_count",
            data.get("conflict_count", metadata.get("final_conflict_count", 0)),
        ),
        "wait_count": len(wait_evidence),
        "reroute_count": metadata.get("reroute_count", 0),
        "resolution_events": events[:20],
    }


def _compact_metrics(response: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    simulation = _as_dict(response.get("simulation"))
    metrics = _as_dict(simulation.get("metrics"))
    battery = data.get("battery_by_robot") or metrics.get("battery_by_robot") or {}
    return {
        "total_distance": simulation.get("total_distance", data.get("total_distance")),
        "makespan_time_steps": simulation.get("makespan", data.get("makespan")),
        "makespan_seconds": data.get("makespan_seconds") or metrics.get("makespan_seconds"),
        "schedule_completion_at": data.get("schedule_completion_at")
        or metrics.get("schedule_completion_at"),
        "tardiness": simulation.get("tardiness", data.get("tardiness")),
        "conflict_count": simulation.get("conflict_count", data.get("conflict_count")),
        "battery_by_robot": battery,
    }



def _compress_route_waypoints(route: dict[str, Any], *, time_step_seconds: int) -> None:
    raw = _as_list(route.get("waypoints"))
    if not raw:
        return
    compressed = compress_waypoints(raw, time_step_seconds=time_step_seconds)
    route["waypoints"] = compressed
    route["waypoint_count_raw"] = len(raw)
    route["waypoint_count_compressed"] = len(compressed)


def _compress_collision_plan_for_view(collision: dict[str, Any]) -> None:
    if not collision:
        return
    step_seconds = max(1, int(collision.get("time_step_seconds") or 1))
    routes = _as_list(collision.get("routes"))
    raw_waypoint_count = 0
    compressed_waypoint_count = 0
    for raw_route in routes:
        route = _as_dict(raw_route)
        raw_waypoint_count += len(_as_list(route.get("waypoints")))
        _compress_route_waypoints(route, time_step_seconds=step_seconds)
        compressed_waypoint_count += len(_as_list(route.get("waypoints")))

    metadata = _as_dict(collision.get("metadata"))
    raw_waits = _as_list(metadata.get("wait_evidence"))
    raw_events = _as_list(metadata.get("resolution_events"))
    if raw_waits:
        metadata["wait_evidence"] = compress_wait_rows(raw_waits)
    if raw_events:
        metadata["resolution_events"] = compress_resolution_events(raw_events)
    metadata["presentation_compression"] = {
        "mode": "CONSECUTIVE_WAIT_AND_CHARGE_RANGES",
        "compressed_actions": ["WAIT", "CHARGE"],
        "lossless_for_wait_duration": True,
        "lossless_for_charge_duration": True,
        "raw_waypoint_count": raw_waypoint_count,
        "compressed_waypoint_count": compressed_waypoint_count,
        "raw_wait_evidence_count": len(raw_waits),
        "compressed_wait_evidence_count": len(_as_list(metadata.get("wait_evidence"))),
        "raw_resolution_event_count": len(raw_events),
        "compressed_resolution_event_count": len(_as_list(metadata.get("resolution_events"))),
    }
    collision["metadata"] = metadata


def _compress_simulation_for_view(simulation: dict[str, Any]) -> None:
    if not simulation:
        return
    metrics = _as_dict(simulation.get("metrics"))
    step_seconds = max(1, int(metrics.get("time_step_seconds") or 1))
    for raw_route in _as_list(simulation.get("robot_routes")):
        route = _as_dict(raw_route)
        _compress_route_waypoints(route, time_step_seconds=step_seconds)
    timeline = _as_list(simulation.get("timeline"))
    if timeline:
        simulation["timeline"] = compress_timeline(
            timeline,
            time_step_seconds=step_seconds,
        )
        simulation["timeline_count_raw"] = len(timeline)
        simulation["timeline_count_compressed"] = len(simulation["timeline"])


def _compress_full_response_for_view(response: dict[str, Any]) -> dict[str, Any]:
    full = deepcopy(response)
    _compress_collision_plan_for_view(_as_dict(full.get("collision_plan")))
    _compress_simulation_for_view(_as_dict(full.get("simulation")))
    data = _as_dict(full.get("data"))
    data_timeline = _as_list(data.get("timeline"))
    if data_timeline:
        step_seconds = max(
            1,
            int(
                _as_dict(_as_dict(full.get("simulation")).get("metrics")).get(
                    "time_step_seconds"
                )
                or _as_dict(full.get("collision_plan")).get("time_step_seconds")
                or 1
            ),
        )
        data["timeline"] = compress_timeline(
            data_timeline,
            time_step_seconds=step_seconds,
        )
        data["timeline_count_raw"] = len(data_timeline)
        data["timeline_count_compressed"] = len(data["timeline"])
    return full

def resolve_response_view(
    requested: ResponseView | str | None,
    *,
    report_detail_level: str | None,
) -> ResponseView:
    if isinstance(requested, ResponseView):
        view = requested
    else:
        raw = getattr(requested, "value", requested)
        try:
            view = ResponseView(str(raw or ResponseView.AUTO.value).upper())
        except ValueError:
            view = ResponseView.AUTO
    if view == ResponseView.AUTO:
        return (
            ResponseView.FULL
            if str(report_detail_level or "").upper() == "DEBUG"
            else ResponseView.COMPACT
        )
    return view


def compact_planning_response(response: dict[str, Any]) -> dict[str, Any]:
    data = _as_dict(response.get("data"))
    assignments = _as_list(data.get("task_assignments"))
    charger_selections = _as_list(data.get("charger_selections"))
    if not charger_selections:
        charger_selections = _as_list(
            _as_dict(_as_dict(response.get("optimization_plan")).get("metadata")).get(
                "charger_selections"
            )
        )
    dependencies = _as_list(response.get("execution_task_dependencies"))
    if not dependencies:
        dependencies = _as_list(data.get("execution_task_dependencies"))
    schedule_validation = _as_dict(response.get("schedule_validation"))
    inventory = response.get("inventory_feasibility") or data.get(
        "inventory_feasibility"
    )
    resources = response.get("resource_reservation_plan") or data.get(
        "resource_reservation_plan"
    )
    operational_objective = response.get("operational_objective") or data.get(
        "operational_objective"
    )

    compact = {
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "response_view": ResponseView.COMPACT.value,
        "status": response.get("status"),
        "message": response.get("message"),
        "answer": sanitize_public_answer(response.get("answer")),
        "intent": response.get("intent"),
        "command_id": response.get("command_id"),
        "conversation_id": response.get("conversation_id"),
        "parent_command_id": response.get("parent_command_id"),
        "plan_version": response.get("plan_version"),
        "simulation_id": response.get("simulation_id"),
        "execution_mode": data.get("execution_mode"),
        "plan_mode": response.get("plan_mode") or data.get("plan_mode"),
        "verification": _compact_verification(response),
        "result": {
            "valid": data.get("valid", _as_dict(response.get("simulation")).get("valid")),
            "assignments": [_compact_assignment(row) for row in assignments],
            "metrics": _compact_metrics(response, data),
            "charging": [_compact_charger(row) for row in charger_selections],
            "dependencies": dependencies,
            "schedule_validation": _pick(
                schedule_validation,
                "valid",
                "dependency_count",
                "execution_dependency_count",
                "execution_dependency_order",
                "execution_dependency_violations",
                "validated_after_routing",
                "resource_capacity_valid",
                "resource_reservation_count",
            ),
            "collision_resolution": _compact_collision(response, data),
            "inventory": _compact_inventory(inventory),
            "resources": _compact_resources(resources),
            "objective": _compact_operational_objective(operational_objective),
            "mapf_replan": _compact_mapf_replan(response, data),
            "optimizer_roles": _as_dict(
                _as_dict(response.get("optimization_plan")).get("metadata")
            ).get("charge_visit_optimization_contract", {}),
            "dispatch": {
                "gateway_dispatched": response.get("gateway_dispatched", False),
                "dispatched_robot_count": response.get("dispatched_robot_count", 0),
                "dispatched_command_count": response.get("dispatched_command_count", 0),
            },
        },
        "report_detail_level": response.get("report_detail_level"),
        "report_source": response.get("report_source"),
        "warnings": sanitize_public_warnings(response.get("warnings")),
        "errors": sanitize_public_warnings(response.get("errors")),
        "details": {
            "full_response_request": {
                "response_view": ResponseView.FULL.value,
                "report_detail_level": "DEBUG",
            },
            "result_api": (
                f"/v1/commands/{response.get('command_id')}/result"
                if response.get("command_id")
                else None
            ),
            "simulation_view_api": (
                f"/v1/simulations/{response.get('simulation_id')}/view"
                if response.get("simulation_id")
                else None
            ),
            "execution_status_api": (
                f"/v1/execution/plans/{response.get('plan_version')}/status"
                if response.get("plan_version")
                else None
            ),
            "debug_api": (
                f"/v1/commands/{response.get('command_id')}/debug"
                if response.get("command_id")
                else None
            ),
            "evidence_api": (
                f"/v1/commands/{response.get('command_id')}/plan-evidence"
                if response.get("command_id")
                else None
            ),
        },
    }
    return compact


def shape_planning_response(
    response: dict[str, Any],
    requested: ResponseView | str | None,
) -> dict[str, Any]:
    """Return a public API view without mutating the persisted full response."""

    resolved = resolve_response_view(
        requested,
        report_detail_level=response.get("report_detail_level"),
    )
    if resolved == ResponseView.COMPACT:
        return compact_planning_response(response)
    full = _compress_full_response_for_view(response)
    full["response_schema_version"] = RESPONSE_SCHEMA_VERSION
    full["response_view"] = ResponseView.FULL.value
    return full

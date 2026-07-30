"""Stable public views derived from persisted planning output.

The planning graph keeps one rich internal result. This module projects that
result into purpose-specific contracts instead of asking each consumer to parse
the full response independently.
"""

from __future__ import annotations

from typing import Any

from app.integration_models import (
    DebugPlanningResponse,
    ExecutionStatusResponse,
    IntegrationResourceLinks,
    PlanningUiResponse,
    SimulationViewResponse,
)

from app.services.public_output import (
    sanitize_public_answer,
    sanitize_public_verification,
    sanitize_public_warnings,
)
from app.services.route_plan_view import build_route_plan_view



def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _compact_dict(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if source.get(key) is not None}


def _links(
    *,
    command_id: str | None,
    plan_version: str | None,
    simulation_id: str | None,
) -> IntegrationResourceLinks:
    return IntegrationResourceLinks(
        simulation_view=(
            f"/v1/simulations/{simulation_id}/view" if simulation_id else None
        ),
        execution_status=(
            f"/v1/execution/plans/{plan_version}/status" if plan_version else None
        ),
        plan_evidence=(
            f"/v1/commands/{command_id}/plan-evidence" if command_id else None
        ),
        stage_logs=f"/v1/commands/{command_id}/stages" if command_id else None,
        debug_view=f"/v1/commands/{command_id}/debug" if command_id else None,
    )


def build_planning_ui_view(
    history: dict[str, Any],
    output: dict[str, Any] | None = None,
) -> PlanningUiResponse:
    output = _as_dict(output)
    result_summary = _as_dict(history.get("result_summary"))
    result_data = _as_dict(result_summary.get("data"))
    simulation = _as_dict(output.get("simulation"))
    simulation_metrics = _as_dict(simulation.get("metrics"))
    plan_validation = _as_dict(output.get("plan_validation"))
    verification = (
        _as_dict(output.get("verification_decision"))
        or _as_dict(result_summary.get("verification"))
    )
    interpretation = _as_dict(output.get("interpretation"))
    plan_version = history.get("plan_version") or output.get("plan_version")
    simulation_id = history.get("simulation_id") or output.get("simulation_id")
    command_id = str(history.get("command_id") or output.get("command_id") or "")
    if not command_id:
        raise ValueError("command_id가 필요합니다.")

    if result_data:
        summary = _compact_dict(
            result_data,
            (
                "valid",
                "task_count",
                "robot_count",
                "total_distance",
                "makespan_seconds",
                "tardiness",
                "conflict_count",
                "route_count",
            ),
        )
        summary.setdefault(
            "route_count",
            len(_as_list(simulation.get("robot_routes")) or _as_list(_as_dict(output.get("collision_plan")).get("routes"))),
        )
    else:
        metrics = simulation_metrics or _as_dict(plan_validation.get("metrics"))
        summary = {
            "valid": simulation.get("valid", plan_validation.get("valid")),
            "task_count": len(
                _as_list(_as_dict(output.get("cuopt_plan")).get("scheduled_tasks"))
            ),
            "robot_count": metrics.get("robot_count"),
            "total_distance": metrics.get("total_distance"),
            "makespan_seconds": metrics.get("makespan_seconds"),
            "tardiness": metrics.get("tardiness_seconds"),
            "conflict_count": metrics.get("conflict_count"),
            "route_count": len(
                _as_list(simulation.get("robot_routes"))
                or _as_list(_as_dict(output.get("collision_plan")).get("routes"))
            ),
        }
        summary = {key: value for key, value in summary.items() if value is not None}

    user_warnings = (
        verification.get("user_visible_warnings")
        or result_summary.get("warnings")
        or output.get("warnings")
    )
    status = str(
        result_summary.get("status")
        or history.get("status")
        or output.get("status")
        or "UNKNOWN"
    )
    return PlanningUiResponse(
        command_id=command_id,
        conversation_id=history.get("conversation_id") or output.get("conversation_id"),
        warehouse_id=history.get("warehouse_id") or output.get("warehouse_id"),
        status=status,
        plan_version=plan_version,
        simulation_id=simulation_id,
        execution_mode=(
            history.get("resolved_execution_mode")
            or interpretation.get("execution_mode")
            or result_summary.get("execution_mode")
        ),
        intent=interpretation.get("intent") or result_summary.get("intent"),
        plan_mode=(
            _as_dict(output.get("scope")).get("plan_mode")
            or result_summary.get("plan_mode")
        ),
        message=result_summary.get("message"),
        answer=sanitize_public_answer(result_summary.get("answer")),
        summary=summary,
        verification=sanitize_public_verification(verification),
        warnings=sanitize_public_warnings(user_warnings),
        errors=_as_list(result_summary.get("errors") or output.get("errors")),
        resources=_links(
            command_id=command_id,
            plan_version=plan_version,
            simulation_id=simulation_id,
        ),
    )


def build_simulation_view(run: dict[str, Any]) -> SimulationViewResponse:
    output = _as_dict(run.get("output_payload"))
    if not (run.get("simulation_id") or output.get("simulation_id")):
        raise ValueError("simulation_id가 필요합니다.")
    return build_route_plan_view(output)


def build_execution_status_view(
    run: dict[str, Any],
    *,
    approval: dict[str, Any] | None,
    dispatch: dict[str, Any] | None,
) -> ExecutionStatusResponse:
    output = _as_dict(run.get("output_payload"))
    interpretation = _as_dict(output.get("interpretation"))
    execution_mode = str(interpretation.get("execution_mode") or "") or None
    plan_version = str(run.get("plan_version") or "")
    if not plan_version:
        raise ValueError("plan_version이 필요합니다.")
    dispatch_row = _as_dict(dispatch)
    approval_row = _as_dict(approval)
    result_summary = _as_dict(dispatch_row.get("result_summary"))
    execution_requested = execution_mode == "EXECUTE"
    if not execution_requested:
        execution_state = "NOT_REQUESTED"
    elif dispatch_row.get("status"):
        execution_state = str(dispatch_row.get("status"))
    elif approval_row:
        execution_state = "APPROVED_NOT_DISPATCHED"
    else:
        execution_state = "PENDING_APPROVAL"

    return ExecutionStatusResponse(
        plan_version=plan_version,
        command_id=run.get("command_id"),
        warehouse_id=run.get("warehouse_id"),
        simulation_id=run.get("simulation_id"),
        planning_status=run.get("status"),
        execution_mode=execution_mode,
        execution_requested=execution_requested,
        execution_state=execution_state,
        approval=approval_row,
        dispatch={
            key: dispatch_row.get(key)
            for key in (
                "dispatch_id",
                "gateway_dispatch_id",
                "status",
                "attempt_count",
                "gateway_cancel_confirmed",
                "created_at",
                "updated_at",
            )
            if dispatch_row.get(key) is not None
        },
        verification=sanitize_public_verification(output.get("verification_decision")),
        inventory_reservations=_as_dict(
            result_summary.get("inventory_reservation_release")
            or result_summary.get("inventory_reservations")
        ),
    )


def build_debug_view(
    history: dict[str, Any],
    output: dict[str, Any] | None,
) -> DebugPlanningResponse:
    output = _as_dict(output)
    command_id = str(history.get("command_id") or "")
    if not command_id:
        raise ValueError("command_id가 필요합니다.")
    plan_version = history.get("plan_version")
    simulation_id = history.get("simulation_id")
    return DebugPlanningResponse(
        command_id=command_id,
        plan_version=plan_version,
        simulation_id=simulation_id,
        status=history.get("status"),
        output=output,
        resources=_links(
            command_id=command_id,
            plan_version=plan_version,
            simulation_id=simulation_id,
        ),
    )

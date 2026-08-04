"""Apply generic incident safety actions to the planning/runtime overlay.

This PoC does not write to a real Redis/WCS yet.  "APPLIED" means the action is
committed to the current orchestration state and optimization constraints before
any HITL wait.  A production Redis/WCS adapter should perform the same update
atomically and then persist the returned runtime version.
"""
from __future__ import annotations

import re

from app.core.node_observability import observe_node
from app.domain.schemas import (
    IncidentResponseAction,
    IncidentResponsePlan,
    NormalizedWarehouseRequest,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState


_EDGE_ID = re.compile(r"^(?:H|V)\d+_\d+$", re.I)
_ROBOT_ID = re.compile(r"^R\d{3}$", re.I)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


@observe_node(
    "incident_immediate_action_executor",
    purpose="HITL 대기 전에 운영 사고의 임시 차단·로봇 Hold를 계획 Overlay에 먼저 반영",
)
def incident_immediate_action_executor_node(state: LaroGraphState) -> dict:
    """Commit conservative incident actions to the current planning overlay."""

    try:
        plan = model_from_state(state, "incident_response_plan", IncidentResponsePlan)
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        constraints = request.constraints
        hard_edges = list(constraints.hard_block_edge_ids)
        excluded_robots = list(constraints.excluded_robot_ids)
        updated_actions: list[IncidentResponseAction] = []

        for action in plan.immediate_actions:
            if action.action == "TEMPORARILY_BLOCK_RESOURCE":
                hard_edges.extend(
                    value for value in action.affected_resource_ids if _EDGE_ID.fullmatch(value)
                )
            elif action.action == "HOLD_AFFECTED_ROBOT":
                excluded_robots.extend(
                    value for value in action.affected_resource_ids if _ROBOT_ID.fullmatch(value)
                )
            # STOP_AFFECTED_MISSIONS is represented as an applied safety action;
            # mission cancellation/hold is a future WCS command concern.
            updated_actions.append(
                action.model_copy(
                    update={
                        "execution_status": "APPLIED",
                        "applied_immediately": True,
                        "reason": (
                            action.reason
                            + " Applied to the current planning overlay before any human response."
                        ),
                    }
                )
            )

        updated_request = request.model_copy(
            update={
                "constraints": constraints.model_copy(
                    update={
                        "hard_block_edge_ids": _dedupe(hard_edges),
                        "excluded_robot_ids": _dedupe(excluded_robots),
                    }
                )
            }
        )
        updated_plan = plan.model_copy(update={"immediate_actions": updated_actions})
        return {
            "normalized_request": updated_request,
            "incident_response_plan": updated_plan,
            "operator_notifications": list(updated_plan.notifications),
            **trace_update("incident_immediate_action_executor"),
        }
    except Exception as exc:
        return error_update(
            stage="incident_immediate_action_executor",
            code="incident_immediate_action_failed",
            message=str(exc),
        )

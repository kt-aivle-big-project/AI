"""Outer request classification before semantic formulation."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.policies.routing_policy import classify_entry_route


@observe_node(
    "entry_route_classifier",
    purpose="특수 경로를 분리하고 일반 Mission을 단일 입력 라우터 또는 강제 Rule 경로로 보냄",
)
def entry_route_classifier_node(state: LaroGraphState) -> dict:
    """Classify special routes and choose normalization/supervision strategy."""

    try:
        if state.get("normalized_request_override") is not None:
            from app.domain.schemas import EntryRouteDecision

            decision = EntryRouteDecision(
                route="NORMAL_FORMULATION",
                normalization_strategy="STRUCTURED",
                supervisor_strategy="DETERMINISTIC",
                reasons=[
                    "A frozen normalized request was supplied by the deferred evaluation replay; "
                    "no second normalization call is permitted."
                ],
            )
        else:
            decision = classify_entry_route(
                request_mode=state["request_mode"],
                planning_mode=state.get("planning_mode", "llm_router"),
                events=list(state.get("events", [])),
                user_command=state.get("user_command"),
                mission_spec=state.get("mission_spec"),
            )
        return {"entry_route_decision": decision, **trace_update("entry_route_classifier")}
    except Exception as exc:
        return error_update(stage="entry_route_classifier", code="entry_route_classification_failed", message=str(exc))

"""Build the final workflow plan after semantic supervision."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import EntryRouteDecision, FormulationDecision, NormalizedWarehouseRequest
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.policies.routing_policy import build_final_orchestration_plan


@observe_node(
    "orchestration_plan_builder",
    purpose="Supervisor와 결정론적 Guard 결과를 받아 Rule/Agent 조회·정식화 경로를 최종 확정",
)
def orchestration_plan_builder_node(state: LaroGraphState) -> dict:
    """Build the authoritative route after normalization and supervision."""

    try:
        entry = state.get("entry_route_decision")
        if entry is None:
            raise ValueError("orchestration_plan_builder requires entry_route_decision")
        if not isinstance(entry, EntryRouteDecision):
            entry = EntryRouteDecision.model_validate(entry)
        decision = state.get("formulation_decision")
        if decision is not None and not isinstance(decision, FormulationDecision):
            decision = FormulationDecision.model_validate(decision)
        normalized = state.get("normalized_request")
        if normalized is not None and not isinstance(normalized, NormalizedWarehouseRequest):
            normalized = NormalizedWarehouseRequest.model_validate(normalized)
        plan = build_final_orchestration_plan(
            entry=entry,
            planning_mode=state.get("planning_mode", "llm_router"),
            events=list(state.get("events", [])),
            user_command=state.get("user_command"),
            mission_spec=state.get("mission_spec"),
            normalized_request=normalized,
            formulation_decision=decision,
            requested_planning_mode=state.get("requested_planning_mode"),
            planning_mode_source=state.get("planning_mode_source", "environment"),
        )
        return {"orchestration_plan": plan, **trace_update("orchestration_plan_builder")}
    except Exception as exc:
        return error_update(stage="orchestration_plan_builder", code="orchestration_plan_failed", message=str(exc))

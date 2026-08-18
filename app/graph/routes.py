"""Conditional routing functions for the v13.20 HITL-aware single-decision Rule/Agent workflow."""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.schemas import (
    CandidateSpaceValidation,
    CuOptDynamicInputDraft,
    CuOptDynamicInputValidationResult,
    EntryRouteDecision,
    FormulationDecision,
    RequestGateDecision,
    MAPFValidationResult,
    OptimizerAssignmentValidation,
    OptimizerResult,
    OrchestrationPlan,
    PayloadValidationResult,
    PolicyValidationResult,
    RetrievalAgentStep,
    RetrievalToolCallValidationResult,
    StructuredKeyValidationResult,
    RetrievalContextSufficiencyResult,
    RouteValidationResult,
    SituationGraphValidationResult,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
)
from app.graph.node_support import model_from_state
from app.graph.state import LaroGraphState
from app.policies.routing_policy import CONTEXT_ORDER


def start_router(state: LaroGraphState) -> str:
    """Always start with outer request classification; event loading lives outside the graph."""

    return "entry_route_classifier"


def after_entry_route_router(state: LaroGraphState) -> str:
    """Normalize ordinary input or directly build a plan for special routes."""

    if state.get("failure_requested"):
        return "workflow_failure"
    decision = model_from_state(state, "entry_route_decision", EntryRouteDecision)
    if decision.route != "NORMAL_FORMULATION":
        return "orchestration_plan_builder"
    if decision.normalization_strategy == "LLM_ROUTER":
        return "request_router_llm"
    if decision.normalization_strategy == "LLM":
        return "input_normalizer_llm"
    return "structured_request_normalizer"


def _supervisor_node(state: LaroGraphState) -> str:
    """Return the only post-normalization supervisor used outside llm_router.

    ``force_rule`` and ``force_agent`` may still need one LLM normalization
    call for natural-language input, but their branch decision is deterministic.
    Trusted structured force_agent input also uses this helper so the router LLM
    can be skipped without weakening the route lock.
    """

    decision = model_from_state(state, "entry_route_decision", EntryRouteDecision)
    return (
        "deterministic_formulation_supervisor"
        if decision.supervisor_strategy == "DETERMINISTIC"
        else "workflow_failure"
    )


def after_input_normalizer_router(state: LaroGraphState) -> str:
    """Continue force_rule input or pause for an incident decision before route locking."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if _incident_actions_pending(state):
        return "incident_immediate_action_executor"
    if state.get("pending_human_interaction") is not None:
        return "human_interaction_pause"
    gate = state.get("request_gate_decision")
    if gate is not None:
        action = getattr(gate, "action", None) if not isinstance(gate, dict) else gate.get("action")
        if action == "REJECT_INPUT":
            return "input_rejected"
        if action == "HOLD_WORKFLOW":
            return "workflow_hold"
        if action == "HANDLE_INCIDENT":
            return "incident_handled"
    return _supervisor_node(state)


def after_structured_normalizer_router(state: LaroGraphState) -> str:
    """Run the deterministic supervisor or pause for an incident decision."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if _incident_actions_pending(state):
        return "incident_immediate_action_executor"
    if state.get("pending_human_interaction") is not None:
        return "human_interaction_pause"
    gate = state.get("request_gate_decision")
    if gate is not None:
        action = getattr(gate, "action", None) if not isinstance(gate, dict) else gate.get("action")
        if action == "REJECT_INPUT":
            return "input_rejected"
        if action == "HOLD_WORKFLOW":
            return "workflow_hold"
        if action == "HANDLE_INCIDENT":
            return "incident_handled"
    return _supervisor_node(state)


def _incident_actions_pending(state: LaroGraphState) -> bool:
    plan = state.get("incident_response_plan")
    if plan is None:
        return False
    actions = getattr(plan, "immediate_actions", None)
    if actions is None and isinstance(plan, dict):
        actions = plan.get("immediate_actions", [])
    return any(
        (getattr(value, "execution_status", None) if not isinstance(value, dict) else value.get("execution_status"))
        == "PLANNED"
        for value in (actions or [])
    )


def after_request_router_llm_router(state: LaroGraphState) -> str:
    """Apply immediate incident safety actions, then pause or lock one branch."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if _incident_actions_pending(state):
        return "incident_immediate_action_executor"
    gate = model_from_state(state, "request_gate_decision", RequestGateDecision)
    if gate.action in {"ASK_CLARIFICATION", "REQUIRE_HUMAN_APPROVAL"}:
        return "human_interaction_pause"
    if gate.action == "REJECT_INPUT":
        return "input_rejected"
    if gate.action == "HOLD_WORKFLOW":
        return "workflow_hold"
    if gate.action == "HANDLE_INCIDENT":
        return "incident_handled"
    return "orchestration_plan_builder"


def after_incident_response_router(state: LaroGraphState) -> str:
    """Pause only for a real human decision; otherwise continue automatically."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("pending_human_interaction") is not None:
        return "human_interaction_pause"
    if state.get("request_gate_decision") is not None:
        gate = model_from_state(state, "request_gate_decision", RequestGateDecision)
        if gate.action in {"ASK_CLARIFICATION", "REQUIRE_HUMAN_APPROVAL"}:
            return "human_interaction_pause"
        if gate.action == "REJECT_INPUT":
            return "input_rejected"
        if gate.action == "HOLD_WORKFLOW":
            return "workflow_hold"
        if gate.action == "HANDLE_INCIDENT":
            return "incident_handled"
    # force_rule normalizers have not built a FormulationDecision yet.
    if state.get("formulation_decision") is None and state.get("request_gate_decision") is None:
        return _supervisor_node(state)
    return "orchestration_plan_builder"


def after_formulation_supervisor_router(state: LaroGraphState) -> str:
    """Stop for true ambiguity/review or build the final workflow plan."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("input_rejection") is not None:
        return "input_rejected"
    if state.get("clarification") is not None:
        return "clarification_required"
    if state.get("human_review") is not None:
        return "human_review"
    decision = model_from_state(state, "formulation_decision", FormulationDecision)
    if decision.route == "ASK_CLARIFICATION":
        return "clarification_required"
    if decision.route == "HUMAN_REVIEW":
        return "human_review"
    return "orchestration_plan_builder"


def _next_legacy_context(state: LaroGraphState) -> str | None:
    """Choose the next LangGraph node from the current typed state."""
    plan = model_from_state(state, "orchestration_plan", OrchestrationPlan)
    completed = set(state.get("completed_context_nodes", []))
    for name in CONTEXT_ORDER:
        if name in plan.selected_context_nodes and name not in completed:
            return name
    return None


def after_plan_router(state: LaroGraphState) -> str:
    """Send Rule input to direct contexts and Agent input to the stepwise Tool loop."""

    if state.get("failure_requested"):
        return "workflow_failure"
    plan = model_from_state(state, "orchestration_plan", OrchestrationPlan)
    if plan.route == "RULE_MISSION_PIPELINE":
        # Keep canonical-ID validation in front of the integrated G2P compiler.
        return "structured_key_validator"
    if plan.route == "AGENT_MISSION_PIPELINE":
        return (
            "canonical_retrieval_key_builder"
            if plan.retrieval_strategy == "PARALLEL_TOOL_PLAN"
            else "llm_retrieval_agent"
        )
    return _next_legacy_context(state) or "context_snapshot_finalize"


def after_canonical_retrieval_key_builder_router(state: LaroGraphState) -> str:
    """Optionally call the LLM for extra reads, then validate the merged DAG."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("retrieval_planner_skipped"):
        return "parallel_retrieval_plan_validator"
    return "llm_retrieval_planner"


def after_structured_key_validation_router(state: LaroGraphState) -> str:
    """Continue the direct Rule fast path or route unknown structured IDs."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("input_rejection") is not None:
        return "input_rejected"
    if state.get("clarification") is not None:
        return "clarification_required"
    if state.get("human_review") is not None:
        return "human_review"
    result = model_from_state(state, "structured_key_validation", StructuredKeyValidationResult)
    if not result.valid:
        return "workflow_failure"
    return _next_legacy_context(state) or "context_snapshot_finalize"


def after_retrieval_agent_router(state: LaroGraphState) -> str:
    """Route one LLM retrieval action."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("clarification") is not None:
        return "clarification_required"
    if state.get("human_review") is not None:
        return "human_review"
    step = model_from_state(state, "retrieval_agent_step", RetrievalAgentStep)
    return {
        "CALL_TOOL": "retrieval_tool_call_validator",
        "FINALIZE_RETRIEVAL": "retrieval_context_sufficiency_guard",
        "ASK_CLARIFICATION": "clarification_required",
        "HUMAN_REVIEW": "human_review",
    }[step.action]


def after_retrieval_tool_call_validation_router(state: LaroGraphState) -> str:
    """Resolve one safe Tool call or ask the LLM to repair it once."""

    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(
        state,
        "retrieval_tool_call_validation",
        RetrievalToolCallValidationResult,
    )
    if result.valid:
        return "query_key_resolver"
    if int(state.get("retrieval_agent_retry_count", 0)) < 1:
        return "retrieval_agent_retry_prepare"
    return "human_review"


def after_stepwise_key_resolution_router(state: LaroGraphState) -> str:
    """Distinguish user ambiguity from an LLM-invented identifier."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("input_rejection") is not None:
        return "input_rejected"
    if state.get("current_ambiguous_references"):
        return "in_route_human_interaction"
    if state.get("current_user_not_found_references"):
        return "input_rejected"
    if state.get("current_not_found_references"):
        if int(state.get("retrieval_agent_retry_count", 0)) < 1:
            return "retrieval_agent_retry_prepare"
        return "human_review"
    return "retrieval_tool_executor"


def after_retrieval_tool_execution_router(state: LaroGraphState) -> str:
    """Retry one transient adapter failure, otherwise assess evidence sufficiency."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("retrieval_tool_error") is not None:
        return "retrieval_tool_retry_prepare" if int(state.get("retrieval_tool_retry_count", 0)) < 1 else "workflow_failure"
    return "retrieval_context_sufficiency_guard"


def after_stepwise_retrieval_sufficiency_router(state: LaroGraphState) -> str:
    """Loop for one more Tool, materialize contexts, or stop on typed business outcomes."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("clarification") is not None:
        return "clarification_required"
    if state.get("human_review") is not None:
        return "human_review"
    result = model_from_state(
        state,
        "retrieval_context_sufficiency",
        RetrievalContextSufficiencyResult,
    )
    if result.ready:
        return "agent_context_materializer"
    if result.retryable and int(state.get("retrieval_agent_step_count", 0)) < int(state.get("max_agent_steps", 6)):
        return "llm_retrieval_agent"
    if result.ambiguous_references or result.not_found_references:
        return "in_route_human_interaction"
    return "human_review"


def after_parallel_retrieval_executor_router(state: LaroGraphState) -> str:
    """Materialize complete observations or terminate on typed retrieval outcomes."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("input_rejection") is not None:
        return "input_rejected"
    if state.get("current_ambiguous_references"):
        return "in_route_human_interaction"
    result = state.get("parallel_retrieval_execution")
    if result is not None and not getattr(result, "valid", False):
        return "human_review"
    return "agent_context_materializer"


def after_context_router(state: LaroGraphState) -> str:
    """Continue legacy prebuilt/recovery/query context collection exactly once."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("workflow_hold") is not None:
        return "workflow_hold"
    return _next_legacy_context(state) or "context_snapshot_finalize"


def after_snapshot_router(state: LaroGraphState) -> str:
    """Build a situation graph for normal formulation or enter legacy routes."""

    if state.get("failure_requested"):
        return "workflow_failure"
    plan = model_from_state(state, "orchestration_plan", OrchestrationPlan)
    if plan.route == "RULE_MISSION_PIPELINE":
        return "rule_cuopt_formulator_direct"
    if plan.route == "AGENT_MISSION_PIPELINE":
        return "warehouse_situation_graph_builder"
    return {
        "PREBUILT_MISSION_PIPELINE": "policy_validation",
        "EXECUTION_RECOVERY_PIPELINE": "recovery_planner",
        "QUERY_ONLY": "query_response",
        "NO_ACTION": "no_action",
        "HUMAN_REVIEW": "human_review",
    }[plan.route]


def after_situation_graph_builder_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    return "workflow_failure" if state.get("failure_requested") else "situation_graph_sufficiency_guard"


def after_situation_graph_validation_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    validation = model_from_state(state, "situation_graph_validation", SituationGraphValidationResult)
    if not validation.valid:
        # Missing or contradictory situation-graph evidence is a system/data
        # contract failure.  HITL is reserved for genuine operator decisions.
        return "workflow_failure"
    decision = model_from_state(state, "formulation_decision", FormulationDecision)
    if decision.route != "AGENT_FORMULATION":
        return "workflow_failure"
    return "llm_cuopt_formulator"


def after_cuopt_formulator_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    return "workflow_failure" if state.get("failure_requested") else "cuopt_dynamic_input_validator"


def after_dynamic_input_validation_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    validation = model_from_state(state, "cuopt_dynamic_input_validation", CuOptDynamicInputValidationResult)
    if validation.valid:
        return "pre_optimization_approval_gate"
    draft = model_from_state(state, "cuopt_dynamic_input_draft", CuOptDynamicInputDraft)
    if (
        draft.formulation_source == "llm"
        and validation.repairable
        and int(state.get("formulation_retry_count", 0))
        < get_settings().llm_cuopt_formulation_max_retries
    ):
        return "cuopt_formulation_retry_prepare"
    return "human_review"


def after_pre_optimization_approval_router(state: LaroGraphState) -> str:
    """Pause for an auditable approval or continue with the validated draft."""

    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("pending_human_interaction") is not None:
        return "human_interaction_pause"
    return "optimization_request_from_dynamic_input"


def after_retry_prepare_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    return "workflow_failure" if state.get("failure_requested") else "llm_cuopt_formulator"


def pass_or_failure(state: LaroGraphState, next_node: str) -> str:
    """Internal helper for pass or failure."""
    return "workflow_failure" if state.get("failure_requested") else next_node


def after_recovery_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    return "human_review" if state.get("human_review") is not None else "policy_validation"


def after_policy_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "policy_validation", PolicyValidationResult)
    return "global_inventory_allocator" if result.status == "pass" else "human_review"


def after_inventory_allocation_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "policy_validation", PolicyValidationResult)
    return "optimization_request" if result.status == "pass" else "human_review"


def after_payload_validation_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "payload_validation", PayloadValidationResult)
    return "candidate_space_guard" if result.valid else "workflow_failure"


def after_candidate_space_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "candidate_space_validation", CandidateSpaceValidation)
    if not result.valid:
        return "workflow_failure"
    return "payload_ready" if state["optimization_backend"] == "cuopt_payload_only" else "optimizer"


def after_optimizer_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "optimizer_result", OptimizerResult)
    if result.status == "success":
        return "optimizer_assignment_validator"
    if result.status == "infeasible":
        return "human_review"
    return "workflow_failure"


def after_assignment_router(state: LaroGraphState) -> str:
    """Validate solver output, then enrich G2P post-station execution goals."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "optimizer_assignment_validation", OptimizerAssignmentValidation)
    return "goods_to_person_execution_enricher" if result.valid else "workflow_failure"


def after_goods_to_person_compiler_router(state: LaroGraphState) -> str:
    """Continue from the domain compiler into the one common payload pipeline."""
    if state.get("failure_requested"):
        return "workflow_failure"
    if state.get("input_rejection") is not None:
        return "input_rejected"
    return "cuopt_payload"


def after_goods_to_person_enricher_router(state: LaroGraphState) -> str:
    """Use the unchanged solver result plus deterministic same-AMR post goals."""
    if state.get("failure_requested"):
        return "workflow_failure"
    value = state.get("goods_to_person_route_enrichment")
    if value is not None:
        valid = getattr(value, "valid", None) if not isinstance(value, dict) else value.get("valid")
        if valid is False:
            return "workflow_failure"
    return "terminal_relocation_enricher"


def after_mapf_planner_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    expansion = model_from_state(state, "waypoint_route_expansion", WaypointRouteExpansionResult)
    schedule = model_from_state(state, "traffic_schedule", TrafficScheduleResult)
    return "route_static_validator" if expansion.status == "expanded" and schedule.valid else "human_review"


def after_route_validation_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "route_validation", RouteValidationResult)
    return "mapf_plan_validator" if result.valid else "workflow_failure"


def after_mapf_validation_router(state: LaroGraphState) -> str:
    """Return the next LangGraph node after this validation or execution stage."""
    if state.get("failure_requested"):
        return "workflow_failure"
    result = model_from_state(state, "mapf_validation", MAPFValidationResult)
    return "simulation_plan_builder" if result.valid else "human_review"

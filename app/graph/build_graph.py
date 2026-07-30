"""Build the typed LARO v13.21 HITL-aware single-decision Rule/Agent workflow."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.context_snapshot import context_snapshot_finalize_node
from app.graph.cuopt_formulation import (
    cuopt_dynamic_input_validator_node,
    cuopt_evidence_enricher_node,
    cuopt_formulation_retry_prepare_node,
    llm_cuopt_formulator_node,
    optimization_request_from_dynamic_input_node,
)
from app.graph.entry_routing import entry_route_classifier_node
from app.graph.frontend_explanation import frontend_explanation_node
from app.graph.goods_to_person import (
    goods_to_person_compiler_node,
    goods_to_person_execution_enricher_node,
)
from app.graph.hitl import (
    human_interaction_pause_node,
    in_route_human_interaction_node,
    pre_optimization_approval_gate_node,
)
from app.graph.input_formulation import (
    deterministic_formulation_supervisor_node,
    input_normalizer_llm_node,
    request_router_llm_node,
    structured_request_normalizer_node,
)
from app.graph.inventory_context import inventory_context_node
from app.graph.incident_response import incident_immediate_action_executor_node
from app.graph.map_context import map_context_node
from app.graph.optimization import (
    cuopt_payload_node,
    cuopt_schema_validator_node,
    optimization_request_node,
    optimizer_assignment_validator_node,
    optimizer_node,
    route_static_validator_node,
)
from app.graph.orchestration_plan import orchestration_plan_builder_node
from app.graph.parallel_retrieval import (
    canonical_retrieval_key_builder_node,
    llm_retrieval_planner_node,
    parallel_retrieval_executor_node,
    parallel_retrieval_plan_validator_node,
)
from app.graph.policy_validation import policy_validation_node
from app.graph.recovery import recovery_planner_node
from app.graph.rule_direct import (
    rule_cuopt_formulator_direct_node,
    structured_key_validator_node,
)
from app.graph.stepwise_retrieval import (
    agent_context_materializer_node,
    llm_retrieval_agent_node,
    query_key_resolver_node,
    retrieval_agent_retry_prepare_node,
    retrieval_context_sufficiency_guard_node,
    retrieval_tool_call_validator_node,
    retrieval_tool_executor_node,
    retrieval_tool_retry_prepare_node,
)
from app.graph.robot_runtime import robot_runtime_node
from app.graph.routes import (
    after_assignment_router,
    after_candidate_space_router,
    after_canonical_retrieval_key_builder_router,
    after_context_router,
    after_cuopt_formulator_router,
    after_dynamic_input_validation_router,
    after_entry_route_router,
    after_formulation_supervisor_router,
    after_goods_to_person_compiler_router,
    after_goods_to_person_enricher_router,
    after_input_normalizer_router,
    after_incident_response_router,
    after_request_router_llm_router,
    after_inventory_allocation_router,
    after_mapf_planner_router,
    after_mapf_validation_router,
    after_optimizer_router,
    after_payload_validation_router,
    after_plan_router,
    after_parallel_retrieval_executor_router,
    after_pre_optimization_approval_router,
    after_policy_router,
    after_recovery_router,
    after_retrieval_agent_router,
    after_retrieval_tool_call_validation_router,
    after_retrieval_tool_execution_router,
    after_stepwise_key_resolution_router,
    after_stepwise_retrieval_sufficiency_router,
    after_structured_key_validation_router,
    after_route_validation_router,
    after_situation_graph_builder_router,
    after_situation_graph_validation_router,
    after_snapshot_router,
    after_structured_normalizer_router,
    after_retry_prepare_router,
    pass_or_failure,
    start_router,
)
from app.graph.simulation_plan import simulation_plan_builder_node
from app.graph.terminal_relocation import terminal_relocation_enricher_node
from app.graph.situation_graph import (
    situation_graph_sufficiency_guard_node,
    warehouse_situation_graph_builder_node,
)
from app.graph.state import LaroGraphState, LaroInputState, LaroOutputState
from app.graph.terminal import (
    clarification_required_node,
    dashboard_event_node,
    human_review_node,
    incident_handled_node,
    input_rejected_node,
    no_action_node,
    payload_ready_node,
    persist_result_node,
    query_response_node,
    workflow_failure_node,
    workflow_hold_node,
)
from app.graph.v9_planning import (
    candidate_space_guard_node,
    global_inventory_allocator_node,
    mapf_plan_validator_node,
    prioritized_mapf_planner_node,
)


def build_laro_graph():
    """Compile one route-locked Rule/Agent graph with integrated G2P, common solver, and MAPF."""

    graph = StateGraph(LaroGraphState, input_schema=LaroInputState, output_schema=LaroOutputState)
    for name, node in {
        "entry_route_classifier": entry_route_classifier_node,
        "structured_request_normalizer": structured_request_normalizer_node,
        "input_normalizer_llm": input_normalizer_llm_node,
        "request_router_llm": request_router_llm_node,
        "incident_immediate_action_executor": incident_immediate_action_executor_node,
        "deterministic_formulation_supervisor": deterministic_formulation_supervisor_node,
        "orchestration_plan_builder": orchestration_plan_builder_node,
        "human_interaction_pause": human_interaction_pause_node,
        "in_route_human_interaction": in_route_human_interaction_node,
        "pre_optimization_approval_gate": pre_optimization_approval_gate_node,
        "structured_key_validator": structured_key_validator_node,
        "canonical_retrieval_key_builder": canonical_retrieval_key_builder_node,
        "llm_retrieval_planner": llm_retrieval_planner_node,
        "parallel_retrieval_plan_validator": parallel_retrieval_plan_validator_node,
        "parallel_retrieval_executor": parallel_retrieval_executor_node,
        "llm_retrieval_agent": llm_retrieval_agent_node,
        "retrieval_tool_call_validator": retrieval_tool_call_validator_node,
        "query_key_resolver": query_key_resolver_node,
        "retrieval_tool_executor": retrieval_tool_executor_node,
        "retrieval_context_sufficiency_guard": retrieval_context_sufficiency_guard_node,
        "agent_context_materializer": agent_context_materializer_node,
        "retrieval_agent_retry_prepare": retrieval_agent_retry_prepare_node,
        "retrieval_tool_retry_prepare": retrieval_tool_retry_prepare_node,
        # Legacy context nodes remain for prebuilt/recovery/query routes.
        "inventory_context": inventory_context_node,
        "map_context": map_context_node,
        "robot_runtime": robot_runtime_node,
        "context_snapshot_finalize": context_snapshot_finalize_node,
        "warehouse_situation_graph_builder": warehouse_situation_graph_builder_node,
        "situation_graph_sufficiency_guard": situation_graph_sufficiency_guard_node,
        "rule_cuopt_formulator_direct": rule_cuopt_formulator_direct_node,
        "llm_cuopt_formulator": llm_cuopt_formulator_node,
        "cuopt_evidence_enricher": cuopt_evidence_enricher_node,
        "cuopt_dynamic_input_validator": cuopt_dynamic_input_validator_node,
        "cuopt_formulation_retry_prepare": cuopt_formulation_retry_prepare_node,
        "optimization_request_from_dynamic_input": optimization_request_from_dynamic_input_node,
        "recovery_planner": recovery_planner_node,
        "policy_validation": policy_validation_node,
        "global_inventory_allocator": global_inventory_allocator_node,
        "goods_to_person_compiler": goods_to_person_compiler_node,
        "goods_to_person_execution_enricher": goods_to_person_execution_enricher_node,
        "terminal_relocation_enricher": terminal_relocation_enricher_node,
        "optimization_request": optimization_request_node,
        "cuopt_payload": cuopt_payload_node,
        "cuopt_schema_validator": cuopt_schema_validator_node,
        "candidate_space_guard": candidate_space_guard_node,
        "optimizer": optimizer_node,
        "optimizer_assignment_validator": optimizer_assignment_validator_node,
        "prioritized_mapf_planner": prioritized_mapf_planner_node,
        "route_static_validator": route_static_validator_node,
        "mapf_plan_validator": mapf_plan_validator_node,
        "simulation_plan_builder": simulation_plan_builder_node,
        "query_response": query_response_node,
        "no_action": no_action_node,
        "incident_handled": incident_handled_node,
        "input_rejected": input_rejected_node,
        "workflow_hold": workflow_hold_node,
        "clarification_required": clarification_required_node,
        "human_review": human_review_node,
        "workflow_failure": workflow_failure_node,
        "payload_ready": payload_ready_node,
        "frontend_explanation": frontend_explanation_node,
        "persist_result": persist_result_node,
        "dashboard_event": dashboard_event_node,
    }.items():
        graph.add_node(name, node)

    graph.add_conditional_edges(START, start_router, {"entry_route_classifier": "entry_route_classifier"})
    graph.add_conditional_edges(
        "entry_route_classifier",
        after_entry_route_router,
        {
            "structured_request_normalizer": "structured_request_normalizer",
            "input_normalizer_llm": "input_normalizer_llm",
            "request_router_llm": "request_router_llm",
            "orchestration_plan_builder": "orchestration_plan_builder",
            "workflow_failure": "workflow_failure",
        },
    )
    post_supervisor_mapping = {
        "orchestration_plan_builder": "orchestration_plan_builder",
        "clarification_required": "clarification_required",
        "human_review": "human_review",
        "workflow_failure": "workflow_failure",
    }
    graph.add_conditional_edges(
        "request_router_llm",
        after_request_router_llm_router,
        {
            "incident_immediate_action_executor": "incident_immediate_action_executor",
            "incident_handled": "incident_handled",
            "input_rejected": "input_rejected",
            "workflow_hold": "workflow_hold",
            "orchestration_plan_builder": "orchestration_plan_builder",
            "human_interaction_pause": "human_interaction_pause",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "incident_immediate_action_executor",
        after_incident_response_router,
        {
            "deterministic_formulation_supervisor": "deterministic_formulation_supervisor",
            "incident_handled": "incident_handled",
            "input_rejected": "input_rejected",
            "workflow_hold": "workflow_hold",
            "orchestration_plan_builder": "orchestration_plan_builder",
            "human_interaction_pause": "human_interaction_pause",
            "workflow_failure": "workflow_failure",
        },
    )

    supervisor_mapping = {
        "incident_immediate_action_executor": "incident_immediate_action_executor",
        "incident_handled": "incident_handled",
        "input_rejected": "input_rejected",
        "workflow_hold": "workflow_hold",
        "human_interaction_pause": "human_interaction_pause",
        "deterministic_formulation_supervisor": "deterministic_formulation_supervisor",
        "workflow_failure": "workflow_failure",
    }
    graph.add_conditional_edges(
        "structured_request_normalizer",
        after_structured_normalizer_router,
        supervisor_mapping,
    )
    graph.add_conditional_edges(
        "input_normalizer_llm",
        after_input_normalizer_router,
        supervisor_mapping,
    )
    graph.add_conditional_edges(
        "deterministic_formulation_supervisor",
        after_formulation_supervisor_router,
        post_supervisor_mapping,
    )

    graph.add_conditional_edges(
        "orchestration_plan_builder",
        after_plan_router,
        {
            "structured_key_validator": "structured_key_validator",
            "canonical_retrieval_key_builder": "canonical_retrieval_key_builder",
            "llm_retrieval_planner": "llm_retrieval_planner",
            "llm_retrieval_agent": "llm_retrieval_agent",
            "inventory_context": "inventory_context",
            "map_context": "map_context",
            "robot_runtime": "robot_runtime",
            "context_snapshot_finalize": "context_snapshot_finalize",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "structured_key_validator",
        after_structured_key_validation_router,
        {
            "inventory_context": "inventory_context",
            "clarification_required": "clarification_required",
            "input_rejected": "input_rejected",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "canonical_retrieval_key_builder",
        after_canonical_retrieval_key_builder_router,
        {
            "llm_retrieval_planner": "llm_retrieval_planner",
            "parallel_retrieval_plan_validator": "parallel_retrieval_plan_validator",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "llm_retrieval_planner",
        lambda state: "workflow_failure" if state.get("failure_requested") else "parallel_retrieval_plan_validator",
        {
            "parallel_retrieval_plan_validator": "parallel_retrieval_plan_validator",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "parallel_retrieval_plan_validator",
        lambda state: "workflow_failure" if state.get("failure_requested") else "parallel_retrieval_executor",
        {
            "parallel_retrieval_executor": "parallel_retrieval_executor",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "parallel_retrieval_executor",
        after_parallel_retrieval_executor_router,
        {
            "agent_context_materializer": "agent_context_materializer",
            "in_route_human_interaction": "in_route_human_interaction",
            "input_rejected": "input_rejected",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "llm_retrieval_agent",
        after_retrieval_agent_router,
        {
            "retrieval_tool_call_validator": "retrieval_tool_call_validator",
            "retrieval_context_sufficiency_guard": "retrieval_context_sufficiency_guard",
            "clarification_required": "clarification_required",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "retrieval_tool_call_validator",
        after_retrieval_tool_call_validation_router,
        {
            "query_key_resolver": "query_key_resolver",
            "retrieval_agent_retry_prepare": "retrieval_agent_retry_prepare",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "query_key_resolver",
        after_stepwise_key_resolution_router,
        {
            "retrieval_tool_executor": "retrieval_tool_executor",
            "retrieval_agent_retry_prepare": "retrieval_agent_retry_prepare",
            "in_route_human_interaction": "in_route_human_interaction",
            "input_rejected": "input_rejected",
            "clarification_required": "clarification_required",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "retrieval_tool_executor",
        after_retrieval_tool_execution_router,
        {
            "retrieval_context_sufficiency_guard": "retrieval_context_sufficiency_guard",
            "retrieval_tool_retry_prepare": "retrieval_tool_retry_prepare",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "retrieval_tool_retry_prepare",
        lambda state: "workflow_failure" if state.get("failure_requested") else "retrieval_tool_executor",
        {"retrieval_tool_executor": "retrieval_tool_executor", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "retrieval_context_sufficiency_guard",
        after_stepwise_retrieval_sufficiency_router,
        {
            "agent_context_materializer": "agent_context_materializer",
            "llm_retrieval_agent": "llm_retrieval_agent",
            "in_route_human_interaction": "in_route_human_interaction",
            "input_rejected": "input_rejected",
            "clarification_required": "clarification_required",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "agent_context_materializer",
        lambda state: "workflow_failure" if state.get("failure_requested") else "context_snapshot_finalize",
        {"context_snapshot_finalize": "context_snapshot_finalize", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "retrieval_agent_retry_prepare",
        lambda state: "workflow_failure" if state.get("failure_requested") else "llm_retrieval_agent",
        {"llm_retrieval_agent": "llm_retrieval_agent", "workflow_failure": "workflow_failure"},
    )
    graph.add_edge("in_route_human_interaction", "human_interaction_pause")

    context_mapping = {
        "inventory_context": "inventory_context",
        "map_context": "map_context",
        "robot_runtime": "robot_runtime",
        "context_snapshot_finalize": "context_snapshot_finalize",
        "workflow_hold": "workflow_hold",
        "workflow_failure": "workflow_failure",
    }
    for context_node in ["inventory_context", "map_context", "robot_runtime"]:
        graph.add_conditional_edges(context_node, after_context_router, context_mapping)

    graph.add_conditional_edges(
        "context_snapshot_finalize",
        after_snapshot_router,
        {
            "rule_cuopt_formulator_direct": "rule_cuopt_formulator_direct",
            "warehouse_situation_graph_builder": "warehouse_situation_graph_builder",
            "policy_validation": "policy_validation",
            "recovery_planner": "recovery_planner",
            "query_response": "query_response",
            "no_action": "no_action",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "warehouse_situation_graph_builder",
        after_situation_graph_builder_router,
        {"situation_graph_sufficiency_guard": "situation_graph_sufficiency_guard", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "situation_graph_sufficiency_guard",
        after_situation_graph_validation_router,
        {
            "llm_cuopt_formulator": "llm_cuopt_formulator",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "rule_cuopt_formulator_direct",
        after_cuopt_formulator_router,
        {"cuopt_dynamic_input_validator": "cuopt_dynamic_input_validator", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "llm_cuopt_formulator",
        after_cuopt_formulator_router,
        {"cuopt_dynamic_input_validator": "cuopt_evidence_enricher", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "cuopt_evidence_enricher",
        lambda state: pass_or_failure(state, "cuopt_dynamic_input_validator"),
        {"cuopt_dynamic_input_validator": "cuopt_dynamic_input_validator", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "cuopt_dynamic_input_validator",
        after_dynamic_input_validation_router,
        {
            "pre_optimization_approval_gate": "pre_optimization_approval_gate",
            "optimization_request_from_dynamic_input": "optimization_request_from_dynamic_input",
            "cuopt_formulation_retry_prepare": "cuopt_formulation_retry_prepare",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "cuopt_formulation_retry_prepare",
        after_retry_prepare_router,
        {"llm_cuopt_formulator": "llm_cuopt_formulator", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "pre_optimization_approval_gate",
        after_pre_optimization_approval_router,
        {
            "human_interaction_pause": "human_interaction_pause",
            "optimization_request_from_dynamic_input": "optimization_request_from_dynamic_input",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "optimization_request_from_dynamic_input",
        lambda state: pass_or_failure(state, "goods_to_person_compiler"),
        {"goods_to_person_compiler": "goods_to_person_compiler", "workflow_failure": "workflow_failure"},
    )

    graph.add_conditional_edges(
        "recovery_planner",
        after_recovery_router,
        {"policy_validation": "policy_validation", "human_review": "human_review", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "policy_validation",
        after_policy_router,
        {"global_inventory_allocator": "global_inventory_allocator", "human_review": "human_review", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "global_inventory_allocator",
        after_inventory_allocation_router,
        {"optimization_request": "optimization_request", "human_review": "human_review", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "optimization_request",
        lambda state: pass_or_failure(state, "goods_to_person_compiler"),
        {"goods_to_person_compiler": "goods_to_person_compiler", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "goods_to_person_compiler",
        after_goods_to_person_compiler_router,
        {
            "cuopt_payload": "cuopt_payload",
            "input_rejected": "input_rejected",
            "workflow_failure": "workflow_failure",
        },
    )

    graph.add_conditional_edges(
        "cuopt_payload",
        lambda state: pass_or_failure(state, "cuopt_schema_validator"),
        {"cuopt_schema_validator": "cuopt_schema_validator", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "cuopt_schema_validator",
        after_payload_validation_router,
        {"candidate_space_guard": "candidate_space_guard", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "candidate_space_guard",
        after_candidate_space_router,
        {"payload_ready": "payload_ready", "optimizer": "optimizer", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "optimizer",
        after_optimizer_router,
        {
            "optimizer_assignment_validator": "optimizer_assignment_validator",
            "human_review": "human_review",
            "workflow_failure": "workflow_failure",
        },
    )
    graph.add_conditional_edges(
        "optimizer_assignment_validator",
        after_assignment_router,
        {"goods_to_person_execution_enricher": "goods_to_person_execution_enricher", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "goods_to_person_execution_enricher",
        after_goods_to_person_enricher_router,
        {"terminal_relocation_enricher": "terminal_relocation_enricher", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "terminal_relocation_enricher",
        lambda state: "workflow_failure" if state.get("failure_requested") else "prioritized_mapf_planner",
        {"prioritized_mapf_planner": "prioritized_mapf_planner", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "prioritized_mapf_planner",
        after_mapf_planner_router,
        {"route_static_validator": "route_static_validator", "human_review": "human_review", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "route_static_validator",
        after_route_validation_router,
        {"mapf_plan_validator": "mapf_plan_validator", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "mapf_plan_validator",
        after_mapf_validation_router,
        {"simulation_plan_builder": "simulation_plan_builder", "human_review": "human_review", "workflow_failure": "workflow_failure"},
    )
    graph.add_conditional_edges(
        "simulation_plan_builder",
        lambda state: "workflow_failure" if state.get("failure_requested") else "frontend_explanation",
        {"frontend_explanation": "frontend_explanation", "workflow_failure": "workflow_failure"},
    )
    graph.add_edge("payload_ready", "frontend_explanation")
    for terminal in [
        "query_response",
        "no_action",
        "incident_handled",
        "input_rejected",
        "workflow_hold",
        "clarification_required",
        "human_review",
        "human_interaction_pause",
        "workflow_failure",
    ]:
        graph.add_edge(terminal, "frontend_explanation")
    graph.add_edge("frontend_explanation", "persist_result")
    graph.add_edge("persist_result", "dashboard_event")
    graph.add_edge("dashboard_event", END)
    return graph.compile()


@lru_cache
def get_laro_graph():
    """Return the cached compiled graph."""

    return build_laro_graph()


def run_laro_graph(initial_state: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke the graph with optional LangSmith run configuration."""

    return get_laro_graph().invoke(initial_state, config=config or {})

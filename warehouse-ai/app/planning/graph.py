from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph

from app.models import NaturalLanguageCommand
from app.planning.nodes import (
    activate_plan_node,
    audit_finalizer_node,
    build_optimization_problem_node,
    build_snapshot_node,
    clarification_node,
    collision_avoidance_node,
    complete_replan_node,
    decide_scope_node,
    dispatch_plan_node,
    execution_precheck_node,
    finalize_failed_command_audit,
    generate_final_report_node,
    finalize_conversation_node,
    interpret_command_node,
    inventory_precheck_node,
    inventory_timeline_validation_node,
    load_conversation_context_node,
    optimizer_node,
    persist_result_node,
    prepare_replan_node,
    route_by_command_node,
    resolve_conversation_context_node,
    select_required_tasks_node,
    simulation_node,
    start_command_audit,
    supervisor_node,
    terminate_replan_node,
    validate_plan_node,
    validate_simulation_node,
    verification_agent_node,
)
from app.state import PlanningState


def after_interpret(state: PlanningState) -> Literal["report", "snapshot"]:
    missing = state.get("interpretation", {}).get("missing_information", [])
    return "report" if missing else "snapshot"


def after_supervisor(state: PlanningState) -> Literal["report", "snapshot"]:
    decision = state.get("supervisor_decision", {})
    if decision.get("requires_clarification"):
        # Direct unit-level callers and legacy interpretation failures keep the
        # historical short path. Real ambiguous commands first take a read-only
        # Snapshot so options can contain only currently valid entities.
        return (
            "snapshot"
            if state.get("command")
            and state.get("final_status") != "INTERPRETATION_FAILED"
            else "report"
        )
    return "report" if decision.get("next_node") == "REPORT" else "snapshot"


def after_snapshot(state: PlanningState) -> Literal["clarification", "route"]:
    return (
        "clarification"
        if state.get("supervisor_decision", {}).get("requires_clarification")
        else "route"
    )


def after_route_by_command(
    state: PlanningState,
) -> Literal["report", "scope"]:
    if not state.get("validation", {}).get("valid", False):
        return "report"
    if state.get("interpretation", {}).get("command_kind") == "QUERY":
        return "report"
    if state.get("scope", {}).get("plan_mode") == "NO_REPLAN":
        return "report"
    return "scope"


def after_scope(state: PlanningState) -> Literal["clarification", "inventory_precheck"]:
    missing = state.get("interpretation", {}).get("missing_information", [])
    return "clarification" if missing else "inventory_precheck"


def after_inventory_precheck(
    state: PlanningState,
) -> Literal["clarification", "select_tasks", "report"]:
    if state.get("interpretation", {}).get("missing_information"):
        return "clarification"
    feasibility = state.get("inventory_feasibility", {})
    if (
        feasibility.get("status") == "FAILED"
        and not feasibility.get("independent_work_ids")
    ):
        return "report"
    return "select_tasks"


def after_inventory_timeline(
    state: PlanningState,
) -> Literal["collision", "report"]:
    validation = state.get("inventory_timeline_validation", {})
    if (
        validation.get("status") == "FAILED"
        and not state.get("cuopt_plan", {}).get("scheduled_tasks")
    ):
        return "report"
    return "collision"


def after_routes(state: PlanningState) -> Literal["validate_plan", "simulate"]:
    if int(state.get("replan_attempt", 0)) > 0:
        return "simulate"
    mode = state.get("interpretation", {}).get("execution_mode")
    return "validate_plan" if mode == "PLAN_ONLY" else "simulate"


def after_select_tasks(state: PlanningState) -> Literal["build_problem", "report"]:
    """Stop before optimization when deterministic schedule validation fails."""
    validation = state.get("schedule_validation", {})
    return "build_problem" if validation.get("valid", False) else "report"


def after_verification(
    state: PlanningState,
) -> Literal["persist", "prepare_replan", "complete_replan", "terminate_replan"]:
    decision = state.get("verification_decision", {}).get("decision")
    attempt = int(state.get("replan_attempt", 0))
    if decision in {"REPLAN_LOCAL", "REPLAN_GLOBAL"}:
        return "prepare_replan"
    if decision in {"PASS", "PASS_WITH_WARNING"} and attempt > 0:
        return "complete_replan"
    if decision in {"CLARIFICATION_REQUIRED", "FAIL"} and attempt > 0:
        return "terminate_replan"
    return "persist"


def after_prepare_replan(state: PlanningState) -> Literal["build_problem", "persist"]:
    return "build_problem" if state.get("replan_ready") else "persist"


def after_persist(state: PlanningState) -> Literal["execution_precheck", "report"]:
    mode = state.get("interpretation", {}).get("execution_mode")
    simulation_valid = state.get("simulation", {}).get("valid", False)
    verification_passed = state.get("verification_decision", {}).get("decision") in {
        "PASS",
        "PASS_WITH_WARNING",
    }
    return (
        "execution_precheck"
        if mode == "EXECUTE" and simulation_valid and verification_passed
        else "report"
    )


def after_execution_precheck(
    state: PlanningState,
) -> Literal["activate", "report"]:
    return "activate" if state.get("execution_ready") else "report"


def after_activate(state: PlanningState) -> Literal["dispatch", "report"]:
    return "dispatch" if state.get("final_status") == "PLAN_ACTIVATED" else "report"


builder = StateGraph(PlanningState)
builder.add_node("conversation_context", load_conversation_context_node)
builder.add_node("interpret", interpret_command_node)
builder.add_node("resolve_conversation", resolve_conversation_context_node)
builder.add_node("supervisor", supervisor_node)
builder.add_node("snapshot", build_snapshot_node)
builder.add_node("clarification", clarification_node)
builder.add_node("route_command", route_by_command_node)
builder.add_node("scope", decide_scope_node)
builder.add_node("inventory_precheck", inventory_precheck_node)
builder.add_node("select_tasks", select_required_tasks_node)
builder.add_node("build_problem", build_optimization_problem_node)
builder.add_node("optimize", optimizer_node)
builder.add_node("inventory_timeline", inventory_timeline_validation_node)
builder.add_node("collision", collision_avoidance_node)
builder.add_node("validate_plan", validate_plan_node)
builder.add_node("simulate", simulation_node)
builder.add_node("validate_simulation", validate_simulation_node)
builder.add_node("verification", verification_agent_node)
builder.add_node("prepare_replan", prepare_replan_node)
builder.add_node("complete_replan", complete_replan_node)
builder.add_node("terminate_replan", terminate_replan_node)
builder.add_node("persist", persist_result_node)
builder.add_node("execution_precheck", execution_precheck_node)
builder.add_node("activate", activate_plan_node)
builder.add_node("dispatch", dispatch_plan_node)
builder.add_node("report", generate_final_report_node)
builder.add_node("audit_finalize", audit_finalizer_node)
builder.add_node("conversation_finalize", finalize_conversation_node)

builder.add_edge(START, "conversation_context")
builder.add_edge("conversation_context", "interpret")
builder.add_edge("interpret", "resolve_conversation")
builder.add_edge("resolve_conversation", "supervisor")
builder.add_conditional_edges(
    "supervisor",
    after_supervisor,
    {"report": "report", "snapshot": "snapshot"},
)
builder.add_conditional_edges(
    "snapshot",
    after_snapshot,
    {"clarification": "clarification", "route": "route_command"},
)
builder.add_edge("clarification", "report")
builder.add_conditional_edges(
    "route_command",
    after_route_by_command,
    {"report": "report", "scope": "scope"},
)
builder.add_conditional_edges(
    "scope",
    after_scope,
    {
        "clarification": "clarification",
        "inventory_precheck": "inventory_precheck",
    },
)
builder.add_conditional_edges(
    "inventory_precheck",
    after_inventory_precheck,
    {
        "clarification": "clarification",
        "select_tasks": "select_tasks",
        "report": "report",
    },
)
builder.add_conditional_edges(
    "select_tasks",
    after_select_tasks,
    {"build_problem": "build_problem", "report": "report"},
)
builder.add_edge("build_problem", "optimize")
builder.add_edge("optimize", "inventory_timeline")
builder.add_conditional_edges(
    "inventory_timeline",
    after_inventory_timeline,
    {"collision": "collision", "report": "report"},
)
builder.add_conditional_edges(
    "collision",
    after_routes,
    {"validate_plan": "validate_plan", "simulate": "simulate"},
)
builder.add_edge("validate_plan", "verification")
builder.add_edge("simulate", "validate_simulation")
builder.add_edge("validate_simulation", "verification")
builder.add_conditional_edges(
    "verification",
    after_verification,
    {
        "persist": "persist",
        "prepare_replan": "prepare_replan",
        "complete_replan": "complete_replan",
        "terminate_replan": "terminate_replan",
    },
)
builder.add_conditional_edges(
    "prepare_replan",
    after_prepare_replan,
    {"build_problem": "build_problem", "persist": "persist"},
)
builder.add_edge("complete_replan", "persist")
builder.add_edge("terminate_replan", "persist")
builder.add_conditional_edges(
    "persist",
    after_persist,
    {"execution_precheck": "execution_precheck", "report": "report"},
)
builder.add_conditional_edges(
    "execution_precheck",
    after_execution_precheck,
    {"activate": "activate", "report": "report"},
)
builder.add_conditional_edges(
    "activate",
    after_activate,
    {"dispatch": "dispatch", "report": "report"},
)
builder.add_edge("dispatch", "report")
builder.add_edge("report", "conversation_finalize")
builder.add_edge("conversation_finalize", "audit_finalize")
builder.add_edge("audit_finalize", END)

planning_graph = builder.compile()


def run_planning(command: NaturalLanguageCommand) -> dict:
    if not command.conversation_id:
        command = command.model_copy(
            update={
                "conversation_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"warehouse-conversation:{command.command_id}",
                    )
                )
            }
        )
    clarification_trace = (
        [
            {
                "node": "clarification_response_received",
                "at": datetime.now(UTC).isoformat(),
                "clarification_id": command.clarification_id,
            },
            {
                "node": "clarification_resolved",
                "at": datetime.now(UTC).isoformat(),
                "clarification_id": command.clarification_id,
            },
        ]
        if command.clarification_id
        else []
    )
    initial: PlanningState = {
        "command": command.model_dump(mode="json"),
        "conversation_id": command.conversation_id,
        "parent_command_id": command.parent_command_id,
        "replan_count": 0,
        "replan_attempt": 0,
        "max_replan_attempts": 0,
        "replan_history": [],
        "last_verification_decision": {},
        "repeated_failure_signatures": {},
        "replan_reason": "",
        "replan_ready": False,
        "route_failure": {},
        "mapf_replan_policy": {},
        "errors": [],
        "warnings": [],
        "supervisor_warnings": [],
        "verification_warnings": [],
        "audit_warnings": [],
        "trace": clarification_trace,
        "final_status": "RECEIVED",
    }
    initial.update(start_command_audit(initial))
    try:
        result = planning_graph.invoke(initial, config={"recursion_limit": 50})
    except Exception as exc:
        finalize_failed_command_audit(initial["command"], exc)
        raise
    return result["response"]

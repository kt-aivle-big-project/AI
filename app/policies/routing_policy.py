"""Deterministic outer routing and post-supervision orchestration planning."""
from __future__ import annotations

from app.core.config import get_settings

from app.domain.schemas import (
    QUERY_EVENT_TYPES,
    ContextNodeName,
    EntryRouteDecision,
    EventInput,
    FormulationDecision,
    MissionSpec,
    NormalizedWarehouseRequest,
    OrchestrationPlan,
    PlanningMode,
    PlanningModeSource,
)

CONTEXT_ORDER: list[ContextNodeName] = ["inventory_context", "map_context", "robot_runtime"]


def resolve_effective_planning_mode(
    *,
    requested_mode: PlanningMode | None,
    default_mode: PlanningMode,
    allow_request_override: bool,
) -> tuple[PlanningMode, PlanningModeSource]:
    """Resolve one run's mode without silently mixing request and environment values."""

    if requested_mode is not None and allow_request_override:
        return requested_mode, "request_override"
    return default_mode, "environment"


def classify_entry_route(
    *,
    request_mode: str,
    planning_mode: PlanningMode,
    events: list[EventInput],
    user_command: str | None,
    mission_spec: MissionSpec | None,
) -> EntryRouteDecision:
    """Classify only special routes and the required normalization/supervision mode."""

    event_types = {event.type for event in events}
    if mission_spec is not None:
        return EntryRouteDecision(
            route="PREBUILT_MISSION_PIPELINE",
            reasons=["An external MissionSpec was supplied."],
        )
    if "robot_recovery_requested" in event_types:
        return EntryRouteDecision(
            route="EXECUTION_RECOVERY_PIPELINE",
            reasons=["A robot recovery event requires the deterministic recovery path."],
        )
    if event_types and event_types.issubset(QUERY_EVENT_TYPES) and not (user_command or "").strip():
        return EntryRouteDecision(
            route="QUERY_ONLY",
            reasons=["Only structured query events were supplied."],
        )
    if not events and not (user_command or "").strip():
        return EntryRouteDecision(route="NO_ACTION", reasons=["No actionable input was supplied."])

    has_command = bool((user_command or "").strip())

    if planning_mode == "llm_router":
        # Every normal mission request, including fully structured events, is
        # normalized and classified by one tool-free LLM call.  The route is
        # finalized before either Rule or Agent begins.
        normalization = "LLM_ROUTER"
        supervisor = "UNIFIED_LLM"
    elif planning_mode == "force_agent":
        # Trusted structured events already contain canonical IDs and do not
        # need a slow router LLM merely to reach a branch that is explicitly
        # forced. Natural-language commands still require one normalization call.
        use_router_llm = get_settings().force_agent_structured_input_router_llm
        if events and not has_command and not use_router_llm:
            normalization = "STRUCTURED"
            supervisor = "DETERMINISTIC"
        else:
            normalization = "LLM"
            supervisor = "DETERMINISTIC"
    else:  # force_rule
        # Force Rule is a deterministic baseline.  Natural language still needs
        # one normalization call, but no LLM route recommendation is used.
        normalization = "LLM" if has_command else "STRUCTURED"
        supervisor = "DETERMINISTIC"

    mode_reason = {
        "llm_router": (
            "One tool-free request-router call will normalize the input and recommend "
            "Rule or Agent before branch execution."
        ),
        "force_agent": (
            "force_agent locks Agent before branch execution. Trusted structured events "
            "use deterministic normalization unless FORCE_AGENT_STRUCTURED_INPUT_ROUTER_LLM=true; "
            "natural-language input still uses one normalization call."
        ),
        "force_rule": (
            "force_rule will normalize the input as needed and lock the deterministic "
            "Rule branch before repository access."
        ),
    }[planning_mode]
    return EntryRouteDecision(
        route="NORMAL_FORMULATION",
        normalization_strategy=normalization,
        supervisor_strategy=supervisor,
        reasons=[mode_reason],
    )


def infer_legacy_contexts(
    *,
    route: str,
    mission_spec: MissionSpec | None,
    user_command: str | None,
) -> list[ContextNodeName]:
    """Return contexts for prebuilt, recovery, and query routes only."""

    selected: set[ContextNodeName] = set()
    if route == "PREBUILT_MISSION_PIPELINE":
        selected.update({"map_context", "robot_runtime"})
        if mission_spec is None or any(task.request_type == "outbound_pick" for task in mission_spec.task_requests):
            selected.add("inventory_context")
    elif route == "EXECUTION_RECOVERY_PIPELINE":
        selected.update({"map_context", "robot_runtime"})
    elif route == "QUERY_ONLY":
        command = (user_command or "").lower()
        if any(term in command for term in ["재고", "inventory", "품목", "item"]):
            selected.add("inventory_context")
        if any(term in command for term in ["로봇", "robot", "battery"]):
            selected.add("robot_runtime")
        if any(term in command for term in ["통로", "edge", "map", "경로"]):
            selected.add("map_context")
        if not selected:
            selected.update(CONTEXT_ORDER)
    return [name for name in CONTEXT_ORDER if name in selected]


def build_final_orchestration_plan(
    *,
    entry: EntryRouteDecision,
    planning_mode: PlanningMode,
    events: list[EventInput],
    user_command: str | None,
    mission_spec: MissionSpec | None,
    normalized_request: NormalizedWarehouseRequest | None,
    formulation_decision: FormulationDecision | None,
    requested_planning_mode: PlanningMode | None = None,
    planning_mode_source: str = "environment",
) -> OrchestrationPlan:
    """Build the authoritative plan after supervision, not before it."""

    if entry.route == "NORMAL_FORMULATION":
        if formulation_decision is None:
            raise ValueError("Normal formulation requires FormulationDecision")
        if formulation_decision.route == "RULE_FORMULATION":
            route = "RULE_MISSION_PIPELINE"
            retrieval = "DIRECT_CONTEXT"
        elif formulation_decision.route == "AGENT_FORMULATION":
            route = "AGENT_MISSION_PIPELINE"
            retrieval = (
                "PARALLEL_TOOL_PLAN"
                if get_settings().agent_retrieval_mode == "parallel_plan"
                else "STEPWISE_TOOL_AGENT"
            )
        elif formulation_decision.route == "HUMAN_REVIEW":
            route = "HUMAN_REVIEW"
            retrieval = "NONE"
        else:
            route = "HUMAN_REVIEW"
            retrieval = "NONE"
        goal = (
            (normalized_request.raw_user_command if normalized_request else None)
            or user_command
            or f"Process {len(normalized_request.operations) if normalized_request else len(events)} warehouse operation(s)."
        )
        return OrchestrationPlan(
            orchestration_goal=str(goal).strip(),
            route=route,
            formulation_route=formulation_decision.route,
            retrieval_strategy=retrieval,
            selected_context_nodes=(list(CONTEXT_ORDER) if route == "RULE_MISSION_PIPELINE" else []),
            selected_retrieval_tools=[],
            routing_reason=[*entry.reasons, *formulation_decision.reasons],
            routing_source=(
                "request_router_llm"
                if entry.supervisor_strategy == "UNIFIED_LLM"
                else "formulation_supervisor"
            ),
            planning_mode=planning_mode,
            requested_planning_mode=requested_planning_mode,
            planning_mode_source=planning_mode_source,
            route_locked=True,
            route_switch_allowed=False,
            needs_optimization=route in {"RULE_MISSION_PIPELINE", "AGENT_MISSION_PIPELINE"},
        )

    route = entry.route
    contexts = infer_legacy_contexts(route=route, mission_spec=mission_spec, user_command=user_command)
    if route == "PREBUILT_MISSION_PIPELINE":
        source = "external_mission"
        goal = "Validate and optimize the externally supplied mission."
    else:
        source = "special_route"
        goal = {
            "EXECUTION_RECOVERY_PIPELINE": "Build a safe recovery mission for the affected robot.",
            "QUERY_ONLY": user_command or "Answer the warehouse status query.",
            "NO_ACTION": "No warehouse action is required.",
            "HUMAN_REVIEW": "Route the request to an operator.",
        }[route]
    return OrchestrationPlan(
        orchestration_goal=goal,
        route=route,
        formulation_route=None,
        retrieval_strategy="LEGACY_CONTEXT" if contexts else "NONE",
        selected_context_nodes=contexts,
        selected_retrieval_tools=[],
        routing_reason=entry.reasons,
        routing_source=source,
        planning_mode=planning_mode,
        requested_planning_mode=requested_planning_mode,
        planning_mode_source=planning_mode_source,
        route_locked=True,
        route_switch_allowed=False,
        needs_optimization=route in {"PREBUILT_MISSION_PIPELINE", "EXECUTION_RECOVERY_PIPELINE"},
    )

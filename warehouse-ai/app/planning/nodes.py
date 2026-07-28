import hashlib
import heapq
import json
import logging
import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.models import (
    AtomicTask,
    ClarificationOption,
    ClarificationRequest,
    CollisionFreePlan,
    CommandInterpretation,
    CuOptPlan,
    FinalReportOutput,
    EmergencyReviewItem,
    InventoryFeasibilityResult,
    InventoryOperationRequest,
    ItemInventoryResult,
    NaturalLanguageCommand,
    OptimizationWeights,
    PlanExecutionApprovalRequest,
    ReplanHistoryEntry,
    ScenarioDefinition,
    ScopeDecision,
    SimulationIssue,
    SimulationResult,
    SupervisorDecision,
    TaskDependency,
    TaskScheduleConstraint,
    VerificationDecision,
)
from app.prompts import (
    COMMAND_SUPERVISOR_PROMPT,
    FINAL_REPORT_PROMPT,
    FINAL_REPORT_PROMPT_VERSION,
    SCOPE_SUPERVISOR_PROMPT,
    SUPERVISOR_PROMPT,
    SUPERVISOR_PROMPT_VERSION,
    VERIFICATION_PROMPT,
    VERIFICATION_PROMPT_VERSION,
)
from app.services.container import get_services
from app.services.energy_reconciliation import reconcile_plan_energy
from app.services.audit import AuditService, sanitize_log_details
from app.services.base_plan import active_plan_base, base_plan_from_evidence
from app.services.command_language import (
    canonical_robot_id,
    canonical_task_id,
    is_deterministically_supported,
    optimization_weights_for_priority,
    parse_optimization_goal,
    parse_deterministic_command,
    requires_deterministic_clarification,
)
from app.services.optimizer import (
    optimize_problem,
    optimize_problem_locally,
    validate_or_fallback_charge_visit_second_pass,
)
from app.services.inventory_projection import (
    InventoryProjectionService,
    capacity_feasibility,
)
from app.services.inventory_reservations import (
    InventoryReservationConflict,
    InventoryReservationService,
    simulation_reservation_summaries,
)
from app.services.conversation import (
    ConversationAccessError,
    apply_conversation_inheritance,
    compact_conversation_summary,
    constraints_from_interpretation,
    explicitly_discards_base_plan,
)
from app.services.plan_evidence import build_route_evidence
from app.services.robot_gateway import RobotGateway
from app.services.robot_adapter import RobotAdapter
from app.services.execution_delivery import ExecutionDeliveryService
from app.services.task_splitting import (
    capacity_trip_groups,
    capacity_trip_pairs,
    outbound_trip_capacity,
)
from app.services.reporting import (
    build_report_evidence,
    report_evidence_summary,
)
from app.services.user_reporting import (
    build_debug_report_payload,
    build_user_report_summary,
    determine_report_detail_level,
    llm_report_is_supported,
    render_user_report,
    report_payload_for_level,
    report_state_fingerprint,
)
from app.services.routing import build_collision_plan
from app.services.mapf_replan import (
    build_mapf_replan_policy,
    classify_mapf_failure,
)
from app.services.opportunity_charging import augment_plan_with_opportunity_charging
from app.services.charge_visit_optimization import (
    prepare_charge_visit_optimization_problem,
)
from app.services.operational_objective import calculate_operational_objective
from app.services.shared_resources import (
    finalize_idle_resource_reservations,
    schedule_shared_resources,
)
from app.services.charger_selection import (
    expected_opportunity_candidate,
    is_opportunity_policy,
)
from app.services.wait_compression import (
    compact_debug_payload_for_llm,
    compact_route_metadata_for_llm,
    compress_debug_payload_for_presentation,
)
from app.services.simulation import simulate_plan
from app.services.simulation_session import replay_simulation_session
from app.services.scheduling import (
    canonical_work_id,
    parse_schedule_language,
    planned_at,
    reconcile_task_time_window,
    ready_task_ids as calculate_ready_task_ids,
    resolve_warehouse_timezone,
    scope_dependency_graph,
    validate_dependency_graph,
)
from app.services.schedule_dispatcher import ready_only_plan_payload
from app.state import PlanningState
from app.time_utils import as_utc_datetime, task_tardiness_steps


logger = logging.getLogger(__name__)




def _edge_identifier_aliases(edge: dict[str, Any]) -> set[str]:
    """Return all identifiers that may refer to the same topology edge.

    Neo4j stores stable edge IDs such as ``H1_1`` while natural-language
    commands use endpoint aliases such as ``2013->2014``.  Bidirectional
    edges also accept the reverse endpoint alias.
    """
    aliases: set[str] = set()
    edge_id = edge.get("edge_id")
    if edge_id is not None:
        aliases.add(str(edge_id))
    from_node = edge.get("from_node")
    to_node = edge.get("to_node")
    if from_node is None or to_node is None:
        return aliases
    aliases.add(f"{from_node}->{to_node}")
    if str(edge.get("direction") or "").upper() in {"BOTH", "BIDIRECTIONAL"}:
        aliases.add(f"{to_node}->{from_node}")
    return aliases

def trace(node: str, **details: Any) -> list[dict[str, Any]]:
    return [{"node": node, "at": datetime.now(UTC).isoformat(), **details}]


def start_command_audit(state: PlanningState) -> dict[str, Any]:
    try:
        AuditService(get_services().postgres).create_or_get_command_history(
            state["command"]
        )
        return {}
    except Exception as exc:
        message = f"명령 감사 이력 시작 저장 실패: {sanitize_log_details(str(exc))}"
        logger.warning(message)
        return {"audit_warnings": [message]}


def finalize_failed_command_audit(
    command: dict[str, Any],
    error: Exception,
) -> None:
    try:
        AuditService(get_services().postgres).finalize_unhandled_failure(
            command,
            error,
        )
    except Exception as audit_exc:
        logger.warning(
            "처리되지 않은 명령 실패 감사 저장 실패: %s",
            sanitize_log_details(str(audit_exc)),
        )


def build_supervisor_llm() -> ChatOpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=None,
        timeout=settings.request_timeout_seconds,
        max_retries=2,
        use_responses_api=True,
    )


def load_conversation_context_node(state: PlanningState) -> dict[str, Any]:
    command = NaturalLanguageCommand.model_validate(state["command"])
    repository = get_services().postgres
    if not hasattr(repository, "create_or_get_conversation"):
        return {
            "conversation_id": command.conversation_id,
            "parent_command_id": command.parent_command_id,
            "inherited_constraints": {},
            "resolved_constraints": {},
            "conversation_summary": {},
        }
    try:
        session = repository.create_or_get_conversation(
            str(command.conversation_id),
            command.warehouse_id,
        )
    except ValueError as exc:
        raise ConversationAccessError(str(exc)) from exc
    if command.simulation_id and hasattr(repository, "get_simulation_session"):
        simulation = repository.get_simulation_session(command.simulation_id)
        if simulation and int(simulation["warehouse_id"]) != command.warehouse_id:
            raise ConversationAccessError(
                "다른 warehouse의 simulation_id를 참조할 수 없습니다."
            )

    previous_command_id = session.get("active_command_id")
    parent_command_id = command.parent_command_id or previous_command_id
    if parent_command_id == command.command_id:
        existing_link = repository.get_conversation_command_link(
            str(command.conversation_id), command.command_id
        )
        parent_command_id = (
            existing_link.get("parent_command_id") if existing_link else None
        )
    if parent_command_id and hasattr(repository, "get_conversation_command_link"):
        parent_link = repository.get_conversation_command_link(
            str(command.conversation_id),
            str(parent_command_id),
        )
        if parent_link is None:
            raise ConversationAccessError(
                "parent_command_id가 현재 conversation에 속하지 않습니다."
            )
    command.parent_command_id = (
        str(parent_command_id) if parent_command_id else None
    )
    repository.link_conversation_command(
        conversation_id=str(command.conversation_id),
        command_id=command.command_id,
        parent_command_id=command.parent_command_id,
    )
    if hasattr(repository, "update_command_parent"):
        repository.update_command_parent(
            command.command_id,
            command.parent_command_id,
        )
    inherited = dict(session.get("resolved_constraints") or {})
    summary = dict(session.get("summary") or {})
    return {
        "command": command.model_dump(mode="json"),
        "conversation_id": command.conversation_id,
        "parent_command_id": command.parent_command_id,
        "previous_command_id": previous_command_id,
        "active_plan_version": session.get("active_plan_version"),
        "active_simulation_id": session.get("active_simulation_id"),
        "inherited_constraints": inherited,
        "resolved_constraints": inherited,
        "conversation_summary": summary,
        "trace": trace(
            "conversation_context_loaded",
            conversation_id=command.conversation_id,
            previous_command_id=previous_command_id,
            inherited_fields=sorted(inherited),
        ),
    }


def resolve_conversation_context_node(state: PlanningState) -> dict[str, Any]:
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    inherited = dict(state.get("inherited_constraints") or {})
    if explicitly_discards_base_plan(interpretation.objective):
        inherited = {}
    resolved, applied, overridden = apply_conversation_inheritance(
        interpretation,
        inherited,
        active_plan_version=state.get("active_plan_version"),
        active_simulation_id=state.get("active_simulation_id"),
    )
    constraints = dict(inherited)
    constraints.update(constraints_from_interpretation(resolved))
    if overridden.get("target_task_ids") == []:
        constraints.pop("target_task_ids", None)
    if resolved.included_robot_ids:
        constraints["excluded_robot_ids"] = resolved.excluded_robot_ids
    command = dict(state["command"])
    if (
        not command.get("simulation_id")
        and resolved.execution_mode == "SIMULATE_ONLY"
        and "target_reference" in applied
        and state.get("active_simulation_id")
    ):
        command["simulation_id"] = state["active_simulation_id"]
    update: dict[str, Any] = {
        "command": command,
        "interpretation": resolved.model_dump(mode="json"),
        "resolved_constraints": constraints,
        "inherited_constraints": applied,
        "overridden_constraints": overridden,
    }
    if state.get("conversation_summary") or applied or overridden:
        update["trace"] = trace(
            "conversation_context_resolved",
            inherited_fields=sorted(applied),
            overridden_fields=sorted(overridden),
        )
    return update


def finalize_conversation_node(state: PlanningState) -> dict[str, Any]:
    repository = get_services().postgres
    if not hasattr(repository, "update_conversation_session"):
        return {}
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    constraints = dict(state.get("resolved_constraints") or {})
    constraints.update(constraints_from_interpretation(interpretation))
    if interpretation.included_robot_ids:
        constraints["excluded_robot_ids"] = interpretation.excluded_robot_ids
    summary_state = dict(state)
    summary_state["resolved_constraints"] = constraints
    summary = compact_conversation_summary(summary_state)
    clarification_id = state.get("clarification", {}).get("clarification_id")
    verification_passed = state.get("verification_decision", {}).get("decision") in {
        "PASS",
        "PASS_WITH_WARNING",
    }
    active_plan = (
        state.get("plan_version")
        if verification_passed
        else state.get("active_plan_version")
    )
    simulation_valid = bool(state.get("simulation", {}).get("valid"))
    active_simulation = (
        state.get("simulation_id")
        if simulation_valid
        else state.get("active_simulation_id")
    )
    try:
        repository.update_conversation_session(
            str(state["command"].get("conversation_id")),
            {
                "active_command_id": state["command"]["command_id"],
                "active_plan_version": active_plan,
                "active_simulation_id": active_simulation,
                "active_clarification_id": clarification_id,
                "resolved_constraints": constraints,
                "summary": summary,
            },
        )
    except Exception as exc:
        message = f"Conversation 상태 저장 실패: {sanitize_log_details(str(exc))}"
        logger.warning(message)
        return {"audit_warnings": [message]}
    response = dict(state.get("response", {}))
    response["conversation_id"] = state["command"].get("conversation_id")
    response["parent_command_id"] = state["command"].get("parent_command_id")
    return {
        "conversation_summary": summary,
        "resolved_constraints": constraints,
        "response": response,
        "trace": trace(
            "conversation_context_updated",
            conversation_id=state["command"].get("conversation_id"),
            active_plan_version=active_plan,
            active_simulation_id=active_simulation,
        ),
    }


def normalize_command_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("갯수", "개수")).strip()


OPTIMIZATION_PROFILE_LABELS = {
    "MINIMIZE_MAKESPAN": "MAKESPAN",
    "MINIMIZE_DISTANCE": "TOTAL_DISTANCE",
    "MINIMIZE_TARDINESS": "TARDINESS",
    "MINIMIZE_ENERGY": "ENERGY",
    "MINIMIZE_ROBOTS": "ROBOT_ACTIVATION",
    "MINIMIZE_PLAN_CHANGE": "PLAN_CHANGE",
}


def resolve_optimization_weights(
    command_text: str,
) -> tuple[str, OptimizationWeights]:
    """사용자가 명시한 최적화 기준만 결정적 가중치 프로필로 반영한다."""

    priority, weights, _ = parse_optimization_goal(command_text)
    if not priority:
        return "DEFAULT", weights
    profile = "+".join(
        OPTIMIZATION_PROFILE_LABELS.get(name, name)
        for name in priority.split("+")
    )
    return profile, weights


def rule_based_query_interpretation(text: str) -> CommandInterpretation | None:
    deterministic = parse_deterministic_command(text)
    if deterministic.command_kind == "QUERY" and deterministic.intent != "OTHER":
        return deterministic
    if requires_deterministic_clarification(deterministic):
        return deterministic
    normalized = normalize_command_text(text)
    planning_markers = (
        "계획",
        "재계획",
        "배정해",
        "배정하고",
        "시뮬레이션",
        "실행해",
        "옮겨",
        "출고해",
        "입고해",
        "삽입해",
    )
    if any(marker in normalized for marker in planning_markers):
        return None

    target = "NONE"
    intent = "OTHER"
    sql_reads: list[str] = []
    graph_reads: list[str] = []
    if "로봇" in normalized:
        target, intent, sql_reads = "ROBOT", "ROBOT_QUERY", ["ROBOTS"]
    elif any(token in normalized for token in ("재고", "품목", "보관 위치")):
        target, intent, sql_reads = "INVENTORY", "INVENTORY_QUERY", ["INVENTORY"]
    elif any(token in normalized for token in ("작업", "업무")):
        target, intent, sql_reads = "WORK", "WORK_QUERY", ["WORKS"]
    elif any(token in normalized for token in ("지도", "노드", "통로", "구역")):
        target, intent, graph_reads = "MAP", "MAP_QUERY", ["TOPOLOGY"]
    elif any(token in normalized for token in ("시스템", "연결 상태")):
        target, intent = "SYSTEM", "SYSTEM_QUERY"
    if target == "NONE":
        return None

    count_patterns = (
        r"개수",
        r"몇\s*대",
        r"몇\s*개",
        r"\b수\b",
        r"수량",
        r"얼마",
    )
    status_patterns = ("상태", "현황", "사용 가능", "사용가능", "가용", "배터리")
    detail_patterns = ("위치", "상세")
    if any(re.search(pattern, normalized) for pattern in count_patterns):
        action = "COUNT"
    elif any(pattern in normalized for pattern in status_patterns):
        action = "STATUS"
    elif any(pattern in normalized for pattern in detail_patterns):
        action = "DETAIL"
    else:
        action = "LIST"

    return CommandInterpretation(
        command_kind="QUERY",
        intent=intent,
        objective=text.strip(),
        query_target=target,
        query_action=action,
        required_sql_reads=sql_reads,
        required_graph_reads=graph_reads,
        execution_mode="PLAN_ONLY",
        summary=f"{target} {action} 조회",
    )


def interpret_command_node(state: PlanningState) -> dict[str, Any]:
    command = NaturalLanguageCommand.model_validate(state["command"])
    settings = get_settings()
    deterministic = parse_deterministic_command(
        command.text,
        reference_time=command.received_at,
        warehouse_timezone=getattr(settings, "warehouse_timezone", ""),
    )
    legacy_query_fallback = rule_based_query_interpretation(command.text)
    fallback = (
        legacy_query_fallback
        if legacy_query_fallback is not None
        else deterministic
        if (
            is_deterministically_supported(deterministic)
            or requires_deterministic_clarification(deterministic)
        )
        else None
    )
    interpretation: CommandInterpretation | None = None
    warnings: list[str] = []
    errors: list[str] = []
    classification_source = "llm"
    llm_error: Exception | None = None

    if getattr(settings, "openai_api_key", ""):
        try:
            command_payload: dict[str, Any] = command.model_dump(mode="json")
            if state.get("conversation_summary"):
                command_payload = {
                    "command": command_payload,
                    "conversation_summary": state["conversation_summary"],
                }
            structured = build_supervisor_llm().with_structured_output(
                CommandInterpretation,
                method="json_schema",
            )
            interpretation = structured.invoke(
                [
                    SystemMessage(content=COMMAND_SUPERVISOR_PROMPT),
                    HumanMessage(
                        content=json.dumps(
                            command_payload,
                            ensure_ascii=False,
                        )
                    ),
                ]
            )
        except Exception as exc:
            llm_error = exc

    if interpretation is None:
        if fallback is None:
            reason = f": {llm_error}" if llm_error else ""
            message = (
                "복잡한 계획 명령 해석에는 OPENAI_API_KEY와 정상 LLM 연결이 필요합니다"
                + reason
            )
            interpretation = CommandInterpretation(
                command_kind="PLAN",
                intent="OTHER",
                objective=command.text,
                execution_mode=(
                    command.requested_execution_mode
                    if command.requested_execution_mode != "AUTO"
                    else "PLAN_ONLY"
                ),
                missing_information=[message],
                summary="명령 해석 실패",
            )
            errors.append(message)
            classification_source = "unavailable"
        else:
            interpretation = fallback
            classification_source = "rule_fallback"
            if llm_error:
                warnings.append(
                    f"LLM 명령 해석 실패로 단순 조회 규칙을 사용했습니다: {llm_error}"
                )
    elif fallback and (
        fallback.confidence >= 0.9
        or bool(fallback.missing_information)
        or interpretation.intent == "OTHER"
        or (
            fallback.command_kind == "QUERY"
            and interpretation.command_kind != "QUERY"
        )
    ):
        interpretation = fallback
        classification_source = "rule_correction"
        warnings.append("명확한 단순 조회 표현을 규칙 기반 분류로 보정했습니다.")
    elif fallback and interpretation.command_kind == "QUERY":
        if interpretation.query_target == "NONE":
            interpretation.query_target = fallback.query_target
        if interpretation.query_action == "NONE":
            interpretation.query_action = fallback.query_action

    if deterministic.item_ids:
        interpretation.item_ids = sorted(
            set(interpretation.item_ids) | set(deterministic.item_ids)
        )

    # Inventory quantities and units use the conservative deterministic parser
    # even when the LLM classified the surrounding command.  This prevents an
    # LLM from silently converting EA/PALLET to BOX or enabling partial output.
    if (
        deterministic.inventory_operations
        or deterministic.load_open_inventory_orders
        or any(
            str(value).startswith(
                ("inventory_unit_confirmation:", "invalid_inventory_quantity:")
            )
            for value in deterministic.missing_information
        )
    ):
        interpretation.inventory_operations = list(
            deterministic.inventory_operations
        )
        interpretation.load_open_inventory_orders = (
            deterministic.load_open_inventory_orders
        )
        interpretation.item_ids = list(deterministic.item_ids)
        interpretation.quantity = deterministic.quantity
        if deterministic.inventory_operations and interpretation.intent == "OTHER":
            interpretation.intent = deterministic.intent
        interpretation.missing_information = list(
            dict.fromkeys(
                [
                    *interpretation.missing_information,
                    *[
                        value
                        for value in deterministic.missing_information
                        if str(value).startswith(
                            (
                                "inventory_unit_confirmation:",
                                "invalid_inventory_quantity:",
                            )
                        )
                    ],
                ]
            )
        )
        interpretation.ambiguous_terms = list(
            dict.fromkeys(
                [
                    *interpretation.ambiguous_terms,
                    *deterministic.ambiguous_terms,
                ]
            )
        )

    # A command-level planning clock is deterministic operational input.  It
    # must not be replaced by an LLM classification or Snapshot capture time.
    if deterministic.planning_reference is not None:
        interpretation.planning_reference = deterministic.planning_reference
        interpretation.daily_schedule_requested = True
    elif "planning_reference_time" in deterministic.missing_information:
        interpretation.missing_information = list(
            dict.fromkeys(
                [*interpretation.missing_information, "planning_reference_time"]
            )
        )

    if deterministic.scheduled_task_constraints:
        interpretation.scheduled_task_constraints = list(
            deterministic.scheduled_task_constraints
        )
        interpretation.daily_schedule_requested = True

    # Explicit numeric assumptions and labelled target nodes are operational
    # facts. Keep the deterministic values even when the LLM handles the
    # surrounding intent.
    if deterministic.source_node_ids:
        interpretation.source_node_ids = list(deterministic.source_node_ids)
    if deterministic.target_node_ids:
        interpretation.target_node_ids = list(deterministic.target_node_ids)
        interpretation.target_node_type = deterministic.target_node_type

    deterministic_battery_events = [
        event
        for event in deterministic.hypothetical_events
        if event.event_type == "LOW_BATTERY"
        and event.parameters.battery_percent is not None
    ]
    if deterministic_battery_events:
        retained_events = [
            event
            for event in interpretation.hypothetical_events
            if not (
                event.event_type == "LOW_BATTERY"
                and any(
                    canonical_robot_id(target_id)
                    in {
                        canonical_robot_id(candidate)
                        for deterministic_event in deterministic_battery_events
                        for candidate in deterministic_event.target_ids
                    }
                    for target_id in event.target_ids
                )
            )
        ]
        interpretation.hypothetical_events = [
            *retained_events,
            *deterministic_battery_events,
        ]

    if deterministic.fixed_robot_assignments:
        interpretation.fixed_robot_assignments = list(
            deterministic.fixed_robot_assignments
        )

    if deterministic.hard_constraints:
        interpretation.hard_constraints = list(
            dict.fromkeys(
                [
                    *interpretation.hard_constraints,
                    *deterministic.hard_constraints,
                ]
            )
        )
    if "EXPLICIT_TASK_SCOPE_ONLY" in deterministic.hard_constraints:
        interpretation.target_task_ids = list(deterministic.target_task_ids)

    if classification_source == "rule_correction":
        warnings = []
    if (
        deterministic.daily_schedule_requested
        and not getattr(settings, "warehouse_timezone", "")
        and "DEFAULT_WAREHOUSE_TIMEZONE_USED" not in warnings
    ):
        warnings.append("DEFAULT_WAREHOUSE_TIMEZONE_USED")
    if interpretation.command_kind == "QUERY":
        interpretation.execution_mode = "PLAN_ONLY"
    elif command.requested_execution_mode != "AUTO":
        interpretation.execution_mode = command.requested_execution_mode
    if command.scenario_definition:
        scenario = ScenarioDefinition.model_validate(command.scenario_definition)
        interpretation.command_kind = "PLAN"
        if "전체 재계획" in command.text:
            interpretation.intent = "GLOBAL_REPLAN"
        elif "재계획" in command.text:
            interpretation.intent = "LOCAL_REPLAN"
        else:
            interpretation.intent = (
                "HYPOTHETICAL_SCENARIO"
                if scenario.hypothetical_events
                else "DAILY_PLAN"
            )
        interpretation.execution_mode = (
            "EXECUTE"
            if command.requested_execution_mode == "EXECUTE"
            else "SIMULATE_ONLY"
        )
        interpretation.robot_limit = scenario.robot_limit
        interpretation.excluded_robot_ids = sorted(
            set(scenario.excluded_robot_ids)
        )
        interpretation.extracted_robot_ids = sorted(
            set(interpretation.extracted_robot_ids)
            | set(scenario.excluded_robot_ids)
            | set(scenario.fixed_robot_assignments.values())
        )
        interpretation.extracted_task_ids = sorted(
            set(interpretation.extracted_task_ids)
            | set(scenario.fixed_robot_assignments)
        )
        interpretation.excluded_node_ids = sorted(
            set(scenario.excluded_node_ids)
        )
        interpretation.assumed_closed_node_ids = sorted(
            set(scenario.excluded_node_ids)
        )
        interpretation.excluded_edge_ids = sorted(
            set(scenario.excluded_edge_ids)
        )
        interpretation.fixed_robot_assignments = [
            {"task_id": task_id, "robot_id": robot_id}
            for task_id, robot_id in sorted(
                scenario.fixed_robot_assignments.items()
            )
        ]
        interpretation.optimization_priority = scenario.optimization_priority
        if scenario.optimization_weights:
            interpretation.optimization_weights = OptimizationWeights.model_validate(
                scenario.optimization_weights
            )
        interpretation.hypothetical_events = scenario.hypothetical_events
        if scenario.source_plan_version:
            interpretation.target_plan_versions = [scenario.source_plan_version]
        interpretation.target_task_ids = sorted(
            set(interpretation.target_task_ids)
            | set(scenario.affected_task_ids)
            | set(scenario.changeable_task_ids)
        )
        interpretation.extracted_task_ids = sorted(
            set(interpretation.extracted_task_ids)
            | set(scenario.affected_task_ids)
            | set(scenario.protected_task_ids)
            | set(scenario.changeable_task_ids)
        )
        interpretation.extracted_robot_ids = sorted(
            set(interpretation.extracted_robot_ids)
            | set(scenario.affected_robot_ids)
        )
        interpretation.comparison_requested = False
        interpretation.requires_future_feature = False
        interpretation.missing_information = []
        interpretation.ambiguous_terms = []
        interpretation.summary = f"What-if 시나리오: {scenario.name}"
        classification_source = "scenario_definition"
    if re.search(r"(폐쇄|차단|사용\s*불가|사용할\s*수\s*없)", command.text):
        mentioned_nodes = {
            int(value) for value in re.findall(r"노드\s*(\d+)", command.text)
        }
        interpretation.assumed_closed_node_ids = sorted(
            set(interpretation.assumed_closed_node_ids) | mentioned_nodes
        )
    if (
        command.requested_execution_mode == "AUTO"
        and interpretation.execution_mode == "EXECUTE"
        and not settings.robot_gateway_url
    ):
        interpretation.execution_mode = "SIMULATE_ONLY"
        warnings.append(
            "AUTO 모드에서 ROBOT_GATEWAY_URL이 없어 SIMULATE_ONLY로 제한했습니다."
        )
    return {
        "interpretation": interpretation.model_dump(mode="json"),
        "final_status": "INTERPRETATION_FAILED" if errors else "INTERPRETED",
        "errors": errors,
        "warnings": warnings,
        "trace": (
            trace(
                "interpret_command",
                intent=interpretation.intent,
                command_kind=interpretation.command_kind,
                query_target=interpretation.query_target,
                query_action=interpretation.query_action,
                classification_source=classification_source,
                execution_mode=interpretation.execution_mode,
                planning_reference=(
                    interpretation.planning_reference.model_dump(mode="json")
                    if interpretation.planning_reference
                    else None
                ),
            )
            + (
                trace(
                    "parse_daily_schedule",
                    timezone=(
                        getattr(settings, "warehouse_timezone", "")
                        or "Asia/Seoul"
                    ),
                    timezone_defaulted=not bool(
                        getattr(settings, "warehouse_timezone", "")
                    ),
                    constraint_count=len(interpretation.scheduled_task_constraints),
                    dependency_count=len(interpretation.task_dependencies),
                    insertion_policy=interpretation.insertion_policy,
                    preemption_policy=interpretation.preemption_policy,
                )
                if interpretation.daily_schedule_requested
                else []
            )
        ),
    }


SUPERVISOR_TOOL_ORDER = (
    "SNAPSHOT",
    "OPTIMIZER",
    "ROUTING",
    "SIMULATION",
    "VERIFICATION",
    "EXECUTION",
)
RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def required_supervisor_tools(
    command_kind: str,
    execution_mode: str,
) -> list[str]:
    if command_kind == "QUERY":
        return ["SNAPSHOT"]
    tools = ["SNAPSHOT", "OPTIMIZER", "ROUTING"]
    if execution_mode in {"SIMULATE_ONLY", "EXECUTE"}:
        tools.append("SIMULATION")
    tools.append("VERIFICATION")
    if execution_mode == "EXECUTE":
        tools.append("EXECUTION")
    return tools


def _hypothetical_scope_plan_mode(
    interpretation: CommandInterpretation,
) -> str | None:
    """Return a deterministic replan scope for bounded what-if commands.

    The LLM may describe the same single-robot what-if request as LOCAL or
    GLOBAL across runs.  A bounded command should not expand the blast radius
    merely because wording changed, so scope is fixed before the Snapshot:

    * exactly one referenced robot
    * at most one inventory operation / target task
    * no broad daily schedule request
    * at most one temporary node or edge assumption

    Commands outside that boundary remain GLOBAL because their impact cannot be
    proven local from the interpretation alone.
    """

    if interpretation.intent != "HYPOTHETICAL_SCENARIO":
        return None
    robot_ids = {
        canonical_robot_id(value)
        for value in (
            *interpretation.target_robot_ids,
            *interpretation.extracted_robot_ids,
            *interpretation.verified_robot_ids,
        )
        if value
    }
    bounded_work_count = max(
        len(interpretation.inventory_operations),
        len(interpretation.target_task_ids),
    )
    bounded_map_change_count = (
        len(interpretation.assumed_closed_node_ids)
        + len(interpretation.assumed_closed_edges)
    )
    if (
        len(robot_ids) == 1
        and bounded_work_count <= 1
        and bounded_map_change_count <= 1
        and not interpretation.daily_schedule_requested
    ):
        return "LOCAL_REPLAN"
    return "GLOBAL_REPLAN"


def default_supervisor_plan_mode(
    interpretation: CommandInterpretation,
) -> str:
    if interpretation.command_kind == "QUERY":
        return "NO_REPLAN"
    if interpretation.intent == "INSERT_TASK" or interpretation.intent in {
        "OUTBOUND",
        "INBOUND",
        "RELOCATION",
    }:
        return "INSERT_TASK"
    if interpretation.intent == "LOCAL_REPLAN":
        return "LOCAL_REPLAN"
    if interpretation.intent == "GLOBAL_REPLAN":
        return "GLOBAL_REPLAN"
    hypothetical_mode = _hypothetical_scope_plan_mode(interpretation)
    if hypothetical_mode is not None:
        return hypothetical_mode
    return "INITIAL_PLAN"


def deterministic_supervisor_decision(
    command: NaturalLanguageCommand,
    interpretation: CommandInterpretation,
) -> SupervisorDecision:
    settings = get_settings()
    effective_execution_mode = (
        command.requested_execution_mode
        if command.requested_execution_mode != "AUTO"
        else interpretation.execution_mode
    )
    if interpretation.command_kind == "QUERY":
        effective_execution_mode = "PLAN_ONLY"
    command_kind = (
        "QUERY"
        if interpretation.command_kind == "QUERY"
        else "EXECUTE"
        if effective_execution_mode == "EXECUTE"
        else "PLAN"
    )
    missing = [str(value) for value in interpretation.missing_information if value]
    requires_clarification = bool(missing)
    if command_kind == "QUERY":
        risk_level = "LOW"
    elif effective_execution_mode == "EXECUTE":
        risk_level = "HIGH"
    else:
        risk_level = "MEDIUM"
    allow_replan = command_kind != "QUERY" and not requires_clarification
    configured_limit = max(
        0,
        min(int(getattr(settings, "max_replan_count", 3)), 3),
    )
    max_attempts = min(2, configured_limit) if allow_replan else 0
    return SupervisorDecision(
        intent=interpretation.intent,
        command_kind=command_kind,
        execution_mode=(
            "PLAN_ONLY" if command_kind == "QUERY" else effective_execution_mode
        ),
        required_tools=required_supervisor_tools(
            command_kind,
            "PLAN_ONLY" if command_kind == "QUERY" else effective_execution_mode,
        ),
        plan_mode=default_supervisor_plan_mode(interpretation),
        requires_clarification=requires_clarification,
        clarification_reason="; ".join(missing) if missing else None,
        risk_level=risk_level,
        allow_replan=allow_replan,
        max_replan_attempts=max_attempts,
        next_node="REPORT" if requires_clarification else "SNAPSHOT",
        reasoning_summary=(
            "필수 정보가 부족해 추가 확인이 필요합니다."
            if requires_clarification
            else f"{command_kind} 명령에 필요한 안전한 처리 단계를 선택했습니다."
        ),
    )


def normalize_supervisor_decision(
    raw_decision: SupervisorDecision,
    command: NaturalLanguageCommand,
    interpretation: CommandInterpretation,
) -> SupervisorDecision:
    """LLM 판단이 실행 권한이나 결정론적 안전 경계를 넘지 못하게 보정합니다."""

    fallback = deterministic_supervisor_decision(command, interpretation)
    decision = raw_decision.model_copy(deep=True)
    decision.intent = interpretation.intent
    decision.command_kind = fallback.command_kind
    decision.execution_mode = fallback.execution_mode
    decision.required_tools = fallback.required_tools

    deterministic_plan_mode = default_supervisor_plan_mode(interpretation)
    if deterministic_plan_mode != "INITIAL_PLAN":
        # Explicit/bounded scopes are deterministic.  The LLM still explains
        # the decision but cannot widen a single-robot scenario to GLOBAL or
        # shrink a broad scenario to LOCAL.
        decision.plan_mode = deterministic_plan_mode
    elif decision.plan_mode == "NO_REPLAN":
        decision.plan_mode = fallback.plan_mode

    if interpretation.missing_information:
        decision.requires_clarification = True
        decision.clarification_reason = "; ".join(
            str(value) for value in interpretation.missing_information
        )
    decision.next_node = (
        "REPORT" if decision.requires_clarification else "SNAPSHOT"
    )

    minimum_risk = fallback.risk_level
    if RISK_ORDER[decision.risk_level] < RISK_ORDER[minimum_risk]:
        decision.risk_level = minimum_risk
    if decision.command_kind == "QUERY" or decision.requires_clarification:
        decision.allow_replan = False
        decision.max_replan_attempts = 0
    else:
        configured_limit = max(
            0,
            min(int(getattr(get_settings(), "max_replan_count", 3)), 3),
        )
        # For a valid planning command, deterministic safety owns whether a
        # failed verification may replan.  An LLM response cannot silently set
        # allow_replan=False and remove the configured recovery path.
        decision.allow_replan = fallback.allow_replan and configured_limit > 0
        decision.max_replan_attempts = (
            min(
                max(
                    int(decision.max_replan_attempts),
                    int(fallback.max_replan_attempts),
                ),
                configured_limit,
            )
            if decision.allow_replan
            else 0
        )
    decision.reasoning_summary = decision.reasoning_summary.strip()[:500]
    return decision


def supervisor_node(state: PlanningState) -> dict[str, Any]:
    command = NaturalLanguageCommand.model_validate(state["command"])
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    settings = get_settings()
    source = "deterministic_fallback"
    fallback_reason: str | None = None
    supervisor_warnings: list[str] = []
    started_trace = trace(
        "supervisor_started",
        prompt_version=SUPERVISOR_PROMPT_VERSION,
        model_name=(getattr(settings, "openai_model", None) or None),
        llm_enabled=bool(getattr(settings, "openai_api_key", "")),
    )
    decision: SupervisorDecision | None = None

    if getattr(settings, "openai_api_key", ""):
        try:
            structured = build_supervisor_llm().with_structured_output(
                SupervisorDecision,
                method="json_schema",
            )
            raw_decision = structured.invoke(
                [
                    SystemMessage(content=SUPERVISOR_PROMPT),
                    HumanMessage(
                        content=json.dumps(
                            {
                                "command": command.model_dump(mode="json"),
                                "interpretation": interpretation.model_dump(
                                    mode="json"
                                ),
                                "allowed_tools": list(SUPERVISOR_TOOL_ORDER),
                                "configured_max_replan_count": min(
                                    int(getattr(settings, "max_replan_count", 3)),
                                    3,
                                ),
                                "conversation_summary": state.get(
                                    "conversation_summary", {}
                                ),
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                ]
            )
            decision = normalize_supervisor_decision(
                SupervisorDecision.model_validate(raw_decision),
                command,
                interpretation,
            )
            source = "llm"
        except Exception as exc:
            fallback_reason = f"LLM Supervisor 실패: {exc}"
            supervisor_warnings.append(fallback_reason)
    else:
        fallback_reason = "OPENAI_API_KEY가 없어 deterministic Supervisor를 사용했습니다."

    if decision is None:
        decision = deterministic_supervisor_decision(command, interpretation)

    supervisor_trace = list(started_trace)
    if source == "deterministic_fallback":
        supervisor_trace.extend(
            trace(
                "supervisor_fallback_used",
                prompt_version=SUPERVISOR_PROMPT_VERSION,
                reason=fallback_reason,
            )
        )
    supervisor_trace.extend(
        trace(
            "supervisor_completed",
            prompt_version=SUPERVISOR_PROMPT_VERSION,
            source=source,
            fallback_used=source != "llm",
            command_kind=decision.command_kind,
            execution_mode=decision.execution_mode,
            required_tools=decision.required_tools,
            plan_mode=decision.plan_mode,
            requires_clarification=decision.requires_clarification,
            risk_level=decision.risk_level,
            allow_replan=decision.allow_replan,
            max_replan_attempts=decision.max_replan_attempts,
            next_node=decision.next_node,
            reasoning_summary=decision.reasoning_summary,
        )
    )
    return {
        "supervisor_decision": decision.model_dump(mode="json"),
        "max_replan_attempts": min(decision.max_replan_attempts, 3),
        "supervisor_source": source,
        "supervisor_prompt_version": SUPERVISOR_PROMPT_VERSION,
        "supervisor_warnings": supervisor_warnings,
        "warnings": supervisor_warnings,
        "final_status": (
            "INTERPRETATION_FAILED"
            if state.get("final_status") == "INTERPRETATION_FAILED"
            else "CLARIFICATION_REQUIRED"
            if decision.requires_clarification
            else "SUPERVISOR_COMPLETED"
        ),
        "trace": supervisor_trace,
    }


def _normalized_inventory_item_key(value: Any) -> str:
    return re.sub(r"[^0-9A-Z가-힣]", "", str(value or "").upper())


def _inventory_item_candidates(
    item_id: str, known_items: set[str]
) -> list[str]:
    normalized = _normalized_inventory_item_key(item_id)
    if not normalized:
        return []
    return sorted(
        value
        for value in known_items
        if _normalized_inventory_item_key(value) == normalized
        and str(value) != str(item_id)
    )


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    sql = snapshot["sql"]
    live = snapshot["redis"]
    graph = snapshot["graph"]
    return {
        "captured_at": snapshot["captured_at"],
        "inventory_candidate_count": len(sql.get("inventory", [])),
        "robot_count": len(sql.get("robots", [])),
        "open_works": [
            {
                "work_id": str(work.get("work_id")),
                "status": work.get("status"),
                "assigned_robot_id": work.get("assigned_robot_id"),
                "priority": work.get("priority"),
            }
            for work in sql.get("works", [])
        ],
        "map_node_count": len(graph.get("nodes", [])),
        "map_edge_count": len(graph.get("edges", [])),
        "active_plan_version": live.get("active_plan_version"),
        "executing_task_ids": live.get("executing_task_ids", []),
        "planned_task_ids": live.get("planned_task_ids", []),
        "temporary_closures": live.get("temporary_closures", []),
        "validation": snapshot.get("validation", {}),
    }


def _clarification_options(
    interpretation: CommandInterpretation,
    snapshot: dict[str, Any] | None,
) -> list[ClarificationOption]:
    missing = set(interpretation.missing_information)
    ambiguous = set(interpretation.ambiguous_terms)
    if "requested_execution_mode" in missing or ambiguous.intersection(
        {"적용해봐", "처리해줘", "돌려줘"}
    ):
        return [
            ClarificationOption(value="PLAN_ONLY", label="계획만 생성"),
            ClarificationOption(value="SIMULATE_ONLY", label="가상 시뮬레이션"),
            ClarificationOption(
                value="EXECUTE",
                label="실제 실행",
                description="최신 상태 재검증 후 실행합니다.",
            ),
        ]
    if "optimization_priority" in missing:
        return [
            ClarificationOption(value="MINIMIZE_MAKESPAN", label="완료시간 최소화"),
            ClarificationOption(value="MINIMIZE_TARDINESS", label="마감 지연 최소화"),
            ClarificationOption(value="MINIMIZE_DISTANCE", label="이동거리 최소화"),
            ClarificationOption(value="MINIMIZE_ENERGY", label="에너지 최소화"),
        ]
    if "event_application_mode" in missing:
        return [
            ClarificationOption(value="SIMULATE_ONLY", label="가상 상황으로 확인"),
            ClarificationOption(
                value="REAL_EVENT",
                label="실제 운영 이벤트",
                description="별도의 명시적 운영 이벤트 승인이 필요합니다.",
            ),
        ]
    ambiguous_item_marker = next(
        (
            value
            for value in missing
            if value.startswith("ambiguous_inventory_item:")
        ),
        None,
    )
    if snapshot and ambiguous_item_marker:
        _, raw_item_id = ambiguous_item_marker.split(":", 1)
        normalized = _normalized_inventory_item_key(raw_item_id)
        candidates = []
        for row in snapshot.get("sql", {}).get("inventory_items", []):
            candidate_id = row.get("item_id")
            if candidate_id is None:
                continue
            if (
                _normalized_inventory_item_key(candidate_id) == normalized
                and str(candidate_id) != raw_item_id
            ):
                candidates.append(
                    ClarificationOption(
                        value=str(candidate_id),
                        label=str(row.get("item_name") or candidate_id),
                        description=f"등록 품목 ID: {candidate_id}",
                    )
                )
        return candidates[:10]
    if snapshot and missing.intersection({"target_reference", "target_task_scope"}):
        robots = snapshot.get("sql", {}).get("robots", [])
        works = snapshot.get("sql", {}).get("works", [])
        if len(robots) + len(works) > 20:
            return []
        options = [
            ClarificationOption(
                value=str(row.get("robot_id")),
                label=f"로봇 {row.get('robot_id')}",
                description=str(row.get("status") or "상태 미확인"),
            )
            for row in robots[:10]
            if row.get("robot_id") is not None
        ]
        options.extend(
            ClarificationOption(
                value=str(row.get("work_id")),
                label=f"작업 {row.get('work_id')}",
                description=str(row.get("status") or "상태 미확인"),
            )
            for row in works[:10]
            if row.get("work_id") is not None
        )
        return options
    return []


def clarification_node(state: PlanningState) -> dict[str, Any]:
    command = NaturalLanguageCommand.model_validate(state["command"])
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    missing = list(dict.fromkeys(interpretation.missing_information))
    ambiguous = list(dict.fromkeys(interpretation.ambiguous_terms))
    unit_marker = next(
        (
            value
            for value in missing
            if value.startswith("inventory_unit_confirmation:")
        ),
        None,
    )
    quantity_marker = next(
        (
            value
            for value in missing
            if value.startswith("invalid_inventory_quantity:")
        ),
        None,
    )
    ambiguous_item_marker = next(
        (
            value
            for value in missing
            if value.startswith("ambiguous_inventory_item:")
        ),
        None,
    )
    unknown_item_marker = next(
        (
            value
            for value in missing
            if value.startswith("unknown_inventory_item:")
        ),
        None,
    )
    approval_marker = next(
        (
            value
            for value in missing
            if value.startswith("approved_inventory_work_or_order:")
        ),
        None,
    )
    if approval_marker:
        _, item_id = approval_marker.split(":", 1)
        reason_code = "APPROVED_INVENTORY_WORK_REQUIRED"
        question = (
            f"{item_id} 실제 실행에 연결할 PostgreSQL 주문 또는 승인된 작업을 "
            "찾지 못했습니다. 대상 작업 ID를 지정해 주세요."
        )
    elif ambiguous_item_marker:
        _, item_id = ambiguous_item_marker.split(":", 1)
        reason_code = "AMBIGUOUS_INVENTORY_ITEM"
        question = (
            f"{item_id}와 표기가 유사한 등록 품목이 있습니다. "
            "사용할 품목을 선택해 주세요."
        )
    elif unknown_item_marker:
        # Backward compatibility for previously persisted conversations. New
        # commands without any candidate are rejected without clarification.
        _, item_id = unknown_item_marker.split(":", 1)
        reason_code = "UNKNOWN_INVENTORY_ITEM"
        question = (
            f"PostgreSQL 품목 마스터에서 {item_id}를 찾을 수 없습니다. "
            "정확한 품목 ID를 알려주세요."
        )
    elif unit_marker:
        _, item_id, quantity, _ = unit_marker.split(":", 3)
        reason_code = "INVENTORY_UNIT_CONFIRMATION_REQUIRED"
        question = (
            "현재 시스템은 박스 단위로 재고를 관리합니다. "
            f"{item_id} 물품 {quantity}박스가 맞는지 확인해 주세요."
        )
    elif quantity_marker:
        _, item_id, quantity = quantity_marker.split(":", 2)
        reason_code = "INVALID_INVENTORY_QUANTITY"
        question = (
            f"{item_id} 수량 {quantity}은 사용할 수 없습니다. "
            "양의 정수 박스 수량으로 다시 알려주세요."
        )
    elif "requested_execution_mode" in missing:
        reason_code = "AMBIGUOUS_EXECUTION_MODE"
        question = "계획만 만들까요, 가상 시뮬레이션할까요, 실제 실행할까요?"
    elif "optimization_priority" in missing:
        reason_code = "AMBIGUOUS_OPTIMIZATION_OBJECTIVE"
        question = "완료시간, 마감 지연, 이동거리, 에너지 중 무엇을 우선할까요?"
    elif "event_application_mode" in missing:
        reason_code = "AMBIGUOUS_EVENT_CONTEXT"
        question = "실제 운영 이벤트로 반영할까요, 가상 시나리오로 확인할까요?"
    elif "comparison_dimensions" in missing:
        reason_code = "MISSING_COMPARISON_BASIS"
        question = "어떤 계획 또는 기준들을 비교하려는지 지정해 주세요."
    elif "target_task_scope" in missing:
        reason_code = "MISSING_INHERITED_TASK_SCOPE"
        question = (
            "같은 조건으로 처리할 이전 작업 범위를 찾지 못했습니다. "
            "대상 작업을 지정하거나 전체 작업 대상임을 명시해 주세요."
        )
    elif "explicit_schedule_time" in missing:
        reason_code = "AMBIGUOUS_SCHEDULE_TIME"
        question = "작업에 적용할 정확한 시작 시각 또는 시간 범위를 알려주세요."
    elif "safe_stop_confirmation" in missing:
        reason_code = "PREEMPTION_REQUIRES_SAFE_STOP_CONFIRMATION"
        question = (
            "실행 중 작업을 중단하려면 Robot Gateway의 안전 정지 확인이 필요합니다. "
            "중단 없이 미래 일정만 재계획할까요?"
        )
    elif "cyclic_task_dependency" in missing:
        reason_code = "CYCLIC_TASK_DEPENDENCY"
        question = "작업 선후관계에 순환이 있습니다. 실행 순서를 다시 지정해주세요."
    elif "simulated_base_plan_execution_confirmation" in missing:
        reason_code = "SIMULATED_BASE_PLAN_REQUIRES_EXECUTION_CONFIRMATION"
        question = (
            "가상 시뮬레이션 계획을 실제 실행 기준으로 사용할지 명시적으로 "
            "확인해 주세요. 실제 활성 계획이 있으면 해당 계획을 지정할 수 있습니다."
        )
    elif "target_reference" in missing:
        reason_code = "AMBIGUOUS_TARGET"
        question = "대상 로봇, 작업, 계획 또는 시뮬레이션을 지정해 주세요."
    else:
        reason_code = "MISSING_REQUIRED_INFORMATION"
        question = "명령을 처리하려면 부족한 정보를 추가로 알려주세요."

    clarification = ClarificationRequest(
        clarification_id=str(
            uuid5(NAMESPACE_URL, f"warehouse-clarification:{command.command_id}")
        ),
        conversation_id=command.conversation_id,
        command_id=command.command_id,
        reason_code=reason_code,
        question=question,
        missing_fields=missing,
        ambiguous_fields=ambiguous,
        options=_clarification_options(interpretation, state.get("snapshot")),
        original_text=command.text,
    )
    audit_warnings: list[str] = []
    repository = get_services().postgres
    if hasattr(repository, "create_clarification_request"):
        try:
            repository.create_clarification_request(
                {
                    **clarification.model_dump(mode="json"),
                    "warehouse_id": command.warehouse_id,
                    "created_at": datetime.now(UTC),
                }
            )
        except Exception as exc:
            message = f"Clarification 저장 실패: {sanitize_log_details(str(exc))}"
            logger.warning(message)
            audit_warnings.append(message)
    return {
        "clarification": clarification.model_dump(mode="json"),
        "final_status": "CLARIFICATION_REQUIRED",
        "audit_warnings": audit_warnings,
        "trace": trace(
            "clarification_required",
            clarification_id=clarification.clarification_id,
            reason_code=reason_code,
            missing_fields=missing,
            ambiguous_fields=ambiguous,
        ),
    }


def route_by_command_node(state: PlanningState) -> dict[str, Any]:
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    supervisor = (
        SupervisorDecision.model_validate(state["supervisor_decision"])
        if state.get("supervisor_decision")
        else None
    )
    command_kind = supervisor.command_kind if supervisor else interpretation.command_kind
    execution_mode = (
        supervisor.execution_mode if supervisor else interpretation.execution_mode
    )
    if command_kind == "QUERY":
        scope = ScopeDecision(
            plan_mode="NO_REPLAN",
            include_new_command=False,
            optimization_goal="조회만 수행",
            reason_summary="단순 조회는 최적화·경로·시뮬레이션을 실행하지 않습니다.",
        )
        return {
            "scope": scope.model_dump(mode="json"),
            "final_status": "QUERY_READY",
            "trace": trace(
                "route_by_command",
                branch="QUERY",
                supervisor_source=state.get("supervisor_source"),
                optimizer_called=False,
                routing_called=False,
                simulation_called=False,
            ),
        }
    return {
        "final_status": "COMMAND_ROUTED",
        "trace": trace(
            "route_by_command",
            branch=execution_mode,
            supervisor_source=state.get("supervisor_source"),
            required_tools=(supervisor.required_tools if supervisor else []),
        ),
    }


TERMINAL_WORK_STATUSES = {"COMPLETED", "CANCELLED", "FAILED"}


def _sanitize_base_plan_terminal_tasks(
    base_plan: dict[str, Any] | None,
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Drop active-plan tasks whose persisted work is already terminal.

    Tasks without a matching works row are preserved because candidate or
    externally supplied plans may legitimately exist before work persistence.
    """

    if not base_plan:
        return None, []
    statuses = {
        str(row.get("work_id")): str(row.get("status") or "").upper()
        for row in (snapshot.get("sql") or {}).get("work_statuses", [])
        if row.get("work_id")
    }
    terminal_ids = {
        work_id
        for work_id, status in statuses.items()
        if status in TERMINAL_WORK_STATUSES
    }
    if not terminal_ids:
        return base_plan, []

    result = deepcopy(base_plan)
    cuopt_plan = result.get("cuopt_plan") or {}
    scheduled = list(cuopt_plan.get("scheduled_tasks") or [])
    kept = []
    dropped_task_ids: list[str] = []
    for row in scheduled:
        task_id = str(row.get("task_id") or "")
        work_id = str(row.get("work_id") or task_id.split(":", 1)[0])
        if work_id in terminal_ids:
            if task_id:
                dropped_task_ids.append(task_id)
            continue
        kept.append(row)
    if len(kept) == len(scheduled):
        return base_plan, []

    cuopt_plan["scheduled_tasks"] = kept
    result["cuopt_plan"] = cuopt_plan
    for field in ("required_tasks",):
        if isinstance(result.get(field), list):
            result[field] = [
                row
                for row in result[field]
                if str((row or {}).get("work_id") or (row or {}).get("task_id") or "").split(":", 1)[0]
                not in terminal_ids
            ]
    if not kept:
        return None, sorted(dropped_task_ids)
    return result, sorted(dropped_task_ids)


def _select_base_plan(
    state: PlanningState,
    interpretation: CommandInterpretation,
) -> tuple[dict[str, Any] | None, str | None]:
    """Select a base without turning a draft/simulation into an active plan."""

    command = state["command"]
    live = state["snapshot"]["redis"]
    if explicitly_discards_base_plan(interpretation.objective):
        return None, None

    scenario_definition = command.get("scenario_definition") or {}
    scenario_base = scenario_definition.get("source_plan_snapshot")
    if isinstance(scenario_base, dict) and (scenario_base.get("cuopt_plan") or {}).get(
        "scheduled_tasks"
    ):
        selected = deepcopy(scenario_base)
        if scenario_definition.get("source_plan_version"):
            selected["plan_version"] = str(
                scenario_definition["source_plan_version"]
            )
        selected.setdefault(
            "base_plan_is_simulated",
            command.get("requested_execution_mode") != "EXECUTE",
        )
        selected.setdefault(
            "candidate_plan",
            command.get("requested_execution_mode") != "EXECUTE",
        )
        selected.setdefault(
            "execution_mode",
            "SIMULATE_ONLY"
            if command.get("requested_execution_mode") != "EXECUTE"
            else "EXECUTE",
        )
        return selected, "EVENT_SOURCE_PLAN"

    state_base = state.get("replan_base_plan")
    if isinstance(state_base, dict) and state_base:
        return state_base, state.get("base_plan_source") or "STATE_BASE_PLAN"

    real_active = active_plan_base(live.get("active_plan"))
    real_active, stale_active_task_ids = _sanitize_base_plan_terminal_tasks(
        real_active, state["snapshot"]
    )
    if stale_active_task_ids:
        state.setdefault("state_consistency_warnings", []).append(
            {
                "code": "STALE_ACTIVE_PLAN_TASKS_DROPPED",
                "task_ids": stale_active_task_ids,
            }
        )
    if real_active and not real_active.get("plan_version"):
        real_active["plan_version"] = live.get("active_plan_version")

    # EXECUTE always prefers the actual Redis active plan. Candidate-only
    # plans are inspected below solely to trigger a safe clarification.
    if interpretation.execution_mode == "EXECUTE" and real_active:
        return real_active, "ACTIVE_PLAN"

    def persisted(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
        return base_plan_from_evidence(evidence)

    conversation_id = command.get("conversation_id")
    parent_command_id = command.get("parent_command_id")
    requires_persisted_lookup = bool(
        interpretation.target_plan_versions or parent_command_id or conversation_id
    )
    if not requires_persisted_lookup:
        return (real_active, "ACTIVE_PLAN") if real_active else (None, None)

    # Repository evidence is only needed when the state/snapshot cannot
    # resolve an explicitly requested, parent, or conversation base plan.
    repository = get_services().postgres
    if conversation_id and hasattr(repository, "get_plan_evidence_by_version"):
        for version in interpretation.target_plan_versions:
            selected = persisted(
                repository.get_plan_evidence_by_version(
                    warehouse_id=int(command["warehouse_id"]),
                    conversation_id=str(conversation_id),
                    plan_version=str(version),
                )
            )
            if selected:
                return selected, "EXPLICIT_PLAN_VERSION"

    if parent_command_id and hasattr(repository, "get_latest_command_plan_evidence"):
        selected = persisted(
            repository.get_latest_command_plan_evidence(str(parent_command_id))
        )
        if selected:
            return selected, (
                "PARENT_SIMULATION_PLAN"
                if selected.get("base_plan_is_simulated")
                else "CONVERSATION_PLAN"
            )

    if conversation_id and hasattr(repository, "list_conversation_commands") and hasattr(
        repository, "get_latest_command_plan_evidence"
    ):
        rows = repository.list_conversation_commands(
            str(conversation_id), limit=100, offset=0
        )
        for row in reversed(rows):
            candidate_command_id = row.get("command_id")
            if not candidate_command_id or candidate_command_id == command.get(
                "command_id"
            ):
                continue
            selected = persisted(
                repository.get_latest_command_plan_evidence(
                    str(candidate_command_id)
                )
            )
            if selected:
                return selected, "CONVERSATION_PLAN"

    if real_active:
        return real_active, "ACTIVE_PLAN"
    return None, None


def decide_scope_node(state: PlanningState) -> dict[str, Any]:
    settings = get_settings()
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    snapshot_summary = compact_snapshot(state["snapshot"])
    supervisor: SupervisorDecision | None = None
    if state.get("supervisor_decision"):
        supervisor = SupervisorDecision.model_validate(
            state["supervisor_decision"]
        )
        decision = ScopeDecision(
            plan_mode=supervisor.plan_mode,
            freeze_horizon_seconds=int(
                getattr(settings, "freeze_horizon_seconds", 15)
            ),
            include_new_command=supervisor.command_kind != "QUERY",
            optimization_goal=interpretation.objective,
            reason_summary=supervisor.reasoning_summary,
        )
    else:
        # 직접 함수를 호출하는 기존 코드의 하위 호환 경로입니다.
        payload = {
            "command": state["command"],
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot_summary": snapshot_summary,
            "previous_scope": state.get("scope"),
            "impact": state.get("impact"),
            "replan_count": state.get("replan_count", 0),
        }
        structured = build_supervisor_llm().with_structured_output(
            ScopeDecision,
            method="json_schema",
        )
        decision = ScopeDecision.model_validate(
            structured.invoke(
                [
                    SystemMessage(content=SCOPE_SUPERVISOR_PROMPT),
                    HumanMessage(
                        content=json.dumps(payload, ensure_ascii=False, default=str)
                    ),
                ]
            )
        )

    live = state["snapshot"]["redis"]
    if interpretation.command_kind == "QUERY":
        # Queries never need a planning base and must remain executable in a
        # pure unit-test state without configured external repositories.
        base_plan, base_plan_source = None, None
    else:
        base_plan, base_plan_source = _select_base_plan(state, interpretation)
    base_plan_version = (
        str(base_plan.get("plan_version"))
        if base_plan and base_plan.get("plan_version")
        else None
    )
    base_plan_is_simulated = bool(
        base_plan and base_plan.get("base_plan_is_simulated")
    )
    base_plan_is_candidate = bool(base_plan and base_plan.get("candidate_plan"))
    active_plan_version = live.get("active_plan_version")
    frozen_prefix_ids: set[str] = set()
    has_existing_plan = bool(
        base_plan
        or live.get("executing_task_ids")
        or live.get("planned_task_ids")
    )
    if interpretation.command_kind == "QUERY":
        decision.plan_mode = "NO_REPLAN"
    elif not has_existing_plan:
        decision.plan_mode = "INITIAL_PLAN"
    elif interpretation.intent == "INSERT_TASK":
        decision.plan_mode = "INSERT_TASK"
    elif interpretation.intent == "LOCAL_REPLAN":
        decision.plan_mode = "LOCAL_REPLAN"
    elif interpretation.intent == "GLOBAL_REPLAN":
        decision.plan_mode = "GLOBAL_REPLAN"
    elif decision.plan_mode == "INITIAL_PLAN":
        decision.plan_mode = "GLOBAL_REPLAN"

    scenario_definition = state["command"].get("scenario_definition") or {}
    if scenario_definition:
        runtime_affected_tasks = {
            str(value)
            for value in scenario_definition.get("affected_task_ids", [])
            if value
        }
        runtime_changeable_tasks = {
            str(value)
            for value in scenario_definition.get("changeable_task_ids", [])
            if value
        }
        runtime_protected_tasks = {
            str(value)
            for value in scenario_definition.get("protected_task_ids", [])
            if value
        }
        runtime_affected_robots = {
            str(value)
            for value in scenario_definition.get("affected_robot_ids", [])
            if value
        }
        decision.affected_task_ids = sorted(
            set(decision.affected_task_ids) | runtime_affected_tasks
        )
        decision.affected_robot_ids = sorted(
            set(decision.affected_robot_ids) | runtime_affected_robots
        )
        decision.changeable_task_ids = sorted(
            set(decision.changeable_task_ids) | runtime_changeable_tasks
        )
        decision.fixed_task_ids = sorted(
            (set(decision.fixed_task_ids) | runtime_protected_tasks)
            - runtime_changeable_tasks
        )
        if scenario_definition.get("freeze_horizon_seconds") is not None:
            decision.freeze_horizon_seconds = max(
                decision.freeze_horizon_seconds,
                int(scenario_definition["freeze_horizon_seconds"]),
            )
        if runtime_changeable_tasks and decision.plan_mode not in {
            "LOCAL_REPLAN",
            "GLOBAL_REPLAN",
        }:
            decision.plan_mode = "LOCAL_REPLAN"
        if runtime_changeable_tasks:
            decision.reason_summary = (
                "실시간 로봇 상태를 반영해 완료·동결·비영향 작업을 보호하고 "
                "변경 가능 작업만 부분 재계획합니다."
            )
    if (
        interpretation.execution_mode == "EXECUTE"
        and base_plan_is_simulated
        and base_plan_source != "ACTIVE_PLAN"
    ):
        interpretation.missing_information = list(
            dict.fromkeys(
                [
                    *interpretation.missing_information,
                    "simulated_base_plan_execution_confirmation",
                ]
            )
        )
    if has_existing_plan and decision.plan_mode == "INSERT_TASK":
        active_plan = base_plan or live.get("active_plan") or {}
        active_schedule = (active_plan.get("cuopt_plan") or {}).get(
            "scheduled_tasks", []
        )
        activated_at = active_plan.get("activated_at")
        elapsed_steps = 0
        if activated_at:
            try:
                elapsed_seconds = max(
                    0.0,
                    (
                        as_utc_datetime(
                            state["snapshot"]["captured_at"],
                            field_name="captured_at",
                        )
                        - as_utc_datetime(
                            activated_at, field_name="activated_at"
                        )
                    ).total_seconds(),
                )
                elapsed_steps = math.floor(
                    elapsed_seconds
                    / max(1, getattr(settings, "time_step_seconds", 5))
                )
            except (TypeError, ValueError):
                elapsed_steps = 0
        freeze_steps = math.ceil(
            decision.freeze_horizon_seconds
            / max(1, getattr(settings, "time_step_seconds", 5))
        )
        executing = {
            str(value) for value in live.get("executing_task_ids", [])
        }
        for row in active_schedule:
            task_id = str(row.get("task_id"))
            work_id = str(row.get("work_id") or task_id.split(":", 1)[0])
            if (
                task_id in executing
                or work_id in executing
                or (
                    not base_plan_is_candidate
                    and int(row.get("start_time_step") or 0)
                    <= elapsed_steps + freeze_steps
                )
            ):
                frozen_prefix_ids.update((task_id, work_id))
        existing_ids = {
            str(value)
            for row in active_schedule
            for value in (
                row.get("task_id"),
                row.get("work_id")
                or str(row.get("task_id") or "").split(":", 1)[0],
            )
            if value
        }
        inserted_ids = {
            str(value)
            for value in interpretation.target_task_ids
            if str(value) not in existing_ids
        }
        decision.fixed_task_ids = sorted(
            set(decision.fixed_task_ids) | frozen_prefix_ids
        )
        decision.changeable_task_ids = sorted(
            set(decision.changeable_task_ids) | inserted_ids
        )
        decision.affected_task_ids = sorted(
            set(decision.affected_task_ids)
            | set(decision.changeable_task_ids)
        )
    live_robots = live.get("robots", [])
    failed_robot_ids = {
        str(row.get("robot_id"))
        for row in live_robots
        if str(row.get("last_event") or row.get("status") or "").upper()
        in {"ROBOT_FAILED", "FAILED", "OFFLINE", "MAINTENANCE"}
    }
    if failed_robot_ids and has_existing_plan and decision.plan_mode != "GLOBAL_REPLAN":
        decision.plan_mode = "LOCAL_REPLAN"
        decision.affected_robot_ids = sorted(
            set(decision.affected_robot_ids) | failed_robot_ids
        )
        affected_tasks = {
            str(row.get("task_id"))
            for row in live.get("tasks", [])
            if str(row.get("robot_id")) in failed_robot_ids and row.get("task_id")
        }
        decision.affected_task_ids = sorted(
            set(decision.affected_task_ids) | affected_tasks
        )
        decision.changeable_task_ids = sorted(
            set(decision.changeable_task_ids) | affected_tasks
        )
        decision.fixed_task_ids = sorted(
            set(decision.fixed_task_ids) - affected_tasks
        )
    decision.freeze_horizon_seconds = max(
        decision.freeze_horizon_seconds,
        settings.freeze_horizon_seconds,
    )
    update = {
        "interpretation": interpretation.model_dump(mode="json"),
        "scope": decision.model_dump(mode="json"),
        "replan_base_plan": base_plan or {},
        "base_plan_source": base_plan_source,
        "base_plan_version": base_plan_version,
        "base_plan_is_simulated": base_plan_is_simulated,
        "active_plan_version": active_plan_version,
        "original_plan_version": base_plan_version,
        "final_status": "SCOPE_DECIDED",
        "warnings": [
            *(state.get("warnings") or []),
            *(
                ["STALE_ACTIVE_PLAN_TASKS_DROPPED"]
                if state.get("state_consistency_warnings")
                else []
            ),
        ],
        "trace": (
            trace("decide_scope", plan_mode=decision.plan_mode)
            + trace(
                "base_plan_selected",
                base_plan_source=base_plan_source,
                base_plan_version=base_plan_version,
                active_plan_version=active_plan_version,
                base_plan_is_simulated=base_plan_is_simulated,
            )
            + (
                trace(
                    "stale_active_plan_tasks_dropped",
                    details=state.get("state_consistency_warnings"),
                )
                if state.get("state_consistency_warnings")
                else []
            )
            + (
                trace(
                    "frozen_prefix_preserved",
                    frozen_task_ids=sorted(frozen_prefix_ids),
                    freeze_horizon_seconds=decision.freeze_horizon_seconds,
                )
                if frozen_prefix_ids
                else []
            )
            + (
                trace(
                    "remaining_tasks_replanned",
                    changeable_task_ids=decision.changeable_task_ids,
                )
                if decision.plan_mode == "INSERT_TASK"
                else []
            )
        ),
    }
    if supervisor is not None and supervisor.plan_mode != decision.plan_mode:
        supervisor.plan_mode = decision.plan_mode
        update["supervisor_decision"] = supervisor.model_dump(mode="json")
    return update


def validate_snapshot(
    interpretation: CommandInterpretation,
    sql_snapshot: dict[str, Any],
    graph_snapshot: dict[str, Any],
    node_validation: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if node_validation["missing"]:
        errors.append(
            "SQL이 참조한 node_id가 Neo4j에 없습니다: "
            f"{node_validation['missing']}"
        )
    if not graph_snapshot.get("nodes") or not graph_snapshot.get("edges"):
        errors.append("Neo4j 창고 지도가 비어 있습니다.")

    if (
        interpretation.item_ids
        and interpretation.quantity
        and not interpretation.inventory_operations
        and not interpretation.load_open_inventory_orders
    ):
        for item_id in interpretation.item_ids:
            available = sum(
                int(row.get("available_quantity") or 0)
                for row in sql_snapshot.get("inventory", [])
                if str(row.get("item_id")) == item_id
            )
            if available < interpretation.quantity:
                errors.append(
                    f"{item_id} 가용 재고 {available}개가 "
                    f"요청 {interpretation.quantity}개보다 적습니다."
                )

    if interpretation.command_kind != "QUERY" and not sql_snapshot.get("robots"):
        errors.append("PostgreSQL에 창고 로봇 정보가 없습니다.")
    if interpretation.invalid_robot_ids:
        errors.append(
            "명령에서 지정한 로봇을 Snapshot에서 확인할 수 없습니다: "
            f"{interpretation.invalid_robot_ids}"
        )
    if interpretation.invalid_task_ids:
        errors.append(
            "명령에서 지정한 작업을 Snapshot에서 확인할 수 없습니다: "
            f"{interpretation.invalid_task_ids}"
        )
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def build_snapshot_node(state: PlanningState) -> dict[str, Any]:
    reference_time = datetime.now(UTC)
    command = NaturalLanguageCommand.model_validate(state["command"])
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    services = get_services()
    try:
        use_simulation_state = bool(
            command.simulation_id
            and interpretation.execution_mode == "SIMULATE_ONLY"
        )
        if command.simulation_id and not use_simulation_state:
            raise RuntimeError(
                "simulation_id는 SIMULATE_ONLY 명령에서만 사용할 수 있습니다."
            )
        if use_simulation_state:
            virtual_state = services.redis.simulation_snapshot(
                command.simulation_id
            )
            sql_snapshot = {
                "inventory": virtual_state["inventory"],
                "robots": virtual_state["robots"],
                "works": virtual_state["works"],
            }
            executing_task_ids = [
                str(row.get("task_id") or row.get("work_id"))
                for row in virtual_state["works"]
                if str(row.get("status") or "").upper() == "EXECUTING"
            ]
            planned_task_ids = [
                str(row.get("task_id") or row.get("work_id"))
                for row in virtual_state["works"]
                if str(row.get("status") or "").upper() in {"NEW", "PLANNED"}
            ]
            redis_snapshot = {
                "robots": virtual_state["robots"],
                "tasks": virtual_state["works"],
                "executing_task_ids": executing_task_ids,
                "planned_task_ids": planned_task_ids,
                "active_plan_version": None,
                "active_plan": None,
                "temporary_closures": [],
            }
        else:
            sql_snapshot = services.postgres.snapshot(
                command.warehouse_id,
                interpretation.item_ids,
            )
            redis_snapshot = services.redis.live_snapshot(command.warehouse_id)
        scenario_definition = command.scenario_definition or {}
        source_plan = scenario_definition.get("source_plan_snapshot") or {}
        if not isinstance(source_plan, dict):
            source_plan = {}
        source_required_tasks = [
            row
            for row in (source_plan.get("required_tasks") or [])
            if isinstance(row, dict) and row.get("task_id")
        ]
        source_scheduled_tasks = [
            row
            for row in (
                (source_plan.get("cuopt_plan") or {}).get("scheduled_tasks") or []
            )
            if isinstance(row, dict) and row.get("task_id")
        ]
        source_task_work_ids = {
            str(row["task_id"]): str(
                row.get("work_id") or str(row["task_id"]).split(":", 1)[0]
            )
            for row in (*source_required_tasks, *source_scheduled_tasks)
        }

        known_robot_ids = {
            canonical_robot_id(row.get("robot_id"))
            for row in sql_snapshot.get("robots", [])
            if row.get("robot_id") is not None
        }
        known_task_ids = {
            canonical_work_id(str(row.get("work_id") or row.get("task_id")))
            for row in sql_snapshot.get("works", [])
            if row.get("work_id") is not None or row.get("task_id") is not None
        }
        known_task_ids.update(
            canonical_work_id(work_id)
            for work_id in source_task_work_ids.values()
            if work_id
        )

        def resolved_task_scope_id(value: object) -> str:
            raw = str(value)
            source_work_id = source_task_work_ids.get(raw)
            if source_work_id:
                return canonical_work_id(source_work_id)
            return canonical_work_id(canonical_task_id(raw))

        interpretation.verified_robot_ids = sorted(
            value
            for value in set(interpretation.extracted_robot_ids)
            if canonical_robot_id(value) in known_robot_ids
        )
        interpretation.verified_task_ids = sorted(
            value
            for value in set(interpretation.extracted_task_ids)
            if resolved_task_scope_id(value) in known_task_ids
        )
        interpretation.invalid_robot_ids = sorted(
            value
            for value in set(interpretation.extracted_robot_ids)
            if canonical_robot_id(value) not in known_robot_ids
        )
        interpretation.invalid_task_ids = sorted(
            value
            for value in set(interpretation.extracted_task_ids)
            if resolved_task_scope_id(value) not in known_task_ids
        )
        node_ids = (
            list(interpretation.source_node_ids)
            + list(interpretation.target_node_ids)
            + [row.get("node_id") for row in sql_snapshot.get("inventory", [])]
            + [row.get("node_id") for row in sql_snapshot.get("robots", [])]
            + [row.get("source_node") for row in sql_snapshot.get("works", [])]
            + [row.get("target_node") for row in sql_snapshot.get("works", [])]
            + [
                node_id
                for row in source_required_tasks
                for node_id in (
                    *(row.get("source_candidates") or []),
                    *(row.get("target_candidates") or []),
                )
            ]
            + [row.get("source_node") for row in source_scheduled_tasks]
            + [row.get("target_node") for row in source_scheduled_tasks]
            + [
                row.get("node_id")
                for row in scenario_definition.get("robot_state_overrides", {}).values()
                if isinstance(row, dict)
            ]
        )
        graph_snapshot = services.neo4j.fetch_topology(command.warehouse_id)
        node_validation = services.neo4j.validate_node_ids(
            command.warehouse_id,
            node_ids,
        )
        validation = validate_snapshot(
            interpretation,
            sql_snapshot,
            graph_snapshot,
            node_validation,
        )
        if interpretation.excluded_edge_ids:
            known_edge_ids: set[str] = set()
            for edge in graph_snapshot.get("edges", []):
                known_edge_ids.update(_edge_identifier_aliases(edge))
            missing_edge_ids = sorted(
                set(interpretation.excluded_edge_ids) - known_edge_ids
            )
            if missing_edge_ids:
                validation["valid"] = False
                validation["errors"].append(
                    "Neo4j Snapshot에 없는 통로 ID입니다: "
                    f"{missing_edge_ids}"
                )
        snapshot = {
            "warehouse_id": command.warehouse_id,
            "captured_at": reference_time.isoformat(),
            "sql": sql_snapshot,
            "graph": graph_snapshot,
            "redis": redis_snapshot,
            "node_validation": node_validation,
            "validation": validation,
        }
        return {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": snapshot,
            "validation": validation,
            "final_status": (
                "SNAPSHOT_READY" if validation["valid"] else "VALIDATION_FAILED"
            ),
            "errors": validation["errors"],
            "trace": trace(
                "build_snapshot",
                valid=validation["valid"],
                sql_nodes=len(node_ids),
                graph_nodes=len(graph_snapshot["nodes"]),
                state_source=("SIMULATION" if use_simulation_state else "REAL"),
                simulation_id=command.simulation_id,
            ),
        }
    except Exception as exc:
        return {
            "validation": {
                "valid": False,
                "errors": [str(exc)],
                "warnings": [],
            },
            "final_status": "SNAPSHOT_FAILED",
            "errors": [f"Snapshot 생성 실패: {exc}"],
            "trace": trace("build_snapshot", valid=False),
        }


def _inventory_operations_from_snapshot(
    interpretation: CommandInterpretation,
    sql: dict[str, Any],
) -> list[InventoryOperationRequest]:
    target_work_ids = {
        canonical_work_id(value)
        for value in interpretation.target_task_ids
        if value
    }

    def work_is_in_scope(value: Any) -> bool:
        return bool(value) and canonical_work_id(str(value)) in target_work_ids

    operations = [
        operation
        for operation in interpretation.inventory_operations
        if not target_work_ids or work_is_in_scope(operation.work_id)
    ]
    existing_order_ids = {
        str(row.order_id) for row in operations if row.order_id is not None
    }
    existing_work_ids = {
        str(row.work_id) for row in operations if row.work_id is not None
    }
    if interpretation.load_open_inventory_orders:
        for row in sql.get("outbound_orders", []):
            if target_work_ids and not work_is_in_scope(row.get("work_id")):
                continue
            order_id = str(row["outbound_id"])
            if order_id in existing_order_ids:
                continue
            matching_commands = [
                operation
                for operation in operations
                if operation.source == "COMMAND"
                and operation.order_id is None
                and operation.work_id is None
                and operation.operation_type == "OUTBOUND"
                and operation.item_id == str(row["item_id"])
                and operation.quantity_boxes
                == int(row["requested_quantity_boxes"])
            ]
            if len(matching_commands) == 1:
                operation = matching_commands[0]
                operation.order_id = order_id
                operation.work_id = (
                    str(row["work_id"]) if row.get("work_id") else None
                )
                operation.source = "SQL_ORDER"
                existing_order_ids.add(order_id)
                if operation.work_id:
                    existing_work_ids.add(operation.work_id)
                continue
            operations.append(
                InventoryOperationRequest(
                    operation_id=f"outbound:{order_id}",
                    work_id=(str(row["work_id"]) if row.get("work_id") else None),
                    order_id=order_id,
                    operation_type="OUTBOUND",
                    item_id=str(row["item_id"]),
                    quantity_boxes=int(row["requested_quantity_boxes"]),
                    required_at=row.get("required_by"),
                    priority=str(row.get("priority") or "NORMAL").upper(),
                    allow_partial_fulfillment=bool(
                        row.get("allow_partial_fulfillment", False)
                    ),
                    source="SQL_ORDER",
                )
            )
            existing_order_ids.add(order_id)
            if row.get("work_id"):
                existing_work_ids.add(str(row["work_id"]))
        for row in sql.get("inbound_orders", []):
            if target_work_ids and not work_is_in_scope(row.get("work_id")):
                continue
            order_id = str(row["inbound_id"])
            if order_id in existing_order_ids:
                continue
            matching_commands = [
                operation
                for operation in operations
                if operation.source == "COMMAND"
                and operation.order_id is None
                and operation.work_id is None
                and operation.operation_type == "INBOUND"
                and operation.item_id == str(row["item_id"])
                and operation.quantity_boxes == int(row["quantity_boxes"])
            ]
            if len(matching_commands) == 1:
                operation = matching_commands[0]
                operation.order_id = order_id
                operation.work_id = (
                    str(row["work_id"]) if row.get("work_id") else operation.work_id
                )
                operation.actual_arrival_at = row.get("actual_arrival_at")
                operation.actual_available_at = row.get("actual_available_at")
                operation.expected_arrival_at = (
                    operation.expected_arrival_at or row.get("expected_arrival_at")
                )
                operation.expected_available_at = (
                    operation.expected_available_at
                    or row.get("expected_available_at")
                )
                operation.storage_node_id = (
                    operation.storage_node_id or row.get("storage_node_id")
                )
                operation.lot_id = operation.lot_id or row.get("lot_id")
                operation.warehouse_item_id = (
                    operation.warehouse_item_id or row.get("warehouse_item_id")
                )
                operation.source = "SQL_ORDER"
                existing_order_ids.add(order_id)
                continue
            operations.append(
                InventoryOperationRequest(
                    operation_id=f"inbound:{order_id}",
                    work_id=(str(row["work_id"]) if row.get("work_id") else None),
                    order_id=order_id,
                    operation_type="INBOUND",
                    item_id=str(row["item_id"]),
                    quantity_boxes=int(row["quantity_boxes"]),
                    expected_arrival_at=row.get("expected_arrival_at"),
                    expected_available_at=row.get("expected_available_at"),
                    actual_arrival_at=row.get("actual_arrival_at"),
                    actual_available_at=row.get("actual_available_at"),
                    storage_node_id=row.get("storage_node_id"),
                    lot_id=row.get("lot_id"),
                    warehouse_item_id=row.get("warehouse_item_id"),
                    source="SQL_ORDER",
                )
            )
            existing_order_ids.add(order_id)
    include_snapshot_works = bool(target_work_ids) or interpretation.load_open_inventory_orders
    for row in sql.get("works", []) if include_snapshot_works else []:
        if target_work_ids and not work_is_in_scope(row.get("work_id")):
            continue
        operation_type = str(row.get("operation_type") or "").upper()
        quantity = row.get("quantity_boxes")
        if quantity is None and operation_type in {"INBOUND", "OUTBOUND"}:
            quantity = row.get("quantity")
        if (
            operation_type not in {"INBOUND", "OUTBOUND"}
            or not row.get("item_id")
            or not quantity
            or str(row.get("work_id")) in existing_work_ids
        ):
            continue
        work_id = str(row["work_id"])
        operations.append(
            InventoryOperationRequest(
                operation_id=f"work:{work_id}",
                work_id=work_id,
                order_id=(
                    str(row["inventory_order_id"])
                    if row.get("inventory_order_id")
                    else None
                ),
                operation_type=operation_type,
                item_id=str(row["item_id"]),
                quantity_boxes=int(quantity),
                required_at=row.get("required_at"),
                priority=(
                    "EMERGENCY"
                    if int(row.get("priority") or 50) <= 1
                    else "HIGH"
                    if int(row.get("priority") or 50) <= 10
                    else "NORMAL"
                ),
                allow_partial_fulfillment=bool(
                    row.get("allow_partial_fulfillment", False)
                ),
                source="WORK",
            )
        )
        existing_work_ids.add(work_id)
    return operations


def _emergency_review_items(
    feasibility: InventoryFeasibilityResult,
) -> list[EmergencyReviewItem]:
    rows: list[EmergencyReviewItem] = []
    for result in feasibility.item_results:
        if result.status != "EMERGENCY_REVIEW_REQUIRED":
            continue
        rows.append(
            EmergencyReviewItem(
                item_id=result.item_id,
                work_id=result.work_id,
                requested_quantity_boxes=result.requested_quantity_boxes,
                available_quantity_boxes=result.available_quantity_boxes,
                shortage_quantity_boxes=result.shortage_quantity_boxes,
                required_at=result.required_at,
                earliest_full_fulfillment_at=result.earliest_full_fulfillment_at,
                blocked_work_ids=feasibility.blocked_work_ids,
                independent_work_ids=feasibility.independent_work_ids,
                recommended_actions=[
                    (
                        "전체 수량 사용 가능 시각 이후로 출고"
                        if result.earliest_full_fulfillment_at
                        else "현재 등록된 가용 재고·입고 예정 정보가 없으므로 재고 정보를 먼저 등록"
                        if result.available_quantity_boxes == 0
                        else "추가 재고 확보"
                    ),
                    f"추가 재고 {result.shortage_quantity_boxes} BOX 확보",
                    "출고 마감시간 변경",
                    "부분 출고 명시 승인",
                ],
            )
        )
    return rows


def inventory_precheck_node(state: PlanningState) -> dict[str, Any]:
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    snapshot = state["snapshot"]
    sql = snapshot["sql"]
    source_plan = (
        state.get("replan_base_plan")
        or (state.get("command", {}).get("scenario_definition") or {}).get(
            "source_plan_snapshot"
        )
        or snapshot.get("redis", {}).get("active_plan")
        or {}
    )
    if not isinstance(source_plan, dict):
        source_plan = {}
    source_required_tasks = [
        row
        for row in (source_plan.get("required_tasks") or [])
        if isinstance(row, dict) and row.get("task_id")
    ]
    source_scheduled_tasks = [
        row
        for row in ((source_plan.get("cuopt_plan") or {}).get("scheduled_tasks") or [])
        if isinstance(row, dict) and row.get("task_id")
    ]
    source_task_work_ids = {
        str(row["task_id"]): str(
            row.get("work_id") or str(row["task_id"]).split(":", 1)[0]
        )
        for row in (*source_required_tasks, *source_scheduled_tasks)
    }
    requested_work_ids = {
        canonical_work_id(
            source_task_work_ids.get(
                str(value),
                str(value).split(":", 1)[0],
            )
        )
        for value in interpretation.target_task_ids
        if value
    }

    known_work_ids = {
        canonical_work_id(str(row.get("work_id")))
        for row in sql.get("works", [])
        if row.get("work_id")
    }
    known_work_ids.update(
        canonical_work_id(operation.work_id or operation.operation_id)
        for operation in interpretation.inventory_operations
    )
    # Runtime event replans can contain synthetic or not-yet-persisted tasks.
    # The event source plan is an explicit planning contract, so its work ids
    # must participate in dependency/scope validation even when SQL has no row.
    known_work_ids.update(
        canonical_work_id(
            str(row.get("work_id") or str(row.get("task_id") or "").split(":", 1)[0])
        )
        for row in (*source_required_tasks, *source_scheduled_tasks)
        if row.get("work_id") or row.get("task_id")
    )
    unknown_work_ids = sorted(requested_work_ids - known_work_ids)
    if unknown_work_ids:
        interpretation.missing_information = list(
            dict.fromkeys(
                [
                    *interpretation.missing_information,
                    *[
                        f"unknown_target_work_id:{work_id}"
                        for work_id in unknown_work_ids
                    ],
                ]
            )
        )
        result = InventoryFeasibilityResult(status="FAILED", valid=False)
        return {
            "interpretation": interpretation.model_dump(mode="json"),
            "inventory_operations": [],
            "inventory_feasibility": result.model_dump(mode="json"),
            "inventory_projection": [],
            "inventory_blocked_work_ids": [],
            "capacity_feasibility": capacity_feasibility(
                (), sql.get("storage_capacity")
            ).model_dump(mode="json"),
            "emergency_review_items": [],
            "final_status": "CLARIFICATION_REQUIRED",
            "trace": trace(
                "inventory_precheck",
                status="FAILED",
                unknown_target_work_ids=unknown_work_ids,
            ),
        }
    operations = _inventory_operations_from_snapshot(interpretation, sql)
    if interpretation.execution_mode == "EXECUTE":
        for operation in operations:
            if operation.source != "COMMAND" or operation.work_id or operation.order_id:
                continue
            matching_works = [
                row
                for row in sql.get("works", [])
                if str(row.get("operation_type") or "").upper()
                == operation.operation_type
                and str(row.get("item_id") or "") == operation.item_id
                and int(row.get("quantity_boxes") or row.get("quantity") or 0)
                == operation.quantity_boxes
                and (
                    not interpretation.target_task_ids
                    or str(row.get("work_id")) in interpretation.target_task_ids
                )
                and str(row.get("status") or "").upper()
                not in {"COMPLETED", "CANCELLED", "FAILED"}
            ]
            if len(matching_works) == 1:
                operation.work_id = str(matching_works[0]["work_id"])
                operation.order_id = (
                    str(matching_works[0]["inventory_order_id"])
                    if matching_works[0].get("inventory_order_id")
                    else None
                )
                operation.source = "WORK"
            else:
                interpretation.missing_information = list(
                    dict.fromkeys(
                        [
                            *interpretation.missing_information,
                            f"approved_inventory_work_or_order:{operation.item_id}",
                        ]
                    )
                )
    interpretation.inventory_operations = operations
    if not operations:
        result = InventoryFeasibilityResult(
            status="NOT_APPLICABLE", valid=True
        )
        capacity = capacity_feasibility((), sql.get("storage_capacity"))
        return {
            "interpretation": interpretation.model_dump(mode="json"),
            "inventory_operations": [],
            "inventory_feasibility": result.model_dump(mode="json"),
            "inventory_projection": [],
            "inventory_blocked_work_ids": [],
            "capacity_feasibility": capacity.model_dump(mode="json"),
            "emergency_review_items": [],
            "trace": trace(
                "inventory_precheck", status="NOT_APPLICABLE", operation_count=0
            ),
        }

    known_items = {
        str(row.get("item_id"))
        for row in [
            *sql.get("inventory_items", []),
            *sql.get("inventory", []),
            *sql.get("inbound_orders", []),
            *sql.get("outbound_orders", []),
        ]
        if row.get("item_id")
    }
    unknown = sorted({row.item_id for row in operations} - known_items)
    if unknown:
        candidate_map = {
            value: _inventory_item_candidates(value, known_items)
            for value in unknown
        }
        ambiguous_unknown = {
            value: candidates
            for value, candidates in candidate_map.items()
            if candidates
        }
        rejected_unknown = [
            value for value in unknown if not candidate_map.get(value)
        ]
        if ambiguous_unknown:
            interpretation.missing_information = list(
                dict.fromkeys(
                    [
                        *interpretation.missing_information,
                        *[
                            f"ambiguous_inventory_item:{value}"
                            for value in ambiguous_unknown
                        ],
                    ]
                )
            )
            result = InventoryFeasibilityResult(
                status="FAILED",
                valid=False,
                warnings=[
                    "표기가 유사한 등록 품목이 있어 확인이 필요합니다: "
                    f"{ambiguous_unknown}"
                ],
            )
            return {
                "interpretation": interpretation.model_dump(mode="json"),
                "inventory_operations": [
                    row.model_dump(mode="json") for row in operations
                ],
                "inventory_feasibility": result.model_dump(mode="json"),
                "inventory_projection": [],
                "inventory_blocked_work_ids": [],
                "inventory_unknown_item_ids": rejected_unknown,
                "inventory_item_candidates": ambiguous_unknown,
                "capacity_feasibility": capacity_feasibility(
                    (row.operation_type for row in operations),
                    sql.get("storage_capacity"),
                ).model_dump(mode="json"),
                "emergency_review_items": [],
                "final_status": "CLARIFICATION_REQUIRED",
                "trace": trace(
                    "inventory_precheck",
                    status="FAILED",
                    ambiguous_item_candidates=ambiguous_unknown,
                ),
            }

        unknown_operations = [
            operation for operation in operations if operation.item_id in rejected_unknown
        ]
        item_results = [
            ItemInventoryResult(
                operation_id=operation.operation_id,
                work_id=operation.work_id,
                order_id=operation.order_id,
                operation_type=operation.operation_type,
                item_id=operation.item_id,
                requested_quantity_boxes=operation.quantity_boxes,
                planned_quantity_boxes=0,
                available_quantity_boxes=0,
                shortage_quantity_boxes=operation.quantity_boxes,
                required_at=operation.required_at,
                earliest_full_fulfillment_at=None,
                status="EMERGENCY_REVIEW_REQUIRED",
                allow_partial_fulfillment=operation.allow_partial_fulfillment,
                projection=[],
                lot_allocations=[],
            )
            for operation in unknown_operations
        ]
        result = InventoryFeasibilityResult(
            status="FAILED",
            valid=False,
            item_results=item_results,
            shortage_work_ids=[
                operation.work_id or operation.operation_id
                for operation in unknown_operations
            ],
            warnings=[
                "등록되지 않은 품목이므로 계획을 생성하지 않았습니다: "
                f"{rejected_unknown}"
            ],
        )
        emergency_items = [
            EmergencyReviewItem(
                item_id=operation.item_id,
                work_id=operation.work_id,
                requested_quantity_boxes=operation.quantity_boxes,
                available_quantity_boxes=0,
                shortage_quantity_boxes=operation.quantity_boxes,
                required_at=operation.required_at,
                earliest_full_fulfillment_at=None,
                recommended_actions=[
                    "품목 마스터 등록 후 다시 요청",
                    "등록된 다른 품목 ID로 새 요청",
                ],
            )
            for operation in unknown_operations
        ]
        return {
            "interpretation": interpretation.model_dump(mode="json"),
            "inventory_operations": [
                row.model_dump(mode="json") for row in operations
            ],
            "inventory_feasibility": result.model_dump(mode="json"),
            "inventory_projection": [],
            "inventory_blocked_work_ids": result.shortage_work_ids,
            "inventory_unknown_item_ids": rejected_unknown,
            "inventory_item_candidates": {},
            "capacity_feasibility": capacity_feasibility(
                (row.operation_type for row in operations),
                sql.get("storage_capacity"),
            ).model_dump(mode="json"),
            "emergency_review_items": [
                row.model_dump(mode="json") for row in emergency_items
            ],
            "final_status": "EMERGENCY_REVIEW_REQUIRED",
            "trace": trace(
                "inventory_precheck",
                status="FAILED",
                unknown_item_ids=rejected_unknown,
                policy="REJECT_WITHOUT_CLARIFICATION",
            ),
        }

    planning_reference = (
        interpretation.planning_reference.utc_at
        if interpretation.planning_reference is not None
        else as_utc_datetime(snapshot["captured_at"], field_name="captured_at")
    )
    # Keep the business deadline intact on CommandInterpretation. Inventory must
    # be available when PICK can begin, not only by the end of a hard window.
    # Use an ephemeral evaluation copy and preserve the model's synchronized
    # required_at/required_by invariant.
    constraint_by_operation = {
        canonical_work_id(row.work_id): row
        for row in interpretation.scheduled_task_constraints
    }
    projection_operations = []
    for operation in operations:
        if operation.operation_type != "OUTBOUND":
            projection_operations.append(operation)
            continue
        operation_ref = canonical_work_id(operation.work_id or operation.operation_id)
        constraint = constraint_by_operation.get(operation_ref)
        effective_required_at = max(
            planning_reference,
            (constraint.earliest_start if constraint else None)
            or operation.required_at
            or planning_reference,
        )
        projection_operations.append(
            operation.model_copy(
                update={
                    "required_at": effective_required_at,
                    "required_by": effective_required_at,
                }
            )
        )
    service = InventoryProjectionService(planning_reference)
    active_reservations = snapshot.get("redis", {}).get(
        "inventory_reservations", []
    )
    feasibility = service.evaluate(
        projection_operations,
        current_lots=sql.get("inventory", []),
        future_inbounds=sql.get("inbound_orders", []),
        active_reservations=active_reservations,
        dependencies=interpretation.task_dependencies,
    )

    # A hard window means the item does not have to be available exactly at
    # earliest_start.  If the full quantity becomes available later inside the
    # window, evaluate and schedule from that first feasible instant.  The
    # optimizer still enforces latest_finish, including travel and handling.
    availability_adjustments: dict[str, datetime] = {}
    result_by_operation = {
        row.operation_id: row for row in feasibility.item_results
    }
    adjusted_projection_operations = []
    for operation in projection_operations:
        result = result_by_operation.get(operation.operation_id)
        operation_ref = canonical_work_id(operation.work_id or operation.operation_id)
        constraint = constraint_by_operation.get(operation_ref)
        candidate = result.earliest_full_fulfillment_at if result else None
        if (
            operation.operation_type == "OUTBOUND"
            and constraint is not None
            and constraint.time_constraint_type == "HARD_WINDOW"
        ):
            if (
                candidate is not None
                and candidate >= constraint.earliest_start
                and candidate <= constraint.latest_finish
            ):
                evaluation_at = candidate
            else:
                # No full-quantity instant exists inside the window.  Evaluate
                # at the window end so shortage/partial-fulfillment evidence
                # reflects the maximum stock that can become usable in time.
                evaluation_at = constraint.latest_finish
            availability_adjustments[operation.operation_id] = evaluation_at
            adjusted_projection_operations.append(
                operation.model_copy(
                    update={"required_at": evaluation_at, "required_by": evaluation_at}
                )
            )
        else:
            adjusted_projection_operations.append(operation)

    if availability_adjustments:
        feasibility = service.evaluate(
            adjusted_projection_operations,
            current_lots=sql.get("inventory", []),
            future_inbounds=sql.get("inbound_orders", []),
            active_reservations=active_reservations,
            dependencies=interpretation.task_dependencies,
        )
    capacity = capacity_feasibility(
        (row.operation_type for row in operations),
        sql.get("storage_capacity"),
    )
    emergency = _emergency_review_items(feasibility)
    projections = [
        point.model_dump(mode="json")
        for result in feasibility.item_results
        for point in result.projection
    ]
    warnings = list(feasibility.warnings)
    if capacity.status == "NOT_CONFIGURED" and any(
        row.operation_type == "INBOUND" for row in operations
    ):
        warnings.extend(capacity.warnings)
    return {
        "interpretation": interpretation.model_dump(mode="json"),
        "inventory_operations": [row.model_dump(mode="json") for row in operations],
        "inventory_feasibility": feasibility.model_dump(mode="json"),
        "inventory_projection": projections,
        "inventory_blocked_work_ids": sorted(
            set(feasibility.shortage_work_ids) | set(feasibility.blocked_work_ids)
        ),
        "capacity_feasibility": capacity.model_dump(mode="json"),
        "emergency_review_items": [row.model_dump(mode="json") for row in emergency],
        "warnings": warnings,
        "final_status": (
            "INVENTORY_READY"
            if feasibility.valid
            else "EMERGENCY_REVIEW_REQUIRED"
        ),
        "trace": trace(
            "inventory_precheck",
            status=feasibility.status,
            reference_time=planning_reference.isoformat(),
            reference_time_source=(
                interpretation.planning_reference.source
                if interpretation.planning_reference is not None
                else "SNAPSHOT_CAPTURED_AT"
            ),
            operation_count=len(operations),
            shortage_work_ids=feasibility.shortage_work_ids,
            blocked_work_ids=feasibility.blocked_work_ids,
            independent_work_ids=feasibility.independent_work_ids,
            inventory_window_adjustments={
                key: value.isoformat()
                for key, value in availability_adjustments.items()
            },
        ),
    }


def priority_number(value: Any) -> int:
    if isinstance(value, int):
        return max(1, min(100, value))
    return {
        "EMERGENCY": 1,
        "HIGH": 10,
        "NORMAL": 50,
    }.get(str(value).upper(), 50)


def _node_is_active(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() in {"true", "1", "yes", "active"}


def special_nodes(snapshot: dict[str, Any], node_type: str | None) -> list[int]:
    if not node_type:
        return []
    return sorted(
        int(node["node_id"])
        for node in snapshot["graph"]["nodes"]
        if str(node.get("node_type", "")).upper() == node_type.upper()
        and _node_is_active(node.get("active"))
    )


def _graph_shortest_distance(
    snapshot: dict[str, Any],
    start: int,
    target: int,
) -> float | None:
    """Return the active directed-graph distance used for inbound dock choice."""

    nodes = {
        int(row["node_id"])
        for row in snapshot.get("graph", {}).get("nodes", [])
        if row.get("node_id") is not None and _node_is_active(row.get("active"))
    }
    if start not in nodes or target not in nodes:
        return None
    if start == target:
        return 0.0
    graph: dict[int, list[tuple[int, float]]] = {node_id: [] for node_id in nodes}
    for edge in snapshot.get("graph", {}).get("edges", []):
        if not _node_is_active(edge.get("active")):
            continue
        source = edge.get("from_node")
        destination = edge.get("to_node")
        if source is None or destination is None:
            continue
        source = int(source)
        destination = int(destination)
        if source not in nodes or destination not in nodes:
            continue
        distance = float(edge.get("distance") or 1.0)
        graph[source].append((destination, distance))
        if str(edge.get("direction") or "ONE_WAY").upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            graph[destination].append((source, distance))

    queue: list[tuple[float, int]] = [(0.0, start)]
    best: dict[int, float] = {start: 0.0}
    while queue:
        distance, node_id = heapq.heappop(queue)
        if distance != best.get(node_id):
            continue
        if node_id == target:
            return distance
        for neighbor, edge_distance in graph.get(node_id, []):
            candidate = distance + edge_distance
            if candidate < best.get(neighbor, math.inf):
                best[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
    return None


def select_inbound_route_nodes(
    snapshot: dict[str, Any],
    *,
    source_candidates: list[int],
    target_candidates: list[int],
) -> tuple[int, int, dict[str, Any]]:
    """Choose one active inbound dock/storage pair deterministically.

    The score includes the closest active robot's approach distance and the
    inbound-to-storage route.  This fixes the physical source for the paired
    PICK/DROP tasks so the carried item cannot silently switch inbound docks.
    """

    active_nodes = {
        int(row["node_id"]): str(row.get("node_type") or "").upper()
        for row in snapshot.get("graph", {}).get("nodes", [])
        if row.get("node_id") is not None and _node_is_active(row.get("active"))
    }
    active_ids = set(active_nodes)
    sources = sorted(
        {
            int(value)
            for value in source_candidates
            if active_nodes.get(int(value)) == "INBOUND"
        }
    )
    targets = sorted(
        {
            int(value)
            for value in target_candidates
            if active_nodes.get(int(value)) == "STORAGE"
        }
    )
    if not sources:
        raise RuntimeError("INBOUND_SOURCE_NODE_NOT_FOUND")
    if not targets:
        raise RuntimeError("INBOUND_STORAGE_NODE_NOT_FOUND")

    robot_nodes = sorted(
        {
            int(row["node_id"])
            for row in snapshot.get("sql", {}).get("robots", [])
            if row.get("node_id") is not None
            and int(row["node_id"]) in active_ids
            and str(row.get("status") or "ACTIVE").upper()
            not in {"FAILED", "OFFLINE", "MAINTENANCE", "DISABLED"}
        }
    )
    options: list[tuple[float, float, float, int, int]] = []
    for source in sources:
        approaches = [
            distance
            for robot_node in robot_nodes
            if (distance := _graph_shortest_distance(snapshot, robot_node, source))
            is not None
        ]
        if robot_nodes and not approaches:
            continue
        approach_distance = min(approaches) if approaches else 0.0
        for target in targets:
            operation_distance = _graph_shortest_distance(snapshot, source, target)
            if operation_distance is None:
                continue
            options.append(
                (
                    approach_distance + operation_distance,
                    operation_distance,
                    approach_distance,
                    source,
                    target,
                )
            )
    if not options:
        raise RuntimeError("INBOUND_ROUTE_NOT_FOUND")
    total, operation_distance, approach_distance, source, target = min(options)
    return source, target, {
        "selection_policy": "MIN_ACTIVE_ROBOT_APPROACH_PLUS_STORAGE_DISTANCE",
        "source_node_id": source,
        "target_node_id": target,
        "approach_distance": round(approach_distance, 6),
        "operation_distance": round(operation_distance, 6),
        "total_distance": round(total, 6),
        "source_candidate_count": len(sources),
        "target_candidate_count": len(targets),
    }


def allocate_fefo(
    inventory: list[dict[str, Any]],
    item_id: str,
    quantity: int,
) -> list[dict[str, Any]]:
    rows = [row for row in inventory if str(row.get("item_id")) == item_id]
    rows.sort(
        key=lambda row: (
            str(row.get("expiry_date") or "9999-12-31"),
            str(row["warehouse_item_id"]),
        )
    )
    remaining = quantity
    allocations: list[dict[str, Any]] = []
    for row in rows:
        take = min(remaining, int(row.get("available_quantity") or 0))
        if take <= 0:
            continue
        allocations.append(
            {
                "warehouse_item_id": str(row["warehouse_item_id"]),
                "item_id": item_id,
                "node_id": int(row["node_id"]),
                "quantity": take,
                "lot_id": row.get("lot_id"),
                "expiry_date": row.get("expiry_date"),
            }
        )
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise RuntimeError(f"FEFO 배정 중 {item_id} {remaining}개가 부족합니다.")
    return allocations


def tasks_from_work(
    work: dict[str, Any],
    frozen: bool,
    *,
    business_deadline: datetime | None = None,
    constraint: TaskScheduleConstraint | None = None,
    dependencies: list[TaskDependency] | None = None,
    same_robot_group: str | None = None,
    inventory_allocations: list[dict[str, Any]] | None = None,
) -> list[AtomicTask]:
    work_id = str(work["work_id"])
    source = (
        [int(work["source_node"])]
        if work.get("source_node") is not None
        else []
    )
    target = (
        [int(work["target_node"])]
        if work.get("target_node") is not None
        else []
    )
    dependencies = dependencies or []
    predecessor_task_ids = [
        f"{canonical_work_id(row.predecessor_work_id)}:move"
        for row in dependencies
        if canonical_work_id(row.successor_work_id) == canonical_work_id(work_id)
    ]
    # works.scheduled_end is a previous plan result (and the demo reset script
    # also populates it).  It is not a business deadline for a new plan.
    # The linked outbound operation's required_at is the actual business
    # deadline and must survive even when it precedes the planning reference.
    deadline = business_deadline or (constraint.latest_finish if constraint else None)
    allocation_times = [
        as_utc_datetime(row["available_at"], field_name="lot_available_at")
        for row in (inventory_allocations or [])
        if row.get("available_at")
    ]
    inventory_earliest_start = max(allocation_times) if allocation_times else None
    constrained_earliest_start = constraint.earliest_start if constraint else None
    earliest_start = max(
        [
            value
            for value in (inventory_earliest_start, constrained_earliest_start)
            if value is not None
        ],
        default=None,
    )
    return [
        AtomicTask(
            task_id=f"{work_id}:move",
            work_id=work_id,
            action="MOVE",
            item_id=work.get("item_id"),
            quantity=int(work.get("quantity_boxes") or work.get("quantity") or 0),
            source_candidates=source,
            target_candidates=target,
            priority=priority_number(work.get("priority", 50)),
            deadline=deadline,
            predecessors=predecessor_task_ids,
            dependencies=dependencies,
            earliest_start=earliest_start,
            latest_finish=(constraint.latest_finish if constraint else None),
            time_constraint_type=(
                constraint.time_constraint_type if constraint else "DEADLINE"
                if deadline is not None
                else "ASAP"
            ),
            same_robot_group=(
                (constraint.same_robot_group or same_robot_group)
                if constraint
                else same_robot_group
            ),
            frozen=frozen,
            assigned_robot_id=(
                str(work["assigned_robot_id"])
                if work.get("assigned_robot_id") is not None
                else None
            ),
            inventory_allocations=inventory_allocations or [],
        )
    ]


def select_required_tasks_node(state: PlanningState) -> dict[str, Any]:
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    explicit_dependencies = list(interpretation.task_dependencies)
    scope = ScopeDecision.model_validate(state["scope"])
    snapshot = state["snapshot"]
    sql = snapshot["sql"]
    persisted_dependencies = [
        TaskDependency.model_validate(row)
        for row in sql.get("work_dependencies", [])
    ]
    dependency_by_key = {
        (
            canonical_work_id(row.predecessor_work_id),
            canonical_work_id(row.successor_work_id),
        ): row
        for row in persisted_dependencies
    }
    dependency_by_key.update(
        {
            (
                canonical_work_id(row.predecessor_work_id),
                canonical_work_id(row.successor_work_id),
            ): row
            for row in interpretation.task_dependencies
        }
    )
    interpretation.task_dependencies = [
        dependency_by_key[key] for key in sorted(dependency_by_key)
    ]
    persisted_constraints = {
        canonical_work_id(str(row.get("work_id"))):
        TaskScheduleConstraint.model_validate(row)
        for row in sql.get("work_schedule_constraints", [])
    }
    persisted_constraints.update(
        {
            canonical_work_id(row.work_id): row
            for row in interpretation.scheduled_task_constraints
        }
    )
    fixed = set(scope.fixed_task_ids)
    changeable = set(scope.changeable_task_ids)
    tasks: list[AtomicTask] = []
    constraints_by_work = {
        canonical_work_id(row.work_id): row
        for row in interpretation.scheduled_task_constraints
    }
    same_robot_by_work = {
        canonical_work_id(work_id): group.group_id
        for group in interpretation.same_robot_groups
        for work_id in group.work_ids
    }
    active_plan_for_selection = (
        state.get("replan_base_plan")
        or snapshot.get("redis", {}).get("active_plan")
        or {}
    )
    source_plan = (
        active_plan_for_selection
        if isinstance(active_plan_for_selection, dict)
        else {}
    )
    source_required_tasks = [
        row
        for row in (source_plan.get("required_tasks") or [])
        if isinstance(row, dict) and row.get("task_id")
    ]
    source_scheduled_tasks = [
        row
        for row in (
            (source_plan.get("cuopt_plan") or {}).get("scheduled_tasks") or []
        )
        if isinstance(row, dict) and row.get("task_id")
    ]
    source_scheduled_by_task = {
        str(row["task_id"]): row for row in source_scheduled_tasks
    }
    active_plan_work_ids = {
        canonical_work_id(
            str(
                row.get("work_id")
                or str(row.get("task_id") or "").split(":", 1)[0]
            )
        )
        for row in (
            active_plan_for_selection.get("cuopt_plan", {}).get(
                "scheduled_tasks", []
            )
        )
        if row.get("work_id") or row.get("task_id")
    }
    known_work_ids = {
        canonical_work_id(str(row.get("work_id")))
        for row in sql.get("works", [])
        if row.get("work_id")
    }
    known_work_ids.update(
        canonical_work_id(operation.work_id or operation.operation_id)
        for operation in interpretation.inventory_operations
    )
    # Runtime event replans can contain synthetic or not-yet-persisted tasks.
    # The source plan is an explicit contract, so these work IDs are valid
    # planning scope even before they are persisted as SQL work rows.
    known_work_ids.update(
        canonical_work_id(
            str(
                row.get("work_id")
                or str(row.get("task_id") or "").split(":", 1)[0]
            )
        )
        for row in (*source_required_tasks, *source_scheduled_tasks)
        if row.get("work_id") or row.get("task_id")
    )


    def scoped_work_id(value: object) -> str:
        return canonical_work_id(str(value).split(":", 1)[0])

    scope_seeds = {
        scoped_work_id(value)
        for value in (
            *interpretation.target_task_ids,
            *scope.fixed_task_ids,
            *scope.changeable_task_ids,
            *scope.affected_task_ids,
        )
        if value
    }
    scope_seeds.update(
        canonical_work_id(row.work_id)
        for row in interpretation.scheduled_task_constraints
    )
    scope_seeds.update(
        canonical_work_id(work_id)
        for group in interpretation.same_robot_groups
        for work_id in group.work_ids
    )
    scope_seeds.update(
        canonical_work_id(work_id)
        for row in explicit_dependencies
        for work_id in (
            row.predecessor_work_id,
            row.successor_work_id,
        )
    )
    if scope.plan_mode in {"INSERT_TASK", "LOCAL_REPLAN", "GLOBAL_REPLAN"}:
        scope_seeds.update(active_plan_work_ids)

    explicit_task_scope = (
        "EXPLICIT_TASK_SCOPE_ONLY" in interpretation.hard_constraints
        and bool(interpretation.target_task_ids)
    )
    if explicit_task_scope:
        requested_scope = {
            scoped_work_id(value) for value in interpretation.target_task_ids
        }
        plan_scope_work_ids = sorted(requested_scope & known_work_ids)
        scoped_dependencies = [
            row
            for row in interpretation.task_dependencies
            if canonical_work_id(row.predecessor_work_id) in requested_scope
            and canonical_work_id(row.successor_work_id) in requested_scope
        ]
        ignored_count = len(interpretation.task_dependencies) - len(
            scoped_dependencies
        )
        dependency_warnings = (
            [f"OUT_OF_SCOPE_DEPENDENCIES_IGNORED:{ignored_count}"]
            if ignored_count
            else []
        )
        dependency_scope_errors = [
            f"TARGET_WORK_NOT_IN_SNAPSHOT:{work_id}"
            for work_id in sorted(requested_scope - known_work_ids)
        ]
    else:
        (
            scoped_dependencies,
            plan_scope_work_ids,
            dependency_warnings,
            dependency_scope_errors,
        ) = scope_dependency_graph(
            interpretation.task_dependencies,
            seed_work_ids=scope_seeds,
            known_work_ids=known_work_ids,
        )
    interpretation.task_dependencies = scoped_dependencies
    plan_scope_work_id_set = set(plan_scope_work_ids)
    interpretation.scheduled_task_constraints = [
        persisted_constraints[key]
        for key in sorted(persisted_constraints)
        if key in plan_scope_work_id_set
    ]
    constraints_by_work = {
        canonical_work_id(row.work_id): row
        for row in interpretation.scheduled_task_constraints
    }
    dependency_order, dependency_graph_errors = validate_dependency_graph(
        interpretation.task_dependencies,
        plan_scope_work_ids,
    )
    dependency_errors = [
        *dependency_scope_errors,
        *dependency_graph_errors,
    ]
    failed_robot_ids = {
        str(row.get("robot_id"))
        for row in snapshot["redis"].get("robots", [])
        if str(row.get("last_event") or row.get("status") or "").upper()
        in {"ROBOT_FAILED", "FAILED", "OFFLINE", "MAINTENANCE"}
    }
    inventory_blocked = set(state.get("inventory_blocked_work_ids", []))
    precheck_by_work = {
        str(row.get("work_id")): ItemInventoryResult.model_validate(row)
        for row in (
            state.get("inventory_timeline_validation", {}).get("item_results")
            or state.get("inventory_feasibility", {}).get("item_results", [])
        )
        if row.get("work_id")
    }
    business_deadline_by_work = {
        str(operation.work_id): operation.required_by
        for operation in (
            InventoryOperationRequest.model_validate(row)
            for row in state.get("inventory_operations", [])
        )
        if operation.work_id
        and operation.operation_type == "OUTBOUND"
        and operation.required_by is not None
    }

    for work in sql.get("works", []):
        work_id = str(work["work_id"])
        if work_id in inventory_blocked:
            continue
        if canonical_work_id(work_id) not in plan_scope_work_id_set:
            continue
        status = str(work.get("status", ""))
        if status == "COMPLETED":
            continue
        related_to_change = (
            not changeable
            or work_id in changeable
            or any(task_id.startswith(work_id + ":") for task_id in changeable)
        )
        frozen = (
            work_id in fixed
            or (
                status == "EXECUTING"
                and str(work.get("assigned_robot_id")) not in failed_robot_ids
            )
            or (scope.plan_mode == "LOCAL_REPLAN" and not related_to_change)
        )
        work_inventory_result = precheck_by_work.get(work_id)
        work_allocations = (
            [
                {
                    "warehouse_item_id": row.warehouse_item_id,
                    "lot_id": row.lot_id,
                    "item_id": work_inventory_result.item_id,
                    "node_id": row.storage_node_id,
                    "storage_node_id": row.storage_node_id,
                    "quantity": row.quantity_boxes,
                    "quantity_boxes": row.quantity_boxes,
                    "available_at": row.available_at,
                    "source_type": row.source_type,
                    "inbound_source_id": row.inbound_source_id,
                }
                for row in work_inventory_result.lot_allocations
            ]
            if work_inventory_result
            and work_inventory_result.operation_type == "OUTBOUND"
            else []
        )
        tasks.extend(
            tasks_from_work(
                work,
                frozen=frozen,
                business_deadline=business_deadline_by_work.get(work_id),
                constraint=constraints_by_work.get(canonical_work_id(work_id)),
                dependencies=interpretation.task_dependencies,
                same_robot_group=same_robot_by_work.get(
                    canonical_work_id(work_id)
                ),
                inventory_allocations=work_allocations,
            )
        )

    fixed_robot_by_operation = {
        canonical_task_id(row.task_id): row.robot_id
        for row in interpretation.fixed_robot_assignments
    }

    if (
        scope.include_new_command
        and interpretation.intent == "OUTBOUND"
        and not interpretation.inventory_operations
    ):
        if not interpretation.item_ids or not interpretation.quantity:
            raise RuntimeError("OUTBOUND 작업 생성에 item_ids와 quantity가 필요합니다.")
        target_candidates = interpretation.target_node_ids or special_nodes(
            snapshot,
            interpretation.target_node_type or "OUTBOUND",
        )
        if not target_candidates:
            raise RuntimeError("OUTBOUND 목적지 노드 후보가 없습니다.")

        trip_capacity = outbound_trip_capacity(
            sql.get("robots", []),
            included_robot_ids=interpretation.included_robot_ids,
            excluded_robot_ids=interpretation.excluded_robot_ids,
        )
        for item_id in interpretation.item_ids:
            allocations = allocate_fefo(
                sql["inventory"],
                item_id,
                interpretation.quantity,
            )
            trip_index = 0
            for raw_allocation in allocations:
                pairs = capacity_trip_pairs(
                    raw_allocation,
                    trip_capacity,
                    prefix_base=f"{state['command']['command_id']}:{item_id}",
                    start_index=trip_index + 1,
                )
                for pair in pairs:
                    prefix = pair["prefix"]
                    allocation = pair["allocation"]
                    pick_id = pair["pick_id"]
                    drop_id = pair["drop_id"]
                    tasks.append(
                        AtomicTask(
                            task_id=pick_id,
                            action="PICK",
                            item_id=item_id,
                            quantity=allocation["quantity"],
                            source_candidates=[allocation["node_id"]],
                            target_candidates=[allocation["node_id"]],
                            priority=priority_number(interpretation.priority),
                            deadline=interpretation.deadline,
                            predecessors=pair["pick_predecessors"],
                            same_robot_group=prefix,
                            inventory_allocations=[allocation],
                        )
                    )
                    tasks.append(
                        AtomicTask(
                            task_id=drop_id,
                            action="DROP",
                            item_id=item_id,
                            quantity=allocation["quantity"],
                            source_candidates=[allocation["node_id"]],
                            target_candidates=target_candidates,
                            priority=priority_number(interpretation.priority),
                            deadline=interpretation.deadline,
                            predecessors=pair["drop_predecessors"],
                            same_robot_group=prefix,
                            inventory_allocations=[allocation],
                        )
                    )
                trip_index += len(pairs)

    existing_work_ids = {
        str(row.get("work_id")) for row in sql.get("works", []) if row.get("work_id")
    }
    feasibility_by_operation = {
        str(row.get("operation_id")): ItemInventoryResult.model_validate(row)
        for row in state.get("inventory_feasibility", {}).get("item_results", [])
    }
    for operation in interpretation.inventory_operations:
        if operation.operation_type != "OUTBOUND":
            continue
        operation_ref = operation.work_id or operation.operation_id
        if operation_ref in inventory_blocked:
            continue
        if operation.work_id and operation.work_id in existing_work_ids:
            # The corresponding work was already converted by tasks_from_work.
            continue
        result = feasibility_by_operation.get(operation.operation_id)
        if result is None or result.planned_quantity_boxes <= 0:
            continue
        target_candidates = interpretation.target_node_ids or special_nodes(
            snapshot, interpretation.target_node_type or "OUTBOUND"
        )
        if not target_candidates:
            raise RuntimeError("OUTBOUND 목적지 노드 후보가 없습니다.")
        fixed_robot_id = fixed_robot_by_operation.get(
            canonical_task_id(operation_ref)
        )
        trip_capacity = outbound_trip_capacity(
            sql.get("robots", []),
            fixed_robot_id=fixed_robot_id,
            included_robot_ids=interpretation.included_robot_ids,
            excluded_robot_ids=interpretation.excluded_robot_ids,
        )
        raw_allocations = []
        for lot in result.lot_allocations:
            if lot.storage_node_id is None:
                continue
            raw_allocations.append(
                {
                    "warehouse_item_id": lot.warehouse_item_id,
                    "item_id": operation.item_id,
                    "node_id": lot.storage_node_id,
                    "storage_node_id": lot.storage_node_id,
                    "quantity": lot.quantity_boxes,
                    "quantity_boxes": lot.quantity_boxes,
                    "lot_id": lot.lot_id,
                    "available_at": lot.available_at,
                    "source_type": lot.source_type,
                    "inbound_source_id": lot.inbound_source_id,
                }
            )
        constraint = constraints_by_work.get(canonical_work_id(operation_ref))
        pairs = capacity_trip_groups(
            raw_allocations,
            trip_capacity,
            prefix_base=operation_ref,
        )
        for pair in pairs:
            prefix = pair["prefix"]
            pick_id = pair["pick_id"]
            drop_id = pair["drop_id"]
            source_node = int(pair["source_node"])
            quantity = int(pair["quantity_boxes"])
            lot_available_at = pair.get("available_at")
            earliest_values = [
                value
                for value in (lot_available_at, constraint.earliest_start if constraint else None)
                if value is not None
            ]
            earliest_start = max(earliest_values) if earliest_values else None
            latest_finish = constraint.latest_finish if constraint else None
            time_constraint_type = (
                constraint.time_constraint_type if constraint else "DEADLINE"
                if operation.required_at is not None else "ASAP"
            )
            deadline = latest_finish or operation.required_at
            tasks.append(
                AtomicTask(
                    task_id=pick_id,
                    work_id=operation.work_id or operation.operation_id,
                    action="PICK",
                    item_id=operation.item_id,
                    quantity=quantity,
                    source_candidates=[source_node],
                    target_candidates=[source_node],
                    priority=priority_number(operation.priority),
                    deadline=deadline,
                    earliest_start=earliest_start,
                    latest_finish=latest_finish,
                    time_constraint_type=time_constraint_type,
                    predecessors=pair["pick_predecessors"],
                    same_robot_group=prefix,
                    inventory_allocations=list(pair["allocations"]),
                )
            )
            tasks.append(
                AtomicTask(
                    task_id=drop_id,
                    work_id=operation.work_id or operation.operation_id,
                    action="DROP",
                    item_id=operation.item_id,
                    quantity=quantity,
                    source_candidates=[source_node],
                    target_candidates=target_candidates,
                    priority=priority_number(operation.priority),
                    deadline=deadline,
                    earliest_start=earliest_start,
                    latest_finish=latest_finish,
                    time_constraint_type=time_constraint_type,
                    predecessors=pair["drop_predecessors"],
                    same_robot_group=prefix,
                    inventory_allocations=list(pair["allocations"]),
                )
            )

    # Materialize runtime tasks carried by the event source plan. SQL remains
    # authoritative for persisted works, but it cannot describe temporary
    # execution tasks such as a live MOVE segment. Preserve those tasks and
    # apply the same fixed/changeable policy used for persisted works.
    existing_task_ids = {task.task_id for task in tasks}
    for raw in source_required_tasks:
        task_id = str(raw.get("task_id") or "")
        if not task_id or task_id in existing_task_ids:
            continue
        work_id = str(raw.get("work_id") or task_id.split(":", 1)[0])
        if (
            plan_scope_work_id_set
            and canonical_work_id(work_id) not in plan_scope_work_id_set
        ):
            continue
        scheduled = source_scheduled_by_task.get(task_id, {})
        payload = dict(raw)
        payload["task_id"] = task_id
        payload["work_id"] = work_id
        payload.setdefault("action", scheduled.get("action") or "MOVE")
        if not payload.get("source_candidates") and scheduled.get("source_node") is not None:
            payload["source_candidates"] = [int(scheduled["source_node"])]
        if not payload.get("target_candidates") and scheduled.get("target_node") is not None:
            payload["target_candidates"] = [int(scheduled["target_node"])]
        if payload.get("assigned_robot_id") is None and scheduled.get("robot_id"):
            payload["assigned_robot_id"] = str(scheduled["robot_id"])
        related_to_change = (
            not changeable
            or task_id in changeable
            or work_id in changeable
            or any(value.startswith(work_id + ":") for value in changeable)
        )
        payload["frozen"] = bool(
            task_id in fixed
            or work_id in fixed
            or (scope.plan_mode == "LOCAL_REPLAN" and not related_to_change)
        )
        task = AtomicTask.model_validate(payload)
        tasks.append(task)
        existing_task_ids.add(task.task_id)

    inbound_route_selections: list[dict[str, Any]] = []
    for operation in interpretation.inventory_operations:
        if operation.operation_type != "INBOUND":
            continue
        operation_ref = operation.work_id or operation.operation_id
        if operation_ref in inventory_blocked:
            continue
        if operation.work_id and operation.work_id in existing_work_ids:
            # Existing SQL work is already represented by tasks_from_work.
            continue
        result = feasibility_by_operation.get(operation.operation_id)
        planned_quantity = (
            result.planned_quantity_boxes
            if result is not None
            else operation.quantity_boxes
        )
        if planned_quantity <= 0:
            continue

        explicit_sources = list(interpretation.source_node_ids)
        inbound_sources = explicit_sources or special_nodes(snapshot, "INBOUND")
        if operation.storage_node_id is not None:
            storage_targets = [int(operation.storage_node_id)]
        elif interpretation.target_node_type == "STORAGE" and interpretation.target_node_ids:
            storage_targets = list(interpretation.target_node_ids)
        else:
            storage_targets = special_nodes(snapshot, "STORAGE")

        source_node, target_node, selection = select_inbound_route_nodes(
            snapshot,
            source_candidates=inbound_sources,
            target_candidates=storage_targets,
        )
        selection.update(
            {
                "operation_id": operation.operation_id,
                "work_id": operation_ref,
                "item_id": operation.item_id,
                "quantity_boxes": planned_quantity,
                "source_selection": (
                    "EXPLICIT_INBOUND_NODE"
                    if explicit_sources
                    else "ACTIVE_INBOUND_NODE"
                ),
                "destination_selection": (
                    "OPERATION_STORAGE_NODE"
                    if operation.storage_node_id is not None
                    else "COMMAND_STORAGE_NODE"
                    if interpretation.target_node_type == "STORAGE"
                    and interpretation.target_node_ids
                    else "ACTIVE_STORAGE_NODE"
                ),
            }
        )
        inbound_route_selections.append(selection)

        fixed_robot_id = fixed_robot_by_operation.get(
            canonical_task_id(operation_ref)
        )
        trip_capacity = outbound_trip_capacity(
            sql.get("robots", []),
            fixed_robot_id=fixed_robot_id,
            included_robot_ids=interpretation.included_robot_ids,
            excluded_robot_ids=interpretation.excluded_robot_ids,
        )
        pairs = capacity_trip_pairs(
            {
                "item_id": operation.item_id,
                "node_id": source_node,
                "quantity": planned_quantity,
                "quantity_boxes": planned_quantity,
            },
            trip_capacity,
            prefix_base=operation_ref,
        )
        constraint = constraints_by_work.get(canonical_work_id(operation_ref))
        arrival_candidates = [
            value
            for value in (
                operation.actual_arrival_at,
                operation.expected_arrival_at,
                constraint.earliest_start if constraint else None,
            )
            if value is not None
        ]
        earliest_start = max(arrival_candidates) if arrival_candidates else None
        latest_finish = constraint.latest_finish if constraint else None
        time_constraint_type = (
            constraint.time_constraint_type
            if constraint
            else "DEADLINE"
            if operation.expected_available_at is not None
            else "ASAP"
        )
        deadline = latest_finish or operation.expected_available_at

        for pair in pairs:
            prefix = pair["prefix"]
            quantity = int(pair["allocation"]["quantity_boxes"])
            tasks.append(
                AtomicTask(
                    task_id=pair["pick_id"],
                    work_id=operation_ref,
                    action="PICK",
                    item_id=operation.item_id,
                    quantity=quantity,
                    source_candidates=[source_node],
                    target_candidates=[source_node],
                    priority=priority_number(operation.priority),
                    deadline=deadline,
                    earliest_start=earliest_start,
                    latest_finish=latest_finish,
                    time_constraint_type=time_constraint_type,
                    predecessors=pair["pick_predecessors"],
                    same_robot_group=prefix,
                    inventory_allocations=[],
                )
            )
            tasks.append(
                AtomicTask(
                    task_id=pair["drop_id"],
                    work_id=operation_ref,
                    action="DROP",
                    item_id=operation.item_id,
                    quantity=quantity,
                    source_candidates=[source_node],
                    target_candidates=[target_node],
                    priority=priority_number(operation.priority),
                    deadline=deadline,
                    earliest_start=earliest_start,
                    latest_finish=latest_finish,
                    time_constraint_type=time_constraint_type,
                    predecessors=pair["drop_predecessors"],
                    same_robot_group=prefix,
                    inventory_allocations=[],
                )
            )

    # P16.5.14 robot-failure recovery overlay.  A confirmed carried load
    # cannot replay the original storage PICK. Replace the failed robot's
    # original PICK/DROP pair with a synthetic handover chain rooted at the
    # safe-stop node. The overlay is server-generated in ScenarioDefinition;
    # client runtime payloads never reach this path.
    scenario_definition = state.get("command", {}).get("scenario_definition") or {}
    recovery_replace_ids = {
        str(value)
        for value in scenario_definition.get("recovery_replace_task_ids", [])
        if value
    }
    recovery_task_rows = [
        row
        for row in scenario_definition.get("recovery_tasks", [])
        if isinstance(row, dict) and row.get("task_id")
    ]
    recovery_overlay = scenario_definition.get("robot_failure_recovery") or {}
    if recovery_replace_ids or recovery_task_rows:
        tasks = [task for task in tasks if task.task_id not in recovery_replace_ids]
        existing_recovery_ids = {task.task_id for task in tasks}
        for raw in recovery_task_rows:
            task = AtomicTask.model_validate(raw)
            if task.task_id in existing_recovery_ids:
                continue
            tasks.append(task)
            existing_recovery_ids.add(task.task_id)
        dependency_warnings = [
            *dependency_warnings,
            (
                "ROBOT_FAILURE_RECOVERY_OVERLAY:"
                f"{recovery_overlay.get('strategy') or 'UNKNOWN'}:"
                f"replaced={len(recovery_replace_ids)}:"
                f"inserted={len(recovery_task_rows)}"
            ),
        ]

    schedule_validation = {
        "valid": not dependency_errors,
        "errors": dependency_errors,
        "dependency_order": dependency_order,
        "constraint_count": len(interpretation.scheduled_task_constraints),
        "dependency_count": len(interpretation.task_dependencies),
        "scope_work_ids": plan_scope_work_ids,
        "warnings": dependency_warnings,
    }
    if recovery_replace_ids or recovery_task_rows:
        schedule_validation.update(
            {
                "robot_failure_recovery": deepcopy(recovery_overlay),
                "recovery_replace_task_ids": sorted(recovery_replace_ids),
                "recovery_task_ids": [
                    str(row.get("task_id")) for row in recovery_task_rows
                ],
            }
        )
    if inbound_route_selections:
        schedule_validation["inbound_route_selections"] = inbound_route_selections
    return {
        "interpretation": interpretation.model_dump(mode="json"),
        "required_tasks": [task.model_dump(mode="json") for task in tasks],
        "schedule_validation": schedule_validation,
        "cuopt_plan": {},
        "collision_plan": {},
        "simulation": {},
        "plan_validation": {},
        "impact": {},
        "final_status": (
            "VALIDATION_FAILED" if dependency_errors else "TASKS_SELECTED"
        ),
        "errors": dependency_errors,
        "warnings": dependency_warnings,
        "trace": (
            trace(
                "build_task_dependencies",
                dependency_count=len(interpretation.task_dependencies),
            )
            + trace(
                "validate_dependency_graph",
                valid=not dependency_errors,
                order=dependency_order,
                errors=dependency_errors,
                warnings=dependency_warnings,
                scope_work_ids=plan_scope_work_ids,
            )
            + trace(
                "apply_time_windows",
                constraint_count=len(interpretation.scheduled_task_constraints),
            )
            + trace("select_required_tasks", task_count=len(tasks))
            + (
                trace(
                    "robot_failure_recovery_overlay",
                    strategy=recovery_overlay.get("strategy"),
                    replaced_task_ids=sorted(recovery_replace_ids),
                    recovery_task_ids=[
                        str(row.get("task_id")) for row in recovery_task_rows
                    ],
                )
                if recovery_replace_ids or recovery_task_rows
                else []
            )
        ),
    }


def merge_robot_state(
    sql_robots: list[dict[str, Any]],
    live_robots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    live_by_id = {str(row.get("robot_id")): row for row in live_robots}
    merged = []
    for robot in sql_robots:
        row = dict(robot)
        live = live_by_id.get(str(robot["robot_id"]), {})
        if live.get("node_id") not in (None, ""):
            row["node_id"] = int(live["node_id"])
        if live.get("battery") not in (None, ""):
            row["battery"] = float(live["battery"])
        row["live_status"] = live.get("last_event")
        merged.append(row)
    return merged


def build_optimization_problem_node(state: PlanningState) -> dict[str, Any]:
    snapshot = state["snapshot"]
    settings = get_settings()
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    scope = ScopeDecision.model_validate(state["scope"])
    optimization_profile, optimization_weights = resolve_optimization_weights(
        state["command"]["text"]
    )
    optimization_weight_source = (
        "DEFAULT" if optimization_profile == "DEFAULT" else "COMMAND_TEXT_PROFILE"
    )
    if interpretation.optimization_priority:
        optimization_profile = interpretation.optimization_priority
        default_weights = OptimizationWeights()
        profile_weights = optimization_weights_for_priority(
            interpretation.optimization_priority
        )
        scenario_weights = (
            state["command"].get("scenario_definition") or {}
        ).get("optimization_weights") or {}
        has_explicit_weights = bool(scenario_weights) or (
            interpretation.optimization_priority == "USER_DEFINED"
        ) or interpretation.optimization_weights not in (
            default_weights,
            profile_weights,
        )
        if has_explicit_weights:
            # Explicit numeric weights win when a named priority and weights
            # are both present. The priority remains the displayed profile.
            optimization_weights = interpretation.optimization_weights
            optimization_weight_source = "EXPLICIT_WEIGHTS"
        else:
            optimization_weights = profile_weights
            optimization_weight_source = "PRIORITY_PROFILE"
    robot_aliases = {
        canonical_robot_id(row.get("robot_id")): str(row.get("robot_id"))
        for row in snapshot["sql"].get("robots", [])
        if row.get("robot_id") is not None
    }
    excluded_robot_ids = {
        canonical_robot_id(value)
        for value in interpretation.excluded_robot_ids
    }
    battery_overrides: dict[str, float] = {}
    scenario_robot_overrides = (
        state["command"].get("scenario_definition") or {}
    ).get("robot_state_overrides") or {}
    for event in interpretation.hypothetical_events:
        if event.event_type == "ROBOT_FAILURE":
            excluded_robot_ids.update(
                canonical_robot_id(value) for value in event.target_ids
            )
        elif event.event_type == "LOW_BATTERY":
            if event.parameters.battery_percent is None:
                excluded_robot_ids.update(
                    canonical_robot_id(value) for value in event.target_ids
                )
            else:
                for target_id in event.target_ids:
                    battery_overrides[canonical_robot_id(target_id)] = float(
                        event.parameters.battery_percent
                    )

    selected_robots = []
    for merged_robot in merge_robot_state(
        snapshot["sql"]["robots"],
        snapshot["redis"]["robots"],
    ):
        robot_key = canonical_robot_id(merged_robot.get("robot_id"))
        if robot_key in excluded_robot_ids:
            continue
        if robot_key in battery_overrides:
            merged_robot["snapshot_battery"] = float(
                merged_robot.get("battery") or 0.0
            )
            merged_robot["battery"] = battery_overrides[robot_key]
            merged_robot["battery_source"] = "COMMAND_HYPOTHETICAL_OVERRIDE"
        runtime_override = next(
            (
                value
                for key, value in scenario_robot_overrides.items()
                if canonical_robot_id(key) == robot_key
            ),
            None,
        )
        if isinstance(runtime_override, dict):
            if runtime_override.get("node_id") is not None:
                merged_robot["snapshot_node_id"] = merged_robot.get("node_id")
                merged_robot["node_id"] = int(runtime_override["node_id"])
                merged_robot["position_source"] = "EXECUTION_EVENT_OVERRIDE"
            if runtime_override.get("battery") is not None:
                merged_robot["snapshot_battery"] = float(
                    merged_robot.get("battery") or 0.0
                )
                merged_robot["battery"] = float(runtime_override["battery"])
                merged_robot["battery_source"] = "EXECUTION_EVENT_OVERRIDE"
            if runtime_override.get("status") is not None:
                merged_robot["status"] = str(runtime_override["status"])
                merged_robot["status_source"] = "EXECUTION_EVENT_OVERRIDE"
        selected_robots.append(merged_robot)
    if interpretation.robot_limit is not None:
        selected_robots = selected_robots[: interpretation.robot_limit]

    assignment_by_work = {
        canonical_task_id(row.task_id): robot_aliases.get(row.robot_id, row.robot_id)
        for row in interpretation.fixed_robot_assignments
    }
    problem_tasks = deepcopy(state["required_tasks"])
    for task in problem_tasks:
        work_key = canonical_task_id(task.get("work_id") or task.get("task_id"))
        if work_key in assignment_by_work:
            # A fixed robot assignment constrains *who* performs the task; it
            # must not freeze the previous schedule during LOCAL_REPLAN.  The
            # scope's fixed_task_ids / task.frozen flag separately controls
            # whether a task may be rescheduled.
            task["assigned_robot_id"] = assignment_by_work[work_key]
    command_closures = [
        {"node_id": node_id, "reason": "COMMAND_ASSUMPTION"}
        for node_id in interpretation.assumed_closed_node_ids
    ] + [
        {
            "from_node": edge.from_node,
            "to_node": edge.to_node,
            "bidirectional": edge.bidirectional,
            "reason": "COMMAND_ASSUMPTION",
        }
        for edge in interpretation.assumed_closed_edges
    ]
    for edge_id in interpretation.excluded_edge_ids:
        matched = [
            edge
            for edge in snapshot["graph"].get("edges", [])
            if str(edge_id) in _edge_identifier_aliases(edge)
        ]
        command_closures.extend(
            {
                "edge_id": str(edge_id),
                "from_node": int(edge["from_node"]),
                "to_node": int(edge["to_node"]),
                "bidirectional": str(edge.get("direction", "ONE_WAY")).upper()
                in {"BOTH", "BIDIRECTIONAL"},
                "reason": "SCENARIO_EDGE_EXCLUSION",
            }
            for edge in matched
        )
    if any(
        event.event_type == "CHARGER_UNAVAILABLE"
        for event in interpretation.hypothetical_events
    ):
        command_closures.extend(
            {
                "node_id": int(node["node_id"]),
                "reason": "HYPOTHETICAL_CHARGER_UNAVAILABLE",
            }
            for node in snapshot["graph"].get("nodes", [])
            if str(node.get("node_type") or "").upper() == "CHARGER"
        )
    planning_reference_time = (
        interpretation.planning_reference.utc_at
        if interpretation.planning_reference is not None
        else as_utc_datetime(snapshot["captured_at"], field_name="captured_at")
    )
    command_text = str(state.get("command", {}).get("text") or "")
    congestion_node_ids: set[int] = set()
    for match in re.finditer(
        r"(?:경로\s*)?노드\s*(\d+)\s*(?:번)?[^.。!?]{0,50}"
        r"(?:몰리지|혼잡|분산|우회|피해|피하도록)",
        command_text,
        re.IGNORECASE,
    ):
        congestion_node_ids.add(int(match.group(1)))
    warehouse_timezone, warehouse_timezone_name, _ = resolve_warehouse_timezone(
        getattr(settings, "warehouse_timezone", "")
    )
    scheduled_start_values = []
    for task_row in problem_tasks:
        raw_start = task_row.get("earliest_start")
        if raw_start in (None, ""):
            continue
        try:
            scheduled_start_values.append(
                as_utc_datetime(raw_start, field_name="task_earliest_start")
            )
        except (TypeError, ValueError):
            continue
    earliest_scheduled_start = min(scheduled_start_values, default=None)
    # A candidate for a later warehouse operating date is not active yet.
    # Its robots must not reserve chargers, holding nodes or route vertices on
    # the current date. Same-day daily plans continue to manage their initial
    # idle period explicitly.
    defer_initial_pre_activation = bool(
        earliest_scheduled_start is not None
        and earliest_scheduled_start.astimezone(warehouse_timezone).date()
        > planning_reference_time.astimezone(warehouse_timezone).date()
    )

    # A daily multi-window simulation benefits from local warehouse-aware robot
    # balancing. Explicit user robot assignments remain hard constraints.
    allow_local_robot_rebalance = bool(
        interpretation.daily_schedule_requested
        and len(selected_robots) > 1
        and not interpretation.fixed_robot_assignments
        and scope.plan_mode in {"INITIAL_PLAN", "GLOBAL_REPLAN"}
    )
    problem = {
        "warehouse_id": state["command"]["warehouse_id"],
        "captured_at": snapshot["captured_at"],
        "reference_time": planning_reference_time.isoformat(),
        "time_step_seconds": settings.time_step_seconds,
        "max_mapf_time_steps": int(getattr(settings, "max_mapf_time_steps", 720)),
        "warehouse_timezone": warehouse_timezone_name,
        "defer_initial_pre_activation": defer_initial_pre_activation,
        "earliest_scheduled_start": (
            earliest_scheduled_start.isoformat()
            if earliest_scheduled_start is not None
            else None
        ),
        "plan_mode": scope.plan_mode,
        "tasks": problem_tasks,
        "robots": selected_robots,
        "nodes": snapshot["graph"]["nodes"],
        "edges": snapshot["graph"]["edges"],
        "temporary_closures": (
            snapshot["redis"]["temporary_closures"] + command_closures
        ),
        "inventory": snapshot["sql"]["inventory"],
        # Preserve command-level direction and projected allocations for the
        # deterministic simulator.  An inbound PICK is a dock pickup and must
        # never be validated as consumption from warehouse stock.
        "inventory_operations": list(state.get("inventory_operations", [])),
        "active_plan": (
            state.get("replan_base_plan")
            or snapshot["redis"].get("active_plan")
        ),
        "fixed_task_ids": scope.fixed_task_ids,
        "changeable_task_ids": scope.changeable_task_ids,
        "affected_robot_ids": scope.affected_robot_ids,
        "freeze_horizon_seconds": scope.freeze_horizon_seconds,
        "min_robot_battery": settings.min_robot_battery,
        "battery_safety_margin_percent": getattr(
            settings, "battery_safety_margin_percent", 0.5
        ),
        "energy_per_distance": settings.energy_per_distance,
        "charge_target_battery": getattr(settings, "charge_target_battery", 80.0),
        "charge_rate_percent_per_minute": getattr(
            settings, "charge_rate_percent_per_minute", 5.0
        ),
        "optimization_profile": optimization_profile,
        "optimization_weight_source": optimization_weight_source,
        "weights": optimization_weights.model_dump(),
        "hard_constraints": interpretation.hard_constraints,
        "excluded_robot_ids": sorted(excluded_robot_ids),
        "robot_state_overrides": [
            {
                "robot_id": robot_id,
                "battery_percent": battery_percent,
                "source": "COMMAND_HYPOTHETICAL_OVERRIDE",
            }
            for robot_id, battery_percent in sorted(battery_overrides.items())
        ],
        "robot_limit": interpretation.robot_limit,
        "allow_local_robot_rebalance": allow_local_robot_rebalance,
        "parallel_robot_group_penalty": 20.0,
        "congestion_node_ids": sorted(congestion_node_ids),
        "congestion_penalty_steps": 4,
        "idle_allowed_node_types": [
            "PARKING",
            "STAGING",
            "HOLDING",
            "CHARGER_WAITING_AREA",
            "ROBOT_PARKING",
        ],
        "idle_relocation_min_gap_steps": max(
            2,
            math.ceil(60 / settings.time_step_seconds),
        ),
        "idle_whitelist_strict": bool(
            interpretation.daily_schedule_requested
            or "IDLE_ONLY_ON_WHITELISTED_NODE"
            in interpretation.hard_constraints
        ),
        "idle_return_policy": "CHARGER_AREA_FIRST",
        "opportunity_charging_enabled": bool(
            getattr(settings, "opportunity_charging_enabled", True)
            and (
                interpretation.daily_schedule_requested
                or "OPPORTUNITY_CHARGING" in interpretation.hard_constraints
            )
        ),
        "opportunity_charge_min_gap_steps": max(
            2,
            math.ceil(
                float(
                    getattr(settings, "opportunity_charge_min_idle_minutes", 15.0)
                )
                * 60
                / settings.time_step_seconds
            ),
        ),
        "opportunity_charge_target_battery": float(
            getattr(settings, "opportunity_charge_target_battery", 95.0)
        ),
        "opportunity_charge_min_gain_percent": float(
            getattr(settings, "opportunity_charge_min_gain_percent", 2.0)
        ),
        "fixed_robot_assignments": [
            row.model_dump() for row in interpretation.fixed_robot_assignments
        ],
        "task_dependencies": [
            row.model_dump(mode="json") for row in interpretation.task_dependencies
        ],
        "scheduled_task_constraints": [
            row.model_dump(mode="json")
            for row in interpretation.scheduled_task_constraints
        ],
        "insertion_policy": interpretation.insertion_policy,
        "preemption_policy": interpretation.preemption_policy,
        "mapf_replan_policy": deepcopy(state.get("mapf_replan_policy", {})),
        "runtime_partial_replan": {
            "version": "p16.5.12.1",
            "source_plan_version": (
                state["command"].get("scenario_definition") or {}
            ).get("source_plan_version"),
            "affected_robot_ids": (
                state["command"].get("scenario_definition") or {}
            ).get("affected_robot_ids", []),
            "affected_task_ids": (
                state["command"].get("scenario_definition") or {}
            ).get("affected_task_ids", []),
            "protected_task_ids": (
                state["command"].get("scenario_definition") or {}
            ).get("protected_task_ids", []),
            "changeable_task_ids": (
                state["command"].get("scenario_definition") or {}
            ).get("changeable_task_ids", []),
            "freeze_horizon_seconds": (
                state["command"].get("scenario_definition") or {}
            ).get("freeze_horizon_seconds"),
            "robot_state_overrides": deepcopy(scenario_robot_overrides),
        },
    }
    return {
        "optimization_problem": problem,
        "final_status": "OPTIMIZATION_READY",
        "trace": trace(
            "build_optimization_problem",
            optimization_profile=optimization_profile,
            optimization_weight_source=optimization_weight_source,
            weights=optimization_weights.model_dump(),
            reference_time=problem["reference_time"],
            reference_time_source=(
                interpretation.planning_reference.source
                if interpretation.planning_reference is not None
                else "SNAPSHOT_CAPTURED_AT"
            ),
            time_step_seconds=problem["time_step_seconds"],
            warehouse_timezone=warehouse_timezone_name,
            defer_initial_pre_activation=defer_initial_pre_activation,
            earliest_scheduled_start=problem["earliest_scheduled_start"],
            robot_state_overrides=problem["robot_state_overrides"],
            allow_local_robot_rebalance=allow_local_robot_rebalance,
            congestion_node_ids=sorted(congestion_node_ids),
            mapf_replan_policy=problem.get("mapf_replan_policy", {}),
        ),
    }


def inventory_timeline_validation_node(
    state: PlanningState,
) -> dict[str, Any]:
    operations = [
        InventoryOperationRequest.model_validate(row)
        for row in state.get("inventory_operations", [])
    ]
    if not operations:
        result = InventoryFeasibilityResult(
            status="NOT_APPLICABLE", valid=True
        )
        return {
            "inventory_timeline_validation": result.model_dump(mode="json"),
            "trace": trace(
                "inventory_timeline_validation", status="NOT_APPLICABLE"
            ),
        }
    problem = state["optimization_problem"]
    reference_time = as_utc_datetime(
        problem["reference_time"], field_name="optimization_reference_time"
    )
    step_seconds = int(problem["time_step_seconds"])
    scheduled_by_work: dict[str, int] = {}
    for row in state.get("cuopt_plan", {}).get("scheduled_tasks", []):
        work_id = str(row.get("work_id") or "")
        if not work_id:
            continue
        scheduled_by_work[work_id] = min(
            scheduled_by_work.get(work_id, int(row["start_time_step"])),
            int(row["start_time_step"]),
        )
    scheduled_operations = []
    for operation in operations:
        operation_ref = operation.work_id or operation.operation_id
        if operation.operation_type == "OUTBOUND" and operation_ref in scheduled_by_work:
            operation = operation.model_copy(
                update={
                    "required_at": planned_at(
                        reference_time,
                        scheduled_by_work[operation_ref],
                        step_seconds,
                    )
                }
            )
        scheduled_operations.append(operation)
    service = InventoryProjectionService(reference_time)
    snapshot = state["snapshot"]
    result = service.evaluate(
        scheduled_operations,
        current_lots=snapshot["sql"].get("inventory", []),
        future_inbounds=snapshot["sql"].get("inbound_orders", []),
        active_reservations=snapshot.get("redis", {}).get(
            "inventory_reservations", []
        ),
        dependencies=CommandInterpretation.model_validate(
            state["interpretation"]
        ).task_dependencies,
    )
    blocked = set(result.shortage_work_ids) | set(result.blocked_work_ids)
    update: dict[str, Any] = {
        "inventory_timeline_validation": result.model_dump(mode="json"),
        "trace": trace(
            "inventory_timeline_validation",
            status=result.status,
            shortage_work_ids=result.shortage_work_ids,
            blocked_work_ids=result.blocked_work_ids,
        ),
    }
    if blocked:
        required_tasks = [
            row
            for row in state.get("required_tasks", [])
            if str(row.get("work_id") or row.get("task_id", "").split(":", 1)[0])
            not in blocked
        ]
        plan = deepcopy(state.get("cuopt_plan", {}))
        plan["scheduled_tasks"] = [
            row
            for row in plan.get("scheduled_tasks", [])
            if str(row.get("work_id") or row.get("task_id", "").split(":", 1)[0])
            not in blocked
        ]
        plan.setdefault("metadata", {})["inventory_blocked_work_ids"] = sorted(
            blocked
        )
        update.update(
            {
                "required_tasks": required_tasks,
                "cuopt_plan": plan,
                "inventory_blocked_work_ids": sorted(blocked),
            }
        )
    return update


def _low_battery_runtime_robot_ids(problem: dict[str, Any]) -> set[str]:
    runtime = problem.get("runtime_partial_replan") or {}
    overrides = runtime.get("robot_state_overrides") or {}
    return {
        str(robot_id)
        for robot_id, row in overrides.items()
        if isinstance(row, dict)
        and str(row.get("event_type") or "").upper() == "LOW_BATTERY"
    }


def _robots_with_business_tasks(plan: CuOptPlan, robot_ids: set[str]) -> set[str]:
    return {
        str(task.robot_id)
        for task in plan.scheduled_tasks
        if str(task.robot_id) in robot_ids
        and str(task.action).upper() in {"PICK", "DROP", "MOVE"}
    }


def _robots_with_charge_tasks(plan: CuOptPlan) -> set[str]:
    return {
        str(task.robot_id)
        for task in plan.scheduled_tasks
        if str(task.action).upper() == "CHARGE"
    }


def _ensure_low_battery_charge_retention(
    *,
    outcome: Any,
    recovery_problem: dict[str, Any],
    settings: Any,
    charge_visit_contract: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Bounded safety recovery when a low-battery chain loses its CHARGE.

    Managed cuOpt remains the normal assignment/order provider.  This guard is
    only entered when the server-authoritative LOW_BATTERY override still has
    business work assigned to the reporting robot but the final optimizer
    result contains no CHARGE for that robot.  The deterministic local
    optimizer is then run once against the same (possibly enriched two-pass)
    problem.  It must retain every task and restore a safe charge visit or the
    planning stage fails closed.
    """

    low_battery_robots = _low_battery_runtime_robot_ids(recovery_problem)
    required_robots = _robots_with_business_tasks(outcome.plan, low_battery_robots)
    missing = sorted(required_robots - _robots_with_charge_tasks(outcome.plan))
    evidence = {
        "enabled": bool(low_battery_robots),
        "low_battery_robot_ids": sorted(low_battery_robots),
        "business_task_robot_ids": sorted(required_robots),
        "missing_charge_robot_ids_before_recovery": missing,
        "recovery_used": False,
        "recovery_provider": None,
        "status": "NOT_REQUIRED" if not missing else "PENDING",
    }
    if not missing:
        return outcome, evidence

    reason = "LOW_BATTERY_CHARGE_TASK_MISSING:" + ",".join(missing)
    recovery = optimize_problem_locally(
        recovery_problem,
        settings,
        requested_provider="LOW_BATTERY_SAFETY_RECOVERY",
        fallback_reason=reason,
        warnings=[
            "저배터리 부분 재계획의 CHARGE 작업이 최종 optimizer 결과에서 "
            "누락되어 결정론적 안전 복구를 한 번 수행했습니다."
        ],
        attempts=[
            *list(outcome.execution.get("attempts") or []),
            {
                "provider": "LOW_BATTERY_CHARGE_RETENTION_VALIDATION",
                "status": "FAILED",
                "error_code": reason,
            },
            {
                "provider": "CPU_LOW_BATTERY_SAFETY_RECOVERY",
                "status": "SUCCESS",
            },
        ],
    )
    remaining = sorted(
        _robots_with_business_tasks(recovery.plan, low_battery_robots)
        - _robots_with_charge_tasks(recovery.plan)
    )
    if recovery.plan.unassigned_task_ids or remaining:
        details = []
        if recovery.plan.unassigned_task_ids:
            details.append(
                "unassigned=" + ",".join(recovery.plan.unassigned_task_ids)
            )
        if remaining:
            details.append("missing_charge=" + ",".join(remaining))
        raise RuntimeError(
            "LOW_BATTERY_CHARGE_RETENTION_FAILED:" + ";".join(details)
        )

    two_pass = outcome.execution.get("charge_visit_two_pass")
    if two_pass:
        recovery.execution["charge_visit_two_pass"] = dict(two_pass)
    evidence.update(
        {
            "recovery_used": True,
            "recovery_provider": recovery.execution.get("used_provider"),
            "missing_charge_robot_ids_after_recovery": remaining,
            "status": "RECOVERED",
        }
    )
    recovery.execution["low_battery_charge_retention"] = evidence
    recovery.plan.metadata = {
        **recovery.plan.metadata,
        **(
            {"charge_visit_optimization_contract": charge_visit_contract}
            if charge_visit_contract
            else {}
        ),
        "optimizer_execution": recovery.execution,
        "low_battery_charge_retention": evidence,
    }
    return recovery, evidence


def optimizer_node(state: PlanningState) -> dict[str, Any]:
    settings = get_settings()
    try:
        base_optimization_problem = state["optimization_problem"]
        optimization_problem = base_optimization_problem
        first_outcome = optimize_problem(optimization_problem, settings)
        outcome = first_outcome
        charge_visit_contract: dict[str, Any] = {}
        if not first_outcome.plan.unassigned_task_ids:
            enriched_problem, charge_visit_contract = (
                prepare_charge_visit_optimization_problem(
                    optimization_problem,
                    first_outcome.plan,
                )
            )
            if int(charge_visit_contract.get("explicit_charge_task_count", 0)) > 0:
                second_outcome = optimize_problem(enriched_problem, settings)
                second_outcome, second_pass_fallback_used = (
                    validate_or_fallback_charge_visit_second_pass(
                        enriched_problem,
                        settings,
                        second_outcome,
                        charge_visit_contract,
                    )
                )
                second_outcome.execution["charge_visit_two_pass"] = {
                    "enabled": True,
                    "first_pass_provider": first_outcome.execution.get(
                        "used_provider"
                    ),
                    "second_pass_provider": second_outcome.execution.get(
                        "used_provider"
                    ),
                    "explicit_charge_task_count": int(
                        charge_visit_contract.get("explicit_charge_task_count", 0)
                    ),
                    "explicit_charge_task_ids": list(
                        charge_visit_contract.get("explicit_charge_task_ids", [])
                    ),
                    "robot_binding_validation": "PASS",
                    "contract_fallback_used": second_pass_fallback_used,
                }
                second_outcome.plan.metadata = {
                    **second_outcome.plan.metadata,
                    "charge_visit_optimization_contract": charge_visit_contract,
                    "optimizer_execution": second_outcome.execution,
                }
                optimization_problem = enriched_problem
                outcome = second_outcome
        outcome, low_battery_charge_retention = _ensure_low_battery_charge_retention(
            outcome=outcome,
            recovery_problem=optimization_problem,
            settings=settings,
            charge_visit_contract=charge_visit_contract,
        )
        if "low_battery_charge_retention" not in outcome.execution:
            outcome.execution["low_battery_charge_retention"] = (
                low_battery_charge_retention
            )
            outcome.plan.metadata = {
                **outcome.plan.metadata,
                "optimizer_execution": outcome.execution,
                "low_battery_charge_retention": low_battery_charge_retention,
            }
        plan = outcome.plan
        interpretation = CommandInterpretation.model_validate(
            state["interpretation"]
        )
        completed_work_ids = [
            str(row.get("work_id"))
            for row in state.get("snapshot", {}).get("sql", {}).get("works", [])
            if str(row.get("status") or "").upper() == "COMPLETED"
        ]
        ready_ids, waiting_ids = calculate_ready_task_ids(
            plan.scheduled_tasks,
            interpretation.task_dependencies,
            completed_work_ids=completed_work_ids,
            now_step=0,
        )
        blocked_work_ids = {
            canonical_work_id(str(row.get("work_id")))
            for row in state.get("snapshot", {}).get("sql", {}).get("works", [])
            if str(row.get("status") or "").upper() in {"BLOCKED", "FAILED"}
        }
        blocked_ids = sorted(
            task.task_id
            for task in plan.scheduled_tasks
            if canonical_work_id(str(task.work_id or task.task_id))
            in blocked_work_ids
        )
        ready_set = set(ready_ids) - set(blocked_ids)
        waiting_set = (set(waiting_ids) | (set(ready_ids) - ready_set)) - set(
            blocked_ids
        )
        atomic_by_id = {
            str(row.get("task_id")): row
            for row in state.get("required_tasks", [])
            if row.get("task_id")
        }
        for task in plan.scheduled_tasks:
            if task.task_id in blocked_ids:
                task.schedule_status = "BLOCKED"
            elif task.task_id in ready_set:
                task.schedule_status = "READY"
            elif atomic_by_id.get(task.task_id, {}).get("predecessors"):
                task.schedule_status = "WAITING_FOR_PREDECESSOR"
            elif task.task_id in waiting_set:
                task.schedule_status = "SCHEDULED"
        node_name = "local_optimize" if outcome.backend == "local" else "cuopt_optimize"
        optimization_evidence = [
            row.model_dump(mode="json") for row in outcome.optimization_evidence
        ]
        objective_breakdown = (
            outcome.objective_breakdown.model_dump(mode="json")
            if outcome.objective_breakdown is not None
            else {}
        )
        candidate_count = sum(
            int(row.get("candidate_count", 0)) for row in optimization_evidence
        )
        return {
            "optimization_problem": optimization_problem,
            "cuopt_plan": plan.model_dump(mode="json"),
            "optimization_evidence": optimization_evidence,
            "objective_breakdown": objective_breakdown,
            "optimizer_execution": outcome.execution,
            "ready_task_ids": sorted(ready_set),
            "waiting_task_ids": sorted(waiting_set),
            "blocked_task_ids": blocked_ids,
            "final_status": (
                "OPTIMIZATION_READY"
                if not plan.unassigned_task_ids
                else "OPTIMIZATION_PARTIAL"
            ),
            "warnings": outcome.warnings,
            "trace": (
                trace(
                    node_name,
                    backend=outcome.backend,
                    success=not plan.unassigned_task_ids,
                    task_count=len(plan.scheduled_tasks),
                    robot_count=len({task.robot_id for task in plan.scheduled_tasks}),
                    objective_value=plan.objective_value,
                    unassigned_task_ids=plan.unassigned_task_ids,
                    **{
                        key: value
                        for key, value in plan.metadata.items()
                        if key in {
                            "total_distance",
                            "makespan_time_steps",
                            "tardiness_time_steps",
                            "active_robot_count",
                        }
                    },
                )
                + trace(
                    "optimization_candidates_evaluated",
                    backend=outcome.backend,
                    task_count=len(optimization_evidence),
                    candidate_count=candidate_count,
                    evidence_available=bool(optimization_evidence),
                )
                + trace(
                    "objective_breakdown_created",
                    backend=outcome.backend,
                    evidence_available=bool(objective_breakdown),
                    objective_value=plan.objective_value,
                    component_names=sorted(
                        key
                        for key in objective_breakdown
                        if key.endswith("_component")
                    ),
                )
                + trace(
                    "cuopt_charge_visits_optimized",
                    enabled=bool(charge_visit_contract.get("enabled", False)),
                    mode=charge_visit_contract.get("mode"),
                    explicit_charge_task_count=int(
                        charge_visit_contract.get("explicit_charge_task_count", 0)
                    ),
                    explicit_charge_task_ids=charge_visit_contract.get(
                        "explicit_charge_task_ids", []
                    ),
                    second_pass=bool(
                        outcome.execution.get("charge_visit_two_pass")
                    ),
                )
                + trace(
                    "select_ready_tasks",
                    ready_task_ids=sorted(ready_set),
                    waiting_task_ids=sorted(waiting_set),
                    blocked_task_ids=blocked_ids,
                )
            ),
        }
    except Exception as exc:
        return {
            "cuopt_plan": {},
            "optimization_evidence": [],
            "objective_breakdown": {},
            "optimizer_execution": {
                "requested_provider": str(settings.optimizer_backend).upper(),
                "used_provider": None,
                "fallback_used": False,
                "fallback_reason": str(exc),
                "attempts": [],
            },
            "collision_plan": {},
            "final_status": "OPTIMIZATION_FAILED",
            "errors": [f"최적화 실패: {exc}"],
            "trace": trace(
                "local_optimize"
                if settings.optimizer_backend == "local"
                else "optimizer_auto"
                if settings.optimizer_backend == "auto"
                else "cuopt_optimize",
                backend=settings.optimizer_backend,
                success=False,
                reason=str(exc),
            ),
        }


# 기존 코드에서 import할 수 있도록 이름을 유지합니다.
cuopt_node = optimizer_node


def _routing_task_completion_steps(plan: CollisionFreePlan) -> dict[str, int]:
    """Return per-task final routing steps without changing route evidence."""

    values = plan.metadata.get("task_completion_steps", {})
    completion_steps = (
        {str(task_id): int(step) for task_id, step in values.items()}
        if isinstance(values, dict)
        else {}
    )
    # External routing backends may not return task boundaries. A one-task
    # route still has an unambiguous completion waypoint.
    for route in plan.routes:
        if len(route.task_ids) == 1 and route.waypoints:
            completion_steps.setdefault(
                route.task_ids[0], route.waypoints[-1].time_step
            )
    return completion_steps


def _reconcile_routing_schedule(
    optimizer_plan: CuOptPlan,
    collision_plan: CollisionFreePlan,
    optimization_problem: dict[str, Any],
) -> tuple[CuOptPlan, dict[str, Any]]:
    """Promote final routing arrival steps into the operational schedule."""

    completion_steps = _routing_task_completion_steps(collision_plan)
    route_start_values = collision_plan.metadata.get("task_start_steps", {})
    route_start_steps = (
        {str(task_id): int(step) for task_id, step in route_start_values.items()}
        if isinstance(route_start_values, dict)
        else {}
    )
    reference_time = optimization_problem.get("reference_time")
    optimizer_start_steps = {
        task.task_id: task.start_time_step
        for task in optimizer_plan.scheduled_tasks
    }
    optimizer_end_steps = {
        task.task_id: task.end_time_step
        for task in optimizer_plan.scheduled_tasks
    }
    reconciled_tasks = []
    updated_task_ids: list[str] = []
    for task in optimizer_plan.scheduled_tasks:
        route_start_step = route_start_steps.get(task.task_id)
        route_end_step = completion_steps.get(task.task_id)
        if route_start_step is None and route_end_step is None:
            reconciled_tasks.append(task)
            continue
        final_start_step, final_end_step = reconcile_task_time_window(
            task,
            route_start_step=route_start_step,
            route_end_step=route_end_step,
        )
        updates: dict[str, Any] = {
            "start_time_step": final_start_step,
            "end_time_step": final_end_step,
        }
        # Keep the optimizer payload stable when it did not originally carry
        # absolute timestamps.  daily_schedule derives those from the shared
        # planning reference and the reconciled end step.
        if reference_time is not None and task.planned_end_at is not None:
            updates["planned_end_at"] = planned_at(
                reference_time,
                final_end_step,
                collision_plan.time_step_seconds,
            )
        if reference_time is not None and task.planned_start_at is not None:
            updates["planned_start_at"] = planned_at(
                reference_time,
                final_start_step,
                collision_plan.time_step_seconds,
            )
        reconciled_tasks.append(task.model_copy(update=updates))
        if (
            final_start_step != task.start_time_step
            or final_end_step != task.end_time_step
        ):
            updated_task_ids.append(task.task_id)

    metadata = dict(optimizer_plan.metadata)
    metadata["optimizer_estimated_start_time_steps"] = optimizer_start_steps
    metadata["optimizer_estimated_end_time_steps"] = optimizer_end_steps
    metadata["routing_final_start_time_steps"] = route_start_steps
    metadata["routing_final_end_time_steps"] = completion_steps
    task_models = {
        task.task_id: task
        for task in (
            AtomicTask.model_validate(row)
            for row in optimization_problem.get("tasks", [])
        )
    }
    if reference_time is not None:
        metadata["tardiness_time_steps"] = sum(
            task_tardiness_steps(
                deadline=task_models[scheduled_task.task_id].deadline,
                reference_time=reference_time,
                task_end_time_step=scheduled_task.end_time_step,
                time_step_seconds=collision_plan.time_step_seconds,
            )
            for scheduled_task in reconciled_tasks
            if scheduled_task.task_id in task_models
        )
    return (
        optimizer_plan.model_copy(
            update={"scheduled_tasks": reconciled_tasks, "metadata": metadata}
        ),
        {
            "optimizer_start_time_steps": optimizer_start_steps,
            "optimizer_end_time_steps": optimizer_end_steps,
            "routing_start_time_steps": route_start_steps,
            "routing_end_time_steps": completion_steps,
            "updated_task_ids": updated_task_ids,
        },
    )


def validate_execution_task_dependencies(
    plan: CuOptPlan,
    *,
    time_step_seconds: int,
) -> dict[str, Any]:
    """Validate generated CHARGE/PICK/DROP task dependencies after routing.

    Planner-level work dependencies are validated before optimization.  Auto
    charging dependencies do not exist until the optimizer inserts CHARGE, so
    they must be checked again against routing-reconciled start/end steps.
    """

    dependencies = list(
        (plan.metadata or {}).get("execution_task_dependencies", []) or []
    )
    tasks = {row.task_id: row for row in plan.scheduled_tasks}
    graph: dict[str, set[str]] = {task_id: set() for task_id in tasks}
    indegree: dict[str, int] = {task_id: 0 for task_id in tasks}
    violations: list[dict[str, Any]] = []

    def add_violation(
        code: str,
        message: str,
        task_ids: list[str],
    ) -> None:
        violations.append(
            {
                "code": code,
                "message": message,
                "task_ids": sorted({value for value in task_ids if value}),
            }
        )

    for raw in dependencies:
        predecessor_id = str(raw.get("predecessor_task_id") or "")
        successor_id = str(raw.get("successor_task_id") or "")
        missing = [
            task_id
            for task_id in (predecessor_id, successor_id)
            if task_id not in tasks
        ]
        if missing:
            add_violation(
                "EXECUTION_DEPENDENCY_TASK_MISSING",
                (
                    "실행 작업 의존성이 존재하지 않는 작업을 참조합니다: "
                    f"{missing}"
                ),
                [predecessor_id, successor_id],
            )
            continue
        if successor_id not in graph[predecessor_id]:
            graph[predecessor_id].add(successor_id)
            indegree[successor_id] += 1
        lag_seconds = max(0, int(raw.get("lag_seconds") or 0))
        lag_steps = math.ceil(lag_seconds / max(1, time_step_seconds))
        predecessor = tasks[predecessor_id]
        successor = tasks[successor_id]
        required_start = int(predecessor.end_time_step) + lag_steps
        actual_start = int(successor.start_time_step)
        if actual_start < required_start:
            add_violation(
                "EXECUTION_DEPENDENCY_ORDER_VIOLATION",
                (
                    f"작업 {successor_id}은 선행 작업 {predecessor_id} 종료 "
                    f"step {predecessor.end_time_step} 이후 시작해야 하지만 "
                    f"step {successor.start_time_step}에 시작합니다."
                ),
                [predecessor_id, successor_id],
            )

    queue = sorted(task_id for task_id, value in indegree.items() if value == 0)
    order: list[str] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for successor_id in sorted(graph[current]):
            indegree[successor_id] -= 1
            if indegree[successor_id] == 0:
                queue.append(successor_id)
                queue.sort()
    dependency_nodes = {
        str(raw.get("predecessor_task_id") or "")
        for raw in dependencies
    } | {
        str(raw.get("successor_task_id") or "")
        for raw in dependencies
    }
    dependency_nodes.discard("")
    ordered_dependency_nodes = [task_id for task_id in order if task_id in dependency_nodes]
    if dependencies and len(ordered_dependency_nodes) < len(dependency_nodes & tasks.keys()):
        cycle_nodes = sorted((dependency_nodes & tasks.keys()) - set(ordered_dependency_nodes))
        add_violation(
            "EXECUTION_DEPENDENCY_CYCLE",
            f"실행 작업 의존성에 순환이 있습니다: {cycle_nodes}",
            cycle_nodes,
        )

    return {
        "valid": not violations,
        "dependency_count": len(dependencies),
        "dependency_order": ordered_dependency_nodes,
        "violations": violations,
        "errors": [row["message"] for row in violations],
    }


def _merge_execution_schedule_validation(
    state: PlanningState,
    plan: CuOptPlan,
    *,
    time_step_seconds: int,
) -> dict[str, Any]:
    base = deepcopy(state.get("schedule_validation", {}))
    business_count = int(base.get("business_dependency_count", base.get("dependency_count", 0)) or 0)
    business_order = list(base.get("business_dependency_order", base.get("dependency_order", [])) or [])
    result = validate_execution_task_dependencies(
        plan,
        time_step_seconds=time_step_seconds,
    )
    base_errors = list(base.get("errors", []) or [])
    base.update(
        {
            "valid": bool(base.get("valid", True)) and result["valid"],
            "errors": [*base_errors, *result["errors"]],
            "business_dependency_count": business_count,
            "business_dependency_order": business_order,
            "execution_dependency_count": result["dependency_count"],
            "execution_dependency_order": result["dependency_order"],
            "execution_dependency_violations": result["violations"],
            "dependency_count": business_count + result["dependency_count"],
            "dependency_order": (
                result["dependency_order"]
                if result["dependency_count"]
                else business_order
            ),
            "validated_after_routing": True,
        }
    )
    return base


def collision_avoidance_node(state: PlanningState) -> dict[str, Any]:
    settings = get_settings()
    if not state.get("cuopt_plan"):
        return {
            "collision_plan": {},
            "routing_evidence": {},
            "reservation_evidence": {},
            "distance_comparison": {},
            "final_status": "ROUTE_SKIPPED",
            "trace": trace(
                "build_routes",
                routing_backend=settings.routing_backend,
                success=False,
                reason="최적화 계획 없음",
            ),
        }
    try:
        optimizer_plan = CuOptPlan.model_validate(state["cuopt_plan"])
        problem = state["optimization_problem"]
        optimizer_plan, idle_energy_planning = augment_plan_with_opportunity_charging(
            problem,
            optimizer_plan,
        )
        optimizer_plan, resource_reservation_plan = schedule_shared_resources(
            problem,
            optimizer_plan,
        )
        if not resource_reservation_plan.get("valid", False):
            raise RuntimeError(
                "SHARED_RESOURCE_SCHEDULING_FAILED: "
                + "; ".join(resource_reservation_plan.get("errors", [])[:3])
            )
        resource_adjustment_history = list(
            resource_reservation_plan.get("adjustments", []) or []
        )
        plan = build_collision_plan(problem, optimizer_plan, settings)
        operational_plan, schedule_reconciliation = _reconcile_routing_schedule(
            optimizer_plan,
            plan,
            problem,
        )

        # Routing waits/detours can move the real service time, and final route
        # distance can change charge duration. Reconcile resource windows,
        # energy, and MAPF in one bounded fixed-point loop. A* therefore receives
        # an executable shared-resource schedule instead of repairing one.
        energy_reconciliation: dict[str, Any] = {}
        for _reconciliation_attempt in range(4):
            before_resource_times = {
                task.task_id: (task.start_time_step, task.end_time_step)
                for task in operational_plan.scheduled_tasks
            }
            resource_adjusted_plan, resource_reservation_plan = (
                schedule_shared_resources(problem, operational_plan)
            )
            if not resource_reservation_plan.get("valid", False):
                raise RuntimeError(
                    "SHARED_RESOURCE_SCHEDULING_FAILED_AFTER_ROUTING: "
                    + "; ".join(resource_reservation_plan.get("errors", [])[:3])
                )
            resource_adjustment_history.extend(
                resource_reservation_plan.get("adjustments", []) or []
            )
            after_resource_times = {
                task.task_id: (task.start_time_step, task.end_time_step)
                for task in resource_adjusted_plan.scheduled_tasks
            }
            resource_requires_reroute = (
                before_resource_times != after_resource_times
            )
            energy_adjusted_plan, energy_reconciliation = reconcile_plan_energy(
                resource_adjusted_plan,
                plan,
                problem,
            )
            operational_plan = energy_adjusted_plan
            requires_reroute = bool(
                resource_requires_reroute
                or energy_reconciliation.get("requires_reroute")
            )
            if not requires_reroute:
                break
            plan = build_collision_plan(problem, operational_plan, settings)
            operational_plan, schedule_reconciliation = _reconcile_routing_schedule(
                operational_plan,
                plan,
                problem,
            )
        else:
            raise RuntimeError(
                "RESOURCE_ROUTE_ENERGY_RECONCILIATION_DID_NOT_CONVERGE"
            )

        unique_resource_adjustments: list[dict[str, Any]] = []
        seen_resource_adjustments: set[tuple[Any, ...]] = set()
        for row in resource_adjustment_history:
            signature = (
                row.get("task_id"),
                row.get("old_start_time_step"),
                row.get("new_start_time_step"),
                row.get("old_end_time_step"),
                row.get("new_end_time_step"),
                row.get("reason"),
                row.get("node_id"),
            )
            if signature in seen_resource_adjustments:
                continue
            seen_resource_adjustments.add(signature)
            unique_resource_adjustments.append(row)
        resource_reservation_plan["adjustments"] = unique_resource_adjustments
        resource_reservation_plan["adjustment_count"] = len(
            unique_resource_adjustments
        )
        resource_reservation_plan = finalize_idle_resource_reservations(
            problem,
            plan,
            resource_reservation_plan,
        )
        resource_metadata = dict(operational_plan.metadata)
        resource_metadata["shared_resource_scheduling"] = resource_reservation_plan
        resource_metadata["resource_reservations"] = resource_reservation_plan.get(
            "reservations", []
        )
        operational_plan = operational_plan.model_copy(
            update={"metadata": resource_metadata}
        )
        collision_metadata = dict(plan.metadata)
        collision_metadata["shared_resource_scheduling"] = resource_reservation_plan
        plan = plan.model_copy(update={"metadata": collision_metadata})
        operational_objective = calculate_operational_objective(
            problem,
            operational_plan,
            plan,
            resource_reservation_plan,
        )
        objective_metadata = dict(operational_plan.metadata)
        objective_metadata["operational_objective"] = operational_objective
        operational_plan = operational_plan.model_copy(
            update={
                "metadata": objective_metadata,
                "objective_value": float(operational_objective["total"]),
            }
        )

        schedule_validation = _merge_execution_schedule_validation(
            state,
            operational_plan,
            time_step_seconds=int(
                problem.get("time_step_seconds") or settings.time_step_seconds
            ),
        )
        if not resource_reservation_plan.get("valid", False):
            resource_errors = list(resource_reservation_plan.get("errors", []) or [])
            schedule_validation = dict(schedule_validation)
            schedule_validation["valid"] = False
            schedule_validation["errors"] = [
                *list(schedule_validation.get("errors", []) or []),
                *resource_errors,
            ]
            schedule_validation["resource_capacity_valid"] = False
        else:
            schedule_validation = dict(schedule_validation)
            schedule_validation["resource_capacity_valid"] = True
            schedule_validation["resource_reservation_count"] = int(
                resource_reservation_plan.get("reservation_count", 0)
            )
        unsafe_energy_robots = list(
            energy_reconciliation.get("unsafe_robot_ids", []) or []
        )
        if unsafe_energy_robots:
            energy_errors = [
                "최종 라우팅 거리 기준으로 최소 배터리를 보장할 수 없습니다: "
                + ", ".join(sorted(unsafe_energy_robots))
            ]
            schedule_validation = dict(schedule_validation)
            schedule_validation["valid"] = False
            schedule_validation["errors"] = [
                *list(schedule_validation.get("errors", []) or []),
                *energy_errors,
            ]
            schedule_validation["route_energy_unsafe_robot_ids"] = sorted(
                unsafe_energy_robots
            )
        warning = plan.metadata.get("fallback_warning")
        routing_evidence, reservation_evidence, distance_comparison = (
            build_route_evidence(
                state["optimization_problem"],
                optimizer_plan,
                plan,
            )
        )
        routing_payload = routing_evidence.model_dump(mode="json")
        reservation_payload = reservation_evidence.model_dump(mode="json")
        distance_payload = distance_comparison.model_dump(mode="json")
        plan_version = (
            state.get("current_plan_version")
            or state.get("plan_version")
            or str(uuid4())
        )
        adapter_plan = {
            "warehouse_id": state["command"]["warehouse_id"],
            "required_tasks": state.get("required_tasks", []),
            "inventory_operations": state.get("inventory_operations", []),
            "cuopt_plan": operational_plan.model_dump(mode="json"),
            "collision_plan": plan.model_dump(mode="json"),
            "charger_node_ids": [
                int(row["node_id"])
                for row in state.get("optimization_problem", {}).get("nodes", [])
                if row.get("active", True)
                and str(row.get("node_type") or "").upper() == "CHARGER"
            ],
        }
        adapter = RobotAdapter(
            time_step_seconds=int(
                state.get("optimization_problem", {}).get("time_step_seconds")
                or settings.time_step_seconds
            )
        )
        command_batches, adapter_validation = adapter.adapt(
            plan_version,
            adapter_plan,
        )
        serialized_batches = [
            batch.model_dump(mode="json") for batch in command_batches
        ]
        update: dict[str, Any] = {
            "plan_version": plan_version,
            "route_failure": {},
            "cuopt_plan": operational_plan.model_dump(mode="json"),
            "collision_plan": plan.model_dump(mode="json"),
            "robot_command_batches": serialized_batches,
            "adapter_validation": adapter_validation,
            "dispatched_robot_count": 0,
            "dispatched_command_count": 0,
            "gateway_dispatched": False,
            "routing_evidence": routing_payload,
            "reservation_evidence": reservation_payload,
            "distance_comparison": distance_payload,
            "schedule_validation": schedule_validation,
            "route_energy_reconciliation": energy_reconciliation,
            "idle_energy_planning": idle_energy_planning,
            "resource_reservation_plan": resource_reservation_plan,
            "operational_objective": operational_objective,
            "final_status": (
                "ROUTES_READY"
                if schedule_validation.get("valid", False)
                else "ROUTE_VALIDATION_FAILED"
            ),
            "errors": list(schedule_validation.get("errors", [])),
            "warnings": list(
                dict.fromkeys(
                    ([warning] if warning else [])
                    + list(resource_reservation_plan.get("warnings", []) or [])
                )
            ),
            "trace": (
                trace(
                    "build_routes",
                    routing_backend=plan.metadata.get(
                        "routing_backend", settings.routing_backend
                    ),
                    engine=plan.engine,
                    success=True,
                    robot_count=len(plan.routes),
                    total_distance=plan.total_distance,
                )
                + trace(
                    "robot_adapter_preview",
                    success=adapter_validation["valid"],
                    plan_version=plan_version,
                    batch_count=len(serialized_batches),
                    command_count=sum(
                        batch["command_count"] for batch in serialized_batches
                    ),
                    gateway_dispatched=False,
                    errors=adapter_validation.get("errors", []),
                )
                + trace(
                    "route_evidence_created",
                    route_count=len(routing_payload.get("routes", [])),
                    route_segment_count=routing_payload.get(
                        "route_segment_count", 0
                    ),
                    complete=routing_payload.get("complete", False),
                    issue_count=len(routing_payload.get("issues", [])),
                )
                + trace(
                    "reservation_evidence_created",
                    vertex_reservation_count=reservation_payload.get(
                        "vertex_reservation_count", 0
                    ),
                    edge_reservation_count=reservation_payload.get(
                        "edge_reservation_count", 0
                    ),
                    wait_count=reservation_payload.get("wait_count", 0),
                    reroute_count=reservation_payload.get("reroute_count"),
                )
                + trace(
                    "distance_comparison_created",
                    optimizer_estimated_distance=distance_payload.get(
                        "optimizer_estimated_distance", 0.0
                    ),
                    routing_final_distance=distance_payload.get(
                        "routing_final_distance", 0.0
                    ),
                    difference=distance_payload.get("difference", 0.0),
                    robot_count=len(distance_payload.get("robot_differences", [])),
                )
                + trace(
                    "routing_schedule_reconciled",
                    optimizer_end_time_steps=schedule_reconciliation[
                        "optimizer_end_time_steps"
                    ],
                    routing_end_time_steps=schedule_reconciliation[
                        "routing_end_time_steps"
                    ],
                    updated_task_ids=schedule_reconciliation["updated_task_ids"],
                )
                + trace(
                    "route_energy_reconciled",
                    energy_source=energy_reconciliation.get("energy_source"),
                    requires_reroute=energy_reconciliation.get("requires_reroute", False),
                    unsafe_robot_ids=energy_reconciliation.get("unsafe_robot_ids", []),
                    robots=energy_reconciliation.get("robots", {}),
                )
                + trace(
                    "shared_resources_reserved",
                    valid=resource_reservation_plan.get("valid", False),
                    reservation_count=resource_reservation_plan.get(
                        "reservation_count", 0
                    ),
                    adjustment_count=resource_reservation_plan.get(
                        "adjustment_count", 0
                    ),
                    idle_reservation_count=resource_reservation_plan.get(
                        "idle_reservation_count", 0
                    ),
                    errors=resource_reservation_plan.get("errors", []),
                )
                + trace(
                    "operational_objective_calculated",
                    version=operational_objective.get("version"),
                    total=operational_objective.get("total"),
                    component_names=sorted(
                        operational_objective.get("components", {})
                    ),
                    hard_constraint_policy=operational_objective.get(
                        "hard_constraint_policy"
                    ),
                )
                + trace(
                    "execution_dependencies_validated",
                    valid=schedule_validation.get("valid", False),
                    dependency_count=schedule_validation.get(
                        "execution_dependency_count", 0
                    ),
                    dependency_order=schedule_validation.get(
                        "execution_dependency_order", []
                    ),
                    violation_count=len(
                        schedule_validation.get(
                            "execution_dependency_violations", []
                        )
                    ),
                )
            ),
        }
        if not state.get("current_plan_version"):
            update.update(
                {
                    "original_plan_version": state.get(
                        "original_plan_version"
                    ) or plan_version,
                    "current_plan_version": plan_version,
                }
            )
        return update
    except Exception as exc:
        if isinstance(exc, IndexError):
            error_code = "INTERNAL_ROUTING_STATE_ERROR"
            reason = (
                "재계획 경로 상태가 비어 내부 인덱스 접근을 차단했습니다. "
                "기존 성공 후보는 보존되며 현재 후보는 안전하게 거부됩니다."
            )
        else:
            error_code = "ROUTE_FAILED"
            reason = str(exc)
        route_failure = classify_mapf_failure(
            reason,
            error_code=error_code,
            routing_backend=str(settings.routing_backend),
            problem=state.get("optimization_problem", {}),
            cuopt_plan=state.get("cuopt_plan", {}),
        )
        message = f"충돌 방지 경로 실패 [{error_code}]: {reason}"
        return {
            "collision_plan": {},
            "routing_evidence": {},
            "reservation_evidence": {},
            "distance_comparison": {},
            "resource_reservation_plan": {},
            "route_failure": route_failure,
            "final_status": "ROUTE_FAILED",
            # Retryable MAPF failures are represented by structured verification
            # evidence, not accumulated in the additive pipeline error list.
            # Otherwise a successful LOCAL_REPLAN would still fail because the
            # first attempt's ROUTE_FAILED string remains in LangGraph state.
            "errors": [] if route_failure.get("retryable") else [message],
            "trace": trace(
                "build_routes",
                routing_backend=settings.routing_backend,
                success=False,
                error_code=error_code,
                reason=reason,
                route_failure_code=route_failure.get("code"),
                retryable=route_failure.get("retryable"),
                recommended_scope=route_failure.get("recommended_scope"),
                affected_robot_ids=route_failure.get("affected_robot_ids", []),
                affected_task_ids=route_failure.get("affected_task_ids", []),
            ),
        }


def validate_plan_node(state: PlanningState) -> dict[str, Any]:
    if not state.get("collision_plan") or not state.get("cuopt_plan"):
        result = SimulationResult(
            success=False,
            valid=False,
            status="FAILED",
            issues=[
                SimulationIssue(
                    code="PLAN_COMPONENT_MISSING",
                    message="최적화 계획 또는 충돌 방지 경로가 없습니다.",
                )
            ],
            errors=["최적화 계획 또는 충돌 방지 경로가 없습니다."],
        )
    else:
        result = simulate_plan(
            CollisionFreePlan.model_validate(state["collision_plan"]),
            CuOptPlan.model_validate(state["cuopt_plan"]),
            state["optimization_problem"],
            include_timeline=False,
        )
    reservation_evidence = deepcopy(state.get("reservation_evidence", {}))
    if reservation_evidence:
        reservation_evidence["final_conflict_count"] = result.conflict_count
    return {
        "plan_validation": result.model_dump(mode="json"),
        "reservation_evidence": reservation_evidence,
        "final_status": "PLAN_READY" if result.valid else "PLAN_VALIDATION_FAILED",
        "warnings": result.warnings,
        "trace": trace(
            "validate_plan",
            success=result.valid,
            total_distance=result.total_distance,
            makespan=result.makespan,
            conflict_count=result.conflict_count,
            reason=result.errors[:3],
        ),
    }


def simulation_node(state: PlanningState) -> dict[str, Any]:
    plan_version = state.get("plan_version") or str(uuid4())
    if not state.get("collision_plan"):
        result = SimulationResult(
            success=False,
            valid=False,
            status="FAILED",
            issues=[
                SimulationIssue(
                    code="NO_COLLISION_PLAN",
                    message=state.get("final_status", "경로 계획 없음"),
                )
            ],
            errors=[state.get("final_status", "경로 계획 없음")],
        )
    else:
        result = simulate_plan(
            CollisionFreePlan.model_validate(state["collision_plan"]),
            CuOptPlan.model_validate(state["cuopt_plan"]),
            state["optimization_problem"],
            include_timeline=True,
        )
    session: dict[str, Any] | None = None
    execution_mode = state.get("interpretation", {}).get("execution_mode")
    if result.success and execution_mode == "SIMULATE_ONLY":
        try:
            replay_state = dict(state)
            replay_state["plan_version"] = plan_version
            session = replay_simulation_session(
                replay_state,
                result,
                get_services().redis,
            )
            result_payload = result.model_dump(mode="json")
            result_payload.update(
                {
                    "simulation_id": session["simulation_id"],
                    "checkpoint": session["checkpoint"],
                    "replayed_event_count": session["event_count"],
                    "session_reset_for_replan": bool(
                        session.get("session_reset_for_replan")
                    ),
                }
            )
        except Exception as exc:
            result.success = False
            result.valid = False
            result.status = "FAILED"
            result.errors.append(f"simulation session 재생 실패: {exc}")
            result_payload = result.model_dump(mode="json")
    else:
        result_payload = result.model_dump(mode="json")

    update: dict[str, Any] = {
        "plan_version": plan_version,
        "simulation": result_payload,
        "final_status": (
            "SIMULATION_SUCCESS" if result.success else "SIMULATION_FAILED"
        ),
        "trace": trace(
            "simulation",
            success=result.success,
            total_distance=result.total_distance,
            makespan=result.makespan,
            conflict_count=result.conflict_count,
            reason=result.errors[:3],
        ),
    }
    if session:
        reservations = simulation_reservation_summaries(
            warehouse_id=int(
                state.get("command", {}).get("warehouse_id")
                or state.get("optimization_problem", {}).get("warehouse_id")
                or 0
            ),
            simulation_id=session["simulation_id"],
            plan_version=plan_version,
            item_results=(
                state.get("inventory_timeline_validation", {}).get("item_results")
                or state.get("inventory_feasibility", {}).get("item_results", [])
            ),
        )
        update.update(
            {
                "simulation_id": session["simulation_id"],
                "simulation_base_state": session.get(
                    "base_state",
                    session["current_state"],
                ),
                "simulation_current_state": session["current_state"],
                "simulation_checkpoint": session["checkpoint"],
                "inventory_reservations": [
                    row.model_dump(mode="json") for row in reservations
                ],
                "trace": update["trace"]
                + trace(
                    "inventory_reservations",
                    scope="SIMULATION",
                    simulation_id=session["simulation_id"],
                    reservation_count=len(reservations),
                ),
            }
        )
    return update


def validate_simulation_node(state: PlanningState) -> dict[str, Any]:
    result = SimulationResult.model_validate(state["simulation"])
    reservation_evidence = deepcopy(state.get("reservation_evidence", {}))
    if reservation_evidence:
        reservation_evidence["final_conflict_count"] = result.conflict_count
    return {
        "reservation_evidence": reservation_evidence,
        "final_status": (
            "SIMULATION_SUCCESS" if result.valid else "SIMULATION_INVALID"
        ),
        "warnings": result.warnings,
        "trace": trace(
            "validate_simulation",
            success=result.valid,
            total_distance=result.total_distance,
            makespan=result.makespan,
            tardiness=result.tardiness,
            conflict_count=result.conflict_count,
            reason=result.errors[:3],
        ),
    }


HARD_VERIFICATION_FAILURE_CODES = {
    "CLARIFICATION_REQUIRED",
    "DETERMINISTIC_RESULT_MISSING",
    "INSUFFICIENT_INVENTORY",
    "NO_COLLISION_PLAN",
    "PIPELINE_ERROR",
    "PLAN_COMPONENT_MISSING",
    "SNAPSHOT_VALIDATION_ERROR",
    "ROBOT_STATE_OVERRIDE_NOT_APPLIED",
    # P16.3.3: battery/charger failures are normally recoverable by changing
    # the charger, charge timing, or local robot schedule. They must remain
    # blocking evidence, but should request LOCAL_REPLAN instead of terminating
    # the whole plan as an unrecoverable verification failure.
    "TARGET_NODE_NOT_APPLIED",
    "EXECUTION_DEPENDENCY_TASK_MISSING",
    "EXECUTION_DEPENDENCY_ORDER_VIOLATION",
    "EXECUTION_DEPENDENCY_CYCLE",
    "MAPF_CONFIGURATION_FAILURE",
    "MAPF_BACKEND_UNAVAILABLE",
    "MAPF_UNCLASSIFIED_FAILURE",
}
GLOBAL_REPLAN_CODES = {
    "DISCONNECTED_OR_CLOSED_EDGE",
    "INVALID_OR_CLOSED_NODE",
    "MAPF_TOPOLOGY_FAILURE",
    "MAPF_GLOBAL_CONFLICT",
}


def _verification_evidence_row(
    evidence: list[dict[str, Any]],
    *,
    source: str,
    severity: str,
    code: str,
    message: str,
    robot_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    node_ids: list[int] | None = None,
    time_steps: list[int] | None = None,
) -> None:
    robot_ids = sorted({str(value) for value in robot_ids or [] if value})
    task_ids = sorted({str(value) for value in task_ids or [] if value})
    node_ids = sorted({int(value) for value in node_ids or []})
    time_steps = sorted({int(value) for value in time_steps or []})
    signature = (
        source,
        severity,
        code,
        message,
        tuple(robot_ids),
        tuple(task_ids),
        tuple(node_ids),
        tuple(time_steps),
    )
    if any(row.get("signature") == signature for row in evidence):
        return
    evidence.append(
        {
            "evidence_id": f"verification:{len(evidence) + 1:03d}",
            "source": source,
            "severity": severity,
            "code": code,
            "message": message,
            "robot_ids": robot_ids,
            "task_ids": task_ids,
            "node_ids": node_ids,
            "time_steps": time_steps,
            "signature": signature,
        }
    )


def build_verification_evidence(state: PlanningState) -> list[dict[str, Any]]:
    """이미 계산된 검증 결과만 compact evidence로 변환합니다."""

    evidence: list[dict[str, Any]] = []
    supervisor = state.get("supervisor_decision", {})
    if supervisor.get("requires_clarification"):
        _verification_evidence_row(
            evidence,
            source="SUPERVISOR",
            severity="BLOCKING",
            code="CLARIFICATION_REQUIRED",
            message=str(
                supervisor.get("clarification_reason")
                or "사용자에게 추가 정보 확인이 필요합니다."
            ),
        )

    snapshot_validation = state.get("validation", {})
    for message in snapshot_validation.get("errors", []):
        _verification_evidence_row(
            evidence,
            source="SNAPSHOT_VALIDATION",
            severity="BLOCKING",
            code="SNAPSHOT_VALIDATION_ERROR",
            message=str(message),
        )
    for message in snapshot_validation.get("warnings", []):
        _verification_evidence_row(
            evidence,
            source="SNAPSHOT_VALIDATION",
            severity="WARNING",
            code="SNAPSHOT_VALIDATION_WARNING",
            message=str(message),
        )

    route_failure = state.get("route_failure") or {}
    if route_failure:
        _verification_evidence_row(
            evidence,
            source="MAPF_ROUTING",
            severity="BLOCKING",
            code=str(route_failure.get("code") or "MAPF_UNCLASSIFIED_FAILURE"),
            message=str(route_failure.get("reason") or "MAPF 경로 생성에 실패했습니다."),
            robot_ids=[str(value) for value in route_failure.get("affected_robot_ids", [])],
            task_ids=[str(value) for value in route_failure.get("affected_task_ids", [])],
            node_ids=[int(value) for value in route_failure.get("affected_node_ids", [])],
        )

    deterministic_result = state.get("simulation") or state.get("plan_validation")
    if deterministic_result:
        issue_messages: set[str] = set()
        for raw_issue in deterministic_result.get("issues", []):
            issue = SimulationIssue.model_validate(raw_issue)
            if route_failure and issue.code in {"PLAN_COMPONENT_MISSING", "NO_COLLISION_PLAN"}:
                continue
            issue_messages.add(issue.message)
            _verification_evidence_row(
                evidence,
                source="DETERMINISTIC_VALIDATION",
                severity="BLOCKING",
                code=issue.code,
                message=issue.message,
                robot_ids=issue.robot_ids,
                task_ids=issue.task_ids,
                node_ids=issue.node_ids,
                time_steps=issue.time_steps,
            )
        for message in deterministic_result.get("errors", []):
            if route_failure:
                continue
            if str(message) in issue_messages:
                continue
            _verification_evidence_row(
                evidence,
                source="DETERMINISTIC_VALIDATION",
                severity="BLOCKING",
                code="DETERMINISTIC_ERROR",
                message=str(message),
            )
        for message in deterministic_result.get("warnings", []):
            _verification_evidence_row(
                evidence,
                source="DETERMINISTIC_VALIDATION",
                severity="WARNING",
                code="DETERMINISTIC_WARNING",
                message=str(message),
            )
    elif not route_failure:
        _verification_evidence_row(
            evidence,
            source="DETERMINISTIC_VALIDATION",
            severity="BLOCKING",
            code="DETERMINISTIC_RESULT_MISSING",
            message="결정론적 계획 또는 시뮬레이션 검증 결과가 없습니다.",
        )

    # Inventory projection is the authoritative source for command-level
    # shortages.  Tasks with zero feasible quantity are intentionally omitted
    # before optimization, so relying only on simulation issues would hide the
    # blocked A/B-like operations from final verification.
    inventory_validation = (
        state.get("inventory_timeline_validation")
        or state.get("inventory_feasibility")
        or {}
    )
    for row in inventory_validation.get("item_results", []):
        if str(row.get("operation_type") or "").upper() != "OUTBOUND":
            continue
        shortage = int(row.get("shortage_quantity_boxes") or 0)
        if shortage <= 0:
            continue
        operation_ref = str(
            row.get("work_id") or row.get("operation_id") or ""
        )
        _verification_evidence_row(
            evidence,
            source="INVENTORY_PROJECTION",
            severity="BLOCKING",
            code="INSUFFICIENT_INVENTORY",
            message=(
                f"{row.get('item_id')} 재고가 요청 시점까지 "
                f"{shortage} BOX 부족합니다."
            ),
            task_ids=[operation_ref] if operation_ref else [],
        )

    # P16.5.9 shared-resource capacity is a hard feasibility gate. The
    # verifier consumes the reservation ledger produced before routing rather
    # than asking A* to repair overlapping service or charger slots.
    resource_plan_present = "resource_reservation_plan" in state
    resource_plan = state.get("resource_reservation_plan", {}) or {}
    if resource_plan:
        for message in resource_plan.get("errors", []) or []:
            _verification_evidence_row(
                evidence,
                source="SHARED_RESOURCE_SCHEDULER",
                severity="BLOCKING",
                code="SHARED_RESOURCE_CAPACITY_INVALID",
                message=str(message),
            )
        for message in resource_plan.get("warnings", []) or []:
            _verification_evidence_row(
                evidence,
                source="SHARED_RESOURCE_SCHEDULER",
                severity="WARNING",
                code="SHARED_RESOURCE_CAPACITY_WARNING",
                message=str(message),
            )
        reservations = list(resource_plan.get("reservations", []) or [])
        grouped_reservations: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for row in reservations:
            key = (
                str(row.get("resource_type") or "UNKNOWN"),
                int(row.get("node_id") or 0),
            )
            grouped_reservations.setdefault(key, []).append(row)
        for (resource_type, node_id), rows in grouped_reservations.items():
            events: list[tuple[int, int]] = []
            capacity = max(1, int(rows[0].get("capacity") or 1))
            for row in rows:
                start = int(row.get("start_time_step") or 0)
                end = int(row.get("end_time_step") or start)
                events.append((start, 1))
                events.append((end, -1))
            occupancy = 0
            maximum = 0
            # End events are processed before start events at the same step,
            # matching half-open reservation windows [start, end).
            for _step, delta in sorted(events, key=lambda item: (item[0], item[1])):
                occupancy += delta
                maximum = max(maximum, occupancy)
            if maximum > capacity:
                _verification_evidence_row(
                    evidence,
                    source="SHARED_RESOURCE_SCHEDULER",
                    severity="BLOCKING",
                    code="SHARED_RESOURCE_CAPACITY_EXCEEDED",
                    message=(
                        f"{resource_type} 노드 {node_id}의 최대 동시 점유 "
                        f"{maximum}이 용량 {capacity}를 초과했습니다."
                    ),
                    node_ids=[node_id],
                    task_ids=[
                        str(row.get("task_id"))
                        for row in rows
                        if row.get("task_id")
                    ],
                )
    elif resource_plan_present and state.get("cuopt_plan", {}).get("scheduled_tasks"):
        _verification_evidence_row(
            evidence,
            source="SHARED_RESOURCE_SCHEDULER",
            severity="BLOCKING",
            code="SHARED_RESOURCE_RESERVATION_MISSING",
            message="공유 작업 노드와 충전 슬롯 예약 결과가 없습니다.",
        )

    # Enforce command-level robot exclusions again during verification.
    # This is a safety net for parser or optimizer regressions: a plan must
    # never pass verification when it assigns a task to an explicitly
    # excluded robot.
    interpretation_raw = state.get("interpretation", {}) or {}
    excluded_robot_ids = {
        canonical_robot_id(value)
        for value in interpretation_raw.get("excluded_robot_ids", [])
        if value
    }
    if excluded_robot_ids:
        for task in state.get("cuopt_plan", {}).get("scheduled_tasks", []):
            robot_id = canonical_robot_id(task.get("robot_id"))
            if robot_id not in excluded_robot_ids:
                continue
            task_id = str(task.get("task_id") or "")
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="EXCLUDED_ROBOT_ASSIGNED",
                message=(
                    f"제외된 로봇 {robot_id}에 작업 {task_id}이 배정되었습니다."
                ),
                robot_ids=[robot_id],
                task_ids=[task_id] if task_id else [],
            )

    # P11 command-satisfaction checks: a valid route is not enough when the
    # user supplied an explicit battery assumption, minimum reserve policy, or
    # destination node. These checks prevent a false PASS on omitted features.
    problem = state.get("optimization_problem", {})
    plan_rows = state.get("cuopt_plan", {}).get("scheduled_tasks", [])
    requested_operations = list(interpretation_raw.get("inventory_operations") or [])
    if (
        str(interpretation_raw.get("command_kind") or "").upper() in {"PLAN", "EXECUTE"}
        and requested_operations
        and not plan_rows
    ):
        requested_ids = [
            str(row.get("operation_id") or row.get("work_id") or "")
            for row in requested_operations
            if isinstance(row, dict)
        ]
        _verification_evidence_row(
            evidence,
            source="DETERMINISTIC_VALIDATION",
            severity="BLOCKING",
            code="EMPTY_EXECUTION_PLAN",
            message="요청된 입출고 작업이 있지만 실행 작업과 로봇 명령이 생성되지 않았습니다.",
            task_ids=[value for value in requested_ids if value],
        )
    battery_overrides: dict[str, float] = {}
    for raw_event in interpretation_raw.get("hypothetical_events", []):
        event = (
            raw_event.model_dump(mode="json")
            if hasattr(raw_event, "model_dump")
            else dict(raw_event)
        )
        parameters = event.get("parameters") or {}
        battery_percent = parameters.get("battery_percent")
        if event.get("event_type") == "LOW_BATTERY" and battery_percent is not None:
            for target_id in event.get("target_ids", []):
                battery_overrides[canonical_robot_id(target_id)] = float(
                    battery_percent
                )

    problem_robots = {
        canonical_robot_id(row.get("robot_id")): row
        for row in problem.get("robots", [])
        if row.get("robot_id") is not None
    }
    battery_metrics = (deterministic_result or {}).get("metrics", {}).get(
        "battery_by_robot", {}
    )
    canonical_battery_metrics = {
        canonical_robot_id(robot_id): row
        for robot_id, row in battery_metrics.items()
    }
    minimum_battery = float(problem.get("min_robot_battery") or 0.0)
    for robot_id, expected_battery in sorted(battery_overrides.items()):
        problem_robot = problem_robots.get(robot_id)
        metric = canonical_battery_metrics.get(robot_id)
        problem_value = (
            float(problem_robot.get("battery"))
            if problem_robot and problem_robot.get("battery") is not None
            else None
        )
        metric_value = (
            float(metric.get("initial_battery"))
            if metric and metric.get("initial_battery") is not None
            else None
        )
        # The optimization input is the authoritative proof that the
        # hypothetical override was applied.  Simulation metrics are an
        # additional consistency check only when simulation reached the
        # battery-calculation stage.  A routing/resource failure must not be
        # misreported as ROBOT_STATE_OVERRIDE_NOT_APPLIED merely because
        # battery_by_robot is unavailable.
        if (
            problem_value is None
            or not math.isclose(problem_value, expected_battery, abs_tol=1e-6)
            or (
                metric_value is not None
                and not math.isclose(metric_value, expected_battery, abs_tol=1e-6)
            )
        ):
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="ROBOT_STATE_OVERRIDE_NOT_APPLIED",
                message=(
                    f"{robot_id}의 가정 배터리 {expected_battery:.3f}%가 "
                    "최적화 문제와 시뮬레이션 초기 상태에 적용되지 않았습니다."
                ),
                robot_ids=[robot_id],
            )
            continue

        if metric is None:
            # Other deterministic evidence already reports routing or
            # simulation failure.  Do not invent an override failure.
            continue

        consumption = float(metric.get("estimated_consumption") or 0.0)
        projected_without_charge = expected_battery - consumption
        charge_task_ids = [
            str(value) for value in metric.get("charge_task_ids", [])
        ]
        if (
            projected_without_charge < minimum_battery - 1e-6
            and not charge_task_ids
        ):
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="MISSING_REQUIRED_CHARGE",
                message=(
                    f"{robot_id}는 충전 없이 완료하면 "
                    f"{projected_without_charge:.3f}%로 최소 기준 "
                    f"{minimum_battery:.3f}%를 충족하지 못하지만 CHARGE 작업이 없습니다."
                ),
                robot_ids=[robot_id],
            )
        final_battery = float(metric.get("final_battery") or 0.0)
        if final_battery < minimum_battery - 1e-6:
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="BATTERY_BELOW_MINIMUM",
                message=(
                    f"{robot_id}의 예상 완료 배터리 {final_battery:.3f}%가 "
                    f"최소 기준 {minimum_battery:.3f}%보다 낮습니다."
                ),
                robot_ids=[robot_id],
                task_ids=charge_task_ids,
            )

    charger_selection_rows = {
        str(row.get("task_id") or ""): row
        for row in state.get("cuopt_plan", {})
        .get("metadata", {})
        .get("charger_selections", [])
        if row.get("task_id")
    }
    robot_command_batches_present = "robot_command_batches" in state
    charge_commands = {
        str(command.get("task_id") or ""): command
        for batch in state.get("robot_command_batches", [])
        for command in batch.get("commands", [])
        if str(command.get("action") or "").upper() == "CHARGE"
        and command.get("task_id")
    }

    active_chargers = {
        int(row["node_id"])
        for row in problem.get("nodes", [])
        if row.get("active", True)
        and str(row.get("node_type") or "").upper() == "CHARGER"
    }
    step_seconds = int(problem.get("time_step_seconds") or 1)
    charge_rate = float(problem.get("charge_rate_percent_per_minute") or 0.0)
    battery_safety_margin = max(
        0.0, float(problem.get("battery_safety_margin_percent") or 0.0)
    )
    safe_arrival_threshold = minimum_battery + battery_safety_margin
    operation_charge_target = float(
        problem.get("charge_target_battery") or 80.0
    )
    for raw_task in plan_rows:
        if str(raw_task.get("action") or "").upper() != "CHARGE":
            continue
        task_id = str(raw_task.get("task_id") or "")
        robot_id = canonical_robot_id(raw_task.get("robot_id"))
        charger_node = int(raw_task.get("target_node"))
        if charger_node not in active_chargers:
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="CHARGE_NODE_INVALID",
                message=(
                    f"CHARGE 작업 {task_id}의 노드 {charger_node}는 "
                    "active CHARGER가 아닙니다."
                ),
                robot_ids=[robot_id],
                task_ids=[task_id],
                node_ids=[charger_node],
            )
        charged_percent = float(raw_task.get("charged_percent") or 0.0)
        duration_seconds = raw_task.get("charge_duration_seconds")
        if charge_rate > 0 and duration_seconds is not None:
            expected_seconds = math.ceil(
                (charged_percent / charge_rate) * 60 / step_seconds
            ) * step_seconds
            if int(duration_seconds) != expected_seconds:
                _verification_evidence_row(
                    evidence,
                    source="COMMAND_CONSTRAINT",
                    severity="BLOCKING",
                    code="CHARGE_CALCULATION_MISMATCH",
                    message=(
                        f"CHARGE 작업 {task_id}의 충전량 {charged_percent:.3f}%와 "
                        f"충전 시간 {duration_seconds}초가 설정 속도와 일치하지 않습니다."
                    ),
                    robot_ids=[robot_id],
                    task_ids=[task_id],
                )
        selection = charger_selection_rows.get(task_id, {})
        battery_at_charger = selection.get("battery_at_charger")
        if (
            battery_at_charger is not None
            and float(battery_at_charger) < safe_arrival_threshold - 1e-6
        ):
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="BATTERY_BELOW_SAFE_CHARGER_ARRIVAL",
                message=(
                    f"{robot_id}가 충전소 {charger_node}에 도착할 때 배터리 "
                    f"{float(battery_at_charger):.3f}%로 안전 도달 기준 "
                    f"{safe_arrival_threshold:.3f}% "
                    f"(최소 {minimum_battery:.3f}% + 여유 "
                    f"{battery_safety_margin:.3f}%)를 충족하지 못합니다."
                ),
                robot_ids=[robot_id],
                task_ids=[task_id],
                node_ids=[charger_node],
            )

        target_battery = raw_task.get("charge_target_battery")
        if (
            target_battery is None
            or float(target_battery) < operation_charge_target - 1e-6
        ):
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="CHARGE_TARGET_POLICY_NOT_MET",
                message=(
                    f"CHARGE 작업 {task_id}의 목표 배터리가 "
                    f"{float(target_battery or 0.0):.3f}%로 작업 투입 기준 "
                    f"{operation_charge_target:.3f}%를 충족하지 못합니다."
                ),
                robot_ids=[robot_id],
                task_ids=[task_id],
                node_ids=[charger_node],
            )

        charge_command = charge_commands.get(task_id)
        if robot_command_batches_present and charge_command is None:
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="CHARGE_COMMAND_NOT_GENERATED",
                message=(
                    f"CHARGE 작업 {task_id}에 대응하는 실제 로봇 CHARGE "
                    "명령이 생성되지 않았습니다."
                ),
                robot_ids=[robot_id],
                task_ids=[task_id],
                node_ids=[charger_node],
            )
        elif robot_command_batches_present and duration_seconds is not None:
            routed_duration = int(
                (charge_command.get("payload") or {}).get("duration_seconds") or 0
            )
            if routed_duration != int(duration_seconds):
                _verification_evidence_row(
                    evidence,
                    source="COMMAND_CONSTRAINT",
                    severity="BLOCKING",
                    code="CHARGE_DURATION_NOT_ROUTED",
                    message=(
                        f"CHARGE 작업 {task_id}의 계획 충전시간은 "
                        f"{int(duration_seconds)}초이지만 로봇 명령에는 "
                        f"{routed_duration}초가 반영되었습니다."
                    ),
                    robot_ids=[robot_id],
                    task_ids=[task_id],
                    node_ids=[charger_node],
                )

        candidates = list(raw_task.get("charger_candidates") or [])
        safe_candidates = [
            row
            for row in candidates
            if row.get("safe_reachable") is not False
            and row.get("rejection_reason") in (None, "")
        ]
        selection_policy = (
            raw_task.get("charger_selection_policy")
            or selection.get("selection_policy")
        )
        if is_opportunity_policy(selection_policy):
            expected, expected_policy, expected_reason = (
                expected_opportunity_candidate(candidates)
            )
            selected_rows = [
                row for row in safe_candidates if bool(row.get("selected"))
            ]
            if expected is None:
                _verification_evidence_row(
                    evidence,
                    source="COMMAND_CONSTRAINT",
                    severity="BLOCKING",
                    code="OPPORTUNITY_CHARGER_EVIDENCE_MISSING",
                    message=(
                        f"CHARGE 작업 {task_id}에 안전한 기회 충전 후보가 "
                        "기록되지 않았습니다."
                    ),
                    robot_ids=[robot_id],
                    task_ids=[task_id],
                    node_ids=[charger_node],
                )
            else:
                expected_node = int(expected["charger_node"])
                if len(selected_rows) != 1:
                    _verification_evidence_row(
                        evidence,
                        source="COMMAND_CONSTRAINT",
                        severity="BLOCKING",
                        code="OPPORTUNITY_CHARGER_SELECTION_EVIDENCE_INVALID",
                        message=(
                            f"CHARGE 작업 {task_id}의 후보 증거에서 선택된 "
                            f"충전소가 {len(selected_rows)}개입니다. 정확히 1개여야 합니다."
                        ),
                        robot_ids=[robot_id],
                        task_ids=[task_id],
                        node_ids=[charger_node],
                    )
                elif int(selected_rows[0]["charger_node"]) != charger_node:
                    _verification_evidence_row(
                        evidence,
                        source="COMMAND_CONSTRAINT",
                        severity="BLOCKING",
                        code="OPPORTUNITY_CHARGER_SELECTED_NODE_MISMATCH",
                        message=(
                            f"CHARGE 작업 {task_id}의 대상 노드 {charger_node}와 "
                            "후보 평가에서 선택된 충전소가 일치하지 않습니다."
                        ),
                        robot_ids=[robot_id],
                        task_ids=[task_id],
                        node_ids=[charger_node, int(selected_rows[0]["charger_node"])],
                    )
                if expected_node != charger_node:
                    _verification_evidence_row(
                        evidence,
                        source="COMMAND_CONSTRAINT",
                        severity="BLOCKING",
                        code="OPPORTUNITY_CHARGER_POLICY_SELECTION_INVALID",
                        message=(
                            f"CHARGE 작업 {task_id}은 공통 선택 정책 "
                            f"{expected_policy} 기준으로 노드 {expected_node}를 "
                            f"선택해야 하지만 노드 {charger_node}를 선택했습니다."
                        ),
                        robot_ids=[robot_id],
                        task_ids=[task_id],
                        node_ids=[charger_node, expected_node],
                    )
                legacy_opportunity_policy = (
                    str(selection_policy or "").upper()
                    == "OPPORTUNITY_CHARGE_WITH_LINKED_WAITING_AREA"
                )
                if str(selection_policy or "") != expected_policy:
                    _verification_evidence_row(
                        evidence,
                        source="COMMAND_CONSTRAINT",
                        severity=(
                            "WARNING" if legacy_opportunity_policy else "BLOCKING"
                        ),
                        code=(
                            "LEGACY_OPPORTUNITY_CHARGER_POLICY_NORMALIZED"
                            if legacy_opportunity_policy
                            else "OPPORTUNITY_CHARGER_POLICY_MISMATCH"
                        ),
                        message=(
                            f"CHARGE 작업 {task_id}의 기록 정책 "
                            f"{selection_policy}를 후보 데이터로 재현한 정책 "
                            f"{expected_policy}로 검증했습니다."
                        ),
                        robot_ids=[robot_id],
                        task_ids=[task_id],
                        node_ids=[charger_node],
                    )
                if expected_policy.endswith("DISTANCE_FALLBACK"):
                    _verification_evidence_row(
                        evidence,
                        source="COMMAND_CONSTRAINT",
                        severity="WARNING",
                        code="CHARGER_COST_DATA_INCOMPLETE_DISTANCE_FALLBACK",
                        message=expected_reason,
                        robot_ids=[robot_id],
                        task_ids=[task_id],
                        node_ids=[int(row["charger_node"]) for row in safe_candidates],
                    )
        else:
            cost_candidates = [
                row
                for row in safe_candidates
                if row.get("charger_cost") is not None
            ]
            if cost_candidates:
                minimum_cost = min(float(row["charger_cost"]) for row in cost_candidates)
                selected_cost = raw_task.get("charger_cost")
                if (
                    selected_cost is None
                    or not math.isclose(
                        float(selected_cost), minimum_cost, abs_tol=1e-9
                    )
                ):
                    _verification_evidence_row(
                        evidence,
                        source="COMMAND_CONSTRAINT",
                        severity="BLOCKING",
                        code="CHARGER_COST_SELECTION_INVALID",
                        message=(
                            f"CHARGE 작업 {task_id}이 안전 도달 가능한 후보 중 "
                            f"최저 설정 비용 {minimum_cost}의 충전소를 "
                            "선택하지 않았습니다."
                        ),
                        robot_ids=[robot_id],
                        task_ids=[task_id],
                        node_ids=[charger_node],
                    )
            elif safe_candidates:
                _verification_evidence_row(
                    evidence,
                    source="COMMAND_CONSTRAINT",
                    severity="WARNING",
                    code="CHARGER_COST_DATA_MISSING",
                    message=(
                        "안전 도달 가능한 active CHARGER 후보에 비교 가능한 "
                        "비용 속성이 없어 거리 기준 fallback을 사용했습니다."
                    ),
                    robot_ids=[robot_id],
                    task_ids=[task_id],
                    node_ids=[int(row["charger_node"]) for row in safe_candidates],
                )

    if interpretation_raw.get("target_node_ids"):
        requested_targets = {
            int(value) for value in interpretation_raw.get("target_node_ids", [])
        }
        raw_operations = list(interpretation_raw.get("inventory_operations", []))
        outbound_operations = [
            row
            for row in raw_operations
            if str(
                row.get("operation_type")
                if isinstance(row, dict)
                else getattr(row, "operation_type", "")
            ).upper()
            == "OUTBOUND"
        ]
        outbound_requested = bool(outbound_operations)

        # P16.3.1: A global outbound target is a constraint only for outbound
        # operations that survived inventory feasibility and were actually
        # planned.  In a partial-success daily plan, all outbound operations
        # can be blocked while an independent inbound operation continues.
        # The inbound DROP must not be treated as evidence that the outbound
        # target was omitted, and the verifier must not turn the valid partial
        # result into VERIFICATION_FAILED merely because no outbound task was
        # eligible for planning.
        planned_outbound_work_ids: set[str] | None = None
        if outbound_requested:
            def outbound_operation_work_id(row: object) -> str:
                raw_work_id = (
                    row.get("work_id")
                    if isinstance(row, dict)
                    else getattr(row, "work_id", None)
                )
                raw_operation_id = (
                    row.get("operation_id")
                    if isinstance(row, dict)
                    else getattr(row, "operation_id", None)
                )
                raw_value = raw_work_id or raw_operation_id
                if not raw_value:
                    return ""
                text = str(raw_value).strip()
                # Existing PostgreSQL works are sometimes surfaced as
                # operation_id="work:<work_id>" after clarification binding.
                # Strip only that explicit namespace; do not infer arbitrary
                # operation IDs as work IDs.
                if not raw_work_id and text.lower().startswith("work:"):
                    text = text.split(":", 1)[1]
                return canonical_work_id(text)

            outbound_work_ids = {
                value
                for row in outbound_operations
                if (value := outbound_operation_work_id(row))
            }
            feasibility_rows = list(
                (state.get("inventory_feasibility") or {}).get("item_results", [])
            )
            matched_feasibility = [
                row
                for row in feasibility_rows
                if outbound_operation_work_id(row) in outbound_work_ids
            ]
            if matched_feasibility:
                planned_outbound_work_ids = {
                    outbound_operation_work_id(row)
                    for row in matched_feasibility
                    if int(row.get("planned_quantity_boxes") or 0) > 0
                }

        should_validate_target = (
            not outbound_requested
            or planned_outbound_work_ids is None
            or bool(planned_outbound_work_ids)
        )

        def applies_requested_outbound_target(row: dict[str, Any]) -> bool:
            if row.get("target_node") is None:
                return False
            if int(row.get("target_node")) not in requested_targets:
                return False
            if not outbound_requested:
                return True

            row_work_id = canonical_work_id(str(row.get("work_id") or ""))
            if (
                planned_outbound_work_ids is not None
                and row_work_id not in planned_outbound_work_ids
            ):
                return False

            action = str(row.get("action") or "").upper()
            if action == "DROP":
                # Preserve the existing PICK/DROP outbound contract.
                return True
            if action != "MOVE":
                return False

            # P16.5.15.1: legacy PostgreSQL works are represented by a single
            # <work_id>:move task.  The execution adapter expands that task to
            # PICKUP -> MOVE -> DROPOFF, so its target is valid outbound
            # evidence only when it is explicitly bound to the requested
            # outbound work.  Unrelated relocation MOVE rows must not satisfy
            # the command constraint.
            task_id = str(row.get("task_id") or "").strip()
            if not row_work_id or row_work_id not in outbound_work_ids:
                return False
            if not task_id.lower().endswith(":move"):
                return False
            task_work_id = canonical_work_id(task_id.rsplit(":", 1)[0])
            return task_work_id == row_work_id

        applied = False
        if should_validate_target:
            applied = any(
                applies_requested_outbound_target(row)
                for row in plan_rows
                if isinstance(row, dict)
            )
        if should_validate_target and not applied:
            _verification_evidence_row(
                evidence,
                source="COMMAND_CONSTRAINT",
                severity="BLOCKING",
                code="TARGET_NODE_NOT_APPLIED",
                message=(
                    f"사용자가 지정한 목적지 노드 {sorted(requested_targets)}가 "
                    "계획 작업에 적용되지 않았습니다."
                ),
                node_ids=sorted(requested_targets),
            )

    unassigned = [
        str(value)
        for value in state.get("cuopt_plan", {}).get("unassigned_task_ids", [])
    ]
    if unassigned:
        _verification_evidence_row(
            evidence,
            source="OPTIMIZER",
            severity="BLOCKING",
            code="UNASSIGNED_TASKS",
            message=f"미배정 작업: {unassigned}",
            task_ids=unassigned,
        )
        for task_row in state.get("optimization_evidence", []):
            task_id = str(task_row.get("task_id"))
            if task_id not in unassigned:
                continue
            if any(
                candidate.get("rejection_reason") == "HARD_WINDOW_VIOLATION"
                for candidate in task_row.get("candidates", [])
            ):
                _verification_evidence_row(
                    evidence,
                    source="OPTIMIZER",
                    severity="BLOCKING",
                    code="HARD_WINDOW_VIOLATION",
                    message=f"작업 {task_id}는 지정된 hard window 안에 완료할 수 없습니다.",
                    task_ids=[task_id],
                )

    schedule_validation = state.get("schedule_validation", {}) or {}
    for violation in schedule_validation.get(
        "execution_dependency_violations", []
    ):
        _verification_evidence_row(
            evidence,
            source="SCHEDULE_VALIDATION",
            severity="BLOCKING",
            code=str(
                violation.get("code")
                or "EXECUTION_DEPENDENCY_ORDER_VIOLATION"
            ),
            message=str(violation.get("message") or "실행 작업 의존성 위반"),
            task_ids=[str(value) for value in violation.get("task_ids", [])],
        )

    known_messages = {str(row["message"]) for row in evidence}
    if route_failure:
        known_messages.add(str(route_failure.get("reason") or ""))
        error_code = str(route_failure.get("error_code") or "ROUTE_FAILED")
        known_messages.add(
            f"충돌 방지 경로 실패 [{error_code}]: {route_failure.get('reason')}"
        )
    for message in state.get("errors", []):
        if str(message) in known_messages:
            continue
        _verification_evidence_row(
            evidence,
            source="PIPELINE",
            severity="BLOCKING",
            code="PIPELINE_ERROR",
            message=str(message),
        )
    known_messages = {str(row["message"]) for row in evidence}
    for message in state.get("warnings", []):
        if str(message) in known_messages:
            continue
        _verification_evidence_row(
            evidence,
            source="PIPELINE",
            severity="WARNING",
            code="PIPELINE_WARNING",
            message=str(message),
        )

    if not any(row["severity"] == "BLOCKING" for row in evidence):
        valid = bool((deterministic_result or {}).get("valid"))
        if valid:
            _verification_evidence_row(
                evidence,
                source="DETERMINISTIC_VALIDATION",
                severity="PASS",
                code="DETERMINISTIC_VALIDATION_PASSED",
                message="결정론적 계획 검증을 통과했습니다.",
            )

    sanitized = sanitize_log_details(evidence)
    for row in sanitized:
        row.pop("signature", None)
    return sanitized


def _verification_summary(decision: str) -> str:
    summaries = {
        "PASS": "결정론적 검증 결과가 유효하며 차단 사유가 없습니다.",
        "PASS_WITH_WARNING": "결정론적 검증은 통과했으며 확인할 경고가 있습니다.",
        "REPLAN_LOCAL": "확인된 일부 로봇 또는 작업 범위의 재계획이 필요합니다.",
        "REPLAN_GLOBAL": "확인된 전체 계획 범위 제약으로 전역 재계획이 필요합니다.",
        "CLARIFICATION_REQUIRED": "계획을 계속하기 전에 사용자 확인이 필요합니다.",
        "FAIL": "확인된 차단 사유로 계획을 승인할 수 없습니다.",
    }
    return summaries[decision]


def deterministic_verification_decision(
    state: PlanningState,
    evidence: list[dict[str, Any]] | None = None,
) -> VerificationDecision:
    evidence = evidence or build_verification_evidence(state)
    blocking = [row for row in evidence if row["severity"] == "BLOCKING"]
    warning_rows = [row for row in evidence if row["severity"] == "WARNING"]
    codes = {str(row["code"]) for row in blocking}
    affected_robot_ids = sorted(
        {
            str(robot_id)
            for row in blocking
            for robot_id in row.get("robot_ids", [])
        }
    )
    affected_task_ids = sorted(
        {
            str(task_id)
            for row in blocking
            for task_id in row.get("task_ids", [])
        }
    )
    supervisor = state.get("supervisor_decision", {})
    clarification = bool(supervisor.get("requires_clarification"))
    allow_replan = bool(supervisor.get("allow_replan", True))

    execute_hard_window = (
        state.get("interpretation", {}).get("execution_mode") == "EXECUTE"
        and "HARD_WINDOW_VIOLATION" in codes
    )
    if clarification or "CLARIFICATION_REQUIRED" in codes or execute_hard_window:
        decision = "CLARIFICATION_REQUIRED"
    elif blocking:
        if codes & HARD_VERIFICATION_FAILURE_CODES or not allow_replan:
            decision = "FAIL"
        elif codes & GLOBAL_REPLAN_CODES:
            decision = "REPLAN_GLOBAL"
        elif affected_robot_ids or affected_task_ids:
            decision = "REPLAN_LOCAL"
        else:
            decision = "FAIL"
    elif warning_rows:
        decision = "PASS_WITH_WARNING"
    else:
        decision = "PASS"

    requires_replan = decision in {"REPLAN_LOCAL", "REPLAN_GLOBAL"}
    replan_scope = (
        "LOCAL_REPLAN"
        if decision == "REPLAN_LOCAL"
        else "GLOBAL_REPLAN"
        if decision == "REPLAN_GLOBAL"
        else "NO_REPLAN"
    )
    confidence = 0.95 if decision in {"PASS_WITH_WARNING", "REPLAN_LOCAL", "REPLAN_GLOBAL"} else 1.0
    return VerificationDecision(
        decision=decision,
        requires_replan=requires_replan,
        replan_scope=replan_scope,
        affected_robot_ids=affected_robot_ids,
        affected_task_ids=affected_task_ids,
        blocking_findings=[str(row["message"]) for row in blocking],
        warning_findings=[str(row["message"]) for row in warning_rows],
        user_visible_warnings=[str(row["message"]) for row in warning_rows],
        confidence=confidence,
        evidence_ids=[str(row["evidence_id"]) for row in evidence],
        summary=_verification_summary(decision),
    )


def normalize_verification_decision(
    raw_decision: VerificationDecision,
    deterministic: VerificationDecision,
) -> VerificationDecision:
    """LLM이 deterministic 차단·경고·evidence를 지우거나 만들지 못하게 합니다."""

    allowed_by_baseline = {
        "PASS": {"PASS"},
        "PASS_WITH_WARNING": {"PASS_WITH_WARNING"},
        "REPLAN_LOCAL": {"REPLAN_LOCAL", "REPLAN_GLOBAL", "FAIL"},
        "REPLAN_GLOBAL": {"REPLAN_GLOBAL", "FAIL"},
        "CLARIFICATION_REQUIRED": {"CLARIFICATION_REQUIRED"},
        "FAIL": {"FAIL"},
    }
    decision = (
        raw_decision.decision
        if raw_decision.decision
        in allowed_by_baseline[deterministic.decision]
        else deterministic.decision
    )
    requires_replan = decision in {"REPLAN_LOCAL", "REPLAN_GLOBAL"}
    replan_scope = (
        "LOCAL_REPLAN"
        if decision == "REPLAN_LOCAL"
        else "GLOBAL_REPLAN"
        if decision == "REPLAN_GLOBAL"
        else "NO_REPLAN"
    )
    return VerificationDecision(
        decision=decision,
        requires_replan=requires_replan,
        replan_scope=replan_scope,
        affected_robot_ids=deterministic.affected_robot_ids,
        affected_task_ids=deterministic.affected_task_ids,
        blocking_findings=deterministic.blocking_findings,
        warning_findings=deterministic.warning_findings,
        user_visible_warnings=deterministic.user_visible_warnings,
        confidence=raw_decision.confidence,
        evidence_ids=deterministic.evidence_ids,
        summary=_verification_summary(decision),
    )


def build_verification_llm() -> ChatOpenAI:
    return build_supervisor_llm()


def _verification_llm_payload(
    state: PlanningState,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    deterministic_result = state.get("simulation") or state.get("plan_validation", {})
    cuopt_plan = state.get("cuopt_plan", {})
    collision_plan = state.get("collision_plan", {})
    return sanitize_log_details(
        {
            "command": {
                "command_id": state.get("command", {}).get("command_id"),
                "text": state.get("command", {}).get("text"),
            },
            "interpretation": state.get("interpretation", {}),
            "supervisor_decision": state.get("supervisor_decision", {}),
            "snapshot_validation": state.get("validation", {}),
            "optimization_summary": {
                "scheduled_task_count": len(cuopt_plan.get("scheduled_tasks", [])),
                "unassigned_task_ids": cuopt_plan.get("unassigned_task_ids", []),
                "objective_value": cuopt_plan.get("objective_value"),
                "metadata": cuopt_plan.get("metadata", {}),
            },
            "route_summary": {
                "route_count": len(collision_plan.get("routes", [])),
                "total_distance": collision_plan.get("total_distance"),
                "metadata": compact_route_metadata_for_llm(
                    collision_plan.get("metadata", {})
                ),
                "routes": [
                    {
                        "robot_id": route.get("robot_id"),
                        "task_ids": route.get("task_ids", []),
                        "waypoint_count": len(route.get("waypoints", [])),
                        "distance": route.get("distance"),
                    }
                    for route in collision_plan.get("routes", [])
                ],
            },
            "deterministic_validation": {
                "valid": deterministic_result.get("valid"),
                "status": deterministic_result.get("status"),
                "issues": deterministic_result.get("issues", []),
                "errors": deterministic_result.get("errors", []),
                "warnings": deterministic_result.get("warnings", []),
                "metrics": deterministic_result.get("metrics", {}),
            },
            "pipeline_errors": state.get("errors", []),
            "pipeline_warnings": state.get("warnings", []),
            "replan_history": state.get("replan_history", []),
            "evidence": evidence,
        }
    )


def _history_with_verification_after(
    state: PlanningState,
    decision: VerificationDecision,
) -> list[dict[str, Any]] | None:
    history = deepcopy(state.get("replan_history", []))
    attempt = int(state.get("replan_attempt", 0))
    if not history or attempt <= 0:
        return None
    if int(history[-1].get("attempt") or 0) != attempt:
        return None
    history[-1]["verification_after"] = decision.decision
    if decision.decision not in {"PASS", "PASS_WITH_WARNING"}:
        history[-1]["status"] = "FAILED"
    return history


def verification_agent_node(state: PlanningState) -> dict[str, Any]:
    settings = get_settings()
    evidence = build_verification_evidence(state)
    deterministic = deterministic_verification_decision(state, evidence)
    source = "deterministic_fallback"
    fallback_reason: str | None = None
    verification_warnings: list[str] = []
    started_trace = trace(
        "verification_started",
        prompt_version=VERIFICATION_PROMPT_VERSION,
        model_name=(getattr(settings, "openai_model", None) or None),
        llm_enabled=bool(getattr(settings, "openai_api_key", "")),
        evidence_count=len(evidence),
    )
    decision: VerificationDecision | None = None

    if getattr(settings, "openai_api_key", ""):
        try:
            structured = build_verification_llm().with_structured_output(
                VerificationDecision,
                method="json_schema",
            )
            raw_decision = structured.invoke(
                [
                    SystemMessage(content=VERIFICATION_PROMPT),
                    HumanMessage(
                        content=json.dumps(
                            _verification_llm_payload(state, evidence),
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                ]
            )
            decision = normalize_verification_decision(
                VerificationDecision.model_validate(raw_decision),
                deterministic,
            )
            source = "llm"
        except Exception as exc:
            fallback_reason = f"LLM Verification 실패: {exc}"
            verification_warnings.append(fallback_reason)
    else:
        fallback_reason = (
            "OPENAI_API_KEY가 없어 deterministic Verification을 사용했습니다."
        )

    if decision is None:
        decision = deterministic

    verification_trace = list(started_trace)
    if source == "deterministic_fallback":
        verification_trace.extend(
            trace(
                "verification_fallback_used",
                prompt_version=VERIFICATION_PROMPT_VERSION,
                reason=fallback_reason,
            )
        )
    verification_trace.extend(
        trace(
            "verification_completed",
            success=decision.decision in {"PASS", "PASS_WITH_WARNING"},
            prompt_version=VERIFICATION_PROMPT_VERSION,
            source=source,
            fallback_used=source != "llm",
            decision=decision.decision,
            requires_replan=decision.requires_replan,
            replan_scope=decision.replan_scope,
            affected_robot_ids=decision.affected_robot_ids,
            affected_task_ids=decision.affected_task_ids,
            blocking_finding_count=len(decision.blocking_findings),
            warning_finding_count=len(decision.warning_findings),
            evidence_ids=decision.evidence_ids,
            confidence=decision.confidence,
            summary=decision.summary,
        )
    )

    if decision.decision in {"PASS", "PASS_WITH_WARNING"}:
        final_status = state.get("final_status", "VERIFICATION_PASSED")
    elif decision.decision in {"REPLAN_LOCAL", "REPLAN_GLOBAL"}:
        final_status = "REPLAN_REQUIRED"
    elif decision.decision == "CLARIFICATION_REQUIRED":
        final_status = "CLARIFICATION_REQUIRED"
    else:
        final_status = "VERIFICATION_FAILED"
    update: dict[str, Any] = {
        "verification_decision": decision.model_dump(mode="json"),
        "verification_evidence": evidence,
        "verification_source": source,
        "verification_prompt_version": VERIFICATION_PROMPT_VERSION,
        "verification_warnings": verification_warnings,
        "warnings": verification_warnings,
        "final_status": final_status,
        "trace": verification_trace,
    }
    hard_window_blocked = any(
        row.get("code") == "HARD_WINDOW_VIOLATION" for row in evidence
    )
    if decision.decision == "CLARIFICATION_REQUIRED" and hard_window_blocked:
        command = NaturalLanguageCommand.model_validate(state["command"])
        clarification = ClarificationRequest(
            clarification_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"warehouse-hard-window:{command.command_id}",
                )
            ),
            conversation_id=command.conversation_id,
            command_id=command.command_id,
            reason_code="HARD_WINDOW_INSERTION_CONFLICT",
            question=(
                "긴급 작업을 삽입하면 hard window를 지킬 수 없습니다. "
                "긴급 작업을 가능한 다음 시각으로 미루거나, 기존 시간창 변경, "
                "다른 로봇 사용, 작업 취소 중 하나를 선택해주세요."
            ),
            missing_fields=["hard_window_resolution"],
            ambiguous_fields=[],
            options=[
                ClarificationOption(value="DELAY_URGENT", label="긴급 작업 연기"),
                ClarificationOption(value="CHANGE_WINDOW", label="기존 시간창 변경"),
                ClarificationOption(value="USE_OTHER_ROBOT", label="다른 로봇 사용"),
                ClarificationOption(value="CANCEL_TASK", label="작업 취소"),
            ],
            original_text=command.text,
        )
        update["clarification"] = clarification.model_dump(mode="json")
    history = _history_with_verification_after(state, decision)
    if history is not None:
        update["replan_history"] = history
    return update


def verification_failure_signature(state: PlanningState) -> str:
    """결정론적 blocking evidence와 영향 대상의 안정적인 서명입니다."""

    blocking = [
        {
            "source": row.get("source"),
            "code": row.get("code"),
            "robot_ids": sorted(str(value) for value in row.get("robot_ids", [])),
            "task_ids": sorted(str(value) for value in row.get("task_ids", [])),
            "node_ids": sorted(int(value) for value in row.get("node_ids", [])),
            "time_steps": sorted(
                int(value) for value in row.get("time_steps", [])
            ),
        }
        for row in state.get("verification_evidence", [])
        if row.get("severity") == "BLOCKING"
    ]
    blocking.sort(
        key=lambda row: (
            str(row["source"]),
            str(row["code"]),
            row["robot_ids"],
            row["task_ids"],
            row["node_ids"],
            row["time_steps"],
        )
    )
    decision = state.get("verification_decision", {})
    payload = {
        "blocking": blocking,
        "affected_robot_ids": sorted(
            str(value) for value in decision.get("affected_robot_ids", [])
        ),
        "affected_task_ids": sorted(
            str(value) for value in decision.get("affected_task_ids", [])
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_task_ids(
    requested_ids: set[str],
    tasks: list[dict[str, Any]],
) -> set[str]:
    resolved: set[str] = set()
    for task in tasks:
        task_id = str(task.get("task_id"))
        work_id = str(task.get("work_id")) if task.get("work_id") else None
        if task_id in requested_ids or (work_id and work_id in requested_ids):
            resolved.add(task_id)
    return resolved


def _protected_replan_task_ids(state: PlanningState) -> set[str]:
    """실행 중 작업과 실제 활성 계획의 freeze horizon 작업을 보호합니다."""

    tasks = state.get("required_tasks", [])
    protected_refs: set[str] = {
        str(value)
        for value in state.get("snapshot", {})
        .get("redis", {})
        .get("executing_task_ids", [])
        if value
    }
    for work in state.get("snapshot", {}).get("sql", {}).get("works", []):
        if str(work.get("status") or "").upper() == "EXECUTING":
            protected_refs.add(str(work.get("work_id")))

    protected = _resolve_task_ids(protected_refs, tasks)
    active_plan = (
        state.get("snapshot", {}).get("redis", {}).get("active_plan") or {}
    )
    collision = active_plan.get("collision_plan") or {}
    routes = collision.get("routes") or []
    if not routes:
        return protected

    step_seconds = max(
        1,
        int(
            collision.get("time_step_seconds")
            or state.get("optimization_problem", {}).get("time_step_seconds")
            or getattr(get_settings(), "time_step_seconds", 1)
        ),
    )
    freeze_seconds = int(
        state.get("scope", {}).get("freeze_horizon_seconds")
        or getattr(get_settings(), "freeze_horizon_seconds", 15)
    )
    current_step = 0
    if active_plan.get("activated_at"):
        try:
            captured_at = as_utc_datetime(
                state["snapshot"]["captured_at"],
                field_name="snapshot.captured_at",
            )
            activated_at = as_utc_datetime(
                active_plan["activated_at"],
                field_name="active_plan.activated_at",
            )
            current_step = max(
                0,
                math.floor((captured_at - activated_at).total_seconds() / step_seconds),
            )
        except (TypeError, ValueError):
            current_step = 0
    freeze_until = current_step + math.ceil(freeze_seconds / step_seconds)
    frozen_route_task_ids: set[str] = set()
    scheduled_tasks = (
        active_plan.get("cuopt_plan", {}).get("scheduled_tasks", [])
    )
    if scheduled_tasks:
        frozen_route_task_ids.update(
            str(task.get("task_id"))
            for task in scheduled_tasks
            if task.get("task_id")
            and int(task.get("start_time_step") or 0) <= freeze_until
            and int(task.get("end_time_step") or 0) >= current_step
        )
    else:
        # 오래된 활성 계획에 task schedule이 없을 때만 route 단위로 보수적으로
        # 보호합니다.
        for route in routes:
            if any(
                current_step <= int(waypoint.get("time_step") or 0) <= freeze_until
                for waypoint in route.get("waypoints", [])
            ):
                frozen_route_task_ids.update(
                    str(value) for value in route.get("task_ids", []) if value
                )
    protected.update(_resolve_task_ids(frozen_route_task_ids, tasks))
    return protected


def _replan_guard_failure(
    state: PlanningState,
    *,
    reason: str,
    stage: str,
    signatures: dict[str, int] | None = None,
) -> dict[str, Any]:
    previous = VerificationDecision.model_validate(state["verification_decision"])
    evidence = deepcopy(state.get("verification_evidence", []))
    evidence_id = f"verification:{len(evidence) + 1:03d}"
    evidence.append(
        {
            "evidence_id": evidence_id,
            "source": "REPLAN_GUARD",
            "severity": "BLOCKING",
            "code": stage.upper(),
            "message": reason,
            "robot_ids": previous.affected_robot_ids,
            "task_ids": previous.affected_task_ids,
        }
    )
    failed = VerificationDecision(
        decision="FAIL",
        requires_replan=False,
        replan_scope="NO_REPLAN",
        affected_robot_ids=previous.affected_robot_ids,
        affected_task_ids=previous.affected_task_ids,
        blocking_findings=previous.blocking_findings + [reason],
        warning_findings=previous.warning_findings,
        user_visible_warnings=previous.user_visible_warnings,
        confidence=1.0,
        evidence_ids=previous.evidence_ids + [evidence_id],
        summary="재계획 안전 가드가 추가 시도를 차단했습니다.",
    )
    history = deepcopy(state.get("replan_history", []))
    if history:
        history[-1]["status"] = "FAILED"
        history[-1]["verification_after"] = "FAIL"
    return {
        "verification_decision": failed.model_dump(mode="json"),
        "verification_evidence": evidence,
        "last_verification_decision": previous.model_dump(mode="json"),
        "repeated_failure_signatures": (
            signatures
            if signatures is not None
            else state.get("repeated_failure_signatures", {})
        ),
        "replan_history": history,
        "replan_ready": False,
        "replan_reason": reason,
        "final_status": "VERIFICATION_FAILED",
        "errors": [reason],
        "trace": trace(stage, success=False, reason=reason)
        + (
            []
            if stage == "replan_failed"
            else trace("replan_failed", success=False, reason=reason)
        ),
    }


def _previous_successful_candidate(state: PlanningState) -> dict[str, Any]:
    existing = state.get("previous_successful_candidate")
    if isinstance(existing, dict) and existing:
        return deepcopy(existing)
    collision = deepcopy(state.get("collision_plan", {}))
    simulation = deepcopy(state.get("simulation", {}))
    plan_validation = deepcopy(state.get("plan_validation", {}))
    routes = list(collision.get("routes") or [])
    simulation_succeeded = bool(
        simulation.get("success") or plan_validation.get("success")
    )
    if not routes or not simulation_succeeded:
        return {}
    return {
        "plan_version": state.get("current_plan_version")
        or state.get("plan_version"),
        "routing_succeeded": True,
        "simulation_succeeded": True,
        "verification_decision": deepcopy(state.get("verification_decision", {})),
        "rejection_reason": state.get("replan_reason")
        or "; ".join(
            state.get("verification_decision", {}).get("blocking_findings", [])
        ),
        "cuopt_plan": deepcopy(state.get("cuopt_plan", {})),
        "collision_plan": collision,
        "simulation": simulation,
        "plan_validation": plan_validation,
        "routing_evidence": deepcopy(state.get("routing_evidence", {})),
        "reservation_evidence": deepcopy(state.get("reservation_evidence", {})),
        "distance_comparison": deepcopy(state.get("distance_comparison", {})),
    }


def prepare_replan_node(state: PlanningState) -> dict[str, Any]:
    decision = VerificationDecision.model_validate(state["verification_decision"])
    if decision.decision not in {"REPLAN_LOCAL", "REPLAN_GLOBAL"}:
        return _replan_guard_failure(
            state,
            reason="재계획 결정이 아닌 상태에서 재계획이 요청되었습니다.",
            stage="replan_failed",
        )

    attempt = int(state.get("replan_attempt", 0))
    max_attempts = min(
        3,
        max(
            0,
            int(
                state.get("max_replan_attempts")
                or state.get("supervisor_decision", {}).get(
                    "max_replan_attempts", 0
                )
            ),
        ),
    )
    signature = verification_failure_signature(state)
    signatures = dict(state.get("repeated_failure_signatures", {}))
    signatures[signature] = signatures.get(signature, 0) + 1
    route_failure = state.get("route_failure") or {}
    mapf_repeat_escalation = bool(
        signatures[signature] >= 2
        and route_failure.get("retryable")
        and decision.replan_scope == "LOCAL_REPLAN"
        and attempt < max_attempts
    )
    requested_trace = trace(
        "replan_requested",
        attempt=attempt + 1,
        max_replan_attempts=max_attempts,
        scope=("GLOBAL_REPLAN" if mapf_repeat_escalation else decision.replan_scope),
        failure_signature=signature,
        route_failure_code=route_failure.get("code"),
        mapf_repeat_escalation=mapf_repeat_escalation,
    )

    if signatures[signature] >= 2 and not mapf_repeat_escalation:
        failed = _replan_guard_failure(
            state,
            reason="동일한 검증 실패 signature가 2회 반복되었습니다.",
            stage="repeated_failure_detected",
            signatures=signatures,
        )
        failed["trace"] = requested_trace + failed["trace"]
        return failed
    if attempt >= max_attempts:
        failed = _replan_guard_failure(
            state,
            reason=f"Supervisor가 허용한 최대 재계획 횟수 {max_attempts}회에 도달했습니다.",
            stage="replan_limit_reached",
            signatures=signatures,
        )
        failed["trace"] = requested_trace + failed["trace"]
        return failed

    tasks = deepcopy(state.get("required_tasks", []))
    protected = _protected_replan_task_ids(state)
    all_task_ids = {str(task.get("task_id")) for task in tasks}
    schedule = state.get("cuopt_plan", {}).get("scheduled_tasks", [])
    task_robot = {
        str(row.get("task_id")): str(row.get("robot_id"))
        for row in schedule
        if row.get("task_id") and row.get("robot_id")
    }
    known_robot_ids = {
        str(row.get("robot_id"))
        for row in state.get("snapshot", {}).get("sql", {}).get("robots", [])
        if row.get("robot_id")
    } | set(task_robot.values())

    if decision.decision == "REPLAN_LOCAL" and not mapf_repeat_escalation:
        requested_task_ids = set(decision.affected_task_ids)
        requested_robot_ids = {
            robot_id
            for robot_id in decision.affected_robot_ids
            if robot_id in known_robot_ids
        }
        changeable = _resolve_task_ids(requested_task_ids, tasks)
        changeable.update(
            task_id
            for task_id, robot_id in task_robot.items()
            if robot_id in requested_robot_ids
        )
        changeable.difference_update(protected)
        affected_robots = requested_robot_ids | {
            task_robot[task_id]
            for task_id in changeable
            if task_id in task_robot
        }
        scope_name = "LOCAL_REPLAN"
    else:
        changeable = all_task_ids - protected
        affected_robots = {
            task_robot[task_id]
            for task_id in changeable
            if task_id in task_robot
        }
        if not affected_robots:
            affected_robots = known_robot_ids
        scope_name = "GLOBAL_REPLAN"

    if not changeable:
        failed = _replan_guard_failure(
            state,
            reason="보호 대상을 제외한 재계획 대상 작업이 없습니다.",
            stage="replan_failed",
            signatures=signatures,
        )
        failed["trace"] = requested_trace + failed["trace"]
        return failed

    fixed = (all_task_ids - changeable) | protected
    for task in tasks:
        task_id = str(task.get("task_id"))
        task["frozen"] = task_id in fixed

    previous_version = (
        state.get("current_plan_version")
        or state.get("plan_version")
        or str(uuid4())
    )
    original_version = state.get("original_plan_version") or previous_version
    new_version = str(uuid4())
    reason = "; ".join(decision.blocking_findings) or decision.summary
    if mapf_repeat_escalation:
        reason = (
            reason
            + "; 동일한 MAPF 국소 실패가 반복되어 전역 재계획으로 1회 확장합니다."
        )
    history = deepcopy(state.get("replan_history", []))
    history.append(
        ReplanHistoryEntry(
            attempt=attempt + 1,
            scope=scope_name,
            reason=reason,
            affected_robot_ids=sorted(affected_robots),
            affected_task_ids=sorted(changeable),
            protected_task_ids=sorted(protected),
            previous_plan_version=previous_version,
            new_plan_version=new_version,
            verification_before=decision.decision,
            failure_signature=signature,
        ).model_dump(mode="json")
    )
    scope = ScopeDecision.model_validate(state["scope"])
    scope.plan_mode = scope_name
    scope.affected_robot_ids = sorted(affected_robots)
    scope.affected_task_ids = sorted(changeable)
    scope.changeable_task_ids = sorted(changeable)
    scope.fixed_task_ids = sorted(fixed)
    scope.include_new_command = False
    scope.reason_summary = reason[:500]
    replan_base_plan = {
        "plan_version": previous_version,
        "reference_time": state.get("optimization_problem", {}).get("reference_time"),
        "cuopt_plan": deepcopy(state.get("cuopt_plan", {})),
        "collision_plan": deepcopy(state.get("collision_plan", {})),
        "activated_at": None,
        "candidate_plan": True,
    }
    previous_successful_candidate = _previous_successful_candidate(state)
    mapf_replan_policy = (
        build_mapf_replan_policy(
            attempt=attempt + 1,
            scope=scope_name,
            affected_robot_ids=sorted(affected_robots),
            escalated_from_local=mapf_repeat_escalation,
        )
        if route_failure.get("retryable")
        else deepcopy(state.get("mapf_replan_policy", {}))
    )
    return {
        "scope": scope.model_dump(mode="json"),
        "required_tasks": tasks,
        "replan_attempt": attempt + 1,
        "replan_count": attempt + 1,
        "max_replan_attempts": max_attempts,
        "replan_history": history,
        "last_verification_decision": decision.model_dump(mode="json"),
        "repeated_failure_signatures": signatures,
        "replan_reason": reason,
        "route_failure": {},
        "mapf_replan_policy": mapf_replan_policy,
        "original_plan_version": original_version,
        "current_plan_version": new_version,
        "plan_version": new_version,
        "replan_base_plan": replan_base_plan,
        "previous_successful_candidate": previous_successful_candidate,
        "replan_ready": True,
        "cuopt_plan": {},
        "collision_plan": {},
        "simulation": {},
        "plan_validation": {},
        "verification_decision": {},
        "verification_evidence": [],
        # Route/adapter/report fields are derived from the candidate plan.
        # Explicitly clear them so LangGraph state merging cannot expose stale
        # CHARGE commands, task IDs, reservations, or battery evidence from the
        # rejected plan during LOCAL_REPLAN.
        "robot_command_batches": [],
        "adapter_validation": {},
        "routing_evidence": {},
        "reservation_evidence": {},
        "distance_comparison": {},
        "route_energy_reconciliation": {},
        "idle_energy_planning": {},
        "daily_schedule": [],
        "final_status": "REPLAN_READY",
        "trace": requested_trace
        + trace(
            "local_replan_started"
            if scope_name == "LOCAL_REPLAN"
            else "global_replan_started",
            success=True,
            attempt=attempt + 1,
            affected_robot_ids=sorted(affected_robots),
            affected_task_ids=sorted(changeable),
            protected_task_ids=sorted(protected),
            previous_plan_version=previous_version,
            new_plan_version=new_version,
            mapf_replan_policy=mapf_replan_policy,
            escalated_from_local=mapf_repeat_escalation,
        ),
    }


def complete_replan_node(state: PlanningState) -> dict[str, Any]:
    history = deepcopy(state.get("replan_history", []))
    decision = state.get("verification_decision", {}).get("decision")
    if history:
        history[-1]["verification_after"] = decision
        history[-1]["status"] = "COMPLETED"
    final_status = state.get("final_status")
    if state.get("interpretation", {}).get("execution_mode") == "PLAN_ONLY":
        final_status = "PLAN_READY"
    return {
        "replan_history": history,
        "replan_ready": False,
        "final_status": final_status,
        "trace": trace(
            "replan_completed",
            success=True,
            attempt=state.get("replan_attempt", 0),
            final_decision=decision,
            plan_version=state.get("current_plan_version"),
        ),
    }


def terminate_replan_node(state: PlanningState) -> dict[str, Any]:
    if int(state.get("replan_attempt", 0)) <= 0:
        return {}
    history = deepcopy(state.get("replan_history", []))
    decision = state.get("verification_decision", {}).get("decision")
    if history:
        history[-1]["verification_after"] = decision
        history[-1]["status"] = "FAILED"
    return {
        "replan_history": history,
        "replan_ready": False,
        "trace": trace(
            "replan_failed",
            success=False,
            attempt=state.get("replan_attempt", 0),
            final_decision=decision,
        ),
    }


def impact_analyzer_node(state: PlanningState) -> dict[str, Any]:
    result = SimulationResult.model_validate(state["simulation"])
    robot_ids = sorted(
        {robot_id for issue in result.issues for robot_id in issue.robot_ids}
    )
    task_ids = sorted(
        {task_id for issue in result.issues for task_id in issue.task_ids}
    )
    node_ids = sorted(
        {node_id for issue in result.issues for node_id in issue.node_ids}
    )
    time_steps = sorted(
        {step for issue in result.issues for step in issue.time_steps}
    )
    replan_count = state.get("replan_count", 0) + 1
    return {
        "impact": {
            "failure_codes": sorted({issue.code for issue in result.issues}),
            "affected_robot_ids": robot_ids,
            "affected_task_ids": task_ids,
            "affected_node_ids": node_ids,
            "affected_time_steps": time_steps,
            "recommended_scope": (
                "LOCAL_REPLAN" if replan_count < 2 else "GLOBAL_REPLAN"
            ),
            "fixed_rule": "완료 구간과 freeze horizon은 유지",
        },
        "replan_count": replan_count,
        "final_status": "REPLAN_ANALYZED",
        "trace": trace(
            "impact_analyzer",
            replan_count=replan_count,
            affected_tasks=task_ids,
        ),
    }


def persist_result_node(state: PlanningState) -> dict[str, Any]:
    report_evidence = build_report_evidence(state)
    state_for_storage = dict(state)
    state_for_storage["report_evidence"] = report_evidence
    state_for_storage["report_prompt_version"] = FINAL_REPORT_PROMPT_VERSION
    try:
        get_services().postgres.record_simulation(state_for_storage)
        return {
            "report_evidence": report_evidence,
            "report_prompt_version": FINAL_REPORT_PROMPT_VERSION,
            "trace": trace("persist_result", success=True),
        }
    except Exception as exc:
        return {
            "report_evidence": report_evidence,
            "report_prompt_version": FINAL_REPORT_PROMPT_VERSION,
            "errors": [f"simulation_run 저장 실패: {exc}"],
            "trace": trace("persist_result", success=False),
        }


def plan_payload(state: PlanningState, plan_version: str) -> dict[str, Any]:
    return {
        "plan_version": plan_version,
        "command_id": state["command"]["command_id"],
        "warehouse_id": state["command"]["warehouse_id"],
        "scope": state["scope"],
        "required_tasks": state["required_tasks"],
        "cuopt_plan": state["cuopt_plan"],
        "collision_plan": state["collision_plan"],
        "inventory_operations": state.get("inventory_operations", []),
        "charger_node_ids": [
            int(row["node_id"])
            for row in state.get("optimization_problem", {}).get("nodes", [])
            if row.get("active", True)
            and str(row.get("node_type") or "").upper() == "CHARGER"
        ],
        "task_dependencies": state.get("interpretation", {}).get(
            "task_dependencies", []
        ),
        "execution_task_dependencies": state.get("cuopt_plan", {}).get(
            "metadata", {}
        ).get("execution_task_dependencies", []),
        "scheduled_task_constraints": state.get("interpretation", {}).get(
            "scheduled_task_constraints", []
        ),
        "ready_task_ids": state.get("ready_task_ids", []),
        "waiting_task_ids": state.get("waiting_task_ids", []),
        "blocked_task_ids": state.get("blocked_task_ids", []),
        "activated_at": datetime.now(UTC).isoformat(),
    }


def execution_precheck_node(state: PlanningState) -> dict[str, Any]:
    settings = get_settings()
    simulation = state.get("simulation", {})
    verification_decision = state.get("verification_decision", {}).get("decision")
    if verification_decision not in {"PASS", "PASS_WITH_WARNING"}:
        message = (
            "Verification Agent가 실행 계획을 승인하지 않아 실행할 수 없습니다."
        )
        return {
            "execution_ready": False,
            "final_status": "EXECUTION_BLOCKED",
            "errors": [message],
            "trace": trace(
                "execution_precheck",
                success=False,
                reason=message,
                verification_decision=verification_decision,
            ),
        }
    if not simulation.get("valid"):
        message = "검증된 시뮬레이션이 없어 실행할 수 없습니다."
        return {
            "execution_ready": False,
            "final_status": "EXECUTION_BLOCKED",
            "errors": [message],
            "trace": trace("execution_precheck", success=False, reason=message),
        }
    if not settings.robot_gateway_url:
        message = "EXECUTE에는 ROBOT_GATEWAY_URL이 필요합니다."
        return {
            "execution_ready": False,
            "final_status": "EXECUTION_BLOCKED",
            "errors": [message],
            "trace": trace("execution_precheck", success=False, reason=message),
        }

    services = get_services()
    approval: dict[str, Any] = {
        "status": "LEGACY_TEST_DOUBLE",
        "plan_version": state.get("plan_version"),
    }
    # Unit-test doubles from earlier phases intentionally do not implement the
    # P16.5.15 delivery tables. Real PostgresRepository instances do, and in
    # that path execution is blocked unless the exact verified plan version is
    # durably approved before activation.
    if hasattr(services.postgres, "approve_execution_plan"):
        try:
            plan_version = str(state.get("plan_version") or "")
            if not plan_version:
                raise RuntimeError("PLAN_VERSION_REQUIRED")
            approval = ExecutionDeliveryService(services).approve_plan(
                plan_version=plan_version,
                command_id=state.get("command", {}).get("command_id"),
                warehouse_id=int(state["command"]["warehouse_id"]),
                verification_decision=str(verification_decision),
                plan_payload=plan_payload(state, plan_version),
                request=PlanExecutionApprovalRequest(
                    warehouse_id=int(state["command"]["warehouse_id"]),
                    actor_id="SYSTEM_VERIFICATION",
                    reason=(
                        "최종 시뮬레이션과 결정론적 검증을 통과한 "
                        f"계획 버전 {plan_version} 실행 승인"
                    ),
                    expected_active_plan_version=state.get("snapshot", {})
                    .get("redis", {})
                    .get("active_plan_version"),
                ),
            )
        except Exception as exc:
            message = f"실행 계획 버전 승인 실패: {exc}"
            return {
                "execution_ready": False,
                "execution_approval": {
                    "status": "FAILED",
                    "error": str(exc),
                },
                "final_status": "EXECUTION_BLOCKED",
                "errors": [message],
                "trace": trace(
                    "execution_plan_approval",
                    success=False,
                    reason=str(exc),
                ),
            }
    return {
        "execution_ready": True,
        "execution_approval": approval,
        "final_status": "EXECUTION_READY",
        "trace": trace(
            "execution_plan_approval",
            success=True,
            approval=approval,
        ) + trace("execution_precheck", success=True),
    }


def dispatch_plan_node(state: PlanningState) -> dict[str, Any]:
    settings = get_settings()
    if not settings.robot_gateway_url:
        message = "EXECUTE에는 ROBOT_GATEWAY_URL이 필요합니다."
        return {
            "final_status": "EXECUTION_BLOCKED",
            "errors": [message],
            "trace": trace("dispatch_plan", success=False, reason=message),
        }
    try:
        plan_version = state.get("plan_version") or str(uuid4())
        previous_active_version = state.get("snapshot", {}).get("redis", {}).get(
            "active_plan_version"
        )
        reservation_release_warnings: list[str] = []

        def release_replaced_plan_reservations() -> None:
            if previous_active_version and previous_active_version != plan_version:
                try:
                    InventoryReservationService(
                        get_services().postgres,
                        get_services().redis,
                    ).release_plan(
                        state["command"]["warehouse_id"],
                        previous_active_version,
                        status="RELEASED",
                    )
                except Exception as release_exc:
                    reservation_release_warnings.append(str(release_exc))

        ready_ids = list(state.get("ready_task_ids", []))
        if not ready_ids:
            release_replaced_plan_reservations()
            return {
                "plan_version": plan_version,
                "dispatch_result": {
                    "accepted": True,
                    "status": "WAITING_FOR_READY_TASK",
                    "plan_version": plan_version,
                    "received_robot_count": 0,
                },
                "final_status": "SCHEDULED",
                "trace": trace(
                    "dispatch_ready_tasks",
                    success=True,
                    ready_task_ids=[],
                    dispatched=False,
                )
                + (
                    trace(
                        "inventory_reservation_release_warning",
                        reason=reservation_release_warnings,
                    )
                    if reservation_release_warnings
                    else []
                ),
            }
        payload = ready_only_plan_payload(
            plan_payload(state, plan_version), ready_ids
        )
        adapter = RobotAdapter(
            time_step_seconds=int(
                state.get("optimization_problem", {}).get("time_step_seconds")
                or settings.time_step_seconds
            )
        )
        batches, adapter_validation = adapter.adapt(plan_version, payload)
        if not adapter_validation["valid"] or not batches:
            raise RuntimeError(
                "ROBOT_ADAPTER_VALIDATION_FAILED: "
                + ", ".join(adapter_validation.get("errors") or ["EMPTY_COMMAND_BATCH"])
            )
        serialized_batches = [batch.model_dump(mode="json") for batch in batches]
        services = get_services()
        durable_delivery = hasattr(
            services.postgres, "create_or_get_execution_dispatch"
        )
        gateway = RobotGateway(
            settings.robot_gateway_url,
            settings.request_timeout_seconds,
            # Durable retries are counted by ExecutionDeliveryService. Keep
            # the transport call single-attempt so one logical attempt cannot
            # fan out into multiple untracked sends.
            max_attempts=(
                1
                if durable_delivery
                else getattr(settings, "robot_gateway_max_attempts", 3)
            ),
            retry_backoff_seconds=getattr(
                settings, "robot_gateway_retry_backoff_seconds", 0.2
            ),
        )
        if durable_delivery:
            delivery_result = ExecutionDeliveryService(
                services, gateway=gateway
            ).dispatch(
                plan_version=plan_version,
                warehouse_id=int(state["command"]["warehouse_id"]),
                command_id=state.get("command", {}).get("command_id"),
                batches=serialized_batches,
                previous_active_plan_version=previous_active_version,
                max_attempts=getattr(
                    settings, "robot_gateway_max_attempts", 3
                ),
            )
            gateway_result = delivery_result
        else:
            # Backward-compatible unit-test path. Production uses the durable
            # execution delivery service above.
            gateway_result = gateway.dispatch(plan_version, serialized_batches)
        gateway_result = {
            **gateway_result,
            "dispatched_robot_count": len(batches),
            "dispatched_command_count": sum(batch.command_count for batch in batches),
            "robot_command_batches": serialized_batches,
            "adapter_validation": adapter_validation,
        }
        if not gateway_result.get("accepted", False):
            return {
                "plan_version": plan_version,
                "dispatch_result": gateway_result,
                "final_status": "DISPATCH_RETRY_PENDING",
                "trace": trace(
                    "dispatch_retry_pending",
                    success=False,
                    dispatch_id=gateway_result.get("dispatch_id"),
                    attempt_count=gateway_result.get("attempt_count"),
                    max_attempts=gateway_result.get("max_attempts"),
                    reason=gateway_result.get("error"),
                ),
            }
        release_replaced_plan_reservations()
        return {
            "plan_version": plan_version,
            "dispatch_result": gateway_result,
            "final_status": "DISPATCHED",
            "trace": trace(
                "dispatch_ready_tasks",
                success=True,
                ready_task_ids=ready_ids,
                gateway=gateway_result,
            )
            + (
                trace(
                    "inventory_reservation_release_warning",
                    reason=reservation_release_warnings,
                )
                if reservation_release_warnings
                else []
            ),
        }
    except Exception as exc:
        rollback_message = ""
        if state.get("plan_version"):
            try:
                rolled_back = get_services().redis.rollback_plan_activation(
                    state["command"]["warehouse_id"],
                    state["plan_version"],
                    state["snapshot"]["redis"].get("active_plan_version"),
                )
                InventoryReservationService(
                    get_services().postgres,
                    get_services().redis,
                ).release_plan(
                    state["command"]["warehouse_id"],
                    state["plan_version"],
                    status="RELEASED",
                )
                rollback_message = f"; Redis 활성화 롤백={rolled_back}"
            except Exception as rollback_exc:
                rollback_message = f"; Redis 롤백 실패={rollback_exc}"
        return {
            "final_status": "DISPATCH_FAILED",
            "errors": [f"로봇 전송 실패: {exc}{rollback_message}"],
            "trace": trace(
                "dispatch_plan",
                success=False,
                reason=f"{exc}{rollback_message}",
            ),
        }


def activate_plan_node(state: PlanningState) -> dict[str, Any]:
    if not state.get("execution_ready"):
        return {
            "final_status": "ACTIVATION_BLOCKED",
            "errors": ["실행 사전검증 통과 없이 계획을 활성화할 수 없습니다."],
            "trace": trace("activate_plan", success=False, reason="precheck missing"),
        }
    plan_activated = False
    inventory_reserved = False
    reservations: list[Any] = []
    plan_version = state.get("plan_version")
    try:
        plan_version = state["plan_version"]
        reservation_service = InventoryReservationService(
            get_services().postgres,
            get_services().redis,
        )
        reservations = reservation_service.reserve_active_plan(
            warehouse_id=int(state["command"]["warehouse_id"]),
            plan_version=plan_version,
            item_results=(
                state.get("inventory_timeline_validation", {}).get("item_results")
                or state.get("inventory_feasibility", {}).get("item_results", [])
            ),
            replace_plan_version=state["snapshot"]["redis"].get(
                "active_plan_version"
            ),
        )
        inventory_reserved = bool(reservations)
        payload = plan_payload(state, plan_version)
        payload["inventory_reservations"] = [
            row.model_dump(mode="json") for row in reservations
        ]
        get_services().redis.atomic_activate_plan(
            state["command"]["warehouse_id"],
            plan_version,
            payload,
            expected_active_version=state["snapshot"]["redis"].get(
                "active_plan_version"
            ),
        )
        plan_activated = True
        repository = get_services().postgres
        if hasattr(repository, "persist_work_schedule"):
            reference_time = state["optimization_problem"]["reference_time"]
            step_seconds = int(state["optimization_problem"]["time_step_seconds"])
            scheduled_rows = []
            for raw in state.get("cuopt_plan", {}).get("scheduled_tasks", []):
                row = dict(raw)
                row["planned_start_at"] = as_utc_datetime(
                    row["planned_start_at"], field_name="planned_start_at"
                ) if row.get("planned_start_at") else planned_at(
                    reference_time,
                    int(row.get("start_time_step") or 0),
                    step_seconds,
                )
                row["planned_end_at"] = as_utc_datetime(
                    row["planned_end_at"], field_name="planned_end_at"
                ) if row.get("planned_end_at") else planned_at(
                    reference_time,
                    int(row.get("end_time_step") or 0),
                    step_seconds,
                )
                scheduled_rows.append(row)
            repository.persist_work_schedule(
                command_id=state["command"]["command_id"],
                plan_version=plan_version,
                dependencies=state.get("interpretation", {}).get(
                    "task_dependencies", []
                ),
                constraints=state.get("interpretation", {}).get(
                    "scheduled_task_constraints", []
                ),
                scheduled_tasks=scheduled_rows,
            )
        return {
            "final_status": "PLAN_ACTIVATED",
            "inventory_reservations": [
                row.model_dump(mode="json") for row in reservations
            ],
            "trace": trace("activate_plan", success=True, plan_version=plan_version)
            + trace(
                "inventory_reservations",
                scope="ACTIVE_PLAN",
                plan_version=plan_version,
                reservation_count=len(reservations),
            )
            + trace(
                "schedule_persisted",
                plan_version=plan_version,
                dependency_count=len(
                    state.get("interpretation", {}).get("task_dependencies", [])
                ),
                constraint_count=len(
                    state.get("interpretation", {}).get(
                        "scheduled_task_constraints", []
                    )
                ),
            ),
        }
    except Exception as exc:
        rollback_message = ""
        if inventory_reserved and plan_version:
            try:
                InventoryReservationService(
                    get_services().postgres,
                    get_services().redis,
                ).release_plan(
                    state["command"]["warehouse_id"],
                    plan_version,
                    status="RELEASED",
                )
            except Exception as reservation_exc:
                rollback_message += f"; 재고 예약 해제 실패={reservation_exc}"
        if plan_activated and plan_version:
            try:
                rolled_back = get_services().redis.rollback_plan_activation(
                    state["command"]["warehouse_id"],
                    plan_version,
                    state["snapshot"]["redis"].get("active_plan_version"),
                )
                rollback_message = f"; Redis 활성화 롤백={rolled_back}"
            except Exception as rollback_exc:
                rollback_message = f"; Redis 롤백 실패={rollback_exc}"
        return {
            "final_status": "ACTIVATION_FAILED",
            "errors": [f"계획 활성화 실패: {exc}{rollback_message}"],
            "trace": trace(
                "activate_plan",
                success=False,
                reason=f"{exc}{rollback_message}",
            ),
        }


def activate_and_dispatch_node(state: PlanningState) -> dict[str, Any]:
    """하위 호환용이며 새 그래프에서는 dispatch와 activate를 분리합니다."""
    activated = activate_plan_node(state)
    if activated.get("final_status") != "PLAN_ACTIVATED":
        return activated
    merged = dict(state)
    merged.update(activated)
    dispatched = dispatch_plan_node(merged)
    return {**activated, **dispatched, "trace": activated["trace"] + dispatched["trace"]}


def query_report(
    interpretation: CommandInterpretation,
    snapshot: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    sql = snapshot["sql"]
    live = snapshot["redis"]
    target = interpretation.query_target
    action = interpretation.query_action

    if target == "ROBOT":
        live_by_id = {str(row.get("robot_id")): row for row in live.get("robots", [])}
        robots: list[dict[str, Any]] = []
        for row in sql.get("robots", []):
            robot_id = str(row["robot_id"])
            live_row = live_by_id.get(robot_id, {})
            robots.append(
                {
                    "robot_id": robot_id,
                    "robot_code": row.get("robot_code"),
                    "status": live_row.get("status") or row.get("status"),
                    "last_event": live_row.get("last_event"),
                    "node_id": live_row.get("node_id") or row.get("node_id"),
                    "battery": live_row.get("battery") or row.get("battery"),
                }
            )
        if interpretation.target_robot_ids:
            requested = set(interpretation.target_robot_ids)
            robots = [
                row
                for row in robots
                if canonical_robot_id(row.get("robot_id")) in requested
            ]
        filters = set(interpretation.query_filters)
        if "AVAILABLE" in filters:
            robots = [
                row
                for row in robots
                if str(row.get("status") or "").upper()
                in {"IDLE", "AVAILABLE", "READY"}
            ]
        if "LOW_BATTERY" in filters:
            threshold = float(get_settings().min_robot_battery)
            robots = [
                row for row in robots if float(row.get("battery") or 0) < threshold
            ]
        if "FAILED_OR_DELAYED" in filters:
            robots = [
                row
                for row in robots
                if str(row.get("status") or row.get("last_event") or "").upper()
                in {"FAILED", "OFFLINE", "MAINTENANCE", "DELAYED", "ROBOT_FAILED", "ROBOT_DELAYED"}
            ]
        executing_task_ids = set(live.get("executing_task_ids", []))
        planned_task_ids = set(live.get("planned_task_ids", []))
        executing_robot_ids = {
            str(row.get("robot_id"))
            for row in live.get("tasks", [])
            if row.get("task_id") in executing_task_ids and row.get("robot_id")
        }
        planned_robot_ids = {
            str(row.get("robot_id"))
            for row in live.get("tasks", [])
            if row.get("task_id") in planned_task_ids and row.get("robot_id")
        }
        for robot in robots:
            status = str(robot.get("status") or "").upper()
            if status in {"EXECUTING", "BUSY", "RUNNING"}:
                executing_robot_ids.add(robot["robot_id"])
        available_count = sum(
            str(robot.get("status") or "").upper() in {"IDLE", "AVAILABLE", "READY"}
            for robot in robots
        )
        waiting_count = max(
            0,
            len(robots) - len(executing_robot_ids | planned_robot_ids),
        )
        settings = get_settings()
        policy_explicitly_configured = "min_robot_battery" in getattr(
            settings, "model_fields_set", set()
        )
        minimum_battery_policy = {
            "status": (
                "CONFIGURED"
                if policy_explicitly_configured
                else "WAREHOUSE_POLICY_NOT_CONFIGURED_USING_SYSTEM_DEFAULT"
            ),
            "minimum_battery_percent": float(settings.min_robot_battery),
            "source": "ENV_OR_DOTENV" if policy_explicitly_configured else "SYSTEM_DEFAULT",
        }
        data = {
            "robot_count": len(robots),
            "available_robot_count": available_count,
            "executing_robot_count": len(executing_robot_ids),
            "planned_robot_count": len(planned_robot_ids),
            "waiting_robot_count": waiting_count,
            "robots": robots,
            "minimum_battery_policy": minimum_battery_policy,
        }
        robot_lines = [
            "- {code}: 상태 {status}, 현재 노드 {node}, 현재 배터리 {battery}".format(
                code=robot.get("robot_code") or robot["robot_id"],
                status=robot.get("status") or "미등록",
                node=(robot.get("node_id") if robot.get("node_id") is not None else "미등록"),
                battery=(
                    f"{robot.get('battery')}%"
                    if robot.get("battery") is not None
                    else "미등록"
                ),
            )
            for robot in robots
        ]
        if policy_explicitly_configured:
            policy_line = (
                "최소 운용 배터리 정책은 "
                f"{float(settings.min_robot_battery):g}%로 설정되어 있습니다."
            )
        else:
            policy_line = (
                "창고별 최소 운용 배터리 정책은 등록되지 않았으며, "
                f"시스템 기본 기준 {float(settings.min_robot_battery):g}%를 사용합니다."
            )
        if action == "COUNT":
            answer = f"현재 창고에 등록된 로봇은 총 {len(robots)}대입니다."
        elif action == "STATUS":
            answer = (
                f"현재 등록된 로봇은 총 {len(robots)}대입니다. "
                f"작업 중인 로봇은 {len(executing_robot_ids)}대, "
                f"계획된 작업이 있는 로봇은 {len(planned_robot_ids)}대, "
                f"대기 상태는 {waiting_count}대이며 사용 가능한 로봇은 {available_count}대입니다."
            )
            if robot_lines:
                answer += "\n" + "\n".join(robot_lines)
            answer += "\n" + policy_line
        else:
            answer = f"현재 등록된 로봇은 총 {len(robots)}대입니다."
            if robot_lines:
                answer += "\n" + "\n".join(robot_lines)
            answer += "\n" + policy_line
        return answer, data

    if target == "INVENTORY":
        requested_item_ids = {str(value) for value in interpretation.item_ids}
        master_items = {
            str(row.get("item_id")): dict(row)
            for row in sql.get("inventory_items", [])
            if row.get("item_id")
        }
        raw_inventory = [dict(row) for row in sql.get("inventory", [])]
        raw_inbound_orders = [dict(row) for row in sql.get("inbound_orders", [])]
        raw_outbound_orders = [dict(row) for row in sql.get("outbound_orders", [])]
        registered_item_ids = (
            set(master_items)
            | {str(row.get("item_id")) for row in raw_inventory if row.get("item_id")}
            | {str(row.get("item_id")) for row in raw_inbound_orders if row.get("item_id")}
            | {str(row.get("item_id")) for row in raw_outbound_orders if row.get("item_id")}
        )
        unregistered_item_ids = sorted(requested_item_ids - registered_item_ids)
        selected_item_ids = (
            requested_item_ids & registered_item_ids
            if requested_item_ids
            else set(registered_item_ids)
        )
        inventory = [
            row for row in raw_inventory
            if str(row.get("item_id")) in selected_item_ids
        ]
        include_inbound_orders = interpretation.load_open_inventory_orders or any(
            marker in interpretation.objective.lower()
            for marker in ("예정 입고", "입고 예정", "inbound")
        ) or (not requested_item_ids and interpretation.query_action == "DETAIL")
        reservations = live.get("inventory_reservations", [])
        reserved_total = sum(int(row.get("quantity_boxes") or 0) for row in reservations)

        item_totals: dict[str, int] = {item_id: 0 for item_id in selected_item_ids}
        item_lot_counts: dict[str, int] = {item_id: 0 for item_id in selected_item_ids}
        for row in inventory:
            item_id = str(row.get("item_id"))
            item_totals[item_id] = item_totals.get(item_id, 0) + int(
                row.get("available_quantity") or 0
            )
            item_lot_counts[item_id] = item_lot_counts.get(item_id, 0) + 1

        quantity_filters = [
            value
            for value in interpretation.query_filters
            if not isinstance(value, str)
            and getattr(value, "field", None) == "available_quantity_boxes"
        ]

        def quantity_matches(quantity: int, operator: str, value: int) -> bool:
            if operator == "LT":
                return quantity < value
            if operator == "LTE":
                return quantity <= value
            if operator == "GT":
                return quantity > value
            if operator == "GTE":
                return quantity >= value
            return True

        for predicate in quantity_filters:
            selected_item_ids = {
                item_id
                for item_id in selected_item_ids
                if quantity_matches(item_totals.get(item_id, 0), str(predicate.operator), int(predicate.value))
            }

        inventory = [
            row for row in inventory if str(row.get("item_id")) in selected_item_ids
        ]
        item_ids = sorted(selected_item_ids)
        item_totals = {item_id: item_totals.get(item_id, 0) for item_id in item_ids}
        item_lot_counts = {item_id: item_lot_counts.get(item_id, 0) for item_id in item_ids}
        total_available = sum(item_totals.values())

        inbound_orders = [
            row for row in raw_inbound_orders
            if include_inbound_orders and str(row.get("item_id")) in selected_item_ids
        ]

        item_summaries = [
            {
                "item_id": item_id,
                "available_quantity_boxes": item_totals[item_id],
                "lot_count": item_lot_counts[item_id],
                "unit": master_items.get(item_id, {}).get("base_unit") or "BOX",
            }
            for item_id in item_ids
        ]
        item_registration_summaries = [
            {
                "item_id": item_id,
                "item_name": master_items.get(item_id, {}).get("item_name"),
                "registered": True,
                "availability_status": (
                    "AVAILABLE" if item_totals[item_id] > 0 else "NO_AVAILABLE_STOCK"
                ),
            }
            for item_id in item_ids
        ]

        def format_timestamp(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value)

        inbound_order_summaries = [
            {
                "inbound_id": row.get("inbound_id"),
                "item_id": row.get("item_id"),
                "quantity_boxes": int(row.get("quantity_boxes") or 0),
                "status": row.get("status"),
                "expected_arrival_at": format_timestamp(row.get("expected_arrival_at")),
                "expected_available_at": format_timestamp(row.get("expected_available_at")),
                "actual_arrival_at": format_timestamp(row.get("actual_arrival_at")),
                "actual_available_at": format_timestamp(row.get("actual_available_at")),
                "storage_node_id": row.get("storage_node_id"),
                "lot_id": row.get("lot_id"),
            }
            for row in inbound_orders
        ]
        inbound_quantity_by_item = {item_id: 0 for item_id in item_ids}
        inbound_count_by_item = {item_id: 0 for item_id in item_ids}
        for row in inbound_order_summaries:
            item_id = str(row.get("item_id"))
            if item_id not in inbound_quantity_by_item:
                continue
            inbound_quantity_by_item[item_id] += int(row.get("quantity_boxes") or 0)
            inbound_count_by_item[item_id] += 1

        item_inventory_status_summaries = [
            {
                "item_id": item_id,
                "available_quantity_boxes": item_totals[item_id],
                "has_scheduled_inbound": inbound_count_by_item[item_id] > 0,
                "scheduled_inbound_quantity_boxes": inbound_quantity_by_item[item_id],
                "inbound_order_count": inbound_count_by_item[item_id],
                "unit": master_items.get(item_id, {}).get("base_unit") or "BOX",
            }
            for item_id in item_ids
        ]
        item_availability_status_summaries = [
            {
                "item_id": item_id,
                "status": (
                    "AVAILABLE"
                    if item_totals[item_id] > 0
                    else "SCHEDULED_INBOUND_ONLY"
                    if inbound_count_by_item[item_id] > 0
                    else "NO_AVAILABLE_OR_SCHEDULED_STOCK"
                ),
            }
            for item_id in item_ids
        ]

        storage_requested = (
            interpretation.target_node_type == "STORAGE"
            or "STORAGE_NODES" in interpretation.required_graph_reads
        )

        def node_is_active(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if value is None:
                return True
            return str(value).strip().lower() in {"true", "1", "yes", "active"}

        storage_node_candidates: list[dict[str, Any]] = []
        if storage_requested:
            storage_node_candidates = [
                {
                    "node_id": row.get("node_id"),
                    "node_type": row.get("node_type"),
                    "zone_id": row.get("zone_id"),
                    "active": node_is_active(row.get("active")),
                    "x": row.get("x"),
                    "y": row.get("y"),
                }
                for row in snapshot.get("graph", {}).get("nodes", [])
                if str(row.get("node_type") or "").upper() == "STORAGE"
                and node_is_active(row.get("active"))
            ]
            storage_node_candidates.sort(key=lambda row: str(row.get("node_id") or ""))

        available_lot_summaries = [
            {
                "item_id": row.get("item_id"),
                "lot_id": row.get("lot_id"),
                "available_quantity_boxes": int(row.get("available_quantity") or 0),
                "storage_node_id": row.get("storage_node_id", row.get("node_id")),
                "status": row.get("status"),
                "available_at": format_timestamp(row.get("available_at")),
            }
            for row in inventory
        ]
        data = {
            "inventory_row_count": len(inventory),
            "item_count": len(item_ids),
            "registered_item_count": len(item_ids),
            "unregistered_item_ids": unregistered_item_ids,
            "total_available_quantity": total_available,
            "item_ids": item_ids,
            "unit": "BOX",
            "item_summaries": item_summaries,
            "item_registration_summaries": item_registration_summaries,
            "item_inventory_status_summaries": item_inventory_status_summaries,
            "item_availability_status_summaries": item_availability_status_summaries,
            "available_lot_summaries": available_lot_summaries,
            "inbound_order_summaries": inbound_order_summaries,
            "storage_node_candidates": storage_node_candidates,
            "active_plan_reserved_quantity_boxes": reserved_total,
            "active_plan_reservations": reservations,
            "open_inbound_order_count": len(inbound_orders),
            "open_outbound_order_count": len(raw_outbound_orders),
        }

        if interpretation.load_open_inventory_orders:
            item_lines = []
            for row in item_inventory_status_summaries:
                current = (
                    f"현재 {row['available_quantity_boxes']} BOX"
                    if row["available_quantity_boxes"] > 0
                    else "현재 가용 재고 없음"
                )
                inbound = (
                    f"예정 입고 있음, {row['scheduled_inbound_quantity_boxes']} BOX"
                    if row["has_scheduled_inbound"]
                    else "예정 입고 없음, 0 BOX"
                )
                item_lines.append(f"- {row['item_id']}: {current} / {inbound}")
        else:
            item_lines = [
                (
                    f"- {row['item_id']}: {row['available_quantity_boxes']} BOX"
                    if row["available_quantity_boxes"] > 0
                    else f"- {row['item_id']}: 등록됨 / 현재 가용 재고 없음"
                )
                for row in item_summaries
            ]
        lot_lines = [
            f"- {row.get('item_id')} / LOT {row.get('lot_id') or '미지정'}: "
            f"{row.get('available_quantity_boxes')} BOX, 저장 노드 {row.get('storage_node_id')}"
            for row in available_lot_summaries
        ]
        inbound_lines = [
            f"- {row.get('item_id')}: {row.get('quantity_boxes')} BOX, "
            f"도착 예정 {row.get('expected_arrival_at') or '미정'}, "
            f"사용 가능 예정 {row.get('expected_available_at') or '미정'}"
            for row in inbound_order_summaries
        ]

        if requested_item_ids and not item_ids:
            answer = (
                "요청한 품목이 현재 시스템에 등록되어 있지 않습니다: "
                + ", ".join(sorted(requested_item_ids))
            )
        else:
            answer = (
                f"요청한 등록 품목은 {len(item_ids)}종이며 현재 가용 재고는 총 {total_available} BOX입니다."
                if requested_item_ids
                else f"현재 등록된 품목은 {len(item_ids)}종이며 SQL 가용 재고는 총 {total_available} BOX, 활성 계획 예약은 {reserved_total} BOX입니다."
            )
            if item_lines:
                answer += "\n" + "\n".join(item_lines)
        if unregistered_item_ids:
            answer += "\n미등록 품목: " + ", ".join(unregistered_item_ids)
        if (interpretation.query_action == "DETAIL" or requested_item_ids) and lot_lines:
            answer += "\n현재 가용 LOT:\n" + "\n".join(lot_lines)
        if inbound_lines:
            answer += f"\n예정 입고 주문은 {len(inbound_order_summaries)}건입니다.\n" + "\n".join(inbound_lines)
        elif include_inbound_orders:
            answer += "\n예정 입고 주문은 없습니다."
        if storage_requested:
            if storage_node_candidates:
                preview_candidates = storage_node_candidates[:5]
                answer += (
                    f"\n활성 STORAGE 노드 후보는 {len(storage_node_candidates)}개입니다.\n"
                    + "\n".join(
                        f"- 노드 {row.get('node_id')} / 구역 {row.get('zone_id') or '미지정'}"
                        for row in preview_candidates
                    )
                )
            else:
                answer += "\n활성 STORAGE 노드 후보가 없습니다."
        return answer, data

    if target == "WORK":
        works = sql.get("works", [])
        if interpretation.target_task_ids:
            requested = set(interpretation.target_task_ids)
            works = [
                row
                for row in works
                if canonical_task_id(row.get("work_id") or row.get("task_id"))
                in requested
            ]
        filters = set(interpretation.query_filters)
        if filters.intersection({"EXECUTING", "PLANNED", "DELAYED"}):
            allowed = filters.intersection({"EXECUTING", "PLANNED", "DELAYED"})
            works = [
                row
                for row in works
                if str(row.get("status") or "").upper() in allowed
            ]
        if "UNASSIGNED" in filters:
            works = [row for row in works if not row.get("assigned_robot_id")]
        status_counts: dict[str, int] = {}
        for work in works:
            status = str(work.get("status") or "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1
        unassigned = sum(not work.get("assigned_robot_id") for work in works)
        data = {
            "work_count": len(works),
            "status_counts": status_counts,
            "unassigned_work_count": unassigned,
        }
        return (
            f"현재 미완료 작업은 총 {len(works)}건이며 배정되지 않은 작업은 {unassigned}건입니다.",
            data,
        )

    if target == "MAP":
        graph = snapshot["graph"]
        closures = live.get("temporary_closures", [])
        data = {
            "node_count": len(graph.get("nodes", [])),
            "edge_count": len(graph.get("edges", [])),
            "temporary_closure_count": len(closures),
        }
        return (
            f"현재 지도에는 노드 {data['node_count']}개와 연결 통로 {data['edge_count']}개가 있으며 "
            f"임시 폐쇄 항목은 {data['temporary_closure_count']}건입니다.",
            data,
        )

    if target == "PLAN":
        active_plan = live.get("active_plan")
        data = {
            "active_plan_version": live.get("active_plan_version"),
            "active_plan": active_plan,
        }
        if data["active_plan_version"]:
            return (
                f"현재 활성 계획 버전은 {data['active_plan_version']}입니다.",
                data,
            )
        return "현재 활성 계획이 없습니다.", data

    if target in {"SIMULATION", "REPLAN", "VERIFICATION", "RESET", "EVIDENCE"}:
        repository = get_services().postgres
        warehouse_id = int(snapshot["warehouse_id"])
        try:
            if target == "SIMULATION" and hasattr(repository, "list_simulation_sessions"):
                if (
                    interpretation.target_simulation_ids
                    and hasattr(repository, "get_simulation_session")
                ):
                    rows = []
                    for simulation_id in interpretation.target_simulation_ids:
                        row = repository.get_simulation_session(simulation_id)
                        if row and int(row.get("warehouse_id")) == warehouse_id:
                            rows.append(row)
                    return (
                        f"요청한 시뮬레이션 중 {len(rows)}건을 확인했습니다.",
                        {"simulations": rows},
                    )
                rows = repository.list_simulation_sessions(
                    warehouse_id=warehouse_id,
                    limit=20,
                    offset=0,
                )
                return f"확인된 시뮬레이션 이력은 {len(rows)}건입니다.", {"simulations": rows}
            if target == "RESET" and hasattr(repository, "list_simulation_reset_audits"):
                rows = repository.list_simulation_reset_audits(
                    warehouse_id=warehouse_id,
                    limit=20,
                    offset=0,
                )
                return f"확인된 초기화 이력은 {len(rows)}건입니다.", {"reset_history": rows}
            if target in {"REPLAN", "VERIFICATION", "EVIDENCE"} and hasattr(repository, "list_command_history"):
                commands = repository.list_command_history(
                    warehouse_id=warehouse_id,
                    limit=20,
                    offset=0,
                )
                key = "replan" if target == "REPLAN" else "verification" if target == "VERIFICATION" else "evidence"
                rows = [
                    {
                        "command_id": row.get("command_id"),
                        key: (row.get("result_summary") or {}).get(key),
                    }
                    for row in commands
                    if (row.get("result_summary") or {}).get(key)
                ]
                return f"실제 저장된 {target} 결과는 {len(rows)}건입니다.", {f"{key}_history": rows}
        except Exception as exc:
            return "요청한 이력을 조회하지 못했습니다.", {"error": str(exc)}
        return "현재 저장소에서 요청한 이력을 확인할 수 없습니다.", {}

    data = compact_snapshot(snapshot)
    return "현재 창고 운영 Snapshot을 조회했습니다.", data


def scheduled_assignment_time(
    assignment: dict[str, Any],
    *,
    field: str,
    time_step_field: str,
    fallback_reference_time: Any,
    time_step_seconds: int,
) -> datetime | None:
    explicit = assignment.get(field)
    if explicit is not None:
        return as_utc_datetime(explicit, field_name=field)
    if fallback_reference_time is None:
        return None
    return planned_at(
        fallback_reference_time,
        int(assignment.get(time_step_field) or 0),
        time_step_seconds,
    )


def same_absolute_schedule(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    before_reference_time: Any,
    after_reference_time: Any,
    time_step_seconds: int,
) -> bool:
    before_window = (
        scheduled_assignment_time(
            before,
            field="planned_start_at",
            time_step_field="start_time_step",
            fallback_reference_time=before_reference_time,
            time_step_seconds=time_step_seconds,
        ),
        scheduled_assignment_time(
            before,
            field="planned_end_at",
            time_step_field="end_time_step",
            fallback_reference_time=before_reference_time,
            time_step_seconds=time_step_seconds,
        ),
    )
    after_window = (
        scheduled_assignment_time(
            after,
            field="planned_start_at",
            time_step_field="start_time_step",
            fallback_reference_time=after_reference_time,
            time_step_seconds=time_step_seconds,
        ),
        scheduled_assignment_time(
            after,
            field="planned_end_at",
            time_step_field="end_time_step",
            fallback_reference_time=after_reference_time,
            time_step_seconds=time_step_seconds,
        ),
    )
    return before.get("robot_id") == after.get("robot_id") and before_window == after_window


def planning_report_data(state: PlanningState) -> dict[str, Any]:
    simulation = state.get("simulation", {})
    plan_validation = state.get("plan_validation", {})
    metrics_source = simulation or plan_validation
    assignments = (
        metrics_source.get("task_assignments")
        or state.get("cuopt_plan", {}).get("scheduled_tasks", [])
    )
    routes = (
        metrics_source.get("robot_routes")
        or state.get("collision_plan", {}).get("routes", [])
    )
    ordered_by_robot: dict[str, list[str]] = {}
    for assignment in sorted(
        assignments,
        key=lambda row: (
            int(row.get("start_time_step") or 0),
            str(row.get("robot_id")),
            str(row.get("task_id")),
        ),
    ):
        ordered_by_robot.setdefault(str(assignment.get("robot_id")), []).append(
            str(assignment.get("task_id"))
        )
    errors = list(state.get("errors", []))
    errors.extend(metrics_source.get("errors", []))
    validation = state.get("validation", {})
    errors.extend(validation.get("errors", []))
    warnings = list(state.get("warnings", []))
    warnings.extend(metrics_source.get("warnings", []))
    time_step_seconds = int(
        state.get("collision_plan", {}).get("time_step_seconds")
        or state.get("optimization_problem", {}).get("time_step_seconds")
        or getattr(get_settings(), "time_step_seconds", 5)
    )
    makespan_steps = int(metrics_source.get("makespan") or 0)
    verification = state.get("verification_decision", {})
    verification_decision = verification.get("decision")
    verification_passed = verification_decision in {
        None,
        "PASS",
        "PASS_WITH_WARNING",
    }
    warnings.extend(verification.get("user_visible_warnings", []))
    optimization_problem = state.get("optimization_problem", {})
    reference_time = optimization_problem.get("reference_time")
    timezone, timezone_name, _ = resolve_warehouse_timezone(
        getattr(get_settings(), "warehouse_timezone", "")
    )
    daily_schedule: list[dict[str, Any]] = []

    for assignment in sorted(
        assignments,
        key=lambda row: (
            int(row.get("start_time_step") or 0),
            str(row.get("task_id")),
        ),
    ):
        start_at = scheduled_assignment_time(
            assignment,
            field="planned_start_at",
            time_step_field="start_time_step",
            fallback_reference_time=reference_time,
            time_step_seconds=time_step_seconds,
        )
        end_at = scheduled_assignment_time(
            assignment,
            field="planned_end_at",
            time_step_field="end_time_step",
            fallback_reference_time=reference_time,
            time_step_seconds=time_step_seconds,
        )
        daily_schedule.append(
            {
                "task_id": assignment.get("task_id"),
                "work_id": assignment.get("work_id"),
                "robot_id": assignment.get("robot_id"),
                "start_time_step": assignment.get("start_time_step"),
                "end_time_step": assignment.get("end_time_step"),
                "planned_start_at": start_at.isoformat() if start_at else None,
                "planned_end_at": end_at.isoformat() if end_at else None,
                "local_start_at": (
                    start_at.astimezone(timezone).isoformat() if start_at else None
                ),
                "local_end_at": (
                    end_at.astimezone(timezone).isoformat() if end_at else None
                ),
                "timezone": timezone_name,
                "schedule_status": assignment.get("schedule_status"),
            }
        )

    active_plan_for_report = (
        state.get("replan_base_plan")
        or state.get("snapshot", {}).get("redis", {}).get("active_plan")
        or {}
    )
    previous_schedule = {
        str(row.get("task_id")): row
        for row in active_plan_for_report.get("cuopt_plan", {}).get(
            "scheduled_tasks", []
        )
        if row.get("task_id")
    }
    current_schedule = {
        str(row.get("task_id")): row for row in assignments if row.get("task_id")
    }
    inserted = sorted(set(current_schedule) - set(previous_schedule))
    shifted = []
    previous_reference_time = active_plan_for_report.get(
        "reference_time"
    ) or active_plan_for_report.get("activated_at")

    def schedule_window(
        row: dict[str, Any], fallback_reference_time: Any
    ) -> tuple[datetime | None, datetime | None]:
        return (
            scheduled_assignment_time(
                row,
                field="planned_start_at",
                time_step_field="start_time_step",
                fallback_reference_time=fallback_reference_time,
                time_step_seconds=time_step_seconds,
            ),
            scheduled_assignment_time(
                row,
                field="planned_end_at",
                time_step_field="end_time_step",
                fallback_reference_time=fallback_reference_time,
                time_step_seconds=time_step_seconds,
            ),
        )

    preserved: list[str] = []
    for task_id in sorted(set(current_schedule) & set(previous_schedule)):
        before = previous_schedule[task_id]
        after = current_schedule[task_id]
        if same_absolute_schedule(
            before,
            after,
            before_reference_time=previous_reference_time,
            after_reference_time=reference_time,
            time_step_seconds=time_step_seconds,
        ):
            preserved.append(task_id)

    for task_id in sorted(set(current_schedule) & set(previous_schedule)):
        before = previous_schedule[task_id]
        after = current_schedule[task_id]
        if task_id in preserved:
            continue
        previous_start_at, previous_end_at = schedule_window(
            before, previous_reference_time
        )
        revised_start_at, revised_end_at = schedule_window(after, reference_time)
        delay_seconds = (
            (revised_start_at - previous_start_at).total_seconds()
            if revised_start_at is not None and previous_start_at is not None
            else 0.0
        )
        shifted.append(
            {
                "task_id": task_id,
                "previous_start_time_step": before.get("start_time_step"),
                "previous_end_time_step": before.get("end_time_step"),
                "revised_start_time_step": after.get("start_time_step"),
                "revised_end_time_step": after.get("end_time_step"),
                "previous_start_at": (
                    previous_start_at.isoformat() if previous_start_at else None
                ),
                "previous_end_at": (
                    previous_end_at.isoformat() if previous_end_at else None
                ),
                "revised_start_at": (
                    revised_start_at.isoformat() if revised_start_at else None
                ),
                "revised_end_at": (
                    revised_end_at.isoformat() if revised_end_at else None
                ),
                "delay_seconds": delay_seconds,
                "previous_robot_id": before.get("robot_id"),
                "revised_robot_id": after.get("robot_id"),
            }
        )
    schedule_impact = {
        "inserted_task_ids": inserted,
        "preserved_task_ids": preserved,
        "frozen_task_ids": state.get("scope", {}).get("fixed_task_ids", []),
        "shifted_task_ids": [row["task_id"] for row in shifted],
        "blocked_task_ids": state.get("blocked_task_ids", []),
        "affected_robot_ids": sorted(
            {
                str(row.get("robot_id"))
                for row in assignments
                if row.get("task_id") in inserted
                or row.get("task_id") in {value["task_id"] for value in shifted}
            }
        ),
        "previous_plan_version": state.get("base_plan_version")
        or state.get("snapshot", {}).get("redis", {}).get(
            "active_plan_version"
        ),
        "new_plan_version": state.get("plan_version"),
        "changes": shifted,
        "replan_scope": state.get("scope", {}).get("plan_mode"),
        "insertion_reason": state.get("interpretation", {}).get(
            "insertion_policy"
        ),
        "deadline_violation": float(metrics_source.get("tardiness") or 0.0) > 0,
        "hard_window_violation": any(
            issue.get("code") == "HARD_WINDOW_VIOLATION"
            for issue in metrics_source.get("issues", [])
        ),
    }
    task_duration_steps = sum(
        max(
            0,
            int(row.get("end_time_step") or 0)
            - int(row.get("start_time_step") or 0),
        )
        for row in assignments
    )
    schedule_completion_at = (
        planned_at(reference_time, makespan_steps, time_step_seconds).isoformat()
        if reference_time
        else None
    )
    dispatch_result = state.get("dispatch_result", {})
    robot_command_batches = dispatch_result.get(
        "robot_command_batches",
        state.get("robot_command_batches", []),
    )
    adapter_validation = dispatch_result.get(
        "adapter_validation",
        state.get("adapter_validation", {}),
    )
    return {
        "execution_mode": state.get("interpretation", {}).get("execution_mode"),
        "plan_mode": state.get("scope", {}).get("plan_mode"),
        "base_plan_source": state.get("base_plan_source"),
        "base_plan_version": state.get("base_plan_version"),
        "active_plan_version": state.get("active_plan_version"),
        "base_plan_is_simulated": state.get("base_plan_is_simulated", False),
        "previous_successful_candidate": state.get(
            "previous_successful_candidate", {}
        ),
        "valid": (
            bool(metrics_source.get("valid"))
            and not errors
            and verification_passed
        ),
        "verification_decision": verification_decision,
        "verification_summary": verification.get("summary"),
        "verification_replan_scope": verification.get("replan_scope"),
        "optimization_profile": optimization_problem.get(
            "optimization_profile", "DEFAULT"
        ),
        "optimization_weight_source": optimization_problem.get(
            "optimization_weight_source", "DEFAULT"
        ),
        "optimization_weights": optimization_problem.get("weights", {}),
        "operational_objective": state.get("operational_objective", {}),
        "route_failure": state.get("route_failure", {}),
        "mapf_replan_policy": state.get("mapf_replan_policy", {}),
        "replan_history": state.get("replan_history", []),
        "optimizer_execution": state.get(
            "optimizer_execution",
            state.get("cuopt_plan", {}).get("metadata", {}).get(
                "optimizer_execution", {}
            ),
        ),
        "optimizer_postprocessing": {
            "cuopt_assignment_application": state.get("cuopt_plan", {})
            .get("metadata", {})
            .get("cuopt_assignment_application", {}),
            "parallel_robot_rebalance": state.get("cuopt_plan", {})
            .get("metadata", {})
            .get("parallel_robot_rebalance", {}),
        },
        "task_count": len(assignments),
        "robot_count": len(ordered_by_robot),
        "task_assignments": assignments,
        "daily_schedule": daily_schedule,
        "scheduled_tasks": assignments,
        "task_dependencies": state.get("interpretation", {}).get(
            "task_dependencies", []
        ),
        "execution_task_dependencies": state.get("cuopt_plan", {}).get(
            "metadata", {}
        ).get("execution_task_dependencies", []),
        "ready_task_ids": state.get("ready_task_ids", []),
        "waiting_task_ids": state.get("waiting_task_ids", []),
        "blocked_task_ids": state.get("blocked_task_ids", []),
        "timeline": metrics_source.get("timeline", daily_schedule),
        "insertion_result": schedule_impact,
        "plan_changes": shifted,
        "schedule_validation": state.get("schedule_validation", {}),
        "robot_task_order": ordered_by_robot,
        "robot_command_batches": robot_command_batches,
        "adapter_validation": adapter_validation,
        "dispatched_robot_count": int(
            dispatch_result.get(
                "dispatched_robot_count", state.get("dispatched_robot_count", 0)
            )
        ),
        "dispatched_command_count": int(
            dispatch_result.get(
                "dispatched_command_count",
                state.get("dispatched_command_count", 0),
            )
        ),
        "gateway_dispatched": bool(
            dispatch_result.get(
                "accepted", state.get("gateway_dispatched", False)
            )
        ),
        "execution_approval": state.get("execution_approval", {}),
        "execution_dispatch": {
            key: dispatch_result.get(key)
            for key in (
                "dispatch_id",
                "status",
                "duplicate",
                "attempt_count",
                "max_attempts",
                "ack_policy",
            )
            if dispatch_result.get(key) is not None
        },
        "robot_routes": [
            {
                "robot_id": route.get("robot_id"),
                "task_ids": route.get("task_ids", []),
                "distance": route.get("distance", 0),
                "waypoint_count": len(route.get("waypoints", [])),
            }
            for route in routes
        ],
        "congestion_avoidance": state.get("collision_plan", {})
        .get("metadata", {})
        .get("congestion_avoidance", {}),
        "total_distance": float(metrics_source.get("total_distance") or 0.0),
        "makespan": makespan_steps,
        "makespan_seconds": makespan_steps * time_step_seconds,
        "schedule_completion_at": schedule_completion_at,
        "active_work_duration_seconds": task_duration_steps * time_step_seconds,
        "elapsed_until_completion_seconds": makespan_steps * time_step_seconds,
        "tardiness": float(metrics_source.get("tardiness") or 0.0),
        "conflict_count": int(metrics_source.get("conflict_count") or 0),
        "battery_by_robot": metrics_source.get("metrics", {}).get(
            "battery_by_robot", {}
        ),
        "route_energy_reconciliation": state.get(
            "route_energy_reconciliation", {}
        ),
        "idle_energy_planning": state.get("idle_energy_planning", {}),
        "resource_reservation_plan": state.get(
            "resource_reservation_plan", {}
        ),
        "opportunity_charging": state.get("cuopt_plan", {})
        .get("metadata", {})
        .get("opportunity_charging", {}),
        "idle_return_policy": state.get("cuopt_plan", {})
        .get("metadata", {})
        .get("idle_return_policy", {}),
        "idle_energy_policy": state.get("collision_plan", {})
        .get("metadata", {})
        .get("idle_energy_policy", {}),
        "idle_relocations": state.get("collision_plan", {})
        .get("metadata", {})
        .get("idle_relocations", []),
        "idle_action_tasks": state.get("collision_plan", {})
        .get("metadata", {})
        .get("idle_action_tasks", []),
        "charge_tasks": [
            row
            for row in assignments
            if str(row.get("action") or "").upper() == "CHARGE"
        ],
        "charger_selections": state.get("cuopt_plan", {})
        .get("metadata", {})
        .get("charger_selections", []),
        "robot_state_overrides": optimization_problem.get(
            "robot_state_overrides", []
        ),
        "inventory_operations": state.get("inventory_operations", []),
        "inventory_feasibility": state.get("inventory_feasibility", {}),
        "inventory_timeline_validation": state.get(
            "inventory_timeline_validation", {}
        ),
        "inventory_projection": state.get("inventory_projection", []),
        "inventory_reservations": state.get("inventory_reservations", []),
        "capacity_feasibility": state.get("capacity_feasibility", {}),
        "emergency_review_items": state.get("emergency_review_items", []),
        "inventory_unknown_item_ids": state.get("inventory_unknown_item_ids", []),
        "inventory_item_candidates": state.get("inventory_item_candidates", {}),
        "errors": list(dict.fromkeys(str(error) for error in errors if error)),
        "warnings": list(dict.fromkeys(str(warning) for warning in warnings if warning)),
    }


def generate_final_report_node(state: PlanningState) -> dict[str, Any]:
    state_fingerprint_before = report_state_fingerprint(state)
    interpretation = CommandInterpretation.model_validate(state["interpretation"])
    report_warnings: list[str] = []
    report_source = "deterministic_template"
    report_evidence = state.get("report_evidence") or build_report_evidence(state)
    fallback_trace: list[dict[str, Any]] = []
    llm_report_allowed = False
    primary_message: str | None = None
    if state.get("clarification"):
        clarification = state["clarification"]
        primary_message = clarification["question"]
        data = {
            "clarification_id": clarification["clarification_id"],
            "reason_code": clarification["reason_code"],
            "options": clarification.get("options", []),
        }
        message = "추가 정보가 필요하여 안전하게 실행을 중단했습니다."
        status = "CLARIFICATION_REQUIRED"
        report_source = "deterministic_clarification_template"
    elif interpretation.intent == "SCENARIO_COMPARISON":
        primary_message = (
            "비교 의도와 기준은 확인했지만 실제 다중 시나리오 비교는 "
            "PHASE 11 범위이므로 실행하지 않았습니다."
        )
        data = {
            "comparison_requested": True,
            "comparison_dimensions": interpretation.comparison_dimensions,
            "requires_future_feature": True,
        }
        message = "비교 요청을 분류했으며 운영 상태는 변경하지 않았습니다."
        status = "FUTURE_FEATURE_REQUIRED"
        report_source = "deterministic_comparison_template"
    elif interpretation.command_kind == "QUERY" and state.get("snapshot"):
        primary_message, data = query_report(interpretation, state["snapshot"])
        message = "조회가 완료되었습니다."
        status = "COMPLETED" if not state.get("errors") else state.get("final_status")
        report_source = "deterministic_query_template"
    else:
        data = planning_report_data(state)
        message = "처리 결과 보고서를 생성했습니다."
        status = state.get("final_status")
        llm_report_allowed = True

    report_level = determine_report_detail_level(state, data)
    user_report_summary = build_user_report_summary(
        state,
        data,
        report_level=report_level,
        primary_message=primary_message,
    )
    debug_payload = compress_debug_payload_for_presentation(
        build_debug_report_payload(
            state,
            user_report_summary,
            report_evidence,
        )
    )
    answer = render_user_report(
        user_report_summary,
        debug_payload=debug_payload,
    )
    report_llm_payload = report_payload_for_level(
        user_report_summary,
        debug_payload=compact_debug_payload_for_llm(debug_payload),
    )

    settings = get_settings()
    if (
        llm_report_allowed
        and getattr(settings, "report_with_llm", True)
        and getattr(settings, "openai_api_key", "")
    ):
        try:
            structured = build_supervisor_llm().with_structured_output(
                FinalReportOutput,
                method="json_schema",
            )
            report = structured.invoke(
                [
                    SystemMessage(content=FINAL_REPORT_PROMPT),
                    HumanMessage(
                        content=json.dumps(
                            report_llm_payload,
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                ]
            )
            if not llm_report_is_supported(report.answer, user_report_summary):
                raise ValueError(
                    "LLM 보고서가 결정론적 summary의 핵심 사실을 보존하지 않았습니다."
                )
            answer = report.answer
            report_source = "llm"
        except Exception as exc:
            report_warnings.append(
                f"LLM 보고서 생성 실패로 템플릿을 사용했습니다: {exc}"
            )
            fallback_trace = trace(
                "report_template_fallback_used",
                reason=str(exc),
                prompt_version=FINAL_REPORT_PROMPT_VERSION,
                report_detail_level=report_level.value,
            )

    evidence_summary = report_evidence_summary(report_evidence)
    evidence_trace = (
        trace(
            "evidence_report_generated",
            source=report_source,
            prompt_version=FINAL_REPORT_PROMPT_VERSION,
            **evidence_summary,
        )
        if interpretation.command_kind != "QUERY"
        and any(evidence_summary.values())
        else []
    )
    report_trace = (
        fallback_trace
        + evidence_trace
        + trace(
            "generate_final_report",
            success=not bool(state.get("errors")),
            intent=interpretation.intent,
            report_mode=report_source,
            report_detail_level=report_level.value,
            planning_state_unchanged=(
                state_fingerprint_before == report_state_fingerprint(state)
            ),
        )
        + (
            trace(
                "urgent_task_inserted",
                inserted_task_ids=data.get("insertion_result", {}).get(
                    "inserted_task_ids", []
                ),
            )
            + trace(
                "schedule_impact_created",
                shifted_task_ids=data.get("insertion_result", {}).get(
                    "shifted_task_ids", []
                ),
            )
            if interpretation.insertion_policy == "URGENT"
            and isinstance(data, dict)
            else []
        )
    )
    user_warnings = list(
        dict.fromkeys(str(value) for value in state.get("warnings", []) if value)
    )
    response = {
        "status": status,
        "message": message,
        "answer": answer,
        "intent": interpretation.intent,
        "interpretation": interpretation.model_dump(mode="json"),
        "supervisor_decision": state.get("supervisor_decision", {}),
        "supervisor_source": state.get("supervisor_source"),
        "supervisor_prompt_version": state.get("supervisor_prompt_version"),
        "supervisor_warnings": state.get("supervisor_warnings", []),
        "verification_decision": state.get("verification_decision", {}),
        "verification_source": state.get("verification_source"),
        "verification_prompt_version": state.get("verification_prompt_version"),
        "verification_warnings": state.get("verification_warnings", []),
        "verification_evidence": state.get("verification_evidence", []),
        "previous_successful_candidate": state.get(
            "previous_successful_candidate", {}
        ),
        "replan_attempt": state.get("replan_attempt", 0),
        "max_replan_attempts": state.get("max_replan_attempts", 0),
        "replan_history": state.get("replan_history", []),
        "last_verification_decision": state.get(
            "last_verification_decision", {}
        ),
        "repeated_failure_signatures": state.get(
            "repeated_failure_signatures", {}
        ),
        "replan_reason": state.get("replan_reason"),
        "route_failure": state.get("route_failure", {}),
        "mapf_replan_policy": state.get("mapf_replan_policy", {}),
        "original_plan_version": state.get("original_plan_version"),
        "current_plan_version": state.get("current_plan_version"),
        "base_plan_source": state.get("base_plan_source"),
        "base_plan_version": state.get("base_plan_version"),
        "active_plan_version": state.get("active_plan_version"),
        "base_plan_is_simulated": state.get("base_plan_is_simulated", False),
        "data": data,
        "robot_command_batches": data.get("robot_command_batches", [])
        if isinstance(data, dict)
        else [],
        "adapter_validation": data.get("adapter_validation", {})
        if isinstance(data, dict)
        else {},
        "dispatched_robot_count": data.get("dispatched_robot_count", 0)
        if isinstance(data, dict)
        else 0,
        "dispatched_command_count": data.get("dispatched_command_count", 0)
        if isinstance(data, dict)
        else 0,
        "gateway_dispatched": data.get("gateway_dispatched", False)
        if isinstance(data, dict)
        else False,
        "execution_approval": state.get("execution_approval", {}),
        "execution_dispatch": data.get("execution_dispatch", {})
        if isinstance(data, dict)
        else {},
        "command_id": state["command"]["command_id"],
        "conversation_id": state["command"].get("conversation_id"),
        "parent_command_id": state["command"].get("parent_command_id"),
        "clarification": state.get("clarification"),
        "plan_version": state.get("plan_version"),
        "simulation_id": state.get("simulation_id"),
        "plan_mode": state.get("scope", {}).get("plan_mode"),
        "optimization_profile": state.get("optimization_problem", {}).get(
            "optimization_profile"
        ),
        "optimization_weight_source": state.get("optimization_problem", {}).get(
            "optimization_weight_source"
        ),
        "optimization_weights": state.get("optimization_problem", {}).get(
            "weights", {}
        ),
        "optimizer_execution": state.get(
            "optimizer_execution",
            state.get("cuopt_plan", {}).get("metadata", {}).get(
                "optimizer_execution", {}
            ),
        ),
        "simulation": state.get("simulation", {}),
        "plan_validation": state.get("plan_validation", {}),
        "optimization_plan": state.get("cuopt_plan", {}),
        "daily_schedule": data.get("daily_schedule", [])
        if isinstance(data, dict)
        else [],
        "task_dependencies": data.get("task_dependencies", [])
        if isinstance(data, dict)
        else [],
        "execution_task_dependencies": data.get(
            "execution_task_dependencies", []
        )
        if isinstance(data, dict)
        else [],
        "ready_task_ids": state.get("ready_task_ids", []),
        "waiting_task_ids": state.get("waiting_task_ids", []),
        "blocked_task_ids": state.get("blocked_task_ids", []),
        "insertion_result": data.get("insertion_result", {})
        if isinstance(data, dict)
        else {},
        "plan_changes": data.get("plan_changes", [])
        if isinstance(data, dict)
        else [],
        "schedule_validation": state.get("schedule_validation", {}),
        "route_energy_reconciliation": state.get(
            "route_energy_reconciliation", {}
        ),
        "idle_energy_planning": state.get("idle_energy_planning", {}),
        "resource_reservation_plan": state.get(
            "resource_reservation_plan", {}
        ),
        "opportunity_charging": state.get("cuopt_plan", {})
        .get("metadata", {})
        .get("opportunity_charging", {}),
        "idle_return_policy": state.get("cuopt_plan", {})
        .get("metadata", {})
        .get("idle_return_policy", {}),
        "idle_energy_policy": state.get("collision_plan", {})
        .get("metadata", {})
        .get("idle_energy_policy", {}),
        "inventory_operations": state.get("inventory_operations", []),
        "inventory_feasibility": state.get("inventory_feasibility", {}),
        "inventory_timeline_validation": state.get(
            "inventory_timeline_validation", {}
        ),
        "inventory_projection": state.get("inventory_projection", []),
        "inventory_reservations": state.get("inventory_reservations", []),
        "capacity_feasibility": state.get("capacity_feasibility", {}),
        "emergency_review_items": state.get("emergency_review_items", []),
        "inventory_unknown_item_ids": state.get("inventory_unknown_item_ids", []),
        "inventory_item_candidates": state.get("inventory_item_candidates", {}),
        "collision_plan": state.get("collision_plan", {}),
        "validation": state.get("validation", {}),
        "snapshot_summary": (
            compact_snapshot(state["snapshot"]) if state.get("snapshot") else None
        ),
        "evidence_summary": evidence_summary,
        "report_detail_level": report_level.value,
        "user_report_summary": user_report_summary.model_dump(mode="json"),
        "report_source": report_source,
        "report_prompt_version": FINAL_REPORT_PROMPT_VERSION,
        "report_generation_warnings": report_warnings,
        "errors": state.get("errors", []),
        "warnings": user_warnings,
        "trace": state.get("trace", []) + report_trace,
    }
    return {
        "answer": answer,
        "report_data": data,
        "report_evidence": report_evidence,
        "report_detail_level": report_level.value,
        "user_report_summary": user_report_summary.model_dump(mode="json"),
        "report_source": report_source,
        "report_prompt_version": FINAL_REPORT_PROMPT_VERSION,
        "report_generation_warnings": report_warnings,
        "response": response,
        "trace": report_trace,
    }


# 기존 import 경로를 사용하는 코드와의 하위 호환성입니다.
def audit_finalizer_node(state: PlanningState) -> dict[str, Any]:
    response = dict(state.get("response", {}))
    prior_warnings = list(state.get("audit_warnings", []))
    new_warnings: list[str] = []
    try:
        AuditService(get_services().postgres).finalize_command_audit(state)
    except Exception as exc:
        message = f"명령 감사 이력 최종 저장 실패: {sanitize_log_details(str(exc))}"
        logger.warning(message)
        new_warnings.append(message)
    all_audit_warnings = list(dict.fromkeys(prior_warnings + new_warnings))
    if all_audit_warnings:
        response["audit_warnings"] = all_audit_warnings
    return {
        "response": response,
        "audit_warnings": new_warnings,
    }


response_node = generate_final_report_node

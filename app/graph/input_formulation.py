"""Input normalization and formulation-route supervisor nodes."""
from __future__ import annotations

import hashlib
import re

from app.core.config import get_settings
from app.core.llm_gateway import get_default_llm_gateway
from app.core.node_observability import observe_node
from app.domain.schemas import (
    ClarificationResult,
    ConditionalEdgePolicy,
    FormulationDecision,
    FormulationRecommendation,
    GeneratedCommandRoutingDecision,
    HumanReviewResult,
    HumanInteractionResponse,
    HumanInteractionOption,
    OperationalIncidentImpact,
    OrchestrationPlan,
    RequestGateDecision,
    RoutingWorkloadContext,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    PolicyDefaultRequirement,
    RoutedNormalizedWarehouseRequest,
    SystemContextRequirement,
)
from app.graph.node_support import error_update, llm_summary, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.policies.routing_policy import CONTEXT_ORDER
from app.services.request_gate_service import (
    _inventory_data_conflict,
    code_input_rejection,
    resolve_request_gate,
)
from app.services.incident_response_service import build_incident_response_plan
from app.services.workload_routing_service import assess_workload_route
from app.prompts.input_normalizer import (
    INPUT_NORMALIZER_SYSTEM,
    PROMPT_VERSION as NORMALIZER_PROMPT_VERSION,
)
from app.prompts.request_router import (
    GENERATED_COMMAND_ROUTER_SYSTEM,
    REQUEST_ROUTER_SYSTEM,
    PROMPT_VERSION as REQUEST_ROUTER_PROMPT_VERSION,
)


def _dedupe(values: list[str]) -> list[str]:
    """Preserve order while removing duplicate strings."""

    return list(dict.fromkeys(value for value in values if value))


def _routing_workload_context(state: LaroGraphState) -> RoutingWorkloadContext | None:
    structured_input = state.get("structured_input")
    if structured_input is None:
        return None
    raw = (
        structured_input.get("routing_context")
        if isinstance(structured_input, dict)
        else getattr(structured_input, "routing_context", None)
    )
    if raw is None:
        return None
    return raw if isinstance(raw, RoutingWorkloadContext) else RoutingWorkloadContext.model_validate(raw)


def _is_generated_command_batch(state: LaroGraphState) -> bool:
    """Return whether the authoritative operations came from the command Agent."""

    workload = _routing_workload_context(state)
    return bool(
        state.get("structured_input") is not None
        and workload is not None
        and str(workload.source or "").upper() == "AI_FULFILLMENT_COMMAND_AGENT"
    )


def _pre_router_code_rejection(state: LaroGraphState) -> dict | None:
    """Reject malformed canonical IDs before any Router LLM invocation.

    This check is intentionally syntax-only and performs no database lookup.
    Existence and warehouse ownership remain downstream repository validation.
    """

    if state.get("structured_input") is None or not state.get("events"):
        return None
    request = _structured_normalized_request(state)
    rejection = code_input_rejection(request)
    if rejection is None:
        return None
    gate = RequestGateDecision(
        action="REJECT_INPUT",
        reasons=[
            "Canonical operation/resource syntax was rejected deterministically before the Router LLM."
        ],
        input_rejection=rejection,
    )
    return {
        "normalized_request": request,
        "request_gate_decision": gate,
        "pending_human_interaction": None,
        "input_rejection": rejection,
        "workflow_hold": None,
        "_llm_used": False,
        **trace_update("pre_router_canonical_id_validator"),
    }



def _incident_orchestration_plan(
    state: LaroGraphState,
    request: NormalizedWarehouseRequest,
    *,
    reasons: list[str],
) -> OrchestrationPlan:
    """Create the immutable dedicated route used by incident-only workflows."""

    return OrchestrationPlan(
        orchestration_goal=request.raw_user_command or "Handle the reported operational incident safely.",
        route="INCIDENT_RESPONSE_PIPELINE",
        formulation_route="INCIDENT_RESPONSE",
        retrieval_strategy="NONE",
        selected_context_nodes=[],
        selected_retrieval_tools=[],
        routing_reason=_dedupe(reasons),
        routing_source="incident_response_service",
        planning_mode=state.get("planning_mode", "llm_router"),
        requested_planning_mode=state.get("requested_planning_mode"),
        planning_mode_source=state.get("planning_mode_source", "environment"),
        route_locked=True,
        route_switch_allowed=False,
        needs_optimization=False,
    )


# The request router is allowed to see typed event facts, not arbitrary free-text
# metadata.  In particular, ``payload.note`` must never become an instruction
# that can alter the Rule/Agent route or authoritative entity IDs.
_TRUSTED_ROUTER_PAYLOAD_KEYS: dict[str, set[str]] = {
    "new_order": {"priority", "objective_profile", "deadline_ms"},
    "inbound_item_arrived": {
        "inbound_id", "handling_unit_id", "item_id", "quantity", "priority", "source_node", "target_node"
    },
    "edge_congested": {"status", "congestion_level", "travel_time_multiplier", "cost_multiplier"},
    "edge_occupied": {"status", "occupied_from_ms", "occupied_until_ms", "robot_id"},
    "edge_reserved": {"status", "start_at_ms", "end_at_ms", "robot_id"},
    "edge_blocked": {"status", "reason_code"},
    "robot_recovery_requested": {
        "operator_recovery_action", "source_incident_id", "active_task_id", "load_state"
    },
    "operational_incident": {
        "incident_id", "description", "incident_description", "scope",
        "affected_resource_ids", "affected_resource_references", "observed_effect",
        "robot_operability", "load_state", "handling_mode", "immediate_safety_action",
        "physical_intervention_required", "operator_decision_reason", "decision_prompt",
        "decision_options", "notification_title", "notification_message", "reason_codes",
        "confidence",
    },
    "status_query": {"query_scope"},
    "explain_only": {"query_scope"},
}


def _router_safe_event(event: object) -> dict[str, object]:
    """Return a prompt-safe event view with untrusted metadata removed.

    The full event remains in graph state and is available to deterministic
    adapters.  The LLM receives only canonical identity fields and a strict
    payload allowlist.  This makes event notes data rather than prompt text.
    """

    event_type = str(getattr(event, "type", ""))
    payload = dict(getattr(event, "payload", {}) or {})
    allowed = _TRUSTED_ROUTER_PAYLOAD_KEYS.get(event_type, set())
    trusted_payload = {key: payload[key] for key in allowed if key in payload}
    return {
        "type": event_type,
        "order_id": getattr(event, "order_id", None),
        "robot_id": getattr(event, "robot_id", None),
        "edge_id": getattr(event, "edge_id", None),
        "node_id": getattr(event, "node_id", None),
        "payload": trusted_payload,
    }


_ORDER_TOKEN = re.compile(r"(?<![A-Z0-9])ORD-\d{3,}(?![A-Z0-9])", re.I)
_INBOUND_TOKEN = re.compile(r"(?<![A-Z0-9])IN-\d{3,}(?![A-Z0-9])", re.I)
_ROBOT_TOKEN = re.compile(r"(?<![A-Z0-9])R\d{3}(?![A-Z0-9])", re.I)
_EDGE_TOKEN = re.compile(r"(?<![A-Z0-9])(?:H|V)\d+_\d+(?![A-Z0-9])", re.I)


def _preserve_canonical_codes_from_text(
    request: NormalizedWarehouseRequest,
) -> NormalizedWarehouseRequest:
    """Recover explicit canonical codes without interpreting warehouse facts.

    LLM outputs occasionally place an exact code such as ``H3_7`` in a prose
    reference field or omit it from the typed ID list.  Exact tokens in the
    operator command are authoritative syntax, so this pass restores them
    deterministically.  It never resolves aliases or looks up a database.
    """

    command = (request.raw_user_command or "").strip()
    if not command:
        return request

    upper = command.upper()
    order_ids = _dedupe([value.upper() for value in _ORDER_TOKEN.findall(upper)])
    inbound_ids = _dedupe([value.upper() for value in _INBOUND_TOKEN.findall(upper)])
    edge_ids = _dedupe([value.upper() for value in _EDGE_TOKEN.findall(upper)])
    robot_ids = _dedupe([value.upper() for value in _ROBOT_TOKEN.findall(upper)])

    operations = list(request.operations)
    if order_ids:
        operations = [
            value for value in operations
            if value.operation_type != "OUTBOUND_ORDER"
            or re.fullmatch(r"ORD-\d{3,}", value.operation_id.upper())
        ]
        existing = {value.operation_id.upper() for value in operations if value.operation_type == "OUTBOUND_ORDER"}
        for order_id in order_ids:
            if order_id not in existing:
                operations.append(
                    NormalizedOperation(
                        operation_id=order_id,
                        operation_type="OUTBOUND_ORDER",
                        source_event_type="natural_language_code",
                        raw_reference=order_id,
                        attributes="Canonical order ID preserved from the operator command.",
                    )
                )
    if inbound_ids:
        operations = [
            value for value in operations
            if value.operation_type != "INBOUND_ITEM"
            or re.fullmatch(r"IN-\d{3,}", value.operation_id.upper())
        ]
        existing = {value.operation_id.upper() for value in operations if value.operation_type == "INBOUND_ITEM"}
        for inbound_id in inbound_ids:
            if inbound_id not in existing:
                operations.append(
                    NormalizedOperation(
                        operation_id=inbound_id,
                        operation_type="INBOUND_ITEM",
                        source_event_type="natural_language_code",
                        raw_reference=inbound_id,
                        attributes="Canonical inbound ID preserved from the operator command.",
                    )
                )

    constraints = request.constraints
    folded = command.casefold()
    conditional = any(marker in folded for marker in ("넘으면", "이면", "아니면", "otherwise", " if ", "when "))
    avoid_language = any(marker in folded for marker in ("avoid", "회피", "피해", "우회", "soft", "hard"))
    hard_only = any(marker in folded for marker in ("hard avoid", "완전 차단", "사용 금지")) and not conditional

    soft_edges = list(constraints.soft_avoid_edge_ids)
    hard_edges = list(constraints.hard_block_edge_ids)
    if edge_ids and avoid_language:
        if hard_only:
            hard_edges.extend(edge_ids)
        else:
            # Conditional hard/soft policies start as a typed soft candidate;
            # the Agent may promote it after runtime-context evaluation.
            soft_edges.extend(edge_ids)

    excluded_robots = list(constraints.excluded_robot_ids)
    if robot_ids and any(marker in folded for marker in ("제외", "빼", "사용하지", "exclude", "without")):
        excluded_robots.extend(robot_ids)

    max_wait = constraints.max_edge_wait_ms
    wait_match = re.search(r"(\d+(?:\.\d+)?)\s*초", command)
    if wait_match and any(marker in folded for marker in ("대기", "wait", "지연")):
        max_wait = int(float(wait_match.group(1)) * 1000)

    conditional_edge_policies = list(constraints.conditional_edge_policies)
    if conditional and edge_ids and max_wait is not None and avoid_language:
        true_action = "HARD_AVOID" if any(
            marker in folded for marker in ("hard avoid", "강하게 회피", "완전 회피", "사용 금지")
        ) else "SOFT_AVOID"
        false_action = "SOFT_AVOID" if any(
            marker in folded for marker in ("아니면 soft", "otherwise soft", "그 외에는 soft", "아니면 완화")
        ) else "ALLOW"
        existing_policy_edges = {value.edge_id for value in conditional_edge_policies}
        for edge_id in edge_ids:
            if edge_id not in existing_policy_edges:
                conditional_edge_policies.append(
                    ConditionalEdgePolicy(
                        edge_id=edge_id,
                        metric="EXPECTED_WAIT_MS",
                        operator="GT",
                        threshold_ms=max_wait,
                        when_true=true_action,
                        when_false=false_action,
                        source_text=command,
                    )
                )

    constraints = constraints.model_copy(
        update={
            "excluded_robot_ids": _dedupe(excluded_robots),
            "soft_avoid_edge_ids": _dedupe(soft_edges),
            "hard_block_edge_ids": _dedupe(hard_edges),
            "conditional_edge_policies": conditional_edge_policies,
            "max_edge_wait_ms": max_wait,
        }
    )
    constraints = _canonicalize_constraint_references(
        constraints,
        raw_user_command=command,
    )

    summary = request.normalization_summary
    if any((order_ids, inbound_ids, edge_ids, excluded_robots)):
        summary += " Explicit canonical codes in the operator command were preserved deterministically."
    return request.model_copy(
        update={
            "operations": operations,
            "constraints": constraints,
            "normalization_summary": summary,
        }
    )


_ROBOT_STATUS_MARKERS: dict[str, tuple[str, ...]] = {
    "charging": ("charging", "충전 중", "충전중", "충전 상태"),
    "working": ("working", "busy", "작업 중", "작업중", "업무 중", "임무 수행 중"),
    "maintenance": ("maintenance", "정비 중", "정비중", "점검 중", "점검중"),
    "offline": ("offline", "오프라인", "연결 끊김"),
    "error": ("fault", "error", "고장", "오류 상태"),
}


def _robot_statuses_from_text(value: str) -> list[str]:
    """Extract robot-runtime status filters from one natural-language phrase."""

    text = str(value).casefold()
    return [
        status
        for status, markers in _ROBOT_STATUS_MARKERS.items()
        if any(marker.casefold() in text for marker in markers)
    ]


def _contains_exact_identifier(reference: str, identifiers: list[str]) -> bool:
    """Return whether a prose reference already embeds a supplied canonical ID."""

    text = str(reference).casefold()
    for identifier in identifiers:
        token = str(identifier).casefold()
        if token and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text):
            return True
    return False


def _canonicalize_constraint_references(
    constraints: NormalizedRequestConstraints,
    *,
    raw_user_command: str | None,
) -> NormalizedRequestConstraints:
    """Separate status filters and remove exact-ID prose duplicates.

    Live v12 traces showed two recurring model errors:

    * exact IDs such as ``H3_7`` and ``R003`` were emitted in both the ID and
      semantic-reference fields, causing the resolver to re-search already
      authoritative keys; and
    * phrases such as ``충전 중인 로봇`` were emitted as robot names rather than
      runtime-status filters.

    This pass only normalizes the request contract.  It does not query or invent
    warehouse facts.
    """

    canonical_status_names = set(_ROBOT_STATUS_MARKERS)
    excluded_statuses: list[str] = []
    status_references = list(constraints.excluded_robot_status_references)
    for value in constraints.excluded_robot_statuses:
        normalized = str(value).strip().casefold()
        if normalized in canonical_status_names:
            excluded_statuses.append(normalized)
            continue
        extracted = _robot_statuses_from_text(value)
        if extracted:
            excluded_statuses.extend(extracted)
            status_references.append(str(value).strip())

    retained_robot_refs: list[str] = []
    for reference in constraints.excluded_robot_references:
        statuses = _robot_statuses_from_text(reference)
        if statuses:
            excluded_statuses.extend(statuses)
            status_references.append(reference)
            continue
        if _contains_exact_identifier(reference, constraints.excluded_robot_ids):
            continue
        retained_robot_refs.append(reference)

    command = raw_user_command or ""
    exclusion_language = any(
        marker in command.casefold()
        for marker in ("제외", "빼", "사용하지", "exclude", "do not use", "without")
    )
    if exclusion_language:
        excluded_statuses.extend(_robot_statuses_from_text(command))

    soft_refs = [
        value
        for value in constraints.soft_avoid_edge_references
        if not _contains_exact_identifier(value, constraints.soft_avoid_edge_ids)
    ]
    hard_refs = [
        value
        for value in constraints.hard_block_edge_references
        if not _contains_exact_identifier(value, constraints.hard_block_edge_ids)
    ]
    return constraints.model_copy(
        update={
            "excluded_robot_statuses": _dedupe([value.casefold() for value in excluded_statuses]),
            "excluded_robot_status_references": _dedupe(status_references),
            "excluded_robot_references": _dedupe(retained_robot_refs),
            "soft_avoid_edge_references": _dedupe(soft_refs),
            "hard_block_edge_references": _dedupe(hard_refs),
        }
    )



def _incident_only_request(request: NormalizedWarehouseRequest) -> bool:
    """Return whether all normalized operations are generic incidents."""

    return bool(request.operations) and all(
        operation.operation_type == "INCIDENT" for operation in request.operations
    )

def _structured_requirements(operations: list[NormalizedOperation]) -> list[SystemContextRequirement]:
    """Describe canonical read-only facts needed for structured operations."""

    requirements: list[SystemContextRequirement] = []
    operation_ids = [operation.operation_id for operation in operations]
    if any(operation.operation_type in {"OUTBOUND_ORDER", "INBOUND_ITEM"} for operation in operations):
        requirements.extend(
            [
                SystemContextRequirement(
                    code="ORDER_AND_INVENTORY_FACTS",
                    context_node="inventory_context",
                    description="Load authoritative order, item, quantity, destination, and stock candidates.",
                    entity_ids=operation_ids,
                ),
                SystemContextRequirement(
                    code="WAREHOUSE_MAP_AND_TRAFFIC",
                    context_node="map_context",
                    description="Load warehouse topology and current map/traffic overlays.",
                    entity_ids=operation_ids,
                ),
                SystemContextRequirement(
                    code="ROBOT_RUNTIME_STATE",
                    context_node="robot_runtime",
                    description="Load current robot status, battery, capacity, load, and location.",
                    entity_ids=operation_ids,
                ),
            ]
        )
    if any(operation.operation_type == "INCIDENT" for operation in operations):
        requirements.extend(
            [
                SystemContextRequirement(
                    code="INCIDENT_MAP_RUNTIME",
                    context_node="map_context",
                    description="Load affected map resources and current traversability/traffic state.",
                    entity_ids=operation_ids,
                ),
                SystemContextRequirement(
                    code="INCIDENT_ROBOT_RUNTIME",
                    context_node="robot_runtime",
                    description="Load affected robot and active mission state when applicable.",
                    entity_ids=operation_ids,
                ),
            ]
        )
    if any(operation.operation_type == "RECOVERY" for operation in operations):
        requirements.extend(
            [
                SystemContextRequirement(
                    code="RECOVERY_MAP_STATE",
                    context_node="map_context",
                    description="Load current topology and blocked/occupied resources for recovery.",
                    entity_ids=operation_ids,
                ),
                SystemContextRequirement(
                    code="RECOVERY_ROBOT_STATE",
                    context_node="robot_runtime",
                    description="Load the affected robot execution and load state.",
                    entity_ids=operation_ids,
                ),
            ]
        )
    return requirements


def _classify_missing_question(question: str, request: NormalizedWarehouseRequest) -> str | None:
    """Classify an LLM-authored question as system context, policy, or true ambiguity."""

    text = question.casefold()
    # Explicit selection questions are genuine operator ambiguity even though they
    # contain words such as "order" or "robot".
    ambiguity_markers = (
        "which order", "which robot", "which destination", "어떤 주문", "어느 주문",
        "어떤 로봇", "어느 로봇", "어디로", "which one", "choose between",
    )
    if any(marker in text for marker in ambiguity_markers):
        return None

    inventory_markers = (
        "current_inventory", "current inventory", "inventory state", "inventory", "stock", "sku",
        "item", "quantity", "qty", "order detail", "order facts", "destination",
        "fulfillment location", "pickup location", "pick source", "delivery location",
        "현재 재고", "재고 상태", "재고", "품목", "수량", "주문 상세", "출고지", "목적지", "피킹 위치",
    )
    robot_markers = (
        "current_robot", "current robot", "robot runtime", "fleet state", "robot state",
        "robot status", "battery", "capacity", "current task", "robot location", "load state",
        "현재 로봇", "로봇 런타임", "로봇 상태", "로봇", "배터리", "용량", "현재 작업", "로봇 위치", "적재 상태",
    )
    map_markers = (
        "warehouse_graph", "warehouse graph", "warehouse state", "warehouse topology",
        "topology", "map", "edge state", "edge", "route state", "route", "path",
        "congestion", "occupancy", "reservation",
        "현재 창고 상태", "창고 상태", "창고 그래프", "창고 토폴로지", "지도",
        "통로 상태", "통로", "경로", "혼잡", "점유", "예약",
    )
    policy_markers = (
        "split order", "split across", "soft-avoid", "soft avoid", "penalty", "time limit",
        "policy choice", "분할", "패널티", "기본 정책", "정책값",
    )
    if any(marker in text for marker in policy_markers):
        return "policy"
    if any(marker in text for marker in inventory_markers):
        return "inventory_context"
    if any(marker in text for marker in robot_markers):
        return "robot_runtime"
    if any(marker in text for marker in map_markers):
        return "map_context"
    return None



def _incident_from_command(command: str | None) -> OperationalIncidentImpact | None:
    """Create one generic impact record when the LLM omitted an obvious incident.

    This is deliberately not an incident taxonomy.  The fallback only extracts
    affected resources and conservative operational effects visible in the text.
    """

    text = (command or "").strip()
    if not text:
        return None
    incident_markers = (
        "쏟", "떨어", "넘어", "장애물", "통로에 사람", "사람이 통로", "고장", "멈췄",
        "spill", "dropped", "obstacle", "stopped", "fault", "person in",
    )
    folded = text.casefold()
    if not any(value.casefold() in folded for value in incident_markers):
        return None

    exact_ids = _dedupe(
        re.findall(
            r"(?<![A-Z0-9])(?:R\d{3}|H\d+_\d+|V\d+_\d+|K\d+_\d+|O_[A-Z]|I_[A-Za-z]|C\d{2}|RJ\d{2})(?![A-Z0-9])",
            text.upper(),
        )
    )
    broad_refs = _dedupe(re.findall(r"(?<![A-Z0-9])H\d+(?![_A-Z0-9])", text.upper()))
    robot_scope = bool(any(value.startswith("R") for value in exact_ids)) or any(
        value in folded for value in ("로봇", "robot", "amr")
    )
    scope = "ROBOT" if robot_scope else "MAP_RESOURCE"
    if any(value in folded for value in ("통행 불가", "지나갈 수 없", "완전 차단", "not traversable", "blocked")):
        effect = "NOT_TRAVERSABLE"
    elif any(value in folded for value in ("통행 가능", "지나갈 수 있", "passable", "traversable")):
        effect = "TRAVERSABLE"
    elif any(value in folded for value in ("혼잡", "일부 통행", "degraded")):
        effect = "DEGRADED"
    else:
        effect = "UNKNOWN"
    fault = robot_scope and any(value in folded for value in ("고장", "멈췄", "정지", "fault", "stopped"))
    loaded = any(value in folded for value in ("물건을 든 채", "적재한 채", "짐을 싣고", "loaded", "carrying load"))
    physical = any(value in folded for value in ("쏟", "떨어", "장애물", "박스", "팔레트", "사람", "정리", "회수", "spill", "obstacle"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10].upper()
    return OperationalIncidentImpact(
        incident_id=f"INCIDENT-NL-{digest}",
        description=text,
        scope=scope,
        affected_resource_ids=exact_ids,
        affected_resource_references=broad_refs,
        observed_effect=effect,
        robot_operability="FAULTED" if fault else "UNKNOWN" if robot_scope else "NOT_APPLICABLE",
        load_state="LOADED" if loaded else "UNKNOWN" if robot_scope else "NOT_APPLICABLE",
        physical_intervention_required=physical,
        handling_mode="AUTO_HANDLE",
        immediate_safety_action="NONE",
        reason_codes=["DETERMINISTIC_INCIDENT_FALLBACK"],
        confidence=0.7,
    )


def _canonicalize_normalized_request(request: NormalizedWarehouseRequest) -> NormalizedWarehouseRequest:
    """Move system-resolvable questions out of the operator-clarification bucket.

    The LLM is allowed to describe missing facts, but the workflow owns the final
    classification of where those facts come from.  This deterministic pass does
    not invent or replace warehouse facts; it only routes retrieval responsibility.
    """

    canonical_constraints = _canonicalize_constraint_references(
        request.constraints,
        raw_user_command=request.raw_user_command,
    )
    request = request.model_copy(update={"constraints": canonical_constraints})
    request = _preserve_canonical_codes_from_text(request)
    # An authoritative inventory-data conflict is a business-data exception,
    # not a physical operational incident.  It must hold the affected order or
    # stock and create a recount/reconciliation task; it must never block a map
    # resource or hold a robot merely because the LLM emitted an incident.
    if _inventory_data_conflict(request.raw_user_command or ""):
        request = request.model_copy(
            update={
                "operations": [
                    value for value in request.operations
                    if value.operation_type != "INCIDENT"
                ],
                "incidents": [],
            }
        )
    if not request.incidents and not _inventory_data_conflict(request.raw_user_command or ""):
        inferred = _incident_from_command(request.raw_user_command)
        if inferred is not None:
            operations = list(request.operations)
            if not any(value.operation_type == "INCIDENT" for value in operations):
                operations.append(
                    NormalizedOperation(
                        operation_id=inferred.incident_id,
                        operation_type="INCIDENT",
                        source_event_type="natural_language_incident",
                        raw_reference=inferred.description,
                        attributes="generic operational impact; no detailed incident taxonomy",
                    )
                )
            request = request.model_copy(update={"operations": operations, "incidents": [inferred]})

    requirements = [*request.system_context_requirements, *_structured_requirements(request.operations)]
    policy_defaults = list(request.policy_default_requirements)
    clarification_questions: list[str] = []
    for index, question in enumerate(request.user_clarification_questions, start=1):
        classification = _classify_missing_question(question, request)
        if classification == "policy":
            policy_defaults.append(
                PolicyDefaultRequirement(
                    policy_key=f"NORMALIZED_POLICY_DEFAULT_{index}",
                    description=question,
                )
            )
        elif classification in {"inventory_context", "map_context", "robot_runtime"}:
            requirements.append(
                SystemContextRequirement(
                    code=f"NORMALIZED_SYSTEM_CONTEXT_{index}",
                    context_node=classification,
                    description=question,
                    entity_ids=[operation.operation_id for operation in request.operations],
                )
            )
        else:
            clarification_questions.append(question)

    requirements_by_node: dict[str, list[SystemContextRequirement]] = {}
    for requirement in requirements:
        requirements_by_node.setdefault(requirement.context_node, []).append(requirement)
    consolidated_requirements: list[SystemContextRequirement] = []
    for context_node in CONTEXT_ORDER:
        values = requirements_by_node.get(context_node, [])
        if not values:
            continue
        descriptions = _dedupe([value.description for value in values])
        entity_ids = _dedupe([entity_id for value in values for entity_id in value.entity_ids])
        consolidated_requirements.append(
            SystemContextRequirement(
                code=f"CANONICAL_{context_node.upper()}",
                context_node=context_node,
                description="; ".join(descriptions),
                entity_ids=entity_ids,
            )
        )
    policy_by_key: dict[tuple[str, str], PolicyDefaultRequirement] = {}
    for requirement in policy_defaults:
        policy_by_key.setdefault((requirement.policy_key, requirement.description), requirement)

    return request.model_copy(
        update={
            "system_context_requirements": consolidated_requirements,
            "policy_default_requirements": list(policy_by_key.values()),
            "user_clarification_questions": _dedupe(clarification_questions),
        }
    )


def _incident_from_event(event: object, index: int) -> OperationalIncidentImpact | None:
    """Normalize one generic structured incident by impact, not event subtype."""

    event_type = str(getattr(event, "type", ""))
    payload = dict(getattr(event, "payload", {}) or {})
    # The public domain contract intentionally exposes one generic structured
    # event.  Upstream WMS/WCS/sensor-specific names must be translated at the
    # FastAPI/adapter boundary instead of growing an incident taxonomy here.
    if event_type != "operational_incident":
        return None

    incident_id = str(
        payload.get("incident_id")
        or getattr(event, "edge_id", None)
        or getattr(event, "robot_id", None)
        or getattr(event, "node_id", None)
        or f"INCIDENT-{index:03d}"
    )
    description = str(
        payload.get("description")
        or payload.get("incident_description")
        or f"Operational incident reported by event {event_type!r}."
    )
    exact_ids = [
        str(value)
        for value in payload.get("affected_resource_ids", [])
        if str(value).strip()
    ]
    for value in (
        getattr(event, "edge_id", None),
        getattr(event, "robot_id", None),
        getattr(event, "node_id", None),
    ):
        if value:
            exact_ids.append(str(value))
    refs = [
        str(value)
        for value in payload.get("affected_resource_references", [])
        if str(value).strip()
    ]
    options = [
        HumanInteractionOption.model_validate(value)
        for value in payload.get("decision_options", [])
    ]
    return OperationalIncidentImpact(
        incident_id=incident_id,
        description=description,
        scope=str(payload.get("scope") or "UNKNOWN").upper(),
        affected_resource_ids=_dedupe(exact_ids),
        affected_resource_references=_dedupe(refs),
        observed_effect=str(payload.get("observed_effect") or "UNKNOWN").upper(),
        robot_operability=str(payload.get("robot_operability") or "NOT_APPLICABLE").upper(),
        load_state=str(payload.get("load_state") or "NOT_APPLICABLE").upper(),
        handling_mode=str(payload.get("handling_mode") or "AUTO_HANDLE").upper(),
        immediate_safety_action=str(payload.get("immediate_safety_action") or "NONE").upper(),
        physical_intervention_required=bool(payload.get("physical_intervention_required", False)),
        operator_decision_reason=(
            str(payload["operator_decision_reason"])
            if payload.get("operator_decision_reason") is not None
            else None
        ),
        decision_prompt=(
            str(payload["decision_prompt"])
            if payload.get("decision_prompt") is not None
            else None
        ),
        decision_options=options,
        notification_title=(
            str(payload["notification_title"])
            if payload.get("notification_title") is not None
            else None
        ),
        notification_message=(
            str(payload["notification_message"])
            if payload.get("notification_message") is not None
            else None
        ),
        reason_codes=[str(value) for value in payload.get("reason_codes", [])],
        confidence=float(payload.get("confidence", 1.0)),
    )


def _structured_normalized_request(state: LaroGraphState) -> NormalizedWarehouseRequest:
    """Create the canonical request representation for structured events."""
    operations: list[NormalizedOperation] = []
    incidents: list[OperationalIncidentImpact] = []
    clarification_questions: list[str] = []
    for index, event in enumerate(state.get("events", []), start=1):
        incident = _incident_from_event(event, index)
        if incident is not None:
            incidents.append(incident)
            operations.append(
                NormalizedOperation(
                    operation_id=incident.incident_id,
                    operation_type="INCIDENT",
                    source_event_type=event.type,
                    raw_reference=incident.description,
                    attributes=(
                        f"impact={incident.observed_effect}; handling={incident.handling_mode}; "
                        f"safety={incident.immediate_safety_action}"
                    ),
                )
            )
        elif event.type == "new_order" and event.order_id:
            operations.append(
                NormalizedOperation(
                    operation_id=event.order_id,
                    operation_type="OUTBOUND_ORDER",
                    source_event_type=event.type,
                    raw_reference=event.order_id,
                )
            )
        elif event.type == "inbound_item_arrived":
            operation_id = str(event.inbound_id or event.payload.get("inbound_id") or event.payload.get("handling_unit_id") or event.node_id or f"IN-{index:03d}")
            operations.append(
                NormalizedOperation(
                    operation_id=operation_id,
                    operation_type="INBOUND_ITEM",
                    source_event_type=event.type,
                    raw_reference=operation_id,
                )
            )
        elif event.type == "robot_recovery_requested" and event.robot_id:
            operations.append(
                NormalizedOperation(
                    operation_id=event.robot_id,
                    operation_type="RECOVERY",
                    source_event_type=event.type,
                    raw_reference=event.robot_id,
                )
            )
        elif event.type in {"status_query", "explain_only"}:
            operations.append(
                NormalizedOperation(
                    operation_id=f"QUERY-{index:03d}",
                    operation_type="QUERY",
                    source_event_type=event.type,
                )
            )
        elif event.type not in {
            "edge_congested",
            "edge_occupied",
            "edge_reserved",
            "edge_blocked",
            "low_battery",
        }:
            operation_id = event.order_id or event.robot_id or event.edge_id or f"UNKNOWN-{index:03d}"
            operations.append(
                NormalizedOperation(
                    operation_id=operation_id,
                    operation_type="UNKNOWN",
                    source_event_type=event.type,
                )
            )
            clarification_questions.append(
                f"Identify the intended warehouse operation for event {event.type!r} ({operation_id})."
            )
    structured_input = state.get("structured_input")
    structured_constraints = (
        structured_input.get("constraints")
        if isinstance(structured_input, dict)
        else getattr(structured_input, "constraints", None)
    )
    if isinstance(structured_constraints, NormalizedRequestConstraints):
        constraints = structured_constraints
        if (
            "objective_profile" in constraints.model_fields_set
            and "objective_profile_explicit" not in constraints.model_fields_set
        ):
            constraints = constraints.model_copy(update={"objective_profile_explicit": True})
    elif structured_constraints is not None:
        raw_constraints = dict(structured_constraints)
        if (
            "objective_profile" in raw_constraints
            and "objective_profile_explicit" not in raw_constraints
        ):
            raw_constraints["objective_profile_explicit"] = True
        constraints = NormalizedRequestConstraints.model_validate(raw_constraints)
    else:
        constraints = NormalizedRequestConstraints()
    return NormalizedWarehouseRequest(
        source="structured_events",
        operations=operations,
        incidents=incidents,
        constraints=constraints,
        raw_user_command=state.get("user_command"),
        system_context_requirements=_structured_requirements(operations),
        policy_default_requirements=[],
        user_clarification_questions=clarification_questions,
        normalization_summary=f"Deterministically normalized {len(operations)} operation(s) from structured events.",
    )


def _authoritative_structured_router_fallback(
    state: LaroGraphState,
    exc: Exception | None,
) -> dict | None:
    """Route a complete BE structured request without depending on the router LLM.

    Spring-generated command batches already carry the complete operation set in
    ``structured_input`` and do not carry a natural-language command.  The
    deterministic request gate ultimately locks those requests to Rule even when
    the router LLM succeeds.  Skipping that redundant external call keeps the
    zero-minute/rolling command cycle immediate; the exception parameter also
    preserves a safe fallback for callers that begin the LLM call before this
    contract can be recognized.  Natural-language and mixed requests intentionally
    remain fail-closed because their semantics cannot be reconstructed safely
    without the router.
    """

    if (
        state.get("structured_input") is None
        or not state.get("events")
        or bool((state.get("user_command") or "").strip())
    ):
        return None

    workload = assess_workload_route(_routing_workload_context(state), get_settings())
    if (
        state.get("planning_mode", "llm_router") == "llm_router"
        and workload.band == "GRAY"
        and exc is None
    ):
        # Gray workload is intentionally delegated to the tool-free router LLM.
        return None

    request = _canonicalize_normalized_request(_structured_normalized_request(state))
    request = _apply_human_responses(state, request)
    responses = [
        value
        if isinstance(value, HumanInteractionResponse)
        else HumanInteractionResponse.model_validate(value)
        for value in state.get("human_responses", [])
    ]
    incident_response_plan = build_incident_response_plan(
        simulation_id=state["simulation_id"],
        incidents=list(request.incidents),
        human_responses=responses,
    )
    recommendation = FormulationRecommendation(
        route=("AGENT_FORMULATION" if workload.band == "HIGH" else "RULE_FORMULATION"),
        reasons=[
            workload.reason,
            (
                "The complete authoritative structured input requires no semantic "
                "interpretation and was workload-routed without a router LLM call."
                if exc is None
                else "The router LLM was unavailable, so the complete authoritative "
                "structured input was preserved and locked to the deterministic Rule route."
            )
        ],
    )
    gate = resolve_request_gate(
        simulation_id=state["simulation_id"],
        request=request,
        recommendation=recommendation,
        original_user_command=None,
        has_structured_events=True,
        authoritative_structured_input=state.get("structured_input") is not None,
        planning_mode=state.get("planning_mode", "llm_router"),
        requires_agent_guard=_pre_route_guard_requires_agent(request),
        human_responses=responses,
        incident_response_plan=incident_response_plan,
        workload_assessment=workload,
    )
    decision = None
    if gate.final_route in {"RULE_FORMULATION", "AGENT_FORMULATION"}:
        decision = FormulationDecision(
            route=gate.final_route,
            reasons=gate.reasons,
            required_context_nodes=_required_context_nodes(request),
        )

    summary = llm_summary(
        node_name="request_router_llm",
        prompt_version=REQUEST_ROUTER_PROMPT_VERSION,
        task_summary="Router LLM failure fallback for authoritative structured input",
        input_summary=(
            f"events={len(state.get('events', []))}, command=False, "
            f"mode={state.get('planning_mode')}"
        ),
        output_summary=(
            f"fallback=deterministic_rule, operations={len(request.operations)}, "
            f"gate={gate.action}, cause={type(exc).__name__ if exc is not None else 'not_required'}"
        ),
    )
    update = {
        "normalized_request": request,
        "request_gate_decision": gate,
        "incident_response_plan": incident_response_plan,
        "operator_notifications": list(incident_response_plan.notifications),
        "pending_human_interaction": gate.human_interaction,
        "input_rejection": gate.input_rejection,
        "workflow_hold": gate.workflow_hold,
        "llm_node_summaries": ([] if exc is None else [summary]),
        "_llm_used": False,
        **trace_update("request_router_llm_structured_fallback"),
    }
    if decision is not None:
        update["formulation_decision"] = decision
    if gate.final_route == "INCIDENT_RESPONSE":
        update["orchestration_plan"] = _incident_orchestration_plan(
            state, request, reasons=gate.reasons
        )
    return update


def _required_context_nodes(request: NormalizedWarehouseRequest) -> list[str]:
    """Resolve the complete canonical context set without asking the operator."""

    selected = {requirement.context_node for requirement in request.system_context_requirements}
    operation_types = {operation.operation_type for operation in request.operations}
    if operation_types & {"OUTBOUND_ORDER", "INBOUND_ITEM"}:
        selected.update({"inventory_context", "map_context", "robot_runtime"})
    if "RECOVERY" in operation_types or "INCIDENT" in operation_types:
        selected.update({"map_context", "robot_runtime"})
    if (
        request.constraints.excluded_robot_ids
        or request.constraints.excluded_robot_references
        or request.constraints.excluded_robot_statuses
    ):
        selected.add("robot_runtime")
    if (
        request.constraints.soft_avoid_edge_ids
        or request.constraints.hard_block_edge_ids
        or request.constraints.soft_avoid_edge_references
        or request.constraints.hard_block_edge_references
    ):
        selected.add("map_context")
    return [name for name in CONTEXT_ORDER if name in selected]


def _true_clarification_questions(request: NormalizedWarehouseRequest) -> list[str]:
    """Return only ambiguity that warehouse tools and policy cannot resolve."""

    questions = list(request.user_clarification_questions)
    for operation in request.operations:
        if operation.operation_type == "UNKNOWN":
            questions.append(
                f"Clarify the intended operation for {operation.operation_id} ({operation.source_event_type or 'unknown source'})."
            )
    return _dedupe(questions)


def _preserve_authoritative_structured_input(
    state: LaroGraphState,
    request: NormalizedWarehouseRequest,
) -> NormalizedWarehouseRequest:
    """Preserve typed event identifiers while retaining LLM semantic additions.

    The unified router may normalize and recommend a route, but structured
    upstream identifiers remain authoritative.  This function performs no
    repository or Tool access and runs before either branch begins.
    """

    events = list(state.get("events", []))
    if not events:
        return request

    seed = _structured_normalized_request(state)
    seed_keys = {
        (operation.operation_id, operation.operation_type, operation.source_event_type)
        for operation in seed.operations
    }
    has_command = bool((state.get("user_command") or "").strip())
    request_structured_input_is_authoritative = state.get("structured_input") is not None
    extras = [
        operation
        for operation in request.operations
        if (operation.operation_id, operation.operation_type, operation.source_event_type) not in seed_keys
    ]
    # The BE-centered contract carries the complete business-operation set in
    # structured_input. user_command may add objectives, exclusions, edge
    # policies, or incidents, but it may not create another operation. Legacy
    # mixed event+command requests without structured_input retain their former
    # semantic-operation behavior.
    if request_structured_input_is_authoritative:
        extras = []

    # Pure structured events are authoritative.  The LLM may recommend a
    # route, but it cannot invent constraints from untrusted payload metadata.
    # Mixed requests retain the command-derived semantic additions.
    base_constraints = request.constraints if has_command else seed.constraints
    if _is_generated_command_batch(state):
        # The command Agent already emitted the canonical operation/resource
        # contract.  The Router may add objectives and typed policy meaning,
        # but free-form policy text must never manufacture a robot/node/edge
        # identity (for example, treating "K2+ rack level" as an edge ID).
        # Preserve semantic objective fields from the Router and freeze every
        # resource-bearing field to the authoritative structured input.
        base_constraints = base_constraints.model_copy(
            update={
                "excluded_robot_ids": list(seed.constraints.excluded_robot_ids),
                "excluded_robot_references": list(seed.constraints.excluded_robot_references),
                "excluded_robot_statuses": list(seed.constraints.excluded_robot_statuses),
                "excluded_robot_status_references": list(
                    seed.constraints.excluded_robot_status_references
                ),
                "soft_avoid_edge_ids": list(seed.constraints.soft_avoid_edge_ids),
                "soft_avoid_edge_references": list(
                    seed.constraints.soft_avoid_edge_references
                ),
                "hard_block_edge_ids": list(seed.constraints.hard_block_edge_ids),
                "hard_block_edge_references": list(
                    seed.constraints.hard_block_edge_references
                ),
                "conditional_edge_policies": list(
                    seed.constraints.conditional_edge_policies
                ),
            }
        )
    soft_edges = list(base_constraints.soft_avoid_edge_ids)
    hard_edges = list(base_constraints.hard_block_edge_ids)
    for event in events:
        if event.type == "edge_congested" and event.edge_id:
            soft_edges.append(event.edge_id)
        elif event.type == "edge_blocked" and event.edge_id:
            hard_edges.append(event.edge_id)

    merged = request.model_copy(
        update={
            "source": "mixed" if has_command else "structured_events",
            "operations": [*seed.operations, *(extras if has_command else [])],
            "incidents": [*seed.incidents, *(
                [value for value in request.incidents if value.incident_id not in {item.incident_id for item in seed.incidents}]
                if has_command else []
            )],
            "constraints": base_constraints.model_copy(
                update={
                    "soft_avoid_edge_ids": _dedupe(soft_edges),
                    "hard_block_edge_ids": _dedupe(hard_edges),
                }
            ),
            "raw_user_command": state.get("user_command"),
            "system_context_requirements": (
                [*request.system_context_requirements, *seed.system_context_requirements]
                if has_command
                else list(seed.system_context_requirements)
            ),
            "policy_default_requirements": (
                list(request.policy_default_requirements)
                if has_command
                else list(seed.policy_default_requirements)
            ),
            "user_clarification_questions": (
                list(request.user_clarification_questions)
                if has_command
                else list(seed.user_clarification_questions)
            ),
            "normalization_summary": (
                request.normalization_summary
                + (
                    " Structured input is the complete operation authority; "
                    "command-derived operations were discarded deterministically."
                    if request_structured_input_is_authoritative
                    else " Authoritative structured event identifiers were preserved deterministically."
                )
            ),
        }
    )
    return _canonicalize_normalized_request(merged)


def _strip_nonphysical_data_conflict_incidents(
    state: LaroGraphState,
    request: NormalizedWarehouseRequest,
) -> NormalizedWarehouseRequest:
    """Keep inventory-data conflicts out of the physical incident pipeline.

    A discrepancy between PostgreSQL/WMS stock and a sensor observation is an
    authority/reconciliation exception.  It must hold the affected order and
    stock allocation, not block a route node or edge.  Explicit structured
    ``operational_incident`` events remain authoritative and are never removed.
    """

    if any(event.type == "operational_incident" for event in state.get("events", [])):
        return request
    command = str(state.get("user_command") or request.raw_user_command or "").casefold()
    conflict_markers = (
        "재고 불일치",
        "수량 불일치",
        "시스템 재고",
        "센서 수량",
        "inventory mismatch",
        "inventory conflict",
        "sensor quantity",
    )
    if not any(marker.casefold() in command for marker in conflict_markers):
        return request
    filtered_operations = [
        value for value in request.operations if value.operation_type != "INCIDENT"
    ]
    if not request.incidents and len(filtered_operations) == len(request.operations):
        return request
    return request.model_copy(
        update={
            "operations": filtered_operations,
            "incidents": [],
            "normalization_summary": (
                request.normalization_summary
                + " Inventory authority conflict was separated from physical incident handling."
            ),
        }
    )


def _pre_route_guard_requires_agent(request: NormalizedWarehouseRequest) -> bool:
    """Return the deterministic minimum that cannot safely enter Rule.

    This guard runs before either branch starts.  It does not inspect warehouse
    databases and it never performs a Rule-to-Agent fallback.  Exact typed IDs,
    ordinary system-context requirements, batch size, robot count, and solver
    complexity are intentionally not Agent triggers.
    """

    operation_types = {operation.operation_type for operation in request.operations}

    def has_noncanonical_operation_reference() -> bool:
        for operation in request.operations:
            # A canonical normalized operation ID is authoritative.  The raw
            # phrase may contain verbs or Korean particles (for example
            # "ORD-001을 출고해") and must not force an otherwise exact request
            # into Agent formulation after normalization has already resolved it.
            canonical = (operation.operation_id or "").strip().upper()
            if operation.operation_type == "OUTBOUND_ORDER" and re.fullmatch(r"ORD-\d{3,}", canonical):
                continue
            if operation.operation_type == "INBOUND_ITEM" and re.fullmatch(r"IN-\d{3,}", canonical):
                continue
            if operation.operation_type == "RECOVERY" and (
                re.fullmatch(r"R\d{3}", canonical) or canonical.startswith("REC-")
            ):
                continue

            raw = (operation.raw_reference or "").strip().upper()
            if not raw:
                return True
            if operation.operation_type == "OUTBOUND_ORDER" and re.fullmatch(r"ORD-\d{3,}", raw):
                continue
            if operation.operation_type == "INBOUND_ITEM" and re.fullmatch(r"IN-\d{3,}", raw):
                continue
            if operation.operation_type == "RECOVERY" and (
                re.fullmatch(r"R\d{3}", raw) or raw.startswith("REC-")
            ):
                continue
            return True
        return False

    semantic_references = bool(
        has_noncanonical_operation_reference()
        or request.constraints.excluded_robot_references
        or request.constraints.soft_avoid_edge_references
        or request.constraints.hard_block_edge_references
    )
    # Generic incidents always enter Agent formulation after the deterministic
    # safety gate because the workflow may need active-operation, map, and robot
    # context to decide whether a replan is required.  This is a pre-route rule,
    # not a late Rule-to-Agent fallback.
    incident_semantics = bool(request.incidents)
    return bool(
        "UNKNOWN" in operation_types
        or semantic_references
        or len(request.constraints.conditional_edge_policies) > 1
        or incident_semantics
        or request.user_clarification_questions
    )


def _requires_agent_formulation(request: NormalizedWarehouseRequest) -> bool:
    """Backward-compatible alias for the pre-route minimum guard."""

    return _pre_route_guard_requires_agent(request)


def _apply_human_responses(
    state: LaroGraphState,
    request: NormalizedWarehouseRequest,
) -> NormalizedWarehouseRequest:
    """Apply typed operator selections without allowing free-form ID rewrites."""

    operations = list(request.operations)
    incidents = list(request.incidents)
    constraints = request.constraints
    for value in state.get("human_responses", []):
        response = value if isinstance(value, HumanInteractionResponse) else HumanInteractionResponse.model_validate(value)
        if response.action not in {"SELECT", "APPROVE"}:
            continue
        if response.resolution_code == "CONFLICTING_ROBOT_CONSTRAINT":
            robot_id = next((item for item in response.selected_entity_ids if item.upper().startswith("R")), None)
            if not robot_id:
                continue
            if response.resolution_value == "EXCLUDE_ROBOT":
                constraints = constraints.model_copy(
                    update={"excluded_robot_ids": _dedupe([*constraints.excluded_robot_ids, robot_id])}
                )
            elif response.resolution_value == "ONLY_ROBOT":
                constraints = constraints.model_copy(
                    update={"excluded_robot_ids": [item for item in constraints.excluded_robot_ids if item != robot_id]}
                )
        elif response.resolution_code in {
            "AMBIGUOUS_OPERATION_REFERENCE",
            "ENTITY_REFERENCE_AMBIGUOUS",
            "ENTITY_REFERENCE_NOT_FOUND",
        }:
            selected = next(iter(response.selected_entity_ids), None) or response.resolution_value
            if selected and operations:
                operations[0] = operations[0].model_copy(
                    update={"operation_id": selected, "raw_reference": selected}
                )
        elif response.resolution_code == "UNKNOWN_OPERATION" and operations:
            allowed = {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY", "QUERY"}
            operation_type = str(response.resolution_value or "").upper()
            if operation_type in allowed:
                operations[0] = operations[0].model_copy(update={"operation_type": operation_type})
        elif response.resolution_code == "MISSING_INBOUND_QUANTITY" and operations:
            value = str(response.resolution_value or "").strip()
            if value.isdigit():
                current = operations[0].attributes.strip()
                operations[0] = operations[0].model_copy(
                    update={"attributes": (current + f"; operator_quantity={value}").strip("; ")}
                )
        elif response.resolution_code == "CONFLICTING_DESTINATION" and operations:
            selected = next(iter(response.selected_entity_ids), None) or response.resolution_value
            if selected:
                current = operations[0].attributes.strip()
                operations[0] = operations[0].model_copy(
                    update={"attributes": (current + f"; operator_destination={selected}").strip("; ")}
                )
        elif response.resolution_code and "::" in response.resolution_code:
            base, incident_id = response.resolution_code.split("::", 1)
            for index, incident in enumerate(incidents):
                if incident.incident_id != incident_id:
                    continue
                update: dict[str, object] = {}
                selected_ids = list(response.selected_entity_ids)
                if base == "INCIDENT_LOCATION_UNCERTAIN" and selected_ids:
                    update["affected_resource_ids"] = _dedupe(
                        [*incident.affected_resource_ids, *selected_ids]
                    )
                    update["affected_resource_references"] = []
                if base == "INCIDENT_IMPACT_UNCERTAIN":
                    if response.resolution_value == "CONFIRM_SAFE_AND_CONTINUE":
                        update["observed_effect"] = "TRAVERSABLE"
                        update["handling_mode"] = "AUTO_HANDLE"
                    elif response.resolution_value == "KEEP_SAFETY_HOLD":
                        update["observed_effect"] = "NOT_TRAVERSABLE"
                        update["handling_mode"] = "AUTO_HANDLE_AND_NOTIFY_HUMAN"
                if base == "LOADED_ROBOT_RECOVERY_DECISION":
                    update["reason_codes"] = _dedupe(
                        [*incident.reason_codes, f"OPERATOR_RECOVERY={response.resolution_value or response.selected_option_id}"]
                    )
                if update:
                    incidents[index] = incident.model_copy(update=update)
    return request.model_copy(
        update={"operations": operations, "incidents": incidents, "constraints": constraints}
    )


@observe_node(
    "request_router_llm",
    purpose="원본 자연어·구조화 입력을 한 번에 정규화하고 Rule 또는 Agent 경로를 실행 전에 확정",
    llm_used=True,
)
def request_router_llm_node(state: LaroGraphState) -> dict:
    """Normalize the request, run the pre-route HITL gate, then lock one branch."""

    code_rejection = _pre_router_code_rejection(state)
    if code_rejection is not None:
        return code_rejection

    structured_fast_path = _authoritative_structured_router_fallback(state, None)
    if structured_fast_path is not None:
        return structured_fast_path

    routing_workload = _routing_workload_context(state)
    workload_assessment = assess_workload_route(routing_workload, get_settings())
    generated_command_batch = _is_generated_command_batch(state)

    try:
        gateway = get_default_llm_gateway()
        routed = gateway.invoke_structured(
            system_prompt=(
                GENERATED_COMMAND_ROUTER_SYSTEM
                if generated_command_batch
                else REQUEST_ROUTER_SYSTEM
            ),
            user_payload={
                # Warehouse identity is authoritative API scope, not an LLM choice.
                "warehouse_id": state.get("warehouse_id"),
                "request_mode": state.get("request_mode"),
                "user_command": state.get("user_command"),
                "events": [_router_safe_event(event) for event in state.get("events", [])],
                "routing_workload": (
                    {
                        **routing_workload.model_dump(mode="json"),
                        "effective_operation_count": workload_assessment.effective_operation_count,
                        "operations_per_robot": workload_assessment.operations_per_robot,
                        "route_band": workload_assessment.band,
                    }
                    if routing_workload is not None
                    else None
                ),
                "generated_command_batch": generated_command_batch,
                "human_responses": [
                    value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                    for value in state.get("human_responses", [])
                ],
            },
            output_model=(
                GeneratedCommandRoutingDecision
                if generated_command_batch
                else RoutedNormalizedWarehouseRequest
            ),
            trace_name="LARO::request_router_llm",
            tags=["node:request_router_llm", f"prompt-v{REQUEST_ROUTER_PROMPT_VERSION}"],
            metadata={
                "laro_node": "request_router_llm",
                "warehouse_id": state.get("warehouse_id"),
                "simulation_id": state["simulation_id"],
                "event_count": len(state.get("events", [])),
                "planning_mode": state.get("planning_mode"),
                "tool_access": False,
                "human_response_count": len(state.get("human_responses", [])),
            },
        )
        if generated_command_batch and isinstance(routed, GeneratedCommandRoutingDecision):
            seed = _structured_normalized_request(state)
            router_request = seed.model_copy(
                update={
                    "constraints": routed.constraints,
                    "system_context_requirements": routed.system_context_requirements,
                    "policy_default_requirements": routed.policy_default_requirements,
                    "normalization_summary": routed.normalization_summary,
                    "raw_user_command": state.get("user_command"),
                }
            )
        else:
            router_request = routed.normalized_request.model_copy(
                update={
                    "raw_user_command": state.get("user_command")
                    if state.get("user_command") is not None
                    else routed.normalized_request.raw_user_command
                }
            )
        request = _canonicalize_normalized_request(router_request)
        request = _preserve_authoritative_structured_input(state, request)
        request = _apply_human_responses(state, request)
        request = _strip_nonphysical_data_conflict_incidents(state, request)
        recommendation = routed.recommendation
        if generated_command_batch:
            # Generated command batches already passed deterministic canonical
            # syntax validation.  Their operations stay immutable, while an
            # operator-authored user_command may still require clarification or
            # approval before the route is locked.
            request = request.model_copy(update={"user_clarification_questions": []})
            recommendation = recommendation.model_copy(
                update={
                    "reasons": [
                        *recommendation.reasons,
                        "Generated command operations remain authoritative; only the optional operator intent may open Human Review.",
                    ],
                }
            )
        responses = [
            value if isinstance(value, HumanInteractionResponse) else HumanInteractionResponse.model_validate(value)
            for value in state.get("human_responses", [])
        ]
        incident_response_plan = build_incident_response_plan(
            simulation_id=state["simulation_id"],
            incidents=list(request.incidents),
            human_responses=responses,
        )
        gate = resolve_request_gate(
            simulation_id=state["simulation_id"],
            request=request,
            recommendation=recommendation,
            original_user_command=state.get("user_command"),
            has_structured_events=bool(state.get("events")),
            authoritative_structured_input=state.get("structured_input") is not None,
            planning_mode=state.get("planning_mode", "llm_router"),
            requires_agent_guard=_pre_route_guard_requires_agent(request),
            human_responses=responses,
            incident_response_plan=incident_response_plan,
            workload_assessment=workload_assessment,
        )
        decision = None
        if gate.final_route in {"RULE_FORMULATION", "AGENT_FORMULATION"}:
            decision = FormulationDecision(
                route=gate.final_route,
                reasons=gate.reasons,
                required_context_nodes=_required_context_nodes(request),
            )

        summary = llm_summary(
            node_name="request_router_llm",
            prompt_version=REQUEST_ROUTER_PROMPT_VERSION,
            task_summary="원본 요청을 정규화하고 Pre-route HITL 또는 Rule/Agent 경로를 결정",
            input_summary=(
                f"events={len(state.get('events', []))}, "
                f"command={bool(state.get('user_command'))}, mode={state.get('planning_mode')}, "
                f"responses={len(responses)}"
            ),
            output_summary=(
                f"source={request.source}, operations={len(request.operations)}, incidents={len(request.incidents)}, "
                f"recommended={recommendation.route}, gate={gate.action}, "
                f"resolved={gate.final_route or 'HITL'}"
            ),
        )
        update = {
            "normalized_request": request,
            "request_gate_decision": gate,
            "incident_response_plan": incident_response_plan,
            "operator_notifications": list(incident_response_plan.notifications),
            "pending_human_interaction": gate.human_interaction,
            "input_rejection": gate.input_rejection,
            "workflow_hold": gate.workflow_hold,
            "llm_node_summaries": [summary],
            **trace_update("request_router_llm"),
        }
        if decision is not None:
            update["formulation_decision"] = decision
        if gate.final_route == "INCIDENT_RESPONSE":
            update["orchestration_plan"] = _incident_orchestration_plan(
                state, request, reasons=gate.reasons
            )
        return update
    except Exception as exc:
        fallback = _authoritative_structured_router_fallback(state, exc)
        if fallback is not None:
            return fallback
        return error_update(
            stage="request_router_llm",
            code="request_routing_failed",
            message=str(exc),
            retryable=True,
        )


@observe_node(
    "structured_request_normalizer",
    purpose="알려진 구조화 이벤트를 공통 NormalizedWarehouseRequest로 변환",
)
def structured_request_normalizer_node(state: LaroGraphState) -> dict:
    """Normalize structured events without an LLM for the rule baseline."""

    try:
        frozen = state.get("normalized_request_override")
        value = (
            frozen
            if isinstance(frozen, NormalizedWarehouseRequest)
            else NormalizedWarehouseRequest.model_validate(frozen)
            if frozen is not None
            else _structured_normalized_request(state)
        )
        responses = [
            item if isinstance(item, HumanInteractionResponse) else HumanInteractionResponse.model_validate(item)
            for item in state.get("human_responses", [])
        ]
        incident_response_plan = build_incident_response_plan(
            simulation_id=state["simulation_id"],
            incidents=list(value.incidents),
            human_responses=responses,
        )
        input_rejection = code_input_rejection(value)
        update = {
            "normalized_request": value,
            "incident_response_plan": incident_response_plan,
            "operator_notifications": list(incident_response_plan.notifications),
            "pending_human_interaction": incident_response_plan.pending_human_interaction,
            "input_rejection": input_rejection,
            **trace_update("structured_request_normalizer"),
        }
        if incident_response_plan.pending_human_interaction is not None:
            update["request_gate_decision"] = RequestGateDecision(
                action="REQUIRE_HUMAN_APPROVAL",
                final_route="INCIDENT_RESPONSE",
                reasons=["The dedicated incident-response route is locked before safety handling and HITL."],
                route_locked=True,
                human_interaction=incident_response_plan.pending_human_interaction,
            )
            update["orchestration_plan"] = _incident_orchestration_plan(
                state, value, reasons=update["request_gate_decision"].reasons
            )
        elif input_rejection is not None:
            update["request_gate_decision"] = RequestGateDecision(
                action="REJECT_INPUT",
                reasons=[input_rejection.message],
                input_rejection=input_rejection,
            )
        elif _incident_only_request(value):
            update["request_gate_decision"] = RequestGateDecision(
                action="HANDLE_INCIDENT",
                final_route="INCIDENT_RESPONSE",
                reasons=["The dedicated incident-response route is locked before automatic safety handling."],
                route_locked=True,
            )
            update["orchestration_plan"] = _incident_orchestration_plan(
                state, value, reasons=update["request_gate_decision"].reasons
            )
        return update
    except Exception as exc:
        return error_update(
            stage="structured_request_normalizer",
            code="structured_request_normalization_failed",
            message=str(exc),
        )


@observe_node(
    "input_normalizer_llm",
    purpose="자연어와 구조화 이벤트를 동일한 요청 스키마로 정규화",
    llm_used=True,
)
def input_normalizer_llm_node(state: LaroGraphState) -> dict:
    """Use one strict LLM call to normalize the entry request."""

    try:
        gateway = get_default_llm_gateway()
        result = gateway.invoke_structured(
            system_prompt=INPUT_NORMALIZER_SYSTEM,
            user_payload={
                "user_command": state.get("user_command"),
                "events": [_router_safe_event(event) for event in state.get("events", [])],
            },
            output_model=NormalizedWarehouseRequest,
            trace_name="LARO::input_normalizer_llm",
            tags=["node:input_normalizer_llm", f"prompt-v{NORMALIZER_PROMPT_VERSION}"],
            metadata={
                "laro_node": "input_normalizer_llm",
                "simulation_id": state["simulation_id"],
                "event_count": len(state.get("events", [])),
            },
        )
        result = result.model_copy(
            update={
                "raw_user_command": state.get("user_command")
                if state.get("user_command") is not None
                else result.raw_user_command
            }
        )
        result = _canonicalize_normalized_request(result)
        responses = [
            item if isinstance(item, HumanInteractionResponse) else HumanInteractionResponse.model_validate(item)
            for item in state.get("human_responses", [])
        ]
        incident_response_plan = build_incident_response_plan(
            simulation_id=state["simulation_id"],
            incidents=list(result.incidents),
            human_responses=responses,
        )
        input_rejection = code_input_rejection(result)
        summary = llm_summary(
            node_name="input_normalizer_llm",
            prompt_version=NORMALIZER_PROMPT_VERSION,
            task_summary="자연어·이벤트를 공통 창고 요청 계약으로 정규화",
            input_summary=f"events={len(state.get('events', []))}, command={bool(state.get('user_command'))}",
            output_summary=(
                f"source={result.source}, operations={len(result.operations)}, "
                f"system_context={len(result.system_context_requirements)}, "
                f"clarifications={len(result.user_clarification_questions)}, "
                f"policy_defaults={len(result.policy_default_requirements)}"
            ),
        )
        update = {
            "normalized_request": result,
            "incident_response_plan": incident_response_plan,
            "operator_notifications": list(incident_response_plan.notifications),
            "pending_human_interaction": incident_response_plan.pending_human_interaction,
            "input_rejection": input_rejection,
            "llm_node_summaries": [summary],
            **trace_update("input_normalizer_llm"),
        }
        if incident_response_plan.pending_human_interaction is not None:
            update["request_gate_decision"] = RequestGateDecision(
                action="REQUIRE_HUMAN_APPROVAL",
                final_route="INCIDENT_RESPONSE",
                reasons=["The dedicated incident-response route is locked before safety handling and HITL."],
                route_locked=True,
                human_interaction=incident_response_plan.pending_human_interaction,
            )
            update["orchestration_plan"] = _incident_orchestration_plan(
                state, result, reasons=update["request_gate_decision"].reasons
            )
        elif input_rejection is not None:
            update["request_gate_decision"] = RequestGateDecision(
                action="REJECT_INPUT",
                reasons=[input_rejection.message],
                input_rejection=input_rejection,
            )
        elif _incident_only_request(result):
            update["request_gate_decision"] = RequestGateDecision(
                action="HANDLE_INCIDENT",
                final_route="INCIDENT_RESPONSE",
                reasons=["The dedicated incident-response route is locked before automatic safety handling."],
                route_locked=True,
            )
            update["orchestration_plan"] = _incident_orchestration_plan(
                state, result, reasons=update["request_gate_decision"].reasons
            )
        return update
    except Exception as exc:
        return error_update(
            stage="input_normalizer_llm",
            code="input_normalization_failed",
            message=str(exc),
            retryable=True,
        )


@observe_node(
    "deterministic_formulation_supervisor",
    purpose="강제 Rule/Agent 입력의 의미 모호성을 검사하고 실행 전 단일 경로를 고정",
)
def deterministic_formulation_supervisor_node(state: LaroGraphState) -> dict:
    """Lock force_rule or force_agent before repository access."""

    try:
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        questions = _true_clarification_questions(request)
        if questions:
            decision = FormulationDecision(
                route="ASK_CLARIFICATION",
                reasons=["The structured request contains an ambiguity that system contexts cannot resolve."],
                clarification_questions=questions,
            )
        else:
            mode = state.get("planning_mode", "force_rule")
            if mode == "force_agent":
                decision = FormulationDecision(
                    route="AGENT_FORMULATION",
                    reasons=[
                        "force_agent selected the Agent branch before repository access; "
                        "structured canonical input did not require a router LLM call."
                    ],
                    required_context_nodes=_required_context_nodes(request),
                )
            elif _pre_route_guard_requires_agent(request):
                decision = FormulationDecision(
                    route="HUMAN_REVIEW",
                    reasons=[
                        "force_rule was selected, but semantic references remain that the "
                        "deterministic Rule branch is not authorized to interpret."
                    ],
                )
            else:
                decision = FormulationDecision(
                    route="RULE_FORMULATION",
                    reasons=["force_rule selected the deterministic Rule branch before execution."],
                    required_context_nodes=_required_context_nodes(request),
                )
        return _formulation_decision_update(state, decision, "deterministic_formulation_supervisor")
    except Exception as exc:
        return error_update(
            stage="deterministic_formulation_supervisor",
            code="deterministic_formulation_supervision_failed",
            message=str(exc),
        )


def _formulation_decision_update(
    state: LaroGraphState,
    decision: FormulationDecision,
    trace_name: str,
) -> dict:
    """Publish the semantic decision without building the workflow plan early."""

    update: dict = {"formulation_decision": decision, **trace_update(trace_name)}
    if decision.route == "ASK_CLARIFICATION":
        update["clarification"] = ClarificationResult(
            reason="The normalized request contains ambiguity that warehouse tools cannot resolve.",
            questions=decision.clarification_questions,
        )
    elif decision.route == "HUMAN_REVIEW":
        update["human_review"] = HumanReviewResult(
            reason="The formulation supervisor routed the request to human review.",
            details=decision.reasons,
        )
    return update

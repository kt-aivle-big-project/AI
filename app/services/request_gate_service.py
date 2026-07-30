"""Deterministic pre-route clarification and HITL policy.

The request-router LLM may recommend Rule or Agent and may point out an
ambiguity, but this service owns the final gate.  It uses only the incoming
request envelope and the normalized request; it never queries warehouse data or
changes authoritative structured identifiers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from app.domain.schemas import (
    FormulationRecommendation,
    HumanInteractionOption,
    HumanInteractionRequest,
    HumanInteractionResponse,
    IncidentResponsePlan,
    InputRejectionResult,
    NormalizedWarehouseRequest,
    RequestGateDecision,
    WorkflowHoldResult,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(simulation_id: str, reason_code: str, prompt: str) -> str:
    seed = f"{simulation_id}|PRE_ROUTE|{reason_code}|{prompt}"
    return "HITL-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()


def _has_response(responses: list[HumanInteractionResponse], reason_code: str) -> bool:
    return any(
        value.resolution_code == reason_code and value.action in {"SELECT", "APPROVE"}
        for value in responses
    )


def _latest_response(
    responses: list[HumanInteractionResponse],
    reason_code: str,
) -> HumanInteractionResponse | None:
    """Return the latest accepted operator response for one exception boundary."""

    return next(
        (
            value
            for value in reversed(responses)
            if value.resolution_code == reason_code and value.action in {"SELECT", "APPROVE"}
        ),
        None,
    )


def _has_conditional_policy(request: NormalizedWarehouseRequest) -> bool:
    """Return whether deterministic normalization found a typed conditional policy."""

    return bool(request.constraints.conditional_edge_policies)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in markers)


_CANONICAL_ORDER_ID = re.compile(r"^ORD-\d{3,}$", re.IGNORECASE)
_CANONICAL_INBOUND_ID = re.compile(r"^IN-\d{3,}$", re.IGNORECASE)
_CANONICAL_ROBOT_ID = re.compile(r"^R\d{3}$", re.IGNORECASE)
_CANONICAL_EDGE_ID = re.compile(r"^(?:H|V)\d+_\d+$", re.IGNORECASE)

# Only runtime/context facts are system-resolvable before formulation.
# Mission identity itself must already be expressed with canonical codes.
_CONTEXT_ONLY_ROUTER_REASON_CODES = {
    "MISSING_INVENTORY_CONTEXT",
    "MISSING_ROBOT_CONTEXT",
    "MISSING_MAP_CONTEXT",
    "MISSING_RUNTIME_CONTEXT",
    "MISSING_WAREHOUSE_CONTEXT",
}

_ANAPHORIC_REFERENCE_MARKERS = (
    "그 주문",
    "저 주문",
    "해당 주문",
    "그거",
    "that order",
    "that one",
)


def _canonical_operation_reference(operation_type: str, value: str) -> bool:
    """Return whether *value* is already a canonical typed identifier."""

    candidate = value.strip()
    if not candidate:
        return False
    if operation_type == "OUTBOUND_ORDER":
        return bool(_CANONICAL_ORDER_ID.fullmatch(candidate))
    if operation_type == "INBOUND_ITEM":
        return bool(_CANONICAL_INBOUND_ID.fullmatch(candidate))
    if operation_type == "RECOVERY":
        return bool(_CANONICAL_ROBOT_ID.fullmatch(candidate) or candidate.upper().startswith("REC-"))
    return False


def _noncanonical_operation_references(
    request: NormalizedWarehouseRequest,
) -> tuple[list[str], list[str]]:
    """Return invalid operation references and required identifier types.

    LARO does not infer a mission identity from item names or descriptive order
    phrases.  Orders, inbound receipts, and recovery targets must use canonical
    codes supplied by the upstream WMS/WES or operator UI.
    """

    invalid: list[str] = []
    required: list[str] = []
    for operation in request.operations:
        if operation.operation_type in {"QUERY", "INCIDENT", "UNKNOWN"}:
            continue
        value = (operation.operation_id or operation.raw_reference or "").strip()
        if _canonical_operation_reference(operation.operation_type, value):
            continue
        invalid.append(operation.raw_reference or operation.operation_id)
        required.append(
            {
                "OUTBOUND_ORDER": "order_id (ORD-###)",
                "INBOUND_ITEM": "inbound_id (IN-###)",
                "RECOVERY": "robot_id (R###) or recovery_id (REC-*)",
            }.get(operation.operation_type, "canonical operation identifier")
        )
    return list(dict.fromkeys(invalid)), list(dict.fromkeys(required))


def _noncanonical_resource_references(
    request: NormalizedWarehouseRequest,
) -> tuple[list[str], list[str]]:
    """Return prose entity references that must be replaced by resource codes.

    An exact canonical token emitted in a reference field is a model-formatting
    issue, not invalid operator input.  The normalizer restores those tokens to
    the typed ID fields, while this gate rejects only genuinely descriptive
    aliases.
    """

    robot_refs = [
        value
        for value in request.constraints.excluded_robot_references
        if not _CANONICAL_ROBOT_ID.fullmatch(value.strip())
    ]
    soft_refs = [
        value
        for value in request.constraints.soft_avoid_edge_references
        if not _CANONICAL_EDGE_ID.fullmatch(value.strip())
    ]
    hard_refs = [
        value
        for value in request.constraints.hard_block_edge_references
        if not _CANONICAL_EDGE_ID.fullmatch(value.strip())
    ]
    invalid = [*robot_refs, *soft_refs, *hard_refs]
    required: list[str] = []
    if robot_refs:
        required.append("robot_id (R###)")
    if soft_refs or hard_refs:
        required.append("edge_id (H#_# or V#_#)")
    return list(dict.fromkeys(invalid)), list(dict.fromkeys(required))


def _router_clarification_is_context_only(
    *,
    recommendation: FormulationRecommendation,
) -> bool:
    """Return whether the LLM asked for facts owned by system repositories."""

    if recommendation.gate_action != "ASK_CLARIFICATION":
        return False
    return (recommendation.reason_code or "").strip().upper() in _CONTEXT_ONLY_ROUTER_REASON_CODES


def _rejection(
    *,
    reason_code: str,
    message: str,
    invalid_references: list[str] | None = None,
    required_identifier_types: list[str] | None = None,
    reasons: list[str] | None = None,
) -> RequestGateDecision:
    """Create a non-HITL terminal gate for invalid mission input."""

    return RequestGateDecision(
        action="REJECT_INPUT",
        reasons=list(dict.fromkeys([*(reasons or []), message])),
        input_rejection=InputRejectionResult(
            reason_code=reason_code,
            message=message,
            invalid_references=list(dict.fromkeys(invalid_references or [])),
            required_identifier_types=list(dict.fromkeys(required_identifier_types or [])),
        ),
    )




def _command_requires_canonical_operation_id(request: NormalizedWarehouseRequest) -> InputRejectionResult | None:
    """Return a stable rejection when an execution request lacks its operation code.

    The LLM may label the same item-name request as OUTBOUND_ORDER, UNKNOWN, or
    HUMAN_REVIEW across repeats.  The code-first boundary must not inherit that
    variance.  We therefore derive the rejection reason from the original command
    and the presence of canonical IDs, not from the model's chosen operation type.
    """

    command = (request.raw_user_command or "").strip()
    if not command:
        return None
    folded = command.casefold()
    canonical_orders = {
        operation.operation_id.upper()
        for operation in request.operations
        if _CANONICAL_ORDER_ID.fullmatch((operation.operation_id or "").strip())
    }
    canonical_inbounds = {
        operation.operation_id.upper()
        for operation in request.operations
        if _CANONICAL_INBOUND_ID.fullmatch((operation.operation_id or "").strip())
    }
    canonical_recoveries = {
        operation.operation_id.upper()
        for operation in request.operations
        if _CANONICAL_ROBOT_ID.fullmatch((operation.operation_id or "").strip())
        or (operation.operation_id or "").upper().startswith("REC-")
    }

    outbound_intent = (
        any(marker in folded for marker in ("주문", "출고", "order", "fulfill", "ship"))
        and any(marker in folded for marker in ("처리", "실행", "출고", "배정", "process", "fulfill", "ship"))
    )
    inbound_intent = (
        any(marker in folded for marker in ("입고", "inbound", "putaway"))
        and any(marker in folded for marker in ("처리", "적치", "배정", "process", "putaway"))
    )
    recovery_intent = (
        any(marker in folded for marker in ("복구", "고장", "recovery", "recover", "fault"))
        and any(marker in folded for marker in ("처리", "복구", "실행", "recover", "process"))
    )

    if outbound_intent and not canonical_orders:
        return InputRejectionResult(
            reason_code="CANONICAL_OPERATION_ID_REQUIRED",
            message="Executable outbound work requires order_id in the form ORD-###.",
            invalid_references=[command],
            required_identifier_types=["order_id (ORD-###)"],
        )
    if inbound_intent and not canonical_inbounds:
        return InputRejectionResult(
            reason_code="CANONICAL_INBOUND_ID_REQUIRED",
            message="Executable inbound work requires inbound_id in the form IN-###.",
            invalid_references=[command],
            required_identifier_types=["inbound_id (IN-###)"],
        )
    if recovery_intent and not canonical_recoveries:
        return InputRejectionResult(
            reason_code="CANONICAL_RECOVERY_ID_REQUIRED",
            message="Executable recovery work requires robot_id R### or recovery_id REC-*.",
            invalid_references=[command],
            required_identifier_types=["robot_id (R###) or recovery_id (REC-*)"],
        )
    return None

def code_input_rejection(
    request: NormalizedWarehouseRequest,
) -> InputRejectionResult | None:
    """Validate the code-first mission identity contract without Tool access."""

    command_rejection = _command_requires_canonical_operation_id(request)
    if command_rejection is not None:
        return command_rejection

    invalid_operations, required_operation_ids = _noncanonical_operation_references(request)
    if invalid_operations:
        return InputRejectionResult(
            reason_code="CANONICAL_OPERATION_ID_REQUIRED",
            message=(
                "Mission operations must use canonical IDs. Item names or descriptive order phrases "
                "are not resolved into executable orders."
            ),
            invalid_references=invalid_operations,
            required_identifier_types=required_operation_ids,
        )
    invalid_resources, required_resource_ids = _noncanonical_resource_references(request)
    if invalid_resources:
        return InputRejectionResult(
            reason_code="CANONICAL_RESOURCE_ID_REQUIRED",
            message="Robot, edge, node, and destination constraints must use canonical resource IDs.",
            invalid_references=invalid_resources,
            required_identifier_types=required_resource_ids,
        )
    unknowns = [op.operation_id for op in request.operations if op.operation_type == "UNKNOWN"]
    if unknowns:
        return InputRejectionResult(
            reason_code="UNSUPPORTED_OPERATION_TYPE",
            message="The request contains an unsupported operation or event type.",
            invalid_references=unknowns,
            required_identifier_types=["supported event type and canonical operation ID"],
        )
    return None


def _robot_conflict(text: str) -> str | None:
    ids = sorted(set(re.findall(r"(?<![A-Z0-9])R\d{3}(?![A-Z0-9])", text.upper())))
    exclude_markers = ("제외", "빼", "사용하지", "exclude", "without")
    only_markers = ("만 사용", "만 써", "오직", "only use", "use only")
    if not ids or not _contains_any(text, exclude_markers) or not _contains_any(text, only_markers):
        return None
    if len(ids) == 1:
        return ids[0]
    for robot_id in ids:
        if len(re.findall(re.escape(robot_id), text, flags=re.I)) >= 2:
            return robot_id
    return None




def _structured_text_id_conflict(
    command: str,
    request: NormalizedWarehouseRequest,
) -> tuple[list[str], list[str]] | None:
    """Return authoritative and contradictory order IDs visible in the envelope."""

    if not command:
        return None
    structured_ids = sorted({
        operation.operation_id
        for operation in request.operations
        if operation.operation_type == "OUTBOUND_ORDER"
    })
    mentioned_ids = sorted(set(re.findall(r"(?<![A-Z0-9])ORD-\d{3,}(?![A-Z0-9])", command.upper())))
    contradictory = [value for value in mentioned_ids if value not in structured_ids]
    return (structured_ids, contradictory) if structured_ids and contradictory else None


def _conflicting_destinations(command: str) -> list[str]:
    """Detect two or more explicitly asserted outbound destinations."""

    values = sorted(set(re.findall(r"(?<![A-Z0-9])O_[A-Z](?![A-Z0-9])", command.upper())))
    if len(values) < 2:
        return []
    markers = ("최종 목적지", "고정", "보내", "destination", "deliver")
    return values if _contains_any(command, markers) else []


def _inventory_data_conflict(command: str) -> str | None:
    """Detect an explicitly reported DB-versus-sensor quantity conflict."""

    folded = command.casefold()
    if not (
        ("db" in folded or "postgres" in folded or "시스템 재고" in command)
        and ("sensor" in folded or "센서" in command or "실사" in command)
        and any(marker in command for marker in ("불일치", "다르", "차이"))
    ):
        return None
    match = re.search(r"(?<![A-Z0-9])(?:K\d+_\d+(?:-L[123])?|ITEM_[A-Z0-9_]+)(?![A-Z0-9])", command.upper())
    return match.group(0) if match else "inventory-record"


def _committed_task_cancellation(command: str) -> str | None:
    """Detect cancellation of work that has already crossed a physical commitment point."""

    folded = command.casefold()
    if not any(marker in folded for marker in ("취소", "cancel")):
        return None
    committed = any(
        marker in folded
        for marker in ("이미 pickup", "이미 pick", "피킹 완료", "집은 상태", "적재한 상태", "already picked", "already loaded")
    )
    if not committed:
        return None
    match = re.search(r"(?<![A-Z0-9])ORD-\d{3,}(?![A-Z0-9])", command.upper())
    return match.group(0) if match else None


def _destination_override_request(command: str) -> tuple[str | None, list[str]] | None:
    """Detect an explicit request to substitute a contract destination."""

    folded = command.casefold()
    if not any(marker in folded for marker in ("대체", "변경", "override", "substitute")):
        return None
    destinations = sorted(set(re.findall(r"(?<![A-Z0-9])O_[A-Z](?![A-Z0-9])", command.upper())))
    if len(destinations) < 2:
        return None
    order = re.search(r"(?<![A-Z0-9])ORD-\d{3,}(?![A-Z0-9])", command.upper())
    return (order.group(0) if order else None, destinations)


def _missing_inbound_quantity(command: str, has_structured_events: bool) -> bool:
    """Detect a free-form inbound request with no quantity or handling-unit ID."""

    if has_structured_events or not _contains_any(command, ("입고", "putaway", "inbound")):
        return False
    return re.search(r"\d+", command) is None

def _interaction(
    *,
    simulation_id: str,
    kind: str,
    reason_code: str,
    headline: str,
    prompt: str,
    options: list[HumanInteractionOption] | None = None,
    recommended_option_id: str | None = None,
    default_action: str = "HOLD",
    context_summary: str = "",
) -> HumanInteractionRequest:
    return HumanInteractionRequest(
        interaction_id=_id(simulation_id, reason_code, prompt),
        kind=kind,  # type: ignore[arg-type]
        stage="PRE_ROUTE",
        reason_code=reason_code,
        headline=headline,
        prompt=prompt,
        options=options or [],
        recommended_option_id=recommended_option_id,
        default_action=default_action,  # type: ignore[arg-type]
        route_locked=False,
        context_summary=context_summary,
        created_at=_utc_now(),
    )


def _incident_only(request: NormalizedWarehouseRequest) -> bool:
    """Return whether the request contains only generic incident operations."""

    return bool(request.operations) and all(
        operation.operation_type == "INCIDENT" for operation in request.operations
    )


def resolve_request_gate(
    *,
    simulation_id: str,
    request: NormalizedWarehouseRequest,
    recommendation: FormulationRecommendation,
    original_user_command: str | None,
    has_structured_events: bool,
    planning_mode: str,
    requires_agent_guard: bool,
    human_responses: list[HumanInteractionResponse],
    incident_response_plan: IncidentResponsePlan | None = None,
) -> RequestGateDecision:
    """Resolve invalid input/HITL and lock exactly one execution route before work begins."""

    command = (original_user_command or request.raw_user_command or "").strip()
    pure_structured = has_structured_events and not command
    reasons = list(recommendation.reasons)

    # Operational incidents use a dedicated immutable execution route.  This
    # preserves the same one-time route-lock invariant as Rule and Agent while
    # keeping incident response outside both formulation branches.
    if incident_response_plan is not None and incident_response_plan.pending_human_interaction is not None:
        interaction = incident_response_plan.pending_human_interaction.model_copy(
            update={
                "route_locked": True,
                "resume_route": "INCIDENT_RESPONSE",
            }
        )
        return RequestGateDecision(
            action="REQUIRE_HUMAN_APPROVAL",
            final_route="INCIDENT_RESPONSE",
            reasons=[
                *reasons,
                "The dedicated incident-response route was locked before immediate safety handling and HITL pause.",
            ],
            route_locked=True,
            human_interaction=interaction,
        )

    if incident_response_plan is not None and _incident_only(request):
        return RequestGateDecision(
            action="HANDLE_INCIDENT",
            final_route="INCIDENT_RESPONSE",
            reasons=[
                *reasons,
                "The request contains only operational incidents; the dedicated incident-response route is locked before safety actions execute.",
            ],
            route_locked=True,
        )

    # Stabilize code-first rejection independently of how the LLM labeled the
    # unresolved operation.  The same item-name request must always yield the
    # same contract-level reason code.
    command_rejection = _command_requires_canonical_operation_id(request)
    if command_rejection is not None:
        return RequestGateDecision(
            action="REJECT_INPUT",
            reasons=[*reasons, command_rejection.message],
            input_rejection=command_rejection,
        )

    # HOLD_AND_RECOUNT is an accountable terminal exception choice, not a cue
    # to rerun Agent retrieval with the unresolved inventory conflict.
    conflict_response = _latest_response(human_responses, "AUTHORITATIVE_DATA_CONFLICT")
    if conflict_response is not None and (
        conflict_response.selected_option_id == "HOLD_AND_RECOUNT"
        or conflict_response.resolution_value == "HOLD_AND_RECOUNT"
    ):
        return RequestGateDecision(
            action="HOLD_WORKFLOW",
            reasons=[*reasons, "The operator selected recount before any further automation."],
            workflow_hold=WorkflowHoldResult(
                reason_code="AUTHORITATIVE_DATA_CONFLICT",
                message="Automation is held until the conflicting inventory sources are reconciled.",
                selected_option_id="HOLD_AND_RECOUNT",
                required_actions=[
                    "HOLD_AFFECTED_ORDER",
                    "EXCLUDE_STOCK_FROM_ALLOCATION",
                    "CREATE_RECOUNT_WORK_ITEM",
                ],
            ),
        )

    conditional_policy = _has_conditional_policy(request)
    simple_conditional_policy = len(request.constraints.conditional_edge_policies) == 1

    invalid_operations, required_operation_ids = _noncanonical_operation_references(request)
    if invalid_operations:
        return _rejection(
            reason_code="CANONICAL_OPERATION_ID_REQUIRED",
            message=(
                "Mission operations must use canonical IDs. Item names or descriptive order phrases "
                "are not resolved into executable orders."
            ),
            invalid_references=invalid_operations,
            required_identifier_types=required_operation_ids,
            reasons=reasons,
        )

    invalid_resources, required_resource_ids = _noncanonical_resource_references(request)
    if invalid_resources:
        return _rejection(
            reason_code="CANONICAL_RESOURCE_ID_REQUIRED",
            message="Robot, edge, node, and destination constraints must use canonical resource IDs.",
            invalid_references=invalid_resources,
            required_identifier_types=required_resource_ids,
            reasons=reasons,
        )

    # Unsupported event types are malformed input, not a human decision checkpoint.
    unknowns = [op.operation_id for op in request.operations if op.operation_type == "UNKNOWN"]
    if unknowns:
        return _rejection(
            reason_code="UNSUPPORTED_OPERATION_TYPE",
            message="The request contains an unsupported operation or event type.",
            invalid_references=unknowns,
            required_identifier_types=["supported event type and canonical operation ID"],
            reasons=reasons,
        )

    id_conflict = _structured_text_id_conflict(command, request)
    if id_conflict:
        structured_ids, contradictory_ids = id_conflict
        return _rejection(
            reason_code="AUTHORITATIVE_ID_CONFLICT",
            message=(
                f"Structured order IDs {', '.join(structured_ids)} conflict with "
                f"natural-language IDs {', '.join(contradictory_ids)}. Submit one corrected request."
            ),
            invalid_references=contradictory_ids,
            required_identifier_types=["one consistent order_id set"],
            reasons=reasons,
        )

    destinations = _conflicting_destinations(command)
    # Two destination codes are invalid unless the request explicitly asks for
    # an authorized substitution.  That exception is handled by the approval
    # gate below instead of being rejected as malformed input.
    if destinations and _destination_override_request(command) is None:
        return _rejection(
            reason_code="CONFLICTING_DESTINATION",
            message=f"The request asserts multiple final destinations: {', '.join(destinations)}.",
            invalid_references=destinations,
            required_identifier_types=["one destination node ID"],
            reasons=reasons,
        )

    if _missing_inbound_quantity(command, has_structured_events):
        return _rejection(
            reason_code="CANONICAL_INBOUND_ID_REQUIRED",
            message="Free-form inbound requests are unsupported. Provide an inbound_id (IN-###).",
            required_identifier_types=["inbound_id (IN-###)"],
            reasons=reasons,
        )

    robot_id = _robot_conflict(command)
    if robot_id:
        return _rejection(
            reason_code="CONFLICTING_ROBOT_CONSTRAINT",
            message=f"{robot_id} is both excluded and exclusively required.",
            invalid_references=[robot_id],
            required_identifier_types=["one consistent robot constraint"],
            reasons=reasons,
        )

    vague_markers = ("그 주문", "저 주문", "해당 주문", "그거 처리", "that order")
    if _contains_any(command, vague_markers) and not has_structured_events:
        return _rejection(
            reason_code="CANONICAL_OPERATION_ID_REQUIRED",
            message="An anaphoric order reference cannot be executed. Provide order_id (ORD-###).",
            invalid_references=[command],
            required_identifier_types=["order_id (ORD-###)"],
            reasons=reasons,
        )

    inventory_conflict = _inventory_data_conflict(command)
    if inventory_conflict and not _has_response(human_responses, "AUTHORITATIVE_DATA_CONFLICT"):
        prompt = f"{inventory_conflict}의 시스템 재고와 현장 관측값이 다릅니다. 처리 방식을 승인해 주세요."
        interaction = _interaction(
            simulation_id=simulation_id,
            kind="APPROVAL",
            reason_code="AUTHORITATIVE_DATA_CONFLICT",
            headline="권위 데이터 불일치 검토",
            prompt=prompt,
            options=[
                HumanInteractionOption(
                    option_id="HOLD_AND_RECOUNT",
                    label="작업 보류 후 재실사",
                    resolution_value="HOLD_AND_RECOUNT",
                    impact_summary="자동 할당을 중단하고 재고 정합성 확인을 요청합니다.",
                ),
                HumanInteractionOption(
                    option_id="USE_CONFIRMED_SENSOR_QUANTITY",
                    label="확인된 현장 수량 사용",
                    resolution_value="USE_SENSOR_QUANTITY",
                    impact_summary="감사 로그와 재고 보정 절차가 필요합니다.",
                ),
                HumanInteractionOption(
                    option_id="USE_ALTERNATIVE_STOCK",
                    label="다른 재고 후보 사용",
                    resolution_value="USE_ALTERNATIVE_STOCK",
                    impact_summary="현재 충돌 레코드를 제외하고 다른 재고를 조회합니다.",
                ),
            ],
            recommended_option_id="HOLD_AND_RECOUNT",
            default_action="HOLD",
            context_summary="Two authoritative sources report different inventory quantities.",
        )
        return RequestGateDecision(
            action="REQUIRE_HUMAN_APPROVAL",
            recommended_route="AGENT_FORMULATION",
            reasons=[*reasons, "Authoritative inventory sources conflict."],
            human_interaction=interaction,
        )

    cancelled_order = _committed_task_cancellation(command)
    if cancelled_order and not _has_response(human_responses, "COMMITTED_TASK_CANCELLATION"):
        prompt = f"{cancelled_order}은 이미 Pickup이 완료되었습니다. 취소 후 물품 처리 방식을 선택해 주세요."
        interaction = _interaction(
            simulation_id=simulation_id,
            kind="APPROVAL",
            reason_code="COMMITTED_TASK_CANCELLATION",
            headline="실행 확정 작업 취소 승인",
            prompt=prompt,
            options=[
                HumanInteractionOption(option_id="RETURN_TO_SOURCE", label="원위치 반환", resolution_value="RETURN_TO_SOURCE"),
                HumanInteractionOption(option_id="MOVE_TO_BUFFER", label="버퍼 적치", resolution_value="MOVE_TO_BUFFER"),
                HumanInteractionOption(option_id="COMPLETE_AND_RETURN", label="출고 완료 후 반품 처리", resolution_value="COMPLETE_AND_RETURN"),
            ],
            recommended_option_id="MOVE_TO_BUFFER",
            default_action="HOLD",
            context_summary="The cancellation occurs after a physical pickup commitment.",
        )
        return RequestGateDecision(
            action="REQUIRE_HUMAN_APPROVAL",
            recommended_route="AGENT_FORMULATION",
            reasons=[*reasons, "A committed task cancellation requires an accountable recovery choice."],
            human_interaction=interaction,
        )

    destination_override = _destination_override_request(command)
    if destination_override and not _has_response(human_responses, "DESTINATION_OVERRIDE_APPROVAL"):
        order_id, destinations = destination_override
        prompt = (
            f"{order_id or '주문'}의 계약 목적지를 {destinations[0]}에서 {destinations[1]}로 변경하려면 승인이 필요합니다."
        )
        interaction = _interaction(
            simulation_id=simulation_id,
            kind="APPROVAL",
            reason_code="DESTINATION_OVERRIDE_APPROVAL",
            headline="대체 목적지 승인",
            prompt=prompt,
            options=[
                HumanInteractionOption(option_id="WAIT_FOR_CONTRACT_DESTINATION", label=f"{destinations[0]} 복구 대기", resolution_value="WAIT"),
                HumanInteractionOption(option_id="APPROVE_ALTERNATIVE_DESTINATION", label=f"{destinations[1]} 대체 승인", selected_entity_ids=[destinations[1]], resolution_value=destinations[1]),
                HumanInteractionOption(option_id="HOLD_ORDER", label="주문 보류", resolution_value="HOLD_ORDER"),
            ],
            recommended_option_id="WAIT_FOR_CONTRACT_DESTINATION",
            default_action="HOLD",
            context_summary="Changing a contractual delivery destination crosses an authorization boundary.",
        )
        return RequestGateDecision(
            action="REQUIRE_HUMAN_APPROVAL",
            recommended_route="AGENT_FORMULATION",
            reasons=[*reasons, "Alternative destination requires explicit authorization."],
            human_interaction=interaction,
        )

    safety_markers = (
        "안전 규칙 무시",
        "안전검사 생략",
        "차단 통로 무시",
        "재고 확인 생략",
        "검증 생략",
        "ignore safety",
        "bypass policy",
        "skip inventory check",
    )
    if _contains_any(command, safety_markers) and not _has_response(
        human_responses, "SAFETY_OVERRIDE_REQUEST"
    ):
        prompt = "요청이 안전·재고 검증 우회를 포함합니다. 운영자 예외 검토를 진행할까요?"
        interaction = _interaction(
            simulation_id=simulation_id,
            kind="APPROVAL",
            reason_code="SAFETY_OVERRIDE_REQUEST",
            headline="안전 정책 예외 승인 필요",
            prompt=prompt,
            options=[
                HumanInteractionOption(
                    option_id="KEEP_SAFETY_POLICY",
                    label="안전 정책 유지",
                    resolution_value="KEEP_POLICY",
                    impact_summary="모든 결정론적 안전·재고 검증을 유지합니다.",
                ),
                HumanInteractionOption(
                    option_id="REQUEST_EXCEPTION_REVIEW",
                    label="관리자 예외 검토",
                    resolution_value="APPROVE_EXCEPTION",
                    impact_summary="승인 기록을 남기지만 안전 Validator 자체는 계속 실행됩니다.",
                ),
            ],
            recommended_option_id="KEEP_SAFETY_POLICY",
            default_action="HOLD",
            context_summary="An LLM cannot waive deterministic safety validation.",
        )
        return RequestGateDecision(
            action="REQUIRE_HUMAN_APPROVAL",
            recommended_route="AGENT_FORMULATION",
            reasons=[*reasons, "The request crosses a safety or authorization boundary."],
            human_interaction=interaction,
        )

    context_only_router_question = _router_clarification_is_context_only(
        recommendation=recommendation,
    )
    if pure_structured and recommendation.gate_action == "ASK_CLARIFICATION":
        reasons.append(
            "The router requested clarification for a complete structured event envelope; the question was suppressed."
        )
    elif conditional_policy and recommendation.gate_action == "ASK_CLARIFICATION":
        reasons.append(
            "The router rejected a fully typed conditional edge policy; the question was suppressed and the deterministic policy evaluator will resolve it from runtime evidence."
        )
    elif context_only_router_question:
        reasons.append(
            "The router asked for repository-owned runtime context; the question was suppressed and the system will fetch it."
        )
    elif recommendation.gate_action == "ASK_CLARIFICATION":
        return _rejection(
            reason_code=recommendation.reason_code or "INVALID_MISSION_INPUT",
            message=(
                recommendation.prompt
                or "The mission request is incomplete or ambiguous. Submit a corrected code-based request."
            ),
            required_identifier_types=["canonical operation and resource IDs"],
            reasons=reasons,
        )
    elif (
        recommendation.gate_action == "REQUIRE_HUMAN_APPROVAL"
        and recommendation.reason_code
        and _has_response(human_responses, recommendation.reason_code)
    ):
        reasons.append(f"Resolved exception approval: {recommendation.reason_code}.")
    elif recommendation.gate_action == "REQUIRE_HUMAN_APPROVAL" and conditional_policy:
        reasons.append(
            "The router requested approval for a typed conditional policy without a responsibility boundary; the Rule policy evaluator will resolve it without HITL."
        )
    elif recommendation.gate_action == "REQUIRE_HUMAN_APPROVAL":
        # HITL is exception-only and must be opened by a deterministic policy
        # boundary (incident recovery, safety override, deferral, SLA conflict).
        return _rejection(
            reason_code="UNSUPPORTED_HITL_REQUEST",
            message="The router requested approval without a recognized deterministic exception boundary.",
            reasons=reasons,
        )

    route = recommendation.route
    if route == "HUMAN_REVIEW":
        route = "AGENT_FORMULATION"
        reasons.append("Legacy HUMAN_REVIEW recommendation was normalized to Agent after gate clearance.")

    # A trusted, complete structured event envelope is deterministic.  Free-text
    # payload metadata is not allowed to upgrade it to Agent, which prevents
    # prompt-injection notes from influencing the execution branch.
    if planning_mode == "llm_router" and pure_structured:
        route = "RULE_FORMULATION"
        reasons.append(
            "Complete structured events with canonical identifiers are locked to Rule; "
            "untrusted event metadata cannot influence routing."
        )

    if planning_mode == "force_agent":
        route = "AGENT_FORMULATION"
        reasons.append("force_agent selected the Agent branch before execution.")
    elif planning_mode == "force_rule":
        route = "RULE_FORMULATION"
        reasons.append("force_rule selected the Rule branch before execution.")
    elif conditional_policy and not simple_conditional_policy:
        route = "AGENT_FORMULATION"
        reasons.append(
            "Multiple conditional policies require semantic trade-off composition and are routed to Agent."
        )
    elif simple_conditional_policy:
        route = "RULE_FORMULATION"
        reasons.append(
            "A single typed conditional edge policy is evaluated deterministically from runtime evidence in the Rule branch."
        )
    elif route == "RULE_FORMULATION" and requires_agent_guard:
        route = "AGENT_FORMULATION"
        reasons.append("Unresolved semantic references require Agent formulation before branch entry.")

    return RequestGateDecision(
        action="ROUTE_RULE" if route == "RULE_FORMULATION" else "ROUTE_AGENT",
        recommended_route=(
            recommendation.route
            if recommendation.route in {"RULE_FORMULATION", "AGENT_FORMULATION"}
            else None
        ),
        final_route=route,
        reasons=list(dict.fromkeys(reasons)),
        route_locked=True,
    )

"""Impact-based handling for generic operational incidents.

The workflow intentionally does not maintain a detailed taxonomy such as
BOX_SPILLED, FORKLIFT_INCIDENT, PERSON_IN_AISLE, or FALLEN_PALLET.  Any report
is reduced to operational impact and one of three dispositions:

* AUTO_HANDLE: deterministic safety/runtime response only.
* AUTO_HANDLE_AND_NOTIFY_HUMAN: automatic response plus a non-blocking work notice.
* REQUIRE_HUMAN_DECISION: conservative safety action first, then blocking HITL.

This service creates immediate actions as PLANNED.  The graph executor marks them
APPLIED in the current planning overlay before any HITL pause.  A production
Redis/WCS adapter must perform the corresponding atomic runtime write and return
a committed runtime version.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from app.domain.schemas import (
    HumanInteractionOption,
    HumanInteractionRequest,
    HumanInteractionResponse,
    IncidentResponseAction,
    IncidentResponsePlan,
    OperationalIncidentImpact,
    OperatorNotification,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "|".join(parts)
    return f"{prefix}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()


def _contains(text: str, values: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(value.casefold() in folded for value in values)


def _exact_resource_ids(incident: OperationalIncidentImpact) -> list[str]:
    values = list(incident.affected_resource_ids)
    pattern = re.compile(
        r"^(?:R\d{3}|H\d+_\d+|V\d+_\d+|K\d+_\d+|O_[A-Z]|I_[A-Za-z]|C\d{2}|RJ\d{2})$",
        re.I,
    )
    values.extend(
        value for value in incident.affected_resource_references if pattern.fullmatch(value.strip())
    )
    return list(dict.fromkeys(values))


def _response_for(
    responses: list[HumanInteractionResponse],
    incident_id: str,
) -> HumanInteractionResponse | None:
    for response in reversed(responses):
        if response.resolution_code and response.resolution_code.endswith(f"::{incident_id}"):
            if response.action in {"SELECT", "APPROVE"}:
                return response
    return None


def _derive_incident(incident: OperationalIncidentImpact) -> OperationalIncidentImpact:
    """Resolve a conservative impact disposition without classifying event names."""

    text = incident.description
    exact_ids = _exact_resource_ids(incident)
    scope = incident.scope
    if scope == "UNKNOWN":
        if any(value.upper().startswith("R") and re.fullmatch(r"R\d{3}", value.upper()) for value in exact_ids):
            scope = "ROBOT"
        elif exact_ids or incident.affected_resource_references:
            scope = "MAP_RESOURCE"

    observed = incident.observed_effect
    if observed == "UNKNOWN":
        uncertainty_markers = (
            "여부 미확인",
            "확인되지 않",
            "알 수 없",
            "불명확",
            "unknown",
            "not confirmed",
            "uncertain",
        )
        if _contains(text, uncertainty_markers):
            observed = "UNKNOWN"
        elif _contains(text, ("통행 불가", "지나갈 수 없", "완전 차단", "blocked", "not traversable")):
            observed = "NOT_TRAVERSABLE"
        elif _contains(text, ("통행 가능", "지나갈 수 있", "passable", "traversable")):
            observed = "TRAVERSABLE"
        elif _contains(text, ("혼잡", "일부 통행", "degraded", "partially blocked")):
            observed = "DEGRADED"

    robot_operability = incident.robot_operability
    if scope == "ROBOT" and robot_operability in {"UNKNOWN", "NOT_APPLICABLE"}:
        if _contains(text, ("고장", "멈췄", "정지", "fault", "stopped", "offline")):
            robot_operability = "FAULTED"

    load_state = incident.load_state
    if load_state in {"UNKNOWN", "NOT_APPLICABLE"} and _contains(
        text,
        ("물건을 든 채", "적재한 채", "짐을 싣고", "loaded", "carrying load"),
    ):
        load_state = "LOADED"

    physical = incident.physical_intervention_required or _contains(
        text,
        ("쏟", "떨어", "장애물", "박스", "팔레트", "사람", "정리", "회수", "spill", "obstacle"),
    )
    location_uncertain = not exact_ids
    impact_uncertain = (
        (scope == "MAP_RESOURCE" and observed == "UNKNOWN")
        or (scope == "ROBOT" and robot_operability == "UNKNOWN")
    )
    loaded_fault = scope == "ROBOT" and robot_operability == "FAULTED" and load_state == "LOADED"

    reason_codes = list(incident.reason_codes)
    handling_mode = incident.handling_mode
    action = incident.immediate_safety_action
    decision_reason = incident.operator_decision_reason
    decision_prompt = incident.decision_prompt
    options = list(incident.decision_options)

    if loaded_fault:
        handling_mode = "REQUIRE_HUMAN_DECISION"
        action = "HOLD_AFFECTED_ROBOT"
        reason_codes.append("LOADED_ROBOT_RECOVERY_DECISION")
        decision_reason = decision_reason or "A loaded failed robot requires an operator-selected recovery method."
        decision_prompt = decision_prompt or "물품을 적재한 고장 로봇의 복구 방식을 선택해 주세요."
        options = options or [
            HumanInteractionOption(
                option_id="MANUAL_RECOVERY",
                label="현장 수동 회수",
                resolution_value="MANUAL_RECOVERY",
                selected_entity_ids=exact_ids,
            ),
            HumanInteractionOption(
                option_id="ROBOT_HANDOFF",
                label="다른 로봇으로 인계",
                resolution_value="ROBOT_HANDOFF",
                selected_entity_ids=exact_ids,
            ),
            HumanInteractionOption(
                option_id="MOVE_TO_SAFE_NODE",
                label="안전 지점 이동 시도",
                resolution_value="MOVE_TO_SAFE_NODE",
                selected_entity_ids=exact_ids,
            ),
        ]
    elif location_uncertain:
        handling_mode = "REQUIRE_HUMAN_DECISION"
        action = "STOP_AFFECTED_MISSIONS"
        reason_codes.append("INCIDENT_LOCATION_UNCERTAIN")
        decision_reason = decision_reason or "The affected resource is not uniquely identified."
        decision_prompt = decision_prompt or "영향받은 정확한 통로·노드·로봇을 지정해 주세요."
    elif impact_uncertain:
        handling_mode = "REQUIRE_HUMAN_DECISION"
        action = "HOLD_AFFECTED_ROBOT" if scope == "ROBOT" else "TEMPORARILY_BLOCK_RESOURCE"
        reason_codes.append("INCIDENT_IMPACT_UNCERTAIN")
        decision_reason = decision_reason or "The exact resource is known, but safe operability is not confirmed."
        decision_prompt = decision_prompt or "현장 확인 결과 통행·작업 재개가 가능한지 선택해 주세요."
        options = options or [
            HumanInteractionOption(
                option_id="KEEP_SAFETY_HOLD",
                label="임시 차단·정지 유지",
                resolution_value="KEEP_SAFETY_HOLD",
                selected_entity_ids=exact_ids,
            ),
            HumanInteractionOption(
                option_id="CONFIRM_SAFE_AND_CONTINUE",
                label="안전 확인 후 재개",
                resolution_value="CONFIRM_SAFE_AND_CONTINUE",
                selected_entity_ids=exact_ids,
            ),
        ]
    elif observed == "NOT_TRAVERSABLE":
        action = "TEMPORARILY_BLOCK_RESOURCE"
        handling_mode = "AUTO_HANDLE_AND_NOTIFY_HUMAN" if physical else "AUTO_HANDLE"
        reason_codes.append("CONFIRMED_RESOURCE_BLOCKAGE")
    elif scope == "ROBOT" and robot_operability == "FAULTED":
        action = "HOLD_AFFECTED_ROBOT"
        handling_mode = "AUTO_HANDLE_AND_NOTIFY_HUMAN" if physical else "AUTO_HANDLE"
        reason_codes.append("UNLOADED_ROBOT_FAULT_AUTO_REASSIGN")
    elif physical:
        handling_mode = "AUTO_HANDLE_AND_NOTIFY_HUMAN"
        if action == "NONE":
            action = "TEMPORARILY_BLOCK_RESOURCE" if scope == "MAP_RESOURCE" else "HOLD_AFFECTED_ROBOT"
        reason_codes.append("PHYSICAL_WORK_AFTER_AUTO_SAFETY")
    else:
        handling_mode = "AUTO_HANDLE"
        reason_codes.append("DETERMINISTIC_AUTO_HANDLING")

    return incident.model_copy(
        update={
            "scope": scope,
            "affected_resource_ids": exact_ids,
            "observed_effect": observed,
            "robot_operability": robot_operability,
            "load_state": load_state,
            "physical_intervention_required": physical,
            "handling_mode": handling_mode,
            "immediate_safety_action": action,
            "operator_decision_reason": decision_reason,
            "decision_prompt": decision_prompt,
            "decision_options": options,
            "reason_codes": list(dict.fromkeys(reason_codes)),
        }
    )


def _reason_code(incident: OperationalIncidentImpact) -> str:
    base = next(
        (
            value
            for value in incident.reason_codes
            if value
            in {
                "INCIDENT_LOCATION_UNCERTAIN",
                "INCIDENT_IMPACT_UNCERTAIN",
                "LOADED_ROBOT_RECOVERY_DECISION",
            }
        ),
        "INCIDENT_DECISION_REQUIRED",
    )
    return f"{base}::{incident.incident_id}"


def _notification(
    *,
    incident: OperationalIncidentImpact,
    notification_type: str,
    requires_response: bool,
    automatic_actions: list[str],
) -> OperatorNotification:
    title = incident.notification_title or (
        "현장 작업이 필요합니다"
        if notification_type == "HUMAN_WORK_REQUIRED"
        else "운영자 결정이 필요합니다"
        if notification_type == "HUMAN_DECISION_REQUIRED"
        else "운영 사고가 자동 처리되었습니다"
    )
    return OperatorNotification(
        notification_id=_stable_id("NOTICE", incident.incident_id, notification_type, incident.description),
        notification_type=notification_type,  # type: ignore[arg-type]
        title=title,
        message=incident.notification_message or incident.description,
        incident_id=incident.incident_id,
        affected_resource_ids=list(incident.affected_resource_ids),
        automatic_actions=automatic_actions,
        requires_response=requires_response,
        created_at=_utc_now(),
    )


def build_incident_response_plan(
    *,
    simulation_id: str,
    incidents: list[OperationalIncidentImpact],
    human_responses: list[HumanInteractionResponse],
) -> IncidentResponsePlan:
    """Build planned safety actions, non-blocking work notices, and at most one HITL card."""

    normalized = [_derive_incident(value) for value in incidents]
    actions: list[IncidentResponseAction] = []
    notifications: list[OperatorNotification] = []
    pending: HumanInteractionRequest | None = None

    for incident in normalized:
        response = _response_for(human_responses, incident.incident_id)
        action_labels: list[str] = []
        if incident.immediate_safety_action != "NONE":
            actions.append(
                IncidentResponseAction(
                    incident_id=incident.incident_id,
                    action=incident.immediate_safety_action,
                    affected_resource_ids=list(incident.affected_resource_ids),
                    reason=(
                        "Conservative runtime action must be committed before any operator wait. "
                        f"Observed effect={incident.observed_effect}."
                    ),
                    apply_before_human_response=True,
                    execution_status="PLANNED",
                    applied_immediately=False,
                )
            )
            action_labels.append(incident.immediate_safety_action)

        if incident.physical_intervention_required:
            notifications.append(
                _notification(
                    incident=incident,
                    notification_type="HUMAN_WORK_REQUIRED",
                    requires_response=False,
                    automatic_actions=action_labels,
                )
            )

        if incident.handling_mode != "REQUIRE_HUMAN_DECISION" or response is not None:
            if response is not None:
                notifications.append(
                    _notification(
                        incident=incident,
                        notification_type="INFO",
                        requires_response=False,
                        automatic_actions=action_labels,
                    )
                )
            continue

        notifications.append(
            _notification(
                incident=incident,
                notification_type="HUMAN_DECISION_REQUIRED",
                requires_response=True,
                automatic_actions=action_labels,
            )
        )
        if pending is None:
            prompt = incident.decision_prompt or incident.operator_decision_reason or "처리 방식을 선택해 주세요."
            options = list(incident.decision_options)
            code = _reason_code(incident)
            pending = HumanInteractionRequest(
                interaction_id=_stable_id("HITL", simulation_id, "PRE_ROUTE", code, prompt),
                kind="CLARIFICATION" if code.startswith("INCIDENT_LOCATION_UNCERTAIN") else "APPROVAL",
                stage="PRE_ROUTE",
                reason_code=code,
                headline="운영 사고 영향 확인" if code.startswith("INCIDENT_") else "적재 로봇 복구 방식 선택",
                prompt=prompt,
                options=options,
                recommended_option_id=options[0].option_id if options else None,
                default_action="HOLD",
                route_locked=True,
                resume_route="INCIDENT_RESPONSE",
                context_summary=(
                    "Immediate safety handling is planned first. The graph pauses only because a human "
                    "decision is needed, not because physical cleanup alone is required."
                ),
                created_at=_utc_now(),
            )

    return IncidentResponsePlan(
        incidents=normalized,
        immediate_actions=actions,
        notifications=notifications,
        pending_human_interaction=pending,
        summary=(
            f"Impact-based incident handling: incidents={len(normalized)}, actions={len(actions)}, "
            f"notifications={len(notifications)}, decision_pending={pending is not None}."
        ),
    )

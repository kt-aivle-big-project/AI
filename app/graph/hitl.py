"""Human-in-the-loop pause nodes for pre-route, in-route, and pre-optimization gates."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.node_observability import observe_node
from app.domain.schemas import (
    CuOptDynamicInputDraft,
    EntityResolutionResult,
    HumanInteractionOption,
    HumanInteractionRequest,
    HumanInteractionResponse,
    NormalizedWarehouseRequest,
)
from app.graph.node_support import model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.hitl_service import HumanInteractionService


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(state: LaroGraphState, stage: str, reason: str, prompt: str) -> str:
    seed = f"{state.get('simulation_id')}|{stage}|{reason}|{prompt}"
    return "HITL-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()


def _approved(state: LaroGraphState, reason_code: str) -> bool:
    for value in reversed(list(state.get("human_responses", []))):
        response = value if isinstance(value, HumanInteractionResponse) else HumanInteractionResponse.model_validate(value)
        if response.resolution_code == reason_code and response.action in {"APPROVE", "SELECT"}:
            return True
    return False


@observe_node(
    "human_interaction_pause",
    purpose="HITL 요청을 저장하고 프론트 응답을 기다리는 정상 대기 상태로 전환",
)
def human_interaction_pause_node(state: LaroGraphState) -> dict:
    """Persist one interaction checkpoint and return a non-failure waiting state."""

    interaction = model_from_state(state, "pending_human_interaction", HumanInteractionRequest)
    if not state.get("evaluation_shadow_mode", False):
        HumanInteractionService().create_pending(interaction=interaction, state=state)
    return {
        "pending_human_interaction": interaction,
        "workflow_status": (
            "awaiting_clarification"
            if interaction.kind == "CLARIFICATION"
            else "awaiting_human_approval"
        ),
        **trace_update("human_interaction_pause"),
    }


@observe_node(
    "in_route_human_interaction",
    purpose="정확한 코드 조회 후 발견된 권위 데이터 충돌을 같은 잠금 경로 안에서 운영자에게 확인",
)
def in_route_human_interaction_node(state: LaroGraphState) -> dict:
    """Build an exception-only HITL card for conflicting authoritative records."""

    resolutions = [
        value if isinstance(value, EntityResolutionResult) else EntityResolutionResult.model_validate(value)
        for value in state.get("current_entity_resolutions", [])
    ]
    ambiguous = next((value for value in resolutions if value.status == "AMBIGUOUS"), None)
    if ambiguous is None:
        raise ValueError(
            "IN_ROUTE HITL is reserved for authoritative data conflicts; no ambiguous canonical record exists."
        )

    options = [
        HumanInteractionOption(
            option_id=f"USE_{candidate.entity_id}",
            label=candidate.display_name or candidate.entity_id,
            description=f"권위 후보 {candidate.entity_type} · {candidate.match_method}",
            selected_entity_ids=[candidate.entity_id],
            resolution_value=candidate.entity_id,
            impact_summary="선택한 권위 레코드를 이번 실행의 근거로 사용합니다.",
        )
        for candidate in ambiguous.candidates
    ]
    options.append(
        HumanInteractionOption(
            option_id="HOLD_AND_RECONCILE",
            label="작업 보류 후 데이터 정합성 확인",
            resolution_value="HOLD_AND_RECONCILE",
            impact_summary="자동 계획을 중단하고 원천 데이터 정합성 확인 작업을 생성합니다.",
            outcome="HOLD",
            unavailable_reason="원천 데이터 정합성 확인이 완료될 때까지 자동 계획을 재개할 수 없습니다.",
        )
    )
    prompt = (
        f"Canonical reference '{ambiguous.raw_text}'에 서로 충돌하는 권위 레코드가 있습니다. "
        "이번 실행에 사용할 근거를 선택하거나 작업을 보류해 주세요."
    )
    interaction = HumanInteractionRequest(
        interaction_id=_id(state, "IN_ROUTE", "AUTHORITATIVE_DATA_CONFLICT", prompt),
        kind="APPROVAL",
        stage="IN_ROUTE",
        reason_code="AUTHORITATIVE_DATA_CONFLICT",
        headline="권위 데이터 충돌 검토",
        prompt=prompt,
        options=options,
        recommended_option_id="HOLD_AND_RECONCILE",
        default_action="HOLD",
        route_locked=True,
        resume_route="AGENT_FORMULATION",
        context_summary=ambiguous.reason,
        created_at=_utc_now(),
    )
    return {"pending_human_interaction": interaction, **trace_update("in_route_human_interaction")}



def _pre_optimization_policy_interaction(
    state: LaroGraphState,
    draft: CuOptDynamicInputDraft,
) -> HumanInteractionRequest | None:
    """Build approval cards for explicit high-impact policy choices."""

    request_value = state.get("normalized_request")
    request = (
        request_value
        if isinstance(request_value, NormalizedWarehouseRequest)
        else NormalizedWarehouseRequest.model_validate(request_value)
        if request_value is not None
        else None
    )
    command = (request.raw_user_command if request else "") or ""
    folded = command.casefold()

    if (
        ("비상용 로봇" in command or "emergency reserve" in folded)
        and ("sla" in folded or "둘 다 불가능" in command)
        and not _approved(state, "EMERGENCY_CAPACITY_SLA_CONFLICT")
    ):
        prompt = "비상용 로봇 여유와 긴급 출고 SLA를 동시에 만족할 수 없습니다. 이번 배치의 우선 정책을 선택해 주세요."
        return HumanInteractionRequest(
            interaction_id=_id(state, "PRE_OPTIMIZATION", "EMERGENCY_CAPACITY_SLA_CONFLICT", prompt),
            kind="APPROVAL",
            stage="PRE_OPTIMIZATION",
            reason_code="EMERGENCY_CAPACITY_SLA_CONFLICT",
            headline="비상 여유와 SLA 우선순위 승인",
            prompt=prompt,
            options=[
                HumanInteractionOption(
                    option_id="PRIORITIZE_SLA",
                    label="이번 배치는 SLA 우선",
                    resolution_value="PRIORITIZE_SLA",
                    impact_summary="필요한 로봇을 모두 사용하되 결정론적 안전 검증은 유지합니다.",
                ),
                HumanInteractionOption(
                    option_id="PRESERVE_EMERGENCY_RESERVE",
                    label="비상용 로봇 한 대 보존",
                    resolution_value="PRESERVE_RESERVE",
                    impact_summary="긴급 주문 완료시각이 늦어질 수 있습니다.",
                ),
            ],
            recommended_option_id="PRIORITIZE_SLA",
            default_action="HOLD",
            route_locked=True,
            resume_route="AGENT_FORMULATION" if draft.formulation_source == "llm" else "RULE_FORMULATION",
            context_summary=draft.formulation_summary,
            created_at=_utc_now(),
        )

    loaded_fault_markers = (
        "물건을 든 채",
        "적재한 채",
        "loaded robot",
        "carrying load",
    )
    fault_markers = ("고장", "멈췄", "정지", "fault", "stopped")
    if (
        any(marker in folded for marker in loaded_fault_markers)
        and any(marker in folded for marker in fault_markers)
        and not _approved(state, "LOADED_ROBOT_RECOVERY_APPROVAL")
    ):
        prompt = "물품을 적재한 고장 로봇의 복구 방식을 선택해 주세요."
        return HumanInteractionRequest(
            interaction_id=_id(state, "PRE_OPTIMIZATION", "LOADED_ROBOT_RECOVERY_APPROVAL", prompt),
            kind="APPROVAL",
            stage="PRE_OPTIMIZATION",
            reason_code="LOADED_ROBOT_RECOVERY_APPROVAL",
            headline="적재 로봇 복구 방식 승인",
            prompt=prompt,
            options=[
                HumanInteractionOption(
                    option_id="DIVERT_TO_BUFFER",
                    label="안전 버퍼로 이동",
                    resolution_value="DIVERT_TO_BUFFER",
                    impact_summary="가능한 경우 가장 가까운 안전 버퍼까지 이동합니다.",
                ),
                HumanInteractionOption(
                    option_id="HANDOFF_TO_ROBOT",
                    label="다른 로봇으로 인계",
                    resolution_value="HANDOFF_TO_ROBOT",
                    impact_summary="인계 위치와 수신 로봇을 추가 검증합니다.",
                ),
                HumanInteractionOption(
                    option_id="MANUAL_RECOVERY",
                    label="현장 수동 회수",
                    resolution_value="MANUAL_RECOVERY",
                    impact_summary="자동 실행을 보류하고 현장 작업자에게 인계합니다.",
                    outcome="HOLD",
                    unavailable_reason="현장 수동 회수가 완료된 후 새 계획을 요청해야 합니다.",
                ),
            ],
            recommended_option_id="DIVERT_TO_BUFFER",
            default_action="HOLD",
            route_locked=True,
            resume_route="AGENT_FORMULATION",
            context_summary=draft.formulation_summary,
            created_at=_utc_now(),
        )
    return None


@observe_node(
    "pre_optimization_approval_gate",
    purpose="Task 유예처럼 서비스 약속을 바꾸는 정식화 결과를 Solver 전에 운영자 승인 대상으로 판정",
)
def pre_optimization_approval_gate_node(state: LaroGraphState) -> dict:
    """Pause only when the validated draft defers work without prior approval."""

    draft = model_from_state(state, "cuopt_dynamic_input_draft", CuOptDynamicInputDraft)
    normalized_value = state.get("normalized_request")
    normalized = None
    if normalized_value is not None:
        normalized = (
            normalized_value
            if isinstance(normalized_value, NormalizedWarehouseRequest)
            else NormalizedWarehouseRequest.model_validate(normalized_value)
        )
    # In G2P mode an order-level draft may appear to defer sibling orders because
    # the physical compiler has not yet aggregated them into handling-unit cycles.
    # Those apparent deferrals are not an operator business choice; the compiler
    # immediately reconstructs the complete canonical outbound wave.
    pure_outbound_g2p = bool(
        get_settings().outbound_fulfillment_mode == "goods_to_person"
        and normalized is not None
        and normalized.operations
        and all(value.operation_type == "OUTBOUND_ORDER" for value in normalized.operations)
    )
    if pure_outbound_g2p:
        return {
            "pending_human_interaction": None,
            "pre_optimization_approval_cleared": True,
            **trace_update("pre_optimization_approval_gate"),
        }
    policy_interaction = _pre_optimization_policy_interaction(state, draft)
    if policy_interaction is not None:
        return {
            "pending_human_interaction": policy_interaction,
            "pre_optimization_approval_cleared": False,
            **trace_update("pre_optimization_approval_gate"),
        }

    if not draft.deferred_order_ids or _approved(state, "TASK_DEFERRAL_APPROVAL"):
        return {
            "pending_human_interaction": None,
            "pre_optimization_approval_cleared": True,
            **trace_update("pre_optimization_approval_gate"),
        }

    deferred = list(dict.fromkeys(draft.deferred_order_ids))
    prompt = (
        f"현재 계획은 {len(deferred)}개 작업({', '.join(deferred)})을 다음 계획으로 유예합니다. "
        "이 변경을 승인할까요?"
    )
    interaction = HumanInteractionRequest(
        interaction_id=_id(state, "PRE_OPTIMIZATION", "TASK_DEFERRAL_APPROVAL", prompt),
        kind="APPROVAL",
        stage="PRE_OPTIMIZATION",
        reason_code="TASK_DEFERRAL_APPROVAL",
        headline="작업 유예 승인 필요",
        prompt=prompt,
        options=[
            HumanInteractionOption(
                option_id="APPROVE_DEFERRAL",
                label="유예 승인",
                selected_entity_ids=deferred,
                resolution_value="ALLOW_DEFERRAL",
                impact_summary="유예 작업은 미착수 상태로 다음 계획 Horizon에 남습니다.",
            ),
            HumanInteractionOption(
                option_id="REJECT_DEFERRAL",
                label="유예 거절",
                selected_entity_ids=deferred,
                resolution_value="NO_DEFERRAL",
                impact_summary="현재 Draft는 실행하지 않고 작업 구성을 다시 요청해야 합니다.",
            ),
        ],
        recommended_option_id="APPROVE_DEFERRAL",
        default_action="HOLD",
        route_locked=True,
        resume_route=(
            "AGENT_FORMULATION"
            if draft.formulation_source == "llm"
            else "RULE_FORMULATION"
        ),
        context_summary=draft.formulation_summary,
        created_at=_utc_now(),
    )
    return {
        "pending_human_interaction": interaction,
        "pre_optimization_approval_cleared": False,
        **trace_update("pre_optimization_approval_gate"),
    }

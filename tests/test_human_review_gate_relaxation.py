"""Regression coverage for bounded Human Review routing."""

from app.domain.schemas import (
    FormulationRecommendation,
    HumanInteractionOption,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
)
from app.services.request_gate_service import resolve_request_gate


def _request(command: str | None) -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="mixed" if command else "structured_events",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
                raw_reference="ORD-001",
                attributes="",
            )
        ],
        constraints=NormalizedRequestConstraints(),
        raw_user_command=command,
        normalization_summary="test request",
    )


def _resolve(command: str | None, recommendation: FormulationRecommendation):
    return resolve_request_gate(
        simulation_id="SIM-HITL",
        request=_request(command),
        recommendation=recommendation,
        original_user_command=command,
        has_structured_events=True,
        authoritative_structured_input=True,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=[],
    )


def test_operator_intent_can_open_review_for_generated_commands() -> None:
    decision = _resolve(
        "다음 작업은 적당히 알아서 반대로 처리해.",
        FormulationRecommendation(
            route="AGENT_FORMULATION",
            gate_action="ASK_CLARIFICATION",
            reason_code="OPERATOR_INTENT_CLARIFICATION",
            prompt="'반대로'가 작업 순서 변경인지 입출고 방향 변경인지 선택해 주세요.",
        ),
    )

    assert decision.action == "ASK_CLARIFICATION"
    assert decision.human_interaction is not None
    assert decision.human_interaction.reason_code == "OPERATOR_INTENT_CLARIFICATION"


def test_clear_canonical_command_does_not_open_model_only_review() -> None:
    decision = _resolve(
        "ORD-001을 출고하고 전체 완료시간을 최소화해.",
        FormulationRecommendation(
            route="AGENT_FORMULATION",
            gate_action="ASK_CLARIFICATION",
            reason_code="UNREADABLE_COMMAND",
            prompt="명령을 다시 입력해 주세요.",
        ),
    )

    assert decision.action == "ROUTE_AGENT"
    assert decision.human_interaction is None


def test_pure_structured_batch_still_suppresses_unnecessary_review() -> None:
    decision = _resolve(
        None,
        FormulationRecommendation(
            route="RULE_FORMULATION",
            gate_action="ASK_CLARIFICATION",
            prompt="추가 조건을 입력해 주세요.",
        ),
    )

    assert decision.action == "ROUTE_RULE"
    assert decision.human_interaction is None


def test_legacy_human_review_route_is_preserved_as_review() -> None:
    decision = _resolve(
        "이 작업은 무조건 처리하되 처리하면 안 돼.",
        FormulationRecommendation(
            route="HUMAN_REVIEW",
            gate_action="PROCEED",
            reason_code="OPERATOR_REVIEW_REQUIRED",
            prompt="서로 모순되는 처리 지시 중 어느 쪽을 적용할지 선택해 주세요.",
        ),
    )

    assert decision.action == "ASK_CLARIFICATION"
    assert decision.human_interaction is not None


def test_resumable_is_derived_instead_of_rejecting_review_payload() -> None:
    option = HumanInteractionOption(
        option_id="HOLD",
        label="보류",
        outcome="HOLD",
        resumable=True,
    )

    assert option.resumable is False
    assert option.unavailable_reason

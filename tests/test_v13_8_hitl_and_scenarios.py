"""Code-first mission input and exception-only HITL contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.domain.schemas import (
    CuOptDynamicInputDraft,
    CuOptFleetDraft,
    CuOptTaskDraft,
    EntityResolutionCandidate,
    EntityResolutionResult,
    FormulationRecommendation,
    HumanInteractionRequest,
    HumanInteractionResumeRequest,
    HumanInteractionResponse,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    RequestGateDecision,
)
from app.graph.hitl import in_route_human_interaction_node, pre_optimization_approval_gate_node
from app.services.hitl_service import HumanInteractionService, HumanInteractionStore
from app.services.llm_evaluation_service import load_llm_evaluation_scenarios, validate_auto_response
from app.services.request_gate_service import resolve_request_gate


def _request(
    *,
    command: str = "",
    operation_id: str = "ORD-001",
    operation_type: str = "OUTBOUND_ORDER",
    constraints: NormalizedRequestConstraints | None = None,
) -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="natural_language" if command else "structured_events",
        operations=[
            NormalizedOperation(
                operation_id=operation_id,
                operation_type=operation_type,
                raw_reference=operation_id,
                attributes="",
            )
        ],
        constraints=constraints or NormalizedRequestConstraints(),
        raw_user_command=command or None,
        normalization_summary="test",
    )


def _recommendation(
    *,
    route: str = "RULE_FORMULATION",
    gate: str = "PROCEED",
    prompt: str | None = None,
    reason_code: str | None = None,
) -> FormulationRecommendation:
    return FormulationRecommendation(
        route=route,
        gate_action=gate,
        reason_code=reason_code,
        reasons=["test recommendation"],
        prompt=prompt,
    )


def _resolve(
    request: NormalizedWarehouseRequest,
    *,
    recommendation: FormulationRecommendation | None = None,
    has_structured_events: bool = False,
    human_responses: list[HumanInteractionResponse] | None = None,
):
    return resolve_request_gate(
        simulation_id="SIM-GATE",
        request=request,
        recommendation=recommendation or _recommendation(),
        original_user_command=request.raw_user_command,
        has_structured_events=has_structured_events,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=human_responses or [],
    )


def test_structured_ten_orders_ignore_unnecessary_llm_clarification() -> None:
    request = NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id=f"ORD-{index:03d}",
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
                raw_reference=f"ORD-{index:03d}",
                attributes="",
            )
            for index in range(1, 11)
        ],
        constraints=NormalizedRequestConstraints(),
        normalization_summary="ten structured orders",
    )
    decision = _resolve(
        request,
        recommendation=_recommendation(
            route="RULE_FORMULATION",
            gate="ASK_CLARIFICATION",
            prompt="주문별 디스패치 제약을 알려 주세요.",
        ),
        has_structured_events=True,
    )
    assert decision.action == "ROUTE_RULE"
    assert decision.final_route == "RULE_FORMULATION"
    assert decision.route_locked is True
    assert decision.human_interaction is None
    assert decision.input_rejection is None
    assert any("complete structured event envelope" in value for value in decision.reasons)


def test_item_name_based_order_is_rejected_not_semantically_resolved() -> None:
    request = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="UNRESOLVED-BEARING-ORDER",
                operation_type="OUTBOUND_ORDER",
                raw_reference="산업용 베어링 주문",
                attributes="",
            )
        ],
        constraints=NormalizedRequestConstraints(),
        raw_user_command="산업용 베어링 주문을 처리해.",
        normalization_summary="noncanonical order reference",
    )
    decision = _resolve(request, recommendation=_recommendation(route="AGENT_FORMULATION"))
    assert decision.action == "REJECT_INPUT"
    assert decision.input_rejection is not None
    assert decision.input_rejection.reason_code == "CANONICAL_OPERATION_ID_REQUIRED"
    assert "order_id (ORD-###)" in decision.input_rejection.required_identifier_types
    assert decision.human_interaction is None


def test_vague_order_reference_is_rejected_not_hitl() -> None:
    decision = _resolve(
        _request(command="그 주문 처리해.", operation_id="UNRESOLVED"),
        recommendation=_recommendation(route="AGENT_FORMULATION", gate="ASK_CLARIFICATION"),
    )
    assert decision.action == "REJECT_INPUT"
    assert decision.input_rejection is not None
    assert decision.input_rejection.reason_code == "CANONICAL_OPERATION_ID_REQUIRED"


def test_input_visible_robot_conflict_is_invalid_request_not_hitl() -> None:
    decision = _resolve(
        _request(command="R003은 제외하고 R003만 사용해서 ORD-001을 처리해."),
        recommendation=_recommendation(route="AGENT_FORMULATION"),
    )
    assert decision.action == "REJECT_INPUT"
    assert decision.input_rejection is not None
    assert decision.input_rejection.reason_code == "CONFLICTING_ROBOT_CONSTRAINT"
    assert decision.human_interaction is None


def test_structured_and_text_id_conflict_is_rejected() -> None:
    decision = _resolve(
        _request(command="ORD-999를 처리해.", operation_id="ORD-001"),
        recommendation=_recommendation(route="RULE_FORMULATION"),
        has_structured_events=True,
    )
    assert decision.action == "REJECT_INPUT"
    assert decision.input_rejection is not None
    assert decision.input_rejection.reason_code == "AUTHORITATIVE_ID_CONFLICT"


def test_natural_resource_alias_is_rejected_in_code_first_contract() -> None:
    request = _request(
        command="ORD-001을 처리하되 D 출고 통로는 피해.",
        constraints=NormalizedRequestConstraints(soft_avoid_edge_references=["D 출고 통로"]),
    )
    decision = _resolve(request, recommendation=_recommendation(route="AGENT_FORMULATION"))
    assert decision.action == "REJECT_INPUT"
    assert decision.input_rejection is not None
    assert decision.input_rejection.reason_code == "CANONICAL_RESOURCE_ID_REQUIRED"


def test_safety_override_is_exception_hitl() -> None:
    decision = _resolve(
        _request(command="ORD-001을 처리하되 안전검사 생략하고 H3_7 차단도 무시해."),
        recommendation=_recommendation(route="AGENT_FORMULATION"),
    )
    assert decision.action == "REQUIRE_HUMAN_APPROVAL"
    assert decision.human_interaction is not None
    assert decision.human_interaction.reason_code == "SAFETY_OVERRIDE_REQUEST"
    assert decision.human_interaction.default_action == "HOLD"


def test_inventory_source_conflict_is_exception_hitl() -> None:
    decision = _resolve(
        _request(command="ORD-001 처리 전 K1_7-L1 시스템 재고와 센서 수량이 불일치해."),
        recommendation=_recommendation(route="AGENT_FORMULATION"),
    )
    assert decision.action == "REQUIRE_HUMAN_APPROVAL"
    assert decision.human_interaction is not None
    assert decision.human_interaction.reason_code == "AUTHORITATIVE_DATA_CONFLICT"
    assert {option.option_id for option in decision.human_interaction.options} == {
        "HOLD_AND_RECOUNT",
        "USE_CONFIRMED_SENSOR_QUANTITY",
        "USE_ALTERNATIVE_STOCK",
    }


def test_already_picked_order_cancellation_is_exception_hitl() -> None:
    decision = _resolve(
        _request(command="ORD-001은 이미 Pickup 완료했는데 지금 취소해."),
        recommendation=_recommendation(route="AGENT_FORMULATION"),
    )
    assert decision.action == "REQUIRE_HUMAN_APPROVAL"
    assert decision.human_interaction is not None
    assert decision.human_interaction.reason_code == "COMMITTED_TASK_CANCELLATION"


def test_contract_destination_override_is_exception_hitl() -> None:
    decision = _resolve(
        _request(command="ORD-001의 계약 목적지 O_D를 O_E로 대체 변경해."),
        recommendation=_recommendation(route="AGENT_FORMULATION"),
    )
    assert decision.action == "REQUIRE_HUMAN_APPROVAL"
    assert decision.human_interaction is not None
    assert decision.human_interaction.reason_code == "DESTINATION_OVERRIDE_APPROVAL"


def test_in_route_authoritative_conflict_keeps_agent_route_locked() -> None:
    state = {
        "simulation_id": "SIM-IN-ROUTE",
        "workflow_trace": [],
        "current_entity_resolutions": [
            EntityResolutionResult(
                reference_id="stock-record",
                raw_text="K1_7-L1 / ITEM_BATTERY",
                status="AMBIGUOUS",
                candidates=[
                    EntityResolutionCandidate(
                        entity_id="WMS-STOCK-K1_7-L1",
                        entity_type="RACK",
                        display_name="WMS record: quantity=4",
                        match_method="EXACT_ID",
                        confidence=1.0,
                    ),
                    EntityResolutionCandidate(
                        entity_id="SENSOR-STOCK-K1_7-L1",
                        entity_type="RACK",
                        display_name="Sensor record: quantity=3",
                        match_method="EXACT_ID",
                        confidence=1.0,
                    ),
                ],
                reason="WMS and sensor disagree for the same canonical rack slot.",
            )
        ],
        "current_user_not_found_references": [],
    }
    interaction = in_route_human_interaction_node(state)["pending_human_interaction"]
    assert interaction.stage == "IN_ROUTE"
    assert interaction.reason_code == "AUTHORITATIVE_DATA_CONFLICT"
    assert interaction.route_locked is True
    assert interaction.resume_route == "AGENT_FORMULATION"
    assert {value.option_id for value in interaction.options} == {
        "USE_WMS-STOCK-K1_7-L1",
        "USE_SENSOR-STOCK-K1_7-L1",
        "HOLD_AND_RECONCILE",
    }


def test_auto_response_contract_validates_dynamic_exception_options() -> None:
    state = {
        "simulation_id": "SIM-AUTO-RESPONSE",
        "workflow_trace": [],
        "current_entity_resolutions": [
            EntityResolutionResult(
                reference_id="stock-record",
                raw_text="K1_7-L1 / ITEM_BATTERY",
                status="AMBIGUOUS",
                candidates=[
                    EntityResolutionCandidate(
                        entity_id="WMS-STOCK-K1_7-L1",
                        entity_type="RACK",
                        display_name="WMS record",
                        match_method="EXACT_ID",
                        confidence=1.0,
                    ),
                    EntityResolutionCandidate(
                        entity_id="SENSOR-STOCK-K1_7-L1",
                        entity_type="RACK",
                        display_name="Sensor record",
                        match_method="EXACT_ID",
                        confidence=1.0,
                    ),
                ],
                reason="authoritative conflict",
            )
        ],
        "current_user_not_found_references": [],
    }
    interaction = in_route_human_interaction_node(state)["pending_human_interaction"]
    valid = HumanInteractionResumeRequest(
        action="SELECT",
        selected_option_id="USE_WMS-STOCK-K1_7-L1",
        selected_entity_ids=["WMS-STOCK-K1_7-L1"],
    )
    assert validate_auto_response(interaction=interaction, response=valid) == []

    wrong = HumanInteractionResumeRequest(action="SELECT", selected_option_id="SELECT_ORD-001")
    errors = validate_auto_response(interaction=interaction, response=wrong)
    assert errors and "available_option_ids" in errors[0]


def _draft(*, deferred: list[str], summary: str = "draft") -> CuOptDynamicInputDraft:
    return CuOptDynamicInputDraft(
        snapshot_id="SNAP-1",
        graph_version="GRAPH-1",
        formulation_source="llm",
        objective_profile="MIN_COMPLETION_TIME",
        tasks=[
            CuOptTaskDraft(
                task_id="TASK-1",
                order_id="ORD-001",
                item_id="ITEM_BEARING",
                stock_id="STOCK-1",
                pickup_node="K1_7",
                delivery_node="O_D",
                demand=1,
                priority="high",
                evidence_ids=["E1"],
            )
        ],
        deferred_order_ids=deferred,
        fleet=CuOptFleetDraft(included_robot_ids=["R002"]),
        formulation_summary=summary,
    )


def test_task_deferral_requires_pre_optimization_approval() -> None:
    update = pre_optimization_approval_gate_node(
        {
            "simulation_id": "SIM-DEFER",
            "workflow_trace": [],
            "cuopt_dynamic_input_draft": _draft(deferred=["IN-003"]),
            "normalized_request": _request(command="IN-003을 다음 배치로 유예해."),
            "human_responses": [],
        }
    )
    interaction = update["pending_human_interaction"]
    assert interaction.stage == "PRE_OPTIMIZATION"
    assert interaction.reason_code == "TASK_DEFERRAL_APPROVAL"
    assert interaction.route_locked is True


def test_hitl_store_persists_exception_and_rejects_without_resuming(tmp_path: Path) -> None:
    store = HumanInteractionStore(tmp_path)
    service = HumanInteractionService(store)
    request = _request(command="ORD-001을 처리하되 안전검사 생략해.")
    decision = _resolve(request, recommendation=_recommendation(route="AGENT_FORMULATION"))
    assert decision.human_interaction is not None
    state = {
        "simulation_id": "SIM-HITL",
        "request_mode": "human_command",
        "optimization_backend": "cuopt_payload_only",
        "events": [],
        "user_command": request.raw_user_command,
        "requested_planning_mode": None,
        "max_agent_steps": 8,
        "max_planner_retries": 1,
        "human_responses": [],
        "parent_interaction_id": None,
    }
    record = service.create_pending(interaction=decision.human_interaction, state=state)
    assert service.get(record.interaction.interaction_id).status == "PENDING"
    result = service.respond(
        record.interaction.interaction_id,
        HumanInteractionResumeRequest(action="REJECT", actor_id="operator-1"),
    )
    assert result.interaction_status == "REJECTED"
    assert result.orchestration_result is None


def test_hitl_checkpoint_preserves_simulation_run_id_for_resume(tmp_path: Path) -> None:
    store = HumanInteractionStore(tmp_path)
    service = HumanInteractionService(store)
    interaction = HumanInteractionRequest(
        interaction_id="HITL-RUN-19",
        kind="APPROVAL",
        stage="PRE_ROUTE",
        reason_code="TEST_APPROVAL",
        headline="Test approval",
        prompt="Approve the test workflow.",
        route_locked=True,
        resume_route="AGENT_FORMULATION",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    record = service.create_pending(
        interaction=interaction,
        state={
            "warehouse_id": "WH-001",
            "simulation_id": "SIM-HITL-RUN",
            "simulation_run_id": 19,
            "request_mode": "human_command",
            "optimization_backend": "cuopt_payload_only",
            "events": [],
            "user_command": "process ORD-001",
            "requested_planning_mode": None,
            "max_agent_steps": 8,
            "max_planner_retries": 1,
            "human_responses": [],
            "parent_interaction_id": None,
        },
    )

    restored = service.get(record.interaction.interaction_id)
    assert restored.original_request["simulation_run_id"] == 19


def test_llm_scenario_suite_is_code_first_and_exception_oriented() -> None:
    scenarios = load_llm_evaluation_scenarios()
    assert len(scenarios) >= 35
    assert len({value.scenario_id for value in scenarios}) == len(scenarios)
    categories = {value.category for value in scenarios}
    assert {
        "ROUTER_RULE",
        "ROUTER_AGENT",
        "INPUT_REJECTION",
        "HITL_EXCEPTION",
        "INCIDENT_AUTOMATION",
        "PRE_OPTIMIZATION_HITL",
        "ADVERSARIAL",
    }.issubset(categories)
    assert sum(value.difficulty >= 4 for value in scenarios) >= 20
    forbidden_success_phrases = ("베어링 주문", "센서 주문", "배터리 관련 주문")
    for scenario in scenarios:
        command = scenario.request.user_command or ""
        if any(value in command for value in forbidden_success_phrases):
            assert scenario.category == "INPUT_REJECTION"
            assert scenario.expected.input_rejection_reason_code is not None


def test_hold_and_recount_terminates_as_auditable_hold_without_agent_resume() -> None:
    request = _request(command="ORD-001 처리 전 K1_7-L1 시스템 재고와 센서 수량이 불일치해.")
    response = HumanInteractionResponse(
        interaction_id="HITL-HOLD",
        action="SELECT",
        selected_option_id="HOLD_AND_RECOUNT",
        resolution_code="AUTHORITATIVE_DATA_CONFLICT",
        resolution_value="HOLD_AND_RECOUNT",
        actor_id="inventory-manager",
    )
    decision = _resolve(
        request,
        recommendation=_recommendation(route="AGENT_FORMULATION"),
        human_responses=[response],
    )
    assert decision.action == "HOLD_WORKFLOW"
    assert decision.workflow_hold is not None
    assert decision.workflow_hold.selected_option_id == "HOLD_AND_RECOUNT"
    assert decision.final_route is None
    assert decision.route_locked is False


def test_hold_and_recount_resolves_to_terminal_hold_without_rerun(tmp_path: Path) -> None:
    """A recount choice must not relaunch Agent/optimizer against conflicting facts."""

    store = HumanInteractionStore(tmp_path)
    service = HumanInteractionService(store)
    request = _request(command="ORD-001 처리 전 K1_7-L1 시스템 재고와 센서 수량이 불일치해.")
    decision = _resolve(request, recommendation=_recommendation(route="AGENT_FORMULATION"))
    assert decision.human_interaction is not None
    record = service.create_pending(
        interaction=decision.human_interaction,
        state={
            "simulation_id": "SIM-RECOUNT",
            "request_mode": "human_command",
            "optimization_backend": "cuopt_payload_only",
            "events": [],
            "user_command": request.raw_user_command,
            "requested_planning_mode": None,
            "max_agent_steps": 8,
            "max_planner_retries": 1,
            "human_responses": [],
            "parent_interaction_id": None,
        },
    )

    result = service.respond(
        record.interaction.interaction_id,
        HumanInteractionResumeRequest(
            action="SELECT",
            selected_option_id="HOLD_AND_RECOUNT",
            actor_id="inventory-manager",
        ),
    )
    assert result.interaction_status == "RESOLVED"
    assert result.terminal_status == "held_for_human_action"
    assert result.terminal_reason_code == "AUTHORITATIVE_DATA_CONFLICT"
    assert result.orchestration_result is None

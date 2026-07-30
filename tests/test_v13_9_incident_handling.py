"""v13.9 impact-based operational incident and HITL contracts."""
from __future__ import annotations

from app.domain.schemas import (
    EventInput,
    FormulationRecommendation,
    HumanInteractionResponse,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    OperationalIncidentImpact,
)
from app.graph.input_formulation import _structured_normalized_request
from app.graph.incident_response import incident_immediate_action_executor_node
from app.services.incident_response_service import build_incident_response_plan
from app.services.request_gate_service import resolve_request_gate


def _request(incident: OperationalIncidentImpact) -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id=incident.incident_id,
                operation_type="INCIDENT",
                raw_reference=incident.description,
                attributes="generic operational incident",
            )
        ],
        incidents=[incident],
        constraints=NormalizedRequestConstraints(),
        raw_user_command=incident.description,
        normalization_summary="generic incident",
    )


def _recommendation(route: str = "AGENT_FORMULATION") -> FormulationRecommendation:
    return FormulationRecommendation(
        route=route,
        gate_action="PROCEED",
        reasons=["incident impact synthesis"],
    )


def test_auto_handle_and_notify_human_is_non_blocking() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-1",
        description="H3_7 통행 불가 확인",
        affected_resource_ids=["H3_7"],
        observed_effect="NOT_TRAVERSABLE",
        handling_mode="AUTO_HANDLE_AND_NOTIFY_HUMAN",
        immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
        physical_intervention_required=True,
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-1",
        incidents=[incident],
        human_responses=[],
    )
    assert plan.pending_human_interaction is None
    assert [value.action for value in plan.immediate_actions] == ["TEMPORARILY_BLOCK_RESOURCE"]
    assert [value.notification_type for value in plan.notifications] == ["HUMAN_WORK_REQUIRED"]
    assert plan.notifications[0].requires_response is False

    gate = resolve_request_gate(
        simulation_id="SIM-1",
        request=_request(incident),
        recommendation=_recommendation(),
        original_user_command=incident.description,
        has_structured_events=False,
        planning_mode="llm_router",
        requires_agent_guard=True,
        human_responses=[],
        incident_response_plan=plan,
    )
    assert gate.action == "HANDLE_INCIDENT"
    assert gate.human_interaction is None


def test_human_decision_applies_safety_before_hitl() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-2",
        description="H3_8 통행 가능 여부 미확인",
        affected_resource_ids=["H3_8"],
        observed_effect="UNKNOWN",
        handling_mode="REQUIRE_HUMAN_DECISION",
        immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
        physical_intervention_required=True,
        operator_decision_reason="현장 확인 후 차단 유지 또는 재개를 선택해야 합니다.",
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-2",
        incidents=[incident],
        human_responses=[],
    )
    assert plan.immediate_actions[0].apply_before_human_response is True
    assert plan.immediate_actions[0].execution_status == "PLANNED"
    assert plan.immediate_actions[0].applied_immediately is False
    assert plan.immediate_actions[0].action == "TEMPORARILY_BLOCK_RESOURCE"
    assert plan.pending_human_interaction is not None
    assert plan.pending_human_interaction.reason_code == "INCIDENT_IMPACT_UNCERTAIN::INC-2"
    assert {value.notification_type for value in plan.notifications} == {
        "HUMAN_WORK_REQUIRED",
        "HUMAN_DECISION_REQUIRED",
    }

    gate = resolve_request_gate(
        simulation_id="SIM-2",
        request=_request(incident),
        recommendation=_recommendation(),
        original_user_command=incident.description,
        has_structured_events=False,
        planning_mode="llm_router",
        requires_agent_guard=True,
        human_responses=[],
        incident_response_plan=plan,
    )
    assert gate.action == "REQUIRE_HUMAN_APPROVAL"
    assert gate.final_route == "INCIDENT_RESPONSE"
    assert gate.route_locked is True
    assert gate.human_interaction is not None
    assert gate.human_interaction.route_locked is True
    assert gate.human_interaction.resume_route == "INCIDENT_RESPONSE"


def test_resolved_incident_decision_no_longer_pauses() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-3",
        description="영향 자원 처리 방식 선택 필요",
        affected_resource_ids=["H3_9"],
        observed_effect="UNKNOWN",
        handling_mode="REQUIRE_HUMAN_DECISION",
        immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
        operator_decision_reason="차단 상태를 선택해야 합니다.",
    )
    response = HumanInteractionResponse(
        interaction_id="HITL-X",
        action="SELECT",
        selected_option_id="KEEP_SAFETY_HOLD",
        selected_entity_ids=["H3_9"],
        resolution_code="INCIDENT_IMPACT_UNCERTAIN::INC-3",
        resolution_value="KEEP_SAFETY_HOLD",
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-3",
        incidents=[incident],
        human_responses=[response],
    )
    assert plan.pending_human_interaction is None
    assert any(value.notification_type == "INFO" for value in plan.notifications)


def test_incident_name_does_not_change_impact_handling() -> None:
    common = dict(
        affected_resource_ids=["H3_7"],
        observed_effect="NOT_TRAVERSABLE",
        handling_mode="AUTO_HANDLE_AND_NOTIFY_HUMAN",
        immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
        physical_intervention_required=True,
    )
    first = OperationalIncidentImpact(
        incident_id="INC-A",
        description="박스가 떨어짐",
        **common,
    )
    second = OperationalIncidentImpact(
        incident_id="INC-B",
        description="운반 장비 주변에 장애물이 생김",
        **common,
    )
    first_plan = build_incident_response_plan(
        simulation_id="SIM-A",
        incidents=[first],
        human_responses=[],
    )
    second_plan = build_incident_response_plan(
        simulation_id="SIM-B",
        incidents=[second],
        human_responses=[],
    )
    assert [value.action for value in first_plan.immediate_actions] == [
        value.action for value in second_plan.immediate_actions
    ]
    assert [value.notification_type for value in first_plan.notifications] == [
        value.notification_type for value in second_plan.notifications
    ]
    assert first_plan.pending_human_interaction is None
    assert second_plan.pending_human_interaction is None


def test_structured_incident_uses_generic_incident_contract() -> None:
    event = EventInput(
        type="operational_incident",
        edge_id="H3_8",
        payload={
            "incident_description": "H3_8 통행 불가 확인",
            "observed_effect": "NOT_TRAVERSABLE",
            "handling_mode": "AUTO_HANDLE_AND_NOTIFY_HUMAN",
            "immediate_safety_action": "TEMPORARILY_BLOCK_RESOURCE",
            "physical_intervention_required": True,
        },
    )
    request = _structured_normalized_request(
        {
            "events": [event],
            "user_command": None,
        }
    )
    assert request.operations[0].operation_type == "INCIDENT"
    assert request.operations[0].source_event_type == "operational_incident"
    assert request.incidents[0].affected_resource_ids == ["H3_8"]
    assert request.incidents[0].handling_mode == "AUTO_HANDLE_AND_NOTIFY_HUMAN"
    assert not request.user_clarification_questions



def test_detailed_upstream_event_name_is_not_a_domain_incident_type() -> None:
    event = EventInput(
        type="box_spilled",
        edge_id="H3_8",
        payload={
            "incident_description": "H3_8 통행 불가 확인",
            "observed_effect": "NOT_TRAVERSABLE",
            "physical_intervention_required": True,
        },
    )
    request = _structured_normalized_request(
        {
            "events": [event],
            "user_command": None,
        }
    )
    assert request.incidents == []
    assert request.operations[0].operation_type == "UNKNOWN"
    assert request.operations[0].source_event_type == "box_spilled"

def test_incident_executor_applies_planning_overlay_before_pause() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-OVERLAY",
        description="H3_8 통행 가능 여부 미확인",
        affected_resource_ids=["H3_8"],
        observed_effect="UNKNOWN",
        handling_mode="REQUIRE_HUMAN_DECISION",
        immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
        operator_decision_reason="현장 판단 필요",
    )
    request = _request(incident)
    plan = build_incident_response_plan(
        simulation_id="SIM-OVERLAY",
        incidents=[incident],
        human_responses=[],
    )
    update = incident_immediate_action_executor_node(
        {
            "workflow_trace": [],
            "normalized_request": request,
            "incident_response_plan": plan,
        }
    )
    updated_request = update["normalized_request"]
    updated_plan = update["incident_response_plan"]
    assert "H3_8" in updated_request.constraints.hard_block_edge_ids
    assert updated_plan.immediate_actions[0].execution_status == "APPLIED"
    assert updated_plan.immediate_actions[0].applied_immediately is True


def test_incident_only_request_uses_handle_incident_gate() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-ONLY",
        description="H3_7 통행 불가 확인",
        affected_resource_ids=["H3_7"],
        observed_effect="NOT_TRAVERSABLE",
        physical_intervention_required=True,
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-ONLY",
        incidents=[incident],
        human_responses=[],
    )
    gate = resolve_request_gate(
        simulation_id="SIM-ONLY",
        request=_request(incident),
        recommendation=_recommendation(),
        original_user_command=incident.description,
        has_structured_events=False,
        planning_mode="llm_router",
        requires_agent_guard=True,
        human_responses=[],
        incident_response_plan=plan,
    )
    assert gate.action == "HANDLE_INCIDENT"
    assert gate.final_route == "INCIDENT_RESPONSE"
    assert gate.route_locked is True
    assert gate.human_interaction is None


def test_uncertain_incident_location_requires_decision_and_global_hold() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-LOCATION",
        description="H3 쪽에 뭔가 떨어졌지만 정확한 위치는 모름",
        affected_resource_references=["H3"],
        observed_effect="UNKNOWN",
        physical_intervention_required=True,
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-LOCATION",
        incidents=[incident],
        human_responses=[],
    )
    assert plan.incidents[0].handling_mode == "REQUIRE_HUMAN_DECISION"
    assert plan.incidents[0].reason_codes == [
        "INCIDENT_LOCATION_UNCERTAIN"
    ]
    assert plan.immediate_actions[0].action == "STOP_AFFECTED_MISSIONS"
    assert plan.pending_human_interaction is not None
    assert plan.pending_human_interaction.reason_code == (
        "INCIDENT_LOCATION_UNCERTAIN::INC-LOCATION"
    )


def test_loaded_failed_robot_requires_recovery_decision() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-LOADED-ROBOT",
        description="R004가 물건을 든 채 멈췄다",
        affected_resource_ids=["R004"],
        scope="ROBOT",
        robot_operability="FAULTED",
        load_state="LOADED",
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-LOADED",
        incidents=[incident],
        human_responses=[],
    )
    assert plan.incidents[0].handling_mode == "REQUIRE_HUMAN_DECISION"
    assert "LOADED_ROBOT_RECOVERY_DECISION" in plan.incidents[0].reason_codes
    assert plan.immediate_actions[0].action == "HOLD_AFFECTED_ROBOT"
    assert plan.pending_human_interaction is not None
    assert len(plan.pending_human_interaction.options) == 3


def test_unloaded_failed_robot_is_held_without_blocking_hitl() -> None:
    incident = OperationalIncidentImpact(
        incident_id="INC-EMPTY-ROBOT",
        description="짐이 없는 R004가 고장으로 정지했다",
        affected_resource_ids=["R004"],
        scope="ROBOT",
        robot_operability="FAULTED",
        load_state="EMPTY",
        physical_intervention_required=True,
    )
    plan = build_incident_response_plan(
        simulation_id="SIM-EMPTY",
        incidents=[incident],
        human_responses=[],
    )
    assert plan.incidents[0].handling_mode == "AUTO_HANDLE_AND_NOTIFY_HUMAN"
    assert plan.immediate_actions[0].action == "HOLD_AFFECTED_ROBOT"
    assert plan.pending_human_interaction is None
    assert [value.notification_type for value in plan.notifications] == [
        "HUMAN_WORK_REQUIRED"
    ]


def test_empty_generic_incident_event_is_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EventInput(type="operational_incident")


def test_loaded_robot_hitl_resume_keeps_dedicated_incident_route_locked(tmp_path) -> None:
    """H07 resumes the immutable incident route rather than entering Rule or Agent."""

    import pytest

    pytest.importorskip("langgraph")
    from app.domain.schemas import AutoMissionRequest, EventInput, HumanInteractionResumeRequest
    from app.services.hitl_service import HumanInteractionService, HumanInteractionStore
    from app.services.orchestration_service import OrchestrationService

    request = AutoMissionRequest(
        simulation_id="SIM-H07-LOCK",
        request_mode="event_driven",
        optimization_backend="cuopt_payload_only",
        events=[
            EventInput(
                type="operational_incident",
                robot_id="R004",
                payload={
                    "incident_id": "INC-R004-LOADED-LOCK",
                    "description": "R004가 적재한 채 고장",
                    "scope": "ROBOT",
                    "robot_operability": "FAULTED",
                    "load_state": "LOADED",
                    "physical_intervention_required": True,
                },
            )
        ],
    )
    first = OrchestrationService().run(request, trusted_planning_mode="force_rule")
    assert first.status == "awaiting_human_approval"
    assert first.orchestration_plan is not None
    assert first.orchestration_plan.formulation_route == "INCIDENT_RESPONSE"
    assert first.orchestration_plan.route_locked is True
    assert first.pending_human_interaction is not None
    assert first.pending_human_interaction.resume_route == "INCIDENT_RESPONSE"
    assert first.pending_human_interaction.route_locked is True

    # Move the file-backed checkpoint into the temporary test store.
    service = HumanInteractionService(HumanInteractionStore(tmp_path))
    record = service.create_pending(
        interaction=first.pending_human_interaction,
        state={
            "simulation_id": request.simulation_id,
            "request_mode": request.request_mode,
            "optimization_backend": request.optimization_backend,
            "events": list(request.events),
            "user_command": None,
            "requested_planning_mode": None,
            "max_agent_steps": request.max_agent_steps,
            "max_planner_retries": request.max_planner_retries,
            "human_responses": [],
            "parent_interaction_id": None,
        },
    )
    resumed = service.respond(
        record.interaction.interaction_id,
        HumanInteractionResumeRequest(
            action="SELECT",
            selected_option_id="MANUAL_RECOVERY",
            selected_entity_ids=["R004"],
            actor_id="maintenance-manager",
        ),
    )
    assert resumed.orchestration_result is not None
    result = resumed.orchestration_result
    assert result.status == "incident_handled"
    assert result.orchestration_plan is not None
    assert result.orchestration_plan.route == "INCIDENT_RESPONSE_PIPELINE"
    assert result.orchestration_plan.formulation_route == "INCIDENT_RESPONSE"
    assert result.orchestration_plan.route_locked is True
    assert result.orchestration_plan.route_switch_allowed is False

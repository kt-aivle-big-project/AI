"""Optional full LangGraph integration tests for v12 semantic-retrieval formulation paths."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("langgraph") is None:  # pragma: no cover - environment dependent
    pytest.skip("langgraph is not installed in this environment", allow_module_level=True)

from app.domain.schemas import AutoMissionRequest, EventInput
from app.repositories.json_repository import set_data_dir
from app.services.orchestration_service import OrchestrationService

ROOT = Path(__file__).resolve().parents[1]
TEN_ORDER_FIXTURE = ROOT / "scenarios" / "fixtures" / "V9_ten_orders_multitask"


def test_rule_formulation_full_graph_reaches_validated_payload() -> None:
    """Run the supplied one-order warehouse through situation graph and payload validation."""

    request = AutoMissionRequest(
        simulation_id="SIM-V12-RULE-GRAPH",
        request_mode="event_driven",
        planning_mode="force_rule",
        optimization_backend="cuopt_payload_only",
        events=[EventInput(type="new_order", order_id="ORD-001")],
    )
    result = OrchestrationService().run(request)
    assert result.status == "ready_for_cuopt"
    assert result.situation_graph_validation is None
    assert result.cuopt_dynamic_input_validation is not None and result.cuopt_dynamic_input_validation.valid
    assert result.payload_validation is not None and result.payload_validation.valid
    assert result.candidate_space_validation is not None and result.candidate_space_validation.valid
    assert result.warehouse_situation_graph is None
    assert result.cuopt_dynamic_input_draft is not None
    assert result.cuopt_dynamic_input_draft.formulation_source == "rule"
    assert "warehouse_situation_graph_builder" not in result.workflow_trace
    assert "rule_cuopt_formulator_direct" in result.workflow_trace
    assert "cuopt_dynamic_input_validator" in result.workflow_trace
    assert "policy_validation" not in result.workflow_trace


def test_ten_order_rule_formulation_keeps_all_tasks_and_five_robots() -> None:
    """Verify multi-order graph formulation before a real cuOpt/OR-Tools solve."""

    set_data_dir(TEN_ORDER_FIXTURE)
    try:
        request = AutoMissionRequest(
            simulation_id="SIM-V12-TEN-ORDER",
            request_mode="event_driven",
            planning_mode="force_rule",
            optimization_backend="cuopt_payload_only",
            events=[EventInput(type="new_order", order_id=f"ORD-{index:03d}") for index in range(1, 11)],
        )
        result = OrchestrationService().run(request)
        assert result.status == "ready_for_cuopt"
        assert result.cuopt_dynamic_input_draft is not None
        assert len(result.cuopt_dynamic_input_draft.tasks) == 10
        assert not result.cuopt_dynamic_input_draft.deferred_order_ids
        assert len(result.cuopt_dynamic_input_draft.fleet.included_robot_ids) == 5
        assert result.cuopt_payload is not None
        assert len(result.cuopt_payload.task_data.pickup_and_delivery_pairs) == 10
        assert len(result.cuopt_payload.task_data.task_ids) == 20
        assert len(result.cuopt_payload.fleet_data.vehicle_ids) == 5
    finally:
        set_data_dir(None)

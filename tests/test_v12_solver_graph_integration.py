"""Optional full LangGraph integration tests.

The tests are skipped when the LangGraph dependency is not installed.  They are
executed normally after ``pip install -r requirements.txt``.
"""
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
FIXTURE = ROOT / "scenarios" / "fixtures" / "V9_ten_orders_multitask"


def test_payload_only_full_graph_exports_twenty_task_rows() -> None:
    """Compile ten structured orders through the real graph to the payload-only terminal."""

    set_data_dir(FIXTURE)
    try:
        request = AutoMissionRequest(
            simulation_id="SIM-V9-PAYLOAD",
            request_mode="event_driven",
            planning_mode="force_rule",
            optimization_backend="cuopt_payload_only",
            events=[EventInput(type="new_order", order_id=f"ORD-{index:03d}") for index in range(1, 11)],
        )
        result = OrchestrationService().run(request)
        assert result.status == "ready_for_cuopt"
        assert result.payload_validation is not None and result.payload_validation.valid
        assert result.candidate_space_validation is not None and result.candidate_space_validation.valid
        assert result.cuopt_payload is not None
        assert len(result.cuopt_payload.task_data.pickup_and_delivery_pairs) == 10
        assert len(result.cuopt_payload.task_data.task_ids) == 20
        assert "optimizer" not in result.workflow_trace
    finally:
        set_data_dir(None)

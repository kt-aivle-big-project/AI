"""Native main-LangGraph integration contracts for G2P fulfillment."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from app.core.config import get_settings
from app.domain.schemas import AutoMissionRequest, EventInput
from app.graph.build_graph import get_laro_graph
from app.repositories.json_repository import set_data_dir
from app.services.orchestration_service import OrchestrationService

ROOT = Path(__file__).resolve().parents[1]
RETURN_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_return"
DEPLETED_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_depleted"
MULTI_HU_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_multi_hu"
ORDER_IDS = [f"ORD-{index:03d}" for index in range(1, 6)]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fixture: Path,
    backend: str,
):
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    monkeypatch.setenv("ALLOW_REQUEST_PLANNING_MODE_OVERRIDE", "true")
    monkeypatch.setenv("DEFAULT_PLANNING_MODE", "llm_router")
    monkeypatch.setenv("FRONTEND_EXPLANATION_MODE", "deterministic")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    get_settings.cache_clear()
    get_laro_graph.cache_clear()
    set_data_dir(fixture)
    try:
        return OrchestrationService().run(
            AutoMissionRequest(
                simulation_id="SIM-G2P-INTEGRATED",
                request_mode="event_driven",
                planning_mode="force_rule",
                optimization_backend=backend,
                events=[
                    EventInput(type="new_order", order_id=order_id)
                    for order_id in ORDER_IDS
                ],
            )
        )
    finally:
        set_data_dir(None)
        get_settings.cache_clear()
        get_laro_graph.cache_clear()


def test_main_orchestration_compiles_five_orders_to_one_common_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(monkeypatch, fixture=RETURN_FIXTURE, backend="cuopt_payload_only")

    assert result.status == "ready_for_cuopt", result.model_dump(mode="json")
    assert result.goods_to_person_compilation is not None
    compilation = result.goods_to_person_compilation
    assert compilation.applied
    assert len(compilation.batches) == 1
    batch = compilation.batches[0]
    assert batch.order_ids == ORDER_IDS
    assert batch.requested_quantity == 8
    assert batch.quantity_after == 4
    assert batch.return_required
    assert result.cuopt_payload is not None
    assert len(result.cuopt_payload.task_data.pickup_and_delivery_pairs) == 1
    assert "goods_to_person_compiler" in result.workflow_trace
    assert result.orchestration_plan is not None and result.orchestration_plan.route_locked


def test_main_orchestration_exposes_multiple_cycles_in_one_common_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(monkeypatch, fixture=MULTI_HU_FIXTURE, backend="cuopt_payload_only")

    assert result.status == "ready_for_cuopt", result.model_dump(mode="json")
    assert result.goods_to_person_compilation is not None
    assert len(result.goods_to_person_compilation.batches) == 2
    assert result.cuopt_payload is not None
    assert len(result.cuopt_payload.task_data.pickup_and_delivery_pairs) == 2
    assert sum(
        batch.requested_quantity for batch in result.goods_to_person_compilation.batches
    ) == 15


def test_main_orchestration_enriches_same_amr_post_station_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("ortools")
    result = _run(monkeypatch, fixture=DEPLETED_FIXTURE, backend="ortools")

    assert result.status == "plan_validated", result.model_dump(mode="json")
    assert result.goods_to_person_compilation is not None
    assert result.goods_to_person_route_enrichment is not None
    assert result.goods_to_person_route_enrichment.applied
    assert result.goods_to_person_route_enrichment.valid
    assert result.execution_payload is not None
    assert result.execution_optimizer_result is not None
    assert any(
        value.endswith("_EMPTY_TOTE")
        for value in result.execution_payload.task_data.task_ids
    )
    assert result.traffic_schedule is not None and result.traffic_schedule.valid
    assert result.traffic_schedule.station_reservations
    assert result.route_validation is not None and result.route_validation.valid
    assert result.mapf_validation is not None and result.mapf_validation.valid

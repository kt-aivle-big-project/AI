from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core.config import get_settings
from app.domain.schemas import (
    AutoMissionRequest,
    ContextSnapshot,
    EventInput,
    NormalizedOperation,
    NormalizedWarehouseRequest,
)
from app.repositories.json_repository import get_repository, set_data_dir
from app.services.planning_evaluation_service import (
    PlanningEvaluationCaptureService,
    PlanningEvaluationStore,
)

FIXTURE = Path(__file__).resolve().parents[1] / "scenarios" / "fixtures" / "V18_mixed_inbound_outbound"


class FakeResult:
    def __init__(self) -> None:
        self.normalized_request = NormalizedWarehouseRequest(
            source="structured_events",
            operations=[
                NormalizedOperation(
                    operation_id="ORD-001",
                    operation_type="OUTBOUND_ORDER",
                    source_event_type="new_order",
                )
            ],
            normalization_summary="frozen evaluation capture",
        )
        self.context_snapshot = ContextSnapshot(
            snapshot_id="SNAP-EVAL",
            captured_at="2026-07-29T00:00:00Z",
            graph_version="MAP-EVAL",
            inventory_version="INV-EVAL",
            runtime_version="RUN-EVAL",
        )
        self.inventory_context = None
        self.robot_context = None
        self.map_context = None
        self.orchestration_plan = SimpleNamespace(
            formulation_route="RULE_FORMULATION"
        )
        self.status = "ready_for_cuopt"

    def model_dump(self, mode: str = "json") -> dict:
        del mode
        return {
            "status": self.status,
            "normalized_request": self.normalized_request.model_dump(mode="json"),
            "context_snapshot": self.context_snapshot.model_dump(mode="json"),
        }


def test_capture_store_freezes_repository_and_primary_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PLANNING_EVALUATION_MODE", "capture_only")
    monkeypatch.setenv("PLANNING_EVALUATION_PERSIST", "true")
    monkeypatch.setenv("PLANNING_EVALUATION_OUTPUT_DIR", str(tmp_path / "evaluations"))
    monkeypatch.setenv("WAREHOUSE_REPOSITORY_BACKEND", "json")
    monkeypatch.setenv("MAP_REPOSITORY_BACKEND", "json")
    get_settings.cache_clear()
    get_repository.cache_clear()
    set_data_dir(FIXTURE)
    try:
        request = AutoMissionRequest(
            warehouse_id="WH-001",
            simulation_id="SIM-EVAL",
            request_mode="event_driven",
            events=[EventInput(type="new_order", order_id="ORD-001")],
        )
        store = PlanningEvaluationStore(tmp_path / "evaluations")
        reference = PlanningEvaluationCaptureService(store).capture(
            raw_request=request,
            internal_request=request,
            result=FakeResult(),  # type: ignore[arg-type]
            request_kind="PLAN",
            plan=None,
        )
        assert reference is not None
        detail = store.detail(reference.evaluation_id)
        assert detail["manifest"]["primary_route"] == "RULE_FORMULATION"
        frozen = store.capture_dir(reference.evaluation_id) / "frozen_repository"
        assert (frozen / "warehouse_graph.json").exists()
        assert (frozen / "rack_inventory.json").exists()
        assert detail["files"]["normalized_request.json"]["operations"][0]["operation_id"] == "ORD-001"
    finally:
        set_data_dir(None)
        get_repository.cache_clear()
        get_settings.cache_clear()


def test_rule_applicability_accepts_normalized_natural_ids_but_rejects_unresolved_references() -> None:
    from app.services.planning_evaluation_service import PlanningComparisonService

    applicable, reasons = PlanningComparisonService._rule_applicable(
        {
            "operations": [
                {"operation_id": "ORD-001", "operation_type": "OUTBOUND_ORDER"}
            ],
            "constraints": {"excluded_robot_ids": ["R003"]},
            "user_clarification_questions": [],
            "incidents": [],
        }
    )
    assert applicable and not reasons

    applicable, reasons = PlanningComparisonService._rule_applicable(
        {
            "operations": [
                {"operation_id": "ORD-001", "operation_type": "OUTBOUND_ORDER"}
            ],
            "constraints": {"soft_avoid_edge_references": ["혼잡한 중앙 통로"]},
            "user_clarification_questions": [],
            "incidents": [],
        }
    )
    assert not applicable
    assert "UNRESOLVED_SEMANTIC_REFERENCE" in reasons

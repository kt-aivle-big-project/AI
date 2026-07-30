from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph")

from app.core.config import get_settings
from app.main import app
from app.repositories.json_repository import get_repository, set_data_dir


FIXTURE = Path(__file__).resolve().parents[1] / "scenarios" / "fixtures" / "V18_mixed_inbound_outbound"


def test_plan_api_auto_captures_frozen_evaluation_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_PLANNING_MODE", "force_rule")
    monkeypatch.setenv("PLANNING_EVALUATION_MODE", "capture_only")
    monkeypatch.setenv("PLANNING_EVALUATION_PERSIST", "true")
    monkeypatch.setenv("PLANNING_EVALUATION_OUTPUT_DIR", str(tmp_path / "evaluations"))
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    monkeypatch.setenv("WAREHOUSE_REPOSITORY_BACKEND", "json")
    monkeypatch.setenv("MAP_REPOSITORY_BACKEND", "json")
    get_settings.cache_clear()
    get_repository.cache_clear()
    set_data_dir(FIXTURE)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/warehouses/WH-001/missions/plan",
                json={
                    "warehouse_id": "WH-001",
                    "simulation_id": "SIM-EVAL-CAPTURE",
                    "optimization_backend": "cuopt_payload_only",
                    "events": [
                        {"type": "new_order", "order_id": "ORD-001"},
                        {"type": "inbound_item_arrived", "inbound_id": "IN-001"},
                    ],
                },
            )
            assert response.status_code == 200
            body = response.json()
            evaluation_id = body["evaluation_id"]
            assert evaluation_id.startswith("EVAL-WH-001-SIM-EVAL-CAPTURE-")

            detail = client.get(f"/api/v1/debug/evaluations/{evaluation_id}")
            assert detail.status_code == 200
            payload = detail.json()
            assert payload["manifest"]["status"] == "CAPTURED"
            assert payload["manifest"]["comparison_status"] == "NOT_STARTED"
            assert payload["files"]["normalized_request.json"]["operations"]
            assert payload["files"]["context_snapshot.json"]["repository_versions"]

            frozen = tmp_path / "evaluations" / "captures" / evaluation_id / "frozen_repository"
            assert (frozen / "warehouse_graph.json").exists()
            assert (frozen / "rack_inventory.json").exists()
            assert (frozen / "scenario_state.json").exists()
            assert (frozen / "facility_resources.json").exists()

            listing = client.get("/api/v1/debug/evaluations")
            assert listing.status_code == 200
            ids = {value["evaluation_id"] for value in listing.json()["evaluations"]}
            assert evaluation_id in ids
    finally:
        set_data_dir(None)
        get_repository.cache_clear()
        get_settings.cache_clear()

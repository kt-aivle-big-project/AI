from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph")

from app.core.config import get_settings
from app.main import app
from app.repositories.json_repository import get_repository, set_data_dir


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "scenarios"
    / "fixtures"
    / "V18_mixed_inbound_outbound"
)


def test_native_plan_api_returns_plan_and_compact_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_PLANNING_MODE", "force_rule")
    monkeypatch.setenv("OPTIMIZATION_BACKEND", "ortools")
    monkeypatch.setenv("FRONTEND_EXPLANATION_MODE", "deterministic")
    monkeypatch.setenv("PLANNING_EVALUATION_MODE", "off")
    monkeypatch.setenv("WAREHOUSE_REPOSITORY_BACKEND", "json")
    monkeypatch.setenv("MAP_REPOSITORY_BACKEND", "json")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    get_settings.cache_clear()
    get_repository.cache_clear()
    set_data_dir(FIXTURE)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/warehouses/WH-001/missions/plan",
                json={
                    "warehouse_id": "WH-001",
                    "simulation_id": "SIM-V18-MIXED",
                    "optimization_backend": "ortools",
                    "events": [
                        {"type": "new_order", "order_id": "ORD-001"},
                        {
                            "type": "inbound_item_arrived",
                            "inbound_id": "IN-001",
                        },
                    ],
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "plan_validated"
            assert body["final_route"] == "RULE_FORMULATION"
            assert body["router_llm_executed"] is False
            assert body["plan"] is not None
            plan = body["plan"]
            assert plan["robots"]
            assert any(
                step["step_type"] == "MOVE"
                for robot in plan["robots"]
                for step in robot["steps"]
            )
            assert any(
                step["step_type"] == "SERVICE"
                for robot in plan["robots"]
                for step in robot["steps"]
            )

            trace = client.get(
                f"/api/v1/warehouses/WH-001/missions/plans/{plan['plan_id']}/trace"
            )
            assert trace.status_code == 200
            trace_body = trace.json()
            assert trace_body["workflow_status"] == "plan_validated"
            assert trace_body["checks"]["payload_valid"] is True
            assert trace_body["checks"]["candidate_space_valid"] is True
            assert trace_body["checks"]["assignment_valid"] is True
            assert trace_body["checks"]["route_valid"] is True
            assert trace_body["checks"]["mapf_valid"] is True
            assert "prioritized_mapf_planner" in trace_body["workflow_trace"]
    finally:
        set_data_dir(None)
        get_repository.cache_clear()
        get_settings.cache_clear()

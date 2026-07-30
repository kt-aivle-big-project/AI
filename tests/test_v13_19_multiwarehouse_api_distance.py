from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.schemas import (
    EventInput,
    PublicMissionRequest,
    RobotRuntime,
    RobotRuntimeContext,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
)
from app.graph.robot_runtime import robot_runtime_node
from app.infrastructure.embedded import (
    EmbeddedNeo4jMapRepository,
    EmbeddedPostgresWarehouseAdapter,
    EmbeddedRedisRuntimeAdapter,
)
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.context_service import WarehouseContextService

ROOT = Path(__file__).resolve().parents[1]
RETURN_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_return"


def _documents(root: Path) -> tuple[dict, dict, dict, dict]:
    graph = json.loads((root / "warehouse_graph.json").read_text(encoding="utf-8"))
    inventory = json.loads((root / "rack_inventory.json").read_text(encoding="utf-8"))
    scenario = json.loads((root / "scenario_state.json").read_text(encoding="utf-8"))
    facility = json.loads((root / "facility_resources.json").read_text(encoding="utf-8"))
    return graph, inventory, scenario, facility


def test_public_api_infers_request_mode_and_rejects_request_mode_field() -> None:
    structured = PublicMissionRequest(
        warehouse_id="wh-seoul-01",
        simulation_id="SIM-1",
        events=[EventInput(type="new_order", order_id="ORD-001")],
    )
    assert structured.warehouse_id == "WH-SEOUL-01"
    assert structured.to_internal().request_mode == "event_driven"

    natural = PublicMissionRequest(
        warehouse_id="WH-SEOUL-01",
        simulation_id="SIM-2",
        user_command="ORD-001을 처리해.",
    )
    assert natural.to_internal().request_mode == "human_command"

    mixed = PublicMissionRequest(
        warehouse_id="WH-SEOUL-01",
        simulation_id="SIM-3",
        events=[EventInput(type="new_order", order_id="ORD-001")],
        user_command="R003은 제외해.",
    )
    assert mixed.to_internal().request_mode == "mixed"

    with pytest.raises(ValidationError):
        PublicMissionRequest.model_validate(
            {
                "warehouse_id": "WH-SEOUL-01",
                "simulation_id": "SIM-4",
                "request_mode": "event_driven",
                "events": [{"type": "new_order", "order_id": "ORD-001"}],
            }
        )


def test_embedded_stores_isolate_same_ids_by_warehouse(tmp_path: Path) -> None:
    graph, inventory, scenario, facility = _documents(RETURN_FIXTURE)
    settings = Settings(
        WAREHOUSE_REPOSITORY_BACKEND="embedded",
        LOCAL_DB_DIR=tmp_path,
        DEFAULT_WAREHOUSE_ID="WH-001",
    )
    pg = EmbeddedPostgresWarehouseAdapter(settings, tmp_path / "pg.sqlite3")
    redis = EmbeddedRedisRuntimeAdapter(settings, tmp_path / "redis.sqlite3")
    neo4j = EmbeddedNeo4jMapRepository(settings, tmp_path / "neo4j.sqlite3")

    for warehouse_id in ("WH-001", "WH-002"):
        pg.seed_from_documents(
            warehouse_id=warehouse_id,
            inventory=inventory,
            scenario=scenario,
            facility=facility,
            replace=True,
        )
        redis.seed_from_documents(
            warehouse_id=warehouse_id,
            scenario=scenario,
            facility=facility,
            replace=True,
        )
        neo4j.load_route_graph(
            warehouse_id=warehouse_id,
            nodes=graph["nodes"],
            edges=graph["edges"],
            replace=True,
        )

    assert pg.get_order("WH-001", "ORD-001")["warehouse_id"] == "WH-001"
    assert pg.get_order("WH-002", "ORD-001")["warehouse_id"] == "WH-002"

    assert redis.update_robot_state(
        warehouse_id="WH-002",
        simulation_id="SIM-G2P-RETURN",
        robot_id="R001",
        sequence=99,
        state={"battery_pct": 11.0, "status": "idle", "current_node": "R0_0"},
    )
    wh1 = redis.get_robot("WH-001", "SIM-G2P-RETURN", "R001")
    wh2 = redis.get_robot("WH-002", "SIM-G2P-RETURN", "R001")
    assert wh1 is not None and float(wh1["battery_pct"]) != 11.0
    assert wh2 is not None and float(wh2["battery_pct"]) == 11.0

    assert neo4j.graph_counts("WH-001") == neo4j.graph_counts("WH-002") == {
        "nodes": 220,
        "edges": 356,
    }


def test_explicit_physical_edge_metrics_are_authoritative() -> None:
    repository = JsonWarehouseRepository(RETURN_FIXTURE, warehouse_id="WH-001")
    edge_id = next(iter(repository.edges))
    edge = repository.edges[edge_id]
    distance_m, travel_time_ms = repository.base_edge_metrics(edge_id)
    assert distance_m == pytest.approx(float(edge["distance_m"]))
    assert travel_time_ms == int(edge["nominal_travel_time_ms"])
    arcs = repository.adjusted_arcs(
        blocked_edge_ids=set(),
        blocked_node_ids=set(),
        edge_penalties={edge_id: (2.0, 2.0)},
    )
    arc = next(value for value in arcs if value["edge_id"] == edge_id)
    assert arc["distance_m"] == pytest.approx(distance_m)
    assert arc["cost"] == pytest.approx(distance_m * 2.0)
    assert arc["travel_time_ms"] == travel_time_ms * 2


def test_low_battery_runtime_override_is_filtered_by_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    base = RobotRuntimeContext(
        robots=[
            RobotRuntime(
                warehouse_id="WH-001",
                robot_id="R001",
                robot_code="R001",
                status="idle",
                battery_pct=80,
                capacity_units=1,
                current_node="R0_0",
            ),
            RobotRuntime(
                warehouse_id="WH-001",
                robot_id="R002",
                robot_code="R002",
                status="idle",
                battery_pct=75,
                capacity_units=1,
                current_node="R0_1",
            ),
        ],
        candidate_robot_ids=["R001", "R002"],
        min_battery_pct=30,
        min_capacity_units=1,
        summary="base",
    )
    monkeypatch.setattr(
        WarehouseContextService,
        "build_robot_context",
        lambda self, required_capacity=1: base,
    )
    result = robot_runtime_node(
        {
            "runtime_overrides": RuntimePlanningOverrides(
                robot_states=[
                    RobotRuntimeOverride(
                        robot_id="R002",
                        current_node="R0_1",
                        battery_pct=12,
                        status="idle",
                    )
                ]
            ),
            "workflow_trace": [],
        }
    )
    context = result["robot_context"]
    assert context.candidate_robot_ids == ["R001"]
    assert context.excluded_by_reason["low_battery"] == ["R002"]


def test_public_runtime_snapshot_becomes_internal_override() -> None:
    from app.domain.schemas import PublicRuntimeSnapshot

    request = PublicMissionRequest(
        warehouse_id="WH-001",
        simulation_id="SIM-SNAPSHOT",
        events=[EventInput(type="new_order", order_id="ORD-001")],
        runtime_snapshot=PublicRuntimeSnapshot(
            captured_at_sim_time_ms=12_300,
            robot_states=[
                RobotRuntimeOverride(
                    robot_id="R002",
                    current_node="R0_1",
                    status="idle",
                    battery_pct=18,
                )
            ],
        ),
    )
    internal = request.to_internal()
    assert internal.runtime_overrides.robot_states[0].battery_pct == 18
    assert internal.runtime_overrides.robot_states[0].sim_time_ms == 12_300


def test_agent_and_rule_share_same_runtime_override_filter() -> None:
    from app.services.context_service import apply_runtime_overrides

    base = RobotRuntimeContext(
        warehouse_id="WH-002",
        simulation_id="SIM-SHARED",
        robots=[
            RobotRuntime(
                warehouse_id="WH-002",
                simulation_id="SIM-SHARED",
                robot_id="R001",
                robot_code="R001",
                status="idle",
                battery_pct=80,
                capacity_units=1,
                current_node="R0_0",
            )
        ],
        candidate_robot_ids=["R001"],
        min_battery_pct=30,
        min_capacity_units=1,
        summary="base",
    )
    resolved = apply_runtime_overrides(
        base,
        RuntimePlanningOverrides(
            robot_states=[
                RobotRuntimeOverride(
                    robot_id="R001",
                    current_node="R0_0",
                    status="idle",
                    battery_pct=10,
                )
            ]
        ),
    )
    assert resolved.warehouse_id == "WH-002"
    assert resolved.candidate_robot_ids == []
    assert resolved.excluded_by_reason["low_battery"] == ["R001"]

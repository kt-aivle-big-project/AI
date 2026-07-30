from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.domain.schemas import (
    EventInput,
    PublicMissionRequest,
    PublicReplanMissionRequest,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
)
from app.infrastructure.embedded import (
    EmbeddedNeo4jMapRepository,
    EmbeddedPostgresWarehouseAdapter,
    EmbeddedRedisRuntimeAdapter,
)
from app.infrastructure.redis_runtime import RedisRuntimeAdapter
from app.repositories.json_repository import DataContractError, JsonWarehouseRepository


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def _documents() -> tuple[dict, dict, dict, dict]:
    inventory = json.loads((DATA / "rack_inventory.json").read_text(encoding="utf-8"))
    scenario = json.loads((DATA / "scenario_state.json").read_text(encoding="utf-8"))
    facility = json.loads((DATA / "facility_resources.json").read_text(encoding="utf-8"))
    graph = json.loads((DATA / "warehouse_graph.json").read_text(encoding="utf-8"))
    return inventory, scenario, facility, graph


def test_public_api_infers_request_mode_and_rejects_request_mode_field() -> None:
    event_request = PublicMissionRequest(
        warehouse_id="wh-001",
        simulation_id="SIM-1",
        events=[EventInput(type="new_order", order_id="ORD-001")],
    )
    assert event_request.to_internal().request_mode == "event_driven"
    assert event_request.to_internal().warehouse_id == "WH-001"

    command_request = PublicMissionRequest(
        warehouse_id="WH-001",
        simulation_id="SIM-2",
        user_command="ORD-001을 처리해.",
    )
    assert command_request.to_internal().request_mode == "human_command"

    mixed_request = PublicMissionRequest(
        warehouse_id="WH-001",
        simulation_id="SIM-3",
        events=[EventInput(type="new_order", order_id="ORD-001")],
        user_command="R003은 제외해.",
    )
    assert mixed_request.to_internal().request_mode == "mixed"

    with pytest.raises(ValidationError):
        PublicMissionRequest.model_validate(
            {
                "warehouse_id": "WH-001",
                "simulation_id": "SIM-1",
                "request_mode": "event_driven",
                "events": [{"type": "new_order", "order_id": "ORD-001"}],
            }
        )


def test_public_replan_preserves_warehouse_scope() -> None:
    request = PublicReplanMissionRequest(
        active_plan_id="PLAN-1",
        sim_time_ms=1000,
        mission=PublicMissionRequest(
            warehouse_id="WH-002",
            simulation_id="SIM-2",
            events=[EventInput(type="new_order", order_id="ORD-001")],
        ),
    )
    internal = request.to_internal()
    assert internal.mission.warehouse_id == "WH-002"
    assert internal.mission.request_mode == "event_driven"


def test_json_warehouses_are_isolated_and_unknown_warehouse_fails() -> None:
    primary = JsonWarehouseRepository(warehouse_id="WH-001")
    secondary = JsonWarehouseRepository(warehouse_id="WH-002")
    assert primary.get_order("ORD-001")["priority"] == "high"
    assert secondary.get_order("ORD-001")["priority"] == "low"
    assert primary.data_root != secondary.data_root

    with pytest.raises(DataContractError, match="No JSON data directory"):
        JsonWarehouseRepository(warehouse_id="WH-NOT-REGISTERED")


def test_embedded_three_stores_isolate_same_ids_by_warehouse(tmp_path: Path) -> None:
    inventory, scenario, facility, graph = _documents()
    scenario_a = copy.deepcopy(scenario)
    scenario_b = copy.deepcopy(scenario)
    scenario_a["simulation_id"] = "SIM-SAME"
    scenario_b["simulation_id"] = "SIM-SAME"
    scenario_a["orders"][0]["priority"] = "high"
    scenario_b["orders"][0]["priority"] = "low"
    scenario_a["robots"][0]["battery_pct"] = 90.0
    scenario_b["robots"][0]["battery_pct"] = 45.0

    postgres = EmbeddedPostgresWarehouseAdapter(path=tmp_path / "postgres.sqlite3")
    redis = EmbeddedRedisRuntimeAdapter(path=tmp_path / "redis.sqlite3")
    neo4j = EmbeddedNeo4jMapRepository(path=tmp_path / "neo4j.sqlite3")

    for warehouse_id, scenario_doc in (("WH-A", scenario_a), ("WH-B", scenario_b)):
        postgres.seed_from_documents(
            warehouse_id=warehouse_id,
            inventory=inventory,
            scenario=scenario_doc,
            facility=facility,
            replace=True,
        )
        redis.seed_from_documents(
            warehouse_id=warehouse_id,
            scenario=scenario_doc,
            facility=facility,
            replace=True,
        )
        neo4j.load_route_graph(
            warehouse_id=warehouse_id,
            nodes=graph["nodes"],
            edges=graph["edges"],
            replace=True,
        )

    assert postgres.get_order("WH-A", "ORD-001")["priority"] == "high"
    assert postgres.get_order("WH-B", "ORD-001")["priority"] == "low"
    assert redis.get_robot("WH-A", "SIM-SAME", "R001")["battery_pct"] == 90.0
    assert redis.get_robot("WH-B", "SIM-SAME", "R001")["battery_pct"] == 45.0
    assert neo4j.graph_counts("WH-A") == {"nodes": 220, "edges": 356}
    assert neo4j.graph_counts("WH-B") == {"nodes": 220, "edges": 356}


def test_redis_key_namespace_contains_warehouse_and_simulation() -> None:
    adapter = RedisRuntimeAdapter(client=object())
    a = adapter.robot_key("WH-A", "SIM-1", "R001")
    b = adapter.robot_key("WH-B", "SIM-1", "R001")
    assert a != b
    assert "warehouse:WH-A:sim:SIM-1" in a
    assert "warehouse:WH-B:sim:SIM-1" in b


def test_edges_expose_physical_distance_and_penalty_does_not_change_distance() -> None:
    repository = JsonWarehouseRepository(warehouse_id="WH-001")
    edge_id = next(iter(repository.edges))
    edge = repository.edges[edge_id]
    assert edge["distance_m"] > 0
    assert edge["speed_limit_mps"] > 0
    assert edge["nominal_travel_time_ms"] > 0

    base_distance, base_time = repository.base_edge_metrics(edge_id)
    arc = next(
        value
        for value in repository.adjusted_arcs(
            blocked_edge_ids=set(),
            blocked_node_ids=set(),
            edge_penalties={edge_id: (2.0, 1.5)},
        )
        if value["edge_id"] == edge_id
    )
    assert arc["distance_m"] == base_distance
    assert arc["cost"] == pytest.approx(base_distance * 2.0)
    assert arc["travel_time_ms"] == round(base_time * 1.5)


def test_simulation_step_can_expose_physical_motion_contract() -> None:
    distance = 2.5
    speed = 1.0
    step = SimulationPlanStep(
        step_id="R001-0001",
        sequence=1,
        step_type="MOVE",
        start_at_ms=0,
        end_at_ms=2500,
        edge_id="E-1",
        from_node="A",
        to_node="B",
        distance_m=distance,
        nominal_speed_mps=speed,
        nominal_travel_time_ms=2500,
    )
    plan = SimulationPlan(
        plan_id="PLAN-WH-A-SIM-1",
        plan_version=1,
        warehouse_id="WH-A",
        simulation_id="SIM-1",
        map_version="MAP-1",
        makespan_ms=2500,
        absolute_finish_at_ms=2500,
        robots=[
            SimulationRobotPlan(
                robot_id="R001",
                initial_node="A",
                finish_at_ms=2500,
                steps=[step],
            )
        ],
    )
    assert plan.warehouse_id == "WH-A"
    assert plan.robots[0].steps[0].distance_m == 2.5

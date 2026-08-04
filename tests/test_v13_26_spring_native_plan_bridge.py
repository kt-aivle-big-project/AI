from __future__ import annotations

from types import SimpleNamespace

from app.domain.schemas import PublicMissionRequest
from app.repositories.json_repository import create_request_repository
from app.repositories.live_repository import LiveWarehouseRepository


def test_public_request_accepts_spring_run_id_and_preserves_it_internally() -> None:
    public = PublicMissionRequest.model_validate(
        {
            "warehouse_id": "WH-001",
            "simulation_id": "PLAN-SESSION-1",
            "simulationRunId": 42,
            "events": [{"type": "new_order", "order_id": "ORD-001"}],
        }
    )

    assert public.simulation_run_id == 42
    assert public.to_internal().simulation_run_id == 42


def test_request_repository_forwards_spring_run_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create(warehouse_id, simulation_id, data_override, simulation_run_id=None):
        captured.update(
            warehouse_id=warehouse_id,
            simulation_id=simulation_id,
            data_override=data_override,
            simulation_run_id=simulation_run_id,
        )
        return object()

    monkeypatch.setattr(
        "app.repositories.json_repository._create_repository",
        fake_create,
    )

    create_request_repository("WH-001", "PLAN-SESSION-1", 42)

    assert captured == {
        "warehouse_id": "WH-001",
        "simulation_id": "PLAN-SESSION-1",
        "data_override": None,
        "simulation_run_id": 42,
    }


def test_spring_runtime_is_converted_to_native_robot_and_edge_records() -> None:
    robot = SimpleNamespace(
        robot_id=7,
        current_node_code="N-17",
        current_node_id=17,
        status="MOVING",
        battery_level=78,
        capacity_units=2,
        current_edge_code="E-17-18",
        current_task_id=None,
        current_load_units=1,
        handling_unit_code="HU-1",
        sim_time_ms=1000,
        step_end_at_ms=1500,
    )
    tasks = [
        {
            "task_id": "5001",
            "robot_id": "7",
            "status": "ASSIGNED",
        }
    ]

    records = LiveWarehouseRepository._spring_robot_records(
        [robot], tasks, run_sim_time_ms=1200
    )

    assert records == [
        {
            "robot_id": "7",
            "robot_code": "7",
            "status": "moving",
            "battery_pct": 78.0,
            "capacity_units": 2,
            "current_node": "N-17",
            "current_edge": "E-17-18",
            "active_task_id": "5001",
            "load_state": "LOADED",
            "current_load_units": 1,
            "sim_time_ms": 1200,
            "available_at_ms": 1500,
        }
    ]

    edge = SimpleNamespace(
        edge_id=99,
        edge_code="E-17-18",
        status="BLOCKED",
        cost_multiplier=1.0,
        travel_time_multiplier=1.5,
        occupied_by_robot_id=7,
        blocked_until_ms=2000,
    )
    assert LiveWarehouseRepository._spring_edge_records([edge]) == [
        {
            "edge_id": "E-17-18",
            "status": "blocked",
            "cost_multiplier": 1.0,
            "travel_time_multiplier": 1.5,
            "occupied_by_robot_id": "7",
            "blocked_until_ms": 2000,
        }
    ]


def test_spring_source_manifest_and_active_tasks_are_explicit() -> None:
    repository = object.__new__(LiveWarehouseRepository)
    repository.simulation_run_id = 42
    repository.scenario = {
        "spring_tasks": [
            {"task_id": "1", "status": "PENDING"},
            {"task_id": "2", "status": "COMPLETED"},
        ]
    }

    assert repository.source_manifest["robots"] == "spring_redis"
    assert repository.source_manifest["tasks"] == "public.task"
    assert repository.active_operations() == [{"task_id": "1", "status": "PENDING"}]

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.domain.schemas import (
    RobotRuntimeContext,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
)
from app.infrastructure.embedded import EmbeddedRedisRuntimeAdapter
from app.services.context_service import apply_runtime_overrides


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        WAREHOUSE_REPOSITORY_BACKEND="embedded",
        LOCAL_DB_DIR=tmp_path,
        LOCAL_REDIS_PATH=tmp_path / "redis.sqlite3",
        DEFAULT_WAREHOUSE_ID="WH-001",
    )


def test_embedded_runtime_clone_isolated_namespace(tmp_path: Path) -> None:
    adapter = EmbeddedRedisRuntimeAdapter(_settings(tmp_path))
    scenario = {
        "warehouse_id": "WH-001",
        "simulation_id": "SIM-SOURCE",
        "robots": [
            {
                "robot_id": "R001",
                "robot_code": "R001",
                "status": "idle",
                "battery_pct": 80,
                "capacity_units": 4,
                "current_node": "R0_0",
            }
        ],
        "edge_runtime": [
            {"edge_id": "E001", "status": "congested", "cost_multiplier": 1.2}
        ],
        "edge_reservations": [
            {
                "reservation_id": "RES-1",
                "edge_id": "E001",
                "robot_id": "R001",
                "start_at_ms": 0,
                "end_at_ms": 1000,
            }
        ],
    }
    facility = {
        "outbound_stations": [
            {
                "station_id": "OUT_STATION_1",
                "station_robot_id": "SR-01",
                "status": "available",
            }
        ]
    }
    adapter.seed_from_documents(
        warehouse_id="WH-001", scenario=scenario, facility=facility, replace=True
    )

    result = adapter.clone_simulation_runtime(
        warehouse_id="WH-001",
        source_simulation_id="SIM-SOURCE",
        target_simulation_id="SIM-C01",
        reset=True,
        copy_reservations=False,
    )

    assert result["status"] == "BOOTSTRAPPED"
    assert result["robots"] == 1
    assert result["edges"] == 1
    assert result["stations"] == 1
    assert result["reservations"] == 0
    assert adapter.all_robots("WH-001", "SIM-C01")[0]["simulation_id"] == "SIM-C01"
    assert adapter.edge_runtime("WH-001", "SIM-C01")[0]["edge_id"] == "E001"
    assert adapter.station_runtime("WH-001", "SIM-C01")[0]["station_id"] == "OUT_STATION_1"
    assert adapter.existing_reservations("WH-001", "SIM-C01") == []


def test_complete_runtime_snapshot_materializes_robots_without_redis() -> None:
    context = RobotRuntimeContext(
        warehouse_id="WH-001",
        simulation_id="SIM-C20",
        robots=[],
        candidate_robot_ids=[],
        excluded_by_reason={},
        min_battery_pct=30,
        min_capacity_units=1,
        summary="No Redis runtime.",
    )
    overrides = RuntimePlanningOverrides(
        runtime_snapshot_mode="COMPLETE",
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R001",
                current_node="R0_0",
                status="idle",
                battery_pct=90,
                capacity_units=4,
                current_load_units=0,
            ),
            RobotRuntimeOverride(
                robot_id="R002",
                current_node="R0_1",
                status="idle",
                battery_pct=10,
                capacity_units=4,
                current_load_units=0,
            ),
        ],
    )

    effective = apply_runtime_overrides(context, overrides)

    assert [value.robot_id for value in effective.robots] == ["R001", "R002"]
    assert effective.candidate_robot_ids == ["R001"]
    assert effective.excluded_by_reason["low_battery"] == ["R002"]


def test_replan_horizon_clamps_override_only_robot() -> None:
    context = RobotRuntimeContext(
        warehouse_id="WH-001",
        simulation_id="SIM-C07",
        robots=[],
        candidate_robot_ids=[],
        excluded_by_reason={},
        min_battery_pct=30,
        min_capacity_units=1,
        summary="No Redis runtime.",
    )
    overrides = RuntimePlanningOverrides(
        runtime_snapshot_mode="COMPLETE",
        planning_horizon_start_ms=3000,
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R002",
                current_node="R1_5",
                status="idle",
                battery_pct=80,
                capacity_units=8,
                current_load_units=0,
                sim_time_ms=0,
            )
        ],
    )

    effective = apply_runtime_overrides(context, overrides)

    assert effective.robots[0].sim_time_ms == 3000
    assert effective.candidate_robot_ids == ["R002"]

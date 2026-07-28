from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import RobotEvent
from app.planning.nodes import _select_base_plan
from app.services.event_impact import analyze_event_impact
from app.services.event_replan import EventReplanService


RUNTIME_REFERENCE = datetime(2026, 1, 1, tzinfo=UTC)

RUNTIME_PLAN = {
    "plan_version": "P-RUNTIME",
    "reference_time": RUNTIME_REFERENCE.isoformat(),
    "time_step_seconds": 5,
    "cuopt_plan": {
        "scheduled_tasks": [
            {
                "task_id": "T-DONE",
                "work_id": "W-DONE",
                "robot_id": "R-01",
                "action": "MOVE",
                "start_time_step": 0,
                "end_time_step": 1,
            },
            {
                "task_id": "T-RUNNING",
                "work_id": "W-RUNNING",
                "robot_id": "R-01",
                "action": "MOVE",
                "start_time_step": 10,
                "end_time_step": 15,
            },
            {
                "task_id": "T-FUTURE",
                "work_id": "W-FUTURE",
                "robot_id": "R-01",
                "action": "PICK",
                "start_time_step": 30,
                "end_time_step": 35,
            },
            {
                "task_id": "T-OTHER",
                "work_id": "W-OTHER",
                "robot_id": "R-02",
                "action": "PICK",
                "start_time_step": 40,
                "end_time_step": 45,
            },
        ]
    },
    "collision_plan": {
        "routes": [
            {
                "robot_id": "R-01",
                "task_ids": ["T-DONE", "T-RUNNING", "T-FUTURE"],
                "waypoints": [
                    {"node_id": 1, "time_step": 10},
                    {"node_id": 2, "time_step": 11},
                    {"node_id": 3, "time_step": 12},
                ],
            },
            {
                "robot_id": "R-02",
                "task_ids": ["T-OTHER"],
                "waypoints": [
                    {"node_id": 4, "time_step": 10},
                    {"node_id": 3, "time_step": 11},
                ],
            },
        ]
    },
}


class Postgres:
    def __init__(self) -> None:
        self.events = {}
        self.requests = {}

    def snapshot(self, _warehouse_id, _item_ids):
        return {
            "inventory": [],
            "robots": [
                {"robot_id": "R-01", "status": "EXECUTING", "node_id": 1, "battery": 40},
                {"robot_id": "R-02", "status": "IDLE", "node_id": 4, "battery": 90},
            ],
            "works": [
                {"work_id": "W-DONE", "task_id": "T-DONE", "status": "COMPLETED"},
                {"work_id": "W-RUNNING", "task_id": "T-RUNNING", "status": "EXECUTING"},
                {"work_id": "W-FUTURE", "task_id": "T-FUTURE", "status": "PLANNED"},
                {"work_id": "W-OTHER", "task_id": "T-OTHER", "status": "PLANNED"},
            ],
        }

    def get_execution_event_processing(self, event_id):
        return deepcopy(self.events.get(event_id))

    def create_execution_event_processing(self, values):
        self.events[values["event_id"]] = deepcopy(values)
        return {**deepcopy(values), "duplicate": False}

    def finalize_execution_event_processing(self, event_id, values):
        self.events[event_id].update(deepcopy(values))

    def count_recent_event_failure_signature(self, *_args, **_kwargs):
        return 0

    def create_or_get_automatic_replan_request(self, values):
        self.requests[values["request_id"]] = deepcopy(values)
        return deepcopy(values)

    def update_automatic_replan_request(self, request_id, values):
        self.requests[request_id].update(deepcopy(values))


class Redis:
    def simulation_snapshot(self, _simulation_id):
        snapshot = Postgres().snapshot(1, [])
        return {
            "simulation_id": "SIM-RUNTIME",
            "inventory": [],
            "robots": snapshot["robots"],
            "works": snapshot["works"],
            "active_plan_version": "P-RUNTIME",
            "active_plan": deepcopy(RUNTIME_PLAN),
            "reference_time": RUNTIME_REFERENCE.isoformat(),
            "checkpoint": "1-0",
        }

    def live_snapshot(self, _warehouse_id):
        return {
            "robots": [],
            "tasks": [],
            "active_plan_version": "P-RUNTIME",
            "active_plan": deepcopy(RUNTIME_PLAN),
        }


class Neo4j:
    def fetch_topology(self, _warehouse_id):
        return {
            "nodes": [{"node_id": value} for value in range(1, 7)],
            "edges": [
                {"from_node": 1, "to_node": 2, "direction": "BOTH"},
                {"from_node": 2, "to_node": 3, "direction": "BOTH"},
                {"from_node": 4, "to_node": 3, "direction": "BOTH"},
                {"from_node": 5, "to_node": 2, "direction": "BOTH"},
            ],
        }


class Services:
    def __init__(self) -> None:
        self.postgres = Postgres()
        self.redis = Redis()
        self.neo4j = Neo4j()


def low_battery_event(battery: float, *, event_id: str = "LOW-1") -> RobotEvent:
    return RobotEvent(
        event_id=event_id,
        warehouse_id=1,
        robot_id="R-01",
        task_id="T-RUNNING",
        event_type="LOW_BATTERY",
        battery=battery,
        occurred_at=RUNTIME_REFERENCE + timedelta(seconds=50),
        execution_context="SIMULATION",
        simulation_id="SIM-RUNTIME",
        payload={
            "active_plan": {"plan_version": "P-CLIENT-INJECTED"},
            "current_time_step": 999,
        },
    )


def test_critical_low_battery_replans_only_unfinished_affected_tasks() -> None:
    impact = analyze_event_impact(low_battery_event(15), Services())

    assert impact.recommended_scope == "LOCAL_REPLAN"
    assert impact.completed_task_ids == ["T-DONE"]
    assert impact.changeable_task_ids == ["T-FUTURE", "T-RUNNING"]
    assert impact.frozen_task_ids == ["T-OTHER"]
    assert impact.robot_state_overrides["R-01"]["battery"] == 15
    assert impact.freeze_horizon_seconds == 15


def test_moderate_low_battery_preserves_current_execution_and_other_robot() -> None:
    impact = analyze_event_impact(low_battery_event(30), Services())

    assert impact.recommended_scope == "LOCAL_REPLAN"
    assert impact.changeable_task_ids == ["T-FUTURE"]
    assert impact.frozen_task_ids == ["T-OTHER", "T-RUNNING"]


def test_position_update_is_telemetry_only_even_when_off_route() -> None:
    services = Services()
    inside = RobotEvent(
        event_id="POS-1",
        warehouse_id=1,
        robot_id="R-01",
        task_id="T-RUNNING",
        event_type="POSITION_UPDATED",
        node_id=2,
        occurred_at=RUNTIME_REFERENCE + timedelta(seconds=50),
        execution_context="SIMULATION",
        simulation_id="SIM-RUNTIME",
        payload={"active_plan": deepcopy(RUNTIME_PLAN), "expected_node_id": 1},
    )
    off_route = inside.model_copy(
        update={"event_id": "POS-2", "node_id": 5}
    )

    assert analyze_event_impact(inside, services).recommended_scope == "NO_REPLAN"
    off_impact = analyze_event_impact(off_route, services)
    assert off_impact.recommended_scope == "NO_REPLAN"
    assert off_impact.robot_state_overrides["R-01"]["node_id"] == 5


def test_path_deviated_remains_the_explicit_route_replan_event() -> None:
    services = Services()
    deviated = RobotEvent(
        event_id="DEV-1",
        warehouse_id=1,
        robot_id="R-01",
        task_id="T-RUNNING",
        event_type="PATH_DEVIATED",
        node_id=5,
        occurred_at=RUNTIME_REFERENCE + timedelta(seconds=50),
        execution_context="SIMULATION",
        simulation_id="SIM-RUNTIME",
    )

    impact = analyze_event_impact(deviated, services)
    assert impact.recommended_scope == "LOCAL_REPLAN"
    assert impact.changeable_task_ids == ["T-FUTURE", "T-RUNNING"]


def test_event_service_passes_partial_contract_and_low_battery_override() -> None:
    services = Services()
    captured = []

    def event_handler(event, *, auto_replan, analyze_impact):
        assert auto_replan is False
        assert analyze_impact is True
        impact = analyze_event_impact(event, services)
        return {
            "redis_updated": True,
            "sql_committed": True,
            "impact_analysis": impact.model_dump(mode="json"),
            "final_status": "REPLAN_REQUIRED",
        }

    def planner(command):
        captured.append(command)
        return {
            "status": "SIMULATION_SUCCESS",
            "command_id": command.command_id,
            "simulation_id": "SIM-NEW",
            "plan_version": "P-NEW",
            "verification_decision": {"decision": "PASS_WITH_WARNING"},
        }

    result = EventReplanService(
        services,
        planner=planner,
        event_handler=event_handler,
    ).handle(low_battery_event(30, event_id="LOW-SERVICE"))

    assert result["status"] == "REPLAN_VERIFIED"
    assert result["partial_replan"]["version"] == "p16.5.12.1"
    assert result["partial_replan"]["changeable_task_ids"] == ["T-FUTURE"]
    scenario = captured[0].scenario_definition
    assert scenario["source_plan_version"] == "P-RUNTIME"
    assert scenario["source_plan_snapshot"]["plan_version"] == "P-RUNTIME"
    assert scenario["protected_task_ids"] == ["T-OTHER", "T-RUNNING"]
    assert scenario["changeable_task_ids"] == ["T-FUTURE"]
    assert scenario["fixed_robot_assignments"] == {
        "T-OTHER": "R-02",
        "T-RUNNING": "R-01",
    }
    assert scenario["hypothetical_events"] == [
        {
            "event_type": "LOW_BATTERY",
            "target_ids": ["R-01"],
            "parameters": {"battery_percent": 30.0},
        }
    ]


def test_source_plan_snapshot_is_selected_as_event_replan_base() -> None:
    state = {
        "command": {
            "requested_execution_mode": "SIMULATE_ONLY",
            "scenario_definition": {
                "source_plan_version": "P-RUNTIME",
                "source_plan_snapshot": deepcopy(RUNTIME_PLAN),
            },
        },
        "snapshot": {"redis": {"active_plan": None}},
    }

    selected, source = _select_base_plan(
        state,
        SimpleNamespace(objective="minimize distance"),
    )

    assert source == "EVENT_SOURCE_PLAN"
    assert selected["plan_version"] == "P-RUNTIME"
    assert selected["base_plan_is_simulated"] is True
    assert selected["candidate_plan"] is True


def test_low_battery_requires_a_battery_value() -> None:
    with pytest.raises(ValueError, match="battery"):
        RobotEvent(
            warehouse_id=1,
            robot_id="R-01",
            event_type="LOW_BATTERY",
            execution_context="SIMULATION",
            simulation_id="SIM-RUNTIME",
        )

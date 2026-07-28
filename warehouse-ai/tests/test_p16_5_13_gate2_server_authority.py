from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models import RobotEvent, SimulationResult
from app.services.event_impact import analyze_event_impact
from app.services.event_replan import EventReplanService
from app.services.runtime_authority import (
    bind_runtime_context,
    resolve_runtime_context,
)
from app.services.simulation_session import replay_simulation_session


REFERENCE = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
SERVER_PLAN = {
    "plan_version": "P-SERVER",
    "reference_time": REFERENCE.isoformat(),
    "time_step_seconds": 5,
    "required_tasks": [
        {
            "task_id": "T-CURRENT",
            "work_id": "W-CURRENT",
            "action": "MOVE",
            "source_candidates": [1],
            "target_candidates": [2],
            "assigned_robot_id": "R1",
        },
        {
            "task_id": "T-FUTURE",
            "work_id": "W-FUTURE",
            "action": "MOVE",
            "source_candidates": [2],
            "target_candidates": [3],
            "assigned_robot_id": "R1",
        },
    ],
    "cuopt_plan": {
        "scheduled_tasks": [
            {
                "task_id": "T-CURRENT",
                "work_id": "W-CURRENT",
                "robot_id": "R1",
                "action": "MOVE",
                "source_node": 1,
                "target_node": 2,
                "start_time_step": 10,
                "end_time_step": 20,
                "estimated_energy": 1.0,
            },
            {
                "task_id": "T-FUTURE",
                "work_id": "W-FUTURE",
                "robot_id": "R1",
                "action": "MOVE",
                "source_node": 2,
                "target_node": 3,
                "start_time_step": 30,
                "end_time_step": 40,
                "estimated_energy": 2.0,
            },
        ],
        "metadata": {},
    },
    "collision_plan": {
        "time_step_seconds": 5,
        "routes": [
            {
                "robot_id": "R1",
                "task_ids": ["T-CURRENT", "T-FUTURE"],
                "waypoints": [
                    {"node_id": 1, "time_step": 10},
                    {"node_id": 2, "time_step": 20},
                    {"node_id": 3, "time_step": 40},
                ],
            }
        ],
    },
}


class Postgres:
    def __init__(self, *, warehouse_id: int = 1) -> None:
        self.warehouse_id = warehouse_id
        self.events: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}

    def get_simulation_session(self, simulation_id):
        return {
            "simulation_id": simulation_id,
            "warehouse_id": self.warehouse_id,
            "status": "ACTIVE",
            "current_state": {"active_plan": deepcopy(SERVER_PLAN)},
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
    def __init__(self) -> None:
        self.real_updates: list[RobotEvent] = []
        self.simulation_updates: list[RobotEvent] = []
        self.robots = [
            {
                "robot_id": "R1",
                "node_id": 1,
                "battery": 80,
                "status": "EXECUTING",
            }
        ]
        self.works = [
            {
                "work_id": "W-CURRENT",
                "task_id": "T-CURRENT",
                "status": "EXECUTING",
            },
            {
                "work_id": "W-FUTURE",
                "task_id": "T-FUTURE",
                "status": "PLANNED",
            },
        ]

    def live_snapshot(self, _warehouse_id):
        return {
            "robots": deepcopy(self.robots),
            "tasks": deepcopy(self.works),
            "active_plan_version": "P-SERVER",
            "active_plan": deepcopy(SERVER_PLAN),
            "temporary_closures": [],
        }

    def simulation_snapshot(self, simulation_id):
        return {
            "simulation_id": simulation_id,
            "inventory": [],
            "robots": deepcopy(self.robots),
            "works": deepcopy(self.works),
            "active_plan_version": "P-SERVER",
            "active_plan": deepcopy(SERVER_PLAN),
            "reference_time": REFERENCE.isoformat(),
            "checkpoint": "1-0",
        }

    def update_from_event(self, event):
        self.real_updates.append(event)

    def update_simulation_from_event(self, event):
        self.simulation_updates.append(event)
        return self.simulation_snapshot(event.simulation_id)


class Neo4j:
    def fetch_topology(self, _warehouse_id):
        return {
            "nodes": [{"node_id": value} for value in (1, 2, 3, 4)],
            "edges": [
                {"from_node": 1, "to_node": 2, "direction": "BOTH"},
                {"from_node": 2, "to_node": 3, "direction": "BOTH"},
                {"from_node": 4, "to_node": 2, "direction": "BOTH"},
            ],
        }


class Services:
    def __init__(self, *, warehouse_id: int = 1) -> None:
        self.postgres = Postgres(warehouse_id=warehouse_id)
        self.redis = Redis()
        self.neo4j = Neo4j()


def telemetry_event(*, battery: float, event_id: str = "POS-1") -> RobotEvent:
    return RobotEvent(
        event_id=event_id,
        warehouse_id=1,
        robot_id="R1",
        task_id="T-CURRENT",
        event_type="POSITION_UPDATED",
        node_id=2,
        battery=battery,
        occurred_at=REFERENCE + timedelta(seconds=50),
        execution_context="SIMULATION",
        simulation_id="SIM-SERVER",
        payload={
            "active_plan": {
                "plan_version": "P-CLIENT",
                "cuopt_plan": {"scheduled_tasks": []},
            },
            "active_plan_version": "P-CLIENT",
            "current_time_step": 999,
            "reference_time": "2099-01-01T00:00:00Z",
        },
    )


class RedisUnavailable(Redis):
    def simulation_snapshot(self, _simulation_id):
        raise RuntimeError("redis simulation state missing")


def test_runtime_context_uses_postgres_plan_when_simulation_redis_is_missing() -> None:
    services = Services()
    services.redis = RedisUnavailable()
    event = telemetry_event(battery=70, event_id="POS-PG-FALLBACK")

    context = resolve_runtime_context(event, services)
    bound = bind_runtime_context(event, context)

    assert context.source == "SIMULATION_POSTGRES_SESSION"
    assert context.active_plan_version == "P-SERVER"
    assert context.current_time_step == 10
    impact = analyze_event_impact(bound, services)
    assert impact.active_plan_version == "P-SERVER"
    assert impact.affected_robot_ids == ["R1"]
    assert bound.payload["_server_runtime"]["robot_state"]["battery"] == 70


def test_runtime_context_uses_server_plan_and_server_clock() -> None:
    event = telemetry_event(battery=70)
    context = resolve_runtime_context(event, Services())
    bound = bind_runtime_context(event, context)

    assert context.active_plan_version == "P-SERVER"
    assert context.active_plan["plan_version"] == "P-SERVER"
    assert context.current_time_step == 10
    assert context.clock_available is True
    assert set(context.ignored_client_fields) == {
        "active_plan",
        "active_plan_version",
        "current_time_step",
        "reference_time",
    }
    assert "active_plan" not in bound.payload
    assert "current_time_step" not in bound.payload
    assert bound.payload["_server_runtime"]["active_plan_version"] == "P-SERVER"
    assert bound.payload["_server_runtime"]["current_time_step"] == 10


def test_position_updated_is_telemetry_only_when_battery_is_safe() -> None:
    services = Services()
    calls = []

    def handler(event, *, auto_replan, analyze_impact):
        calls.append((event.event_type, auto_replan, analyze_impact))
        return {"redis_updated": True, "final_status": "LIVE_UPDATED", "errors": []}

    result = EventReplanService(
        services,
        planner=lambda _command: (_ for _ in ()).throw(
            AssertionError("safe telemetry must not call planner")
        ),
        event_handler=handler,
    ).handle(telemetry_event(battery=70))

    assert result["status"] == "TELEMETRY_UPDATED"
    assert result["final_status"] == "TELEMETRY_UPDATED"
    assert result["auto_replan_requested"] is False
    assert result["server_derived_event"] is False
    assert result["runtime_context"]["current_time_step"] == 10
    assert result["runtime_context"]["active_plan_version"] == "P-SERVER"
    assert calls == [("POSITION_UPDATED", False, False)]


def test_position_updated_low_battery_is_server_derived_and_replanned() -> None:
    services = Services()
    handled = []
    commands = []

    def handler(event, *, auto_replan, analyze_impact):
        handled.append((event.event_type, auto_replan, analyze_impact))
        return {"redis_updated": True, "final_status": "LIVE_UPDATED", "errors": []}

    def planner(command):
        commands.append(command)
        return {
            "status": "SIMULATION_SUCCESS",
            "command_id": command.command_id,
            "simulation_id": "SIM-REPLAN",
            "plan_version": "P-REPLAN",
            "verification_decision": {"decision": "PASS"},
        }

    result = EventReplanService(
        services,
        planner=planner,
        event_handler=handler,
    ).handle(telemetry_event(battery=15, event_id="POS-LOW"))

    assert result["status"] == "REPLAN_VERIFIED"
    assert result["reported_event_type"] == "POSITION_UPDATED"
    assert result["effective_event_type"] == "LOW_BATTERY"
    assert result["server_derived_event"] is True
    assert result["runtime_context"]["active_plan_version"] == "P-SERVER"
    assert result["runtime_context"]["current_time_step"] == 10
    assert result["impact_analysis"]["trigger_type"] == "LOW_BATTERY"
    assert result["partial_replan"]["runtime_source"] == "SIMULATION_REDIS_SESSION"
    assert handled == [("POSITION_UPDATED", False, False)]
    assert len(commands) == 1
    scenario = commands[0].scenario_definition
    assert scenario["source_plan_version"] == "P-SERVER"
    assert scenario["source_plan_snapshot"]["plan_version"] == "P-SERVER"
    assert scenario["source_plan_snapshot"]["plan_version"] != "P-CLIENT"
    assert scenario["fixed_robot_assignments"] == {}


def test_position_updated_21_percent_uses_remaining_plan_energy_threshold() -> None:
    services = Services()
    commands = []

    def handler(event, *, auto_replan, analyze_impact):
        return {"redis_updated": True, "final_status": "LIVE_UPDATED", "errors": []}

    def planner(command):
        commands.append(command)
        return {
            "status": "SIMULATION_SUCCESS",
            "command_id": command.command_id,
            "simulation_id": "SIM-REPLAN-21",
            "plan_version": "P-REPLAN-21",
            "verification_decision": {"decision": "PASS"},
        }

    result = EventReplanService(
        services,
        planner=planner,
        event_handler=handler,
    ).handle(telemetry_event(battery=21, event_id="POS-LOW-21"))

    assert result["status"] == "REPLAN_VERIFIED"
    assert result["effective_event_type"] == "LOW_BATTERY"
    assert result["server_derived_event"] is True
    assert result["impact_analysis"]["robot_state_overrides"]["R1"]["battery"] == 21
    assert result["server_derived_event_evidence"] == {
        "derived_from_event_type": "POSITION_UPDATED",
        "battery_detection_policy": "MINIMUM_PLUS_MARGIN_AND_REMAINING_PLAN_ENERGY",
        "low_battery_threshold": 23.5,
        "minimum_battery": 20.0,
        "battery_safety_margin_percent": 0.5,
        "remaining_planned_energy_percent": 3.0,
        "reported_battery": 21.0,
    }
    assert len(commands) == 1
    scenario = commands[0].scenario_definition
    assert scenario["changeable_task_ids"] == ["T-CURRENT", "T-FUTURE"]
    assert scenario["protected_task_ids"] == []
    assert scenario["fixed_robot_assignments"] == {
        "T-CURRENT": "R1",
        "T-FUTURE": "R1",
    }


def test_simulation_clock_overrides_fully_replayed_completed_statuses() -> None:
    services = Services()
    for row in services.redis.works:
        row["status"] = "COMPLETED"

    def handler(event, *, auto_replan, analyze_impact):
        return {"redis_updated": True, "final_status": "LIVE_UPDATED", "errors": []}

    def planner(command):
        return {
            "status": "SIMULATION_SUCCESS",
            "command_id": command.command_id,
            "simulation_id": "SIM-TIME-RELATIVE",
            "plan_version": "P-TIME-RELATIVE",
            "verification_decision": {"decision": "PASS"},
        }

    result = EventReplanService(
        services,
        planner=planner,
        event_handler=handler,
    ).handle(telemetry_event(battery=21, event_id="POS-TIME-RELATIVE"))

    assert result["status"] == "REPLAN_VERIFIED"
    assert result["partial_replan"]["current_time_step"] == 10
    assert result["impact_analysis"]["completed_task_ids"] == []
    assert result["impact_analysis"]["frozen_task_ids"] == []
    assert result["impact_analysis"]["changeable_task_ids"] == [
        "T-CURRENT",
        "T-FUTURE",
    ]
    assert any(
        "저배터리 안전 예외" in row
        for row in result["impact_analysis"]["evidence"]
    )


def test_simulation_warehouse_mismatch_is_rejected_before_state_mutation() -> None:
    services = Services(warehouse_id=2)
    calls = []
    result = EventReplanService(
        services,
        event_handler=lambda *_args, **_kwargs: calls.append(True),
    ).handle(telemetry_event(battery=70, event_id="POS-MISMATCH"))

    assert result["status"] == "FAILED"
    assert result["final_status"] == "RUNTIME_CONTEXT_FAILED"
    assert result["failure_reason"] == "SIMULATION_WAREHOUSE_MISMATCH"
    assert calls == []


class PlanStoreRedis:
    def __init__(self) -> None:
        self.plan = None
        self.state = {
            "simulation_id": "SIM-PERSIST",
            "inventory": [],
            "robots": [{"robot_id": "R1", "node_id": 1, "battery": 80}],
            "works": [],
            "checkpoint": "0-0",
        }

    def initialize_simulation_session(self, simulation_id, _snapshot):
        self.state["simulation_id"] = simulation_id
        return deepcopy(self.state)

    def save_simulation_plan(self, simulation_id, plan):
        assert simulation_id == "SIM-PERSIST"
        self.plan = deepcopy(plan)
        self.state["active_plan"] = deepcopy(plan)
        self.state["active_plan_version"] = plan["plan_version"]
        self.state["reference_time"] = plan["reference_time"]
        self.state["checkpoint"] = "1-0"
        return {"saved": True, "plan_version": plan["plan_version"]}

    def simulation_snapshot(self, _simulation_id):
        return deepcopy(self.state)


def test_verified_simulation_plan_is_saved_in_server_session() -> None:
    redis = PlanStoreRedis()
    state = {
        "simulation_id": "SIM-PERSIST",
        "command": {
            "command_id": "C-PERSIST",
            "warehouse_id": 1,
            "simulation_id": "SIM-PERSIST",
        },
        "snapshot": {
            "warehouse_id": 1,
            "captured_at": REFERENCE.isoformat(),
            "sql": {"inventory": [], "robots": [], "works": []},
            "redis": {"robots": []},
        },
        "plan_version": "P-PERSIST",
        "scope": {},
        "required_tasks": [],
        "cuopt_plan": {"scheduled_tasks": [], "metadata": {}},
        "collision_plan": {"routes": [], "time_step_seconds": 5},
        "optimization_problem": {"reference_time": REFERENCE.isoformat()},
        "interpretation": {},
        "ready_task_ids": [],
        "waiting_task_ids": [],
        "blocked_task_ids": [],
    }
    result = SimulationResult(
        success=True,
        valid=True,
        status="SUCCESS",
    )

    session = replay_simulation_session(state, result, redis)

    assert redis.plan is not None
    assert redis.plan["plan_version"] == "P-PERSIST"
    assert redis.plan["reference_time"] == REFERENCE.isoformat()
    assert session["base_state"]["active_plan_version"] == "P-PERSIST"
    assert session["current_state"]["active_plan"]["plan_version"] == "P-PERSIST"

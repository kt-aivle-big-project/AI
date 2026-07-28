from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.models import CommandInterpretation, NaturalLanguageCommand, RobotEvent, ScopeDecision
from app.planning import nodes
from app.planning.graph import run_planning
from app.services.event_replan import EventReplanService


SOURCE_PLAN = {
    "plan_version": "P-RUNTIME-SOURCE",
    "current_time_step": 10,
    "required_tasks": [
        {
            "task_id": "runtime:R1:move:current",
            "work_id": "runtime-work-current",
            "action": "MOVE",
            "source_candidates": [1],
            "target_candidates": [2],
            "priority": 5,
            "assigned_robot_id": "R1",
        },
        {
            "task_id": "runtime:R1:move:future",
            "work_id": "runtime-work-future",
            "action": "MOVE",
            "source_candidates": [2],
            "target_candidates": [3],
            "priority": 5,
            "assigned_robot_id": "R1",
        },
        {
            "task_id": "runtime:R2:move:protected",
            "work_id": "runtime-work-protected",
            "action": "MOVE",
            "source_candidates": [4],
            "target_candidates": [1],
            "priority": 5,
            "assigned_robot_id": "R2",
        },
    ],
    "cuopt_plan": {
        "scheduled_tasks": [
            {
                "task_id": "runtime:R1:move:current",
                "work_id": "runtime-work-current",
                "action": "MOVE",
                "robot_id": "R1",
                "source_node": 1,
                "target_node": 2,
                "start_time_step": 10,
                "end_time_step": 20,
                "priority": 5,
            },
            {
                "task_id": "runtime:R2:move:protected",
                "work_id": "runtime-work-protected",
                "action": "MOVE",
                "robot_id": "R2",
                "source_node": 4,
                "target_node": 1,
                "start_time_step": 20,
                "end_time_step": 30,
                "priority": 5,
            },
            {
                "task_id": "runtime:R1:move:future",
                "work_id": "runtime-work-future",
                "action": "MOVE",
                "robot_id": "R1",
                "source_node": 2,
                "target_node": 3,
                "start_time_step": 50,
                "end_time_step": 60,
                "priority": 5,
            },
        ],
        "objective_value": 0,
        "metadata": {},
    },
    "collision_plan": {"routes": []},
}


def _selection_state() -> dict:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="LOCAL_REPLAN",
        objective="runtime partial replan",
        execution_mode="SIMULATE_ONLY",
        target_task_ids=["runtime:R1:move:current", "runtime:R1:move:future"],
        extracted_task_ids=[
            "runtime:R1:move:current",
            "runtime:R1:move:future",
            "runtime:R2:move:protected",
        ],
        extracted_robot_ids=["R1"],
        summary="runtime test",
    )
    scope = ScopeDecision(
        plan_mode="LOCAL_REPLAN",
        affected_task_ids=["runtime:R1:move:current", "runtime:R1:move:future"],
        affected_robot_ids=["R1"],
        fixed_task_ids=["runtime:R1:move:current", "runtime:R2:move:protected"],
        changeable_task_ids=["runtime:R1:move:future"],
        freeze_horizon_seconds=15,
        optimization_goal="runtime partial replan",
        reason_summary="runtime state",
    )
    return {
        "command": {"command_id": "C-RUNTIME", "warehouse_id": 1, "text": "replan"},
        "interpretation": interpretation.model_dump(mode="json"),
        "scope": scope.model_dump(mode="json"),
        "snapshot": {
            "sql": {
                "inventory": [],
                "robots": [
                    {"robot_id": "R1", "node_id": 1, "battery": 25, "status": "IDLE", "max_load": 100, "current_load": 0},
                    {"robot_id": "R2", "node_id": 4, "battery": 90, "status": "IDLE", "max_load": 100, "current_load": 0},
                ],
                "works": [],
                "work_dependencies": [],
                "work_schedule_constraints": [],
            },
            "redis": {"robots": [], "executing_task_ids": [], "active_plan": None},
            "graph": {"nodes": [], "edges": []},
        },
        "replan_base_plan": deepcopy(SOURCE_PLAN),
        "inventory_operations": [],
        "inventory_feasibility": {"item_results": []},
        "inventory_timeline_validation": {},
        "inventory_blocked_work_ids": [],
    }


def test_runtime_source_task_ids_pass_inventory_precheck_without_sql_rows() -> None:
    state = _selection_state()
    state["command"]["scenario_definition"] = {
        "source_plan_snapshot": deepcopy(SOURCE_PLAN)
    }

    update = nodes.inventory_precheck_node(state)

    assert not any(
        str(value).startswith("unknown_target_work_id:")
        for value in update["interpretation"]["missing_information"]
    )
    assert update["inventory_feasibility"]["valid"] is True


def test_runtime_source_required_tasks_are_materialized_without_sql_rows() -> None:
    update = nodes.select_required_tasks_node(_selection_state())

    assert update["schedule_validation"]["valid"] is True
    by_id = {row["task_id"]: row for row in update["required_tasks"]}
    assert set(by_id) == {
        "runtime:R1:move:current",
        "runtime:R1:move:future",
        "runtime:R2:move:protected",
    }
    assert by_id["runtime:R1:move:current"]["frozen"] is True
    assert by_id["runtime:R2:move:protected"]["frozen"] is True
    assert by_id["runtime:R1:move:future"]["frozen"] is False
    assert by_id["runtime:R1:move:future"]["assigned_robot_id"] == "R1"


class _Postgres:
    def __init__(self) -> None:
        self.events = {}
        self.requests = {}

    def get_execution_event_processing(self, event_id):
        return self.events.get(event_id)

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


class _Redis:
    def simulation_snapshot(self, simulation_id):
        return {
            "simulation_id": simulation_id,
            "inventory": [],
            "robots": [{"robot_id": "R1", "node_id": 1, "battery": 25}],
            "works": [],
            "active_plan_version": "P-RUNTIME-SOURCE",
            "active_plan": deepcopy(SOURCE_PLAN),
            "checkpoint": "1-0",
        }


class _Services:
    def __init__(self) -> None:
        self.postgres = _Postgres()
        self.redis = _Redis()


def test_failed_planner_response_exposes_diagnostic_errors() -> None:
    services = _Services()
    event = RobotEvent(
        event_id="LOW-DIAGNOSTIC",
        warehouse_id=1,
        robot_id="R1",
        task_id="runtime:R1:move:current",
        event_type="LOW_BATTERY",
        battery=25,
        execution_context="SIMULATION",
        simulation_id="SIM-1",
        payload={"active_plan": deepcopy(SOURCE_PLAN), "current_time_step": 10},
    )

    impact = {
        "event_id": event.event_id,
        "trigger_type": "LOW_BATTERY",
        "trigger_source": "SIMULATION",
        "affected_robot_ids": ["R1"],
        "affected_task_ids": ["runtime:R1:move:current", "runtime:R1:move:future"],
        "recommended_scope": "LOCAL_REPLAN",
        "risk_level": "MEDIUM",
        "approval_required": False,
        "active_plan_version": "P-RUNTIME-SOURCE",
        "completed_task_ids": [],
        "frozen_task_ids": ["runtime:R1:move:current", "runtime:R2:move:protected"],
        "changeable_task_ids": ["runtime:R1:move:future"],
        "freeze_horizon_seconds": 15,
        "partial_replan_policy": "FREEZE_COMPLETED_EXECUTING_AND_NEAR_TERM",
        "robot_state_overrides": {"R1": {"battery": 25}},
        "failure_signature": "LOW_BATTERY|R1|runtime:R1:move:future|||1|25.0",
    }

    result = EventReplanService(
        services,
        planner=lambda _command: {
            "status": "VALIDATION_FAILED",
            "final_status": "VALIDATION_FAILED",
            "verification_decision": {},
            "errors": [],
        },
        event_handler=lambda *_args, **_kwargs: {
            "redis_updated": True,
            "sql_committed": True,
            "impact_analysis": impact,
            "final_status": "REPLAN_REQUIRED",
        },
    ).handle(event)

    assert result["status"] == "FAILED"
    assert result["failure_reason"] == "AUTO_REPLAN_PLANNING_FAILED:VALIDATION_FAILED"
    assert result["errors"] == ["AUTO_REPLAN_PLANNING_FAILED:VALIDATION_FAILED"]
    assert result["planning_status"] == "VALIDATION_FAILED"


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
def test_actual_planning_graph_accepts_runtime_source_tasks(monkeypatch) -> None:
    from test_pipeline import FakeServices, FakeSupervisor, fake_settings

    services = FakeServices()
    monkeypatch.setattr(nodes, "get_services", lambda: services)
    monkeypatch.setattr(nodes, "get_settings", fake_settings)
    monkeypatch.setattr(nodes, "build_supervisor_llm", FakeSupervisor)

    command = NaturalLanguageCommand(
        warehouse_id=1,
        text=(
            "운영 이벤트 LOW_BATTERY에 대해 로봇 R1을 대상으로 로컬 재계획하고, "
            "변경 가능 작업 runtime:R1:move:future만 재계획하세요"
        ),
        requested_execution_mode="SIMULATE_ONLY",
        scenario_definition={
            "scenario_id": "runtime-source-hotfix",
            "name": "runtime source hotfix",
            "description": "runtime source tasks",
            "source_plan_version": "P-RUNTIME-SOURCE",
            "source_plan_snapshot": deepcopy(SOURCE_PLAN),
            "affected_robot_ids": ["R1"],
            "affected_task_ids": [
                "runtime:R1:move:current",
                "runtime:R1:move:future",
            ],
            "protected_task_ids": [
                "runtime:R1:move:current",
                "runtime:R2:move:protected",
            ],
            "changeable_task_ids": ["runtime:R1:move:future"],
            "freeze_horizon_seconds": 15,
            "fixed_robot_assignments": {
                "runtime:R1:move:current": "R1",
                "runtime:R2:move:protected": "R2",
            },
            "hypothetical_events": [
                {
                    "event_type": "LOW_BATTERY",
                    "target_ids": ["R1"],
                    "parameters": {"battery_percent": 25},
                }
            ],
            "robot_state_overrides": {
                "R1": {"node_id": 1, "battery": 25, "status": "IDLE"}
            },
        },
    )

    result = run_planning(command)

    # The source tasks must pass Snapshot validation and enter optimization.
    # Routing success itself depends on the fake map/timing used by this test.
    assert result["status"] != "COMMAND_ROUTED"
    assert result["base_plan_source"] == "EVENT_SOURCE_PLAN"
    assert result["data"]["task_count"] == 3
    assert result["interpretation"]["invalid_robot_ids"] == []
    assert result["interpretation"]["invalid_task_ids"] == []
    assignment_ids = {row["task_id"] for row in result["data"]["task_assignments"]}
    assert "runtime:R1:move:future" in assignment_ids

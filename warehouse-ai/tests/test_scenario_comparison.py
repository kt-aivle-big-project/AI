from copy import deepcopy

import pytest

from app.models import ScenarioComparisonRequest, ScenarioDefinition
from app.services.scenario_comparison import (
    ScenarioComparisonLimitError,
    ScenarioComparisonService,
    compare_metrics,
    normalize_scenarios,
    parse_scenario_definitions,
    recommend_scenario,
    scenario_result_from_response,
)


class FakePostgres:
    def __init__(self):
        self.comparisons = {}
        self.by_key = {}
        self.runs = {}
        self.commands = {}
        self.stages = []

    def create_or_get_command_history(self, values):
        self.commands.setdefault(values["command_id"], deepcopy(values))
        return deepcopy(self.commands[values["command_id"]])

    def update_command_history(self, values):
        self.commands.setdefault(values["command_id"], {}).update(deepcopy(values))

    def create_or_get_scenario_comparison(self, values):
        existing = self.by_key.get(values["request_key"])
        if existing:
            return deepcopy(self.comparisons[existing])
        self.comparisons[values["comparison_id"]] = deepcopy(values)
        self.by_key[values["request_key"]] = values["comparison_id"]
        return deepcopy(values)

    def finalize_scenario_comparison(self, comparison_id, *, status, recommendation_summary):
        self.comparisons[comparison_id]["status"] = status
        self.comparisons[comparison_id]["recommendation_summary"] = deepcopy(
            recommendation_summary
        )

    def upsert_scenario_comparison_run(self, values):
        self.runs[(values["comparison_id"], values["scenario_id"])] = deepcopy(values)

    def persist_stage_logs(self, command_id, stages):
        self.stages.extend(deepcopy(stages))

    def get_scenario_comparison(self, comparison_id):
        row = deepcopy(self.comparisons.get(comparison_id))
        if not row:
            return None
        row.update(row.get("recommendation_summary") or {})
        row["scenario_runs"] = [
            deepcopy(value)
            for (stored_comparison, _), value in sorted(self.runs.items())
            if stored_comparison == comparison_id
        ]
        return row


class Services:
    def __init__(self):
        self.postgres = FakePostgres()


def planner(command):
    definition = command.scenario_definition
    scenario_id = definition["scenario_id"]
    robot_limit = definition.get("robot_limit") or 3
    failed = "FAIL" in definition["name"]
    return {
        "command_id": command.command_id,
        "simulation_id": f"SIM-{scenario_id}",
        "interpretation": {"execution_mode": "SIMULATE_ONLY"},
        "verification_decision": {"decision": "FAIL" if failed else "PASS"},
        "simulation": {
            "valid": not failed,
            "total_distance": float(40 - robot_limit),
            "makespan": 10,
            "tardiness": 0,
            "conflict_count": 0,
            "errors": ["failed scenario"] if failed else [],
            "metrics": {
                "time_step_seconds": 5,
                "makespan_seconds": 50,
                "tardiness_seconds": 0,
            },
        },
        "optimization_plan": {
            "scheduled_tasks": [
                {"task_id": f"W-{index}", "robot_id": f"R-{index % robot_limit}"}
                for index in range(3)
            ],
            "unassigned_task_ids": [],
            "metadata": {"energy": 2.5, "plan_changes": 0},
        },
        "collision_plan": {
            "time_step_seconds": 5,
            "metadata": {"wait_evidence": []},
        },
        "replan_attempt": 0,
        "warnings": [],
        "errors": [],
    }


def test_robot_count_and_priority_scenarios_are_parsed() -> None:
    robots, _ = parse_scenario_definitions("로봇 2대와 3대를 사용할 때 비교해줘")
    assert [row.robot_limit for row in robots] == [2, 3]
    priorities, _ = parse_scenario_definitions("거리 우선과 납기 우선 계획을 비교해줘")
    assert [row.optimization_priority for row in priorities] == [
        "MINIMIZE_DISTANCE",
        "MINIMIZE_TARDINESS",
    ]


def test_robot_exclusion_and_edge_closure_scenarios_are_parsed() -> None:
    robots, _ = parse_scenario_definitions("R-02를 포함한 경우와 제외한 경우를 비교해줘")
    assert robots[0].excluded_robot_ids == []
    assert robots[1].excluded_robot_ids == ["R-02"]
    edges, _ = parse_scenario_definitions("통로 6을 폐쇄한 경우와 정상 상태를 비교해줘")
    assert edges[0].excluded_edge_ids == []
    assert edges[1].excluded_edge_ids == ["6"]


def test_comparison_runs_isolated_simulations_and_keeps_conversation_link() -> None:
    services = Services()
    received = []

    def recording_planner(command):
        received.append(command)
        return planner(command)

    result = ScenarioComparisonService(services, planner=recording_planner).execute(
        ScenarioComparisonRequest(
            warehouse_id=1,
            conversation_id="CONV-1",
            text="로봇 2대와 3대를 사용할 때 비교해줘",
            optimization_priority="MINIMIZE_DISTANCE",
        )
    )
    assert result["status"] == "COMPLETED"
    assert result["conversation_id"] == "CONV-1"
    assert len({row["simulation_id"] for row in result["scenarios"]}) == 2
    assert all(command.requested_execution_mode == "SIMULATE_ONLY" for command in received)
    assert all(command.scenario_definition for command in received)


def test_partial_success_and_failed_scenario_is_not_recommended() -> None:
    services = Services()
    result = ScenarioComparisonService(services, planner=planner).execute(
        ScenarioComparisonRequest(
            warehouse_id=1,
            scenarios=[
                ScenarioDefinition(name="정상", robot_limit=2),
                ScenarioDefinition(name="FAIL", robot_limit=3),
            ],
            optimization_priority="MINIMIZE_DISTANCE",
        )
    )
    assert result["status"] == "PARTIAL_SUCCESS"
    assert result["recommended_scenario_id"] == "scenario-1"


def test_explicit_goal_controls_recommendation_and_ambiguous_goal_does_not() -> None:
    a = scenario_result_from_response(ScenarioDefinition(scenario_id="a", name="A"), planner(type("C", (), {"scenario_definition": {"scenario_id": "a", "name": "A", "robot_limit": 2}, "command_id": "A"})()))
    b = scenario_result_from_response(ScenarioDefinition(scenario_id="b", name="B"), planner(type("C", (), {"scenario_definition": {"scenario_id": "b", "name": "B", "robot_limit": 3}, "command_id": "B"})()))
    recommended, _, _ = recommend_scenario([a, b], "MINIMIZE_DISTANCE")
    assert recommended == "b"
    assert recommend_scenario([a, b], None)[0] is None


def test_percentage_is_none_when_baseline_is_zero() -> None:
    response = planner(type("C", (), {"scenario_definition": {"scenario_id": "a", "name": "A", "robot_limit": 2}, "command_id": "A"})())
    response["simulation"]["total_distance"] = 0
    a = scenario_result_from_response(ScenarioDefinition(scenario_id="a", name="A"), response)
    response_b = deepcopy(response)
    response_b["command_id"] = "B"
    response_b["simulation_id"] = "SIM-b"
    response_b["simulation"]["total_distance"] = 5
    b = scenario_result_from_response(ScenarioDefinition(scenario_id="b", name="B"), response_b)
    row = next(row for row in compare_metrics([a, b]) if row["metric"] == "total_distance")
    assert row["percentage_difference"] is None


def test_duplicate_scenarios_are_normalized_and_limit_is_enforced() -> None:
    duplicate = ScenarioDefinition(name="same", robot_limit=2)
    assert len(normalize_scenarios([duplicate, duplicate], limit=4)) == 1
    with pytest.raises(ScenarioComparisonLimitError):
        normalize_scenarios(
            [ScenarioDefinition(name=str(index), robot_limit=index + 1) for index in range(5)],
            limit=4,
        )


def test_duplicate_comparison_request_is_idempotent() -> None:
    services = Services()
    calls = []

    def counted(command):
        calls.append(command.command_id)
        return planner(command)

    service = ScenarioComparisonService(services, planner=counted)
    request = ScenarioComparisonRequest(
        warehouse_id=1,
        text="로봇 2대와 3대를 사용할 때 비교해줘",
        idempotency_key="same-request",
    )
    first = service.execute(request)
    second = service.execute(request)
    assert first["comparison_id"] == second["comparison_id"]
    assert second["duplicate"] is True
    assert len(calls) == 2


def test_missing_or_single_comparison_condition_requires_clarification() -> None:
    services = Services()
    result = ScenarioComparisonService(services, planner=planner).execute(
        ScenarioComparisonRequest(warehouse_id=1, text="비교해줘")
    )
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["scenarios"] == []

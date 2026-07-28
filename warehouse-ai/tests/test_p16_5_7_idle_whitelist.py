from __future__ import annotations

import pytest

from app.services.command_language import parse_deterministic_command
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan
from tests.test_p16_5_6_idle_holding_routing import (
    _daily_multi_robot_plan,
    _reconcile,
    _warehouse_two_problem,
)


ALLOWED_TYPES = {
    "PARKING",
    "STAGING",
    "HOLDING",
    "CHARGER_WAITING_AREA",
    "ROBOT_PARKING",
}


def _strict_problem() -> dict:
    problem = _warehouse_two_problem()
    problem["hard_constraints"] = [
        "NO_IDLE_ON_TRANSIT_NODE",
        "NO_IDLE_ON_INTERSECTION",
        "NO_IDLE_ON_SERVICE_NODE",
        "NO_IDLE_ON_ARTICULATION_NODE",
        "NO_IDLE_ON_CONGESTION_NODE",
        "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE",
        "IDLE_ONLY_ON_WHITELISTED_NODE",
    ]
    problem["idle_whitelist_strict"] = True
    problem["idle_relocation_min_gap_steps"] = 12
    return problem


def test_daily_plan_uses_only_designated_idle_nodes() -> None:
    problem = _strict_problem()
    plan = _daily_multi_robot_plan()
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(plan)
    operational = _reconcile(plan, collision)
    simulation = simulate_plan(collision, operational, problem)

    node_types = {
        int(row["node_id"]): str(row.get("node_type") or "").upper()
        for row in problem["nodes"]
    }
    idle_nodes = {
        int(row["holding_node_id"])
        for row in collision.metadata["idle_relocations"]
    }

    assert simulation.success is True
    assert simulation.valid is True
    assert simulation.conflict_count == 0
    assert idle_nodes
    assert all(node_types[node_id] in ALLOWED_TYPES for node_id in idle_nodes)
    assert collision.metadata["idle_policy"]["strict"] is True
    assert collision.metadata["idle_policy"]["violation_count"] == 0
    assert collision.metadata["idle_action_task_count"] == (
        collision.metadata["idle_relocation_count"] * 2
    )
    assert {
        row["action"] for row in collision.metadata["idle_action_tasks"]
    } == {"MOVE_TO_IDLE_NODE", "WAIT_AT_IDLE_NODE"}


def test_initial_future_tasks_leave_service_and_charger_nodes() -> None:
    collision = PrioritizedTimeExpandedPlanner(
        _strict_problem(), 5, 720
    ).solve(_daily_multi_robot_plan())

    initial_rows = [
        row
        for row in collision.metadata["idle_relocations"]
        if row["reason"] == "INITIAL_IDLE_RELOCATION_TO_WHITELIST"
    ]
    assert {row["robot_id"] for row in initial_rows} == {
        "R2-01",
        "R2-02",
        "R2-03",
    }
    assert {row["from_node"] for row in initial_rows} == {2146, 2152}
    assert all(row["idle_whitelist_valid"] is True for row in initial_rows)


def test_strict_policy_rejects_map_without_idle_nodes() -> None:
    problem = _strict_problem()
    problem["nodes"] = [
        row for row in problem["nodes"] if int(row["node_id"]) < 2160
    ]
    problem["edges"] = [
        row
        for row in problem["edges"]
        if int(row["from_node"]) < 2160 and int(row["to_node"]) < 2160
    ]

    with pytest.raises(RuntimeError, match="IDLE_NODE_NOT_CONFIGURED"):
        PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(
            _daily_multi_robot_plan()
        )


def test_daily_language_adds_no_blocking_idle_hard_constraints() -> None:
    interpretation = parse_deterministic_command(
        "2026년7월25일 오전7시15분 기준으로 오전9시부터10시까지 "
        "A상품 10 BOX를 출고하고, 일이 없으면 통로에서 장시간 대기하지 말고 "
        "안전한 holding 노드에서 기다려줘.",
        warehouse_timezone="Asia/Seoul",
    )

    expected = {
        "NO_IDLE_ON_TRANSIT_NODE",
        "NO_IDLE_ON_INTERSECTION",
        "NO_IDLE_ON_SERVICE_NODE",
        "NO_IDLE_ON_ARTICULATION_NODE",
        "NO_IDLE_ON_CONGESTION_NODE",
        "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE",
        "IDLE_ONLY_ON_WHITELISTED_NODE",
    }
    assert expected.issubset(set(interpretation.hard_constraints))

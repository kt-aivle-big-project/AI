from __future__ import annotations

from app.services.command_language import parse_deterministic_command
from app.services.opportunity_charging import (
    augment_plan_with_opportunity_charging,
)
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan
from tests.test_p16_5_6_idle_holding_routing import (
    _daily_multi_robot_plan,
    _reconcile,
    _warehouse_two_problem,
)


def _p16_5_8_problem() -> dict:
    problem = _warehouse_two_problem()
    problem.update(
        {
            "hard_constraints": [
                "NO_IDLE_ON_TRANSIT_NODE",
                "NO_IDLE_ON_INTERSECTION",
                "NO_IDLE_ON_SERVICE_NODE",
                "NO_IDLE_ON_ARTICULATION_NODE",
                "NO_IDLE_ON_CONGESTION_NODE",
                "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE",
                "IDLE_ONLY_ON_WHITELISTED_NODE",
                "LONG_IDLE_RETURN_TO_CHARGER_AREA",
                "OPPORTUNITY_CHARGING",
                "CHARGER_SLOT_ONLY_WHILE_CHARGING",
            ],
            "idle_whitelist_strict": True,
            "idle_return_policy": "CHARGER_AREA_FIRST",
            "opportunity_charging_enabled": True,
            "opportunity_charge_min_gap_steps": 180,
            "opportunity_charge_target_battery": 95.0,
            "opportunity_charge_min_gain_percent": 2.0,
            "charge_target_battery": 80.0,
            "charge_rate_percent_per_minute": 5.0,
            "battery_safety_margin_percent": 0.5,
        }
    )
    return problem


def test_long_idle_inserts_opportunity_charge_without_delaying_business_tasks() -> None:
    problem = _p16_5_8_problem()
    original = _daily_multi_robot_plan()
    augmented, evidence = augment_plan_with_opportunity_charging(problem, original)

    charge_tasks = [task for task in augmented.scheduled_tasks if task.action == "CHARGE"]
    original_starts = {task.task_id: task.start_time_step for task in original.scheduled_tasks}
    augmented_starts = {
        task.task_id: task.start_time_step
        for task in augmented.scheduled_tasks
        if task.task_id in original_starts
    }

    assert evidence["enabled"] is True
    assert evidence["inserted_charge_task_count"] == 3
    assert len(charge_tasks) == 3
    assert augmented_starts == original_starts
    assert all(task.charge_target_battery == 95.0 for task in charge_tasks)
    assert all(task.charged_percent >= 2.0 for task in charge_tasks)
    assert all(task.end_time_step < 2340 for task in charge_tasks)
    assert len({task.target_node for task in charge_tasks}) == 3


def test_charge_slot_is_released_to_linked_waiting_area() -> None:
    problem = _p16_5_8_problem()
    augmented, _ = augment_plan_with_opportunity_charging(
        problem, _daily_multi_robot_plan()
    )
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(augmented)

    post_charge_rows = [
        row
        for row in collision.metadata["idle_relocations"]
        if row.get("idle_behavior") == "LEAVE_CHARGER_SLOT_TO_WAITING_AREA"
    ]
    assert post_charge_rows
    assert all(row["holding_node_type"] == "CHARGER_WAITING_AREA" for row in post_charge_rows)
    assert all(row["linked_charger_node_id"] == row["from_node"] for row in post_charge_rows)
    assert all(row["idle_whitelist_valid"] is True for row in post_charge_rows)

    behavior_actions = {
        row.get("behavior_action")
        for row in collision.metadata["idle_action_tasks"]
    }
    assert "MOVE_TO_CHARGER_WAITING_AREA" in behavior_actions
    assert "WAIT_AT_CHARGER_WAITING_AREA" in behavior_actions


def test_augmented_plan_routes_and_simulates_without_collision() -> None:
    problem = _p16_5_8_problem()
    augmented, _ = augment_plan_with_opportunity_charging(
        problem, _daily_multi_robot_plan()
    )
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(augmented)
    operational = _reconcile(augmented, collision)
    simulation = simulate_plan(collision, operational, problem)

    assert simulation.success is True
    assert simulation.valid is True
    assert simulation.conflict_count == 0
    assert collision.metadata["idle_policy"]["violation_count"] == 0
    assert collision.metadata["idle_energy_policy"] == {
        "idle_return_policy": "CHARGER_AREA_FIRST",
        "opportunity_charging_enabled": True,
        "charger_slot_idle_allowed": False,
        "post_charge_behavior": "LEAVE_SLOT_TO_LINKED_WAITING_AREA",
    }


def test_high_battery_skips_charge_but_returns_to_charger_area() -> None:
    problem = _p16_5_8_problem()
    for robot in problem["robots"]:
        robot["battery"] = 100.0
    augmented, evidence = augment_plan_with_opportunity_charging(
        problem, _daily_multi_robot_plan()
    )
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(augmented)

    assert evidence["inserted_charge_task_count"] == 0
    assert any(
        row["selected_action"] == "RETURN_TO_CHARGER_AREA_AND_WAIT"
        for row in evidence["decisions"]
    )
    assert collision.metadata["idle_relocations"]
    assert all(
        row["holding_node_type"] == "CHARGER_WAITING_AREA"
        for row in collision.metadata["idle_relocations"]
    )


def test_daily_language_records_charger_area_and_opportunity_charge_policy() -> None:
    interpretation = parse_deterministic_command(
        "오늘 전체 작업을 계획하고 일이 없으면 충전소로 복귀해서 "
        "필요한 만큼 충전한 뒤 충전 대기 구역에서 기다려줘.",
        warehouse_timezone="Asia/Seoul",
    )
    expected = {
        "LONG_IDLE_RETURN_TO_CHARGER_AREA",
        "OPPORTUNITY_CHARGING",
        "CHARGER_SLOT_ONLY_WHILE_CHARGING",
        "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE",
        "IDLE_ONLY_ON_WHITELISTED_NODE",
    }
    assert expected.issubset(set(interpretation.hard_constraints))

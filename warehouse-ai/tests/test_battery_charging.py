from datetime import UTC, datetime
from copy import deepcopy

from app.models import AtomicTask, CuOptPlan, ScheduledTask
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def charging_problem(*, battery: float = 22.0, extra_robot: bool = False, closed: bool = False):
    nodes = [
        {"node_id": 1, "node_type": "AISLE", "active": True},
        {"node_id": 2, "node_type": "CHARGER", "active": not closed},
        {"node_id": 3, "node_type": "AISLE", "active": True},
        {"node_id": 4, "node_type": "AISLE", "active": True},
    ]
    robots = [{"robot_id": "R1", "node_id": 1, "battery": battery, "status": "IDLE"}]
    if extra_robot:
        robots.append({"robot_id": "R2", "node_id": 1, "battery": 100, "status": "IDLE"})
    return {
        "reference_time": datetime(2026, 7, 23, tzinfo=UTC).isoformat(),
        "time_step_seconds": 1,
        "min_robot_battery": 20,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.1,
        "charge_target_battery": 80,
        "charge_rate_percent_per_minute": 60,
        "robots": robots,
        "nodes": nodes,
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 10, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2, "to_node": 3, "distance": 10, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 3, "to_node": 4, "distance": 10, "travel_seconds": 1, "direction": "BOTH"},
        ],
        "tasks": [
            AtomicTask(task_id="W1:move", work_id="W1", action="MOVE", source_candidates=[3], target_candidates=[4]).model_dump()
        ],
    }


def optimizer() -> LocalOptimizer:
    return LocalOptimizer(time_step_seconds=1, min_robot_battery=20, energy_per_distance=0.1)


def test_sufficient_battery_does_not_insert_charge_task():
    plan = optimizer().optimize(charging_problem(battery=100))
    assert [task for task in plan.scheduled_tasks if task.action == "CHARGE"] == []
    assert plan.unassigned_task_ids == []


def test_low_battery_inserts_charge_before_original_task():
    plan = optimizer().optimize(charging_problem())
    charge, move = plan.scheduled_tasks
    assert charge.action == "CHARGE"
    assert charge.task_id == "W1:move:charge:2"
    assert charge.robot_id == move.robot_id == "R1"
    assert move.start_time_step == charge.end_time_step


def test_unreachable_charger_excludes_robot_with_battery_reason():
    plan = optimizer().optimize(charging_problem(battery=0))
    assert plan.unassigned_task_ids == ["W1:move"]


def test_closed_charger_is_not_used():
    plan = optimizer().optimize(charging_problem(closed=True))
    assert plan.unassigned_task_ids == ["W1:move"]


def test_nearest_reachable_charger_is_selected():
    problem = charging_problem()
    problem["nodes"].append({"node_id": 5, "node_type": "CHARGER", "active": True})
    problem["edges"].append(
        {"from_node": 1, "to_node": 5, "distance": 30, "travel_seconds": 3, "direction": "BOTH"}
    )
    plan = optimizer().optimize(problem)
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    assert charge.target_node == 2


def test_higher_battery_robot_avoids_unnecessary_charge():
    plan = optimizer().optimize(charging_problem(extra_robot=True))
    assert all(task.robot_id == "R2" for task in plan.scheduled_tasks)
    assert all(task.action != "CHARGE" for task in plan.scheduled_tasks)


def test_charger_dwell_is_reserved_and_simulation_reports_battery():
    problem = charging_problem()
    plan = optimizer().optimize(problem)
    routed = PrioritizedTimeExpandedPlanner(problem, 1, 2000).solve(plan)
    route = routed.routes[0]
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    charge_waypoints = [
        point for point in route.waypoints
        if point.node_id == charge.target_node and point.action == "CHARGE"
    ]
    assert charge_waypoints
    assert route.waypoints[-1].time_step >= charge.end_time_step
    simulation = simulate_plan(routed, plan, problem)
    battery = simulation.metrics["battery_by_robot"]["R1"]
    assert simulation.success is True
    assert battery["charge_task_ids"] == [charge.task_id]
    assert battery["charger_node_ids"] == [2]
    assert battery["final_battery"] >= 20


def test_two_charge_tasks_cannot_share_charger_at_same_time():
    problem = charging_problem(battery=100, extra_robot=True)
    problem["robots"][1]["node_id"] = 4
    plan = CuOptPlan(
        objective_value=0,
        scheduled_tasks=[
            ScheduledTask(task_id="A:charge", work_id="A", action="CHARGE", robot_id="R1", source_node=1, target_node=2, start_time_step=0, end_time_step=3, charge_target_battery=80),
            ScheduledTask(task_id="B:charge", work_id="B", action="CHARGE", robot_id="R2", source_node=1, target_node=2, start_time_step=0, end_time_step=3, charge_target_battery=80),
        ],
    )
    routed = PrioritizedTimeExpandedPlanner(problem, 1, 100).solve(plan)
    charger_steps = [
        (route.robot_id, point.time_step)
        for route in routed.routes
        for point in route.waypoints
        if point.node_id == 2 and point.action == "CHARGE"
    ]
    assert len({step for _, step in charger_steps}) == len(charger_steps)


def test_simulation_does_not_mutate_robot_snapshot_battery():
    problem = charging_problem()
    before = deepcopy(problem["robots"])
    plan = optimizer().optimize(problem)
    routed = PrioritizedTimeExpandedPlanner(problem, 1, 2000).solve(plan)
    simulate_plan(routed, plan, problem)
    assert problem["robots"] == before


def test_required_charge_uses_operation_ready_target():
    problem = charging_problem(battery=22)
    problem["hard_constraints"] = ["MINIMUM_REQUIRED_CHARGE"]
    plan = optimizer().optimize(problem)
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    assert charge.charge_target_battery == 80
    assert charge.charged_percent > 0
    assert charge.charge_duration_seconds is not None
    routed = PrioritizedTimeExpandedPlanner(problem, 1, 2000).solve(plan)
    simulation = simulate_plan(routed, plan, problem)
    battery = simulation.metrics["battery_by_robot"]["R1"]
    assert battery["initial_battery"] == 22
    assert battery["final_battery"] >= 20
    assert battery["projected_without_charge"] < 20


def test_lowest_configured_charger_cost_wins_over_distance():
    problem = charging_problem()
    problem["hard_constraints"] = ["MINIMUM_REQUIRED_CHARGE"]
    problem["nodes"][1]["charging_cost"] = 5
    problem["nodes"].append(
        {
            "node_id": 5,
            "node_type": "CHARGER",
            "active": True,
            "charging_cost": 1,
        }
    )
    problem["edges"].append(
        {
            "from_node": 1,
            "to_node": 5,
            "distance": 12,
            "travel_seconds": 2,
            "direction": "BOTH",
        }
    )
    plan = optimizer().optimize(problem)
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    assert charge.target_node == 5
    assert charge.charger_cost == 1
    assert charge.charger_selection_policy == "MIN_SAFE_CONFIGURED_CHARGER_COST"
    assert any(row["charger_node"] == 2 for row in charge.charger_candidates)
    assert any(row["charger_node"] == 5 and row["selected"] for row in charge.charger_candidates)


def test_missing_charger_cost_is_explicit_distance_fallback():
    problem = charging_problem()
    problem["hard_constraints"] = ["MINIMUM_REQUIRED_CHARGE"]
    plan = optimizer().optimize(problem)
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    assert charge.target_node == 2
    assert charge.charger_cost is None
    assert charge.charger_selection_policy == "SAFE_DISTANCE_FALLBACK_NO_COST_DATA"
    assert "비용 속성이 없어" in (charge.charger_selection_reason or "")


def test_unsafe_cheapest_charger_is_rejected_before_cost_comparison():
    problem = charging_problem(battery=22)
    problem["nodes"][1]["charging_cost"] = 5
    problem["nodes"].append(
        {
            "node_id": 5,
            "node_type": "CHARGER",
            "active": True,
            "charging_cost": 1,
        }
    )
    problem["edges"].append(
        {
            "from_node": 1,
            "to_node": 5,
            "distance": 30,
            "travel_seconds": 3,
            "direction": "BOTH",
        }
    )
    plan = optimizer().optimize(problem)
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    assert charge.target_node == 2
    unsafe = next(row for row in charge.charger_candidates if row["charger_node"] == 5)
    assert unsafe["safe_reachable"] is False
    assert unsafe["rejection_reason"] == "BATTERY_BELOW_SAFE_ARRIVAL_THRESHOLD"
    selected = next(row for row in charge.charger_candidates if row.get("selected"))
    assert selected["charger_node"] == 2
    assert selected["battery_at_charger"] >= selected["minimum_arrival_battery"]


def test_no_safe_charger_leaves_task_unassigned_for_local_replan():
    problem = charging_problem(battery=21)
    plan = optimizer().optimize(problem)
    assert plan.scheduled_tasks == []
    assert plan.unassigned_task_ids == ["W1:move"]

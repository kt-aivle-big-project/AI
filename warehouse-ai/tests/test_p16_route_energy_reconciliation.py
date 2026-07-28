import math

from app.models import CollisionFreePlan, CuOptPlan, ScheduledTask, TimedRoute, TimedWaypoint
from app.services.energy_reconciliation import (
    calculate_route_battery_metrics,
    reconcile_plan_energy,
)
from app.services.simulation import simulate_plan


def problem() -> dict:
    return {
        "nodes": [
            {"node_id": 1, "node_type": "INTERSECTION", "active": True},
            {"node_id": 2, "node_type": "CHARGER", "active": True},
            {"node_id": 3, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 2.44,
                "direction": "BOTH",
                "active": True,
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 32.0,
                "direction": "BOTH",
                "active": True,
            },
        ],
        "robots": [{"robot_id": "R1", "node_id": 1, "battery": 21.0}],
        "min_robot_battery": 20.0,
        "energy_per_distance": 0.05,
        "charge_rate_percent_per_minute": 5.0,
        "time_step_seconds": 5,
        "inventory": [],
        "tasks": [],
    }


def route() -> CollisionFreePlan:
    return CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        time_step_seconds=5,
        total_distance=34.44,
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=["C1", "D1"],
                distance=34.44,
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0, action="MOVE"),
                    TimedWaypoint(node_id=2, time_step=1, action="MOVE"),
                    TimedWaypoint(node_id=2, time_step=2, action="CHARGE"),
                    TimedWaypoint(node_id=2, time_step=3, action="CHARGE"),
                    TimedWaypoint(node_id=3, time_step=4, action="MOVE"),
                ],
            )
        ],
        metadata={"task_completion_steps": {"C1": 3, "D1": 4}},
    )


def optimizer_plan(include_charge: bool = True) -> CuOptPlan:
    tasks = []
    if include_charge:
        tasks.append(
            ScheduledTask(
                task_id="C1",
                action="CHARGE",
                robot_id="R1",
                source_node=1,
                target_node=2,
                start_time_step=0,
                end_time_step=3,
                estimated_distance=2.44,
                estimated_energy=0.122,
                charged_percent=0.542,
                charge_target_battery=21.42,
                charge_duration_seconds=10,
                charger_cost=1.0,
                charger_selection_policy="MIN_CONFIGURED_CHARGER_COST",
                charger_candidates=[
                    {
                        "charger_node": 2,
                        "selected": True,
                        "charged_percent": 0.542,
                        "charge_duration_seconds": 10,
                    }
                ],
            )
        )
    tasks.append(
        ScheduledTask(
            task_id="D1",
            action="DROP",
            robot_id="R1",
            source_node=2,
            target_node=3,
            start_time_step=3,
            end_time_step=4,
            estimated_distance=28.4,
            estimated_energy=1.42,
        )
    )
    return CuOptPlan(
        scheduled_tasks=tasks,
        objective_value=0,
        metadata={
            "charger_selections": [
                {
                    "task_id": "C1",
                    "robot_id": "R1",
                    "selected_charger_node": 2,
                    "charged_percent": 0.542,
                    "charge_duration_seconds": 10,
                    "candidates": [
                        {
                            "charger_node": 2,
                            "selected": True,
                            "charged_percent": 0.542,
                            "charge_duration_seconds": 10,
                        }
                    ],
                }
            ]
            if include_charge
            else []
        },
    )


def test_charge_is_increased_from_final_routing_distance():
    adjusted, evidence = reconcile_plan_energy(optimizer_plan(), route(), problem())
    charge = next(row for row in adjusted.scheduled_tasks if row.action == "CHARGE")

    assert evidence["energy_source"] == "ROUTING_FINAL_DISTANCE"
    assert evidence["unsafe_robot_ids"] == []
    assert evidence["requires_reroute"] is False
    assert math.isclose(charge.charged_percent, 0.722, abs_tol=1e-9)
    assert math.isclose(charge.charge_target_battery, 21.6, abs_tol=1e-9)
    assert charge.charge_duration_seconds == 10
    robot = evidence["robots"]["R1"]
    assert math.isclose(robot["route_consumption"], 1.722, abs_tol=1e-9)
    assert math.isclose(robot["projected_final_battery"], 20.0, abs_tol=1e-9)
    selection = adjusted.metadata["charger_selections"][0]
    assert selection["charged_percent"] == 0.722
    assert selection["projected_final_battery"] == 20.0


def test_simulation_consumes_final_route_distance_not_optimizer_estimate():
    adjusted, _ = reconcile_plan_energy(optimizer_plan(), route(), problem())
    result = simulate_plan(route(), adjusted, problem(), include_timeline=False)
    metric = result.metrics["battery_by_robot"]["R1"]

    assert result.valid is True
    assert metric["energy_source"] == "ROUTING_FINAL_DISTANCE"
    assert metric["route_distance"] == 34.44
    assert metric["estimated_consumption"] == 1.722
    assert metric["charged_percent"] == 0.722
    assert metric["final_battery"] == 20.0


def test_missing_charge_task_is_marked_unsafe_after_route_expansion():
    adjusted, evidence = reconcile_plan_energy(
        optimizer_plan(include_charge=False),
        route(),
        problem(),
    )

    assert adjusted.scheduled_tasks[0].action == "DROP"
    assert evidence["unsafe_robot_ids"] == ["R1"]
    assert evidence["robots"]["R1"]["status"] == "CHARGE_TASK_REQUIRED"

from app.models import AtomicTask, CuOptPlan, ScheduledTask
from app.services.local_optimizer import LocalOptimizer
from app.services.robot_adapter import RobotAdapter
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def _optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
    )


def _outbound_problem() -> dict:
    return {
        "reference_time": "2026-07-24T00:00:00+00:00",
        "time_step_seconds": 5,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80,
        "charge_rate_percent_per_minute": 5,
        "hard_constraints": ["MINIMUM_REQUIRED_CHARGE"],
        "robot_state_overrides": [
            {
                "robot_id": "R2-03",
                "battery_percent": 21,
                "source": "COMMAND_HYPOTHETICAL_OVERRIDE",
            }
        ],
        "robots": [
            {
                "robot_id": "R2-03",
                "node_id": 1,
                "battery": 21,
                "status": "IDLE",
                "max_load": 100,
            }
        ],
        "nodes": [
            {"node_id": 1, "node_type": "CHARGER", "active": True},
            {"node_id": 2, "node_type": "STORAGE", "active": True},
            {"node_id": 3, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 10,
                "travel_seconds": 30,
                "direction": "BOTH",
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 20,
                "travel_seconds": 60,
                "direction": "BOTH",
            },
        ],
        "tasks": [
            AtomicTask(
                task_id="W:pick",
                work_id="W",
                action="PICK",
                item_id="E",
                quantity=30,
                source_candidates=[2],
                target_candidates=[2],
                same_robot_group="W",
            ).model_dump(),
            AtomicTask(
                task_id="W:drop",
                work_id="W",
                action="DROP",
                item_id="E",
                quantity=30,
                source_candidates=[2],
                target_candidates=[3],
                predecessors=["W:pick"],
                same_robot_group="W",
            ).model_dump(),
        ],
    }


def test_p12_lookahead_charges_before_pick_and_emits_real_charge_command() -> None:
    problem = _outbound_problem()
    plan = _optimizer().optimize(problem)

    assert plan.unassigned_task_ids == []
    assert [task.action for task in plan.scheduled_tasks] == [
        "CHARGE",
        "PICK",
        "DROP",
    ]
    charge = plan.scheduled_tasks[0]
    assert charge.target_node == 1
    assert charge.charged_percent > 0
    assert plan.metadata["charger_selections"][0]["battery_at_charger"] >= 20

    routed = PrioritizedTimeExpandedPlanner(problem, 5, 2000).solve(plan)
    route = routed.routes[0]
    charge_waypoints = [
        point for point in route.waypoints if point.action == "CHARGE"
    ]
    assert len(charge_waypoints) * 5 == charge.charge_duration_seconds

    adapter_plan = {
        "warehouse_id": 2,
        "charger_node_ids": [1],
        "required_tasks": problem["tasks"],
        "inventory_operations": [],
        "cuopt_plan": plan.model_dump(mode="json"),
        "collision_plan": routed.model_dump(mode="json"),
    }
    batches, validation = RobotAdapter(time_step_seconds=5).adapt(
        "P12-PLAN", adapter_plan
    )
    assert validation["valid"] is True
    charge_commands = [
        command
        for batch in batches
        for command in batch.commands
        if command.action == "CHARGE"
    ]
    assert len(charge_commands) == 1
    assert charge_commands[0].payload["duration_seconds"] == (
        charge.charge_duration_seconds
    )

    simulation = simulate_plan(routed, plan, problem)
    battery = simulation.metrics["battery_by_robot"]["R2-03"]
    assert battery["initial_battery"] == 21
    assert battery["final_battery"] >= 20


def test_p12_loaded_drop_continues_from_charger_without_returning_to_pickup() -> None:
    problem = {
        "reference_time": "2026-07-24T00:00:00+00:00",
        "time_step_seconds": 5,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
        "charge_rate_percent_per_minute": 5,
        "hard_constraints": ["MINIMUM_REQUIRED_CHARGE"],
        "battery_safety_margin_percent": 0.5,
        "robot_state_overrides": [{"robot_id": "R", "battery_percent": 20.7}],
        "robots": [
            {
                "robot_id": "R",
                "node_id": 2,
                "battery": 20.7,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 30,
            }
        ],
        "nodes": [
            {"node_id": 2, "node_type": "STORAGE", "active": True},
            {"node_id": 4, "node_type": "CHARGER", "active": True},
            {"node_id": 3, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {
                "from_node": 2,
                "to_node": 4,
                "distance": 2,
                "travel_seconds": 5,
                "direction": "BOTH",
            },
            {
                "from_node": 4,
                "to_node": 3,
                "distance": 14,
                "travel_seconds": 20,
                "direction": "BOTH",
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 15,
                "travel_seconds": 25,
                "direction": "BOTH",
            },
        ],
        "tasks": [
            AtomicTask(
                task_id="W:drop",
                work_id="W",
                action="DROP",
                quantity=30,
                source_candidates=[2],
                target_candidates=[3],
                predecessors=["W:pick"],
                same_robot_group="W",
            ).model_dump()
        ],
    }

    plan = _optimizer().optimize(problem)
    charge, drop = plan.scheduled_tasks
    assert charge.action == "CHARGE"
    assert drop.action == "DROP"
    assert drop.source_node == charge.target_node == 4

    routed = PrioritizedTimeExpandedPlanner(problem, 5, 100).solve(plan)
    route_nodes = [point.node_id for point in routed.routes[0].waypoints]
    charger_index = max(
        index
        for index, point in enumerate(routed.routes[0].waypoints)
        if point.node_id == 4 and point.action == "CHARGE"
    )
    assert 2 not in route_nodes[charger_index + 1 :]
    assert route_nodes[-1] == 3


def test_p12_charge_duration_is_reserved_after_actual_arrival() -> None:
    problem = {
        "robots": [{"robot_id": "R", "node_id": 1, "battery": 50}],
        "nodes": [
            {"node_id": 1, "node_type": "AISLE", "active": True},
            {"node_id": 2, "node_type": "CHARGER", "active": True},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 1,
                "travel_seconds": 10,
                "direction": "BOTH",
            }
        ],
    }
    plan = CuOptPlan(
        objective_value=0,
        scheduled_tasks=[
            ScheduledTask(
                task_id="C",
                action="CHARGE",
                robot_id="R",
                source_node=1,
                target_node=2,
                start_time_step=0,
                # Deliberately underestimated. Routing must still reserve the
                # full explicit duration after actual arrival.
                end_time_step=1,
                charge_duration_seconds=20,
                charged_percent=2,
            )
        ],
    )

    routed = PrioritizedTimeExpandedPlanner(problem, 5, 100).solve(plan)
    route = routed.routes[0]
    arrival = next(point.time_step for point in route.waypoints if point.node_id == 2)
    charge_steps = [point.time_step for point in route.waypoints if point.action == "CHARGE"]
    assert charge_steps == list(range(arrival + 1, arrival + 5))
    assert routed.metadata["task_completion_steps"]["C"] == arrival + 4


def test_p12_adapter_rejects_charge_task_without_charge_waypoint() -> None:
    plan = {
        "warehouse_id": 1,
        "charger_node_ids": [2],
        "required_tasks": [{"task_id": "C", "action": "CHARGE"}],
        "inventory_operations": [],
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "C",
                    "action": "CHARGE",
                    "robot_id": "R",
                    "source_node": 1,
                    "target_node": 2,
                    "start_time_step": 0,
                    "end_time_step": 2,
                    "charge_duration_seconds": 5,
                }
            ]
        },
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R",
                    "task_ids": ["C"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 1, "action": "MOVE"},
                    ],
                }
            ]
        },
    }

    _, validation = RobotAdapter(time_step_seconds=5).adapt("P", plan)
    assert validation["valid"] is False
    assert "CHARGE_COMMAND_MISSING:R:C" in validation["errors"]

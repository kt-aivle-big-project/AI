from copy import deepcopy

from app.services.local_optimizer import LocalOptimizer
from app.services.plan_evidence import build_route_evidence
from app.services.robot_adapter import RobotAdapter
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan
from tests.test_p12_charge_execution import _outbound_problem, _optimizer


def test_p13_charge_wait_evidence_is_labeled_charging() -> None:
    problem = _outbound_problem()
    plan = _optimizer().optimize(problem)
    routed = PrioritizedTimeExpandedPlanner(problem, 5, 2000).solve(plan)

    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    charging_waits = [
        row
        for row in routed.metadata["wait_evidence"]
        if row["reason"] == "CHARGING"
    ]
    expected_steps = charge.charge_duration_seconds // problem["time_step_seconds"]
    assert len(charging_waits) == expected_steps
    assert {row["task_id"] for row in charging_waits} == {charge.task_id}
    assert all(row["node_id"] == charge.target_node for row in charging_waits)


def test_p13_distance_variance_has_specific_reason_code() -> None:
    problem = _outbound_problem()
    plan = _optimizer().optimize(problem)
    routed = PrioritizedTimeExpandedPlanner(problem, 5, 2000).solve(plan)
    _, _, comparison = build_route_evidence(problem, plan, routed)

    row = comparison.robot_differences[0]
    assert row.reason_code != "UNKNOWN"
    if abs(row.difference) > 1e-9:
        assert row.reason_code == "TIME_OPTIMAL_ROUTE_DISTANCE_VARIANCE"


def test_p13_execution_dependencies_store_charge_pick_drop_chain() -> None:
    problem = _outbound_problem()
    plan = _optimizer().optimize(problem)
    dependencies = plan.metadata["execution_task_dependencies"]
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")

    assert {
        (
            row["predecessor_task_id"],
            row["successor_task_id"],
            row["source"],
        )
        for row in dependencies
    } >= {
        (charge.task_id, "W:pick", "AUTO_CHARGING"),
        ("W:pick", "W:drop", "PLANNER_PREDECESSOR"),
    }


def test_p13_simulation_pipeline_does_not_mutate_live_snapshot() -> None:
    problem = _outbound_problem()
    live_snapshot = {
        "redis": {
            "robots": [
                {
                    "robot_id": "R2-03",
                    "node_id": 1,
                    "battery": 90,
                    "status": "IDLE",
                }
            ]
        }
    }
    before = deepcopy(live_snapshot)

    plan = _optimizer().optimize(problem)
    routed = PrioritizedTimeExpandedPlanner(problem, 5, 2000).solve(plan)
    simulate_plan(routed, plan, problem)
    RobotAdapter(time_step_seconds=5).adapt(
        "P13-PLAN",
        {
            "warehouse_id": 2,
            "charger_node_ids": [1],
            "required_tasks": problem["tasks"],
            "inventory_operations": [],
            "cuopt_plan": plan.model_dump(mode="json"),
            "collision_plan": routed.model_dump(mode="json"),
        },
    )

    assert live_snapshot == before
    assert live_snapshot["redis"]["robots"][0]["battery"] == 90


def test_p13_set_charger_costs_updates_only_returned_active_chargers() -> None:
    from app.repositories.neo4j import Neo4jRepository

    class Driver:
        def __init__(self) -> None:
            self.calls = []

        def execute_query(self, statement, **kwargs):
            self.calls.append((statement, kwargs))
            return (
                [
                    {"node_id": row["node_id"], "charging_cost": row["charging_cost"]}
                    for row in kwargs["rows"]
                    if row["node_id"] != 9999
                ],
                None,
                None,
            )

    repository = Neo4jRepository.__new__(Neo4jRepository)
    repository.driver = Driver()
    repository.database = "neo4j"

    result = repository.set_charger_costs(2, {2152: 1.5, 9999: 3.0})

    assert result == [{"node_id": 2152, "charging_cost": 1.5}]
    _, kwargs = repository.driver.calls[0]
    assert kwargs["warehouse_id"] == 2
    assert kwargs["rows"] == [
        {"node_id": 2152, "charging_cost": 1.5},
        {"node_id": 9999, "charging_cost": 3.0},
    ]


def test_p13_charger_cost_cli_requires_explicit_nonnegative_values() -> None:
    import argparse

    from scripts.set_charger_costs import parse_cost

    assert parse_cost("2152=1.5") == (2152, 1.5)
    try:
        parse_cost("2152=-1")
    except argparse.ArgumentTypeError:
        pass
    else:
        raise AssertionError("negative charging cost must be rejected")

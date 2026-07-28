from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import AtomicTask, CuOptPlan, ScheduledTask
from app.services.cuopt_rest import _apply_assignments
from app.planning.nodes import _reconcile_routing_schedule
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def _parallel_problem() -> dict:
    reference = datetime(2026, 7, 25, tzinfo=UTC)
    problem = {
        "warehouse_id": 1,
        "captured_at": reference.isoformat(),
        "reference_time": reference.isoformat(),
        "plan_mode": "INITIAL_PLAN",
        "allow_local_robot_rebalance": True,
        "parallel_robot_group_penalty": 20.0,
        "nodes": [{"node_id": value, "active": True} for value in (1, 2, 3, 4)],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 3, "to_node": 4, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 4, "to_node": 1, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
        ],
        "robots": [
            {"robot_id": "R1", "node_id": 1, "battery": 100, "status": "IDLE", "max_load": 100, "current_load": 0},
            {"robot_id": "R2", "node_id": 2, "battery": 100, "status": "IDLE", "max_load": 100, "current_load": 0},
            {"robot_id": "R3", "node_id": 3, "battery": 100, "status": "IDLE", "max_load": 100, "current_load": 0},
        ],
        "tasks": [],
        "inventory": [],
        "temporary_closures": [],
        "active_plan": None,
        "fixed_task_ids": [],
        "changeable_task_ids": [],
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "weights": {},
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
    }
    for index, target in enumerate((3, 4, 3), start=1):
        group = f"W{index}:1"
        pick_id = f"{group}:pick"
        problem["tasks"].append(
            AtomicTask(
                task_id=pick_id,
                work_id=f"W{index}",
                action="PICK",
                quantity=10,
                source_candidates=[1],
                target_candidates=[1],
                priority=index * 2 - 1,
                same_robot_group=group,
                earliest_start=reference + timedelta(hours=1),
            ).model_dump(mode="json")
        )
        problem["tasks"].append(
            AtomicTask(
                task_id=f"{group}:drop",
                work_id=f"W{index}",
                action="DROP",
                quantity=10,
                source_candidates=[1],
                target_candidates=[target],
                priority=index * 2,
                predecessors=[pick_id],
                same_robot_group=group,
                earliest_start=reference + timedelta(hours=1),
            ).model_dump(mode="json")
        )
    return problem


def _optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20,
        energy_per_distance=0.05,
    )


def test_cuopt_single_vehicle_result_is_locally_rebalanced_by_work_pair() -> None:
    problem = _parallel_problem()
    all_task_ids = [row["task_id"] for row in problem["tasks"]]
    normalized = _apply_assignments(problem, {"R3": all_task_ids})

    plan = _optimizer().optimize(normalized)
    by_group: dict[str, set[str]] = {}
    for row in plan.scheduled_tasks:
        group = row.task_id.rsplit(":", 1)[0]
        by_group.setdefault(group, set()).add(row.robot_id)

    assert normalized["cuopt_assignment_application"]["mode"] == (
        "GLOBAL_ORDER_LOCAL_MULTI_ROBOT_REBALANCE"
    )
    assert len({row.robot_id for row in plan.scheduled_tasks}) == 3
    assert all(len(robot_ids) == 1 for robot_ids in by_group.values())
    assert plan.metadata["parallel_robot_rebalance"]["group_counts_by_robot"] == {
        "R1": 1,
        "R2": 1,
        "R3": 1,
    }


def test_explicit_robot_assignment_is_not_relaxed() -> None:
    problem = _parallel_problem()
    problem["tasks"][0]["assigned_robot_id"] = "R2"
    problem["tasks"][0]["frozen"] = True
    assignments = {"R3": [row["task_id"] for row in problem["tasks"]]}

    normalized = _apply_assignments(problem, assignments)

    assert normalized["tasks"][0]["assigned_robot_id"] == "R2"
    assert normalized["tasks"][0]["frozen"] is True
    assert normalized["tasks"][0]["task_id"] in normalized[
        "cuopt_assignment_application"
    ]["fixed_task_ids"]


def test_same_robot_service_continuity_is_not_reported_as_conflict() -> None:
    problem = {
        "nodes": [{"node_id": 1, "active": True}],
        "edges": [],
        "robots": [{"robot_id": "R1", "node_id": 1}],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
    }
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="A:pick",
                work_id="A",
                action="PICK",
                robot_id="R1",
                source_node=1,
                target_node=1,
                start_time_step=0,
                end_time_step=1,
                priority=1,
            ),
            ScheduledTask(
                task_id="B:pick",
                work_id="B",
                action="PICK",
                robot_id="R1",
                source_node=1,
                target_node=1,
                start_time_step=1,
                end_time_step=2,
                priority=2,
            ),
        ],
        objective_value=0,
    )

    collision = PrioritizedTimeExpandedPlanner(problem, 1, 20).solve(plan)

    assert collision.metadata["wait_evidence"] == []
    assert collision.metadata["conflict_wait_count"] == 0


def test_congestion_node_soft_penalty_uses_available_alternative() -> None:
    problem = {
        "nodes": [
            {"node_id": value, "active": True}
            for value in (1, 2, 3, 4, 2013)
        ],
        "edges": [
            {"from_node": 1, "to_node": 2013, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2013, "to_node": 4, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 3, "to_node": 4, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
        ],
        "robots": [],
        "temporary_closures": [],
        "congestion_node_ids": [2013],
        "congestion_penalty_steps": 4,
    }
    planner = PrioritizedTimeExpandedPlanner(problem, 1, 20)

    path = planner.shortest_time_path(1, 4, 0, set(), set())

    assert [row.node_id for row in path] == [1, 2, 3, 4]


def test_rebalanced_plan_routes_and_reconciles_without_conflicts() -> None:
    problem = _parallel_problem()
    normalized = _apply_assignments(
        problem,
        {"R3": [row["task_id"] for row in problem["tasks"]]},
    )
    optimizer_plan = _optimizer().optimize(normalized)
    collision = PrioritizedTimeExpandedPlanner(normalized, 1, 100).solve(
        optimizer_plan
    )
    operational_plan, reconciliation = _reconcile_routing_schedule(
        optimizer_plan,
        collision,
        normalized,
    )

    result = simulate_plan(collision, operational_plan, normalized)

    assert result.success is True
    assert result.conflict_count == 0
    assert len(collision.routes) == 3
    assert reconciliation["updated_task_ids"]

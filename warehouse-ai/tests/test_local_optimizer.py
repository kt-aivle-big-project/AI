from datetime import UTC, datetime

from app.models import AtomicTask, CuOptPlan, ScheduledTask
from app.planning.nodes import resolve_optimization_weights
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20,
        energy_per_distance=0.05,
    )


def base_problem() -> dict:
    return {
        "warehouse_id": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "plan_mode": "INITIAL_PLAN",
        "nodes": [
            {"node_id": node_id, "active": True}
            for node_id in (1, 2, 3, 4)
        ],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 3, "to_node": 4, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 4, "to_node": 1, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
        ],
        "robots": [
            {"robot_id": "R1", "node_id": 1, "battery": 90, "status": "IDLE", "max_load": 100, "current_load": 0},
            {"robot_id": "R2", "node_id": 2, "battery": 80, "status": "IDLE", "max_load": 100, "current_load": 0},
            {"robot_id": "R3", "node_id": 4, "battery": 70, "status": "IDLE", "max_load": 100, "current_load": 0},
        ],
        "tasks": [
            AtomicTask(task_id="T1", action="MOVE", source_candidates=[1], target_candidates=[3], priority=1).model_dump(mode="json"),
            AtomicTask(task_id="T2", action="MOVE", source_candidates=[2], target_candidates=[4], priority=2).model_dump(mode="json"),
            AtomicTask(task_id="T3", action="MOVE", source_candidates=[4], target_candidates=[2], priority=3).model_dump(mode="json"),
        ],
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


def test_initial_plan_is_deterministic_and_assigns_all_tasks() -> None:
    problem = base_problem()
    first = optimizer().optimize(problem)
    second = optimizer().optimize(problem)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.unassigned_task_ids == []
    assert {task.task_id for task in first.scheduled_tasks} == {"T1", "T2", "T3"}
    assert first.metadata["backend"] == "local"


def test_insert_task_preserves_existing_assignment() -> None:
    problem = base_problem()
    problem["plan_mode"] = "INSERT_TASK"
    existing = ScheduledTask(
        task_id="T1",
        robot_id="R3",
        source_node=1,
        target_node=3,
        start_time_step=4,
        end_time_step=6,
        estimated_distance=2,
    )
    problem["active_plan"] = {
        "cuopt_plan": CuOptPlan(
            scheduled_tasks=[existing],
            objective_value=2,
        ).model_dump(mode="json")
    }
    problem["tasks"] = problem["tasks"][:2]

    plan = optimizer().optimize(problem)
    preserved = next(task for task in plan.scheduled_tasks if task.task_id == "T1")

    assert preserved.robot_id == "R3"
    assert preserved.start_time_step == 4
    assert "T1" in plan.metadata["preserved_task_ids"]
    assert {task.task_id for task in plan.scheduled_tasks} == {"T1", "T2"}


def test_failed_robot_is_excluded() -> None:
    problem = base_problem()
    problem["robots"][0]["live_status"] = "ROBOT_FAILED"

    plan = optimizer().optimize(problem)

    assert plan.unassigned_task_ids == []
    assert all(task.robot_id != "R1" for task in plan.scheduled_tasks)


def test_explicit_fast_request_can_change_assignment_with_makespan_profile() -> None:
    problem = {
        "warehouse_id": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "plan_mode": "INITIAL_PLAN",
        "nodes": [
            {"node_id": node_id, "active": True}
            for node_id in range(1, 12)
        ],
        "edges": [
            {
                "from_node": node_id,
                "to_node": node_id + 1,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            }
            for node_id in range(1, 11)
        ],
        "robots": [
            {
                "robot_id": "R1",
                "node_id": 1,
                "battery": 100,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            },
            {
                "robot_id": "R2",
                "node_id": 5,
                "battery": 100,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            },
        ],
        "tasks": [
            AtomicTask(
                task_id="T1",
                action="MOVE",
                source_candidates=[1],
                target_candidates=[11],
                priority=1,
                frozen=True,
                assigned_robot_id="R1",
            ).model_dump(mode="json"),
            AtomicTask(
                task_id="T2",
                action="MOVE",
                source_candidates=[11],
                target_candidates=[11],
                priority=2,
            ).model_dump(mode="json"),
        ],
        "inventory": [],
        "temporary_closures": [],
        "active_plan": None,
        "fixed_task_ids": [],
        "changeable_task_ids": [],
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
    }
    default_profile, default_weights = resolve_optimization_weights(
        "현재 미완료 작업을 계획해줘"
    )
    fast_profile, fast_weights = resolve_optimization_weights(
        "현재 미완료 작업을 최대한 빨리 처리해줘"
    )

    default_problem = {**problem, "weights": default_weights.model_dump()}
    fast_problem = {**problem, "weights": fast_weights.model_dump()}
    default_plan = optimizer().optimize(default_problem)
    fast_plan = optimizer().optimize(fast_problem)
    default_t2 = next(task for task in default_plan.scheduled_tasks if task.task_id == "T2")
    fast_t2 = next(task for task in fast_plan.scheduled_tasks if task.task_id == "T2")

    assert default_profile == "DEFAULT"
    assert fast_profile == "MAKESPAN"
    assert fast_weights.makespan > default_weights.makespan
    assert default_t2.robot_id == "R1"
    assert fast_t2.robot_id == "R2"


def test_closed_node_is_not_used_by_internal_router() -> None:
    problem = base_problem()
    problem["temporary_closures"] = [{"node_id": 2}]
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="T1",
                robot_id="R1",
                source_node=1,
                target_node=3,
                start_time_step=0,
                end_time_step=2,
            )
        ],
        objective_value=2,
    )

    routes = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(plan)
    result = simulate_plan(routes, plan, problem)

    assert result.valid
    assert all(
        waypoint.node_id != 2
        for route in routes.routes
        for waypoint in route.waypoints
    )
    assert result.conflict_count == 0

from datetime import UTC, datetime, timedelta, timezone

from app.models import (
    AtomicTask,
    CollisionFreePlan,
    CuOptPlan,
    ScheduledTask,
    TimedRoute,
    TimedWaypoint,
)
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan
from app.time_utils import deadline_time_step


REFERENCE_TIME = datetime(2026, 7, 21, 2, 56, 53, tzinfo=UTC)
TIME_STEP_SECONDS = 5


def tardiness_problem(deadlines: list[datetime]) -> dict:
    return {
        "warehouse_id": 1,
        "captured_at": REFERENCE_TIME.isoformat(),
        "reference_time": REFERENCE_TIME.isoformat(),
        "time_step_seconds": TIME_STEP_SECONDS,
        "plan_mode": "INITIAL_PLAN",
        "nodes": [
            {"node_id": 1, "active": True},
            {"node_id": 2, "active": True},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 15,
                "travel_seconds": 15,
                "direction": "BOTH",
                "active": True,
            }
        ],
        "robots": [
            {
                "robot_id": f"R{index}",
                "node_id": 1,
                "battery": 100,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            }
            for index in range(1, len(deadlines) + 1)
        ],
        "tasks": [
            AtomicTask(
                task_id=f"W-{index:03d}:move",
                work_id=f"W-{index:03d}",
                action="MOVE",
                source_candidates=[1],
                target_candidates=[2],
                priority=index,
                deadline=deadline,
            ).model_dump(mode="json")
            for index, deadline in enumerate(deadlines, start=1)
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
        "energy_per_distance": 0.01,
    }


def evaluate(problem: dict):
    optimizer = LocalOptimizer(
        time_step_seconds=TIME_STEP_SECONDS,
        min_robot_battery=20,
        energy_per_distance=0.01,
    )
    plan = optimizer.optimize(problem)
    routes = PrioritizedTimeExpandedPlanner(
        problem,
        TIME_STEP_SECONDS,
        100,
    ).solve(plan)
    result = simulate_plan(routes, plan, problem)
    return plan, routes, result


def test_deadline_one_hour_in_future_has_zero_tardiness() -> None:
    plan, _, result = evaluate(
        tardiness_problem([REFERENCE_TIME + timedelta(hours=1)])
    )

    assert plan.scheduled_tasks[0].end_time_step == 3
    assert plan.metadata["tardiness_time_steps"] == 0
    assert result.tardiness == 0


def test_completion_five_seconds_after_deadline_is_one_step_late() -> None:
    plan, _, result = evaluate(
        tardiness_problem([REFERENCE_TIME + timedelta(seconds=10)])
    )

    assert plan.scheduled_tasks[0].end_time_step == 3
    assert plan.metadata["tardiness_time_steps"] == 1
    assert result.tardiness == 5
    assert result.metrics["tardiness_seconds"] == 5


def test_past_deadline_includes_existing_delay_and_completion_time() -> None:
    plan, _, result = evaluate(
        tardiness_problem([REFERENCE_TIME - timedelta(seconds=10)])
    )

    assert deadline_time_step(
        REFERENCE_TIME - timedelta(seconds=10),
        REFERENCE_TIME,
        TIME_STEP_SECONDS,
    ) == -2
    assert plan.scheduled_tasks[0].end_time_step == 3
    assert plan.metadata["tardiness_time_steps"] == 5
    assert result.tardiness == 25


def test_equivalent_utc_and_kst_deadlines_have_identical_tardiness() -> None:
    utc_deadline = REFERENCE_TIME + timedelta(seconds=10)
    kst_deadline = utc_deadline.astimezone(timezone(timedelta(hours=9)))

    utc_plan, _, utc_result = evaluate(tardiness_problem([utc_deadline]))
    kst_plan, _, kst_result = evaluate(tardiness_problem([kst_deadline]))

    assert AtomicTask.model_validate(
        tardiness_problem([kst_deadline])["tasks"][0]
    ).deadline == utc_deadline
    assert utc_plan.metadata["tardiness_time_steps"] == kst_plan.metadata[
        "tardiness_time_steps"
    ]
    assert utc_result.tardiness == kst_result.tardiness == 5


def test_plan_only_validation_and_simulate_only_use_same_tardiness() -> None:
    problem = tardiness_problem([REFERENCE_TIME + timedelta(seconds=10)])
    plan, routes, _ = evaluate(problem)

    plan_only_validation = simulate_plan(
        routes,
        plan,
        problem,
        include_timeline=False,
    )
    simulate_only_result = simulate_plan(
        routes,
        plan,
        problem,
        include_timeline=True,
    )

    assert plan.metadata["tardiness_time_steps"] * TIME_STEP_SECONDS == 5
    assert plan_only_validation.tardiness == simulate_only_result.tardiness == 5


def test_hourly_deadlines_remain_positive_relative_steps_and_zero_tardiness() -> None:
    deadlines = [
        REFERENCE_TIME + timedelta(hours=hours)
        for hours in (1, 2, 3)
    ]
    relative_steps = [
        deadline_time_step(deadline, REFERENCE_TIME, TIME_STEP_SECONDS)
        for deadline in deadlines
    ]
    plan = LocalOptimizer(
        time_step_seconds=TIME_STEP_SECONDS,
        min_robot_battery=20,
        energy_per_distance=0.01,
    ).optimize(tardiness_problem(deadlines))

    assert relative_steps == [720, 1440, 2160]
    assert all(step >= 0 for step in relative_steps)
    assert plan.metadata["tardiness_time_steps"] == 0


def test_only_overdue_tasks_contribute_to_final_tardiness() -> None:
    _, _, result = evaluate(
        tardiness_problem(
            [
                REFERENCE_TIME - timedelta(seconds=5),
                REFERENCE_TIME + timedelta(minutes=1),
            ]
        )
    )

    assert result.tardiness == 20
    assert result.metrics["tardiness_by_task"] == {"W-001:move": 4}


def test_tardiness_uses_final_routing_arrival_not_optimizer_estimate() -> None:
    problem = tardiness_problem([REFERENCE_TIME + timedelta(seconds=10)])
    plan, routes, _ = evaluate(problem)
    estimated_plan = plan.model_copy(
        update={
            "scheduled_tasks": [
                plan.scheduled_tasks[0].model_copy(update={"end_time_step": 0})
            ]
        }
    )

    result = simulate_plan(routes, estimated_plan, problem)

    assert routes.metadata["task_completion_steps"]["W-001:move"] == 3
    assert result.tardiness == 5
    assert result.metrics["tardiness_by_task"] == {"W-001:move": 1}


def test_reproduction_past_deadlines_total_21835_seconds() -> None:
    reference = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
    tasks = [
        AtomicTask(
            task_id="A:move",
            work_id="A",
            action="MOVE",
            source_candidates=[1],
            target_candidates=[2],
            deadline=datetime(2026, 7, 23, 16, 30, tzinfo=UTC),
        ),
        AtomicTask(
            task_id="F:move",
            work_id="F",
            action="MOVE",
            source_candidates=[1],
            target_candidates=[2],
            deadline=datetime(2026, 7, 23, 22, 0, tzinfo=UTC),
        ),
    ]
    problem = {
        "reference_time": reference.isoformat(),
        "captured_at": reference.isoformat(),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "nodes": [],
        "edges": [],
        "robots": [],
        "inventory": [],
    }
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=task.task_id,
                work_id=task.work_id,
                robot_id=f"R-{index}",
                source_node=1,
                target_node=2,
                start_time_step=0,
                end_time_step=1,
            )
            for index, task in enumerate(tasks, start=1)
        ],
        objective_value=0,
    )
    completion_steps = {"A:move": 23, "F:move": 24}
    routes = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[
            TimedRoute(
                robot_id=f"R-{index}",
                task_ids=[task.task_id],
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0),
                    TimedWaypoint(
                        node_id=2,
                        time_step=completion_steps[task.task_id],
                    ),
                ],
            )
            for index, task in enumerate(tasks, start=1)
        ],
        time_step_seconds=5,
        total_distance=0,
        metadata={"task_completion_steps": completion_steps},
    )

    result = simulate_plan(routes, plan, problem)

    assert result.metrics["tardiness_by_task"] == {
        "A:move": 4163,
        "F:move": 204,
    }
    assert result.metrics["tardiness_by_task_unit"] == "time_step"
    assert result.metrics["tardiness_by_task_seconds"] == {
        "A:move": 20_815,
        "F:move": 1_020,
    }
    assert result.metrics["tardiness_seconds"] == 21_835
    assert result.tardiness == 21_835
    assert "20815초" in result.warnings[0]
    assert "1020초" in result.warnings[1]

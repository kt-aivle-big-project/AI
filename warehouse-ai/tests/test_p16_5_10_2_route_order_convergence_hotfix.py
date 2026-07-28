from __future__ import annotations

from app.models import CuOptPlan, ScheduledTask
from app.services.routing import (
    ROUTING_TASK_ORDER_POLICY,
    PrioritizedTimeExpandedPlanner,
)
from app.services.shared_resources import schedule_shared_resources
from tests.test_p16_5_8_opportunity_charging import _p16_5_8_problem


def _dependencies() -> list[dict]:
    chain = [
        ("charge:1", "move:1"),
        ("move:1", "pick:1"),
        ("pick:1", "drop:1"),
        ("drop:1", "charge:2"),
        ("charge:2", "move:2"),
        ("move:2", "pick:2"),
        ("pick:2", "drop:2"),
    ]
    return [
        {
            "predecessor_task_id": predecessor,
            "successor_task_id": successor,
            "dependency_type": "FINISH_TO_START",
            "lag_seconds": 0,
            "source": "P16_5_10_2_REGRESSION",
        }
        for predecessor, successor in chain
    ]


def _scheduled_chain() -> list[ScheduledTask]:
    # Synthetic CHARGE/MOVE priorities intentionally conflict with chronological
    # order. P16.5.10.1 routed priority=9 charge:2 before priority=10 pick:1,
    # producing a DROP -> CHARGE dependency cycle during resource reconciliation.
    return [
        ScheduledTask(
            task_id="charge:1",
            work_id="W1",
            action="CHARGE",
            robot_id="R2-02",
            source_node=2150,
            target_node=2150,
            start_time_step=0,
            end_time_step=12,
            priority=8,
            charge_duration_seconds=30,
            charged_percent=2.5,
        ),
        ScheduledTask(
            task_id="move:1",
            work_id="W1",
            action="MOVE",
            robot_id="R2-02",
            source_node=2150,
            target_node=2139,
            start_time_step=12,
            end_time_step=14,
            priority=7,
        ),
        ScheduledTask(
            task_id="pick:1",
            work_id="W1",
            action="PICK",
            robot_id="R2-02",
            source_node=2139,
            target_node=2139,
            start_time_step=2340,
            end_time_step=2341,
            priority=10,
        ),
        ScheduledTask(
            task_id="drop:1",
            work_id="W1",
            action="DROP",
            robot_id="R2-02",
            source_node=2139,
            target_node=2088,
            start_time_step=2341,
            end_time_step=2344,
            priority=12,
        ),
        ScheduledTask(
            task_id="charge:2",
            work_id="W2",
            action="CHARGE",
            robot_id="R2-02",
            source_node=2155,
            target_node=2155,
            start_time_step=2350,
            end_time_step=2360,
            priority=9,
            charge_duration_seconds=25,
            charged_percent=2.0,
        ),
        ScheduledTask(
            task_id="move:2",
            work_id="W2",
            action="MOVE",
            robot_id="R2-02",
            source_node=2155,
            target_node=2088,
            start_time_step=2360,
            end_time_step=2365,
            priority=11,
        ),
        ScheduledTask(
            task_id="pick:2",
            work_id="W2",
            action="PICK",
            robot_id="R2-02",
            source_node=2088,
            target_node=2088,
            start_time_step=4140,
            end_time_step=4141,
            priority=13,
        ),
        ScheduledTask(
            task_id="drop:2",
            work_id="W2",
            action="DROP",
            robot_id="R2-02",
            source_node=2088,
            target_node=2146,
            start_time_step=4141,
            end_time_step=4148,
            priority=14,
        ),
    ]


def _problem() -> dict:
    problem = _p16_5_8_problem()
    problem["time_step_seconds"] = 5
    for node in problem["nodes"]:
        node_id = int(node["node_id"])
        if node_id in {2088, 2139, 2146}:
            node["service_capacity"] = 1
            node["service_duration_seconds"] = 5
        if 2150 <= node_id <= 2159:
            node["charger_capacity"] = 1
    # This regression tests convergence, not business deadline policy.
    problem["tasks"] = []
    return problem


def test_router_respects_dependency_chain_before_priority() -> None:
    plan = CuOptPlan(
        scheduled_tasks=_scheduled_chain(),
        objective_value=0,
        metadata={"execution_task_dependencies": _dependencies()},
    )

    collision = PrioritizedTimeExpandedPlanner(_problem(), 5, 720).solve(plan)
    route = next(row for row in collision.routes if row.robot_id == "R2-02")

    assert route.task_ids == [
        "charge:1",
        "move:1",
        "pick:1",
        "drop:1",
        "charge:2",
        "move:2",
        "pick:2",
        "drop:2",
    ]
    assert collision.metadata["task_ordering_policy"] == ROUTING_TASK_ORDER_POLICY


def test_resource_scheduler_repairs_route_times_without_runaway() -> None:
    # Reproduce the P16.5.10.1 post-routing shape: priority-first routing placed
    # charge:2 before pick:1/drop:1. The dependency-aware resource order must
    # repair this once instead of adding the same 62-step delay indefinitely.
    bad_route_times = {
        "charge:1": (34, 46),
        "move:1": (12, 34),
        "pick:1": (2365, 2376),
        "drop:1": (2400, 2412),
        "charge:2": (2350, 2365),
        "move:2": (2376, 2400),
        "pick:2": (4141, 4153),
        "drop:2": (4153, 4167),
    }
    tasks = [
        task.model_copy(
            update={
                "start_time_step": bad_route_times[task.task_id][0],
                "end_time_step": bad_route_times[task.task_id][1],
            }
        )
        for task in _scheduled_chain()
    ]
    plan = CuOptPlan(
        scheduled_tasks=tasks,
        objective_value=0,
        metadata={"execution_task_dependencies": _dependencies()},
    )

    repaired, result = schedule_shared_resources(_problem(), plan)
    by_id = {task.task_id: task for task in repaired.scheduled_tasks}

    assert result["valid"] is True
    assert "RESOURCE_SCHEDULER_DID_NOT_CONVERGE" not in result["errors"]
    assert result["iterations"] < 20
    assert max(task.end_time_step for task in repaired.scheduled_tasks) < 5000
    for dependency in _dependencies():
        predecessor = by_id[dependency["predecessor_task_id"]]
        successor = by_id[dependency["successor_task_id"]]
        assert predecessor.end_time_step <= successor.start_time_step

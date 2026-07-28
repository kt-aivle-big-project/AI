from __future__ import annotations

import json
from pathlib import Path

from app.models import CuOptPlan, ScheduledTask
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.scheduling import reconcile_task_time_window
from app.services.simulation import simulate_plan


ROOT = Path(__file__).resolve().parents[1]


def _warehouse_two_problem() -> dict:
    return {
        "warehouse_id": 2,
        "captured_at": "2026-07-24T22:15:00+00:00",
        "reference_time": "2026-07-24T22:15:00+00:00",
        "nodes": json.loads((ROOT / "examples/map_nodes.json").read_text()),
        "edges": json.loads((ROOT / "examples/map_edges.json").read_text()),
        "robots": [
            {
                "robot_id": "R2-01",
                "node_id": 2146,
                "battery": 90,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            },
            {
                "robot_id": "R2-02",
                "node_id": 2146,
                "battery": 90,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            },
            {
                "robot_id": "R2-03",
                "node_id": 2152,
                "battery": 90,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            },
        ],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "congestion_node_ids": [2013],
        "congestion_penalty_steps": 4,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
    }


def _daily_multi_robot_plan() -> CuOptPlan:
    raw = [
        ("B:pick", "B", "PICK", "R2-03", 2088, 2088, 1260, 1264, 1),
        ("A:pick", "A", "PICK", "R2-03", 2088, 2088, 1264, 1265, 2),
        ("B:drop", "B", "DROP", "R2-03", 2088, 2146, 1265, 1272, 3),
        ("A:drop", "A", "DROP", "R2-03", 2088, 2146, 1272, 1284, 4),
        ("C:pick", "C", "PICK", "R2-01", 2139, 2139, 2340, 2347, 5),
        ("D:pick", "D", "PICK", "R2-02", 2139, 2139, 2340, 2347, 7),
        ("C:drop", "C", "DROP", "R2-01", 2139, 2088, 2347, 2350, 6),
        ("D:drop", "D", "DROP", "R2-02", 2139, 2088, 2347, 2350, 8),
        ("E:pick", "E", "PICK", "R2-01", 2088, 2088, 4140, 4141, 9),
        ("F:pick", "F", "PICK", "R2-02", 2088, 2088, 4140, 4141, 11),
        ("E:drop", "E", "DROP", "R2-01", 2088, 2146, 4141, 4148, 10),
        ("F:drop", "F", "DROP", "R2-02", 2088, 2146, 4141, 4148, 12),
        ("C2:pick", "C2", "PICK", "R2-01", 2139, 2139, 5580, 5587, 13),
        ("C2:drop", "C2", "DROP", "R2-01", 2139, 2088, 5587, 5590, 14),
    ]
    return CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=task_id,
                work_id=work_id,
                action=action,
                robot_id=robot_id,
                source_node=source,
                target_node=target,
                start_time_step=start,
                end_time_step=end,
                priority=priority,
            )
            for (
                task_id,
                work_id,
                action,
                robot_id,
                source,
                target,
                start,
                end,
                priority,
            ) in raw
        ],
        objective_value=0,
    )


def _reconcile(plan: CuOptPlan, collision) -> CuOptPlan:
    route_starts = collision.metadata["task_start_steps"]
    route_ends = collision.metadata["task_completion_steps"]
    tasks = []
    for task in plan.scheduled_tasks:
        start, end = reconcile_task_time_window(
            task,
            route_start_step=route_starts.get(task.task_id),
            route_end_step=route_ends.get(task.task_id),
        )
        tasks.append(
            task.model_copy(
                update={"start_time_step": start, "end_time_step": end}
            )
        )
    return plan.model_copy(update={"scheduled_tasks": tasks})


def test_long_idle_releases_shared_storage_and_routes_all_robots() -> None:
    problem = _warehouse_two_problem()
    plan = _daily_multi_robot_plan()

    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(plan)
    operational = _reconcile(plan, collision)
    simulation = simulate_plan(collision, operational, problem)

    assert simulation.success is True
    assert simulation.valid is True
    assert simulation.conflict_count == 0
    assert len(collision.routes) == 3
    assert collision.metadata["idle_relocation_count"] >= 3
    assert any(
        row["from_node"] == 2088 and row["robot_id"] == "R2-01"
        for row in collision.metadata["idle_relocations"]
    )
    assert any(
        row["from_node"] == 2088 and row["robot_id"] == "R2-02"
        for row in collision.metadata["idle_relocations"]
    )


def test_holding_nodes_avoid_congestion_and_map_cut_vertices() -> None:
    problem = _warehouse_two_problem()
    planner = PrioritizedTimeExpandedPlanner(problem, 5, 720)
    collision = planner.solve(_daily_multi_robot_plan())

    holding_nodes = {
        int(row["holding_node_id"])
        for row in collision.metadata["idle_relocations"]
    }
    assert 2013 not in holding_nodes
    assert not holding_nodes.intersection(planner.articulation_node_ids)
    # 2044 is the only gateway to OUTBOUND 2146 and caused P16.5.5's
    # second afternoon DROP to become unreachable when used as a holding node.
    assert 2044 not in holding_nodes


def test_sparse_robots_sharing_initial_node_activate_sequentially() -> None:
    collision = PrioritizedTimeExpandedPlanner(
        _warehouse_two_problem(), 5, 720
    ).solve(_daily_multi_robot_plan())
    routes = {route.robot_id: route for route in collision.routes}

    assert routes["R2-01"].waypoints[0].node_id == 2146
    assert routes["R2-02"].waypoints[0].node_id == 2146
    assert routes["R2-01"].waypoints[0].time_step != routes["R2-02"].waypoints[0].time_step

"""Deterministic P16.5.3 time-monotonicity release checks."""

from __future__ import annotations

import json

from app.models import CuOptPlan, ScheduledTask
from app.services.cuopt_rest import build_cuopt_routing_payload
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.scheduling import reconcile_task_time_window
from app.services.simulation import simulate_plan


def _problem() -> dict:
    return {
        "time_step_seconds": 5,
        "robots": [{"robot_id": "R1", "node_id": 1, "status": "IDLE", "max_load": 100, "battery": 100, "min_battery": 20}],
        "nodes": [
            {"node_id": 1, "active": True},
            {"node_id": 2, "active": True},
            {"node_id": 3, "active": True},
        ],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 5, "direction": "BOTH", "active": True},
            {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 5, "direction": "BOTH", "active": True},
        ],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "reference_time": "2026-07-25T00:00:00Z",
        "tasks": [
            {
                "task_id": "T-PICK",
                "action": "PICK",
                "quantity": 1,
                "source_candidates": [3],
                "target_candidates": [3],
            },
            {
                "task_id": "T-DROP",
                "action": "DROP",
                "quantity": 1,
                "source_candidates": [3],
                "target_candidates": [2],
                "predecessors": ["T-PICK"],
            },
        ],
    }


def main() -> None:
    problem = _problem()
    scheduled = [
        ScheduledTask(task_id="T-MOVE", action="MOVE", robot_id="R1", source_node=1, target_node=3, start_time_step=0, end_time_step=2, priority=1),
        ScheduledTask(task_id="T-PICK", action="PICK", robot_id="R1", source_node=3, target_node=3, start_time_step=100, end_time_step=101, priority=2),
        ScheduledTask(task_id="T-DROP", action="DROP", robot_id="R1", source_node=3, target_node=2, start_time_step=101, end_time_step=102, priority=3),
    ]
    optimizer_plan = CuOptPlan(scheduled_tasks=scheduled, objective_value=0)
    collision_plan = PrioritizedTimeExpandedPlanner(problem, 5, 200).solve(optimizer_plan)
    steps = [row.time_step for row in collision_plan.routes[0].waypoints]
    strict = all(right > left for left, right in zip(steps, steps[1:]))
    completion = collision_plan.metadata["task_completion_steps"].get("T-PICK")
    simulation_ok = simulate_plan(collision_plan, optimizer_plan).success

    start, end = reconcile_task_time_window(
        ScheduledTask(task_id="P", action="PICK", robot_id="R1", source_node=1, target_node=1, start_time_step=10, end_time_step=11),
        route_start_step=10,
        route_end_step=10,
    )

    payload, _ = build_cuopt_routing_payload(problem, solver_time_limit_seconds=10)
    service_times = payload["task_data"]["service_times"]

    checks = {
        "strict_route_time": strict,
        "same_node_pick_completion": completion == 101,
        "simulation_success": simulation_ok,
        "reconcile_preserves_duration": (start, end) == (10, 11),
        "cuopt_service_times": service_times == [5, 5],
    }
    result = {"all_passed": all(checks.values()), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

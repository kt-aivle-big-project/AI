"""Run deterministic P15 multi-robot collision checks without LLM or databases."""

from __future__ import annotations

import json

from app.models import CuOptPlan, ScheduledTask
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def edge(start: int, target: int) -> dict:
    return {
        "from_node": start,
        "to_node": target,
        "distance": 1.0,
        "travel_seconds": 1,
        "direction": "BOTH",
        "active": True,
    }


def run_case(name: str, problem: dict, tasks: list[ScheduledTask]) -> dict:
    cuopt = CuOptPlan(scheduled_tasks=tasks, objective_value=0)
    collision = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(cuopt)
    simulation = simulate_plan(collision, cuopt, problem)
    return {
        "scenario": name,
        "success": simulation.conflict_count == 0,
        "conflict_count": simulation.conflict_count,
        "wait_evidence": collision.metadata.get("wait_evidence", []),
        "resolution_events": collision.metadata.get("resolution_events", []),
        "reroute_count": collision.metadata.get("reroute_count", 0),
        "routes": [route.model_dump(mode="json") for route in collision.routes],
    }


def base_problem(nodes: list[dict], robots: list[dict], edges: list[dict]) -> dict:
    return {
        "nodes": nodes,
        "robots": robots,
        "edges": edges,
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
    }


def run_all_checks() -> dict:
    central = base_problem(
        [{"node_id": value, "node_type": "INTERSECTION"} for value in range(1, 6)],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 4}],
        [edge(1, 2), edge(2, 3), edge(4, 2), edge(2, 5)],
    )
    vertex_tasks = [
        ScheduledTask(task_id="V1", robot_id="R1", source_node=1, target_node=3, start_time_step=0, end_time_step=10, priority=10),
        ScheduledTask(task_id="V2", robot_id="R2", source_node=4, target_node=5, start_time_step=0, end_time_step=10, priority=20),
    ]
    emergency_tasks = [
        ScheduledTask(task_id="NORMAL", robot_id="R1", source_node=1, target_node=3, start_time_step=0, end_time_step=10, priority=50),
        ScheduledTask(task_id="EMERGENCY", robot_id="R2", source_node=4, target_node=5, start_time_step=0, end_time_step=10, priority=1),
    ]

    swap = base_problem(
        [{"node_id": value, "node_type": "INTERSECTION"} for value in range(1, 5)],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 2}],
        [edge(1, 2), edge(2, 3), edge(3, 4), edge(4, 1)],
    )
    swap_tasks = [
        ScheduledTask(task_id="E1", robot_id="R1", source_node=1, target_node=2, start_time_step=0, end_time_step=10, priority=1),
        ScheduledTask(task_id="E2", robot_id="R2", source_node=2, target_node=1, start_time_step=0, end_time_step=10, priority=2),
    ]

    charger = base_problem(
        [
            {"node_id": 1, "node_type": "INTERSECTION"},
            {"node_id": 2, "node_type": "CHARGER"},
            {"node_id": 3, "node_type": "INTERSECTION"},
        ],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 3}],
        [edge(1, 2), edge(3, 2)],
    )
    charger_tasks = [
        ScheduledTask(task_id="C1", action="CHARGE", robot_id="R1", source_node=2, target_node=2, start_time_step=0, end_time_step=10, priority=1, charge_duration_seconds=2),
        ScheduledTask(task_id="C2", action="CHARGE", robot_id="R2", source_node=2, target_node=2, start_time_step=0, end_time_step=10, priority=2, charge_duration_seconds=2),
    ]

    results = [
        run_case("VERTEX_WAIT", central, vertex_tasks),
        run_case("EDGE_SWAP_REROUTE", swap, swap_tasks),
        run_case("SHARED_CHARGER", charger, charger_tasks),
        run_case("EMERGENCY_PRIORITY", central, emergency_tasks),
    ]
    return {"all_passed": all(row["success"] for row in results), "results": results}


def main() -> None:
    print(json.dumps(run_all_checks(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

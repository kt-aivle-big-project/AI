from __future__ import annotations

import json
from pathlib import Path

from app.services.local_optimizer import LocalOptimizer


ROOT = Path(__file__).resolve().parents[1]


def _warehouse_graph() -> tuple[list[dict], list[dict]]:
    nodes = json.loads((ROOT / "examples" / "map_nodes.json").read_text(encoding="utf-8"))
    edges = json.loads((ROOT / "examples" / "map_edges.json").read_text(encoding="utf-8"))
    return nodes, edges


def test_server_derived_low_battery_can_charge_before_resuming_current_pick() -> None:
    nodes, edges = _warehouse_graph()
    pick_id = "4174d465-5eeb-45eb-b7d2-86faa37f9dc2:1:pick"
    drop_id = "4174d465-5eeb-45eb-b7d2-86faa37f9dc2:1:drop"
    work_id = "4174d465-5eeb-45eb-b7d2-86faa37f9dc2"
    problem = {
        "warehouse_id": 2,
        "reference_time": "2026-07-27T00:00:07.800279+00:00",
        "captured_at": "2026-07-27T00:00:07.800279+00:00",
        "time_step_seconds": 5,
        "plan_mode": "LOCAL_REPLAN",
        "min_robot_battery": 20.0,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80.0,
        "charge_rate_percent_per_minute": 5.0,
        "nodes": nodes,
        "edges": edges,
        "temporary_closures": [],
        "robots": [
            {
                "robot_id": "R2-03",
                "node_id": 2080,
                "battery": 21.0,
                "status": "IDLE",
                "max_load": 50,
                "current_load": 0,
            }
        ],
        "tasks": [
            {
                "task_id": pick_id,
                "work_id": work_id,
                "action": "PICK",
                "item_id": "C",
                "quantity": 10,
                "source_candidates": [2088],
                "target_candidates": [2088],
                "priority": 50,
                "deadline": "2026-07-27T01:00:00Z",
                "predecessors": [],
                "dependencies": [],
                "earliest_start": "2026-07-27T00:00:00Z",
                "latest_finish": "2026-07-27T01:00:00Z",
                "time_constraint_type": "HARD_WINDOW",
                "same_robot_group": f"{work_id}:1",
                "frozen": False,
                "assigned_robot_id": "R2-03",
                "inventory_allocations": [],
            },
            {
                "task_id": drop_id,
                "work_id": work_id,
                "action": "DROP",
                "item_id": "C",
                "quantity": 10,
                "source_candidates": [2088],
                "target_candidates": [2146],
                "priority": 50,
                "deadline": "2026-07-27T01:00:00Z",
                "predecessors": [pick_id],
                "dependencies": [],
                "earliest_start": "2026-07-27T00:00:00Z",
                "latest_finish": "2026-07-27T01:00:00Z",
                "time_constraint_type": "HARD_WINDOW",
                "same_robot_group": f"{work_id}:1",
                "frozen": False,
                "assigned_robot_id": "R2-03",
                "inventory_allocations": [],
            },
        ],
        "active_plan": {
            "plan_version": "P-SERVER",
            "reference_time": "2026-07-26T13:32:22.800279+00:00",
            "candidate_plan": True,
            "cuopt_plan": {
                "scheduled_tasks": [
                    {
                        "task_id": pick_id,
                        "work_id": work_id,
                        "action": "PICK",
                        "robot_id": "R2-03",
                        "source_node": 2088,
                        "target_node": 2088,
                        "start_time_step": 7532,
                        "end_time_step": 7544,
                        "priority": 1,
                        "estimated_distance": 10.93,
                        "estimated_energy": 0.5465,
                    },
                    {
                        "task_id": drop_id,
                        "work_id": work_id,
                        "action": "DROP",
                        "robot_id": "R2-03",
                        "source_node": 2088,
                        "target_node": 2146,
                        "start_time_step": 7544,
                        "end_time_step": 7557,
                        "priority": 2,
                        "estimated_distance": 18.71,
                        "estimated_energy": 0.9355,
                    },
                ],
                "metadata": {},
            },
        },
        "fixed_task_ids": [],
        "changeable_task_ids": [pick_id, drop_id],
        "affected_robot_ids": ["R2-03"],
        "weights": {},
    }

    optimizer = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20.0,
        energy_per_distance=0.05,
        charge_target_battery=80.0,
        charge_rate_percent_per_minute=5.0,
        battery_safety_margin_percent=0.5,
    )
    plan = optimizer.optimize(problem)

    assert plan.unassigned_task_ids == []
    rows = [row for row in plan.scheduled_tasks if row.robot_id == "R2-03"]
    actions = [row.action for row in rows]
    assert actions == ["CHARGE", "PICK", "DROP"]
    charge, pick, drop = rows
    assert charge.source_node == 2080
    assert charge.target_node == 2152
    assert charge.start_time_step <= pick.start_time_step
    assert charge.end_time_step == pick.start_time_step
    assert pick.end_time_step <= drop.start_time_step
    assert charge.charge_target_battery == 80.0
    assert charge.charged_percent > 0
    assert plan.metadata["charger_selections"][0]["battery_at_charger"] >= 20.5
    assert plan.metadata["charger_selections"][0]["projected_final_battery"] >= 20.0

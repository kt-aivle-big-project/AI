from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.models import CuOptPlan, ScheduledTask
from app.planning import nodes
from app.services.charge_visit_optimization import (
    prepare_charge_visit_optimization_problem,
)
from app.services.command_language import parse_deterministic_command
from app.services.event_replan import _planning_failure_debug
from app.services.local_optimizer import LocalOptimizer
from app.services.optimizer import OptimizationOutcome


ROOT = Path(__file__).resolve().parents[1]
C_WORK = "e4b6d147-e479-4ad2-b788-62bf76ee7dc5"
D_WORK = "b0fcf8c9-9b35-4d6e-b922-c908ea00dea5"
C_PICK = f"{C_WORK}:1:pick"
C_DROP = f"{C_WORK}:1:drop"
D_PICK = f"{D_WORK}:1:pick"
D_DROP = f"{D_WORK}:1:drop"


def _warehouse_graph() -> tuple[list[dict], list[dict]]:
    nodes_rows = json.loads(
        (ROOT / "examples" / "map_nodes.json").read_text(encoding="utf-8")
    )
    edge_rows = json.loads(
        (ROOT / "examples" / "map_edges.json").read_text(encoding="utf-8")
    )
    return nodes_rows, edge_rows


def _task(
    task_id: str,
    work_id: str,
    action: str,
    source: int,
    target: int,
    predecessors: list[str],
    earliest_start: str,
    latest_finish: str,
    robot_id: str,
    *,
    frozen: bool,
) -> dict:
    return {
        "task_id": task_id,
        "work_id": work_id,
        "action": action,
        "item_id": "C" if work_id == C_WORK else "D",
        "quantity": 10 if work_id == C_WORK else 5,
        "source_candidates": [source],
        "target_candidates": [target],
        "priority": 50,
        "deadline": latest_finish,
        "predecessors": predecessors,
        "dependencies": [],
        "earliest_start": earliest_start,
        "latest_finish": latest_finish,
        "time_constraint_type": "HARD_WINDOW",
        "same_robot_group": f"{work_id}:1",
        "frozen": frozen,
        "assigned_robot_id": robot_id,
        "inventory_allocations": [],
    }


def _active_schedule() -> list[dict]:
    return [
        {
            "task_id": C_PICK,
            "work_id": C_WORK,
            "action": "PICK",
            "robot_id": "R2-03",
            "source_node": 2088,
            "target_node": 2088,
            "start_time_step": 0,
            "end_time_step": 12,
            "priority": 1,
            "estimated_distance": 10.93,
            "estimated_energy": 0.5465,
        },
        {
            "task_id": C_DROP,
            "work_id": C_WORK,
            "action": "DROP",
            "robot_id": "R2-03",
            "source_node": 2088,
            "target_node": 2146,
            "start_time_step": 12,
            "end_time_step": 25,
            "priority": 2,
            "estimated_distance": 18.71,
            "estimated_energy": 0.9355,
        },
        {
            "task_id": D_PICK,
            "work_id": D_WORK,
            "action": "PICK",
            "robot_id": "R2-01",
            "source_node": 2088,
            "target_node": 2088,
            "start_time_step": 657,
            "end_time_step": 666,
            "priority": 3,
            "estimated_distance": 18.71,
            "estimated_energy": 0.9355,
        },
        {
            "task_id": D_DROP,
            "work_id": D_WORK,
            "action": "DROP",
            "robot_id": "R2-01",
            "source_node": 2088,
            "target_node": 2146,
            "start_time_step": 666,
            "end_time_step": 679,
            "priority": 4,
            "estimated_distance": 18.71,
            "estimated_energy": 0.9355,
        },
    ]


def _actual_gate2_2_problem() -> dict:
    graph_nodes, graph_edges = _warehouse_graph()
    return {
        "warehouse_id": 2,
        "captured_at": "2026-07-27T00:05:22.071889+00:00",
        "reference_time": "2026-07-27T00:05:22.071889+00:00",
        "time_step_seconds": 5,
        "max_mapf_time_steps": 720,
        "warehouse_timezone": "Asia/Seoul",
        "plan_mode": "LOCAL_REPLAN",
        "tasks": [
            _task(
                C_PICK,
                C_WORK,
                "PICK",
                2088,
                2088,
                [],
                "2026-07-27T00:00:00Z",
                "2026-07-27T01:00:00Z",
                "R2-03",
                frozen=False,
            ),
            _task(
                C_DROP,
                C_WORK,
                "DROP",
                2088,
                2146,
                [C_PICK],
                "2026-07-27T00:00:00Z",
                "2026-07-27T01:00:00Z",
                "R2-03",
                frozen=False,
            ),
            _task(
                D_PICK,
                D_WORK,
                "PICK",
                2088,
                2088,
                [],
                "2026-07-27T01:00:00Z",
                "2026-07-27T02:00:00Z",
                "R2-01",
                frozen=True,
            ),
            _task(
                D_DROP,
                D_WORK,
                "DROP",
                2088,
                2146,
                [D_PICK],
                "2026-07-27T01:00:00Z",
                "2026-07-27T02:00:00Z",
                "R2-01",
                frozen=True,
            ),
        ],
        "robots": [
            {
                "robot_id": "R2-03",
                "node_id": 2080,
                "battery": 21.0,
                "status": "IDLE",
                "max_load": 50,
                "current_load": 0,
            },
            {
                "robot_id": "R2-01",
                "node_id": 2146,
                "battery": 100.0,
                "status": "IDLE",
                "max_load": 50,
                "current_load": 0,
            },
            {
                "robot_id": "R2-02",
                "node_id": 2146,
                "battery": 100.0,
                "status": "IDLE",
                "max_load": 50,
                "current_load": 0,
            },
        ],
        "nodes": graph_nodes,
        "edges": graph_edges,
        "temporary_closures": [],
        "inventory": [],
        "inventory_operations": [],
        "active_plan": {
            "plan_version": "e91a1f49-00ae-4a6b-a842-84a8774364e6",
            "reference_time": "2026-07-27T00:05:17.071889+00:00",
            "candidate_plan": True,
            "cuopt_plan": {
                "scheduled_tasks": _active_schedule(),
                "metadata": {},
            },
        },
        "fixed_task_ids": [D_PICK, D_DROP],
        "changeable_task_ids": [C_PICK, C_DROP],
        "affected_robot_ids": ["R2-03"],
        "freeze_horizon_seconds": 15,
        "min_robot_battery": 20.0,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80.0,
        "charge_rate_percent_per_minute": 5.0,
        "weights": {},
        "hard_constraints": ["OPPORTUNITY_CHARGING"],
        "opportunity_charging_enabled": True,
        "allow_local_robot_rebalance": False,
        "idle_allowed_node_types": [
            "PARKING",
            "STAGING",
            "HOLDING",
            "CHARGER_WAITING_AREA",
            "ROBOT_PARKING",
        ],
        "idle_relocation_min_gap_steps": 12,
        "runtime_partial_replan": {
            "version": "p16.5.12.1",
            "source_plan_version": "e91a1f49-00ae-4a6b-a842-84a8774364e6",
            "affected_robot_ids": ["R2-03"],
            "affected_task_ids": [C_PICK, C_DROP],
            "protected_task_ids": [D_PICK, D_DROP],
            "changeable_task_ids": [C_PICK, C_DROP],
            "freeze_horizon_seconds": 15,
            "robot_state_overrides": {
                "R2-03": {
                    "event_type": "LOW_BATTERY",
                    "occurred_at": "2026-07-27T00:05:22.071889+00:00",
                    "node_id": 2080,
                    "battery": 21.0,
                    "status": "IDLE",
                    "current_load": "0.00",
                    "max_load": "50.00",
                }
            },
        },
    }


def _local_optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20.0,
        energy_per_distance=0.05,
        charge_target_battery=80.0,
        charge_rate_percent_per_minute=5.0,
        battery_safety_margin_percent=0.5,
    )


def test_past_opened_successor_window_does_not_create_impossible_charge_deadline() -> None:
    problem = _actual_gate2_2_problem()
    first = _local_optimizer().optimize(problem)
    enriched, contract = prepare_charge_visit_optimization_problem(problem, first)

    assert contract["explicit_charge_task_count"] == 1
    charge_id = contract["explicit_charge_task_ids"][0]
    charge_row = next(row for row in enriched["tasks"] if row["task_id"] == charge_id)
    spec = contract["charge_task_specs"][charge_id]

    assert charge_row["earliest_start"] == "2026-07-27T00:05:22.071889+00:00"
    assert charge_row["latest_finish"] is None
    assert charge_row["deadline"] is None
    assert charge_row["time_constraint_type"] == "ASAP"
    assert spec["hard_latest_finish_at"] is None


def test_actual_gate2_2_low_battery_chain_survives_two_pass_and_routes(monkeypatch) -> None:
    problem = _actual_gate2_2_problem()
    interpretation = parse_deterministic_command(
        "저배터리 R2-03의 C상품 작업만 부분 재계획해줘.",
        warehouse_timezone="Asia/Seoul",
    )
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: Settings(
            optimizer_backend="local",
            cuopt_auto_enable=False,
            report_with_llm=False,
            max_mapf_time_steps=720,
        ),
    )
    state = {
        "optimization_problem": problem,
        "interpretation": interpretation.model_dump(mode="json"),
        "snapshot": {"sql": {"works": []}},
        "required_tasks": problem["tasks"],
        "command": {"warehouse_id": 2},
        "schedule_validation": {"valid": True, "errors": []},
        "errors": [],
        "warnings": [],
    }

    state.update(nodes.optimizer_node(state))

    assert state["final_status"] == "OPTIMIZATION_READY"
    assert state["cuopt_plan"]["unassigned_task_ids"] == []
    actions = [
        row["action"]
        for row in state["cuopt_plan"]["scheduled_tasks"]
        if row["robot_id"] == "R2-03"
    ]
    assert actions == ["CHARGE", "MOVE", "PICK", "DROP"]
    two_pass = state["optimizer_execution"]["charge_visit_two_pass"]
    assert two_pass["enabled"] is True
    assert two_pass["explicit_charge_task_count"] == 1

    state.update(nodes.collision_avoidance_node(state))

    assert state["final_status"] == "ROUTES_READY"
    assert state["errors"] == []
    assert state["schedule_validation"]["valid"] is True
    assert state["route_energy_reconciliation"]["unsafe_robot_ids"] == []
    robot_energy = state["route_energy_reconciliation"]["robots"]["R2-03"]
    assert robot_energy["status"] == "PASS"
    assert robot_energy["projected_final_battery"] >= 20.0
    charge_rows = [
        row
        for row in state["cuopt_plan"]["scheduled_tasks"]
        if row["robot_id"] == "R2-03" and row["action"] == "CHARGE"
    ]
    assert len(charge_rows) == 1


def test_low_battery_charge_retention_runs_one_local_safety_recovery() -> None:
    problem = _actual_gate2_2_problem()
    unsafe_plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=C_PICK,
                work_id=C_WORK,
                action="PICK",
                robot_id="R2-03",
                source_node=2088,
                target_node=2088,
                start_time_step=0,
                end_time_step=4,
                estimated_distance=10.93,
                estimated_energy=0.5465,
            ),
            ScheduledTask(
                task_id=C_DROP,
                work_id=C_WORK,
                action="DROP",
                robot_id="R2-03",
                source_node=2088,
                target_node=2146,
                start_time_step=4,
                end_time_step=11,
                estimated_distance=18.71,
                estimated_energy=0.9355,
            ),
        ],
        objective_value=0.0,
        metadata={},
    )
    outcome = OptimizationOutcome(
        plan=unsafe_plan,
        backend="cuopt",
        warnings=[],
        optimization_evidence=[],
        objective_breakdown=None,
        execution={"attempts": [{"provider": "CUOPT_REST", "status": "SUCCESS"}]},
    )

    recovered, evidence = nodes._ensure_low_battery_charge_retention(
        outcome=outcome,
        recovery_problem=problem,
        settings=Settings(optimizer_backend="local", cuopt_auto_enable=False),
        charge_visit_contract={},
    )

    assert evidence["status"] == "RECOVERED"
    assert evidence["recovery_used"] is True
    actions = [
        task.action
        for task in recovered.plan.scheduled_tasks
        if task.robot_id == "R2-03"
    ]
    assert actions == ["CHARGE", "PICK", "DROP"]
    assert recovered.plan.unassigned_task_ids == []


def test_failed_event_replan_exposes_bounded_charge_debug() -> None:
    debug = _planning_failure_debug(
        {
            "optimizer_execution": {
                "charge_visit_two_pass": {"enabled": True},
                "low_battery_charge_retention": {"status": "RECOVERED"},
            },
            "cuopt_plan": {
                "scheduled_tasks": [
                    {
                        "task_id": "charge:1",
                        "work_id": C_WORK,
                        "robot_id": "R2-03",
                        "action": "CHARGE",
                        "source_node": 2080,
                        "target_node": 2152,
                        "start_time_step": 0,
                        "end_time_step": 143,
                        "charge_target_battery": 80,
                        "charged_percent": 59.03,
                    }
                ],
                "metadata": {
                    "cuopt_assignment_application": {
                        "changeable_robot_bound_task_ids": [C_PICK, C_DROP]
                    }
                },
            },
            "route_energy_reconciliation": {
                "unsafe_robot_ids": ["R2-03"]
            },
            "schedule_validation": {"valid": False},
            "trace": [{"node": f"n{i}"} for i in range(20)],
        }
    )

    assert debug["charge_visit_two_pass"] == {"enabled": True}
    assert debug["low_battery_charge_retention"] == {"status": "RECOVERED"}
    assert debug["scheduled_charge_tasks"][0]["target_node"] == 2152
    assert debug["changeable_robot_bound_task_ids"] == [C_PICK, C_DROP]
    assert debug["route_energy_reconciliation"]["unsafe_robot_ids"] == ["R2-03"]
    assert len(debug["trace_tail"]) == 12

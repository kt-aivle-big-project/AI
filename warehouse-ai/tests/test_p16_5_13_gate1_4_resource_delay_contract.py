from __future__ import annotations

from app.models import CuOptPlan, ScheduledTask
from app.planning.nodes import build_verification_evidence
from app.services.charge_visit_optimization import (
    prepare_charge_visit_optimization_problem,
)
from app.services.local_optimizer import LocalOptimizer
from app.services.shared_resources import schedule_shared_resources
from tests.test_p16_5_13_gate1_2_activation_charge_fallback import (
    _low_battery_problem,
)


def _optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20.0,
        energy_per_distance=0.1,
        charge_target_battery=80.0,
        charge_rate_percent_per_minute=60.0,
        battery_safety_margin_percent=0.5,
    )


def test_first_pass_charge_end_is_not_a_hard_window_without_user_deadline() -> None:
    problem = _low_battery_problem()
    for task in problem["tasks"]:
        task["earliest_start"] = "2026-07-24T06:35:01.532678+00:00"
        task["latest_finish"] = None
        task["time_constraint_type"] = "ASAP"

    first_plan = _optimizer().optimize(problem)
    enriched, contract = prepare_charge_visit_optimization_problem(problem, first_plan)

    charge_ids = set(contract["explicit_charge_task_ids"])
    charge_rows = [row for row in enriched["tasks"] if row["task_id"] in charge_ids]
    assert charge_rows
    assert all(row["latest_finish"] is None for row in charge_rows)
    assert all(row["deadline"] is None for row in charge_rows)
    assert all(row["time_constraint_type"] == "ASAP" for row in charge_rows)
    assert all(
        contract["charge_task_specs"][row["task_id"]]["optimization_window_end_at"]
        for row in charge_rows
    )
    assert all(
        contract["charge_task_specs"][row["task_id"]]["hard_latest_finish_at"]
        is None
        for row in charge_rows
    )


def test_shared_resource_delay_propagates_through_charge_business_chain() -> None:
    problem = {
        "reference_time": "2026-07-26T23:00:00+00:00",
        "time_step_seconds": 5,
        "weights": {"makespan": 1.0},
        "nodes": [
            {"node_id": 2, "node_type": "CHARGER", "charger_capacity": 1},
            {"node_id": 3, "node_type": "STORAGE", "service_capacity": 1},
            {"node_id": 4, "node_type": "OUTBOUND", "service_capacity": 1},
        ],
        "tasks": [
            {
                "task_id": "A:charge",
                "time_constraint_type": "ASAP",
                "latest_finish": None,
                "frozen": False,
            },
            {
                "task_id": "B:charge",
                "time_constraint_type": "ASAP",
                "latest_finish": None,
                "frozen": False,
            },
            {"task_id": "B:move", "time_constraint_type": "ASAP", "frozen": False},
            {"task_id": "B:pick", "time_constraint_type": "ASAP", "frozen": False},
            {"task_id": "B:drop", "time_constraint_type": "ASAP", "frozen": False},
        ],
    }
    dependencies = [
        {
            "predecessor_task_id": "B:charge",
            "successor_task_id": "B:move",
            "dependency_type": "FINISH_TO_START",
            "lag_seconds": 0,
        },
        {
            "predecessor_task_id": "B:move",
            "successor_task_id": "B:pick",
            "dependency_type": "FINISH_TO_START",
            "lag_seconds": 0,
        },
        {
            "predecessor_task_id": "B:pick",
            "successor_task_id": "B:drop",
            "dependency_type": "FINISH_TO_START",
            "lag_seconds": 0,
        },
    ]
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="A:charge",
                work_id="A",
                action="CHARGE",
                robot_id="R1",
                source_node=2,
                target_node=2,
                start_time_step=0,
                end_time_step=7,
                priority=1,
                charge_duration_seconds=35,
            ),
            ScheduledTask(
                task_id="B:charge",
                work_id="B",
                action="CHARGE",
                robot_id="R2",
                source_node=2,
                target_node=2,
                start_time_step=0,
                end_time_step=146,
                priority=3,
                charge_duration_seconds=715,
            ),
            ScheduledTask(
                task_id="B:move",
                work_id="B",
                action="MOVE",
                robot_id="R2",
                source_node=2,
                target_node=3,
                start_time_step=146,
                end_time_step=152,
                priority=4,
            ),
            ScheduledTask(
                task_id="B:pick",
                work_id="B",
                action="PICK",
                robot_id="R2",
                source_node=3,
                target_node=3,
                start_time_step=152,
                end_time_step=153,
                priority=5,
            ),
            ScheduledTask(
                task_id="B:drop",
                work_id="B",
                action="DROP",
                robot_id="R2",
                source_node=3,
                target_node=4,
                start_time_step=153,
                end_time_step=160,
                priority=6,
            ),
        ],
        objective_value=0,
        metadata={"execution_task_dependencies": dependencies},
    )

    updated, result = schedule_shared_resources(problem, plan)
    by_id = {task.task_id: task for task in updated.scheduled_tasks}

    assert result["valid"] is True
    assert result["errors"] == []
    assert by_id["B:charge"].end_time_step == 150
    assert (by_id["B:move"].start_time_step, by_id["B:move"].end_time_step) == (
        150,
        156,
    )
    assert (by_id["B:pick"].start_time_step, by_id["B:pick"].end_time_step) == (
        156,
        157,
    )
    assert (by_id["B:drop"].start_time_step, by_id["B:drop"].end_time_step) == (
        157,
        164,
    )
    assert any(
        row["task_id"] == "B:charge"
        and row["reason"] == "SHARED_RESOURCE_CAPACITY"
        and row["delay_steps"] == 4
        for row in result["adjustments"]
    )


def test_route_failure_does_not_create_false_battery_override_failure() -> None:
    base_state = {
        "validation": {"errors": [], "warnings": []},
        "route_failure": {
            "code": "MAPF_LOCAL_CONFLICT",
            "reason": "RESOURCE_DELAY_CONFLICT",
            "affected_robot_ids": ["R2-02"],
            "affected_task_ids": ["W:charge"],
            "affected_node_ids": [2],
        },
        "simulation": {"success": False, "valid": False, "metrics": {}},
        "interpretation": {
            "command_kind": "PLAN",
            "inventory_operations": [],
            "hypothetical_events": [
                {
                    "event_type": "LOW_BATTERY",
                    "target_ids": ["R2-02"],
                    "parameters": {"battery_percent": 21.0},
                }
            ],
        },
        "optimization_problem": {
            "robots": [{"robot_id": "R2-02", "battery": 21.0}],
            "min_robot_battery": 20.0,
        },
        "cuopt_plan": {"scheduled_tasks": []},
    }

    evidence = build_verification_evidence(base_state)
    codes = {row["code"] for row in evidence}
    assert "MAPF_LOCAL_CONFLICT" in codes
    assert "ROBOT_STATE_OVERRIDE_NOT_APPLIED" not in codes

    mismatch_state = dict(base_state)
    mismatch_state["optimization_problem"] = {
        "robots": [{"robot_id": "R2-02", "battery": 50.0}],
        "min_robot_battery": 20.0,
    }
    mismatch_evidence = build_verification_evidence(mismatch_state)
    mismatch_codes = {row["code"] for row in mismatch_evidence}
    assert "ROBOT_STATE_OVERRIDE_NOT_APPLIED" in mismatch_codes

from __future__ import annotations

from types import SimpleNamespace

from app.models import CuOptPlan, ScheduledTask
from app.services.charge_visit_optimization import (
    charge_visit_robot_binding_errors,
    prepare_charge_visit_optimization_problem,
)
from app.services.local_optimizer import LocalOptimizer
from app.services.opportunity_charging import augment_plan_with_opportunity_charging
from app.services.optimizer import (
    OptimizationOutcome,
    validate_or_fallback_charge_visit_second_pass,
)
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.shared_resources import finalize_idle_resource_reservations
from tests.test_p16_5_6_idle_holding_routing import _warehouse_two_problem


def _low_battery_problem() -> dict:
    return {
        "reference_time": "2026-07-26T23:00:00+00:00",
        "time_step_seconds": 1,
        "plan_mode": "INITIAL_PLAN",
        "min_robot_battery": 20.0,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.1,
        "charge_target_battery": 80.0,
        "charge_rate_percent_per_minute": 60.0,
        "opportunity_charging_enabled": True,
        "hard_constraints": ["OPPORTUNITY_CHARGING"],
        "weights": {},
        "robots": [
            {
                "robot_id": "R2-02",
                "node_id": 1,
                "battery": 21,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            }
        ],
        "nodes": [
            {"node_id": 1, "node_type": "AISLE", "active": True},
            {
                "node_id": 2,
                "node_type": "CHARGER",
                "active": True,
                "charging_cost": 1,
            },
            {"node_id": 3, "node_type": "STORAGE", "active": True},
            {"node_id": 2146, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
                "active": True,
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 4,
                "travel_seconds": 4,
                "direction": "BOTH",
                "active": True,
            },
            {
                "from_node": 1,
                "to_node": 3,
                "distance": 10,
                "travel_seconds": 10,
                "direction": "BOTH",
                "active": True,
            },
            {
                "from_node": 3,
                "to_node": 2146,
                "distance": 6,
                "travel_seconds": 6,
                "direction": "BOTH",
                "active": True,
            },
        ],
        "temporary_closures": [],
        "tasks": [
            {
                "task_id": "W:pick",
                "work_id": "W",
                "action": "PICK",
                "item_id": "C",
                "quantity": 10,
                "source_candidates": [3],
                "target_candidates": [3],
                "priority": 50,
                "predecessors": [],
                "same_robot_group": "W",
                "assigned_robot_id": "R2-02",
                "inventory_allocations": [],
            },
            {
                "task_id": "W:drop",
                "work_id": "W",
                "action": "DROP",
                "item_id": "C",
                "quantity": 10,
                "source_candidates": [3],
                "target_candidates": [2146],
                "priority": 50,
                "predecessors": ["W:pick"],
                "same_robot_group": "W",
                "assigned_robot_id": "R2-02",
                "inventory_allocations": [],
            },
        ],
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        time_step_seconds=1,
        min_robot_battery=20.0,
        energy_per_distance=0.1,
        charge_target_battery=80.0,
        charge_rate_percent_per_minute=60.0,
        battery_safety_margin_percent=0.5,
    )


def _local_optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20.0,
        energy_per_distance=0.1,
        charge_target_battery=80.0,
        charge_rate_percent_per_minute=60.0,
        battery_safety_margin_percent=0.5,
    )


def test_far_future_initial_gap_is_outside_activation_reservations() -> None:
    problem = _warehouse_two_problem()
    problem.update(
        {
            "opportunity_charging_enabled": True,
            "hard_constraints": [
                "OPPORTUNITY_CHARGING",
                "IDLE_ONLY_ON_WHITELISTED_NODE",
            ],
            "idle_whitelist_strict": True,
            "idle_relocation_min_gap_steps": 12,
            "max_mapf_time_steps": 720,
            "defer_initial_pre_activation": True,
        }
    )
    baseline = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="future:R2-01:pick",
                work_id="W-FUTURE",
                action="PICK",
                robot_id="R2-01",
                source_node=2088,
                target_node=2088,
                start_time_step=9000,
                end_time_step=9001,
                priority=5,
                estimated_distance=18.71,
                estimated_energy=0.9355,
            )
        ],
        objective_value=0,
        metadata={"execution_task_dependencies": []},
    )

    augmented, evidence = augment_plan_with_opportunity_charging(problem, baseline)
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(augmented)
    resources = finalize_idle_resource_reservations(
        problem,
        collision,
        {
            "valid": True,
            "status": "PASS",
            "errors": [],
            "warnings": [],
            "reservations": [],
        },
    )

    assert evidence["inserted_charge_task_count"] == 0
    assert evidence["decisions"][0]["selected_action"] == (
        "DEFER_UNTIL_PLAN_ACTIVATION"
    )
    assert collision.metadata["idle_action_tasks"] == []
    assert collision.metadata["idle_relocations"] == []
    assert resources["valid"] is True
    assert not any(
        "MAXIMUM_IDLE_DURATION_EXCEEDED" in error
        for error in resources["errors"]
    )


def test_invalid_second_pass_recovers_with_cpu_and_preserves_chain() -> None:
    problem = _low_battery_problem()
    first_plan = _local_optimizer().optimize(problem)
    enriched, contract = prepare_charge_visit_optimization_problem(
        problem,
        first_plan,
    )
    charge_id = contract["explicit_charge_task_ids"][0]
    charge_task = next(
        task for task in first_plan.scheduled_tasks if task.task_id == charge_id
    )
    invalid_outcome = OptimizationOutcome(
        plan=CuOptPlan(
            scheduled_tasks=[charge_task],
            objective_value=0,
            metadata={},
        ),
        backend="cuopt",
        warnings=[],
        optimization_evidence=[],
        objective_breakdown=None,
        execution={
            "requested_provider": "CUOPT",
            "used_provider": "CUOPT",
            "fallback_used": False,
            "attempts": [{"provider": "CUOPT_REST", "status": "SUCCESS"}],
        },
    )

    recovered, fallback_used = validate_or_fallback_charge_visit_second_pass(
        enriched,
        _settings(),
        invalid_outcome,
        contract,
    )

    assert fallback_used is True
    assert recovered.execution["used_provider"] == "CPU"
    assert recovered.execution["fallback_used"] is True
    assert recovered.plan.unassigned_task_ids == []
    assert charge_visit_robot_binding_errors(recovered.plan, contract) == []
    task_ids = {task.task_id for task in recovered.plan.scheduled_tasks}
    assert {"W:pick", "W:drop"}.issubset(task_ids)
    assert set(contract["explicit_charge_task_ids"]).issubset(task_ids)
    assert set(contract["explicit_relocation_task_ids"]).issubset(task_ids)


def test_past_inventory_availability_does_not_expire_post_charge_relocation() -> None:
    problem = _low_battery_problem()
    # Real Swagger snapshots may propagate an old lot available_at value into
    # the business task's earliest_start.  That lower availability bound must
    # not become MOVE_TO_NEXT's latest_finish after a future planning reference.
    for task in problem["tasks"]:
        task["earliest_start"] = "2026-07-24T06:35:01.532678+00:00"

    first_plan = _local_optimizer().optimize(problem)
    enriched, contract = prepare_charge_visit_optimization_problem(
        problem,
        first_plan,
    )

    relocation_ids = set(contract["explicit_relocation_task_ids"])
    assert relocation_ids
    relocation_rows = [
        row for row in enriched["tasks"] if row["task_id"] in relocation_ids
    ]
    assert relocation_rows
    assert all(row["latest_finish"] is None for row in relocation_rows)
    assert all(row["time_constraint_type"] == "ASAP" for row in relocation_rows)

    # Force the managed second-pass contract failure seen in Swagger, then
    # verify the bounded CPU fallback preserves CHARGE -> MOVE -> PICK -> DROP.
    charge_id = contract["explicit_charge_task_ids"][0]
    charge_task = next(
        task for task in first_plan.scheduled_tasks if task.task_id == charge_id
    )
    invalid_outcome = OptimizationOutcome(
        plan=CuOptPlan(
            scheduled_tasks=[charge_task],
            objective_value=0,
            metadata={},
        ),
        backend="cuopt",
        warnings=[],
        optimization_evidence=[],
        objective_breakdown=None,
        execution={
            "requested_provider": "CUOPT",
            "used_provider": "CUOPT",
            "fallback_used": False,
            "attempts": [{"provider": "CUOPT_REST", "status": "SUCCESS"}],
        },
    )

    recovered, fallback_used = validate_or_fallback_charge_visit_second_pass(
        enriched,
        _settings(),
        invalid_outcome,
        contract,
    )

    assert fallback_used is True
    assert recovered.execution["used_provider"] == "CPU"
    assert recovered.plan.unassigned_task_ids == []
    assert charge_visit_robot_binding_errors(recovered.plan, contract) == []

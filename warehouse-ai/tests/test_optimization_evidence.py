from __future__ import annotations

import math

from app.models import AtomicTask
from tests.test_local_optimizer import base_problem, optimizer


def test_records_one_robot_candidate_per_task() -> None:
    service = optimizer()
    plan = service.optimize(base_problem())

    assert not plan.unassigned_task_ids
    assert len(service.last_optimization_evidence) == 3
    assert sum(row.candidate_count for row in service.last_optimization_evidence) == 9
    assert all(row.candidate_count == 3 for row in service.last_optimization_evidence)
    assert all(sum(candidate.selected for candidate in row.candidates) == 1 for row in service.last_optimization_evidence)


def test_selected_evidence_matches_the_unchanged_schedule() -> None:
    service = optimizer()
    plan = service.optimize(base_problem())
    scheduled = {row.task_id: row for row in plan.scheduled_tasks}

    for evidence in service.last_optimization_evidence:
        selected = next(row for row in evidence.candidates if row.selected)
        actual = scheduled[evidence.task_id]
        assert selected.robot_id == actual.robot_id
        assert selected.source_node == actual.source_node
        assert selected.target_node == actual.target_node
        assert selected.distance == actual.estimated_distance


def test_infeasible_robot_uses_observed_status_reason() -> None:
    problem = base_problem()
    problem["robots"][0]["live_status"] = "ROBOT_FAILED"
    service = optimizer()

    service.optimize(problem)

    for task in service.last_optimization_evidence:
        failed = next(row for row in task.candidates if row.robot_id == "R1")
        assert not failed.feasible
        assert not failed.selected
        assert failed.rejection_reason == "ROBOT_LIVE_STATUS_UNAVAILABLE"


def test_deterministic_tie_break_uses_robot_id_then_nodes() -> None:
    problem = base_problem()
    problem["robots"] = [
        {**problem["robots"][0], "robot_id": "R2", "node_id": 1},
        {**problem["robots"][1], "robot_id": "R1", "node_id": 1, "battery": 90},
    ]
    problem["tasks"] = [
        AtomicTask(
            task_id="T-TIE",
            action="MOVE",
            source_candidates=[1],
            target_candidates=[3],
            priority=1,
        ).model_dump(mode="json")
    ]
    service = optimizer()

    plan = service.optimize(problem)

    assert plan.scheduled_tasks[0].robot_id == "R1"
    evidence = service.last_optimization_evidence[0]
    assert evidence.selected_robot_id == "R1"
    assert evidence.tie_break_rule == [
        "incremental_objective",
        "robot_id",
        "source_node",
        "target_node",
    ]
    selected = next(row for row in evidence.candidates if row.selected)
    assert selected.robot_activation_cost is not None
    assert selected.plan_change_cost is not None
    rejected = next(row for row in evidence.candidates if row.robot_id == "R2")
    assert rejected.rejection_reason == "HIGHER_OBJECTIVE_OR_TIE_BREAK_KEY"


def test_lower_priority_number_is_processed_first() -> None:
    problem = base_problem()
    problem["tasks"] = [problem["tasks"][2], problem["tasks"][0], problem["tasks"][1]]
    service = optimizer()

    service.optimize(problem)

    assert [row.priority for row in service.last_optimization_evidence] == [1, 2, 3]
    assert [row.task_order for row in service.last_optimization_evidence] == [1, 2, 3]


def test_objective_components_sum_to_plan_objective() -> None:
    service = optimizer()
    plan = service.optimize(base_problem())
    breakdown = service.last_objective_breakdown

    assert breakdown is not None
    component_total = sum(
        (
            breakdown.distance_component,
            breakdown.makespan_component,
            breakdown.tardiness_component,
            breakdown.energy_component,
            breakdown.robot_activation_component,
            breakdown.plan_change_component,
        )
    )
    assert math.isclose(component_total, plan.objective_value, abs_tol=1e-6)
    assert math.isclose(breakdown.total, plan.objective_value, abs_tol=1e-6)

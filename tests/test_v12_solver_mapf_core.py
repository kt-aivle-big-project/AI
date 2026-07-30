"""Pure-service tests for LARO v12 multi-task routing and MAPF contracts."""
from __future__ import annotations

import importlib.util
from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.schemas import MissionIntent, OperationIntent, OptimizerResult, PlanningRouteResolution
from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.optimization_service import (
    ORToolsRoutingOptimizer,
    OptimizerAssignmentValidator,
)
from app.services.physical_problem_service import PhysicalProblemProfiler, PlanningRouteResolver
from app.services.route_service import StaticRouteValidator
from scripts.v12_solver_mapf_support import (
    build_baseline_and_reference,
    build_fixture_problem,
    max_wait_ms,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scenarios" / "fixtures" / "V9_ten_orders_multitask"


@pytest.fixture(scope="module")
def problem():
    """Build the canonical ten-order fixture once."""

    return build_fixture_problem(FIXTURE)


@pytest.fixture(scope="module")
def comparison(problem):
    """Return the one-to-one and multi-task reference plans."""

    return build_baseline_and_reference(problem)


def test_fixture_contract(problem) -> None:
    """The v12 fixture must preserve the supplied warehouse size and batch size."""

    assert len(problem.payload.location_index_map) == 220
    assert len(problem.payload.waypoint_graph_data.edge_ids) == 356
    assert len(problem.payload.task_data.pickup_and_delivery_pairs) == 10
    assert len(problem.payload.task_data.task_ids) == 20
    assert len(problem.payload.fleet_data.vehicle_ids) == 5


def test_global_stock_allocation_does_not_fix_robots(problem) -> None:
    """Stock allocation selects pickup racks but leaves all new vehicle choices to the solver."""

    assert len(problem.policy.validated_tasks) == 10
    assert all(task.fixed_robot_id is None for task in problem.policy.validated_tasks)
    used_by_stock: dict[str, int] = defaultdict(int)
    available_by_stock = {
        candidate.stock_id: candidate.available_qty
        for candidate in problem.policy.fulfillment_candidates
    }
    for allocation in problem.policy.stock_allocations:
        used_by_stock[allocation.stock_id] += allocation.quantity
    assert all(used <= available_by_stock[stock_id] for stock_id, used in used_by_stock.items())


def test_candidate_space_and_payload_contract(problem) -> None:
    """Every canonical task and vehicle must reach the solver payload unchanged."""

    task_ids = set(problem.payload.task_data.task_ids)
    assert task_ids == {
        value
        for task in problem.policy.validated_tasks
        for value in (f"{task.task_id}_PICK", f"{task.task_id}_DROP")
    }
    assert set(problem.payload.fleet_data.vehicle_ids) == {
        robot.robot_id for robot in problem.policy.candidate_robots
    }
    assert all(value is None for value in problem.payload.task_data.fixed_vehicle_ids)


def test_physical_guard_forces_global_solver(problem) -> None:
    """Ten tasks and five robots must override an LLM RULE recommendation."""

    profile, *_ = PhysicalProblemProfiler().profile(
        payload=problem.payload,
        map_context=problem.map_context,
        node_types=problem.node_types,
    )
    assert profile.force_global_solver
    assert profile.baseline_deferred_count == 5
    assert "TASK_COUNT_EXCEEDS_AVAILABLE_ROBOTS" in profile.force_reasons
    intent = MissionIntent(
        intent_type="MISSION",
        planning_route="RULE",
        mission_goal="Process the batch.",
        operations=[
            OperationIntent(
                operation_type="FULFILL_OUTBOUND_ORDER",
                target_id="ORD-001",
                reason="Process one order.",
            )
        ],
    )
    resolution: PlanningRouteResolution = PlanningRouteResolver().resolve(
        profile=profile,
        intent=intent,
        optimization_backend="ortools",
    )
    assert resolution.llm_recommended_route == "RULE"
    assert resolution.resolved_route == "GLOBAL_SOLVER"
    assert resolution.override_reasons


def test_one_to_one_baseline_exposes_structural_deferral(comparison) -> None:
    """The one-task-per-robot baseline can cover only five of ten pairs."""

    result = comparison["baseline_result"]
    schedule = comparison["baseline_schedule"]
    assert len(result.routes) == 5
    assert len(result.unassigned_task_ids) == 10
    # The station/handoff topology no longer makes the five selected routes
    # physically invalid.  The structural limitation is still visible in the
    # ten unassigned task rows: one-to-one planning cannot cover the full wave.
    assert schedule.valid
    assert not schedule.conflicts
    assert schedule.total_wait_ms == 16_253
    assert schedule.makespan_ms == 50_406


def test_reference_multitask_sequence_covers_all_tasks(problem, comparison) -> None:
    """The reference multi-task plan must pass assignment, route, and MAPF validation."""

    result = comparison["reference_result"]
    assert any(len(route.task_sequence) > 2 for route in result.routes)
    assert not result.unassigned_task_ids
    assert comparison["reference_assignment_validation"].valid
    assert comparison["reference_route_validation"].valid
    assert comparison["reference_mapf_validation"].valid
    assert {task for route in result.routes for task in route.task_sequence} == set(
        problem.payload.task_data.task_ids
    )


def test_reference_multitask_produces_a_complete_timed_plan(comparison) -> None:
    """The rack-access reference must complete all work with finite timing."""

    reference = comparison["reference_schedule"]
    assert reference.valid
    assert reference.total_wait_ms == 1_831
    assert reference.makespan_ms == 39_687
    assert max_wait_ms(reference) == 1_831
    assert reference.total_service_ms > 0


def test_mapf_validator_is_independent(problem, comparison) -> None:
    """Revalidate the produced time plan without relying on the planner's valid flag."""

    validation = MAPFPlanValidator().validate(
        schedule=comparison["reference_schedule"],
        map_context=problem.map_context,
        node_types=problem.node_types,
        max_edge_wait_ms=problem.mission.max_edge_wait_ms,
    )
    assert validation.valid
    route_validation = StaticRouteValidator().validate(
        payload=problem.payload,
        expansion=comparison["reference_expansion"],
    )
    assert route_validation.valid


def test_llm_intent_schema_forbids_physical_assignment() -> None:
    """The LLM contract must reject direct robot/rack/path assignment fields."""

    with pytest.raises(ValidationError):
        MissionIntent.model_validate(
            {
                "intent_type": "MISSION",
                "planning_route": "GLOBAL_SOLVER",
                "mission_goal": "Process the batch.",
                "operations": [],
                "selected_robot_id": "R002",
            }
        )


def test_ortools_backend_is_explicit_and_never_falls_back(problem) -> None:
    """Run OR-Tools when installed; otherwise require an explicit unavailable result."""

    result: OptimizerResult = ORToolsRoutingOptimizer().solve(problem.payload)
    if importlib.util.find_spec("ortools") is None:
        assert result.status == "unavailable"
        assert result.optimizer == "ortools-routing"
        assert "not installed" in (result.reason or "")
        return
    assert result.status == "success"
    validation = OptimizerAssignmentValidator().validate(payload=problem.payload, result=result)
    assert validation.valid
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=problem.payload,
        result=result,
        map_context=problem.map_context,
        node_types=problem.node_types,
    )
    assert StaticRouteValidator().validate(payload=problem.payload, expansion=expansion).valid
    assert MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=problem.map_context,
        node_types=problem.node_types,
        max_edge_wait_ms=problem.mission.max_edge_wait_ms,
    ).valid

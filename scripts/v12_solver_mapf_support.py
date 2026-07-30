"""Shared helpers for the deterministic v9 multi-task/MAPF probe.

The helpers use the real JSON repository, policy, stock allocator, payload
builder, assignment validator, and prioritized SIPP-style MAPF planner.  The
reference multi-task assignment is intentionally explicit: it demonstrates
that the downstream contracts can execute a multi-task plan even when the
optional OR-Tools native dependency is unavailable in the current environment.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.schemas import (
    ContextSnapshot,
    CuOptPayload,
    EventInput,
    MapContext,
    MissionSpec,
    OptimizerResult,
    OptimizerRoute,
    PolicyValidationResult,
    RobotRuntimeContext,
    TrafficScheduleResult,
)
from app.policies.mission_compiler import compile_structured_events
from app.policies.mission_policy import MissionPolicyService
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.context_service import WarehouseContextService
from app.services.inventory_allocation_service import GlobalInventoryAllocator
from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.optimization_service import (
    CandidateSpaceGuard,
    CuOptPayloadBuilder,
    CuOptPayloadValidator,
    OneToOneRuleOptimizer,
    OptimizerAssignmentValidator,
    build_optimization_request,
)
from app.services.route_service import StaticRouteValidator


@dataclass(frozen=True)
class V9FixtureProblem:
    """All canonical objects required by v9 solver and MAPF probes."""

    fixture_dir: Path
    repository: JsonWarehouseRepository
    mission: MissionSpec
    policy: PolicyValidationResult
    payload: CuOptPayload
    map_context: MapContext
    robot_context: RobotRuntimeContext
    node_types: dict[str, str]


def build_fixture_problem(fixture_dir: Path) -> V9FixtureProblem:
    """Build a canonical ten-order problem from one immutable fixture."""

    repository = JsonWarehouseRepository(fixture_dir)
    context_service = WarehouseContextService(repository)
    order_ids = sorted(repository.orders)
    events = [EventInput(type="new_order", order_id=order_id) for order_id in order_ids]
    inventory = context_service.build_inventory_context(order_ids=order_ids)
    map_bundle = context_service.build_map_context(inventory=inventory)
    robots = context_service.build_robot_context(required_capacity=1)
    mission = compile_structured_events(
        events=events,
        inventory=inventory,
        map_context=map_bundle.context,
    )
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V9-TEN-ORDER-PROBE",
        captured_at=str(repository.scenario.get("captured_at", "2026-07-21T09:00:00Z")),
        graph_version=repository.versions["graph_version"],
        inventory_version=repository.versions["inventory_version"],
        runtime_version=repository.versions["runtime_version"],
    )
    policy = MissionPolicyService().validate(
        mission=mission,
        snapshot=snapshot,
        inventory=inventory,
        map_context=map_bundle.context,
        robots=robots,
        graph_arcs=map_bundle.graph_arcs,
        events=events,
    )
    if policy.status != "pass":
        raise RuntimeError(f"Fixture policy did not pass: {policy.model_dump(mode='json')}")
    allocated = GlobalInventoryAllocator().allocate(
        mission=mission,
        policy=policy,
        graph_arcs=map_bundle.graph_arcs,
    )
    if allocated.status != "pass":
        raise RuntimeError(f"Fixture allocation did not pass: {allocated.model_dump(mode='json')}")
    request = build_optimization_request(allocated, mission)
    payload = CuOptPayloadBuilder().build(
        request=request,
        graph_nodes=map_bundle.graph_nodes,
        graph_arcs=map_bundle.graph_arcs,
        time_limit_seconds=5,
    )
    payload_validation = CuOptPayloadValidator().validate(payload)
    if not payload_validation.valid:
        raise RuntimeError(f"Fixture payload invalid: {payload_validation.errors}")
    candidate_validation = CandidateSpaceGuard().validate(request=request, payload=payload)
    if not candidate_validation.valid:
        raise RuntimeError(f"Fixture candidate space invalid: {candidate_validation.errors}")
    return V9FixtureProblem(
        fixture_dir=fixture_dir,
        repository=repository,
        mission=mission,
        policy=allocated,
        payload=payload,
        map_context=map_bundle.context,
        robot_context=robots,
        node_types=map_bundle.graph_node_types,
    )


def reference_multitask_result(payload: CuOptPayload) -> OptimizerResult:
    """Return a known-feasible reference for the rack-access topology.

    Rack entities are no longer through-routing nodes.  The reference therefore
    consolidates the ten pickup-delivery pairs onto two capable robots, visits
    the two service-only access spurs before entering the outbound terminal
    chain, and covers every task row exactly once.  It is a deterministic
    contract fixture, not an optimality claim.
    """

    sequence_by_robot = {
        "R002": [
            "TASK-ORD-008_PICK",
            "TASK-ORD-005_PICK",
            "TASK-ORD-010_PICK",
            "TASK-ORD-009_PICK",
            "TASK-ORD-001_PICK",
            "TASK-ORD-001_DROP",
            "TASK-ORD-005_DROP",
            "TASK-ORD-009_DROP",
            "TASK-ORD-010_DROP",
            "TASK-ORD-008_DROP",
        ],
        "R005": [
            "TASK-ORD-006_PICK",
            "TASK-ORD-002_PICK",
            "TASK-ORD-004_PICK",
            "TASK-ORD-007_PICK",
            "TASK-ORD-003_PICK",
            "TASK-ORD-002_DROP",
            "TASK-ORD-006_DROP",
            "TASK-ORD-003_DROP",
            "TASK-ORD-007_DROP",
            "TASK-ORD-004_DROP",
        ],
    }
    available_tasks = set(payload.task_data.task_ids)
    covered = {task_id for sequence in sequence_by_robot.values() for task_id in sequence}
    if covered != available_tasks:
        raise RuntimeError(
            "Reference sequence must cover every task row exactly once: "
            f"missing={sorted(available_tasks - covered)} extra={sorted(covered - available_tasks)}"
        )
    return OptimizerResult(
        backend="rule",
        status="success",
        optimizer="rack-access-reference-multitask-sequence",
        routes=[
            OptimizerRoute(vehicle_id=robot_id, task_sequence=sequence)
            for robot_id, sequence in sequence_by_robot.items()
        ],
        unassigned_task_ids=[],
        reason=(
            "Deterministic two-robot sequence for rack-access route/MAPF contract validation; "
            "not an OR-Tools optimum."
        ),
    )


def build_baseline_and_reference(problem: V9FixtureProblem) -> dict[str, object]:
    """Run the one-to-one baseline and reference multi-task MAPF plans."""

    baseline_result = OneToOneRuleOptimizer().solve(problem.payload, allow_partial=True)
    baseline_expansion, baseline_schedule = PrioritizedSIPPPlanner().plan(
        payload=problem.payload,
        result=baseline_result,
        map_context=problem.map_context,
        node_types=problem.node_types,
    )
    reference_result = reference_multitask_result(problem.payload)
    reference_assignment_validation = OptimizerAssignmentValidator().validate(
        payload=problem.payload,
        result=reference_result,
    )
    reference_expansion, reference_schedule = PrioritizedSIPPPlanner().plan(
        payload=problem.payload,
        result=reference_result,
        map_context=problem.map_context,
        node_types=problem.node_types,
    )
    reference_route_validation = StaticRouteValidator().validate(
        payload=problem.payload,
        expansion=reference_expansion,
    )
    reference_mapf_validation = MAPFPlanValidator().validate(
        schedule=reference_schedule,
        map_context=problem.map_context,
        node_types=problem.node_types,
        max_edge_wait_ms=problem.mission.max_edge_wait_ms,
        payload=problem.payload,
    )
    return {
        "baseline_result": baseline_result,
        "baseline_expansion": baseline_expansion,
        "baseline_schedule": baseline_schedule,
        "reference_result": reference_result,
        "reference_assignment_validation": reference_assignment_validation,
        "reference_expansion": reference_expansion,
        "reference_schedule": reference_schedule,
        "reference_route_validation": reference_route_validation,
        "reference_mapf_validation": reference_mapf_validation,
    }


def max_wait_ms(schedule: TrafficScheduleResult) -> int:
    """Return the largest total WAIT accumulated by any one robot."""

    return max(
        (
            sum(
                step.end_at_ms - step.start_at_ms
                for step in route.steps
                if step.step_type == "WAIT"
            )
            for route in schedule.routes
        ),
        default=0,
    )


def max_single_wait_step_ms(schedule: TrafficScheduleResult) -> int:
    """Return the largest individual WAIT interval in the schedule."""

    return max(
        (
            step.end_at_ms - step.start_at_ms
            for route in schedule.routes
            for step in route.steps
            if step.step_type == "WAIT"
        ),
        default=0,
    )

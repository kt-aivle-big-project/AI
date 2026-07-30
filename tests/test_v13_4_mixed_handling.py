"""v13.4 mixed-batch feasibility, handling-time, and cuOpt parser coverage."""
from __future__ import annotations

from pathlib import Path

from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.optimization_service import (
    CuOptNativeRequestBuilder,
    CuOptNativeResponseParser,
    CuOptPayloadValidator,
    OptimizerAssignmentValidator,
)
from app.services.route_service import StaticRouteValidator
from scripts.run_v13_mixed_batch_scenario import (
    build_problem,
    build_reference_result,
    validate_reference_loads,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"


def _problem():
    return build_problem(FIXTURE)


def test_quantity_based_handling_times_are_explicit_and_sent_to_cuopt() -> None:
    _, payload, _, _, _ = _problem()
    service = dict(
        zip(payload.task_data.task_ids, payload.task_data.service_times_ms, strict=True)
    )
    assert service["ORD-001_PICK"] == 1500  # 1000 + 250 * 2
    assert service["ORD-001_DROP"] == 1400  # 1000 + 200 * 2
    assert service["ORD-003_PICK"] == 1750  # 1000 + 250 * 3
    assert service["IN-003_DROP"] == 1800  # 1000 + 200 * 4
    assert sum(service.values()) == 23_000

    native = CuOptNativeRequestBuilder().build(payload)
    assert native["task_data"]["service_times"] == payload.task_data.service_times_ms
    assert "priorities" not in native["task_data"]


def test_service_time_vector_shape_is_validated_before_solver_call() -> None:
    _, payload, _, _, _ = _problem()
    malformed = payload.model_copy(
        update={
            "task_data": payload.task_data.model_copy(
                update={"service_times_ms": [1000]}
            )
        }
    )
    result = CuOptPayloadValidator().validate(malformed)
    assert not result.valid
    assert any("Task arrays must have equal lengths" in value for value in result.errors)


def test_mixed_fixture_has_known_feasible_multi_task_mapf_plan() -> None:
    request, payload, map_context, node_types, metadata = _problem()
    reference = build_reference_result(metadata["reference_routes"])
    load_validation = validate_reference_loads(payload, reference)
    assignment = OptimizerAssignmentValidator().validate(payload=payload, result=reference)
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=payload,
        result=reference,
        map_context=map_context,
        node_types=node_types,
    )
    route = StaticRouteValidator().validate(payload=payload, expansion=expansion)
    mapf = MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=map_context,
        node_types=node_types,
        max_edge_wait_ms=request.max_edge_wait_ms,
        payload=payload,
    )

    assert load_validation["valid"], load_validation["errors"]
    assert assignment.valid, assignment.errors
    assert route.valid, route.errors
    assert mapf.valid, mapf.errors
    assert schedule.valid
    assert schedule.total_service_ms == 23_000
    assert sum(len(value.task_sequence) > 2 for value in reference.routes) >= 2

    expected = dict(
        zip(payload.task_data.task_ids, payload.task_data.service_times_ms, strict=True)
    )
    observed = {
        step.task_id: step.end_at_ms - step.start_at_ms
        for robot_route in schedule.routes
        for step in robot_route.steps
        if step.step_type == "SERVICE"
    }
    assert observed == expected


def test_cuopt_infeasible_response_is_not_reported_as_empty_success() -> None:
    _, payload, _, _, _ = _problem()
    raw = {
        "response": {
            "solver_infeasible_response": {
                "status": 1,
                "message": "No feasible vehicle routing solution.",
            }
        }
    }
    result = CuOptNativeResponseParser().parse(raw, payload)
    assert result.status == "infeasible"
    assert not result.routes
    assert result.errors == ["CUOPT_INFEASIBLE"]


def test_cuopt_empty_success_with_mandatory_tasks_is_rejected() -> None:
    _, payload, _, _, _ = _problem()
    raw = {
        "response": {
            "solver_response": {
                "status": 0,
                "vehicle_data": {},
                "dropped_tasks": {"task_id": []},
            }
        }
    }
    result = CuOptNativeResponseParser().parse(raw, payload)
    assert result.status == "infeasible"
    assert result.errors == ["EMPTY_OR_INCOMPLETE_SUCCESS_RESPONSE"]
    assert set(result.unassigned_task_ids) == set(payload.task_data.task_ids)


def test_reference_shaped_native_response_reaches_mapf_with_handling_times() -> None:
    """Simulate a full cuOpt response using the fixture's feasible route set."""

    request, payload, map_context, node_types, metadata = _problem()
    task_index = {
        task_id: index for index, task_id in enumerate(payload.task_data.task_ids)
    }
    vehicle_index = {
        vehicle_id: index
        for index, vehicle_id in enumerate(payload.fleet_data.vehicle_ids)
    }
    vehicle_data = {
        str(vehicle_index[value["vehicle_id"]]): {
            "task_id": [
                "Depot",
                *[str(task_index[task_id]) for task_id in value["task_sequence"]],
                "Depot",
            ],
            "route_cost": 1.0,
        }
        for value in metadata["reference_routes"]
    }
    raw = {
        "response": {
            "solver_response": {
                "status": 0,
                "vehicle_data": vehicle_data,
                "dropped_tasks": {"task_id": []},
            }
        }
    }
    result = CuOptNativeResponseParser().parse(raw, payload)
    assignment = OptimizerAssignmentValidator().validate(
        payload=payload,
        result=result,
    )
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=payload,
        result=result,
        map_context=map_context,
        node_types=node_types,
    )
    mapf = MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=map_context,
        node_types=node_types,
        max_edge_wait_ms=request.max_edge_wait_ms,
        payload=payload,
    )
    assert result.status == "success"
    assert assignment.valid, assignment.errors
    assert expansion.status == "expanded", expansion.errors
    assert mapf.valid, mapf.errors
    assert schedule.total_service_ms == 23_000

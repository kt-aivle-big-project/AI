"""Run the v13.5 feasible mixed inbound/outbound multi-robot scenario.

The scenario deliberately exercises the common solver contract rather than
hard-coding separate inbound and outbound optimizers.  Every operation is one
pickup-delivery pair:

* outbound: rack -> outbound station
* inbound: inbound source -> robot-accessible R3_0 handoff -> target rack

The source event remains tied to I_a/I_c/I_d, while the routing task begins at
R3_0 because the inbound conveyor is one-way and robots cannot drive into the
elevator line.  The fixture contains a test-only known-feasible assignment so
its global multi-task topology is validated before any external solver call.
OR-Tools and NVIDIA cuOpt still receive the same unconstrained seven-pair
robot-accessible problem.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.console import safe_json_print
from app.core.config import get_settings
from app.domain.schemas import (
    MapConstraints,
    OptimizationRequest,
    OptimizationTask,
    OptimizationVehicle,
    OptimizerResult,
    OptimizerRoute,
)
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.context_service import WarehouseContextService
from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.optimization_service import (
    CandidateSpaceGuard,
    CuOptNativeRequestBuilder,
    CuOptPayloadBuilder,
    CuOptPayloadValidator,
    ExternalCuOptGateway,
    ORToolsRoutingOptimizer,
    OptimizerAssignmentValidator,
)
from app.services.route_service import StaticRouteValidator
from app.services.graph_service import DirectedGraphService
from app.services.rack_access_service import choose_best_access_node



def validate_fixture_operations(fixture: Path, operations: list[dict]) -> dict:
    """Validate outbound stock and inbound target capacity against fixture data."""

    inventory = json.loads((fixture / "rack_inventory.json").read_text(encoding="utf-8"))
    scenario = json.loads((fixture / "scenario_state.json").read_text(encoding="utf-8"))
    rack_by_id = {str(value["rack_id"]): value for value in inventory.get("racks", [])}
    order_by_id = {str(value["order_id"]): value for value in scenario.get("orders", [])}
    errors: list[str] = []
    checks: list[dict] = []
    for operation in operations:
        operation_id = str(operation["operation_id"])
        operation_type = str(operation["operation_type"])
        demand = int(operation["demand"])
        if operation_type == "OUTBOUND":
            rack_id = str(operation.get("rack_id") or operation["pickup_node"])
            rack = rack_by_id.get(rack_id)
            order = order_by_id.get(operation_id)
            expected_item = str((order or {}).get("item_id", ""))
            matching_levels = [
                level
                for level in (rack or {}).get("levels", [])
                if str((level.get("item") or {}).get("item_id", "")) == expected_item
            ]
            available = sum(
                int((level.get("item") or {}).get("quantity", 0))
                for level in matching_levels
            )
            order_consistent = bool(order) and int(order.get("required_qty", -1)) == demand
            valid = (
                rack is not None
                and bool(expected_item)
                and bool(matching_levels)
                and available >= demand
                and order_consistent
            )
            checks.append({
                "operation_id": operation_id,
                "check": "OUTBOUND_STOCK_AVAILABLE",
                "rack_node": rack_id,
                "item_id": expected_item,
                "required": demand,
                "available": available,
                "order_consistent": order_consistent,
                "valid": valid,
            })
            if not valid:
                errors.append(
                    f"{operation_id}: outbound rack {rack_id} has {available} unit(s) "
                    f"of {expected_item or 'UNKNOWN_ITEM'}, requires {demand}; "
                    f"order_consistent={order_consistent}."
                )
        elif operation_type == "INBOUND":
            rack_id = str(operation.get("rack_id") or operation["delivery_node"])
            rack = rack_by_id.get(rack_id)
            free_by_level: list[int] = []
            for level in (rack or {}).get("levels", []):
                item = level.get("item")
                if item is None:
                    free_by_level.append(demand)
                else:
                    free_by_level.append(max(0, int(item.get("capacity", 0)) - int(item.get("quantity", 0))))
            free_capacity = max(free_by_level, default=0)
            valid = rack is not None and free_capacity >= demand
            checks.append({
                "operation_id": operation_id,
                "check": "INBOUND_TARGET_CAPACITY",
                "rack_node": rack_id,
                "required": demand,
                "free_capacity": free_capacity,
                "valid": valid,
            })
            if not valid:
                errors.append(f"{operation_id}: inbound target {rack_id} has free capacity {free_capacity}, requires {demand}.")
        else:
            errors.append(f"{operation_id}: unsupported operation_type={operation_type}.")
    return {"valid": not errors, "checks": checks, "errors": errors}

def build_problem(fixture: Path) -> tuple[OptimizationRequest, object, object, dict[str, str], dict]:
    """Build one seven-pair mixed operation problem from the fixture."""

    repository = JsonWarehouseRepository(fixture)
    context = WarehouseContextService(repository)
    map_bundle = context.build_map_context()
    robots = context.build_robot_context(required_capacity=1)
    raw_operations = list(repository.scenario.get("mixed_operations", []))
    if not raw_operations:
        raise RuntimeError("Fixture does not define mixed_operations.")
    robot_by_id = {value.robot_id: value for value in robots.robots}
    robot_start_nodes = {
        robot_id: robot_by_id[robot_id].current_node
        for robot_id in robots.candidate_robot_ids
        if robot_id in robot_by_id
    }
    directed = DirectedGraphService(map_bundle.graph_arcs)
    operations: list[dict] = []
    tasks: list[OptimizationTask] = []
    for raw in raw_operations:
        value = dict(raw)
        operation_type = str(value["operation_type"])
        if operation_type == "OUTBOUND":
            rack_id = str(value.get("rack_id") or value["pickup_node"])
            choice = choose_best_access_node(
                directed,
                rack_id=rack_id,
                access_node_ids=repository.rack_access_nodes(rack_id),
                robot_start_nodes=robot_start_nodes,
                delivery_node=str(value["delivery_node"]),
            )
            if choice is None:
                raise RuntimeError(f"No executable rack access exists for outbound {value['operation_id']} / {rack_id}.")
            pickup_node = choice.access_node_id
            delivery_node = str(value["delivery_node"])
        else:
            rack_id = str(value.get("rack_id") or value["delivery_node"])
            pickup_node = str(value["pickup_node"])
            # Prefer an access side that remains useful for subsequent rack
            # services in the same batch.  Choosing only the shortest approach
            # can trap a multi-task route on the wrong side of a one-way aisle.
            all_inbound_access_nodes = [
                access_node_id
                for operation in raw_operations
                if str(operation.get("operation_type")) == "INBOUND"
                for access_node_id in repository.rack_access_nodes(
                    str(operation.get("rack_id") or operation.get("delivery_node"))
                )
            ]
            candidates: list[tuple[int, int, str]] = []
            for access_node_id in repository.rack_access_nodes(rack_id):
                travel_time, path = directed.shortest_path(
                    pickup_node, access_node_id, metric="travel_time"
                )
                if not path and pickup_node != access_node_id:
                    continue
                onward_reachable = 0
                for target_access in all_inbound_access_nodes:
                    if target_access == access_node_id:
                        continue
                    _, onward_path = directed.shortest_path(
                        access_node_id, target_access, metric="travel_time"
                    )
                    if onward_path:
                        onward_reachable += 1
                candidates.append((-onward_reachable, int(travel_time), access_node_id))
            if not candidates:
                raise RuntimeError(f"No executable rack access exists for inbound {value['operation_id']} / {rack_id}.")
            delivery_node = min(candidates, key=lambda item: (item[0], item[1], item[2]))[2]
        value["rack_id"] = rack_id
        value["pickup_node"] = pickup_node
        value["delivery_node"] = delivery_node
        operations.append(value)
        tasks.append(
            OptimizationTask(
                task_id=str(value["operation_id"]),
                pickup_node=pickup_node,
                delivery_node=delivery_node,
                demand=int(value["demand"]),
                priority=str(value.get("priority", "medium")),
                rack_id=rack_id,
                rack_level=(int(value["rack_level"]) if value.get("rack_level") is not None else None),
                optional=False,
                unassigned_penalty=None,
                fixed_robot_id=None,
                pickup_service_time_ms=int(value["pickup_service_time_ms"]),
                drop_service_time_ms=int(value["drop_service_time_ms"]),
            )
        )
    vehicles = [
        OptimizationVehicle(
            robot_id=robot_id,
            start_node=robot_by_id[robot_id].current_node,
            capacity_units=robot_by_id[robot_id].capacity_units,
            battery_pct=robot_by_id[robot_id].battery_pct,
        )
        for robot_id in robots.candidate_robot_ids
    ]
    request = OptimizationRequest(
        snapshot_id="SNAP-V13-MIXED-BATCH",
        tasks=tasks,
        vehicles=vehicles,
        map_constraints=map_bundle.context.map_constraints,
        objective_profile="MIN_COMPLETION_TIME",
        max_edge_wait_ms=60000,
    )
    payload = CuOptPayloadBuilder().build(
        request=request,
        graph_nodes=map_bundle.graph_nodes,
        graph_arcs=map_bundle.graph_arcs,
        time_limit_seconds=5,
    )
    return request, payload, map_bundle.context, map_bundle.graph_node_types, {
        "operations": operations,
        "reference_routes": list(repository.scenario.get("reference_routes", [])),
        "fixture_design_notes": list(repository.scenario.get("fixture_design_notes", [])),
        "handling_time_policy": dict(repository.scenario.get("handling_time_policy", {})),
    }


def build_reference_result(reference_routes: list[dict]) -> OptimizerResult:
    """Build a test-only known-feasible assignment for fixture validation.

    This result is never used as a runtime fallback.  It only proves that the
    scenario graph, vehicle starts, capacities, handling times, and MAPF layer
    admit at least one complete multi-task plan before an external API is
    blamed for an infeasible fixture.
    """

    routes = [
        OptimizerRoute(
            vehicle_id=str(value["vehicle_id"]),
            task_sequence=[str(task_id) for task_id in value.get("task_sequence", [])],
        )
        for value in reference_routes
    ]
    return OptimizerResult(
        backend="rule",
        status="success",
        optimizer="fixture-reference-only",
        routes=routes,
        reason="Test-only known-feasible assignment; never used as a runtime fallback.",
    )


def handling_time_summary(payload, operations: list[dict], explicit_policy: dict | None = None) -> dict:
    """Return auditable pickup/drop handling times used by every backend."""

    settings = get_settings()
    by_task = dict(
        zip(
            payload.task_data.task_ids,
            payload.task_data.service_times_ms,
            strict=True,
        )
    )
    explicit = all(
        "pickup_service_time_ms" in value and "drop_service_time_ms" in value
        for value in operations
    )
    environment_policy = {
        "pickup_base_ms": settings.pickup_service_time_ms,
        "pickup_per_unit_ms": settings.pickup_service_time_per_unit_ms,
        "drop_base_ms": settings.drop_service_time_ms,
        "drop_per_unit_ms": settings.drop_service_time_per_unit_ms,
        "formula": "base_ms + per_unit_ms * demand",
        "source": "environment_formula",
    }
    policy = dict(explicit_policy or {}) if explicit else environment_policy
    if explicit and not policy:
        policy = {
            "source": "scenario_explicit_values",
            "formula": "per-operation pickup_service_time_ms/drop_service_time_ms",
        }
    return {
        "source": str(policy.get("source", "scenario_explicit" if explicit else "environment_formula")),
        "policy": policy,
        "task_service_times_ms": by_task,
        "total_service_ms_if_all_tasks_execute": sum(by_task.values()),
    }


def validate_reference_loads(payload, reference_result: OptimizerResult) -> dict:
    """Check cumulative carrying load for the fixture reference routes."""

    demand_by_task = dict(
        zip(payload.task_data.task_ids, payload.task_data.demand, strict=True)
    )
    capacity_by_robot = dict(
        zip(
            payload.fleet_data.vehicle_ids,
            payload.fleet_data.capacities,
            strict=True,
        )
    )
    errors: list[str] = []
    routes: list[dict] = []
    for route in reference_result.routes:
        capacity = capacity_by_robot.get(route.vehicle_id)
        if capacity is None:
            errors.append(f"Unknown reference vehicle {route.vehicle_id}.")
            continue
        load = 0
        peak = 0
        values: list[dict] = []
        for task_id in route.task_sequence:
            if task_id not in demand_by_task:
                errors.append(f"{route.vehicle_id} references unknown task {task_id}.")
                continue
            load += int(demand_by_task[task_id])
            peak = max(peak, load)
            values.append({"task_id": task_id, "load_after": load})
            if load < 0:
                errors.append(
                    f"{route.vehicle_id} unloads before pickup at {task_id}: load={load}."
                )
            if load > capacity:
                errors.append(
                    f"{route.vehicle_id} exceeds capacity {capacity} at {task_id}: load={load}."
                )
        if load != 0:
            errors.append(f"{route.vehicle_id} ends reference route with load {load}.")
        routes.append(
            {
                "vehicle_id": route.vehicle_id,
                "capacity": capacity,
                "peak_load": peak,
                "load_trace": values,
            }
        )
    return {"valid": not errors, "errors": errors, "routes": routes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=["payload_only", "ortools", "cuopt"],
        default="ortools",
        help="Use payload_only to validate without solving, ortools for local VRP, or cuopt for configured external service.",
    )
    args = parser.parse_args()
    fixture = PROJECT_ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    request, payload, map_context, node_types, metadata = build_problem(fixture)
    payload_validation = CuOptPayloadValidator().validate(payload)
    candidate_validation = CandidateSpaceGuard().validate(request=request, payload=payload)
    native = CuOptNativeRequestBuilder().build(payload)
    operations = metadata["operations"]
    fixture_validation = validate_fixture_operations(fixture, operations)
    operation_type_by_task = {str(value["operation_id"]): str(value["operation_type"]) for value in operations}
    reference_result = build_reference_result(metadata["reference_routes"])
    reference_loads = validate_reference_loads(payload, reference_result)
    reference_assignment = OptimizerAssignmentValidator().validate(
        payload=payload,
        result=reference_result,
    )
    reference_expansion, reference_schedule = PrioritizedSIPPPlanner().plan(
        payload=payload,
        result=reference_result,
        map_context=map_context,
        node_types=node_types,
    )
    reference_route_validation = StaticRouteValidator().validate(
        payload=payload,
        expansion=reference_expansion,
    )
    reference_mapf_validation = MAPFPlanValidator().validate(
        schedule=reference_schedule,
        map_context=map_context,
        node_types=node_types,
        max_edge_wait_ms=request.max_edge_wait_ms,
        payload=payload,
    )
    reference_multi_task_routes = sum(
        len(route.task_sequence) > 2 for route in reference_result.routes
    )
    reference_valid = (
        reference_assignment.valid
        and reference_route_validation.valid
        and reference_mapf_validation.valid
        and reference_schedule.valid
        and reference_loads["valid"]
        and reference_multi_task_routes >= 2
    )

    result: dict = {
        "version": "13.12.0",
        "scenario": "S12_mixed_inbound_outbound_multirobot",
        "backend": args.backend,
        "problem": {
            "outbound_operations": sum(value == "OUTBOUND" for value in operation_type_by_task.values()),
            "inbound_operations": sum(value == "INBOUND" for value in operation_type_by_task.values()),
            "pickup_delivery_pairs": len(payload.task_data.pickup_and_delivery_pairs),
            "task_rows": len(payload.task_data.task_ids),
            "eligible_robots": len(payload.fleet_data.vehicle_ids),
            "locations": len(payload.location_index_map),
            "directed_edges": len(payload.waypoint_graph_data.edge_ids),
        },
        "operations": operations,
        "fixture_design_notes": metadata["fixture_design_notes"],
        "handling_times": handling_time_summary(payload, operations, metadata["handling_time_policy"]),
        "validations": {
            "fixture": fixture_validation,
            "payload": payload_validation.model_dump(mode="json"),
            "candidate_space": candidate_validation.model_dump(mode="json"),
            "reference_assignment": reference_assignment.model_dump(mode="json"),
            "reference_loads": reference_loads,
            "reference_route": reference_route_validation.model_dump(mode="json"),
            "reference_mapf": reference_mapf_validation.model_dump(mode="json"),
        },
        "reference_feasibility": {
            "valid": reference_valid,
            "runtime_fallback": False,
            "multi_task_route_count": reference_multi_task_routes,
            "routes": [value.model_dump(mode="json") for value in reference_result.routes],
            "total_wait_ms": reference_schedule.total_wait_ms,
            "total_service_ms": reference_schedule.total_service_ms,
            "makespan_ms": reference_schedule.makespan_ms,
        },
        "native_cuopt_request": {
            "representation": "csr_waypoint_graph",
            "waypoint_nodes": len(native["cost_waypoint_graph_data"]["waypoint_graph"]["0"]["offsets"]) - 1,
            "waypoint_edges": len(native["cost_waypoint_graph_data"]["waypoint_graph"]["0"]["edges"]),
            "task_locations": len(native["task_data"]["task_locations"]),
            "vehicle_locations": len(native["fleet_data"]["vehicle_locations"]),
            "pickup_delivery_pairs": len(native["task_data"]["pickup_and_delivery_pairs"]),
            "serialized_bytes": len(json.dumps(native, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        },
    }
    if (
        not fixture_validation["valid"]
        or not payload_validation.valid
        or not candidate_validation.valid
        or not reference_valid
    ):
        result["status"] = "FAIL"
    elif args.backend == "payload_only":
        result["status"] = "READY_FOR_CUOPT"
    else:
        optimizer = ORToolsRoutingOptimizer() if args.backend == "ortools" else ExternalCuOptGateway()
        optimizer_result = optimizer.solve(payload)
        assignment = OptimizerAssignmentValidator().validate(payload=payload, result=optimizer_result)
        result["optimizer_result"] = optimizer_result.model_dump(mode="json")
        result["validations"]["assignment"] = assignment.model_dump(mode="json")
        if optimizer_result.status == "success" and assignment.valid:
            multi_task_route_count = sum(
                len(route.task_sequence) > 2 for route in optimizer_result.routes
            )
            result["solver_metrics"] = {
                "route_count": len(optimizer_result.routes),
                "multi_task_route_count": multi_task_route_count,
                "assigned_task_rows": sum(
                    len(route.task_sequence) for route in optimizer_result.routes
                ),
                "unassigned_task_rows": len(optimizer_result.unassigned_task_ids),
                "global_objective_cost": optimizer_result.global_objective_cost,
                "objective_values": [
                    value.model_dump(mode="json")
                    for value in optimizer_result.objective_values
                ],
                "cuopt_estimated_makespan_ms": optimizer_result.estimated_makespan_ms,
                "route_timing": [
                    {
                        "vehicle_id": route.vehicle_id,
                        "route_cost": route.route_cost,
                        "task_arrival_stamps_ms": route.task_arrival_stamps_ms,
                        "last_task_arrival_ms": route.last_task_arrival_ms,
                        "completion_ms": route.completion_ms,
                    }
                    for route in optimizer_result.routes
                ],
            }
            expansion, schedule = PrioritizedSIPPPlanner().plan(
                payload=payload,
                result=optimizer_result,
                map_context=map_context,
                node_types=node_types,
            )
            route_validation = StaticRouteValidator().validate(payload=payload, expansion=expansion)
            mapf_validation = MAPFPlanValidator().validate(
                schedule=schedule,
                map_context=map_context,
                node_types=node_types,
                max_edge_wait_ms=request.max_edge_wait_ms,
                payload=payload,
            )
            result["validations"]["route"] = route_validation.model_dump(mode="json")
            result["validations"]["mapf"] = mapf_validation.model_dump(mode="json")
            result["schedule"] = schedule.model_dump(mode="json")
            estimated_makespan = optimizer_result.estimated_makespan_ms
            result["solver_metrics"].update(
                {
                    "mapf_makespan_ms": schedule.makespan_ms,
                    "mapf_added_delay_ms": (
                        float(schedule.makespan_ms) - float(estimated_makespan)
                        if estimated_makespan is not None
                        else None
                    ),
                    "total_mapf_wait_ms": schedule.total_wait_ms,
                    "total_service_ms": schedule.total_service_ms,
                }
            )
            result["handling_execution"] = {
                "total_service_ms": schedule.total_service_ms,
                "service_step_count": sum(
                    step.step_type == "SERVICE"
                    for route in schedule.routes
                    for step in route.steps
                ),
            }
            result["status"] = (
                "PASS"
                if (
                    route_validation.valid
                    and mapf_validation.valid
                    and schedule.valid
                    and multi_task_route_count >= 1
                )
                else "FAIL"
            )
        else:
            if optimizer_result.status == "unavailable":
                result["status"] = "UNAVAILABLE"
            elif optimizer_result.status == "infeasible":
                result["status"] = "INFEASIBLE"
            else:
                result["status"] = "FAIL"

    output = PROJECT_ROOT / "runtime_outputs" / f"v13_mixed_batch_{args.backend}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_json_print(result)
    return 0 if result["status"] in {"PASS", "READY_FOR_CUOPT", "UNAVAILABLE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

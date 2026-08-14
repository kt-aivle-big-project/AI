"""Optimization request, cuOpt payload, local solver, and assignment validation."""
from __future__ import annotations

import heapq
import io
import json
import time
import zipfile
from math import inf, isfinite
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import get_settings
from app.domain.schemas import (
    CandidateRobot,
    CandidateSpaceValidation,
    CuOptPayload,
    FleetData,
    OptimizationRequest,
    OptimizationTask,
    OptimizationVehicle,
    MissionSpec,
    OptimizerAssignmentValidation,
    OptimizerObjectiveMetric,
    OptimizerResult,
    OptimizerRoute,
    PayloadValidationResult,
    PolicyValidationResult,
    TaskData,
    WaypointGraphData,
)
from app.services.graph_service import DirectedGraphService


PRIORITY_SCORE = {"high": 0, "medium": 1, "low": 2}

# cuOpt currently exposes route-cost and route-size variance controls through
# the native API used by this project.  The variance term is the available soft
# proxy for spreading independent BOX cycles across the eligible fleet, which
# reduces the overall completion time without imposing a hard minimum vehicle
# count.  Cost remains present so the solver does not create wasteful routes
# merely to equalize task counts.
ROUTE_BALANCE_WEIGHT_BY_OBJECTIVE_PROFILE = {
    "MIN_TOTAL_COST": 0,
    "MIN_COMPLETION_TIME": 10,
    "THROUGHPUT": 10,
    "BALANCED": 5,
    "URGENT_FIRST": 5,
    "MIN_REHANDLE": 0,
}


def _route_balance_weight(objective_profile: str) -> int:
    """Return the soft fleet-balancing weight for one objective profile."""

    return int(ROUTE_BALANCE_WEIGHT_BY_OBJECTIVE_PROFILE.get(objective_profile, 0))


def _native_objectives(objective_profile: str) -> dict[str, int]:
    """Translate one LARO objective profile to supported native cuOpt terms."""

    weight = _route_balance_weight(objective_profile)
    return {
        "cost": 1,
        **({"variance_route_size": weight} if weight > 0 else {}),
    }


def _rack_access_candidates(node_id: str, graph_nodes: list[str]) -> list[str]:
    """Return migrated service nodes for a legacy rack identifier.

    v13.12 removes ``K*`` rack entities from the routing graph.  This bounded
    compatibility shim lets older fixtures enter through a legacy rack ID while
    ensuring the emitted cuOpt payload contains only ``*_ACCESS_A/B`` nodes.
    """

    candidates = [
        value
        for value in (f"{node_id}_ACCESS_A", f"{node_id}_ACCESS_B")
        if value in graph_nodes
    ]
    return candidates


def _resolve_service_node(
    *,
    requested_node: str,
    counterpart_node: str,
    is_pickup: bool,
    request: OptimizationRequest,
    graph_nodes: list[str],
    graph_arcs: list[dict],
) -> str:
    """Resolve a routing node, converting a legacy rack ID to one access side."""

    if requested_node in graph_nodes:
        return requested_node
    candidates = _rack_access_candidates(requested_node, graph_nodes)
    if not candidates:
        raise KeyError(requested_node)
    graph = DirectedGraphService(graph_arcs)
    scored: list[tuple[float, str]] = []
    if is_pickup:
        for access_node_id in candidates:
            to_drop, drop_path = graph.shortest_path(
                access_node_id, counterpart_node, metric="travel_time"
            )
            if access_node_id != counterpart_node and not drop_path:
                continue
            robot_values: list[float] = []
            for vehicle in request.vehicles:
                value, path = graph.shortest_path(
                    vehicle.start_node, access_node_id, metric="travel_time"
                )
                if vehicle.start_node == access_node_id or path:
                    robot_values.append(float(value))
            if robot_values:
                scored.append((min(robot_values) + float(to_drop), access_node_id))
    else:
        for access_node_id in candidates:
            value, path = graph.shortest_path(
                counterpart_node, access_node_id, metric="travel_time"
            )
            if counterpart_node == access_node_id or path:
                scored.append((float(value), access_node_id))
    if not scored:
        raise KeyError(
            f"Legacy rack {requested_node} has no reachable service access node."
        )
    return min(scored, key=lambda value: (value[0], value[1]))[1]
OPTIONAL_TASK_PENALTY_BY_PRIORITY = {0: 1_000_000_000, 1: 100_000_000, 2: 10_000_000}


def _derived_handling_time_ms(
    *,
    task: OptimizationTask,
    pickup: bool,
) -> int:
    """Return one deterministic pickup/drop handling time for a task.

    The warehouse model previously used one fixed one-second service value for
    every task row.  v13.5 keeps the value configurable but also accounts for
    the requested quantity.  A task-specific authoritative value can override
    the formula when a real WMS/WCS later supplies measured handling times.
    """

    settings = get_settings()
    if pickup:
        if task.pickup_service_time_ms is not None:
            return int(task.pickup_service_time_ms)
        return int(
            settings.pickup_service_time_ms
            + settings.pickup_service_time_per_unit_ms * int(task.demand)
        )
    if task.drop_service_time_ms is not None:
        return int(task.drop_service_time_ms)
    return int(
        settings.drop_service_time_ms
        + settings.drop_service_time_per_unit_ms * int(task.demand)
    )


def _normalized_service_times(payload: CuOptPayload) -> list[int]:
    """Return a complete task-row service-time vector.

    The fallback keeps old serialized fixtures readable while every v13.5
    payload builder emits the explicit vector.
    """

    values = list(payload.task_data.service_times_ms)
    if values:
        return values
    settings = get_settings()
    return [
        (
            settings.pickup_service_time_ms
            if task_id.endswith("_PICK")
            else settings.drop_service_time_ms
        )
        for task_id in payload.task_data.task_ids
    ]


def _normalized_vehicle_available_at_ms(payload: CuOptPayload) -> list[int]:
    """Return one non-negative earliest-start value per vehicle."""

    values = list(payload.fleet_data.vehicle_available_at_ms)
    if values:
        return [int(value) for value in values]
    return [0 for _ in payload.fleet_data.vehicle_ids]


def build_optimization_request(
    policy: PolicyValidationResult,
    mission: MissionSpec,
) -> OptimizationRequest:
    """Convert canonical policy materialization into a solver-neutral request."""

    optional_orders = set(mission.optional_order_ids)
    penalty_by_priority = {"high": 1_000_000_000, "medium": 100_000_000, "low": 10_000_000}
    return OptimizationRequest(
        snapshot_id=policy.snapshot_id,
        tasks=[
            OptimizationTask(
                task_id=task.task_id,
                pickup_node=task.pickup_node,
                delivery_node=task.delivery_node,
                demand=task.demand,
                priority=task.priority,
                operation_type=("OUTBOUND_ORDER" if task.task_type == "outbound_pick" else "RECOVERY"),
                order_id=task.order_id,
                order_ids=[task.order_id] if task.order_id else [],
                item_id=task.item_id,
                stock_id=task.stock_id,
                logical_destination_ids=[task.delivery_node] if task.order_id else [],
                rack_id=task.rack_id,
                rack_level=task.rack_level,
                optional=(task.order_id or "") in optional_orders,
                unassigned_penalty=(
                    penalty_by_priority[task.priority]
                    if (task.order_id or "") in optional_orders
                    else None
                ),
                fixed_robot_id=task.fixed_robot_id,
            )
            for task in policy.validated_tasks
        ],
        vehicles=[
            OptimizationVehicle(
                robot_id=robot.robot_id,
                start_node=robot.start_node,
                end_node=robot.home_node,
                terminal_policy=("CHARGE" if robot.home_node else "STAY"),
                capacity_units=robot.capacity_units,
                battery_pct=robot.battery_pct,
                available_at_ms=robot.available_at_ms,
            )
            for robot in policy.candidate_robots
        ],
        map_constraints=policy.map_constraints,
        objective_profile=mission.objective_profile,
        max_edge_wait_ms=mission.max_edge_wait_ms,
    )


class CuOptPayloadBuilder:
    """Build cuOpt-oriented arrays from a solver-neutral request and graph snapshot."""

    def build(
        self,
        *,
        request: OptimizationRequest,
        graph_nodes: list[str],
        graph_arcs: list[dict],
        time_limit_seconds: int,
    ) -> CuOptPayload:
        """Create indexed vehicle, task, cost, and travel-time arrays."""

        index = {node_id: position for position, node_id in enumerate(graph_nodes)}
        task_ids: list[str] = []
        task_locations: list[int] = []
        pairs: list[list[int]] = []
        demand: list[int] = []
        priorities: list[int] = []
        service_times_ms: list[int] = []
        fixed_vehicle_ids: list[str | None] = []
        optional_task_ids: list[str] = []
        for task in request.tasks:
            pickup_node = _resolve_service_node(
                requested_node=task.pickup_node,
                counterpart_node=task.delivery_node,
                is_pickup=True,
                request=request,
                graph_nodes=graph_nodes,
                graph_arcs=graph_arcs,
            )
            delivery_node = _resolve_service_node(
                requested_node=task.delivery_node,
                counterpart_node=pickup_node,
                is_pickup=False,
                request=request,
                graph_nodes=graph_nodes,
                graph_arcs=graph_arcs,
            )
            pickup_index = len(task_ids)
            task_ids.append(f"{task.task_id}_PICK")
            task_locations.append(index[pickup_node])
            demand.append(task.demand)
            priorities.append(PRIORITY_SCORE[task.priority])
            service_times_ms.append(_derived_handling_time_ms(task=task, pickup=True))
            fixed_vehicle_ids.append(task.fixed_robot_id)
            if task.optional:
                optional_task_ids.append(f"{task.task_id}_PICK")
            delivery_index = len(task_ids)
            task_ids.append(f"{task.task_id}_DROP")
            task_locations.append(index[delivery_node])
            demand.append(-task.demand)
            priorities.append(PRIORITY_SCORE[task.priority])
            service_times_ms.append(_derived_handling_time_ms(task=task, pickup=False))
            fixed_vehicle_ids.append(task.fixed_robot_id)
            if task.optional:
                optional_task_ids.append(f"{task.task_id}_DROP")
            pairs.append([pickup_index, delivery_index])
        return CuOptPayload(
            snapshot_id=request.snapshot_id,
            objective_profile=request.objective_profile,
            location_index_map=index,
            fleet_data=FleetData(
                vehicle_ids=[vehicle.robot_id for vehicle in request.vehicles],
                vehicle_start_locations=[index[vehicle.start_node] for vehicle in request.vehicles],
                vehicle_end_locations=[
                    index[vehicle.end_node or vehicle.start_node]
                    for vehicle in request.vehicles
                ],
                capacities=[vehicle.capacity_units for vehicle in request.vehicles],
                vehicle_available_at_ms=[vehicle.available_at_ms for vehicle in request.vehicles],
                min_vehicles=int(request.minimum_vehicle_count),
                max_g2p_cycles_per_vehicle=request.max_g2p_cycles_per_vehicle,
                skip_first_trips=[get_settings().cuopt_skip_first_trips for _ in request.vehicles],
                drop_return_trips=[
                    (
                        get_settings().cuopt_drop_return_trips
                        if vehicle.terminal_policy == "STAY"
                        else False
                    )
                    for vehicle in request.vehicles
                ],
            ),
            task_data=TaskData(
                task_ids=task_ids,
                task_locations=task_locations,
                pickup_and_delivery_pairs=pairs,
                demand=demand,
                priorities=priorities,
                service_times_ms=service_times_ms,
                fixed_vehicle_ids=fixed_vehicle_ids,
                optional_task_ids=optional_task_ids,
            ),
            waypoint_graph_data=WaypointGraphData(
                edge_ids=[str(arc["edge_id"]) for arc in graph_arcs],
                from_indices=[index[str(arc["source"])] for arc in graph_arcs],
                to_indices=[index[str(arc["target"])] for arc in graph_arcs],
                costs=[float(arc["cost"]) for arc in graph_arcs],
                travel_times_ms=[int(arc["travel_time_ms"]) for arc in graph_arcs],
            ),
            applied_map_constraints=request.map_constraints,
            time_limit_seconds=time_limit_seconds,
        )


class CuOptPayloadValidator:
    """Validate the full indexed payload before any optimizer call."""

    @staticmethod
    def _fleet_vectors(payload: CuOptPayload) -> tuple[list[int], list[bool], list[bool]]:
        """Return normalized end locations and open/closed-route flags."""

        fleet = payload.fleet_data
        count = len(fleet.vehicle_ids)
        end_locations = list(fleet.vehicle_end_locations) or list(fleet.vehicle_start_locations)
        skip_first = list(fleet.skip_first_trips) or [False] * count
        drop_return = list(fleet.drop_return_trips) or [True] * count
        return end_locations, skip_first, drop_return

    def validate(self, payload: CuOptPayload) -> PayloadValidationResult:
        """Check wire shape and necessary physical feasibility before a solver call.

        The feasibility checks are existential: every mandatory pickup-delivery
        pair must have at least one eligible vehicle. The validator does not
        pre-assign tasks and does not solve the multi-task routing problem.
        """

        errors: list[str] = []
        warnings: list[str] = []
        location_count = len(payload.location_index_map)
        valid_indexes = set(range(location_count))
        fleet = payload.fleet_data
        tasks = payload.task_data
        graph = payload.waypoint_graph_data
        end_locations, skip_first_trips, drop_return_trips = self._fleet_vectors(payload)
        vehicle_available_at_ms = _normalized_vehicle_available_at_ms(payload)
        vehicle_count = len(fleet.vehicle_ids)

        if not fleet.vehicle_ids:
            errors.append("At least one vehicle is required.")
        fleet_lengths = {
            len(fleet.vehicle_ids),
            len(fleet.vehicle_start_locations),
            len(end_locations),
            len(fleet.capacities),
            len(vehicle_available_at_ms),
            len(skip_first_trips),
            len(drop_return_trips),
        }
        fleet_shape_valid = len(fleet_lengths) == 1
        if not fleet_shape_valid:
            errors.append("Vehicle start/end/capacity/open-route arrays must have equal lengths.")
        if any(index not in valid_indexes for index in fleet.vehicle_start_locations):
            errors.append("Vehicle start index is outside the location map.")
        if any(index not in valid_indexes for index in end_locations):
            errors.append("Vehicle end index is outside the location map.")
        if any(value <= 0 for value in fleet.capacities):
            errors.append("Vehicle capacities must be positive.")
        if any(value < 0 for value in vehicle_available_at_ms):
            errors.append("Vehicle available-at times must be non-negative.")
        if fleet.min_vehicles > vehicle_count:
            errors.append("min_vehicles cannot exceed the available fleet size.")
        if fleet.min_vehicles > len(tasks.pickup_and_delivery_pairs):
            errors.append("min_vehicles cannot exceed the number of pickup-delivery pairs.")

        task_lengths = {
            len(tasks.task_ids),
            len(tasks.task_locations),
            len(tasks.demand),
            len(tasks.priorities),
            len(_normalized_service_times(payload)),
            len(tasks.fixed_vehicle_ids),
        }
        task_shape_valid = len(task_lengths) == 1
        if not task_shape_valid:
            errors.append("Task arrays must have equal lengths.")
        if any(index not in valid_indexes for index in tasks.task_locations):
            errors.append("Task location index is outside the location map.")
        if any(not 0 <= value <= 255 for value in tasks.priorities):
            errors.append("Task priorities must be within [0, 255].")
        if any(value < 0 for value in _normalized_service_times(payload)):
            errors.append("Task service times must be non-negative.")
        optional_rows = set(tasks.optional_task_ids)
        unknown_optional = sorted(optional_rows.difference(tasks.task_ids))
        if unknown_optional:
            errors.append(f"Unknown optional task ids: {unknown_optional}")

        graph_lengths = {
            len(graph.edge_ids),
            len(graph.from_indices),
            len(graph.to_indices),
            len(graph.costs),
            len(graph.travel_times_ms),
        }
        graph_shape_valid = len(graph_lengths) == 1
        if not graph_shape_valid:
            errors.append("Waypoint graph arrays must have equal lengths.")
        if any(index not in valid_indexes for index in [*graph.from_indices, *graph.to_indices]):
            errors.append("Waypoint graph contains an invalid location index.")
        if any(value <= 0 for value in graph.costs) or any(value <= 0 for value in graph.travel_times_ms):
            errors.append("Graph costs and travel times must be positive.")

        valid_pairs: list[tuple[int, int]] = []
        if task_shape_valid:
            for pair in tasks.pickup_and_delivery_pairs:
                if len(pair) != 2:
                    errors.append(f"Invalid pickup-delivery pair: {pair}")
                    continue
                pickup, delivery = pair
                if not (0 <= pickup < len(tasks.task_ids) and 0 <= delivery < len(tasks.task_ids)):
                    errors.append(f"Pair index is out of range: {pair}")
                    continue
                if tasks.demand[pickup] <= 0 or tasks.demand[delivery] != -tasks.demand[pickup]:
                    errors.append(f"Demand signs do not match pair {pair}.")
                fixed = tasks.fixed_vehicle_ids[pickup]
                delivery_fixed = tasks.fixed_vehicle_ids[delivery]
                if fixed != delivery_fixed:
                    errors.append(f"Pickup and delivery rows use different fixed robots for pair {pair}.")
                if fixed is not None and fixed not in fleet.vehicle_ids:
                    errors.append(f"Fixed robot {fixed} is absent from fleet_data.")
                pickup_optional = tasks.task_ids[pickup] in optional_rows
                delivery_optional = tasks.task_ids[delivery] in optional_rows
                if pickup_optional != delivery_optional:
                    errors.append(f"Pickup and delivery optional flags differ for pair {pair}.")
                valid_pairs.append((pickup, delivery))

        # Do not let malformed arrays raise while the validator is trying to
        # describe them. Deeper reachability checks require a structurally
        # coherent payload and are skipped once a blocking shape error exists.
        if errors or not (fleet_shape_valid and task_shape_valid and graph_shape_valid):
            return PayloadValidationResult(valid=False, errors=errors, warnings=warnings)

        arcs = [
            {
                "edge_id": edge_id,
                "source": str(source),
                "target": str(target),
                "cost": cost,
                "travel_time_ms": travel,
            }
            for edge_id, source, target, cost, travel in zip(
                graph.edge_ids,
                graph.from_indices,
                graph.to_indices,
                graph.costs,
                graph.travel_times_ms,
                strict=True,
            )
        ]
        indexed_graph = DirectedGraphService(arcs)
        vehicle_index = {robot_id: index for index, robot_id in enumerate(fleet.vehicle_ids)}

        for pickup, delivery in valid_pairs:
            pickup_location = tasks.task_locations[pickup]
            delivery_location = tasks.task_locations[delivery]
            pickup_id = tasks.task_ids[pickup]
            delivery_id = tasks.task_ids[delivery]
            if not indexed_graph.reachable(str(pickup_location), str(delivery_location)):
                errors.append(f"No directed path exists for task pair {[pickup, delivery]}.")
                continue

            fixed_robot = tasks.fixed_vehicle_ids[pickup]
            candidate_indexes = (
                [vehicle_index[fixed_robot]]
                if fixed_robot is not None and fixed_robot in vehicle_index
                else list(range(vehicle_count))
            )
            feasible_vehicles: list[str] = []
            for index in candidate_indexes:
                if fleet.capacities[index] < tasks.demand[pickup]:
                    continue
                # Even when cuOpt is configured to skip pricing the first leg,
                # an AMR still needs a physical directed path to the pickup for
                # downstream MAPF execution.
                if not indexed_graph.reachable(
                    str(fleet.vehicle_start_locations[index]),
                    str(pickup_location),
                ):
                    continue
                if not drop_return_trips[index] and not indexed_graph.reachable(
                    str(delivery_location),
                    str(end_locations[index]),
                ):
                    continue
                feasible_vehicles.append(fleet.vehicle_ids[index])

            pair_is_optional = pickup_id in optional_rows and delivery_id in optional_rows
            if not feasible_vehicles and not pair_is_optional:
                return_detail = (
                    "the configured vehicle end"
                    if any(not drop_return_trips[index] for index in candidate_indexes)
                    else "the open-route policy"
                )
                errors.append(
                    "No eligible vehicle can execute mandatory pair "
                    f"{pickup_id}->{delivery_id}; capacity, start-to-pickup reachability, "
                    f"and {return_detail} were checked."
                )

        if not errors and len(payload.location_index_map) > 120:
            warnings.append(
                "Large internal waypoint graph accepted; the external cuOpt adapter serializes it as native CSR."
            )
        if any(drop_return_trips):
            warnings.append(
                "Open-route policy is enabled for at least one vehicle; cuOpt will not price or require its final return leg."
            )
        return PayloadValidationResult(valid=not errors, errors=errors, warnings=warnings)


class CandidateSpaceGuard:
    """Detect silent task or vehicle pruning before solver execution."""

    def validate(
        self,
        *,
        request: OptimizationRequest,
        payload: CuOptPayload,
    ) -> CandidateSpaceValidation:
        """Compare canonical solver-neutral inputs with serialized payload arrays."""

        errors: list[str] = []
        warnings: list[str] = []
        expected_vehicles = {vehicle.robot_id for vehicle in request.vehicles}
        actual_vehicles = set(payload.fleet_data.vehicle_ids)
        if expected_vehicles != actual_vehicles:
            errors.append(
                "Vehicle candidate space changed during payload construction: "
                f"missing={sorted(expected_vehicles-actual_vehicles)}, "
                f"unknown={sorted(actual_vehicles-expected_vehicles)}"
            )
        expected_tasks = {
            value
            for task in request.tasks
            for value in (f"{task.task_id}_PICK", f"{task.task_id}_DROP")
        }
        actual_tasks = set(payload.task_data.task_ids)
        if expected_tasks != actual_tasks:
            errors.append(
                "Task candidate space changed during payload construction: "
                f"missing={sorted(expected_tasks-actual_tasks)}, "
                f"unknown={sorted(actual_tasks-expected_tasks)}"
            )
        optional_expected = {
            value
            for task in request.tasks
            if task.optional
            for value in (f"{task.task_id}_PICK", f"{task.task_id}_DROP")
        }
        if optional_expected != set(payload.task_data.optional_task_ids):
            errors.append("Optional-task markers do not match the canonical request.")
        if not request.tasks:
            warnings.append("Optimization request contains no tasks.")
        return CandidateSpaceValidation(valid=not errors, errors=errors, warnings=warnings)


class OneToOneRuleOptimizer:
    """Deterministic light-path baseline: at most one new task pair per robot."""

    def solve(self, payload: CuOptPayload, *, allow_partial: bool = False) -> OptimizerResult:
        """Greedily assign priority-ordered pairs to distinct robots."""

        reverse_index = {index: node_id for node_id, index in payload.location_index_map.items()}
        arcs = [
            {
                "edge_id": edge_id,
                "source": reverse_index[source],
                "target": reverse_index[target],
                "cost": cost,
                "travel_time_ms": travel,
            }
            for edge_id, source, target, cost, travel in zip(
                payload.waypoint_graph_data.edge_ids,
                payload.waypoint_graph_data.from_indices,
                payload.waypoint_graph_data.to_indices,
                payload.waypoint_graph_data.costs,
                payload.waypoint_graph_data.travel_times_ms,
                strict=True,
            )
        ]
        graph = DirectedGraphService(arcs)
        starts = {
            robot_id: reverse_index[start]
            for robot_id, start in zip(
                payload.fleet_data.vehicle_ids,
                payload.fleet_data.vehicle_start_locations,
                strict=True,
            )
        }
        capacities = dict(zip(payload.fleet_data.vehicle_ids, payload.fleet_data.capacities, strict=True))
        available = set(payload.fleet_data.vehicle_ids)
        routes: list[OptimizerRoute] = []
        unassigned: list[str] = []
        service_times_ms = _normalized_service_times(payload)
        pairs = sorted(
            payload.task_data.pickup_and_delivery_pairs,
            key=lambda pair: (payload.task_data.priorities[pair[0]], payload.task_data.task_ids[pair[0]]),
        )
        for pickup, delivery in pairs:
            pickup_id = payload.task_data.task_ids[pickup]
            delivery_id = payload.task_data.task_ids[delivery]
            pickup_node = reverse_index[payload.task_data.task_locations[pickup]]
            delivery_node = reverse_index[payload.task_data.task_locations[delivery]]
            demand = payload.task_data.demand[pickup]
            fixed = payload.task_data.fixed_vehicle_ids[pickup]
            best: tuple[float, str, float, float] | None = None
            for robot_id in sorted(available):
                if fixed is not None and robot_id != fixed:
                    continue
                if capacities[robot_id] < demand:
                    continue
                first, _ = graph.shortest_path(starts[robot_id], pickup_node, metric="travel_time")
                second, _ = graph.shortest_path(pickup_node, delivery_node, metric="travel_time")
                if first == inf or second == inf:
                    continue
                pickup_arrival_ms = float(first)
                delivery_arrival_ms = float(
                    first + service_times_ms[pickup] + second
                )
                completion_ms = float(
                    delivery_arrival_ms + service_times_ms[delivery]
                )
                candidate = (
                    completion_ms,
                    robot_id,
                    pickup_arrival_ms,
                    delivery_arrival_ms,
                )
                if best is None or candidate < best:
                    best = candidate
            if best is None:
                unassigned.extend([pickup_id, delivery_id])
                continue
            completion_ms, robot_id, pickup_arrival_ms, delivery_arrival_ms = best
            available.remove(robot_id)
            routes.append(
                OptimizerRoute(
                    vehicle_id=robot_id,
                    task_sequence=[pickup_id, delivery_id],
                    route_cost=completion_ms,
                    task_arrival_stamps_ms=[pickup_arrival_ms, delivery_arrival_ms],
                    last_task_arrival_ms=delivery_arrival_ms,
                    completion_ms=completion_ms,
                )
            )
        if unassigned and not allow_partial:
            return OptimizerResult(
                backend="rule",
                status="infeasible",
                optimizer="one-to-one-rule-baseline",
                routes=routes,
                unassigned_task_ids=unassigned,
                reason="One-to-one rule planning cannot cover all task pairs.",
            )
        route_costs = [route.route_cost for route in routes if route.route_cost is not None]
        completions = [route.completion_ms for route in routes if route.completion_ms is not None]
        return OptimizerResult(
            backend="rule",
            status="success",
            optimizer="one-to-one-rule-baseline",
            global_objective_cost=float(sum(route_costs)) if route_costs else 0.0,
            estimated_makespan_ms=max(completions) if completions else 0.0,
            routes=routes,
            unassigned_task_ids=unassigned,
            reason="Partial baseline probe." if unassigned else None,
        )


class ORToolsRoutingOptimizer:
    """Local multi-task pickup-delivery backend using OR-Tools RoutingModel."""

    def solve(self, payload: CuOptPayload) -> OptimizerResult:
        """Assign and sequence multiple pickup-delivery tasks per robot."""

        try:
            from ortools.constraint_solver import pywrapcp, routing_enums_pb2
        except Exception as exc:  # pragma: no cover - depends on optional native wheel
            return OptimizerResult(
                backend="ortools",
                status="unavailable",
                optimizer="ortools-routing",
                reason=(
                    "OR-Tools is not installed. Install requirements.txt in a supported "
                    f"Python environment. Import error: {exc}"
                ),
            )

        if not payload.task_data.pickup_and_delivery_pairs:
            return OptimizerResult(
                backend="ortools",
                status="success",
                optimizer="ortools-routing",
                routes=[],
            )

        reverse_index = {index: node_id for node_id, index in payload.location_index_map.items()}
        graph = DirectedGraphService(
            [
                {
                    "edge_id": edge_id,
                    "source": reverse_index[source],
                    "target": reverse_index[target],
                    "cost": cost,
                    "travel_time_ms": travel,
                }
                for edge_id, source, target, cost, travel in zip(
                    payload.waypoint_graph_data.edge_ids,
                    payload.waypoint_graph_data.from_indices,
                    payload.waypoint_graph_data.to_indices,
                    payload.waypoint_graph_data.costs,
                    payload.waypoint_graph_data.travel_times_ms,
                    strict=True,
                )
            ]
        )
        vehicle_count = len(payload.fleet_data.vehicle_ids)
        # Logical nodes keep duplicate task visits even when two tasks share one rack.
        physical_nodes: list[str | None] = [
            reverse_index[value] for value in payload.fleet_data.vehicle_start_locations
        ]
        task_logical: dict[int, str] = {}
        service_by_logical: dict[int, int] = {}
        task_row_to_logical: dict[int, int] = {}
        service_times_ms = _normalized_service_times(payload)
        for row, (task_id, location) in enumerate(
            zip(payload.task_data.task_ids, payload.task_data.task_locations, strict=True)
        ):
            logical = len(physical_nodes)
            physical_nodes.append(reverse_index[location])
            task_logical[logical] = task_id
            service_by_logical[logical] = int(service_times_ms[row])
            task_row_to_logical[row] = logical
        end_locations = list(payload.fleet_data.vehicle_end_locations) or list(
            payload.fleet_data.vehicle_start_locations
        )
        drop_return_trips = list(payload.fleet_data.drop_return_trips) or [
            True for _ in range(vehicle_count)
        ]
        starts = list(range(vehicle_count))
        ends: list[int] = []
        for vehicle in range(vehicle_count):
            logical = len(physical_nodes)
            physical_nodes.append(
                None
                if drop_return_trips[vehicle]
                else reverse_index[end_locations[vehicle]]
            )
            ends.append(logical)
        manager = pywrapcp.RoutingIndexManager(
            len(physical_nodes), vehicle_count, starts, ends
        )
        routing = pywrapcp.RoutingModel(manager)

        cache: dict[tuple[str, str], int] = {}
        unreachable = 10**9

        def travel_ms(from_index: int, to_index: int) -> int:
            """Return the deterministic travel time used by the routing backend callback."""
            from_logical = manager.IndexToNode(from_index)
            to_logical = manager.IndexToNode(to_index)
            source = physical_nodes[from_logical]
            target = physical_nodes[to_logical]
            if target is None:
                # Closing the virtual route must still account for the final
                # task's handling time. The virtual end itself adds no travel.
                return int(service_by_logical.get(from_logical, 0))
            if source is None:
                return unreachable
            key = (source, target)
            if key not in cache:
                value, _ = graph.shortest_path(source, target, metric="travel_time")
                cache[key] = unreachable if value == inf else int(value)
            service = int(service_by_logical.get(from_logical, 0))
            return min(unreachable, cache[key] + service)

        transit_callback = routing.RegisterTransitCallback(travel_ms)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback)
        vehicle_available_at_ms = _normalized_vehicle_available_at_ms(payload)
        route_budget_ms = sum(payload.waypoint_graph_data.travel_times_ms) * max(
            1, len(payload.task_data.task_ids)
        )
        horizon = max(1, max(vehicle_available_at_ms, default=0) + route_budget_ms)
        routing.AddDimension(
            transit_callback,
            get_settings().mapf_max_wait_ms,
            horizon,
            False,
            "Time",
        )
        time_dimension = routing.GetDimensionOrDie("Time")
        route_balance_weight = _route_balance_weight(payload.objective_profile)
        if route_balance_weight > 0 or payload.fleet_data.min_vehicles > 1:
            # Minimize the longest route as a soft completion-time objective.
            # A historical hard minimum fleet count also retains this behavior
            # when an older persisted payload is replayed.
            time_dimension.SetGlobalSpanCostCoefficient(
                route_balance_weight * 10 if route_balance_weight > 0 else 100
            )
        for vehicle, available_at_ms in enumerate(vehicle_available_at_ms):
            start_var = time_dimension.CumulVar(routing.Start(vehicle))
            start_var.SetRange(int(available_at_ms), horizon)
            routing.AddVariableMinimizedByFinalizer(start_var)
            routing.AddVariableMinimizedByFinalizer(
                time_dimension.CumulVar(routing.End(vehicle))
            )

        demand_by_logical = {task_row_to_logical[i]: value for i, value in enumerate(payload.task_data.demand)}

        def demand_callback(index: int) -> int:
            """Return task demand for the OR-Tools capacity dimension."""
            return int(demand_by_logical.get(manager.IndexToNode(index), 0))

        demand_index = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(
            demand_index,
            0,
            [int(value) for value in payload.fleet_data.capacities],
            True,
            "Capacity",
        )
        solver = routing.solver()
        optional_ids = set(payload.task_data.optional_task_ids)
        vehicle_index = {
            robot_id: position for position, robot_id in enumerate(payload.fleet_data.vehicle_ids)
        }
        g2p_pickup_indices: list[int] = []
        for pickup_row, delivery_row in payload.task_data.pickup_and_delivery_pairs:
            pickup = manager.NodeToIndex(task_row_to_logical[pickup_row])
            delivery = manager.NodeToIndex(task_row_to_logical[delivery_row])
            routing.AddPickupAndDelivery(pickup, delivery)
            solver.Add(routing.VehicleVar(pickup) == routing.VehicleVar(delivery))
            solver.Add(time_dimension.CumulVar(pickup) <= time_dimension.CumulVar(delivery))
            fixed = payload.task_data.fixed_vehicle_ids[pickup_row]
            if fixed is not None:
                if fixed not in vehicle_index:
                    return OptimizerResult(
                        backend="ortools",
                        status="failed",
                        optimizer="ortools-routing",
                        reason=f"Fixed robot {fixed} is absent from the fleet.",
                    )
                solver.Add(routing.VehicleVar(pickup) == vehicle_index[fixed])
                solver.Add(routing.VehicleVar(delivery) == vehicle_index[fixed])
            pickup_id = payload.task_data.task_ids[pickup_row]
            delivery_id = payload.task_data.task_ids[delivery_row]
            if pickup_id.startswith("G2P-"):
                g2p_pickup_indices.append(pickup)
            if pickup_id in optional_ids or delivery_id in optional_ids:
                priority = payload.task_data.priorities[pickup_row]
                penalty = OPTIONAL_TASK_PENALTY_BY_PRIORITY.get(priority, 10_000_000)
                solver.Add(routing.ActiveVar(pickup) == routing.ActiveVar(delivery))
                routing.AddDisjunction([pickup], penalty // 2)
                routing.AddDisjunction([delivery], penalty // 2)

        max_g2p_cycles = payload.fleet_data.max_g2p_cycles_per_vehicle
        if max_g2p_cycles is not None and g2p_pickup_indices:
            for vehicle in range(vehicle_count):
                solver.Add(
                    solver.Sum([
                        solver.IsEqualCstVar(routing.VehicleVar(pickup), vehicle)
                        for pickup in g2p_pickup_indices
                    ]) <= int(max_g2p_cycles)
                )
        if payload.fleet_data.min_vehicles > 0:
            # ActiveVehicleVar counts a route only when it services at least one task.
            solver.Add(
                solver.Sum([
                    routing.ActiveVehicleVar(vehicle)
                    for vehicle in range(vehicle_count)
                ]) >= int(payload.fleet_data.min_vehicles)
            )

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.seconds = int(get_settings().ortools_time_limit_seconds)
        solution = routing.SolveWithParameters(params)
        if solution is None:
            return OptimizerResult(
                backend="ortools",
                status="infeasible",
                optimizer="ortools-routing",
                reason="OR-Tools found no feasible pickup-delivery solution.",
            )

        routes: list[OptimizerRoute] = []
        visited: set[str] = set()
        for vehicle in range(vehicle_count):
            index_value = routing.Start(vehicle)
            sequence: list[str] = []
            task_arrivals_ms: list[float | None] = []
            route_cost = 0.0
            while not routing.IsEnd(index_value):
                logical = manager.IndexToNode(index_value)
                task_id = task_logical.get(logical)
                if task_id:
                    sequence.append(task_id)
                    visited.add(task_id)
                    task_arrivals_ms.append(
                        float(solution.Value(time_dimension.CumulVar(index_value)))
                    )
                next_index = solution.Value(routing.NextVar(index_value))
                route_cost += float(
                    routing.GetArcCostForVehicle(index_value, next_index, vehicle)
                )
                index_value = next_index
            if sequence:
                completion_ms = float(solution.Value(time_dimension.CumulVar(index_value)))
                last_arrival_ms = (
                    task_arrivals_ms[-1]
                    if task_arrivals_ms
                    else None
                )
                routes.append(
                    OptimizerRoute(
                        vehicle_id=payload.fleet_data.vehicle_ids[vehicle],
                        task_sequence=sequence,
                        route_cost=route_cost,
                        task_arrival_stamps_ms=task_arrivals_ms,
                        last_task_arrival_ms=last_arrival_ms,
                        completion_ms=completion_ms,
                    )
                )
        unassigned = sorted(set(payload.task_data.task_ids).difference(visited))
        mandatory_missing = sorted(set(unassigned).difference(optional_ids))
        if mandatory_missing:
            return OptimizerResult(
                backend="ortools",
                status="infeasible",
                optimizer="ortools-routing",
                routes=routes,
                unassigned_task_ids=unassigned,
                reason=f"Mandatory task rows were not assigned: {mandatory_missing}",
            )
        completions = [route.completion_ms for route in routes if route.completion_ms is not None]
        return OptimizerResult(
            backend="ortools",
            status="success",
            optimizer="ortools-routing",
            global_objective_cost=float(solution.ObjectiveValue()),
            estimated_makespan_ms=max(completions) if completions else 0.0,
            routes=routes,
            unassigned_task_ids=unassigned,
        )


class CuOptNativeRequestBuilder:
    """Convert the validated LARO payload to NVIDIA cuOpt native JSON.

    Warehouses and factories are represented as a directed waypoint graph.
    The adapter therefore serializes the existing LARO edge list into cuOpt's
    CSR waypoint-graph contract instead of expanding it to dense N x N
    matrices.  This preserves the full rack-access route topology while keeping Build
    API requests compact enough for normal inline submission in most runs.
    """

    @staticmethod
    def _csr(
        *,
        location_count: int,
        from_indices: list[int],
        to_indices: list[int],
        weights: list[float],
    ) -> dict[str, list[int] | list[float]]:
        """Return one deterministic CSR graph for NVIDIA cuOpt."""

        adjacency: list[list[tuple[int, float]]] = [[] for _ in range(location_count)]
        for source, target, weight in zip(from_indices, to_indices, weights, strict=True):
            if not 0 <= source < location_count or not 0 <= target < location_count:
                raise ValueError(f"Waypoint edge {source}->{target} is outside the location index.")
            if float(weight) < 0:
                raise ValueError("cuOpt waypoint weights must be non-negative.")
            adjacency[source].append((target, float(weight)))

        offsets: list[int] = [0]
        edges: list[int] = []
        csr_weights: list[float] = []
        for values in adjacency:
            # Stable ordering makes payload hashes and tests reproducible.
            for target, weight in sorted(values, key=lambda value: (value[0], value[1])):
                edges.append(target)
                csr_weights.append(weight)
            offsets.append(len(edges))
        return {"offsets": offsets, "edges": edges, "weights": csr_weights}

    def build(self, payload: CuOptPayload) -> dict:
        """Return one native NVIDIA routing problem dictionary."""

        if any(value is not None for value in payload.task_data.fixed_vehicle_ids):
            raise ValueError(
                "The v13.20 native cuOpt adapter does not silently translate fixed_vehicle_ids. "
                "Use a compatibility-capable adapter or remove the fixed assignment."
            )
        if payload.task_data.optional_task_ids:
            raise ValueError(
                "The v13.20 native cuOpt adapter does not silently translate optional task penalties. "
                "Use OR-Tools for optional-task experiments or add an explicitly validated prize policy."
            )

        graph = payload.waypoint_graph_data
        location_count = len(payload.location_index_map)
        cost_graph = self._csr(
            location_count=location_count,
            from_indices=list(graph.from_indices),
            to_indices=list(graph.to_indices),
            weights=[float(value) for value in graph.costs],
        )
        travel_graph = self._csr(
            location_count=location_count,
            from_indices=list(graph.from_indices),
            to_indices=list(graph.to_indices),
            weights=[float(value) for value in graph.travel_times_ms],
        )
        service_times = _normalized_service_times(payload)
        vehicle_available_at_ms = _normalized_vehicle_available_at_ms(payload)
        horizon = max(
            1,
            max(vehicle_available_at_ms, default=0)
            + int(
                sum(payload.waypoint_graph_data.travel_times_ms)
                * max(1, len(payload.task_data.task_ids))
            ),
        )
        return {
            "cost_waypoint_graph_data": {"waypoint_graph": {"0": cost_graph}},
            "travel_time_waypoint_graph_data": {"waypoint_graph": {"0": travel_graph}},
            "task_data": {
                "task_ids": list(payload.task_data.task_ids),
                "task_locations": list(payload.task_data.task_locations),
                "demand": [list(payload.task_data.demand)],
                "pickup_and_delivery_pairs": [
                    list(value) for value in payload.task_data.pickup_and_delivery_pairs
                ],
                "service_times": service_times,
            },
            "fleet_data": {
                "vehicle_ids": list(payload.fleet_data.vehicle_ids),
                "vehicle_types": [0 for _ in payload.fleet_data.vehicle_ids],
                "vehicle_locations": [
                    [start, end]
                    for start, end in zip(
                        payload.fleet_data.vehicle_start_locations,
                        payload.fleet_data.vehicle_end_locations
                        or payload.fleet_data.vehicle_start_locations,
                        strict=True,
                    )
                ],
                "capacities": [list(payload.fleet_data.capacities)],
                **(
                    {"min_vehicles": int(payload.fleet_data.min_vehicles)}
                    if payload.fleet_data.min_vehicles > 0
                    else {}
                ),
                "vehicle_time_windows": [
                    [int(available_at_ms), horizon]
                    for available_at_ms in vehicle_available_at_ms
                ],
                "skip_first_trips": list(payload.fleet_data.skip_first_trips)
                or [False for _ in payload.fleet_data.vehicle_ids],
                "drop_return_trips": list(payload.fleet_data.drop_return_trips)
                or [True for _ in payload.fleet_data.vehicle_ids],
            },
            "solver_config": {
                "time_limit": int(payload.time_limit_seconds),
                "objectives": _native_objectives(payload.objective_profile),
            },
        }


class CuOptNativeResponseParser:
    """Map NVIDIA's routing response back to LARO's typed optimizer result."""

    @staticmethod
    def _task_id(value: object, payload: CuOptPayload) -> str | None:
        """Resolve a native task row or index to the original LARO task ID."""

        if value in {None, "Depot", "Break", "w"}:
            return None
        text = str(value)
        if text in payload.task_data.task_ids:
            return text
        try:
            index = int(text)
        except (TypeError, ValueError):
            return None
        return payload.task_data.task_ids[index] if 0 <= index < len(payload.task_data.task_ids) else None

    @staticmethod
    def _numeric(value: object) -> float | None:
        """Return one finite numeric provider value without guessing strings."""

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        return numeric if isfinite(numeric) else None

    @classmethod
    def _objective_metrics(cls, raw: object) -> list[OptimizerObjectiveMetric]:
        """Normalize provider objective components without conflating route timing."""

        metrics: list[OptimizerObjectiveMetric] = []
        if isinstance(raw, dict):
            for name, value in raw.items():
                numeric = cls._numeric(value)
                if numeric is not None:
                    metrics.append(OptimizerObjectiveMetric(name=str(name), value=numeric))
        elif isinstance(raw, list):
            for value in raw:
                if not isinstance(value, dict):
                    continue
                name = value.get("name") or value.get("objective") or value.get("key")
                numeric = cls._numeric(value.get("value"))
                if name is not None and numeric is not None:
                    metrics.append(OptimizerObjectiveMetric(name=str(name), value=numeric))
        return metrics

    def _route_timing(
        self,
        *,
        data: dict,
        payload: CuOptPayload,
    ) -> tuple[list[str], list[float | None], float | None, float | None, list[str]]:
        """Return task IDs and arrival/completion times aligned to real task rows.

        NVIDIA's ``arrival_stamp`` is aligned with the compact ``task_id`` list,
        which may contain a leading or trailing Depot. Depot values are ignored;
        completion adds the last real task's service time. This remains correct
        if a future closed-route response appends a final Depot arrival.
        """

        raw_task_ids = list(data.get("task_id") or [])
        raw_arrivals = list(data.get("arrival_stamp") or [])
        warnings: list[str] = []
        if raw_arrivals and len(raw_task_ids) != len(raw_arrivals):
            warnings.append(
                "cuOpt task_id and arrival_stamp lengths differ: "
                f"{len(raw_task_ids)} != {len(raw_arrivals)}."
            )

        sequence: list[str] = []
        task_arrivals: list[float | None] = []
        for position, raw_task_id in enumerate(raw_task_ids):
            task_id = self._task_id(raw_task_id, payload)
            if task_id is None:
                continue
            arrival = (
                self._numeric(raw_arrivals[position])
                if position < len(raw_arrivals)
                else None
            )
            sequence.append(task_id)
            task_arrivals.append(arrival)

        last_task_arrival_ms = task_arrivals[-1] if task_arrivals else None
        completion_ms: float | None = None
        if sequence and last_task_arrival_ms is not None:
            service_time_by_task_id = dict(
                zip(
                    payload.task_data.task_ids,
                    _normalized_service_times(payload),
                    strict=True,
                )
            )
            completion_ms = float(
                last_task_arrival_ms
                + service_time_by_task_id.get(sequence[-1], 0)
            )
        elif sequence:
            warnings.append("cuOpt returned task assignments without usable arrival stamps.")

        return sequence, task_arrivals, last_task_arrival_ms, completion_ms, warnings

    def parse(self, raw: dict, payload: CuOptPayload) -> OptimizerResult:
        """Parse synchronous or polled server output conservatively.

        NVIDIA returns feasible routes under ``solver_response`` and may return
        infeasibility diagnostics under ``solver_infeasible_response``.  The
        previous parser treated a missing status as success and therefore
        converted an infeasible response into ``status=success, routes=[]``.
        v13.5 never treats an absent response section as a valid solution.
        """

        if "backend" in raw and "status" in raw:
            normalized = OptimizerResult.model_validate(raw)
            return (
                normalized
                if normalized.backend == "cuopt"
                else normalized.model_copy(update={"backend": "cuopt"})
            )

        response = raw.get("response", raw)
        if not isinstance(response, dict):
            return OptimizerResult(
                backend="cuopt",
                status="failed",
                optimizer="nvidia-cuopt",
                reason="cuOpt response is not a JSON object.",
                errors=["CUOPT_RESPONSE_NOT_OBJECT"],
            )

        infeasible = response.get("solver_infeasible_response")
        if isinstance(infeasible, dict):
            reason = (
                infeasible.get("error")
                or infeasible.get("message")
                or infeasible.get("status")
                or infeasible
            )
            return OptimizerResult(
                backend="cuopt",
                status="infeasible",
                optimizer="nvidia-cuopt",
                reason=(
                    reason
                    if isinstance(reason, str)
                    else json.dumps(reason, ensure_ascii=False, default=str)
                ),
                errors=["CUOPT_INFEASIBLE"],
            )

        solver_response = response.get("solver_response")
        if not isinstance(solver_response, dict):
            # Some self-hosted adapters return the solver payload directly.
            # Accept that only when it contains unmistakable solver fields.
            direct_fields = {"vehicle_data", "dropped_tasks", "solution_cost", "status"}
            if direct_fields.intersection(response):
                solver_response = response
            else:
                return OptimizerResult(
                    backend="cuopt",
                    status="failed",
                    optimizer="nvidia-cuopt",
                    reason=(
                        "cuOpt response contains neither solver_response nor "
                        "solver_infeasible_response."
                    ),
                    errors=["CUOPT_RESPONSE_SECTION_MISSING"],
                )

        native_status = solver_response.get("status")
        if native_status not in {0, "0", "success", "SUCCESS"}:
            status_text = str(native_status).casefold()
            is_infeasible = status_text in {"1", "infeasible", "no_solution"}
            return OptimizerResult(
                backend="cuopt",
                status="infeasible" if is_infeasible else "failed",
                optimizer="nvidia-cuopt",
                reason=str(
                    solver_response.get("error")
                    or solver_response.get("message")
                    or native_status
                ),
                errors=["CUOPT_INFEASIBLE" if is_infeasible else "CUOPT_SOLVER_FAILED"],
            )

        routes: list[OptimizerRoute] = []
        parser_warnings: list[str] = []
        vehicle_data = solver_response.get("vehicle_data", {})
        if isinstance(vehicle_data, list):
            vehicle_data = {str(index): value for index, value in enumerate(vehicle_data)}
        if not isinstance(vehicle_data, dict):
            vehicle_data = {}
        for native_vehicle, data in vehicle_data.items():
            if not isinstance(data, dict):
                continue
            try:
                vehicle_index = int(native_vehicle)
            except (TypeError, ValueError):
                vehicle_index = -1
            vehicle_id = (
                payload.fleet_data.vehicle_ids[vehicle_index]
                if 0 <= vehicle_index < len(payload.fleet_data.vehicle_ids)
                else str(native_vehicle)
            )
            (
                sequence,
                task_arrivals_ms,
                last_task_arrival_ms,
                completion_ms,
                timing_warnings,
            ) = self._route_timing(data=data, payload=payload)
            parser_warnings.extend(
                f"{vehicle_id}: {warning}" for warning in timing_warnings
            )
            if sequence:
                routes.append(
                    OptimizerRoute(
                        vehicle_id=vehicle_id,
                        task_sequence=sequence,
                        route_cost=self._numeric(data.get("route_cost")),
                        task_arrival_stamps_ms=task_arrivals_ms,
                        last_task_arrival_ms=last_task_arrival_ms,
                        completion_ms=completion_ms,
                    )
                )

        global_objective_cost = self._numeric(solver_response.get("solution_cost"))
        objective_values = self._objective_metrics(
            solver_response.get("objective_values", {})
        )
        completion_values = [
            route.completion_ms
            for route in routes
            if route.completion_ms is not None
        ]
        estimated_makespan_ms = (
            max(completion_values)
            if completion_values
            else None
        )

        dropped = solver_response.get("dropped_tasks", {})
        dropped_values: list[object] = []
        if isinstance(dropped, dict):
            dropped_values = list(dropped.get("task_id", [])) or list(
                dropped.get("task_index", [])
            )
        elif isinstance(dropped, list):
            dropped_values = dropped
        unassigned = [
            task_id
            for value in dropped_values
            if (task_id := self._task_id(value, payload)) is not None
        ]
        unassigned = list(dict.fromkeys(unassigned))

        mandatory = set(payload.task_data.task_ids).difference(
            payload.task_data.optional_task_ids
        )
        assigned = {task_id for route in routes for task_id in route.task_sequence}
        missing_mandatory = sorted(mandatory.difference(assigned))
        if missing_mandatory:
            return OptimizerResult(
                backend="cuopt",
                status="infeasible" if not routes else "failed",
                optimizer="nvidia-cuopt",
                global_objective_cost=global_objective_cost,
                objective_values=objective_values,
                estimated_makespan_ms=estimated_makespan_ms,
                routes=routes,
                unassigned_task_ids=sorted(set(unassigned).union(missing_mandatory)),
                reason=(
                    "cuOpt returned no complete mandatory-task coverage: "
                    f"{missing_mandatory}"
                ),
                errors=["EMPTY_OR_INCOMPLETE_SUCCESS_RESPONSE"],
                warnings=parser_warnings,
            )

        return OptimizerResult(
            backend="cuopt",
            status="success",
            optimizer="nvidia-cuopt",
            global_objective_cost=global_objective_cost,
            objective_values=objective_values,
            estimated_makespan_ms=estimated_makespan_ms,
            routes=routes,
            unassigned_task_ids=unassigned,
            warnings=parser_warnings,
        )



class CuOptPublicAPIError(RuntimeError):
    """Structured failure returned by the public NVIDIA cuOpt endpoint."""

    def __init__(self, *, status_code: int, response_body: object) -> None:
        self.status_code = int(status_code)
        self.response_body = response_body
        compact = json.dumps(response_body, ensure_ascii=False, default=str)
        if len(compact) > 4000:
            compact = compact[:4000] + "..."
        super().__init__(f"NVIDIA cuOpt HTTP {self.status_code}: {compact}")


class ExternalCuOptGateway:
    """Submit validated routing data to self-hosted, thin-client, or Build API cuOpt."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.builder = CuOptNativeRequestBuilder()
        self.parser = CuOptNativeResponseParser()

    def _headers(self) -> dict[str, str]:
        """Return headers for a self-hosted HTTP endpoint or private gateway."""

        headers = {"Content-Type": "application/json", "CLIENT-VERSION": "laro-v13.20"}
        key = self.settings.cuopt_http_api_key
        mode = self.settings.cuopt_http_auth_mode.casefold()
        if key and mode == "bearer":
            headers[self.settings.cuopt_http_api_key_header] = (
                key if key.lower().startswith("bearer ") else f"Bearer {key}"
            )
        elif key and mode in {"x-api-key", "api-key", "header"}:
            headers[self.settings.cuopt_http_api_key_header] = key
        return headers

    def _nvidia_headers(self) -> dict[str, str]:
        """Return direct NVIDIA Build/API Catalog authorization headers."""

        key = self.settings.nvidia_build_api_key
        if not key:
            raise ValueError(
                "NVIDIA_API_KEY is required for CUOPT_TRANSPORT=nvidia_api. "
                "Use the key generated on build.nvidia.com."
            )
        token = key if key.lower().startswith("bearer ") else f"Bearer {key}"
        return {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _request_id(raw: dict) -> str | None:
        value = raw.get("requestId") or raw.get("reqId")
        return str(value) if value else None

    def _solution_url(self, req_id: str) -> str:
        template = self.settings.cuopt_solution_url_template
        if template:
            return template.format(req_id=req_id, reqId=req_id, requestId=req_id)
        parts = urlsplit(self.settings.cuopt_api_url)
        path = parts.path
        if path.endswith("/request"):
            path = path[: -len("/request")] + f"/solution/{req_id}"
        else:
            path = path.rstrip("/") + f"/solution/{req_id}"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    @staticmethod
    def _response_json(response: object) -> dict:
        raw = response.json()  # type: ignore[attr-defined]
        if not isinstance(raw, dict):
            raise ValueError("cuOpt returned a non-object JSON response.")
        return raw

    @staticmethod
    def _response_body(response: object) -> object:
        """Return a bounded provider error body without exposing request secrets."""

        try:
            value = response.json()  # type: ignore[attr-defined]
        except Exception:
            value = str(getattr(response, "text", ""))[:4000]
        return value

    def _raise_nvidia_error(self, response: object) -> None:
        """Raise a structured public-API error containing NVIDIA's diagnosis."""

        raise CuOptPublicAPIError(
            status_code=int(getattr(response, "status_code")),
            response_body=self._response_body(response),
        )

    def _download_response_reference(self, client: httpx.Client, raw: dict) -> dict:
        """Download and decode a large NVCF response reference when present."""

        reference = raw.get("responseReference")
        if not reference:
            return raw
        response = client.get(str(reference))
        response.raise_for_status()
        content = bytes(response.content)
        if content.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                candidates = [name for name in archive.namelist() if not name.endswith("/")]
                if not candidates:
                    raise ValueError("NVIDIA response archive is empty.")
                content = archive.read(candidates[0])
        decoded = json.loads(content.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("NVIDIA response reference did not contain a JSON object.")
        return decoded

    def _poll_nvidia_api(self, client: httpx.Client, req_id: str) -> dict:
        """Poll the public cuOpt status endpoint until a terminal response is returned."""

        for _ in range(self.settings.cuopt_max_poll_attempts):
            time.sleep(self.settings.cuopt_poll_interval_seconds)
            response = client.get(self._solution_url(req_id), headers=self._nvidia_headers())
            if response.status_code == 202:
                continue
            if response.status_code != 200:
                self._raise_nvidia_error(response)
            raw = self._response_json(response)
            return self._download_response_reference(client, raw)
        raise TimeoutError(f"Timed out polling NVIDIA cuOpt request {req_id}.")

    def _upload_large_asset(self, client: httpx.Client, data: bytes) -> str:
        """Upload oversized cuOpt JSON through the official NVCF asset API."""

        description = "LARO-cuOpt-routing-problem"
        response = client.post(
            self.settings.cuopt_asset_api_url,
            json={"contentType": "application/json", "description": description},
            headers=self._nvidia_headers(),
        )
        response.raise_for_status()
        raw = self._response_json(response)
        asset_id = raw.get("assetId")
        upload_url = raw.get("uploadUrl")
        if not asset_id or not upload_url:
            raise ValueError("NVCF asset creation response lacks assetId or uploadUrl.")
        upload = client.put(
            str(upload_url),
            content=data,
            headers={
                "Content-Type": "application/json",
                "x-amz-meta-nvcf-asset-description": description,
            },
        )
        upload.raise_for_status()
        return str(asset_id)

    def _delete_large_asset(self, client: httpx.Client, asset_id: str) -> None:
        """Best-effort cleanup for an NVCF input asset."""

        if not self.settings.cuopt_delete_asset_after_solve:
            return
        try:
            response = client.delete(
                f"{self.settings.cuopt_asset_api_url.rstrip('/')}/{asset_id}",
                headers=self._nvidia_headers(),
            )
            if response.status_code not in {200, 202, 204, 404}:
                response.raise_for_status()
        except Exception:
            # Cleanup must not overwrite a valid solver result. NVCF also
            # garbage-collects assets, but the normal path attempts deletion.
            return

    def _nvidia_api_solve(self, native_request: dict, payload: CuOptPayload) -> OptimizerResult:
        """Call the public Build/API Catalog cuOpt endpoint with a Build API key."""

        if self.settings.cuopt_payload_format.casefold() != "native":
            raise ValueError("The NVIDIA Build API transport requires CUOPT_PAYLOAD_FORMAT=native.")
        compact = json.dumps(native_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # Resolve credentials before creating any network client.  Missing-key
        # tests and misconfigured runs therefore fail locally with zero I/O.
        headers = self._nvidia_headers()
        asset_id: str | None = None
        timeout = float(max(payload.time_limit_seconds + 30, 60))
        with httpx.Client(verify=self.settings.cuopt_verify_ssl, timeout=timeout) as client:
            try:
                data: dict | None = native_request
                if len(compact) > self.settings.cuopt_inline_limit_bytes:
                    asset_id = self._upload_large_asset(client, compact)
                    headers = {
                        **headers,
                        "NVCF-INPUT-ASSET-REFERENCES": asset_id,
                    }
                    data = None
                # The deployed Build/API Catalog schema currently rejects a
                # top-level ``parameters`` field even though older reference
                # documentation describes it as ignored.  Send only fields
                # accepted by the live endpoint.
                envelope = {
                    "action": self.settings.cuopt_action,
                    "data": data,
                    "client_version": self.settings.cuopt_client_version,
                }
                response = client.post(
                    self.settings.cuopt_api_url,
                    json=envelope,
                    headers=headers,
                )
                if response.status_code not in {200, 202}:
                    self._raise_nvidia_error(response)
                raw = self._response_json(response)
                req_id = self._request_id(raw)
                if response.status_code == 202 or (req_id and "response" not in raw and "solver_response" not in raw):
                    if not req_id:
                        raise ValueError("NVIDIA cuOpt returned 202 without requestId.")
                    raw = self._poll_nvidia_api(client, req_id)
                else:
                    raw = self._download_response_reference(client, raw)
                return self.parser.parse(raw, payload)
            finally:
                if asset_id:
                    self._delete_large_asset(client, asset_id)

    def _http_solve(self, native_request: dict, payload: CuOptPayload) -> OptimizerResult:
        """Call a self-hosted cuOpt server or private HTTP gateway."""

        timeout = float(max(payload.time_limit_seconds + 10, 30))
        with httpx.Client(verify=self.settings.cuopt_verify_ssl, timeout=timeout) as client:
            response = client.post(
                self.settings.cuopt_api_url,
                json=native_request,
                headers=self._headers(),
            )
            response.raise_for_status()
            raw = self._response_json(response)
            req_id = self._request_id(raw)
            if req_id and "response" not in raw and "solver_response" not in raw:
                for _ in range(self.settings.cuopt_max_poll_attempts):
                    time.sleep(self.settings.cuopt_poll_interval_seconds)
                    poll = client.get(self._solution_url(req_id), headers=self._headers())
                    if poll.status_code == 202:
                        continue
                    poll.raise_for_status()
                    raw = self._response_json(poll)
                    break
                else:
                    return OptimizerResult(
                        backend="cuopt",
                        status="unavailable",
                        optimizer="nvidia-cuopt-http",
                        reason=f"Timed out polling cuOpt request {req_id}.",
                    )
        return self.parser.parse(raw, payload)

    def _repoll_managed(self, client: object, raw: dict) -> dict:
        """Repoll a legacy NVIDIA managed request when only a request id is returned."""

        req_id = self._request_id(raw)
        if not req_id or "response" in raw:
            return raw
        repoll = getattr(client, "repoll", None)
        if repoll is None:
            raise RuntimeError("Managed cuOpt client returned a request id but exposes no repoll method.")
        for _ in range(self.settings.cuopt_max_poll_attempts):
            time.sleep(self.settings.cuopt_poll_interval_seconds)
            raw = repoll(req_id, response_type="dict")
            if not isinstance(raw, dict):
                raise ValueError("Managed cuOpt repoll returned a non-dictionary response.")
            if "response" in raw:
                return raw
        raise TimeoutError(f"Timed out polling managed cuOpt request {req_id}.")

    def _managed_solve(self, native_request: dict, payload: CuOptPayload) -> OptimizerResult:
        """Retain the older thin-client transport for existing deployments."""

        try:
            from cuopt_thin_client import CuOptServiceClient
        except Exception as exc:  # pragma: no cover - optional NVIDIA package
            raise RuntimeError(
                "Install the NVIDIA cuopt_thin_client package for CUOPT_TRANSPORT=managed."
            ) from exc

        sak = self.settings.effective_cuopt_client_sak
        if sak:
            if not self.settings.cuopt_function_id:
                raise ValueError("CUOPT_FUNCTION_ID is required with CUOPT_CLIENT_SAK.")
            client = CuOptServiceClient(
                sak=sak,
                function_id=self.settings.cuopt_function_id,
                timeout_exception=False,
            )
        elif self.settings.cuopt_client_id and self.settings.cuopt_client_secret:
            client = CuOptServiceClient(
                client_id=self.settings.cuopt_client_id,
                client_secret=self.settings.cuopt_client_secret,
            )
        else:
            raise ValueError(
                "Managed transport requires CUOPT_CLIENT_SAK + CUOPT_FUNCTION_ID "
                "or legacy CUOPT_CLIENT_ID + CUOPT_CLIENT_SECRET."
            )

        raw = client.get_optimized_routes(native_request)
        if not isinstance(raw, dict):
            raise ValueError("Managed cuOpt client returned a non-dictionary response.")
        raw = self._repoll_managed(client, raw)
        return self.parser.parse(raw, payload)

    def health_check(self) -> dict:
        """Return a non-secret connection diagnostic for setup scripts."""

        transport = self.settings.cuopt_transport.casefold()
        if transport == "nvidia_api":
            return {
                "transport": "nvidia_api",
                "configured": self.settings.cuopt_nvidia_api_configured,
                "api_url": self.settings.cuopt_api_url,
                "status_url_template": self.settings.cuopt_solution_url_template,
                "action": self.settings.cuopt_action,
                "client_version": self.settings.cuopt_client_version,
                "api_key_configured": bool(self.settings.nvidia_build_api_key),
                "large_asset_fallback": True,
                "inline_limit_bytes": self.settings.cuopt_inline_limit_bytes,
            }
        if transport == "managed":
            auth_style = (
                "identity_federation_sak"
                if self.settings.effective_cuopt_client_sak
                else "legacy_client_id_secret"
                if self.settings.cuopt_client_id or self.settings.cuopt_client_secret
                else "none"
            )
            return {
                "transport": "managed",
                "configured": self.settings.cuopt_managed_credentials_configured,
                "auth_style": auth_style,
                "function_id_configured": bool(self.settings.cuopt_function_id),
                "client_package_required": "cuopt_thin_client",
            }
        url = self.settings.cuopt_health_url
        if not url:
            parts = urlsplit(self.settings.cuopt_api_url)
            path = parts.path
            if path.endswith("/request"):
                path = path[: -len("/request")] + "/health"
            else:
                path = path.rstrip("/") + "/health"
            url = urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
        with httpx.Client(verify=self.settings.cuopt_verify_ssl, timeout=10.0) as client:
            response = client.get(url, headers=self._headers())
            return {
                "transport": "http",
                "url": url,
                "status_code": response.status_code,
                "ok": response.is_success,
                "body_preview": response.text[:300],
            }

    def solve(self, payload: CuOptPayload) -> OptimizerResult:
        """Call the configured service with explicit transport and no solver fallback."""

        # A rolling replan may preserve an already-picked operation on the old
        # plan until its safe handover, leaving no new business task for the
        # optimizer. NVIDIA cuOpt rejects an empty task_locations vector. The
        # following terminal-relocation stage still has to append the robot's
        # PARK/CHARGE goal, so represent the empty assignment problem as a
        # successful local result instead of sending an invalid HTTP request.
        if not payload.task_data.task_ids:
            return OptimizerResult(
                backend="cuopt",
                status="success",
                optimizer="nvidia-cuopt-empty-task-bypass",
                global_objective_cost=0.0,
                estimated_makespan_ms=0.0,
                routes=[],
                reason=(
                    "No new business task after preserving committed work; "
                    "terminal relocation will append PARK/CHARGE goals."
                ),
            )
        try:
            if self.settings.cuopt_payload_format.casefold() == "internal":
                native_request = payload.model_dump(mode="json")
            else:
                native_request = self.builder.build(payload)
            transport = self.settings.cuopt_transport.casefold()
            if transport == "nvidia_api":
                return self._nvidia_api_solve(native_request, payload)
            if transport == "managed":
                return self._managed_solve(native_request, payload)
            return self._http_solve(native_request, payload)
        except CuOptPublicAPIError as exc:
            return OptimizerResult(
                backend="cuopt",
                status="unavailable",
                optimizer="nvidia-cuopt-api",
                reason=str(exc),
                errors=[f"cuopt_http_{exc.status_code}"],
            )
        except Exception as exc:
            return OptimizerResult(
                backend="cuopt",
                status="unavailable",
                optimizer="external-cuopt",
                reason=f"{type(exc).__name__}: {exc}",
                errors=["cuopt_transport_error"],
            )


class OptimizerAssignmentValidator:
    """Validate task coverage, fixed robot constraints, and pickup-before-drop order."""

    def validate(self, *, payload: CuOptPayload, result: OptimizerResult) -> OptimizerAssignmentValidation:
        """Return an independent assignment contract verdict."""

        errors: list[str] = []
        warnings: list[str] = []
        if result.status != "success":
            return OptimizerAssignmentValidation(valid=False, errors=[result.reason or result.status])
        optional = set(payload.task_data.optional_task_ids)
        required = set(payload.task_data.task_ids).difference(optional)
        assigned: dict[str, tuple[str, int]] = {}
        valid_vehicles = set(payload.fleet_data.vehicle_ids)
        for route in result.routes:
            if route.vehicle_id not in valid_vehicles:
                errors.append(f"Unknown vehicle {route.vehicle_id}.")
            for position, task_id in enumerate(route.task_sequence):
                if task_id in assigned:
                    errors.append(f"Task {task_id} was assigned more than once.")
                assigned[task_id] = (route.vehicle_id, position)
        missing = sorted(required.difference(assigned))
        unknown = sorted(set(assigned).difference(set(payload.task_data.task_ids)))
        if missing:
            errors.append(f"Missing mandatory task ids: {missing}")
        if unknown:
            errors.append(f"Unknown task ids: {unknown}")
        optional_missing = sorted(optional.difference(assigned))
        undeclared_unassigned = sorted(set(result.unassigned_task_ids).difference(payload.task_data.task_ids))
        if undeclared_unassigned:
            errors.append(f"Optimizer reported unknown unassigned task ids: {undeclared_unassigned}")
        if set(optional_missing) != set(result.unassigned_task_ids):
            # A solver may report both rows of an optional pair; the sets must still agree.
            errors.append(
                "Unassigned optional task ids do not match optimizer declaration: "
                f"expected={optional_missing}, actual={sorted(result.unassigned_task_ids)}"
            )
        for pickup, delivery in payload.task_data.pickup_and_delivery_pairs:
            pickup_id = payload.task_data.task_ids[pickup]
            delivery_id = payload.task_data.task_ids[delivery]
            if pickup_id not in assigned or delivery_id not in assigned:
                continue
            pickup_robot, pickup_position = assigned[pickup_id]
            delivery_robot, delivery_position = assigned[delivery_id]
            if pickup_robot != delivery_robot:
                errors.append(f"{pickup_id} and {delivery_id} use different robots.")
            if pickup_position >= delivery_position:
                errors.append(f"{delivery_id} occurs before {pickup_id}.")
            fixed = payload.task_data.fixed_vehicle_ids[pickup]
            if fixed is not None and pickup_robot != fixed:
                errors.append(f"Task {pickup_id} must remain on robot {fixed}.")
        used_vehicle_ids = {
            route.vehicle_id for route in result.routes if route.task_sequence
        }
        if len(used_vehicle_ids) < int(payload.fleet_data.min_vehicles):
            errors.append(
                "Optimizer used fewer vehicles than the required minimum: "
                f"required={payload.fleet_data.min_vehicles}, actual={len(used_vehicle_ids)}."
            )
        max_g2p_cycles = payload.fleet_data.max_g2p_cycles_per_vehicle
        if max_g2p_cycles is not None:
            for route in result.routes:
                cycle_count = sum(
                    task_id.startswith("G2P-") and task_id.endswith("_PICK")
                    for task_id in route.task_sequence
                )
                if cycle_count > max_g2p_cycles:
                    errors.append(
                        f"Robot {route.vehicle_id} received {cycle_count} G2P cycle(s); "
                        f"maximum is {max_g2p_cycles}."
                    )
        if not result.routes:
            warnings.append("Optimizer returned no routes.")
        return OptimizerAssignmentValidation(valid=not errors, errors=errors, warnings=warnings)

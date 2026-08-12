"""Deterministic terminal PARK/CHARGE policies for rolling-horizon replans."""
from __future__ import annotations

from math import ceil, inf

from app.core.config import get_settings
from app.domain.schemas import (
    CuOptPayload,
    FleetData,
    OptimizationRequest,
    OptimizationVehicle,
    OptimizerResult,
    OptimizerRoute,
    RobotRuntime,
    RobotRuntimeContext,
    RuntimePlanningOverrides,
    TaskData,
    TerminalPolicy,
    TerminalRelocationRecord,
    TerminalRelocationResult,
)
from app.services.graph_service import DirectedGraphService


def charge_service_duration_ms(
    *,
    battery_pct: float,
    charge_rate_pct_per_minute: float,
    minimum_service_ms: int,
) -> int:
    """Reserve enough station time to reach 100% at the nominal charge rate."""

    required_ms = ceil(
        max(0.0, 100.0 - float(battery_pct))
        / float(charge_rate_pct_per_minute)
        * 60_000
    )
    return max(int(minimum_service_ms), required_ms)


class RobotTerminalPolicyService:
    """Assign one reachable terminal node to old-plan robots being released."""

    @staticmethod
    def _nearest_target(
        *,
        start_node: str,
        policy: TerminalPolicy,
        graph_arcs: list[dict],
        node_types: dict[str, str],
    ) -> str | None:
        target_type = "charging_slot" if policy == "CHARGE" else "route_charge_junction"
        candidates = sorted(
            node_id for node_id, node_type in node_types.items() if node_type == target_type
        )
        graph = DirectedGraphService(graph_arcs)
        scored: list[tuple[float, str]] = []
        for node_id in candidates:
            value, _ = graph.shortest_path(start_node, node_id, metric="travel_time")
            if value != inf:
                scored.append((value, node_id))
        return min(scored, default=(inf, ""))[1] or None

    def policy_for_robot(
        self,
        *,
        robot: RobotRuntime | OptimizationVehicle,
        graph_arcs: list[dict],
        node_types: dict[str, str],
    ) -> tuple[TerminalPolicy, str]:
        settings = get_settings()
        start_node = robot.current_node if isinstance(robot, RobotRuntime) else robot.start_node
        if not start_node or not settings.idle_robot_relocation_enabled:
            return "STAY", start_node or ""
        policy: TerminalPolicy = (
            "CHARGE"
            if float(robot.battery_pct) < settings.robot_opportunistic_charge_threshold_pct
            else settings.robot_default_terminal_policy  # type: ignore[assignment]
        )
        if policy == "STAY":
            return policy, start_node
        target = self._nearest_target(
            start_node=start_node,
            policy=policy,
            graph_arcs=graph_arcs,
            node_types=node_types,
        )
        return (policy, target) if target else ("STAY", start_node)

    def apply_to_request(
        self,
        *,
        request: OptimizationRequest,
        runtime_overrides: RuntimePlanningOverrides | None,
        graph_arcs: list[dict],
        node_types: dict[str, str],
    ) -> OptimizationRequest:
        runtime_overrides = runtime_overrides or RuntimePlanningOverrides()
        relocate_ids = set(runtime_overrides.relocate_idle_robot_ids)
        if not relocate_ids:
            return request
        vehicles: list[OptimizationVehicle] = []
        for vehicle in request.vehicles:
            # A BE-assigned charging home is authoritative. A rolling-horizon
            # relocation hint must not replace it with another nearby slot.
            if vehicle.end_node:
                vehicles.append(vehicle)
                continue
            if vehicle.robot_id not in relocate_ids:
                vehicles.append(vehicle)
                continue
            policy, target = self.policy_for_robot(
                robot=vehicle,
                graph_arcs=graph_arcs,
                node_types=node_types,
            )
            vehicles.append(
                vehicle.model_copy(
                    update={
                        "terminal_policy": policy,
                        "end_node": target,
                    }
                )
            )
        return request.model_copy(update={"vehicles": vehicles})


class TerminalRelocationEnricher:
    """Append execution-visible terminal goals after solver assignment.

    cuOpt/OR-Tools account for a used vehicle's configured end node when they
    optimize assignment and ordering, but the normalized ``OptimizerRoute``
    intentionally contains only business task ids.  Append that contracted end
    node as an execution-only goal so MAPF can materialize the final drive.

    Old-plan robots omitted from the business assignment also receive an
    execution-only route when rolling-horizon relocation requests one.
    """

    @staticmethod
    def _last_route_node(
        route: OptimizerRoute,
        payload: CuOptPayload,
        start_node: str,
    ) -> str:
        reverse = {value: key for key, value in payload.location_index_map.items()}
        location_by_task = {
            task_id: reverse[index]
            for task_id, index in zip(
                payload.task_data.task_ids,
                payload.task_data.task_locations,
                strict=True,
            )
        }
        return location_by_task.get(route.task_sequence[-1], start_node) if route.task_sequence else start_node

    def enrich(
        self,
        *,
        payload: CuOptPayload,
        result: OptimizerResult,
        request: OptimizationRequest,
        robot_context: RobotRuntimeContext,
        runtime_overrides: RuntimePlanningOverrides,
        graph_arcs: list[dict],
        node_types: dict[str, str],
    ) -> tuple[CuOptPayload, OptimizerResult, TerminalRelocationResult]:
        settings = get_settings()
        relocate_ids = set(runtime_overrides.relocate_idle_robot_ids)
        policy_service = RobotTerminalPolicyService()
        request_vehicle = {value.robot_id: value for value in request.vehicles}
        runtime_robot = {value.robot_id: value for value in robot_context.robots}
        route_by_robot = {value.vehicle_id: value for value in result.routes}

        fleet = payload.fleet_data
        vehicle_ids = list(fleet.vehicle_ids)
        starts = list(fleet.vehicle_start_locations)
        ends = list(fleet.vehicle_end_locations) or list(starts)
        capacities = list(fleet.capacities)
        availability = list(fleet.vehicle_available_at_ms) or [0 for _ in vehicle_ids]
        skip_first = list(fleet.skip_first_trips) or [False for _ in vehicle_ids]
        drop_return = list(fleet.drop_return_trips) or [True for _ in vehicle_ids]
        reverse_locations = {
            index: node_id for node_id, index in payload.location_index_map.items()
        }

        # A non-dropped return trip is part of the solver contract.  The solver
        # charges its cost, while this stage makes the same endpoint visible to
        # MAPF and the executable SimulationPlan.
        assigned_terminal_targets: dict[str, str] = {}
        for index, robot_id in enumerate(vehicle_ids):
            route = route_by_robot.get(robot_id)
            if route is None or not route.task_sequence or drop_return[index]:
                continue
            target = reverse_locations.get(ends[index])
            if target is not None:
                assigned_terminal_targets[robot_id] = target

        idle_relocate_ids = (
            relocate_ids if settings.idle_robot_relocation_enabled else set()
        )
        terminal_robot_ids = set(assigned_terminal_targets) | idle_relocate_ids
        if not terminal_robot_ids:
            return payload, result, TerminalRelocationResult(applied=False)

        task_data = payload.task_data
        task_ids = list(task_data.task_ids)
        task_locations = list(task_data.task_locations)
        demand = list(task_data.demand)
        priorities = list(task_data.priorities)
        service_times = list(task_data.service_times_ms)
        fixed = list(task_data.fixed_vehicle_ids)
        optional = list(task_data.optional_task_ids)
        routes = list(result.routes)
        records: list[TerminalRelocationRecord] = []
        errors: list[str] = []

        for robot_id in sorted(terminal_robot_ids):
            vehicle = request_vehicle.get(robot_id)
            runtime = runtime_robot.get(robot_id)
            assigned_target = assigned_terminal_targets.get(robot_id)
            if assigned_target is not None:
                index = vehicle_ids.index(robot_id)
                policy = vehicle.terminal_policy if vehicle is not None else "STAY"
                if policy == "STAY":
                    target_type = node_types.get(assigned_target)
                    policy = "CHARGE" if target_type == "charging_slot" else "PARK"
                start_node = (
                    vehicle.start_node
                    if vehicle is not None
                    else reverse_locations.get(starts[index], "")
                )
                target = assigned_target
                available_at = (
                    vehicle.available_at_ms
                    if vehicle is not None
                    else availability[index]
                )
                capacity = (
                    vehicle.capacity_units
                    if vehicle is not None
                    else capacities[index]
                )
            elif vehicle is not None:
                policy = vehicle.terminal_policy
                start_node = vehicle.start_node
                target = vehicle.end_node or start_node
                available_at = vehicle.available_at_ms
                capacity = vehicle.capacity_units
            elif runtime is not None and runtime.current_node:
                policy, target = policy_service.policy_for_robot(
                    robot=runtime,
                    graph_arcs=graph_arcs,
                    node_types=node_types,
                )
                start_node = runtime.current_node
                available_at = max(runtime.sim_time_ms, runtime_overrides.planning_horizon_start_ms)
                capacity = runtime.capacity_units
            else:
                errors.append(f"No plannable terminal state for {robot_id}.")
                continue

            if policy == "STAY" or not target:
                continue
            if target not in payload.location_index_map or start_node not in payload.location_index_map:
                errors.append(f"Terminal route for {robot_id} references an unknown node.")
                continue

            route = route_by_robot.get(robot_id)
            from_node = (
                self._last_route_node(route, payload, start_node)
                if route is not None
                else start_node
            )
            if from_node == target:
                continue
            task_id = f"TERMINAL-{robot_id}-{policy}"
            if route is None:
                route = OptimizerRoute(vehicle_id=robot_id, task_sequence=[])
                routes.append(route)
                route_by_robot[robot_id] = route
                vehicle_ids.append(robot_id)
                starts.append(payload.location_index_map[start_node])
                ends.append(payload.location_index_map[target])
                capacities.append(int(capacity))
                availability.append(int(available_at))
                skip_first.append(False)
                drop_return.append(False)
            index = vehicle_ids.index(robot_id)
            ends[index] = payload.location_index_map[target]
            drop_return[index] = False
            updated_route = route.model_copy(
                update={"task_sequence": [*route.task_sequence, task_id]}
            )
            routes[routes.index(route)] = updated_route
            route_by_robot[robot_id] = updated_route

            task_ids.append(task_id)
            task_locations.append(payload.location_index_map[target])
            demand.append(0)
            priorities.append(0)
            battery_pct = float(
                runtime.battery_pct
                if runtime is not None
                else vehicle.battery_pct if vehicle is not None else 100.0
            )
            charge_service_ms = charge_service_duration_ms(
                battery_pct=battery_pct,
                charge_rate_pct_per_minute=(
                    settings.robot_charge_rate_pct_per_minute
                ),
                minimum_service_ms=settings.terminal_relocation_service_ms,
            ) if policy == "CHARGE" else 0
            service_times.append(
                max(settings.terminal_relocation_service_ms, charge_service_ms)
            )
            fixed.append(robot_id)
            records.append(
                TerminalRelocationRecord(
                    robot_id=robot_id,
                    policy=policy,
                    from_node=from_node,
                    to_node=target,
                    task_id=task_id,
                    solver_end_cost_included=(
                        assigned_target is not None or vehicle is not None
                    ),
                    execution_only=True,
                    reason=(
                        f"Assigned robot follows its solver-contracted {policy.lower()} "
                        f"endpoint after business work."
                        if assigned_target is not None
                        else f"Old-plan robot receives no guaranteed future assignment; "
                        f"move to {policy.lower()} node after handover."
                    ),
                )
            )

        execution_payload = payload.model_copy(
            update={
                "fleet_data": FleetData(
                    vehicle_ids=vehicle_ids,
                    vehicle_start_locations=starts,
                    vehicle_end_locations=ends,
                    capacities=capacities,
                    vehicle_available_at_ms=availability,
                    min_vehicles=fleet.min_vehicles,
                    max_g2p_cycles_per_vehicle=fleet.max_g2p_cycles_per_vehicle,
                    skip_first_trips=skip_first,
                    drop_return_trips=drop_return,
                ),
                "task_data": TaskData(
                    task_ids=task_ids,
                    task_locations=task_locations,
                    pickup_and_delivery_pairs=list(task_data.pickup_and_delivery_pairs),
                    demand=demand,
                    priorities=priorities,
                    service_times_ms=service_times,
                    fixed_vehicle_ids=fixed,
                    optional_task_ids=optional,
                ),
            }
        )
        execution_result = result.model_copy(update={"routes": routes})
        relocation = TerminalRelocationResult(
            applied=bool(records),
            valid=not errors,
            relocations=records,
            errors=errors,
            warnings=[
                "Terminal routes are execution goals; they preserve the solver endpoint and do not rewrite business-task coverage."
            ] if records else [],
        )
        return execution_payload, execution_result, relocation

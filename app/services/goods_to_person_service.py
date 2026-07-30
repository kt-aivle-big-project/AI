"""Goods-to-person planning with logical destinations and physical station waypoints.

Business flow
-------------
Orders retain their logical destinations (``O_A`` ... ``O_G``).  A mobile robot
never treats a station as the business destination; it transports one physical
handling unit from a rack access node to a selected station access node.  The
fixed station robot then removes one unit per simulation tick and routes each
quantity to its order's logical destination.

If a positive quantity remains, the same mobile robot returns the handling unit
to its source rack access node.  If the handling unit is depleted, the same
mobile robot moves the empty tote to the configured empty-tote buffer.  When an
item quantity is distributed across multiple handling units, each handling unit
becomes an independent mobile-robot cycle.

v13.20 note
------------
The canonical mission graph does not call :meth:`GoodsToPersonPlanningService.plan`.
That method remains only for lower-level domain regression and legacy compatibility.
The active workflow uses :class:`IntegratedGoodsToPersonCompiler`, then the common
cuOpt/OR-Tools and MAPF nodes. Allocation, station-action, and mutation helpers in
this module are reused by the compiler.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil, inf
from typing import Any, Iterable

from app.core.config import get_settings
from app.domain.schemas import (
    CuOptPayload,
    EdgeReservation,
    GoodsToPersonPlanRequest,
    GoodsToPersonPlanResult,
    HandlingUnitBatchPlan,
    InventoryMutationPreview,
    MapContext,
    OptimizationRequest,
    OptimizationTask,
    OptimizationVehicle,
    OptimizerResult,
    OutboundChuteAllocation,
    StationRobotAction,
    StationServiceReservation,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository
from app.services.context_service import WarehouseContextService
from app.services.edge_calendar import EdgeCalendar
from app.services.graph_service import DirectedGraphService
from app.services.optimization_service import (
    CuOptPayloadBuilder,
    CuOptPayloadValidator,
    ExternalCuOptGateway,
    ORToolsRoutingOptimizer,
    OptimizerAssignmentValidator,
)
from app.services.traffic_manager import TrafficManagerService


_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


class GoodsToPersonPlanningError(RuntimeError):
    """Raised for a business- or topology-level planning rejection."""


class GoodsToPersonPlanningService:
    """Compile code-first outbound orders into physical handling-unit cycles."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()
        self.settings = get_settings()
        self.context_service = WarehouseContextService(self.repository)

    def plan(self, request: GoodsToPersonPlanRequest) -> GoodsToPersonPlanResult:
        """Run the legacy standalone plan path for domain regression only.

        Production and FastAPI planning requests use ``OrchestrationService``.
        """

        try:
            backend = request.optimization_backend or self.settings.optimization_backend
            orders = self._orders(request.order_ids)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for order in orders:
                grouped[str(order["item_id"])].append(order)

            station_busy_until = self._initial_station_availability(request.simulation_id)
            used_robots: set[str] = set()
            batches: list[HandlingUnitBatchPlan] = []
            payloads: list[CuOptPayload] = []
            station_actions: list[StationRobotAction] = []
            previews: list[InventoryMutationPreview] = []
            optimizer_results: list[OptimizerResult] = []
            schedules: list[TrafficScheduleResult] = []
            warnings: list[str] = []
            shared_edge_calendar: EdgeCalendar | None = None
            batch_index = 0

            for item_id, item_orders in sorted(grouped.items()):
                cycle_specs = self._allocate_handling_units(
                    item_id=item_id,
                    orders=item_orders,
                    require_single=request.require_single_handling_unit,
                )
                for handling_unit, allocations in cycle_specs:
                    batch_index += 1
                    batch, payload, map_context, node_types = self._compile_cycle(
                        request=request,
                        batch_index=batch_index,
                        item_id=item_id,
                        handling_unit=handling_unit,
                        allocations=allocations,
                        excluded_robot_ids=used_robots,
                        station_busy_until=station_busy_until,
                    )
                    batches.append(batch)
                    payloads.append(payload)
                    station_actions.extend(self._station_actions(batch))
                    previews.append(self._mutation_preview(batch))

                    payload_validation = CuOptPayloadValidator().validate(payload)
                    if not payload_validation.valid:
                        raise GoodsToPersonPlanningError(
                            "Invalid goods-to-person payload: "
                            + "; ".join(payload_validation.errors)
                        )
                    warnings.extend(payload_validation.warnings)

                    if backend == "cuopt_payload_only":
                        # Preserve the predicted station calendar even before a
                        # vehicle solve so following cycles select the other
                        # station when the first one is expected to be busy.
                        station_busy_until[batch.station_id] = max(
                            station_busy_until.get(batch.station_id, 0),
                            batch.station_available_at_ms,
                        ) + batch.station_service_time_ms
                        continue

                    if backend == "cuopt":
                        result = ExternalCuOptGateway().solve(payload)
                    else:
                        result = ORToolsRoutingOptimizer().solve(payload)
                    optimizer_results.append(result)
                    if result.status != "success":
                        return GoodsToPersonPlanResult(
                            simulation_id=request.simulation_id,
                            status="infeasible" if result.status == "infeasible" else "failed",
                            batches=batches,
                            station_actions=station_actions,
                            inventory_mutation_previews=previews,
                            optimizer_payloads=payloads,
                            optimizer_results=optimizer_results,
                            traffic_schedules=schedules,
                            errors=[result.reason or result.status],
                            warnings=list(dict.fromkeys(warnings)),
                            summary="A handling-unit cycle could not be assigned to a mobile robot.",
                        )
                    assignment = OptimizerAssignmentValidator().validate(
                        payload=payload, result=result
                    )
                    if not assignment.valid or not result.routes:
                        raise GoodsToPersonPlanningError(
                            "Optimizer assignment failed: " + "; ".join(assignment.errors)
                        )
                    route = result.routes[0]
                    batch = batch.model_copy(update={"mobile_robot_id": route.vehicle_id})
                    batches[-1] = batch
                    used_robots.add(route.vehicle_id)
                    if shared_edge_calendar is None:
                        shared_edge_calendar = EdgeCalendar.from_map_context(map_context)
                    schedule = self._schedule_cycle(
                        batch=batch,
                        payload=payload,
                        result=result,
                        map_context=map_context,
                        node_types=node_types,
                        edge_calendar=shared_edge_calendar,
                        station_busy_until=station_busy_until,
                    )
                    schedules.append(schedule)
                    if not schedule.valid:
                        raise GoodsToPersonPlanningError(
                            "Traffic scheduling failed: " + "; ".join(schedule.conflicts)
                        )

            status = "ready_for_optimizer" if backend == "cuopt_payload_only" else "planned"
            return GoodsToPersonPlanResult(
                simulation_id=request.simulation_id,
                status=status,
                batches=batches,
                station_actions=station_actions,
                inventory_mutation_previews=previews,
                optimizer_payloads=payloads,
                optimizer_results=optimizer_results,
                traffic_schedules=schedules,
                warnings=list(dict.fromkeys(warnings)),
                summary=(
                    f"Converted {len(orders)} outbound order(s) into {len(batches)} "
                    "physical handling-unit cycle(s). Logical O_* destinations were "
                    "preserved while mobile robots used station access waypoints."
                ),
            )
        except GoodsToPersonPlanningError as exc:
            return GoodsToPersonPlanResult(
                simulation_id=request.simulation_id,
                status="input_rejected",
                errors=[str(exc)],
                summary="The goods-to-person request did not satisfy the cycle contract.",
            )
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            return GoodsToPersonPlanResult(
                simulation_id=request.simulation_id,
                status="failed",
                errors=[f"{type(exc).__name__}: {exc}"],
                summary="Unexpected goods-to-person planning failure.",
            )

    def _orders(self, order_ids: list[str]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for order_id in list(dict.fromkeys(order_ids)):
            order = self.repository.get_order(order_id)
            if order is None:
                raise GoodsToPersonPlanningError(f"Order {order_id} does not exist.")
            if str(order.get("status", "pending")) not in {"pending", "released"}:
                raise GoodsToPersonPlanningError(
                    f"Order {order_id} is not eligible: status={order.get('status')}."
                )
            destination = str(
                order.get("logical_destination_id")
                or order.get("outbound_chute_id")
                or order.get("delivery_node")
                or ""
            )
            if destination not in self.repository.outbound_chutes:
                raise GoodsToPersonPlanningError(
                    f"Order {order_id} does not reference a configured logical O_* destination."
                )
            values.append(
                {
                    **order,
                    "outbound_chute_id": destination,
                    "logical_destination_id": destination,
                }
            )
        return values

    def _allocate_handling_units(
        self,
        *,
        item_id: str,
        orders: list[dict[str, Any]],
        require_single: bool,
    ) -> list[tuple[dict[str, Any], list[OutboundChuteAllocation]]]:
        """Split one item wave across as few physical handling units as possible."""

        ordered_orders = sorted(
            orders,
            key=lambda value: (
                -_PRIORITY_RANK.get(str(value.get("priority", "medium")), 1),
                str(value["order_id"]),
            ),
        )
        total = sum(int(value["required_qty"]) for value in ordered_orders)
        units = [
            value
            for value in self.repository.handling_units(item_id)
            if str(value.get("handling_unit_status", "stored")) == "stored"
            and int(value.get("quantity", 0)) > 0
        ]
        if not units:
            raise GoodsToPersonPlanningError(f"No stored handling unit exists for {item_id}.")

        single = sorted(
            [value for value in units if int(value["quantity"]) >= total],
            key=lambda value: (
                int(value["quantity"]) - total,
                str(value["handling_unit_id"]),
            ),
        )
        if single:
            selected = [single[0]]
        else:
            if require_single:
                raise GoodsToPersonPlanningError(
                    f"No single handling unit for {item_id} contains {total} unit(s)."
                )
            selected = []
            remaining = total
            for value in sorted(
                units,
                key=lambda item: (-int(item["quantity"]), str(item["handling_unit_id"])),
            ):
                selected.append(value)
                remaining -= int(value["quantity"])
                if remaining <= 0:
                    break
            if remaining > 0:
                available = sum(int(value["quantity"]) for value in units)
                raise GoodsToPersonPlanningError(
                    f"Insufficient handling-unit inventory for {item_id}: required={total}, available={available}."
                )

        order_remaining = {
            str(value["order_id"]): int(value["required_qty"])
            for value in ordered_orders
        }
        order_by_id = {str(value["order_id"]): value for value in ordered_orders}
        result: list[tuple[dict[str, Any], list[OutboundChuteAllocation]]] = []
        for handling_unit in selected:
            cycle_capacity = min(
                int(handling_unit["quantity"]),
                sum(order_remaining.values()),
            )
            left = cycle_capacity
            allocations: list[OutboundChuteAllocation] = []
            for order_id in [str(value["order_id"]) for value in ordered_orders]:
                needed = order_remaining[order_id]
                if needed <= 0 or left <= 0:
                    continue
                allocated = min(needed, left)
                order = order_by_id[order_id]
                destination = str(order["logical_destination_id"])
                allocations.append(
                    OutboundChuteAllocation(
                        order_id=order_id,
                        chute_id=destination,
                        logical_destination_id=destination,
                        quantity=allocated,
                    )
                )
                order_remaining[order_id] -= allocated
                left -= allocated
            if allocations:
                result.append((handling_unit, allocations))
        if any(value > 0 for value in order_remaining.values()):
            raise GoodsToPersonPlanningError(
                f"Handling-unit allocation left unresolved quantities: {order_remaining}."
            )
        return result

    def _initial_station_availability(self, simulation_id: str) -> dict[str, int]:
        values = self.repository.station_runtime(simulation_id)
        result: dict[str, int] = {}
        for value in values:
            station_id = str(value["station_id"])
            result[station_id] = max(
                int(value.get("available_at_ms", 0)),
                int(value.get("queue_depth", 0)) * self.settings.simulation_tick_ms,
            )
        return result

    def _compile_cycle(
        self,
        *,
        request: GoodsToPersonPlanRequest,
        batch_index: int,
        item_id: str,
        handling_unit: dict[str, Any],
        allocations: list[OutboundChuteAllocation],
        excluded_robot_ids: set[str],
        station_busy_until: dict[str, int],
    ) -> tuple[HandlingUnitBatchPlan, CuOptPayload, MapContext, dict[str, str]]:
        requested_quantity = sum(value.quantity for value in allocations)
        quantity_before = int(handling_unit["quantity"])
        quantity_after = quantity_before - requested_quantity
        return_required = quantity_after > 0

        robots_context = self.context_service.build_robot_context(required_capacity=1)
        effective_excluded = set(excluded_robot_ids) | set(request.excluded_robot_ids)
        allowed_robot_ids = set(request.allowed_robot_ids)
        robots = [
            value
            for value in robots_context.robots
            if value.robot_id in robots_context.candidate_robot_ids
            and value.robot_id not in effective_excluded
            and (not allowed_robot_ids or value.robot_id in allowed_robot_ids)
        ]
        if not robots:
            raise GoodsToPersonPlanningError(
                "Each simultaneous handling-unit cycle requires a distinct idle mobile robot in this PoC."
            )

        destinations = sorted({value.logical_destination_id for value in allocations})
        stations = self.repository.outbound_station_candidates(destinations)
        if request.preferred_station_id:
            stations = [
                value
                for value in stations
                if str(value["station_id"]) == request.preferred_station_id
            ]
        if not stations:
            raise GoodsToPersonPlanningError(
                f"No outbound station can route to logical destinations {destinations}."
            )
        empty_buffers = self.repository.empty_tote_buffer_candidates()
        if not return_required and not empty_buffers:
            raise GoodsToPersonPlanningError("No empty-tote buffer is configured.")

        processing_ticks = max(
            1,
            ceil(requested_quantity / self.settings.outbound_station_items_per_tick),
        )
        station_receive_ms = self.settings.outbound_station_receive_ms
        station_sort_ms = processing_ticks * self.settings.simulation_tick_ms
        station_release_ms = self.settings.outbound_station_release_ms
        station_service_ms = station_receive_ms + station_sort_ms + station_release_ms

        candidate_nodes = [
            *[str(value) for value in handling_unit.get("access_node_ids", [])],
            *[node for station in stations for node in station.get("access_node_ids", [])],
            *[node for buffer in empty_buffers for node in buffer.get("access_node_ids", [])],
            *[value.current_node for value in robots],
        ]
        graph_bundle = self.context_service.build_map_context(node_ids=candidate_nodes)
        graph = DirectedGraphService(graph_bundle.graph_arcs)

        best: tuple[int, str, str, str, str, str | None, int, int] | None = None
        # score, station_id, source_access, station_access, post_node,
        # empty_buffer_id, available_at, queue_wait
        for station in stations:
            station_id = str(station["station_id"])
            station_robot = self.repository.station_robot(station_id)
            if station_robot is None or str(station_robot.get("status", "idle")) not in {
                "idle", "available"
            }:
                continue
            available_at = int(station_busy_until.get(station_id, 0))
            for source_access in handling_unit.get("access_node_ids", []):
                source_access = str(source_access)
                source_to_station_candidates: list[tuple[int, str]] = []
                for station_access in station.get("access_node_ids", []):
                    travel, _ = graph.shortest_path(
                        source_access, str(station_access), metric="travel_time"
                    )
                    if travel != inf:
                        source_to_station_candidates.append((int(travel), str(station_access)))
                for source_to_station, station_access in source_to_station_candidates:
                    robot_travel = min(
                        int(
                            graph.shortest_path(
                                value.current_node, source_access, metric="travel_time"
                            )[0]
                        )
                        for value in robots
                        if graph.shortest_path(
                            value.current_node, source_access, metric="travel_time"
                        )[0]
                        != inf
                    ) if any(
                        graph.shortest_path(value.current_node, source_access, metric="travel_time")[0]
                        != inf
                        for value in robots
                    ) else None
                    if robot_travel is None:
                        continue
                    if return_required:
                        post_candidates = [(source_access, None)]
                    else:
                        post_candidates = [
                            (str(node), str(buffer["buffer_id"]))
                            for buffer in empty_buffers
                            for node in buffer.get("access_node_ids", [])
                        ]
                    for post_node, buffer_id in post_candidates:
                        post_travel, _ = graph.shortest_path(
                            station_access, post_node, metric="travel_time"
                        )
                        if post_travel == inf:
                            continue
                        estimated_arrival = (
                            robot_travel
                            + self.settings.handling_unit_pickup_service_ms
                            + source_to_station
                        )
                        queue_wait = max(0, available_at - estimated_arrival)
                        score = int(
                            estimated_arrival
                            + queue_wait
                            + station_service_ms
                            + post_travel
                        )
                        candidate = (
                            score,
                            station_id,
                            source_access,
                            station_access,
                            post_node,
                            buffer_id,
                            available_at,
                            queue_wait,
                        )
                        if best is None or candidate < best:
                            best = candidate
        if best is None:
            raise GoodsToPersonPlanningError(
                "No robot/rack/station/post-station route is reachable on the directed graph."
            )

        (
            score,
            station_id,
            source_access,
            station_access,
            post_station_node,
            empty_buffer_id,
            station_available_at,
            station_queue_wait,
        ) = best
        station_robot = self.repository.station_robot(station_id)
        assert station_robot is not None
        if len({value.order_id for value in allocations}) > int(
            station_robot.get("max_orders_per_wave", 1)
        ):
            raise GoodsToPersonPlanningError(
                f"Station robot {station_robot['station_robot_id']} supports at most "
                f"{station_robot.get('max_orders_per_wave')} orders per wave."
            )

        batch_id = f"G2P-{request.simulation_id}-{batch_index:03d}-{item_id}"
        batch = HandlingUnitBatchPlan(
            batch_id=batch_id,
            item_id=item_id,
            order_ids=list(dict.fromkeys(value.order_id for value in allocations)),
            logical_destination_ids=destinations,
            handling_unit_id=str(handling_unit["handling_unit_id"]),
            handling_unit_version=int(handling_unit.get("version", 0)),
            source_stock_id=str(handling_unit["stock_id"]),
            source_rack_id=str(handling_unit["rack_id"]),
            source_rack_level=int(handling_unit["rack_level"]),
            source_access_node=source_access,
            station_id=station_id,
            station_robot_id=str(station_robot["station_robot_id"]),
            station_access_node=station_access,
            station_selection_score_ms=score,
            station_available_at_ms=station_available_at,
            station_queue_wait_ms=station_queue_wait,
            allocations=allocations,
            requested_quantity=requested_quantity,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            return_required=return_required,
            disposition=(
                "RETURN_TO_HOME"
                if return_required
                else "MOVE_TO_EMPTY_TOTE_BUFFER"
            ),
            post_station_action=(
                "RETURN_TO_SOURCE"
                if return_required
                else "MOVE_TO_EMPTY_TOTE_BUFFER"
            ),
            post_station_node=post_station_node,
            empty_tote_buffer_id=empty_buffer_id,
            station_receive_time_ms=station_receive_ms,
            station_sort_time_ms=station_sort_ms,
            station_release_time_ms=station_release_ms,
            station_service_time_ms=station_service_ms,
            station_processing_ticks=processing_ticks,
        )
        optimization_request = OptimizationRequest(
            snapshot_id=f"G2P-{request.simulation_id}-{batch_index:03d}",
            tasks=[
                OptimizationTask(
                    task_id=batch_id,
                    pickup_node=source_access,
                    delivery_node=station_access,
                    demand=1,
                    priority=max(
                        (
                            str(self.repository.get_order(value.order_id).get("priority", "medium"))
                            for value in allocations
                        ),
                        key=_PRIORITY_RANK.get,
                    ),
                    rack_id=str(handling_unit["rack_id"]),
                    rack_level=int(handling_unit["rack_level"]),
                    pickup_service_time_ms=self.settings.handling_unit_pickup_service_ms,
                    drop_service_time_ms=station_service_ms,
                )
            ],
            vehicles=[
                OptimizationVehicle(
                    robot_id=value.robot_id,
                    start_node=value.current_node,
                    capacity_units=value.capacity_units,
                    battery_pct=value.battery_pct,
                )
                for value in robots
            ],
            map_constraints=graph_bundle.context.map_constraints,
            objective_profile=request.objective_profile,
        )
        payload = CuOptPayloadBuilder().build(
            request=optimization_request,
            graph_nodes=graph_bundle.graph_nodes,
            graph_arcs=graph_bundle.graph_arcs,
            time_limit_seconds=(
                self.settings.ortools_time_limit_seconds
                if (request.optimization_backend or self.settings.optimization_backend) == "ortools"
                else self.settings.cuopt_time_limit_seconds
            ),
        )
        if request.same_mobile_robot_round_trip:
            end_index = payload.location_index_map[post_station_node]
            payload = payload.model_copy(
                update={
                    "fleet_data": payload.fleet_data.model_copy(
                        update={
                            "vehicle_end_locations": [
                                end_index for _ in payload.fleet_data.vehicle_ids
                            ],
                            "drop_return_trips": [
                                False for _ in payload.fleet_data.vehicle_ids
                            ],
                        }
                    )
                }
            )
        return batch, payload, graph_bundle.context, graph_bundle.graph_node_types

    def _station_actions(self, batch: HandlingUnitBatchPlan) -> list[StationRobotAction]:
        common = {
            "station_robot_id": batch.station_robot_id,
            "station_id": batch.station_id,
            "handling_unit_id": batch.handling_unit_id,
            "order_ids": batch.order_ids,
            "logical_destination_ids": batch.logical_destination_ids,
        }
        return [
            StationRobotAction(
                **common,
                action="RECEIVE_HANDLING_UNIT",
                duration_ms=batch.station_receive_time_ms,
            ),
            StationRobotAction(
                **common,
                action="SORT_TO_DESTINATIONS",
                duration_ms=batch.station_sort_time_ms,
                processing_ticks=batch.station_processing_ticks,
            ),
            StationRobotAction(
                **common,
                action=(
                    "RELEASE_REMAINDER"
                    if batch.return_required
                    else "RELEASE_EMPTY_TOTE"
                ),
                duration_ms=batch.station_release_time_ms,
            ),
        ]

    @staticmethod
    def _mutation_preview(batch: HandlingUnitBatchPlan) -> InventoryMutationPreview:
        return InventoryMutationPreview(
            handling_unit_id=batch.handling_unit_id,
            expected_version=batch.handling_unit_version,
            quantity_before=batch.quantity_before,
            reserved_quantity=batch.requested_quantity,
            quantity_after=batch.quantity_after,
            next_status=("returning" if batch.return_required else "empty_in_transit"),
            home_rack_id=batch.source_rack_id,
            home_rack_level=batch.source_rack_level,
            post_station_node=batch.post_station_node,
            order_ids=batch.order_ids,
        )

    def _schedule_cycle(
        self,
        *,
        batch: HandlingUnitBatchPlan,
        payload: CuOptPayload,
        result: OptimizerResult,
        map_context: MapContext,
        node_types: dict[str, str],
        edge_calendar: EdgeCalendar,
        station_busy_until: dict[str, int],
    ) -> TrafficScheduleResult:
        robot_id = result.routes[0].vehicle_id
        reverse = {value: key for key, value in payload.location_index_map.items()}
        start_by_robot = {
            value: reverse[index]
            for value, index in zip(
                payload.fleet_data.vehicle_ids,
                payload.fleet_data.vehicle_start_locations,
                strict=True,
            )
        }
        graph = DirectedGraphService(
            [
                {
                    "edge_id": edge_id,
                    "source": reverse[source],
                    "target": reverse[target],
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
        goals = [
            (
                batch.source_access_node,
                self.settings.handling_unit_pickup_service_ms,
                f"{batch.batch_id}_PICK",
                "PICKUP",
            ),
            (
                batch.station_access_node,
                batch.station_service_time_ms,
                f"{batch.batch_id}_STATION",
                "STATION",
            ),
            (
                batch.post_station_node,
                (
                    self.settings.handling_unit_return_service_ms
                    if batch.return_required
                    else self.settings.empty_tote_buffer_service_ms
                ),
                (
                    f"{batch.batch_id}_RETURN"
                    if batch.return_required
                    else f"{batch.batch_id}_EMPTY_TOTE"
                ),
                "RETURN" if batch.return_required else "EMPTY_TOTE_BUFFER",
            ),
        ]
        current_node = start_by_robot[robot_id]
        current_time = 0
        steps: list[TimedRouteStep] = []
        reservations: list[EdgeReservation] = []
        station_reservations: list[StationServiceReservation] = []

        for goal_node, service_ms, action_id, service_kind in goals:
            value, arcs = graph.shortest_path(current_node, goal_node, metric="travel_time")
            if value == inf:
                return TrafficScheduleResult(
                    valid=False,
                    planner="goods_to_person_station_waypoint",
                    conflicts=[f"No path from {current_node} to {goal_node}."],
                )
            for arc in arcs:
                slot = edge_calendar.earliest_slot(
                    edge_id=arc.edge_id,
                    earliest=current_time,
                    duration=arc.travel_time_ms,
                )
                if slot > current_time:
                    if node_types.get(current_node) not in TrafficManagerService.SAFE_WAIT_NODE_TYPES:
                        return TrafficScheduleResult(
                            valid=False,
                            planner="goods_to_person_station_waypoint",
                            conflicts=[f"{robot_id} cannot wait at {current_node}."],
                        )
                    steps.append(
                        TimedRouteStep(
                            step_type="WAIT",
                            node_id=current_node,
                            start_at_ms=current_time,
                            end_at_ms=slot,
                            reason=f"Wait for {arc.edge_id}.",
                        )
                    )
                end = slot + arc.travel_time_ms
                steps.append(
                    TimedRouteStep(
                        step_type="MOVE",
                        edge_id=arc.edge_id,
                        from_node=arc.source,
                        to_node=arc.target,
                        start_at_ms=slot,
                        end_at_ms=end,
                    )
                )
                reservation = EdgeReservation(
                    reservation_id=f"G2P-{robot_id}-{len(reservations):04d}",
                    edge_id=arc.edge_id,
                    robot_id=robot_id,
                    direction=f"{arc.source}_TO_{arc.target}",
                    start_at_ms=slot,
                    end_at_ms=end,
                    from_node=arc.source,
                    to_node=arc.target,
                )
                reservations.append(reservation)
                edge_calendar.reserve(
                    edge_id=arc.edge_id,
                    start=slot,
                    end=end,
                    robot_id=robot_id,
                )
                current_node = arc.target
                current_time = end

            if service_kind == "STATION":
                station_start = max(
                    current_time,
                    int(station_busy_until.get(batch.station_id, 0)),
                )
                if station_start > current_time:
                    steps.append(
                        TimedRouteStep(
                            step_type="WAIT",
                            node_id=goal_node,
                            start_at_ms=current_time,
                            end_at_ms=station_start,
                            reason=f"Station robot {batch.station_robot_id} is processing another tote.",
                        )
                    )
                    current_time = station_start
                station_end = current_time + service_ms
                station_reservations.append(
                    StationServiceReservation(
                        reservation_id=f"STATION-{batch.batch_id}",
                        station_id=batch.station_id,
                        station_robot_id=batch.station_robot_id,
                        handling_unit_id=batch.handling_unit_id,
                        mobile_robot_id=robot_id,
                        start_at_ms=current_time,
                        end_at_ms=station_end,
                        processed_quantity=batch.requested_quantity,
                        processing_ticks=batch.station_processing_ticks,
                    )
                )
                station_busy_until[batch.station_id] = station_end
            if service_ms > 0:
                reason = {
                    "PICKUP": "The mobile robot picks the physical handling unit.",
                    "STATION": (
                        "The fixed station robot processes one item per tick and routes "
                        "quantities to logical O_* destinations."
                    ),
                    "RETURN": "The same mobile robot returns the positive remainder home.",
                    "EMPTY_TOTE_BUFFER": (
                        "The same mobile robot deposits the depleted tote in the empty-tote buffer."
                    ),
                }[service_kind]
                steps.append(
                    TimedRouteStep(
                        step_type="SERVICE",
                        node_id=goal_node,
                        start_at_ms=current_time,
                        end_at_ms=current_time + service_ms,
                        task_id=action_id,
                        service_kind=service_kind,
                        reason=reason,
                    )
                )
                current_time += service_ms

        wait_ms = sum(
            value.end_at_ms - value.start_at_ms
            for value in steps
            if value.step_type == "WAIT"
        )
        service_total = sum(
            value.end_at_ms - value.start_at_ms
            for value in steps
            if value.step_type == "SERVICE"
        )
        return TrafficScheduleResult(
            valid=True,
            planner="goods_to_person_station_waypoint",
            routes=[TimedRobotRoute(robot_id=robot_id, steps=steps, finish_at_ms=current_time)],
            reservations=reservations,
            station_reservations=station_reservations,
            total_wait_ms=wait_ms,
            total_service_ms=service_total,
            makespan_ms=current_time,
        )

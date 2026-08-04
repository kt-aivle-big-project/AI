"""Compile outbound order tasks into physical goods-to-person handling-unit cycles.

This module is intentionally solver-neutral.  It is called after the Rule or
Agent branch has produced and validated an :class:`OptimizationRequest`, but
before the common cuOpt payload builder.  The compiler preserves the selected
fleet, objective, map constraints, and every non-outbound task.  It replaces
only outbound order-level tasks with handling-unit pickup-to-station tasks.

cuOpt/OR-Tools assignment, MAPF, static validation, and terminal persistence
remain shared LangGraph nodes.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil, inf
from typing import Any

from app.core.config import get_settings
from app.domain.schemas import (
    GoodsToPersonCompilationResult,
    GoodsToPersonOptions,
    HandlingUnitBatchPlan,
    InventoryMutationPreview,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    OptimizationTask,
    OutboundChuteAllocation,
    StationRobotAction,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository
from app.services.goods_to_person_service import GoodsToPersonPlanningError, GoodsToPersonPlanningService
from app.services.graph_service import DirectedGraphService

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


class IntegratedGoodsToPersonCompiler:
    """Transform validated outbound orders without invoking a solver."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()
        self.settings = get_settings()
        # Reuse the mature allocation and mutation helpers from the compatibility
        # service.  The compiler itself never calls ``plan`` or any optimizer.
        self.domain_helpers = GoodsToPersonPlanningService(self.repository)

    def compile(
        self,
        *,
        simulation_id: str,
        normalized_request: NormalizedWarehouseRequest | None,
        optimization_request: OptimizationRequest,
        graph_arcs: list[dict[str, Any]],
        options: GoodsToPersonOptions | None = None,
    ) -> GoodsToPersonCompilationResult:
        """Return one transformed optimization request and auditable G2P metadata."""

        options = options or GoodsToPersonOptions()
        order_ids = self._outbound_order_ids(normalized_request, optimization_request)
        original_task_ids = [value.task_id for value in optimization_request.tasks]
        if not order_ids:
            return GoodsToPersonCompilationResult(
                applied=False,
                source_order_ids=[],
                original_task_ids=original_task_ids,
                compiled_task_ids=original_task_ids,
                preserved_task_ids=original_task_ids,
                optimization_request=optimization_request,
                summary="No outbound order task required goods-to-person compilation.",
            )
        if not optimization_request.vehicles:
            raise GoodsToPersonPlanningError("No eligible mobile robot remains for the outbound wave.")

        orders = self.domain_helpers._orders(order_ids)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for order in orders:
            grouped[str(order["item_id"])].append(order)

        graph = DirectedGraphService(graph_arcs)
        station_busy_until = self.domain_helpers._initial_station_availability(simulation_id)
        batches: list[HandlingUnitBatchPlan] = []
        g2p_tasks: list[OptimizationTask] = []
        station_actions: list[StationRobotAction] = []
        mutation_previews: list[InventoryMutationPreview] = []
        warnings: list[str] = []
        batch_index = 0

        for item_id, item_orders in sorted(grouped.items()):
            cycles = self.domain_helpers._allocate_handling_units(
                item_id=item_id,
                orders=item_orders,
                require_single=options.require_single_handling_unit,
            )
            for handling_unit, allocations in cycles:
                batch_index += 1
                batch = self._compile_batch(
                    simulation_id=simulation_id,
                    batch_index=batch_index,
                    item_id=item_id,
                    handling_unit=handling_unit,
                    allocations=allocations,
                    vehicles=optimization_request.vehicles,
                    graph=graph,
                    station_busy_until=station_busy_until,
                    options=options,
                )
                batches.append(batch)
                station_actions.extend(self.domain_helpers._station_actions(batch))
                mutation_previews.append(self.domain_helpers._mutation_preview(batch))
                g2p_tasks.append(
                    OptimizationTask(
                        task_id=batch.batch_id,
                        pickup_node=batch.source_access_node,
                        delivery_node=(
                            batch.mobile_handoff_node
                            or batch.station_access_node
                        ),
                        demand=1,
                        priority=self._batch_priority(batch.order_ids),
                        operation_type="G2P_HANDLING_UNIT",
                        order_ids=list(batch.order_ids),
                        item_id=batch.item_id,
                        stock_id=batch.source_stock_id,
                        logical_destination_ids=list(batch.logical_destination_ids),
                        handling_unit_id=batch.handling_unit_id,
                        g2p_batch_id=batch.batch_id,
                        station_id=batch.station_id,
                        station_access_node=batch.station_access_node,
                        post_station_node=batch.post_station_node,
                        rack_id=batch.source_rack_id,
                        rack_level=batch.source_rack_level,
                        pickup_service_time_ms=self.settings.handling_unit_pickup_service_ms,
                        drop_service_time_ms=batch.station_service_time_ms,
                    )
                )
                # Predict one station calendar for the next cycle.  The common
                # MAPF node later enforces the exact capacity-one reservation.
                station_busy_until[batch.station_id] = max(
                    station_busy_until.get(batch.station_id, 0),
                    batch.station_available_at_ms,
                ) + max(1, batch.station_receive_time_ms)

        replaced, preserved = self._partition_existing_tasks(
            optimization_request=optimization_request,
            order_ids=order_ids,
        )
        compiled_vehicles = list(optimization_request.vehicles)
        pickup_nodes = {
            task.pickup_node for task in [*preserved, *g2p_tasks]
        }
        unreachable_robot_ids = [
            vehicle.robot_id
            for vehicle in compiled_vehicles
            if pickup_nodes and not any(
                graph.reachable(vehicle.start_node, pickup_node)
                for pickup_node in pickup_nodes
            )
        ]
        if unreachable_robot_ids:
            compiled_vehicles = [
                vehicle
                for vehicle in compiled_vehicles
                if vehicle.robot_id not in set(unreachable_robot_ids)
            ]
            warnings.append(
                "Excluded robot(s) that cannot reach any pending pickup: "
                + ", ".join(sorted(unreachable_robot_ids))
            )
        if not compiled_vehicles:
            raise GoodsToPersonPlanningError(
                "No eligible mobile robot can reach a pending pickup node."
            )
        if not preserved:
            # One AMR carries one physical handling unit at a time.  Reusing the
            # existing cuOpt capacity dimension with capacity=1 prevents a route
            # from picking multiple boxes before reaching a station. The same
            # vehicle may perform another cycle after completing the first one.
            compiled_vehicles = [
                value.model_copy(update={"capacity_units": 1})
                for value in compiled_vehicles
            ]
        useful_vehicle_count = min(
            len(compiled_vehicles),
            len(preserved) + len(g2p_tasks),
        )
        compiled_minimum_vehicle_count = min(
            int(optimization_request.minimum_vehicle_count),
            useful_vehicle_count,
        )
        compiled_request = optimization_request.model_copy(
            update={
                "snapshot_id": f"{optimization_request.snapshot_id}-G2P",
                "tasks": [*preserved, *g2p_tasks],
                "vehicles": compiled_vehicles,
                # Preserve a validated Agent parallelism lower bound while
                # bounding it to the reachable fleet and physical cycles.
                # cuOpt still chooses AMR identities, assignment, and reuse.
                "minimum_vehicle_count": compiled_minimum_vehicle_count,
                "max_g2p_cycles_per_vehicle": None,
            }
        )
        if len(g2p_tasks) < len(order_ids):
            warnings.append(
                f"Aggregated {len(order_ids)} outbound order(s) into "
                f"{len(g2p_tasks)} physical handling-unit cycle(s)."
            )
        return GoodsToPersonCompilationResult(
            applied=True,
            source_order_ids=order_ids,
            original_task_ids=original_task_ids,
            compiled_task_ids=[value.task_id for value in compiled_request.tasks],
            preserved_task_ids=[value.task_id for value in preserved],
            batches=batches,
            station_actions=station_actions,
            inventory_mutation_previews=mutation_previews,
            optimization_request=compiled_request,
            warnings=warnings,
            summary=(
                f"Compiled {len(order_ids)} outbound order(s) into {len(batches)} "
                "handling-unit cycle(s); common cuOpt/OR-Tools and MAPF nodes remain downstream."
            ),
        )

    def _outbound_order_ids(
        self,
        normalized_request: NormalizedWarehouseRequest | None,
        optimization_request: OptimizationRequest,
    ) -> list[str]:
        if normalized_request is not None:
            values = [
                value.operation_id
                for value in normalized_request.operations
                if value.operation_type == "OUTBOUND_ORDER"
            ]
            if values:
                return list(dict.fromkeys(values))
        # Prebuilt mission compatibility: infer only from an exact configured
        # O_* logical destination.  Item names and fuzzy references are never used.
        return list(
            dict.fromkeys(
                task.task_id.removeprefix("TASK-")
                for task in optimization_request.tasks
                if task.delivery_node in self.repository.outbound_chutes
                and self.repository.get_order(task.task_id.removeprefix("TASK-")) is not None
            )
        )

    def _partition_existing_tasks(
        self,
        *,
        optimization_request: OptimizationRequest,
        order_ids: list[str],
    ) -> tuple[list[OptimizationTask], list[OptimizationTask]]:
        order_tokens = set(order_ids)
        replaced: list[OptimizationTask] = []
        preserved: list[OptimizationTask] = []
        for task in optimization_request.tasks:
            task_order_hint = task.task_id.removeprefix("TASK-")
            is_outbound = (
                task.delivery_node in self.repository.outbound_chutes
                or task_order_hint in order_tokens
                or any(order_id in task.task_id for order_id in order_tokens)
            )
            (replaced if is_outbound else preserved).append(task)
        if order_ids and not replaced:
            # Dynamic drafts often use opaque TASK-001 ids.  In a pure outbound
            # request all existing tasks are therefore the order-level rows being
            # replaced.  Mixed requests retain tasks whose destination is not O_*.
            if all(task.delivery_node in self.repository.outbound_chutes for task in optimization_request.tasks):
                replaced = list(optimization_request.tasks)
                preserved = []
        return replaced, preserved

    def _batch_priority(self, order_ids: list[str]) -> str:
        priorities = [
            str((self.repository.get_order(value) or {}).get("priority", "medium"))
            for value in order_ids
        ]
        return max(priorities or ["medium"], key=lambda value: _PRIORITY_RANK.get(value, 1))

    def _compile_batch(
        self,
        *,
        simulation_id: str,
        batch_index: int,
        item_id: str,
        handling_unit: dict[str, Any],
        allocations: list[OutboundChuteAllocation],
        vehicles: list,
        graph: DirectedGraphService,
        station_busy_until: dict[str, int],
        options: GoodsToPersonOptions,
    ) -> HandlingUnitBatchPlan:
        requested_quantity = sum(value.quantity for value in allocations)
        quantity_before = int(handling_unit["quantity"])
        quantity_after = quantity_before - requested_quantity
        return_required = quantity_after > 0
        destinations = sorted({value.logical_destination_id for value in allocations})
        stations = self.domain_helpers._stations_covering_all(destinations)
        if options.preferred_station_id:
            stations = [
                value
                for value in stations
                if str(value["station_id"]) == options.preferred_station_id
            ]
        if not stations:
            raise GoodsToPersonPlanningError(
                f"No outbound station can route to logical destinations {destinations}."
            )
        processing_ticks = max(
            1,
            ceil(requested_quantity / self.settings.outbound_station_items_per_tick),
        )
        station_receive_ms = self.settings.outbound_station_receive_ms
        station_sort_ms = processing_ticks * self.settings.simulation_tick_ms
        station_release_ms = self.settings.outbound_station_release_ms
        station_service_ms = station_receive_ms + station_sort_ms + station_release_ms

        best: tuple[int, str, str, str, str, str, str | None, int, int] | None = None
        for station in stations:
            station_id = str(station["station_id"])
            station_robot = self.repository.station_robot(station_id)
            if station_robot is None or str(station_robot.get("status", "idle")) not in {
                "idle",
                "available",
            }:
                continue
            available_at = int(station_busy_until.get(station_id, 0))
            for source_access_value in handling_unit.get("access_node_ids", []):
                source_access = str(source_access_value)
                for station_access_value in station.get("access_node_ids", []):
                    station_access = str(station_access_value)
                    mobile_handoff = self.repository.mobile_handoff_node_for_station_access(
                        station_access
                    )
                    source_to_station, _ = graph.shortest_path(
                        source_access,
                        mobile_handoff,
                        metric="travel_time",
                    )
                    if source_to_station == inf:
                        continue
                    # Eligibility is a hard feasibility check only.  Do not
                    # use the nearest candidate robot to rank a station: that
                    # would pre-bias the exact AMR assignment before cuOpt.
                    reachable_robot_costs = [
                        graph.shortest_path(value.start_node, source_access, metric="travel_time")[0]
                        for value in vehicles
                    ]
                    reachable_robot_costs = [value for value in reachable_robot_costs if value != inf]
                    if not reachable_robot_costs:
                        continue
                    if return_required:
                        post_candidates = [(source_access, None)]
                    else:
                        # The complete BOX is transferred to the fixed outbound
                        # robot at the purple AMR boundary. It leaves inventory
                        # there; no empty tote remains for the AMR to return.
                        post_candidates = [(mobile_handoff, None)]
                    for post_node, buffer_id in post_candidates:
                        post_travel, _ = graph.shortest_path(
                            mobile_handoff,
                            post_node,
                            metric="travel_time",
                        )
                        if post_travel == inf:
                            continue
                        estimated_arrival = (
                            self.settings.handling_unit_pickup_service_ms
                            + int(source_to_station)
                        )
                        queue_wait = max(0, available_at - estimated_arrival)
                        score = int(
                            estimated_arrival
                            + queue_wait
                            + station_service_ms
                            + int(post_travel)
                        )
                        candidate = (
                            score,
                            station_id,
                            source_access,
                            station_access,
                            mobile_handoff,
                            post_node,
                            buffer_id,
                            available_at,
                            queue_wait,
                        )
                        if best is None or candidate < best:
                            best = candidate
        if best is None:
            raise GoodsToPersonPlanningError(
                "No robot/rack/station/post-station route is reachable on the adjusted graph."
            )

        (
            score,
            station_id,
            source_access,
            station_access,
            mobile_handoff,
            post_station_node,
            empty_buffer_id,
            station_available_at,
            station_queue_wait,
        ) = best
        station_robot = self.repository.station_robot(station_id)
        assert station_robot is not None
        unique_order_count = len({value.order_id for value in allocations})
        if unique_order_count > int(station_robot.get("max_orders_per_wave", 1)):
            raise GoodsToPersonPlanningError(
                f"Station robot {station_robot['station_robot_id']} supports at most "
                f"{station_robot.get('max_orders_per_wave')} orders per wave."
            )

        batch_id = f"G2P-{simulation_id}-{batch_index:03d}-{item_id}"
        return HandlingUnitBatchPlan(
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
            station_access_node_ids=[str(value) for value in self.repository.outbound_stations[station_id].get("access_node_ids", [])],
            mobile_handoff_node=mobile_handoff,
            station_selection_score_ms=score,
            station_available_at_ms=station_available_at,
            station_queue_wait_ms=station_queue_wait,
            allocations=allocations,
            requested_quantity=requested_quantity,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            return_required=return_required,
            disposition=("RETURN_TO_HOME" if return_required else "CONSUMED_AT_STATION"),
            post_station_action=(
                "RETURN_TO_SOURCE" if return_required else "COMPLETE_AT_STATION"
            ),
            post_station_node=post_station_node,
            empty_tote_buffer_id=empty_buffer_id,
            station_receive_time_ms=station_receive_ms,
            station_sort_time_ms=station_sort_ms,
            station_release_time_ms=station_release_ms,
            station_service_time_ms=station_service_ms,
            station_processing_ticks=processing_ticks,
        )

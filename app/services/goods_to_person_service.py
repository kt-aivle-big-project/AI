"""Goods-to-person inventory allocation helpers used by the active compiler.

The retired standalone planning API and its duplicate solver/MAPF path were
removed. IntegratedGoodsToPersonCompiler reuses only the business allocation,
station selection, action, and mutation helpers kept here.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from app.core.config import get_settings
from app.domain.schemas import (
    HandlingUnitBatchPlan,
    InventoryMutationPreview,
    OutboundChuteAllocation,
    StationRobotAction,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository


_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


class GoodsToPersonPlanningError(RuntimeError):
    """Raised for a business- or topology-level planning rejection."""


class GoodsToPersonPlanningService:
    """Provide authoritative inventory and station allocation helpers."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()
        self.settings = get_settings()

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
        """Allocate request operations to BE ``warehouse_items`` inventory units.

        The historical method name is retained for downstream compatibility,
        but no ``handling_units`` table is required.  If a structured operation
        supplies ``source_warehouse_item_id``, that inventory row is a hard
        source constraint.  Operations without a source keep the existing
        fewest-inventory-units allocation policy.
        """

        ordered_orders = sorted(
            orders,
            key=lambda value: (
                -_PRIORITY_RANK.get(str(value.get("priority", "medium")), 1),
                str(value["order_id"]),
            ),
        )
        units = [
            dict(value)
            for value in self.repository.handling_units(item_id)
            if str(value.get("handling_unit_status", "stored")) == "stored"
            and int(value.get("quantity", 0)) > 0
        ]
        if not units:
            raise GoodsToPersonPlanningError(
                f"No stored BE warehouse_items inventory unit exists for {item_id}."
            )

        def unit_key(value: dict[str, Any]) -> str:
            return str(
                value.get("warehouse_item_id")
                or value.get("inventory_unit_id")
                or value.get("handling_unit_id")
            )

        units_by_key = {unit_key(value): value for value in units}
        remaining = {key: int(value["quantity"]) for key, value in units_by_key.items()}
        allocations_by_unit: dict[str, list[OutboundChuteAllocation]] = defaultdict(list)
        selected_keys: list[str] = []

        def select(key: str) -> None:
            if key not in selected_keys:
                selected_keys.append(key)

        def allocate(order: dict[str, Any], key: str, quantity: int) -> None:
            if quantity <= 0:
                return
            destination = str(order["logical_destination_id"])
            allocations_by_unit[key].append(
                OutboundChuteAllocation(
                    order_id=str(order["order_id"]),
                    chute_id=destination,
                    logical_destination_id=destination,
                    quantity=quantity,
                )
            )
            remaining[key] -= quantity
            select(key)

        unpinned: list[dict[str, Any]] = []
        for order in ordered_orders:
            required = int(order["required_qty"])
            source_id = order.get("source_warehouse_item_id")
            if source_id is None:
                unpinned.append(order)
                continue
            key = str(source_id)
            if key not in units_by_key:
                raise GoodsToPersonPlanningError(
                    f"Structured operation {order['order_id']} references source_warehouse_item_id="
                    f"{source_id}, but that BE inventory row is not an eligible stored unit for {item_id}."
                )
            if remaining[key] < required:
                raise GoodsToPersonPlanningError(
                    f"Structured operation {order['order_id']} requires {required} unit(s) from "
                    f"warehouse_item_id={source_id}, but only {remaining[key]} remain."
                )
            allocate(order, key, required)

        if require_single and len(selected_keys) > 1:
            raise GoodsToPersonPlanningError(
                f"Pinned structured operations for {item_id} require multiple BE inventory rows, "
                "but require_single_handling_unit=true."
            )

        # Fill all unsourced operations as one wave, preserving the historical
        # "single smallest sufficient row, otherwise largest rows first" policy.
        total_unpinned = sum(int(order["required_qty"]) for order in unpinned)
        fill_keys: list[str] = []
        if total_unpinned > 0:
            single_candidates = sorted(
                [key for key, value in remaining.items() if value >= total_unpinned],
                key=lambda key: (
                    0 if key in selected_keys else 1,
                    remaining[key] - total_unpinned,
                    key,
                ),
            )
            if single_candidates:
                fill_keys = [single_candidates[0]]
            else:
                if require_single:
                    raise GoodsToPersonPlanningError(
                        f"No single BE inventory row for {item_id} contains the remaining "
                        f"{total_unpinned} unit(s)."
                    )
                fill_keys = [
                    key for key in selected_keys if remaining.get(key, 0) > 0
                ]
                fill_keys.extend(
                    key
                    for key, value in sorted(
                        remaining.items(), key=lambda item: (-item[1], item[0])
                    )
                    if value > 0 and key not in fill_keys
                )
                cumulative = 0
                bounded: list[str] = []
                for key in fill_keys:
                    bounded.append(key)
                    cumulative += remaining[key]
                    if cumulative >= total_unpinned:
                        break
                fill_keys = bounded
                if cumulative < total_unpinned:
                    raise GoodsToPersonPlanningError(
                        f"Insufficient BE warehouse_items inventory for {item_id}: "
                        f"required={total_unpinned}, available={cumulative}."
                    )

        key_index = 0
        for order in unpinned:
            needed = int(order["required_qty"])
            while needed > 0 and key_index < len(fill_keys):
                key = fill_keys[key_index]
                amount = min(needed, remaining[key])
                allocate(order, key, amount)
                needed -= amount
                if remaining[key] <= 0:
                    key_index += 1
            if needed > 0:
                raise GoodsToPersonPlanningError(
                    f"BE inventory allocation left operation {order['order_id']} unresolved by {needed}."
                )

        if require_single and len(selected_keys) > 1:
            raise GoodsToPersonPlanningError(
                f"No single BE inventory row for {item_id} can satisfy the structured wave."
            )

        return [
            (units_by_key[key], allocations_by_unit[key])
            for key in selected_keys
            if allocations_by_unit[key]
        ]

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

    def _stations_covering_all(
        self, destinations: list[str]
    ) -> list[dict[str, Any]]:
        """Return stations capable of completing one physical HU cycle."""

        required = {str(value) for value in destinations}
        return [
            station
            for station in self.repository.outbound_station_candidates(destinations)
            if required.issubset(
                {
                    str(station.get("station_id") or ""),
                    *(str(value) for value in station.get("served_chute_ids", [])),
                }
            )
        ]

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
                    else "COMPLETE_OUTBOUND"
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
            next_status=("returning" if batch.return_required else "consumed"),
            home_rack_id=batch.source_rack_id,
            home_rack_level=batch.source_rack_level,
            post_station_node=batch.post_station_node,
            order_ids=batch.order_ids,
        )

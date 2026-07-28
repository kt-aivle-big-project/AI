"""Deterministic time-indexed inventory feasibility.

PostgreSQL current AVAILABLE lots are the opening balance.  Past movements are
audit evidence and are intentionally not replayed into that balance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Iterable

from app.models import (
    CapacityFeasibilityResult,
    InventoryFeasibilityResult,
    InventoryOperationRequest,
    InventoryProjectionPoint,
    ItemInventoryResult,
    LotAllocation,
    TaskDependency,
)
from app.time_utils import as_utc_datetime


EVENT_PRECEDENCE = {
    "INBOUND_AVAILABLE": 10,
    "CURRENT_LOT_AVAILABLE": 10,
    "COMPLETED_INVENTORY_CHANGE": 20,
    "RESERVATION_RELEASED": 30,
    "ACTIVE_PLAN_RESERVED": 40,
    "SIMULATION_RESERVED": 40,
    "OUTBOUND_START": 50,
}


def _utc(value: Any, *, default: datetime) -> datetime:
    if value in (None, ""):
        return default
    return as_utc_datetime(value, field_name="inventory_projection_time")


def _event_sort_key(row: dict[str, Any]) -> tuple[datetime, int, str]:
    return (
        row["at"],
        int(row["precedence"]),
        str(row.get("source_id") or ""),
    )


def _lot_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    expiration = row.get("expiration_at") or row.get("expiry_date")
    available = row.get("available_at") or row.get("received_at")
    return (
        str(expiration or "9999-12-31T23:59:59+00:00"),
        str(available or "9999-12-31T23:59:59+00:00"),
        str(row.get("lot_id") or row.get("warehouse_item_id") or ""),
    )


def allocate_lots_fefo(
    lots: Iterable[dict[str, Any]],
    *,
    item_id: str,
    quantity_boxes: int,
) -> list[LotAllocation]:
    """Allocate existing AVAILABLE lots by FEFO, FIFO, then lot id."""

    remaining = int(quantity_boxes)
    allocations: list[LotAllocation] = []
    for row in sorted(
        (
            value
            for value in lots
            if str(value.get("item_id")) == item_id
            and str(value.get("status") or "AVAILABLE").upper() == "AVAILABLE"
        ),
        key=_lot_sort_key,
    ):
        available = int(
            row.get("available_quantity")
            if row.get("available_quantity") is not None
            else row.get("quantity_boxes")
            if row.get("quantity_boxes") is not None
            else row.get("quantity")
            or 0
        )
        take = min(remaining, max(0, available))
        if take <= 0:
            continue
        allocations.append(
            LotAllocation(
                warehouse_item_id=str(row.get("warehouse_item_id") or row.get("lot_id")),
                item_id=(str(row["item_id"]) if row.get("item_id") is not None else None),
                lot_id=(str(row["lot_id"]) if row.get("lot_id") is not None else None),
                quantity_boxes=take,
                storage_node_id=(
                    int(row.get("storage_node_id") or row.get("node_id"))
                    if row.get("storage_node_id") is not None
                    or row.get("node_id") is not None
                    else None
                ),
                available_at=(
                    _utc(row.get("available_at"), default=datetime.min.replace(tzinfo=UTC))
                    if row.get("available_at")
                    else None
                ),
                source_type=str(row.get("source_type") or "CURRENT_LOT"),
                inbound_source_id=(
                    str(row["inbound_source_id"])
                    if row.get("inbound_source_id") is not None
                    else None
                ),
            )
        )
        remaining -= take
        if remaining == 0:
            break
    return allocations


class InventoryProjectionService:
    """Calculate warehouse/item availability at each operation timestamp."""

    def __init__(self, reference_time: datetime):
        self.reference_time = as_utc_datetime(
            reference_time, field_name="inventory_reference_time"
        )

    def _opening_balance(
        self, lots: Iterable[dict[str, Any]]
    ) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for row in lots:
            if str(row.get("status") or "AVAILABLE").upper() != "AVAILABLE":
                continue
            if (
                row.get("available_at")
                and _utc(row.get("available_at"), default=self.reference_time)
                > self.reference_time
            ):
                continue
            item_id = str(row.get("item_id") or "")
            if not item_id:
                continue
            totals[item_id] += int(
                row.get("available_quantity")
                if row.get("available_quantity") is not None
                else row.get("quantity_boxes")
                if row.get("quantity_boxes") is not None
                else row.get("quantity")
                or 0
            )
        return dict(totals)

    def _base_events(
        self,
        *,
        future_inbounds: Iterable[dict[str, Any]],
        active_reservations: Iterable[dict[str, Any]],
        simulation_events: Iterable[dict[str, Any]],
        command_operations: Iterable[InventoryOperationRequest],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        sql_operation_order_ids = {
            str(row.order_id)
            for row in command_operations
            if row.source == "SQL_ORDER" and row.order_id
        }
        for row in future_inbounds:
            status = str(row.get("status") or "SCHEDULED").upper()
            if status in {"CANCELLED", "FAILED"}:
                continue
            available_at = row.get("actual_available_at") or row.get(
                "expected_available_at"
            )
            if available_at is None:
                continue
            # An AVAILABLE order already materialized in warehouse_items is not
            # a future increment.  Replaying it would double count the lot.
            if row.get("lot_reflected") or row.get("warehouse_item_id"):
                continue
            if str(row.get("inbound_id") or row.get("order_id") or "") in sql_operation_order_ids:
                continue
            events.append(
                {
                    "at": _utc(available_at, default=self.reference_time),
                    "item_id": str(row["item_id"]),
                    "delta": int(row.get("quantity_boxes") or row.get("quantity") or 0),
                    "event_type": "INBOUND_AVAILABLE",
                    "source_id": str(row.get("inbound_id") or row.get("order_id") or ""),
                    "precedence": EVENT_PRECEDENCE["INBOUND_AVAILABLE"],
                }
            )
        for operation in command_operations:
            if operation.operation_type != "INBOUND":
                continue
            available_at = operation.actual_available_at or operation.expected_available_at
            if available_at is None:
                continue
            events.append(
                {
                    "at": available_at,
                    "item_id": operation.item_id,
                    "delta": operation.quantity_boxes,
                    "event_type": "INBOUND_AVAILABLE",
                    "source_id": operation.operation_id,
                    "precedence": EVENT_PRECEDENCE["INBOUND_AVAILABLE"],
                }
            )
        for row in active_reservations:
            if str(row.get("status") or "RESERVED").upper() != "RESERVED":
                continue
            events.append(
                {
                    "at": _utc(row.get("required_at"), default=self.reference_time),
                    "item_id": str(row["item_id"]),
                    "delta": -int(
                        row.get("remaining_quantity_boxes")
                        if row.get("remaining_quantity_boxes") is not None
                        else row.get("quantity_boxes")
                        or 0
                    ),
                    "event_type": "ACTIVE_PLAN_RESERVED",
                    "source_id": str(row.get("reservation_id") or ""),
                    "precedence": EVENT_PRECEDENCE["ACTIVE_PLAN_RESERVED"],
                }
            )
        for row in simulation_events:
            event_type = str(row.get("event_type") or "SIMULATION_RESERVED")
            delta = int(
                row.get("quantity_delta_boxes")
                if row.get("quantity_delta_boxes") is not None
                else -int(row.get("quantity_boxes") or 0)
            )
            events.append(
                {
                    "at": _utc(row.get("at") or row.get("occurred_at"), default=self.reference_time),
                    "item_id": str(row["item_id"]),
                    "delta": delta,
                    "event_type": event_type,
                    "source_id": str(row.get("source_id") or row.get("event_id") or ""),
                    "precedence": int(
                        row.get("precedence")
                        or EVENT_PRECEDENCE.get(event_type, 40)
                    ),
                }
            )
        return sorted(events, key=_event_sort_key)

    def _available_at(
        self,
        opening: dict[str, int],
        events: Iterable[dict[str, Any]],
        *,
        item_id: str,
        at: datetime,
    ) -> tuple[int, list[InventoryProjectionPoint]]:
        quantity = int(opening.get(item_id, 0))
        points = [
            InventoryProjectionPoint(
                at=min(at, self.reference_time),
                item_id=item_id,
                quantity_boxes=max(0, quantity),
                quantity_delta_boxes=0,
                event_type="OPENING_AVAILABLE_LOTS",
                precedence=0,
            )
        ]
        for row in sorted(events, key=_event_sort_key):
            if row["item_id"] != item_id or row["at"] > at:
                continue
            quantity += int(row["delta"])
            points.append(
                InventoryProjectionPoint(
                    at=row["at"],
                    item_id=item_id,
                    quantity_boxes=max(0, quantity),
                    quantity_delta_boxes=int(row["delta"]),
                    event_type=row["event_type"],
                    source_id=row.get("source_id") or None,
                    precedence=int(row["precedence"]),
                )
            )
        return max(0, quantity), points

    @staticmethod
    def _earliest_full(
        *,
        available: int,
        requested: int,
        required_at: datetime,
        item_id: str,
        events: Iterable[dict[str, Any]],
    ) -> datetime | None:
        quantity = int(available)
        for row in sorted(events, key=_event_sort_key):
            if row["item_id"] != item_id or row["at"] <= required_at:
                continue
            delta = int(row.get("delta") or 0)
            # Never manufacture an availability timestamp from an anonymous or
            # non-positive projection event.  A future fulfillment time is
            # reported only when PostgreSQL/Redis supplied an identifiable
            # positive inventory event such as an inbound, a registered lot
            # becoming usable, or a reservation release.
            if delta <= 0 or not str(row.get("source_id") or "").strip():
                continue
            quantity += delta
            if quantity >= requested:
                return row["at"]
        return None

    def evaluate(
        self,
        operations: Iterable[InventoryOperationRequest | dict[str, Any]],
        *,
        current_lots: Iterable[dict[str, Any]],
        future_inbounds: Iterable[dict[str, Any]] = (),
        active_reservations: Iterable[dict[str, Any]] = (),
        simulation_events: Iterable[dict[str, Any]] = (),
        dependencies: Iterable[TaskDependency | dict[str, Any]] = (),
    ) -> InventoryFeasibilityResult:
        operation_rows = [
            row
            if isinstance(row, InventoryOperationRequest)
            else InventoryOperationRequest.model_validate(row)
            for row in operations
        ]
        if not operation_rows:
            return InventoryFeasibilityResult(
                status="NOT_APPLICABLE",
                valid=True,
            )
        lot_rows = [dict(row) for row in current_lots]
        working_lots = [dict(row) for row in lot_rows]
        for row in active_reservations:
            if str(row.get("status") or "RESERVED").upper() != "RESERVED":
                continue
            for allocation in row.get("lot_allocations", []):
                allocation_id = str(allocation.get("warehouse_item_id"))
                for lot in working_lots:
                    if str(lot.get("warehouse_item_id")) == allocation_id:
                        available = int(
                            lot.get("available_quantity")
                            if lot.get("available_quantity") is not None
                            else lot.get("quantity")
                            or 0
                        )
                        lot["available_quantity"] = max(
                            0,
                            available - int(allocation.get("quantity_boxes") or 0),
                        )
        command_order_ids = {
            str(row.order_id)
            for row in operation_rows
            if row.source == "SQL_ORDER" and row.order_id
        }
        for row in future_inbounds:
            available_at = row.get("actual_available_at") or row.get(
                "expected_available_at"
            )
            inbound_id = str(row.get("inbound_id") or row.get("order_id") or "")
            if (
                available_at is None
                or row.get("lot_reflected")
                or row.get("warehouse_item_id")
                or inbound_id in command_order_ids
                or str(row.get("status") or "").upper() in {"CANCELLED", "FAILED"}
            ):
                continue
            working_lots.append(
                {
                    **dict(row),
                    "warehouse_item_id": f"FUTURE:{inbound_id}",
                    "available_quantity": int(row.get("quantity_boxes") or 0),
                    "available_at": available_at,
                    "status": "AVAILABLE",
                    "source_type": "FUTURE_INBOUND",
                    "inbound_source_id": inbound_id,
                }
            )
        for operation in operation_rows:
            if operation.operation_type != "INBOUND":
                continue
            available_at = operation.actual_available_at or operation.expected_available_at
            if available_at is None:
                continue
            working_lots.append(
                {
                    "warehouse_item_id": operation.warehouse_item_id
                    or f"FUTURE:{operation.operation_id}",
                    "item_id": operation.item_id,
                    "lot_id": operation.lot_id,
                    "storage_node_id": operation.storage_node_id,
                    "available_quantity": operation.quantity_boxes,
                    "available_at": available_at,
                    "status": "AVAILABLE",
                    "source_type": "FUTURE_INBOUND",
                    "inbound_source_id": operation.operation_id,
                }
            )
        opening = self._opening_balance(lot_rows)
        events = self._base_events(
            future_inbounds=future_inbounds,
            active_reservations=active_reservations,
            simulation_events=simulation_events,
            command_operations=operation_rows,
        )
        # A warehouse item can already exist in PostgreSQL while becoming
        # usable shortly after the planning reference (quality release,
        # receiving completion, etc.).  It is excluded from the opening
        # balance, so add an availability event; otherwise the projection
        # incorrectly jumps to a much later inbound order.
        for row in lot_rows:
            if str(row.get("status") or "AVAILABLE").upper() != "AVAILABLE":
                continue
            if not row.get("available_at"):
                continue
            available_at = _utc(row.get("available_at"), default=self.reference_time)
            if available_at <= self.reference_time:
                continue
            quantity = int(
                row.get("available_quantity")
                if row.get("available_quantity") is not None
                else row.get("quantity_boxes")
                if row.get("quantity_boxes") is not None
                else row.get("quantity")
                or 0
            )
            if quantity <= 0 or not row.get("item_id"):
                continue
            events.append(
                {
                    "at": available_at,
                    "item_id": str(row["item_id"]),
                    "delta": quantity,
                    "event_type": "CURRENT_LOT_AVAILABLE",
                    "source_id": str(
                        row.get("warehouse_item_id") or row.get("lot_id") or ""
                    ),
                    "precedence": EVENT_PRECEDENCE["CURRENT_LOT_AVAILABLE"],
                }
            )
        events = sorted(events, key=_event_sort_key)
        results: list[ItemInventoryResult] = []
        shortage_ids: list[str] = []
        independent_ids: list[str] = []
        ordered = sorted(
            operation_rows,
            key=lambda row: (
                row.required_at
                or row.expected_available_at
                or self.reference_time,
                0 if row.operation_type == "INBOUND" else 1,
                row.operation_id,
            ),
        )
        for operation in ordered:
            operation_ref = operation.work_id or operation.operation_id
            if operation.operation_type == "INBOUND":
                results.append(
                    ItemInventoryResult(
                        operation_id=operation.operation_id,
                        work_id=operation.work_id,
                        order_id=operation.order_id,
                        operation_type="INBOUND",
                        item_id=operation.item_id,
                        requested_quantity_boxes=operation.quantity_boxes,
                        planned_quantity_boxes=operation.quantity_boxes,
                        available_quantity_boxes=int(opening.get(operation.item_id, 0)),
                        shortage_quantity_boxes=0,
                        required_at=operation.expected_available_at,
                        earliest_full_fulfillment_at=operation.expected_available_at,
                        status="PASS",
                    )
                )
                independent_ids.append(operation_ref)
                continue

            required_at = operation.required_at or self.reference_time
            available, points = self._available_at(
                opening,
                events,
                item_id=operation.item_id,
                at=required_at,
            )
            shortage = max(0, operation.quantity_boxes - available)
            partial = shortage > 0 and operation.allow_partial_fulfillment and available > 0
            planned = available if partial else operation.quantity_boxes if shortage == 0 else 0
            if shortage:
                status = (
                    "PARTIAL_FULFILLMENT_APPROVED"
                    if partial
                    else "EMERGENCY_REVIEW_REQUIRED"
                )
            else:
                status = "PASS"
            earliest = (
                None
                if shortage == 0
                else self._earliest_full(
                    available=available,
                    requested=operation.quantity_boxes,
                    required_at=required_at,
                    item_id=operation.item_id,
                    events=events,
                )
            )
            eligible_lots = [
                row
                for row in working_lots
                if _utc(row.get("available_at"), default=self.reference_time)
                <= required_at
            ]
            allocations = allocate_lots_fefo(
                eligible_lots,
                item_id=operation.item_id,
                quantity_boxes=planned,
            ) if planned else []
            if sum(row.quantity_boxes for row in allocations) < planned:
                # Quantity may be projected but cannot be routed without a Lot/source.
                allocations = []
                planned = 0
                shortage = operation.quantity_boxes
                partial = False
                status = "EMERGENCY_REVIEW_REQUIRED"
            results.append(
                ItemInventoryResult(
                    operation_id=operation.operation_id,
                    work_id=operation.work_id,
                    order_id=operation.order_id,
                    operation_type="OUTBOUND",
                    item_id=operation.item_id,
                    requested_quantity_boxes=operation.quantity_boxes,
                    planned_quantity_boxes=planned,
                    available_quantity_boxes=available,
                    shortage_quantity_boxes=shortage,
                    required_at=required_at,
                    earliest_full_fulfillment_at=earliest,
                    status=status,
                    allow_partial_fulfillment=operation.allow_partial_fulfillment,
                    projection=points,
                    lot_allocations=allocations,
                )
            )
            if shortage and not partial:
                shortage_ids.append(operation_ref)
                continue
            independent_ids.append(operation_ref)
            if planned:
                for allocation in allocations:
                    for lot in working_lots:
                        if str(lot.get("warehouse_item_id")) == allocation.warehouse_item_id:
                            current = int(
                                lot.get("available_quantity")
                                if lot.get("available_quantity") is not None
                                else lot.get("quantity")
                                or 0
                            )
                            lot["available_quantity"] = current - allocation.quantity_boxes
                events.append(
                    {
                        "at": required_at,
                        "item_id": operation.item_id,
                        "delta": -planned,
                        "event_type": "OUTBOUND_START",
                        "source_id": operation.operation_id,
                        "precedence": EVENT_PRECEDENCE["OUTBOUND_START"],
                    }
                )

        dependency_rows = [
            row if isinstance(row, TaskDependency) else TaskDependency.model_validate(row)
            for row in dependencies
        ]
        blocked = set(shortage_ids)
        changed = True
        while changed:
            changed = False
            for row in dependency_rows:
                if (
                    row.predecessor_work_id in blocked
                    and row.successor_work_id not in blocked
                ):
                    blocked.add(row.successor_work_id)
                    changed = True
        blocked_successors = sorted(blocked - set(shortage_ids))
        independent = sorted(set(independent_ids) - blocked)
        if not shortage_ids:
            status = "PASS"
            valid = True
            partial_success = any(
                row.status == "PARTIAL_FULFILLMENT_APPROVED" for row in results
            )
        elif independent:
            status = "PARTIAL_SUCCESS"
            valid = True
            partial_success = True
        else:
            status = "FAILED"
            valid = False
            partial_success = False
        return InventoryFeasibilityResult(
            status=status,
            valid=valid,
            partial_success=partial_success,
            item_results=results,
            shortage_work_ids=sorted(set(shortage_ids)),
            blocked_work_ids=blocked_successors,
            independent_work_ids=independent,
        )


def capacity_feasibility(
    operation_types: Iterable[str],
    capacity: dict[str, Any] | None,
) -> CapacityFeasibilityResult:
    if "INBOUND" not in {str(value).upper() for value in operation_types}:
        return CapacityFeasibilityResult(status="NOT_APPLICABLE")
    if not capacity or capacity.get("capacity_value") is None:
        return CapacityFeasibilityResult(
            status="NOT_CONFIGURED",
            warnings=["CAPACITY_DATA_NOT_CONFIGURED"],
        )
    return CapacityFeasibilityResult(
        status="PASS",
        capacity_value=float(capacity["capacity_value"]),
        capacity_unit=str(capacity.get("capacity_unit") or "BOX"),
        capacity_type=(
            str(capacity["capacity_type"])
            if capacity.get("capacity_type") is not None
            else None
        ),
        usable_capacity_value=(
            float(capacity["usable_capacity_value"])
            if capacity.get("usable_capacity_value") is not None
            else None
        ),
    )

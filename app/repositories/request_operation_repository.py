"""Request-scoped business-operation overlay.

The BE sends authoritative structured operations with the plan request.  This
adapter exposes those operations through the stable repository methods used by
existing Rule/Agent/G2P nodes, so no durable ``orders`` or ``handling_units``
master table is required.  Inventory units are still read from the BE's
``warehouse_items`` table by the wrapped repository.
"""
from __future__ import annotations

import json
from typing import Any

from app.domain.schemas import StructuredMissionInput, StructuredOperationInput


class RequestOperationRepository:
    """Delegate warehouse reads while sourcing business operations from request."""

    def __init__(self, base: Any, structured_input: StructuredMissionInput | None) -> None:
        self.base = base
        self.structured_input = structured_input
        self.warehouse_id = base.warehouse_id
        self.simulation_id = base.simulation_id
        self._outbound: dict[str, dict[str, Any]] = {}
        self._inbound: dict[str, dict[str, Any]] = {}
        self._materialize_operations()

        # Existing G2P helpers inspect these indexes directly.  They are
        # request-scoped copies, not durable business tables.
        self.orders = dict(self._outbound)
        self.inbound_receipts = dict(self._inbound)
        self.outbound_chutes = dict(getattr(base, "outbound_chutes", {}))
        self._ensure_request_destinations()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    @property
    def versions(self) -> dict[str, str]:
        values = dict(self.base.versions)
        request_id = (
            self.structured_input.request_id
            if self.structured_input is not None
            else None
        )
        values["business_version"] = request_id or "request-structured-input"
        return values

    @property
    def source_manifest(self) -> dict[str, str]:
        values = dict(getattr(self.base, "source_manifest", {}))
        values.update(
            {
                "operations": "request_structured_input",
                "orders": "not_used",
                "handling_units": "not_used",
                "inventory_units": "be_warehouse_items_live",
            }
        )
        return values

    def _item_code(self, operation: StructuredOperationInput) -> str:
        resolver = getattr(self.base, "canonical_item_code", None)
        if callable(resolver):
            return str(resolver(operation.item_id, operation.product_code))
        return str(operation.product_code or operation.item_id)

    @staticmethod
    def _priority(operation: StructuredOperationInput) -> str:
        return str(operation.priority)

    @staticmethod
    def _transport_unit_count(operation: StructuredOperationInput) -> int | None:
        try:
            attributes = json.loads(operation.attributes or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(attributes, dict):
            return None
        value = attributes.get("box_count")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _resolve_node_code(
        self,
        *,
        node_code: str | None,
        node_id: int | None,
        facility_code: str | None,
        storage_location_id: int | None,
    ) -> str | None:
        if node_code:
            return str(node_code)
        if facility_code:
            facility = getattr(self.base, "facility_by_code", lambda _: None)(facility_code)
            if facility:
                return str(facility.get("access_node_id") or facility.get("node_code") or facility_code)
            return str(facility_code)
        if node_id is not None:
            value = getattr(self.base, "node_code_for_numeric_id", lambda _: None)(node_id)
            return str(value) if value else None
        if storage_location_id is not None:
            value = getattr(self.base, "node_code_for_storage_location", lambda _: None)(
                storage_location_id
            )
            return str(value) if value else None
        return None

    def _materialize_operations(self) -> None:
        if self.structured_input is None:
            return
        for operation in self.structured_input.operations:
            if operation.operation_type == "OUTBOUND":
                destination_access_node = self._resolve_node_code(
                    node_code=operation.destination_node_code,
                    node_id=operation.destination_node_id,
                    facility_code=operation.destination_facility_code,
                    storage_location_id=operation.destination_storage_location_id,
                )
                # Goods-to-person station coverage is expressed with the
                # canonical facility/station ID, while robot travel ends at one
                # of that station's physical access nodes. Mixing the two made
                # OUT_STATION_1_ACCESS_A look like a new logical chute and could
                # eliminate every station candidate.
                logical_destination = (
                    str(operation.destination_facility_code)
                    if operation.destination_facility_code
                    else destination_access_node
                )
                self._outbound[operation.operation_id] = {
                    "warehouse_id": self.warehouse_id,
                    "order_id": operation.operation_id,
                    "task_id": operation.task_id,
                    "item_id": self._item_code(operation),
                    "required_qty": int(operation.quantity),
                    "delivery_node": logical_destination,
                    "delivery_access_node": destination_access_node,
                    "outbound_chute_id": logical_destination,
                    "logical_destination_id": logical_destination,
                    "priority": self._priority(operation),
                    "status": "pending",
                    "preferred_station_ids": [],
                    "source_warehouse_item_id": operation.source_warehouse_item_id,
                    "source_storage_location_id": operation.source_storage_location_id,
                    "source_node_id": operation.source_node_id,
                    "source_node_code": operation.source_node_code,
                    "release_at_ms": int(operation.release_at_ms),
                    "pickup_service_time_ms": int(operation.pickup_service_time_ms),
                    "drop_service_time_ms": int(operation.drop_service_time_ms),
                    "attributes": operation.attributes,
                }
            elif operation.operation_type in {"INBOUND", "TRANSFER"}:
                source = self._resolve_node_code(
                    node_code=operation.source_node_code,
                    node_id=operation.source_node_id,
                    facility_code=operation.source_facility_code,
                    storage_location_id=operation.source_storage_location_id,
                )
                destination = self._resolve_node_code(
                    node_code=operation.destination_node_code,
                    node_id=operation.destination_node_id,
                    facility_code=operation.destination_facility_code,
                    storage_location_id=operation.destination_storage_location_id,
                )
                self._inbound[operation.operation_id] = {
                    "warehouse_id": self.warehouse_id,
                    "inbound_id": operation.operation_id,
                    # A request-local inventory movement ID preserves existing
                    # compiler contracts without creating a handling_units row.
                    "handling_unit_id": f"REQ-{operation.operation_id}",
                    "inventory_unit_id": f"REQ-{operation.operation_id}",
                    "task_id": operation.task_id,
                    "item_id": self._item_code(operation),
                    "quantity": int(operation.quantity),
                    "transport_unit_count": self._transport_unit_count(operation),
                    "source_port_id": operation.source_facility_code or source,
                    "source_node": source,
                    "target_node": destination,
                    "target_rack_id": self._resolve_target_rack_id(destination),
                    "target_rack_level": operation.target_rack_level,
                    "priority": self._priority(operation),
                    "status": "arrived",
                    "release_at_ms": int(operation.release_at_ms),
                    "pickup_service_time_ms": int(operation.pickup_service_time_ms),
                    "drop_service_time_ms": int(operation.drop_service_time_ms),
                    "attributes": operation.attributes,
                }

    def _resolve_target_rack_id(self, destination: str | None) -> str | None:
        """Resolve either a route access node or an authoritative BE rack code.

        Replan requests carry the physical rack stored on the Spring ``Task``.
        That rack is not itself a MAPF route node, so the old access-node-only
        lookup returned ``None`` and allowed putaway to be selected again.
        """

        if not destination:
            return None
        destination = str(destination)
        rack_id = getattr(
            self.base, "rack_id_for_access_node", lambda _: None
        )(destination)
        if rack_id:
            return str(rack_id)

        rack = getattr(self.base, "rack", lambda _: None)(destination)
        if rack is not None:
            return destination
        access_nodes = getattr(self.base, "rack_access_nodes", lambda _: [])(
            destination
        )
        return destination if access_nodes else None

    def _ensure_request_destinations(self) -> None:
        for value in self._outbound.values():
            destination = str(value.get("logical_destination_id") or "")
            if not destination:
                continue
            self.outbound_chutes.setdefault(
                destination,
                {
                    "warehouse_id": self.warehouse_id,
                    "chute_id": destination,
                    "label": destination,
                    "source": "request_structured_input",
                },
            )

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        value = self._outbound.get(str(order_id))
        return dict(value) if value else None

    def find_orders(
        self,
        *,
        order_ids: list[str] | None = None,
        item_ids: list[str] | None = None,
        item_text: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if item_text:
            return []
        values = list(self._outbound.values())
        if order_ids:
            allowed = {str(value) for value in order_ids}
            values = [value for value in values if str(value["order_id"]) in allowed]
        if item_ids:
            allowed = {str(value) for value in item_ids}
            values = [value for value in values if str(value["item_id"]) in allowed]
        if statuses:
            allowed = {str(value) for value in statuses}
            values = [value for value in values if str(value.get("status")) in allowed]
        return [dict(value) for value in sorted(values, key=lambda row: str(row["order_id"]))]

    def get_inbound_receipt(self, inbound_id: str) -> dict[str, Any] | None:
        value = self._inbound.get(str(inbound_id))
        return dict(value) if value else None

    def all_inbound_receipts(self) -> list[dict[str, Any]]:
        return [
            dict(value)
            for value in sorted(self._inbound.values(), key=lambda row: str(row["inbound_id"]))
        ]

    def empty_putaway_slots(self) -> list[dict[str, Any]]:
        """Expose free slots plus slots already reserved by these exact BE tasks.

        A running inbound task owns its destination even though that rack level is
        no longer returned by the BE's generic empty-slot query.  Replanning must
        keep that physical contract instead of deferring the task or selecting a
        second destination.  The reservation marker prevents unrelated/new
        inbound operations from taking the committed slot.
        """

        values = [dict(value) for value in self.base.empty_putaway_slots()]
        by_key = {
            (str(value.get("rack_id")), int(value.get("rack_level") or 0)): value
            for value in values
        }
        for inbound in self._inbound.values():
            task_id = inbound.get("task_id")
            rack_id = inbound.get("target_rack_id")
            rack_level = inbound.get("target_rack_level")
            if task_id is None or not rack_id or rack_level is None:
                continue
            key = (str(rack_id), int(rack_level))
            existing = by_key.get(key)
            if existing is not None:
                existing["reservation_task_id"] = int(task_id)
                continue
            access_node_ids = list(self.base.rack_access_nodes(str(rack_id)))
            if not access_node_ids:
                continue
            committed = {
                "rack_id": str(rack_id),
                "rack_level": int(rack_level),
                "access_node_ids": access_node_ids,
                "capacity": 0,
                "reservation_task_id": int(task_id),
            }
            values.append(committed)
            by_key[key] = committed
        return values

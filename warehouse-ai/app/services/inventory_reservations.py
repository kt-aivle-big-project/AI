"""Concurrency-safe ACTIVE_PLAN inventory reservations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.models import (
    InventoryReservationSummary,
    ItemInventoryResult,
    LotAllocation,
)
from app.services.inventory_projection import allocate_lots_fefo


class InventoryReservationConflict(RuntimeError):
    code = "INVENTORY_RESERVATION_CONFLICT"


def _reservation_id(idempotency_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"inventory-reservation:{idempotency_key}"))


class InventoryReservationService:
    def __init__(self, postgres_repository: Any, redis_repository: Any):
        self.postgres = postgres_repository
        self.redis = redis_repository

    def reserve_active_plan(
        self,
        *,
        warehouse_id: int,
        plan_version: str,
        item_results: Iterable[ItemInventoryResult | dict[str, Any]],
        replace_plan_version: str | None = None,
    ) -> list[InventoryReservationSummary]:
        results = [
            row
            if isinstance(row, ItemInventoryResult)
            else ItemInventoryResult.model_validate(row)
            for row in item_results
            if (
                row.operation_type if isinstance(row, ItemInventoryResult)
                else row.get("operation_type")
            )
            == "OUTBOUND"
            and int(
                row.planned_quantity_boxes
                if isinstance(row, ItemInventoryResult)
                else row.get("planned_quantity_boxes") or 0
            )
            > 0
        ]
        if not results:
            return []
        by_item: dict[str, list[ItemInventoryResult]] = {}
        for row in results:
            by_item.setdefault(row.item_id, []).append(row)

        token = str(uuid4())
        locked: list[str] = []
        try:
            for item_id in sorted(by_item):
                if not self.redis.acquire_inventory_lock(
                    warehouse_id, item_id, token, ttl_seconds=15
                ):
                    raise InventoryReservationConflict(
                        f"{item_id} 재고 예약 lock을 획득하지 못했습니다."
                    )
                locked.append(item_id)

            current_lots = self.postgres.fetch_inventory(
                warehouse_id, sorted(by_item)
            )
            future_lots: list[dict[str, Any]] = []
            if hasattr(self.postgres, "fetch_inbound_orders"):
                for row in self.postgres.fetch_inbound_orders(
                    warehouse_id, sorted(by_item)
                ):
                    available_at = row.get("actual_available_at") or row.get(
                        "expected_available_at"
                    )
                    if (
                        available_at is None
                        or row.get("lot_reflected")
                        or row.get("warehouse_item_id")
                        or str(row.get("status") or "").upper()
                        in {"CANCELLED", "FAILED"}
                    ):
                        continue
                    inbound_id = str(row.get("inbound_id") or row.get("order_id"))
                    future_lots.append(
                        {
                            **dict(row),
                            "warehouse_item_id": f"FUTURE:{inbound_id}",
                            "available_quantity": int(
                                row.get("quantity_boxes") or 0
                            ),
                            "available_at": available_at,
                            "status": "AVAILABLE",
                        }
                    )
            existing = self.redis.list_inventory_reservations(
                warehouse_id,
                scope="ACTIVE_PLAN",
                statuses={"RESERVED"},
            )
            existing_for_plan = [
                row for row in existing if str(row.get("plan_version")) == plan_version
            ]
            if existing_for_plan:
                return [
                    InventoryReservationSummary.model_validate(row)
                    for row in existing_for_plan
                ]
            reserved_by_lot: dict[str, int] = {}
            for row in existing:
                if (
                    replace_plan_version
                    and str(row.get("plan_version")) == replace_plan_version
                ):
                    continue
                for allocation in row.get("lot_allocations", []):
                    key = str(allocation["warehouse_item_id"])
                    reserved_by_lot[key] = reserved_by_lot.get(key, 0) + int(
                        allocation.get("quantity_boxes") or 0
                    )
            adjusted_lots = []
            for raw in [*current_lots, *future_lots]:
                row = dict(raw)
                key = str(row.get("warehouse_item_id"))
                available = int(row.get("available_quantity") or 0) - int(
                    reserved_by_lot.get(key, 0)
                )
                row["available_quantity"] = max(0, available)
                adjusted_lots.append(row)

            reservations: list[InventoryReservationSummary] = []
            for item_id in sorted(by_item):
                for result in by_item[item_id]:
                    eligible_lots = [
                        row
                        for row in adjusted_lots
                        if str(row.get("item_id")) == item_id
                        and (
                            not str(row.get("warehouse_item_id")).startswith("FUTURE:")
                            or
                            not row.get("available_at")
                            or (
                                result.required_at is not None
                                and datetime.fromisoformat(
                                    str(row["available_at"]).replace("Z", "+00:00")
                                )
                                <= result.required_at
                            )
                        )
                    ]
                    allocations = allocate_lots_fefo(
                        eligible_lots,
                        item_id=item_id,
                        quantity_boxes=result.planned_quantity_boxes,
                    )
                    if sum(row.quantity_boxes for row in allocations) < result.planned_quantity_boxes:
                        raise InventoryReservationConflict(
                            f"{item_id} Lot 할당 가능 수량이 부족합니다."
                        )
                    for allocation in allocations:
                        for lot in adjusted_lots:
                            if str(lot.get("warehouse_item_id")) == allocation.warehouse_item_id:
                                lot["available_quantity"] = int(
                                    lot.get("available_quantity") or 0
                                ) - allocation.quantity_boxes
                    work_id = result.work_id or result.operation_id
                    idempotency_key = f"{plan_version}:{work_id}:{item_id}"
                    reservations.append(
                        InventoryReservationSummary(
                            reservation_id=_reservation_id(idempotency_key),
                            warehouse_id=warehouse_id,
                            item_id=item_id,
                            quantity_boxes=result.planned_quantity_boxes,
                            work_id=work_id,
                            order_id=result.order_id,
                            plan_version=plan_version,
                            scope="ACTIVE_PLAN",
                            status="RESERVED",
                            required_at=result.required_at,
                            idempotency_key=idempotency_key,
                            lot_allocations=allocations,
                        )
                    )
            stored = self.redis.save_inventory_reservations(
                warehouse_id,
                [row.model_dump(mode="json") for row in reservations],
            )
            return [InventoryReservationSummary.model_validate(row) for row in stored]
        finally:
            for item_id in reversed(locked):
                self.redis.release_inventory_lock(warehouse_id, item_id, token)

    def release_plan(
        self, warehouse_id: int, plan_version: str, *, status: str = "RELEASED"
    ) -> list[dict[str, Any]]:
        return self.redis.update_inventory_reservations(
            warehouse_id,
            plan_version=plan_version,
            from_statuses={"RESERVED"},
            status=status,
        )


def simulation_reservation_summaries(
    *,
    warehouse_id: int,
    simulation_id: str,
    plan_version: str,
    item_results: Iterable[ItemInventoryResult | dict[str, Any]],
) -> list[InventoryReservationSummary]:
    """Create response-only simulation reservations; no global Redis write."""

    rows: list[InventoryReservationSummary] = []
    for raw in item_results:
        result = (
            raw
            if isinstance(raw, ItemInventoryResult)
            else ItemInventoryResult.model_validate(raw)
        )
        if result.operation_type != "OUTBOUND" or result.planned_quantity_boxes <= 0:
            continue
        work_id = result.work_id or result.operation_id
        key = f"{simulation_id}:{plan_version}:{work_id}:{result.item_id}"
        rows.append(
            InventoryReservationSummary(
                reservation_id=_reservation_id(key),
                warehouse_id=warehouse_id,
                item_id=result.item_id,
                quantity_boxes=result.planned_quantity_boxes,
                work_id=work_id,
                order_id=result.order_id,
                plan_version=plan_version,
                scope="SIMULATION",
                status="RESERVED",
                required_at=result.required_at,
                simulation_id=simulation_id,
                idempotency_key=key,
                lot_allocations=result.lot_allocations,
            )
        )
    return rows

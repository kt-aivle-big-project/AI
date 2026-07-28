from collections.abc import Iterable, Mapping
from typing import Any

from app.models import InventoryDelta


def calculate_inventory_transition(
    current_quantities: Mapping[str, int],
    deltas: Iterable[InventoryDelta | dict[str, Any]],
) -> dict[str, int]:
    """REAL과 SIMULATION이 공유하는 결정적 재고 수량 전이 규칙."""

    next_quantities = {
        str(warehouse_item_id): int(quantity)
        for warehouse_item_id, quantity in current_quantities.items()
    }
    for raw_delta in deltas:
        delta = (
            raw_delta
            if isinstance(raw_delta, InventoryDelta)
            else InventoryDelta.model_validate(raw_delta)
        )
        warehouse_item_id = str(delta.warehouse_item_id)
        if warehouse_item_id not in next_quantities:
            raise RuntimeError(f"재고 항목을 찾을 수 없습니다: {warehouse_item_id}")
        new_quantity = (
            next_quantities[warehouse_item_id] + int(delta.quantity_delta)
        )
        if new_quantity < 0:
            raise RuntimeError(
                f"재고 음수 방지: {warehouse_item_id}, 결과={new_quantity}"
            )
        next_quantities[warehouse_item_id] = new_quantity
    return next_quantities

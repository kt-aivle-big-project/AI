from __future__ import annotations

import math
from typing import Any, Iterable


def _canonical_robot_id(value: Any) -> str:
    return str(value or "").strip().upper().replace("_", "-")


def outbound_trip_capacity(
    robots: Iterable[dict[str, Any]],
    *,
    fixed_robot_id: str | None = None,
    included_robot_ids: Iterable[str] = (),
    excluded_robot_ids: Iterable[str] = (),
) -> int | None:
    """Return the largest usable robot load for one outbound trip.

    A fixed robot takes precedence. Otherwise included/excluded robot filters are
    applied before choosing the largest positive max_load. ``None`` means the
    snapshot did not provide a finite load limit, so the allocation is left as-is.
    """

    fixed = _canonical_robot_id(fixed_robot_id) if fixed_robot_id else None
    included = {_canonical_robot_id(value) for value in included_robot_ids if value}
    excluded = {_canonical_robot_id(value) for value in excluded_robot_ids if value}

    capacities: list[int] = []
    for robot in robots:
        robot_id = _canonical_robot_id(robot.get("robot_id"))
        if not robot_id or robot_id in excluded:
            continue
        if fixed and robot_id != fixed:
            continue
        if included and robot_id not in included:
            continue

        raw_capacity = robot.get("max_load")
        if raw_capacity in (None, ""):
            continue
        try:
            capacity = int(math.floor(float(raw_capacity)))
        except (TypeError, ValueError, OverflowError):
            continue
        if capacity > 0:
            capacities.append(capacity)

    return max(capacities) if capacities else None


def split_allocation_by_capacity(
    allocation: dict[str, Any],
    max_quantity: int | None,
) -> list[dict[str, Any]]:
    """Split one inventory allocation into robot-carryable trip chunks."""

    quantity = int(
        allocation.get("quantity_boxes")
        or allocation.get("quantity")
        or 0
    )
    if quantity <= 0:
        return []
    if max_quantity is None or max_quantity <= 0 or quantity <= max_quantity:
        row = dict(allocation)
        row["quantity"] = quantity
        row["quantity_boxes"] = quantity
        return [row]

    rows: list[dict[str, Any]] = []
    remaining = quantity
    while remaining > 0:
        take = min(remaining, max_quantity)
        row = dict(allocation)
        row["quantity"] = take
        row["quantity_boxes"] = take
        rows.append(row)
        remaining -= take
    return rows


def capacity_trip_pairs(
    allocation: dict[str, Any],
    max_quantity: int | None,
    *,
    prefix_base: str,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    """Build independent PICK/DROP pairs for capacity-split outbound trips.

    Each trip may be assigned to a different robot and may run in parallel with
    other trips. Only the DROP of the same trip depends on its PICK.
    """

    pairs: list[dict[str, Any]] = []
    for index, row in enumerate(
        split_allocation_by_capacity(allocation, max_quantity), start=start_index
    ):
        prefix = f"{prefix_base}:{index}"
        pick_id = f"{prefix}:pick"
        drop_id = f"{prefix}:drop"
        pairs.append(
            {
                "prefix": prefix,
                "pick_id": pick_id,
                "drop_id": drop_id,
                "pick_predecessors": [],
                "drop_predecessors": [pick_id],
                "allocation": row,
            }
        )
    return pairs


def capacity_trip_groups(
    allocations: Iterable[dict[str, Any]],
    max_quantity: int | None,
    *,
    prefix_base: str,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    """Pack FEFO lot allocations from the same node into transport trips.

    Lot identity remains in ``allocations`` for reservations and inventory
    commits, while the physical PICK/DROP pair is created once per robot load.
    """

    rows = [dict(row) for row in allocations]
    grouped: dict[int, list[dict[str, Any]]] = {}
    node_order: list[int] = []
    for row in rows:
        node_raw = row.get("node_id") or row.get("storage_node_id")
        if node_raw is None:
            continue
        node_id = int(node_raw)
        if node_id not in grouped:
            grouped[node_id] = []
            node_order.append(node_id)
        quantity = int(row.get("quantity_boxes") or row.get("quantity") or 0)
        if quantity <= 0:
            continue
        row["node_id"] = node_id
        row["storage_node_id"] = node_id
        row["quantity"] = quantity
        row["quantity_boxes"] = quantity
        grouped[node_id].append(row)

    trip_rows: list[dict[str, Any]] = []
    capacity = int(max_quantity) if max_quantity and int(max_quantity) > 0 else None
    for node_id in node_order:
        current: list[dict[str, Any]] = []
        current_quantity = 0

        def flush() -> None:
            nonlocal current, current_quantity
            if not current:
                return
            trip_rows.append(
                {
                    "source_node": node_id,
                    "quantity_boxes": current_quantity,
                    "allocations": current,
                }
            )
            current = []
            current_quantity = 0

        for raw in grouped[node_id]:
            remaining = int(raw["quantity_boxes"])
            while remaining > 0:
                room = remaining if capacity is None else capacity - current_quantity
                if room <= 0:
                    flush()
                    room = remaining if capacity is None else capacity
                take = min(remaining, room)
                piece = dict(raw)
                piece["quantity"] = take
                piece["quantity_boxes"] = take
                current.append(piece)
                current_quantity += take
                remaining -= take
                if capacity is not None and current_quantity >= capacity:
                    flush()
        flush()

    pairs: list[dict[str, Any]] = []
    for index, trip in enumerate(trip_rows, start=start_index):
        prefix = f"{prefix_base}:{index}"
        pick_id = f"{prefix}:pick"
        drop_id = f"{prefix}:drop"
        available_values = [
            row.get("available_at")
            for row in trip["allocations"]
            if row.get("available_at") is not None
        ]
        pairs.append(
            {
                "prefix": prefix,
                "pick_id": pick_id,
                "drop_id": drop_id,
                "pick_predecessors": [],
                "drop_predecessors": [pick_id],
                "source_node": trip["source_node"],
                "quantity_boxes": trip["quantity_boxes"],
                "allocations": trip["allocations"],
                "available_at": max(available_values) if available_values else None,
            }
        )
    return pairs

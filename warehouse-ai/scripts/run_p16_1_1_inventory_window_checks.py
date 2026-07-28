"""Run deterministic P16.1.1 inventory-window checks.

No PostgreSQL, Redis, Neo4j, or LLM connection is required. The checks cover
future-dated current lots, hard-window availability adjustment, mixed-item
partial success, and required_at/required_by normalization.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import (
    CommandInterpretation,
    InventoryOperationRequest,
    TaskScheduleConstraint,
)
from app.planning.nodes import inventory_precheck_node


REFERENCE = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)
LOT_AVAILABLE = datetime(2026, 7, 24, 0, 4, 31, tzinfo=UTC)


def _interpretation(operations: list[InventoryOperationRequest]) -> CommandInterpretation:
    return CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective="P16.1.1 inventory window check",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=operations,
        scheduled_task_constraints=[
            TaskScheduleConstraint(
                work_id=row.operation_id,
                earliest_start=WINDOW_START,
                latest_finish=WINDOW_END,
                time_constraint_type="HARD_WINDOW",
            )
            for row in operations
        ],
        planning_reference={
            "original_text": "2026년 7월 24일 오전 7시 15분",
            "local_at": "2026-07-24T07:15:00+09:00",
            "utc_at": REFERENCE,
            "timezone": "Asia/Seoul",
            "source": "USER_COMMAND",
        },
        summary="P16.1.1 check",
    )


def _snapshot(inventory: list[dict[str, Any]], inbound_orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "captured_at": REFERENCE,
        "sql": {
            "works": [],
            "inventory_items": [{"item_id": "A"}, {"item_id": "B"}],
            "inventory": inventory,
            "inbound_orders": inbound_orders or [],
            "outbound_orders": [],
            "storage_capacity": None,
        },
        "redis": {"inventory_reservations": []},
    }


def _required_time_normalization_check() -> dict[str, Any]:
    operation = InventoryOperationRequest(
        operation_id="OP-B",
        operation_type="OUTBOUND",
        item_id="B",
        quantity_boxes=20,
        required_at=WINDOW_START,
        required_by=WINDOW_END,
    )
    return {
        "passed": operation.required_at == WINDOW_END and operation.required_by == WINDOW_END,
        "required_at": operation.required_at.isoformat() if operation.required_at else None,
        "required_by": operation.required_by.isoformat() if operation.required_by else None,
    }


def _within_window_check() -> dict[str, Any]:
    operation = InventoryOperationRequest(
        operation_id="OP-B",
        operation_type="OUTBOUND",
        item_id="B",
        quantity_boxes=20,
        required_at=WINDOW_END,
    )
    update = inventory_precheck_node(
        {
            "interpretation": _interpretation([operation]).model_dump(mode="json"),
            "snapshot": _snapshot(
                [
                    {
                        "warehouse_item_id": "LOT-B1",
                        "item_id": "B",
                        "lot_id": "B-01",
                        "available_quantity": 20,
                        "node_id": 2088,
                        "available_at": LOT_AVAILABLE,
                        "status": "AVAILABLE",
                    }
                ]
            ),
        }
    )
    item = update["inventory_feasibility"]["item_results"][0]
    passed = bool(
        update["inventory_feasibility"]["status"] == "PASS"
        and item["planned_quantity_boxes"] == 20
        and item["required_at"] == LOT_AVAILABLE.isoformat().replace("+00:00", "Z")
        and len(item["lot_allocations"]) == 1
    )
    return {
        "passed": passed,
        "status": update["inventory_feasibility"]["status"],
        "evaluation_at": item["required_at"],
        "planned_quantity_boxes": item["planned_quantity_boxes"],
    }


def _mixed_partial_success_check() -> dict[str, Any]:
    operations = [
        InventoryOperationRequest(
            operation_id="OP-A",
            operation_type="OUTBOUND",
            item_id="A",
            quantity_boxes=30,
            required_at=WINDOW_END,
        ),
        InventoryOperationRequest(
            operation_id="OP-B",
            operation_type="OUTBOUND",
            item_id="B",
            quantity_boxes=20,
            required_at=WINDOW_END,
        ),
    ]
    update = inventory_precheck_node(
        {
            "interpretation": _interpretation(operations).model_dump(mode="json"),
            "snapshot": _snapshot(
                [
                    {
                        "warehouse_item_id": "LOT-A1",
                        "item_id": "A",
                        "available_quantity": 10,
                        "node_id": 2088,
                        "available_at": LOT_AVAILABLE,
                        "status": "AVAILABLE",
                    },
                    {
                        "warehouse_item_id": "LOT-B1",
                        "item_id": "B",
                        "available_quantity": 20,
                        "node_id": 2088,
                        "available_at": LOT_AVAILABLE,
                        "status": "AVAILABLE",
                    },
                ],
                [
                    {
                        "inbound_id": "IN-A2",
                        "item_id": "A",
                        "quantity_boxes": 20,
                        "storage_node_id": 2088,
                        "expected_available_at": WINDOW_END + timedelta(days=1),
                        "status": "EXPECTED",
                    }
                ],
            ),
        }
    )
    feasibility = update["inventory_feasibility"]
    by_item = {row["item_id"]: row for row in feasibility["item_results"]}
    passed = bool(
        feasibility["status"] == "PARTIAL_SUCCESS"
        and feasibility["valid"] is True
        and by_item["A"]["available_quantity_boxes"] == 10
        and by_item["A"]["shortage_quantity_boxes"] == 20
        and by_item["B"]["planned_quantity_boxes"] == 20
        and feasibility["shortage_work_ids"] == ["OP-A"]
        and feasibility["independent_work_ids"] == ["OP-B"]
    )
    return {
        "passed": passed,
        "status": feasibility["status"],
        "a_available": by_item["A"]["available_quantity_boxes"],
        "a_shortage": by_item["A"]["shortage_quantity_boxes"],
        "b_planned": by_item["B"]["planned_quantity_boxes"],
    }


def run_all_checks() -> dict[str, Any]:
    checks = {
        "required_time_normalization": _required_time_normalization_check(),
        "inventory_available_inside_window": _within_window_check(),
        "mixed_item_partial_success": _mixed_partial_success_check(),
    }
    return {
        "all_passed": all(row["passed"] for row in checks.values()),
        "checks": checks,
    }


def main() -> None:
    result = run_all_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

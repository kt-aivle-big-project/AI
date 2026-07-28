from datetime import UTC, datetime, timedelta

from app.models import CommandInterpretation, InventoryOperationRequest, TaskScheduleConstraint
from app.planning.nodes import inventory_precheck_node
from app.services.inventory_projection import InventoryProjectionService

REFERENCE = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)
LOT_AVAILABLE = datetime(2026, 7, 24, 0, 4, 31, tzinfo=UTC)


def _snapshot(*, inventory, inbound_orders=()):
    return {
        "captured_at": REFERENCE,
        "sql": {
            "works": [],
            "inventory_items": [{"item_id": "A"}, {"item_id": "B"}],
            "inventory": list(inventory),
            "inbound_orders": list(inbound_orders),
            "outbound_orders": [],
            "storage_capacity": None,
        },
        "redis": {"inventory_reservations": []},
    }


def _interpretation(operations):
    return CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective="시간창 출고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=operations,
        scheduled_task_constraints=[
            TaskScheduleConstraint(
                work_id=operation.operation_id,
                earliest_start=WINDOW_START,
                latest_finish=WINDOW_END,
                time_constraint_type="HARD_WINDOW",
            )
            for operation in operations
        ],
        planning_reference={
            "original_text": "2026년 7월 24일 오전 7시 15분",
            "local_at": "2026-07-24T07:15:00+09:00",
            "utc_at": REFERENCE,
            "timezone": "Asia/Seoul",
            "source": "USER_COMMAND",
        },
        summary="test",
    )


def test_required_time_aliases_normalize_to_later_deadline() -> None:
    operation = InventoryOperationRequest(
        operation_id="OP-B",
        operation_type="OUTBOUND",
        item_id="B",
        quantity_boxes=20,
        required_at=WINDOW_START,
        required_by=WINDOW_END,
    )
    assert operation.required_at == WINDOW_END
    assert operation.required_by == WINDOW_END


def test_projection_detects_existing_lot_becoming_available_after_reference() -> None:
    service = InventoryProjectionService(REFERENCE)
    result = service.evaluate(
        [
            InventoryOperationRequest(
                operation_id="OP-B",
                operation_type="OUTBOUND",
                item_id="B",
                quantity_boxes=20,
                required_at=WINDOW_START,
            )
        ],
        current_lots=[
            {
                "warehouse_item_id": "LOT-B1",
                "item_id": "B",
                "lot_id": "B-01",
                "available_quantity": 20,
                "node_id": 2088,
                "available_at": LOT_AVAILABLE,
                "status": "AVAILABLE",
            }
        ],
    )
    item = result.item_results[0]
    assert item.status == "EMERGENCY_REVIEW_REQUIRED"
    assert item.earliest_full_fulfillment_at == LOT_AVAILABLE


def test_inventory_precheck_restarts_at_first_available_time_inside_window() -> None:
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
                inventory=[
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
    assert update["inventory_feasibility"]["status"] == "PASS"
    assert update["final_status"] == "INVENTORY_READY"
    assert item["required_at"] == LOT_AVAILABLE.isoformat().replace("+00:00", "Z")
    assert item["planned_quantity_boxes"] == 20
    assert item["shortage_quantity_boxes"] == 0
    assert item["lot_allocations"][0]["warehouse_item_id"] == "LOT-B1"
    trace_row = update["trace"][0]
    assert trace_row["inventory_window_adjustments"]["OP-B"] == LOT_AVAILABLE.isoformat()


def test_inventory_after_window_remains_blocked() -> None:
    after_window = WINDOW_END + timedelta(minutes=1)
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
                inventory=[
                    {
                        "warehouse_item_id": "LOT-B1",
                        "item_id": "B",
                        "available_quantity": 20,
                        "node_id": 2088,
                        "available_at": after_window,
                        "status": "AVAILABLE",
                    }
                ]
            ),
        }
    )
    assert update["inventory_feasibility"]["status"] == "FAILED"
    assert update["final_status"] == "EMERGENCY_REVIEW_REQUIRED"


def test_mixed_ab_request_returns_partial_success_for_feasible_b() -> None:
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
                inventory=[
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
                inbound_orders=[
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
    assert feasibility["status"] == "PARTIAL_SUCCESS"
    assert feasibility["valid"] is True
    assert feasibility["partial_success"] is True
    assert by_item["A"]["shortage_quantity_boxes"] == 20
    assert by_item["B"]["planned_quantity_boxes"] == 20
    assert feasibility["shortage_work_ids"] == ["OP-A"]
    assert feasibility["independent_work_ids"] == ["OP-B"]

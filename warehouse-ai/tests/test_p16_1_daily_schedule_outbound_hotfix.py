from datetime import UTC, datetime, timedelta

from app.models import (
    AtomicTask,
    CommandInterpretation,
    InventoryOperationRequest,
    TaskScheduleConstraint,
)
from app.planning.nodes import (
    build_verification_evidence,
    inventory_precheck_node,
    select_required_tasks_node,
)
from app.services.command_language import parse_deterministic_command
from app.services.task_splitting import capacity_trip_groups


REFERENCE = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)


def test_operation_window_uses_planning_reference_date_and_binds_both_items() -> None:
    result = parse_deterministic_command(
        "2026년 7월 24일 오전 7시 15분을 기준으로 "
        "오전 9시부터 오전 11시 사이에 A상품 30 BOX와 B상품 20 BOX를 "
        "출고 노드 2146으로 이동하는 계획을 시뮬레이션해줘.",
        reference_time=datetime(2026, 7, 24, 6, 0, tzinfo=UTC),
        warehouse_timezone=None,
    )

    assert len(result.inventory_operations) == 2
    assert {row.item_id for row in result.inventory_operations} == {"A", "B"}
    assert all(row.required_at == WINDOW_END for row in result.inventory_operations)
    assert len(result.scheduled_task_constraints) == 2
    assert {
        (row.earliest_start, row.latest_finish, row.time_constraint_type)
        for row in result.scheduled_task_constraints
    } == {(WINDOW_START, WINDOW_END, "HARD_WINDOW")}
    assert {row.work_id for row in result.scheduled_task_constraints} == {
        row.operation_id for row in result.inventory_operations
    }


def test_capacity_trip_groups_merges_multiple_lots_at_same_node() -> None:
    pairs = capacity_trip_groups(
        [
            {
                "warehouse_item_id": "LOT-A1",
                "node_id": 2088,
                "quantity_boxes": 10,
                "available_at": WINDOW_START,
            },
            {
                "warehouse_item_id": "LOT-A2",
                "node_id": 2088,
                "quantity_boxes": 20,
                "available_at": WINDOW_START + timedelta(minutes=5),
            },
        ],
        50,
        prefix_base="OP-A",
    )

    assert len(pairs) == 1
    assert pairs[0]["quantity_boxes"] == 30
    assert pairs[0]["source_node"] == 2088
    assert len(pairs[0]["allocations"]) == 2
    assert pairs[0]["available_at"] == WINDOW_START + timedelta(minutes=5)


def test_select_required_tasks_keeps_operation_window_and_one_transport_pair() -> None:
    operation = InventoryOperationRequest(
        operation_id="OP-A",
        operation_type="OUTBOUND",
        item_id="A",
        quantity_boxes=30,
        required_at=WINDOW_END,
    )
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective="A 출고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[operation],
        target_node_ids=[2146],
        target_node_type="OUTBOUND",
        scheduled_task_constraints=[
            TaskScheduleConstraint(
                work_id="OP-A",
                earliest_start=WINDOW_START,
                latest_finish=WINDOW_END,
                time_constraint_type="HARD_WINDOW",
            )
        ],
        daily_schedule_requested=True,
        summary="test",
    )
    result = select_required_tasks_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "scope": {
                "plan_mode": "INITIAL_PLAN",
                "fixed_task_ids": [],
                "changeable_task_ids": [],
                "affected_robot_ids": [],
                "affected_task_ids": [],
                "freeze_horizon_seconds": 15,
                "include_new_command": True,
                "optimization_goal": "test",
                "reason_summary": "test",
            },
            "snapshot": {
                "sql": {
                    "works": [],
                    "work_dependencies": [],
                    "work_schedule_constraints": [],
                    "robots": [{"robot_id": "R2-01", "max_load": 50}],
                },
                "graph": {
                    "nodes": [
                        {"node_id": 2088, "node_type": "STORAGE"},
                        {"node_id": 2146, "node_type": "OUTBOUND"},
                    ]
                },
                "redis": {"active_plan": None, "robots": []},
            },
            "inventory_feasibility": {
                "status": "PASS",
                "valid": True,
                "item_results": [
                    {
                        "operation_id": "OP-A",
                        "operation_type": "OUTBOUND",
                        "item_id": "A",
                        "requested_quantity_boxes": 30,
                        "planned_quantity_boxes": 30,
                        "available_quantity_boxes": 30,
                        "shortage_quantity_boxes": 0,
                        "required_at": WINDOW_START,
                        "status": "PASS",
                        "lot_allocations": [
                            {
                                "warehouse_item_id": "LOT-A1",
                                "item_id": "A",
                                "quantity_boxes": 10,
                                "storage_node_id": 2088,
                                "available_at": WINDOW_START,
                                "source_type": "CURRENT_LOT",
                            },
                            {
                                "warehouse_item_id": "LOT-A2",
                                "item_id": "A",
                                "quantity_boxes": 20,
                                "storage_node_id": 2088,
                                "available_at": WINDOW_START,
                                "source_type": "CURRENT_LOT",
                            },
                        ],
                    }
                ],
                "shortage_work_ids": [],
                "blocked_work_ids": [],
                "independent_work_ids": ["OP-A"],
            },
            "inventory_blocked_work_ids": [],
            "command": {"command_id": "C-P16-1"},
        }
    )

    tasks = [AtomicTask.model_validate(row) for row in result["required_tasks"]]
    assert len(tasks) == 2
    assert {row.action for row in tasks} == {"PICK", "DROP"}
    assert all(row.quantity == 30 for row in tasks)
    assert all(len(row.inventory_allocations) == 2 for row in tasks)
    assert all(row.earliest_start == WINDOW_START for row in tasks)
    assert all(row.latest_finish == WINDOW_END for row in tasks)
    assert result["schedule_validation"]["constraint_count"] == 1


def test_inventory_precheck_accepts_inventory_available_inside_window() -> None:
    operation = InventoryOperationRequest(
        operation_id="OP-A",
        operation_type="OUTBOUND",
        item_id="A",
        quantity_boxes=30,
        required_at=WINDOW_END,
    )
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective="A 출고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[operation],
        scheduled_task_constraints=[
            TaskScheduleConstraint(
                work_id="OP-A",
                earliest_start=WINDOW_START,
                latest_finish=WINDOW_END,
                time_constraint_type="HARD_WINDOW",
            )
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
    update = inventory_precheck_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": {
                "captured_at": REFERENCE,
                "sql": {
                    "works": [],
                    "inventory_items": [{"item_id": "A"}],
                    "inventory": [
                        {
                            "warehouse_item_id": "LOT-A1",
                            "item_id": "A",
                            "available_quantity": 10,
                            "node_id": 2088,
                            "available_at": REFERENCE,
                            "status": "AVAILABLE",
                        }
                    ],
                    "inbound_orders": [
                        {
                            "inbound_id": "IN-A2",
                            "item_id": "A",
                            "quantity_boxes": 20,
                            "storage_node_id": 2088,
                            "expected_available_at": WINDOW_END - timedelta(minutes=5),
                            "status": "EXPECTED",
                        }
                    ],
                    "outbound_orders": [],
                    "storage_capacity": None,
                },
                "redis": {"inventory_reservations": []},
            },
        }
    )

    item = update["inventory_feasibility"]["item_results"][0]
    expected = WINDOW_END - timedelta(minutes=5)
    assert item["required_at"] == expected.isoformat().replace("+00:00", "Z")
    assert item["available_quantity_boxes"] == 30
    assert item["planned_quantity_boxes"] == 30
    assert item["shortage_quantity_boxes"] == 0
    assert update["inventory_feasibility"]["status"] == "PASS"


def test_verification_blocks_empty_execution_plan() -> None:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="INBOUND",
        objective="C 입고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[
            InventoryOperationRequest(
                operation_id="OP-C",
                operation_type="INBOUND",
                item_id="C",
                quantity_boxes=50,
            )
        ],
        summary="test",
    )
    evidence = build_verification_evidence(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "simulation": {"valid": True, "issues": [], "errors": [], "warnings": []},
            "cuopt_plan": {"scheduled_tasks": [], "unassigned_task_ids": []},
            "optimization_problem": {"robots": [], "min_robot_battery": 20},
            "schedule_validation": {},
            "errors": [],
            "warnings": [],
            "validation": {"errors": [], "warnings": []},
        }
    )

    by_code = {row["code"]: row for row in evidence}
    assert by_code["EMPTY_EXECUTION_PLAN"]["severity"] == "BLOCKING"
    assert "DETERMINISTIC_VALIDATION_PASSED" not in by_code

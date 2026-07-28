from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from app.models import InventoryOperationRequest, ItemInventoryResult, PlanningReference
from app.models import AtomicTask, RobotEvent, SimulationResult
from app.execution import graph as execution_nodes
from app.planning.nodes import _inventory_operations_from_snapshot, inventory_precheck_node
from app.services.command_language import parse_inventory_operations
from app.services.conversation import (
    apply_conversation_inheritance,
    constraints_from_interpretation,
)
from app.models import CommandInterpretation
from app.repositories.redis_store import RedisRepository
from app.services.inventory_projection import (
    InventoryProjectionService,
    allocate_lots_fefo,
    capacity_feasibility,
)
from app.services.inventory_reservations import (
    InventoryReservationConflict,
    InventoryReservationService,
    simulation_reservation_summaries,
)
from app.services.user_reporting import (
    build_user_report_summary,
    determine_report_detail_level,
    render_standard_report,
)
from app.services.simulation_session import _inventory_deltas, replay_simulation_session


REFERENCE = datetime(2026, 7, 22, 21, 0, tzinfo=UTC)  # 2026-07-23 06:00 KST


def scoped_inventory_sql() -> dict:
    return {
        "inventory": [
            {
                "warehouse_item_id": "WI-A",
                "item_id": "A",
                "node_id": 1,
                "quantity": 10,
                "available_quantity": 10,
                "status": "AVAILABLE",
            }
        ],
        "inventory_items": [{"item_id": "A"}, {"item_id": "F"}],
        "works": [
            {
                "work_id": "W-001",
                "operation_type": "OUTBOUND",
                "item_id": "A",
                "quantity_boxes": 5,
                "source_node": 1,
                "target_node": 2,
                "status": "NEW",
            },
            {
                "work_id": "W-002",
                "operation_type": "OUTBOUND",
                "item_id": "F",
                "quantity_boxes": 20,
                "source_node": 1,
                "target_node": 2,
                "status": "NEW",
            },
        ],
        "outbound_orders": [
            {
                "outbound_id": "OUT-A",
                "work_id": "W-001",
                "item_id": "A",
                "requested_quantity_boxes": 5,
                "priority": "NORMAL",
            },
            {
                "outbound_id": "OUT-F",
                "work_id": "W-002",
                "item_id": "F",
                "requested_quantity_boxes": 20,
                "priority": "NORMAL",
            },
        ],
        "inbound_orders": [],
    }


def test_specific_work_scope_filters_snapshot_inventory_operations() -> None:
    interpretation = CommandInterpretation(
        command_kind="EXECUTE",
        intent="EXECUTE",
        objective="W-001만 실행",
        execution_mode="EXECUTE",
        target_task_ids=["W-001"],
        load_open_inventory_orders=True,
        hard_constraints=["EXPLICIT_TASK_SCOPE_ONLY"],
        summary="single work",
    )

    operations = _inventory_operations_from_snapshot(
        interpretation,
        scoped_inventory_sql(),
    )

    assert {row.work_id for row in operations} == {"W-001"}
    assert {row.item_id for row in operations} == {"A"}


def test_multiple_explicit_work_ids_include_only_requested_operations() -> None:
    sql = scoped_inventory_sql()
    sql["works"].append(
        {
            "work_id": "W-003",
            "operation_type": "OUTBOUND",
            "item_id": "C",
            "quantity_boxes": 1,
            "status": "NEW",
        }
    )
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="W-001과 W-002만",
        execution_mode="PLAN_ONLY",
        target_task_ids=["W-001", "W-002"],
        load_open_inventory_orders=True,
        summary="two works",
    )

    operations = _inventory_operations_from_snapshot(interpretation, sql)

    assert {row.work_id for row in operations} == {"W-001", "W-002"}


def test_open_work_command_without_target_scope_keeps_all_operations() -> None:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="미완료 작업 전체",
        execution_mode="PLAN_ONLY",
        load_open_inventory_orders=True,
        summary="all works",
    )

    operations = _inventory_operations_from_snapshot(
        interpretation,
        scoped_inventory_sql(),
    )

    assert {row.work_id for row in operations} == {"W-001", "W-002"}


def test_inventory_precheck_excludes_unrequested_shortage_and_emergency_review() -> None:
    interpretation = CommandInterpretation(
        command_kind="EXECUTE",
        intent="EXECUTE",
        objective="W-001만 실행",
        execution_mode="EXECUTE",
        target_task_ids=["W-001"],
        load_open_inventory_orders=True,
        hard_constraints=["EXPLICIT_TASK_SCOPE_ONLY"],
        summary="single work",
    )
    update = inventory_precheck_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": {
                "captured_at": REFERENCE.isoformat(),
                "sql": scoped_inventory_sql(),
                "redis": {"inventory_reservations": []},
            },
        }
    )

    assert {row["work_id"] for row in update["inventory_operations"]} == {"W-001"}
    assert update["inventory_feasibility"]["shortage_work_ids"] == []
    assert update["emergency_review_items"] == []
    reservations = simulation_reservation_summaries(
        simulation_id="SIM-SCOPE",
        plan_version="PLAN-SCOPE",
        warehouse_id=1,
        item_results=[
            ItemInventoryResult.model_validate(row)
            for row in update["inventory_feasibility"]["item_results"]
        ],
    )
    assert {row.work_id for row in reservations} == {"W-001"}


def test_unknown_target_work_id_requires_clarification() -> None:
    interpretation = CommandInterpretation(
        command_kind="EXECUTE",
        intent="EXECUTE",
        objective="W-999만 실행",
        execution_mode="EXECUTE",
        target_task_ids=["W-999"],
        load_open_inventory_orders=True,
        summary="unknown work",
    )
    update = inventory_precheck_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": {
                "captured_at": REFERENCE.isoformat(),
                "sql": scoped_inventory_sql(),
                "redis": {"inventory_reservations": []},
            },
        }
    )

    assert update["final_status"] == "CLARIFICATION_REQUIRED"
    assert "unknown_target_work_id:W-999" in update["interpretation"]["missing_information"]
    assert update["inventory_operations"] == []


def parse_inventory(text: str):
    return parse_inventory_operations(
        text,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )


@pytest.mark.parametrize("unit", ["박스", "BOX", "BOXES", "box", "boxes"])
def test_box_units_are_normalized(unit: str) -> None:
    operations, missing, _, _ = parse_inventory(f"오전 7시까지 A 30{unit}를 출고해줘")
    assert not missing
    assert operations[0].unit == "BOX"
    assert operations[0].quantity_boxes == 30
    assert operations[0].operation_type == "OUTBOUND"


@pytest.mark.parametrize("unit", ["개", "EA", "낱개", "팔레트", "PALLET", "kg", "g", "L", "mL"])
def test_non_box_units_require_clarification(unit: str) -> None:
    operations, missing, ambiguous, _ = parse_inventory(f"E 100{unit}를 출고해줘")
    assert operations == []
    assert any(value.startswith("inventory_unit_confirmation:E:100:") for value in missing)
    assert ambiguous


@pytest.mark.parametrize("quantity", ["0", "-1", "1.5"])
def test_non_positive_or_fractional_box_quantity_is_rejected(quantity: str) -> None:
    operations, missing, _, _ = parse_inventory(f"A {quantity}박스를 출고해줘")
    assert operations == []
    assert any(value.startswith("invalid_inventory_quantity:A:") for value in missing)


def test_inbound_multiple_items_and_available_clock() -> None:
    operations, missing, _, _ = parse_inventory(
        "오전 7시에 A 50박스와 B 100박스가 입고되고, "
        "검수 완료 예정은 오전 7시 15분이야"
    )
    assert not missing
    assert [row.item_id for row in operations] == ["A", "B"]
    assert all(row.operation_type == "INBOUND" for row in operations)
    assert all(row.expected_available_at.minute == 15 for row in operations)
    assert all(row.expected_arrival_at.hour == 22 for row in operations)  # UTC


def test_open_order_phrase_does_not_create_temporary_operations() -> None:
    operations, _, _, load_open = parse_inventory(
        "오늘 주문과 입고 예정 데이터를 기준으로 계획해줘"
    )
    assert operations == []
    assert load_open is True


def test_planning_reference_does_not_mutate_work_required_times() -> None:
    work_deadline = datetime(2026, 7, 23, 22, 0, tzinfo=UTC)
    planning_reference = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
    operation = InventoryOperationRequest(
        operation_id="work:outbound",
        work_id="W-OUT",
        operation_type="OUTBOUND",
        item_id="ITEM-X",
        quantity_boxes=50,
        required_at=work_deadline,
        required_by=work_deadline,
        source="WORK",
    )
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="future reference plan",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[operation],
        planning_reference=PlanningReference(
            original_text="relative clock",
            local_at=datetime.fromisoformat("2026-07-24T07:15:00+09:00"),
            utc_at=planning_reference,
            timezone="Asia/Seoul",
            source="USER_COMMAND",
        ),
        summary="reference test",
    )
    update = inventory_precheck_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": {
                "captured_at": REFERENCE.isoformat(),
                "sql": {
                    "inventory_items": [{"item_id": "ITEM-X"}],
                    "inventory": [
                        {
                            "warehouse_item_id": "L1",
                            "item_id": "ITEM-X",
                            "available_quantity": 30,
                            "status": "AVAILABLE",
                        }
                    ],
                    "inbound_orders": [
                        {
                            "inbound_id": "IN-1",
                            "item_id": "ITEM-X",
                            "quantity_boxes": 30,
                            "expected_available_at": datetime(2026, 7, 23, 22, 10, tzinfo=UTC),
                            "status": "INSPECTING",
                        }
                    ],
                    "outbound_orders": [],
                    "storage_capacity": None,
                },
                "redis": {"inventory_reservations": []},
            },
        }
    )

    returned = update["interpretation"]["inventory_operations"][0]
    assert returned["required_at"] == work_deadline.isoformat().replace("+00:00", "Z")
    assert returned["required_by"] == work_deadline.isoformat().replace("+00:00", "Z")
    assert update["inventory_feasibility"]["status"] == "PASS"


def test_command_interpretation_accepts_multiple_work_deadlines_with_reference() -> None:
    deadline_a = datetime(2026, 7, 23, 21, 30, tzinfo=UTC)
    deadline_b = datetime(2026, 7, 23, 22, 0, tzinfo=UTC)
    result = CommandInterpretation.model_validate(
        {
            "command_kind": "PLAN",
            "intent": "DAILY_PLAN",
            "objective": "reference plan",
            "execution_mode": "SIMULATE_ONLY",
            "planning_reference": {
                "original_text": "relative clock",
                "local_at": "2026-07-24T07:15:00+09:00",
                "utc_at": "2026-07-23T22:15:00Z",
                "timezone": "Asia/Seoul",
                "source": "USER_COMMAND",
            },
            "inventory_operations": [
                {
                    "operation_id": "work:A",
                    "operation_type": "OUTBOUND",
                    "item_id": "A",
                    "quantity_boxes": 10,
                    "required_at": deadline_a.isoformat(),
                    "required_by": deadline_a.isoformat(),
                    "source": "WORK",
                },
                {
                    "operation_id": "work:B",
                    "operation_type": "OUTBOUND",
                    "item_id": "B",
                    "quantity_boxes": 20,
                    "required_at": deadline_b.isoformat(),
                    "required_by": deadline_b.isoformat(),
                    "source": "WORK",
                },
            ],
            "summary": "reference test",
        }
    )

    assert result.planning_reference.source == "USER_COMMAND"
    assert [row.required_at for row in result.inventory_operations] == [
        deadline_a,
        deadline_b,
    ]
    assert [row.required_by for row in result.inventory_operations] == [
        deadline_a,
        deadline_b,
    ]


def test_open_order_and_linked_work_are_not_counted_twice() -> None:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective="demo outbound",
        execution_mode="SIMULATE_ONLY",
        summary="demo outbound",
        load_open_inventory_orders=True,
        inventory_operations=[
            InventoryOperationRequest(
                operation_id="command:A",
                operation_type="OUTBOUND",
                item_id="A",
                quantity_boxes=30,
                required_at=REFERENCE,
            )
        ],
    )
    operations = _inventory_operations_from_snapshot(
        interpretation,
        {
            "outbound_orders": [
                {
                    "outbound_id": "OUT-A",
                    "work_id": "W-A",
                    "item_id": "A",
                    "requested_quantity_boxes": 30,
                    "required_by": REFERENCE,
                    "priority": "NORMAL",
                }
            ],
            "inbound_orders": [],
            "works": [
                {
                    "work_id": "W-A",
                    "operation_type": "OUTBOUND",
                    "item_id": "A",
                    "quantity_boxes": 30,
                    "required_at": REFERENCE,
                    "priority": 10,
                    "inventory_order_id": "OUT-A",
                }
            ],
        },
    )
    assert len(operations) == 1
    assert operations[0].order_id == "OUT-A"
    assert operations[0].work_id == "W-A"
    assert operations[0].source == "SQL_ORDER"


def test_open_inbound_order_reuses_matching_command_operation() -> None:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="INBOUND",
        objective="demo inbound",
        execution_mode="SIMULATE_ONLY",
        summary="demo inbound",
        load_open_inventory_orders=True,
        inventory_operations=[
            InventoryOperationRequest(
                operation_id="command:F",
                operation_type="INBOUND",
                item_id="F",
                quantity_boxes=20,
            )
        ],
    )
    operations = _inventory_operations_from_snapshot(
        interpretation,
        {
            "outbound_orders": [],
            "inbound_orders": [
                {
                    "inbound_id": "IN-F",
                    "item_id": "F",
                    "quantity_boxes": 20,
                    "expected_arrival_at": REFERENCE,
                    "expected_available_at": REFERENCE + timedelta(minutes=10),
                    "status": "INSPECTING",
                }
            ],
            "works": [],
        },
    )
    assert len(operations) == 1
    assert operations[0].order_id == "IN-F"
    assert operations[0].expected_available_at == REFERENCE + timedelta(minutes=10)


def test_inventory_operations_are_inherited_in_same_conversation_context() -> None:
    previous = CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective="A 10박스 출고 시뮬레이션",
        summary="재고 출고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[
            InventoryOperationRequest(
                operation_type="OUTBOUND", item_id="A", quantity_boxes=10
            )
        ],
    )
    followup = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="같은 조건에서 이동거리 우선으로 시뮬레이션해줘",
        summary="후속 명령",
        execution_mode="SIMULATE_ONLY",
    )
    resolved, inherited, _ = apply_conversation_inheritance(
        followup,
        constraints_from_interpretation(previous),
        active_plan_version=None,
        active_simulation_id="SIM-OLD",
    )
    assert resolved.inventory_operations[0].item_id == "A"
    assert "inventory_operations" in inherited


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("가능한 수량만 먼저 출고해줘", True),
        ("재고가 있는 만큼만 우선 처리해줘", True),
        ("부분 출고를 승인해", True),
        ("일반 출고해줘", False),
    ],
)
def test_partial_fulfillment_requires_explicit_phrase(phrase: str, expected: bool) -> None:
    operations, _, _, _ = parse_inventory(f"A 50박스 출고 요청이고 {phrase}")
    assert operations[0].allow_partial_fulfillment is expected


def lot(item: str, quantity: int, *, status: str = "AVAILABLE", lot_id: str = "L1", **extra):
    return {
        "warehouse_item_id": f"WI-{item}-{lot_id}",
        "warehouse_id": 1,
        "item_id": item,
        "lot_id": lot_id,
        "node_id": extra.pop("node_id", 2),
        "quantity": quantity,
        "available_quantity": quantity,
        "status": status,
        **extra,
    }


def outbound(item: str, quantity: int, *, at: datetime, partial: bool = False, work_id: str | None = None):
    return InventoryOperationRequest(
        operation_id=f"OUT-{item}-{work_id or '1'}",
        work_id=work_id,
        operation_type="OUTBOUND",
        item_id=item,
        quantity_boxes=quantity,
        required_at=at,
        allow_partial_fulfillment=partial,
    )


@pytest.mark.parametrize("status", ["ARRIVED", "UNLOADING", "INSPECTING"])
def test_non_available_lot_is_not_usable(status: str) -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("A", 1, at=REFERENCE)],
        current_lots=[lot("A", 10, status=status)],
    )
    assert result.item_results[0].available_quantity_boxes == 0
    assert result.item_results[0].status == "EMERGENCY_REVIEW_REQUIRED"


def test_available_lot_is_opening_balance() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("A", 7, at=REFERENCE)], current_lots=[lot("A", 10)]
    )
    assert result.item_results[0].status == "PASS"
    assert result.item_results[0].available_quantity_boxes == 10


def test_expected_available_not_arrival_controls_future_inbound() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("A", 10, at=REFERENCE + timedelta(minutes=5))],
        current_lots=[],
        future_inbounds=[
            {
                "inbound_id": "IN-1",
                "item_id": "A",
                "quantity_boxes": 10,
                "expected_arrival_at": REFERENCE,
                "expected_available_at": REFERENCE + timedelta(minutes=10),
                "status": "INSPECTING",
            }
        ],
    )
    item = result.item_results[0]
    assert item.available_quantity_boxes == 0
    assert item.earliest_full_fulfillment_at == REFERENCE + timedelta(minutes=10)


def test_actual_available_overrides_expected_available() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("A", 10, at=REFERENCE + timedelta(minutes=8))],
        current_lots=[],
        future_inbounds=[
            {
                "inbound_id": "IN-1",
                "item_id": "A",
                "quantity_boxes": 10,
                "expected_available_at": REFERENCE + timedelta(minutes=10),
                "actual_available_at": REFERENCE + timedelta(minutes=7),
                "storage_node_id": 2,
                "status": "AVAILABLE",
            }
        ],
    )
    assert result.item_results[0].available_quantity_boxes == 10


def test_same_timestamp_inbound_precedes_outbound() -> None:
    at = REFERENCE + timedelta(minutes=10)
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("A", 10, at=at)],
        current_lots=[],
        future_inbounds=[
            {
                "inbound_id": "IN-1",
                "item_id": "A",
                "quantity_boxes": 10,
                "expected_available_at": at,
                "storage_node_id": 2,
                "status": "INSPECTING",
            }
        ],
    )
    assert result.item_results[0].status == "PASS"


def test_reflected_inbound_is_not_double_counted() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("A", 11, at=REFERENCE + timedelta(minutes=10))],
        current_lots=[lot("A", 10)],
        future_inbounds=[
            {
                "inbound_id": "IN-1",
                "warehouse_item_id": "WI-A-L1",
                "item_id": "A",
                "quantity_boxes": 10,
                "actual_available_at": REFERENCE,
                "status": "AVAILABLE",
            }
        ],
    )
    assert result.item_results[0].available_quantity_boxes == 10


def test_active_plan_reservation_reduces_projection() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("F", 25, at=REFERENCE)],
        current_lots=[lot("F", 30)],
        active_reservations=[
            {"reservation_id": "R1", "item_id": "F", "quantity_boxes": 10, "status": "RESERVED"}
        ],
    )
    assert result.item_results[0].available_quantity_boxes == 20
    assert result.item_results[0].shortage_quantity_boxes == 5


def test_simulation_events_are_isolated_input_and_time_ordered() -> None:
    service = InventoryProjectionService(REFERENCE)
    first = service.evaluate(
        [outbound("A", 7, at=REFERENCE + timedelta(seconds=5))],
        current_lots=[lot("A", 10)],
        simulation_events=[
            {"item_id": "A", "quantity_boxes": 5, "at": REFERENCE, "event_id": "SIM-1"}
        ],
    )
    second = service.evaluate(
        [outbound("A", 7, at=REFERENCE + timedelta(seconds=5))],
        current_lots=[lot("A", 10)],
    )
    assert first.item_results[0].available_quantity_boxes == 5
    assert second.item_results[0].available_quantity_boxes == 10


def test_shortage_and_earliest_full_fulfillment() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("F", 50, at=REFERENCE)],
        current_lots=[lot("F", 30)],
        future_inbounds=[
            {
                "inbound_id": "IN-F",
                "item_id": "F",
                "quantity_boxes": 20,
                "expected_available_at": REFERENCE + timedelta(minutes=10),
                "storage_node_id": 2,
                "status": "INSPECTING",
            }
        ],
    )
    item = result.item_results[0]
    assert item.shortage_quantity_boxes == 20
    assert item.planned_quantity_boxes == 0
    assert item.earliest_full_fulfillment_at == REFERENCE + timedelta(minutes=10)


def test_explicit_partial_approval_plans_only_available_quantity() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [outbound("F", 50, at=REFERENCE, partial=True)],
        current_lots=[lot("F", 30)],
    )
    item = result.item_results[0]
    assert item.status == "PARTIAL_FULFILLMENT_APPROVED"
    assert item.planned_quantity_boxes == 30


def test_shortage_blocks_only_dependents_and_keeps_independent_work() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [
            outbound("F", 50, at=REFERENCE, work_id="W-F"),
            outbound("A", 5, at=REFERENCE, work_id="W-A"),
        ],
        current_lots=[lot("F", 30), lot("A", 10)],
        dependencies=[
            {"predecessor_work_id": "W-F", "successor_work_id": "W-D", "dependency_type": "FINISH_TO_START"}
        ],
    )
    assert result.status == "PARTIAL_SUCCESS"
    assert result.shortage_work_ids == ["W-F"]
    assert result.blocked_work_ids == ["W-D"]
    assert result.independent_work_ids == ["W-A"]


def test_fefo_fifo_and_multi_lot_allocation() -> None:
    allocations = allocate_lots_fefo(
        [
            lot("A", 4, lot_id="L2", expiration_at="2026-08-01T00:00:00+00:00", available_at="2026-07-02T00:00:00+00:00"),
            lot("A", 3, lot_id="L1", expiration_at="2026-08-01T00:00:00+00:00", available_at="2026-07-01T00:00:00+00:00"),
            lot("A", 5, lot_id="L3", expiration_at="2026-09-01T00:00:00+00:00"),
        ],
        item_id="A",
        quantity_boxes=6,
    )
    assert [(row.lot_id, row.quantity_boxes) for row in allocations] == [("L1", 3), ("L2", 3)]


def test_capacity_not_configured_only_for_inbound() -> None:
    inbound_result = capacity_feasibility(["INBOUND"], None)
    outbound_result = capacity_feasibility(["OUTBOUND"], None)
    assert inbound_result.status == "NOT_CONFIGURED"
    assert inbound_result.warnings == ["CAPACITY_DATA_NOT_CONFIGURED"]
    assert outbound_result.status == "NOT_APPLICABLE"


def feasibility_item(*, work_id: str = "W-1", quantity: int = 5) -> ItemInventoryResult:
    return ItemInventoryResult(
        operation_id=f"OP-{work_id}",
        work_id=work_id,
        operation_type="OUTBOUND",
        item_id="A",
        requested_quantity_boxes=quantity,
        planned_quantity_boxes=quantity,
        available_quantity_boxes=10,
        shortage_quantity_boxes=0,
        required_at=REFERENCE,
        status="PASS",
        lot_allocations=allocate_lots_fefo([lot("A", 10)], item_id="A", quantity_boxes=quantity),
    )


class ReservationRedis:
    def __init__(self):
        self.rows: list[dict] = []
        self.owner: str | None = None
        self.guard = Lock()

    def acquire_inventory_lock(self, warehouse_id, item_id, token, ttl_seconds=15):
        with self.guard:
            if self.owner is not None:
                return False
            self.owner = token
            return True

    def release_inventory_lock(self, warehouse_id, item_id, token):
        with self.guard:
            if self.owner != token:
                return False
            self.owner = None
            return True

    def list_inventory_reservations(self, warehouse_id, **filters):
        rows = list(self.rows)
        if filters.get("scope"):
            rows = [row for row in rows if row["scope"] == filters["scope"]]
        if filters.get("statuses"):
            rows = [row for row in rows if row["status"] in filters["statuses"]]
        return rows

    def save_inventory_reservations(self, warehouse_id, rows):
        for row in rows:
            existing = next((value for value in self.rows if value["idempotency_key"] == row["idempotency_key"]), None)
            if existing is None:
                self.rows.append(dict(row))
        return [next(value for value in self.rows if value["idempotency_key"] == row["idempotency_key"]) for row in rows]

    def update_inventory_reservations(self, warehouse_id, **filters):
        updated = []
        for row in self.rows:
            if filters.get("plan_version") and row["plan_version"] != filters["plan_version"]:
                continue
            if filters.get("work_id") and row["work_id"] != filters["work_id"]:
                continue
            if filters.get("from_statuses") and row["status"] not in filters["from_statuses"]:
                continue
            row["status"] = filters["status"]
            updated.append(dict(row))
        return updated


class InventoryPostgres:
    def __init__(self, quantity: int = 10):
        self.quantity = quantity

    def fetch_inventory(self, warehouse_id, item_ids):
        return [lot("A", self.quantity)]


class BlockingInventoryPostgres(InventoryPostgres):
    def __init__(self):
        super().__init__(10)
        self.entered = Event()
        self.release = Event()

    def fetch_inventory(self, warehouse_id, item_ids):
        self.entered.set()
        assert self.release.wait(timeout=3)
        return super().fetch_inventory(warehouse_id, item_ids)


def test_simulation_reservation_has_scope_but_no_global_write() -> None:
    rows = simulation_reservation_summaries(
        warehouse_id=1,
        simulation_id="SIM-A",
        plan_version="P1",
        item_results=[feasibility_item()],
    )
    assert rows[0].scope == "SIMULATION"
    assert rows[0].simulation_id == "SIM-A"
    assert rows[0].status == "RESERVED"


def test_active_reservation_is_idempotent_and_releasable() -> None:
    redis = ReservationRedis()
    service = InventoryReservationService(InventoryPostgres(), redis)
    first = service.reserve_active_plan(warehouse_id=1, plan_version="P1", item_results=[feasibility_item()])
    second = service.reserve_active_plan(warehouse_id=1, plan_version="P1", item_results=[feasibility_item()])
    assert first[0].reservation_id == second[0].reservation_id
    assert len(redis.rows) == 1
    assert service.release_plan(1, "P1", status="CANCELLED")[0]["status"] == "CANCELLED"


def test_active_reservation_rejects_global_overbooking() -> None:
    redis = ReservationRedis()
    redis.rows.append(
        simulation_reservation_summaries(
            warehouse_id=1,
            simulation_id="IGNORED",
            plan_version="OLD",
            item_results=[feasibility_item(quantity=8)],
        )[0].model_copy(update={"scope": "ACTIVE_PLAN"}).model_dump(mode="json")
    )
    with pytest.raises(InventoryReservationConflict):
        InventoryReservationService(InventoryPostgres(10), redis).reserve_active_plan(
            warehouse_id=1,
            plan_version="P2",
            item_results=[feasibility_item(work_id="W-2", quantity=5)],
        )


def test_inventory_lock_requires_owner_token() -> None:
    redis = ReservationRedis()
    assert redis.acquire_inventory_lock(1, "A", "owner")
    assert redis.release_inventory_lock(1, "A", "other") is False
    assert redis.release_inventory_lock(1, "A", "owner") is True


def test_concurrent_active_reservation_allows_only_lock_owner() -> None:
    redis = ReservationRedis()
    postgres = BlockingInventoryPostgres()
    service = InventoryReservationService(postgres, redis)
    completed: list[object] = []

    def first_request() -> None:
        try:
            completed.extend(
                service.reserve_active_plan(
                    warehouse_id=1,
                    plan_version="P1",
                    item_results=[feasibility_item(quantity=8)],
                )
            )
        except Exception as exc:  # pragma: no cover - assertion below exposes it
            completed.append(exc)

    thread = Thread(target=first_request)
    thread.start()
    assert postgres.entered.wait(timeout=3)
    with pytest.raises(InventoryReservationConflict):
        service.reserve_active_plan(
            warehouse_id=1,
            plan_version="P2",
            item_results=[feasibility_item(work_id="W-2", quantity=8)],
        )
    postgres.release.set()
    thread.join(timeout=3)
    assert len(redis.rows) == 1
    assert not any(isinstance(row, Exception) for row in completed)


def test_partial_success_user_report_contains_emergency_evidence() -> None:
    inventory = {
        "status": "PARTIAL_SUCCESS",
        "valid": True,
        "partial_success": True,
        "item_results": [],
        "shortage_work_ids": ["W-F"],
        "blocked_work_ids": [],
        "independent_work_ids": ["W-A"],
        "warnings": [],
    }
    emergency = {
        "item_id": "F",
        "work_id": "W-F",
        "requested_quantity_boxes": 50,
        "available_quantity_boxes": 30,
        "shortage_quantity_boxes": 20,
        "required_at": REFERENCE,
        "earliest_full_fulfillment_at": REFERENCE + timedelta(minutes=10),
        "recommended_actions": ["전체 수량 사용 가능 시각 이후로 출고", "부분 출고 명시 승인"],
    }
    state = {
        "command": {"text": "오늘 재고 작업을 시뮬레이션해줘"},
        "interpretation": {"execution_mode": "SIMULATE_ONLY"},
        "inventory_operations": [{"operation_type": "OUTBOUND"}],
        "inventory_feasibility": inventory,
        "emergency_review_items": [emergency],
        "scope": {"plan_mode": "INITIAL_PLAN"},
    }
    data = {
        "execution_mode": "SIMULATE_ONLY",
        "plan_mode": "INITIAL_PLAN",
        "inventory_feasibility": inventory,
        "emergency_review_items": [emergency],
    }
    level = determine_report_detail_level(state, data)
    summary = build_user_report_summary(state, data, report_level=level)
    report = render_standard_report(summary)
    assert summary.outcome == "PARTIAL_SUCCESS_WITH_EMERGENCY"
    assert level.value == "STANDARD"
    assert "20 BOX 부족" in report
    assert "전체 출고 가능 예상" in report


def test_move_work_uses_common_inventory_delta_and_drop_does_not() -> None:
    allocation = {"warehouse_item_id": "WI-A-L1", "quantity": 3}
    move = AtomicTask(
        task_id="W-1:move",
        work_id="W-1",
        action="MOVE",
        inventory_allocations=[allocation],
    )
    drop = move.model_copy(update={"action": "DROP"})
    assert _inventory_deltas(move) == [
        {"warehouse_item_id": "WI-A-L1", "quantity_delta": -3}
    ]
    assert _inventory_deltas(drop) == []


class CompletionPostgres:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.completion_count = 0

    def commit_completion(self, event):
        if self.fail:
            raise RuntimeError("sql failure")
        self.completion_count += 1
        return {"committed": True, "idempotent_replay": False}

    def commit_failure(self, event):
        return {"committed": True, "idempotent_replay": False}


class CompletionRedis:
    def __init__(self):
        self.statuses: list[str] = []
        self.live_update_count = 0

    def update_inventory_reservations(self, warehouse_id, **values):
        self.statuses.append(values["status"])
        return [{"status": values["status"]}]

    def update_from_event(self, _event):
        self.live_update_count += 1


class ReservationHashPipeline:
    def __init__(self, client):
        self.client = client
        self.writes: list[tuple[str, str, str]] = []

    def hset(self, key, field, value):
        self.writes.append((key, field, value))
        return self

    def execute(self):
        for key, field, value in self.writes:
            self.client.hset(key, field, value)
        return [1 for _ in self.writes]


class ReservationHashClient:
    def __init__(self, rows):
        self.hashes = {
            "wh:1:inventory:reservations": {
                row["reservation_id"]: json.dumps(row) for row in rows
            }
        }

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hvals(self, key):
        return list(self.hashes.get(key, {}).values())

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def pipeline(self, transaction=True):
        return ReservationHashPipeline(self)


def reservation_repository(rows):
    repository = RedisRepository.__new__(RedisRepository)
    repository.client = ReservationHashClient(rows)
    return repository


def active_reservation_row(quantity: int = 5) -> dict:
    return {
        "reservation_id": "RSV-1",
        "warehouse_id": 1,
        "item_id": "A",
        "quantity_boxes": quantity,
        "work_id": "W-1",
        "plan_version": "PLAN-1",
        "scope": "ACTIVE_PLAN",
        "status": "RESERVED",
        "idempotency_key": "PLAN-1:W-1:A",
        "lot_allocations": [
            {
                "warehouse_item_id": "WI-A-L1",
                "lot_id": "L1",
                "quantity_boxes": quantity,
            }
        ],
    }


def test_partial_real_completion_keeps_unconsumed_reservation() -> None:
    repository = reservation_repository([active_reservation_row(5)])
    rows = repository.consume_inventory_reservations(
        1,
        work_id="W-1",
        inventory_deltas=[
            {"warehouse_item_id": "WI-A-L1", "quantity_delta": -2}
        ],
    )
    assert rows[0]["status"] == "RESERVED"
    assert rows[0]["consumed_quantity_boxes"] == 2
    assert rows[0]["remaining_quantity_boxes"] == 3
    assert rows[0]["lot_allocations"][0]["quantity_boxes"] == 3


def test_reservation_consumption_is_not_written_when_delta_exceeds_lot() -> None:
    repository = reservation_repository([active_reservation_row(2)])
    before = repository.client.hgetall("wh:1:inventory:reservations")
    with pytest.raises(RuntimeError, match="exceed ACTIVE_PLAN"):
        repository.consume_inventory_reservations(
            1,
            work_id="W-1",
            inventory_deltas=[
                {"warehouse_item_id": "WI-A-L1", "quantity_delta": -3}
            ],
        )
    assert repository.client.hgetall("wh:1:inventory:reservations") == before


def real_event(event_type: str) -> RobotEvent:
    return RobotEvent(
        warehouse_id=1,
        robot_id="R-1",
        work_id="W-1",
        task_id="W-1:move",
        event_type=event_type,
        execution_context="REAL",
    )


def test_real_completion_consumes_reservation_only_after_sql_success(monkeypatch) -> None:
    redis = CompletionRedis()
    monkeypatch.setattr(
        execution_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=CompletionPostgres(), redis=redis),
    )
    result = execution_nodes.commit_completion_node(
        {"event": real_event("TASK_COMPLETED").model_dump(mode="json")}
    )
    assert result["sql_committed"] is True
    assert redis.statuses == ["CONSUMED"]


def test_sql_failure_keeps_active_inventory_reservation(monkeypatch) -> None:
    redis = CompletionRedis()
    monkeypatch.setattr(
        execution_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=CompletionPostgres(fail=True), redis=redis),
    )
    result = execution_nodes.commit_completion_node(
        {"event": real_event("TASK_COMPLETED").model_dump(mode="json")}
    )
    assert result["sql_committed"] is False
    assert redis.statuses == []
    assert redis.live_update_count == 0


def test_real_completion_requires_exact_active_plan_inventory_reservation(monkeypatch) -> None:
    redis = reservation_repository([active_reservation_row(30)])
    postgres = CompletionPostgres()
    monkeypatch.setattr(
        execution_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=postgres, redis=redis),
    )
    event = RobotEvent.model_validate(
        {
            **real_event("TASK_COMPLETED").model_dump(mode="json"),
            "inventory_deltas": [
                {"warehouse_item_id": "WI-A-L1", "quantity_delta": -1}
            ],
        }
    )
    result = execution_nodes.commit_completion_node({"event": event.model_dump(mode="json")})
    assert result["sql_committed"] is False
    assert result["final_status"] == "VALIDATION_FAILED"
    assert result["errors"] == ["OUTBOUND_COMPLETION_RESERVATION_MISMATCH"]
    assert postgres.completion_count == 0
    assert redis.list_inventory_reservations(1, scope="ACTIVE_PLAN", statuses={"RESERVED"})[0]["status"] == "RESERVED"


def test_real_completion_rejects_empty_inventory_deltas_when_active_reservation_exists(monkeypatch) -> None:
    redis = reservation_repository([active_reservation_row(30)])
    postgres = CompletionPostgres()
    monkeypatch.setattr(
        execution_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=postgres, redis=redis),
    )
    result = execution_nodes.commit_completion_node(
        {"event": real_event("TASK_COMPLETED").model_dump(mode="json")}
    )
    assert result["sql_committed"] is False
    assert result["errors"] == ["OUTBOUND_COMPLETION_INVENTORY_DELTAS_REQUIRED"]
    assert postgres.completion_count == 0


def test_real_completion_rejects_active_plan_version_mismatch(monkeypatch) -> None:
    redis = reservation_repository([active_reservation_row(30)])
    postgres = CompletionPostgres()
    monkeypatch.setattr(
        execution_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=postgres, redis=redis),
    )
    event = RobotEvent.model_validate(
        {
            **real_event("TASK_COMPLETED").model_dump(mode="json"),
            "payload": {"plan_version": "PLAN-OTHER"},
            "inventory_deltas": [
                {"warehouse_item_id": "WI-A-L1", "quantity_delta": -30}
            ],
        }
    )
    result = execution_nodes.commit_completion_node({"event": event.model_dump(mode="json")})
    assert result["sql_committed"] is False
    assert result["errors"] == ["PLAN_VERSION_MISMATCH"]
    assert postgres.completion_count == 0


def test_real_task_failure_releases_inventory_reservation(monkeypatch) -> None:
    redis = CompletionRedis()
    monkeypatch.setattr(
        execution_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=CompletionPostgres(), redis=redis),
    )
    result = execution_nodes.commit_completion_node(
        {"event": real_event("TASK_FAILED").model_dump(mode="json")}
    )
    assert result["sql_committed"] is True
    assert redis.statuses == ["RELEASED"]


class SimulationInventoryRedis:
    def __init__(self):
        self.events: list[RobotEvent] = []

    def initialize_simulation_session(self, simulation_id, snapshot):
        return {"inventory": [], "robots": [], "works": [], "checkpoint": "0-0"}

    def update_simulation_from_event(self, event):
        self.events.append(event)
        return {"inventory": [], "robots": [], "works": [], "checkpoint": f"{len(self.events)}-0"}

    def simulation_snapshot(self, simulation_id):
        return {"inventory": [], "robots": [], "works": [], "checkpoint": "0-0"}


class ReplayInventoryRedis:
    """Small in-memory simulation store; it never touches operating Redis state."""

    def __init__(self) -> None:
        self.events: list[RobotEvent] = []
        self.inventory: list[dict] = []

    def initialize_simulation_session(self, simulation_id, snapshot):
        self.inventory = [dict(row) for row in snapshot["sql"]["inventory"]]
        return {"inventory": list(self.inventory), "robots": [], "works": [], "checkpoint": "0-0"}

    def update_simulation_from_event(self, event):
        self.events.append(event)
        by_id = {str(row["warehouse_item_id"]): dict(row) for row in self.inventory}
        if event.event_type == "INBOUND_AVAILABLE":
            by_id[str(event.payload["warehouse_item_id"])] = {
                "warehouse_item_id": event.payload["warehouse_item_id"],
                "item_id": event.payload["item_id"],
                "lot_id": event.payload.get("lot_id"),
                "node_id": event.node_id,
                "quantity": event.payload["quantity_boxes"],
                "available_at": event.occurred_at.isoformat(),
            }
        elif event.event_type == "TASK_COMPLETED":
            for delta in event.inventory_deltas:
                row = by_id.get(str(delta.warehouse_item_id))
                if row is None:
                    raise ValueError(f"재고 항목을 찾을 수 없습니다: {delta.warehouse_item_id}")
                row["quantity"] += int(delta.quantity_delta)
        self.inventory = list(by_id.values())
        return {"inventory": self.inventory, "robots": [], "works": [], "checkpoint": f"{len(self.events)}-0"}

    def simulation_snapshot(self, simulation_id):
        return {"inventory": self.inventory, "robots": [], "works": [], "checkpoint": f"{len(self.events)}-0"}


def _future_lot_replay_state(*, available_step: int, completion_step: int, allocations: list[dict]) -> dict:
    future_available_at = REFERENCE + timedelta(seconds=available_step * 5)
    return {
        "simulation_id": "SIM-FUTURE",
        "command": {"warehouse_id": 1},
        "snapshot": {
            "captured_at": REFERENCE.isoformat(),
            "sql": {
                "inventory": [
                    {
                        "warehouse_item_id": "CURRENT-A",
                        "item_id": "A",
                        "quantity": 30,
                    }
                ]
            },
        },
        "optimization_problem": {"reference_time": REFERENCE.isoformat()},
        "collision_plan": {"time_step_seconds": 5, "routes": []},
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "robot_id": "R-1",
                    "task_id": "W-1:move",
                    "work_id": "W-1",
                    "source_node": 1,
                    "target_node": 2,
                    "start_time_step": 0,
                    "end_time_step": completion_step,
                }
            ]
        },
        "required_tasks": [
            AtomicTask(
                task_id="W-1:move",
                work_id="W-1",
                action="MOVE",
                inventory_allocations=allocations,
            ).model_dump(mode="json")
        ],
        "inventory_timeline_validation": {
            "item_results": [
                {
                    "lot_allocations": [
                        {
                            "warehouse_item_id": "virtual-lot-42",
                            "item_id": "A",
                            "lot_id": "LOT-FUTURE",
                            "quantity_boxes": 20,
                            "storage_node_id": 99,
                            "available_at": future_available_at.isoformat(),
                            "source_type": "FUTURE_INBOUND",
                            "inbound_source_id": "inbound-order-42",
                        }
                    ]
                }
            ]
        },
        "inventory_operations": [],
    }


def test_simulation_replay_creates_future_lot_at_available_time_for_mixed_allocation() -> None:
    state = _future_lot_replay_state(
        available_step=2,
        completion_step=3,
        allocations=[
            {"warehouse_item_id": "CURRENT-A", "quantity": 30},
            {"warehouse_item_id": "virtual-lot-42", "quantity": 20},
        ],
    )
    original_inventory = [dict(row) for row in state["snapshot"]["sql"]["inventory"]]
    redis = ReplayInventoryRedis()

    replay_simulation_session(
        state,
        SimulationResult(success=True, valid=True, status="SUCCESS"),
        redis,
    )

    assert [event.event_type for event in redis.events] == [
        "TASK_STARTED",
        "INBOUND_AVAILABLE",
        "TASK_COMPLETED",
    ]
    quantities = {row["warehouse_item_id"]: row["quantity"] for row in redis.inventory}
    assert quantities == {"CURRENT-A": 0, "virtual-lot-42": 0}
    assert state["snapshot"]["sql"]["inventory"] == original_inventory
    assert redis.events[1].payload["inbound_id"] == "inbound-order-42"
    assert redis.events[1].payload["source_type"] == "FUTURE_INBOUND"


def test_simulation_replay_rejects_future_lot_consumption_before_available_time() -> None:
    state = _future_lot_replay_state(
        available_step=2,
        completion_step=1,
        allocations=[{"warehouse_item_id": "virtual-lot-42", "quantity": 20}],
    )

    with pytest.raises(ValueError, match="재고 항목을 찾을 수 없습니다: virtual-lot-42"):
        replay_simulation_session(
            state,
            SimulationResult(success=True, valid=True, status="SUCCESS"),
            ReplayInventoryRedis(),
        )


def test_simulation_replay_allows_future_only_allocation_at_available_time() -> None:
    state = _future_lot_replay_state(
        available_step=2,
        completion_step=2,
        allocations=[{"warehouse_item_id": "virtual-lot-42", "quantity": 20}],
    )
    redis = ReplayInventoryRedis()

    replay_simulation_session(
        state,
        SimulationResult(success=True, valid=True, status="SUCCESS"),
        redis,
    )

    assert [event.event_type for event in redis.events][-2:] == [
        "INBOUND_AVAILABLE",
        "TASK_COMPLETED",
    ]
    assert {row["warehouse_item_id"]: row["quantity"] for row in redis.inventory}["virtual-lot-42"] == 0


def test_simulation_replays_inbound_available_without_real_state() -> None:
    redis = SimulationInventoryRedis()
    state = {
        "simulation_id": "SIM-IN",
        "command": {"warehouse_id": 1},
        "snapshot": {"captured_at": REFERENCE.isoformat()},
        "collision_plan": {"time_step_seconds": 5, "routes": []},
        "cuopt_plan": {"scheduled_tasks": []},
        "required_tasks": [],
        "inventory_operations": [
            InventoryOperationRequest(
                operation_id="IN-1",
                operation_type="INBOUND",
                item_id="A",
                quantity_boxes=10,
                expected_available_at=REFERENCE + timedelta(minutes=10),
            ).model_dump(mode="json")
        ],
    }
    result = replay_simulation_session(
        state,
        SimulationResult(success=True, valid=True, status="SUCCESS"),
        redis,
    )
    assert result["event_count"] == 1
    assert redis.events[0].event_type == "INBOUND_AVAILABLE"
    assert redis.events[0].execution_context == "SIMULATION"

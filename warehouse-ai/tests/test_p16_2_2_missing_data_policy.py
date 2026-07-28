from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models import CommandInterpretation, InventoryOperationRequest
from app.planning import nodes
from app.repositories.postgres import PostgresRepository
from app.services.inventory_projection import InventoryProjectionService


REFERENCE = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)


def test_robot_status_reports_each_robot_and_system_default_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(min_robot_battery=20.0),
    )
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="ROBOT_QUERY",
        objective="모든 로봇 상태 위치 배터리와 최소 운용 배터리 기준 알려줘",
        query_target="ROBOT",
        query_action="STATUS",
        execution_mode="PLAN_ONLY",
        summary="로봇 상태",
    )
    answer, data = nodes.query_report(
        interpretation,
        {
            "sql": {
                "robots": [
                    {
                        "robot_id": "R1",
                        "robot_code": "R1",
                        "status": "IDLE",
                        "node_id": 10,
                        "battery": 91,
                    }
                ]
            },
            "redis": {
                "robots": [],
                "tasks": [],
                "executing_task_ids": [],
                "planned_task_ids": [],
            },
        },
    )

    assert "현재 노드 10" in answer
    assert "현재 배터리 91%" in answer
    assert "창고별 최소 운용 배터리 정책은 등록되지 않았으며" in answer
    assert data["minimum_battery_policy"] == {
        "status": "WAREHOUSE_POLICY_NOT_CONFIGURED_USING_SYSTEM_DEFAULT",
        "minimum_battery_percent": 20.0,
        "source": "SYSTEM_DEFAULT",
    }


def test_robot_status_reports_explicit_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            min_robot_battery=25.0,
            model_fields_set={"min_robot_battery"},
        ),
    )
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="ROBOT_QUERY",
        objective="최소 운용 배터리 알려줘",
        query_target="ROBOT",
        query_action="STATUS",
        execution_mode="PLAN_ONLY",
        summary="배터리 정책",
    )
    answer, data = nodes.query_report(
        interpretation,
        {
            "sql": {"robots": []},
            "redis": {
                "robots": [],
                "tasks": [],
                "executing_task_ids": [],
                "planned_task_ids": [],
            },
        },
    )
    assert "최소 운용 배터리 정책은 25%로 설정" in answer
    assert data["minimum_battery_policy"]["status"] == "CONFIGURED"


def test_inventory_query_lists_registered_zero_stock_and_unregistered_items() -> None:
    all_items = CommandInterpretation(
        command_kind="QUERY",
        intent="INVENTORY_QUERY",
        objective="모든 상품과 현재 재고, 예정 입고를 알려줘",
        query_target="INVENTORY",
        query_action="COUNT",
        load_open_inventory_orders=True,
        execution_mode="PLAN_ONLY",
        summary="전체 재고",
    )
    answer, data = nodes.query_report(
        all_items,
        {
            "sql": {
                "inventory_items": [
                    {"item_id": "A", "item_name": "A", "base_unit": "BOX"},
                    {"item_id": "B", "item_name": "B", "base_unit": "BOX"},
                    {"item_id": "C", "item_name": "C", "base_unit": "BOX"},
                ],
                "inventory": [
                    {"item_id": "A", "available_quantity": 10, "lot_id": "A-1"}
                ],
                "inbound_orders": [
                    {"inbound_id": "IN-B", "item_id": "B", "quantity_boxes": 20}
                ],
                "outbound_orders": [],
            },
            "redis": {"inventory_reservations": []},
            "graph": {"nodes": []},
        },
    )
    assert data["item_ids"] == ["A", "B", "C"]
    assert "- B: 현재 가용 재고 없음 / 예정 입고 있음, 20 BOX" in answer
    assert "- C: 현재 가용 재고 없음 / 예정 입고 없음, 0 BOX" in answer

    unknown = all_items.model_copy(update={"item_ids": ["Z"]})
    unknown_answer, unknown_data = nodes.query_report(
        unknown,
        {
            "sql": {
                "inventory_items": [{"item_id": "A", "item_name": "A"}],
                "inventory": [],
                "inbound_orders": [],
                "outbound_orders": [],
            },
            "redis": {"inventory_reservations": []},
            "graph": {"nodes": []},
        },
    )
    assert unknown_data["unregistered_item_ids"] == ["Z"]
    assert "현재 시스템에 등록되어 있지 않습니다: Z" in unknown_answer


def test_snapshot_empty_item_filter_means_all_items() -> None:
    repo = object.__new__(PostgresRepository)
    captured: list[list[str]] = []
    repo.fetch_open_works = lambda warehouse_id: [
        {"work_id": "W-A", "item_id": "A", "status": "NEW"}
    ]
    repo.fetch_inventory = lambda warehouse_id, item_ids: captured.append(list(item_ids)) or []
    repo.fetch_inventory_items = lambda item_ids: []
    repo.fetch_inbound_orders = lambda warehouse_id, item_ids: []
    repo.fetch_outbound_orders = lambda warehouse_id, item_ids: []
    repo.fetch_storage_capacity = lambda warehouse_id: []
    repo.fetch_robots = lambda warehouse_id: []
    repo.fetch_work_statuses = lambda warehouse_id: []
    repo.fetch_work_dependencies = lambda warehouse_id: []
    repo.fetch_work_schedule_constraints = lambda warehouse_id: []

    PostgresRepository.snapshot(repo, 2, [])
    PostgresRepository.snapshot(repo, 2, ["B"])

    assert captured[0] == []
    assert captured[1] == ["A", "B"]


def test_anonymous_future_event_does_not_create_fulfillment_time() -> None:
    result = InventoryProjectionService(REFERENCE).evaluate(
        [
            InventoryOperationRequest(
                operation_id="OP-X",
                operation_type="OUTBOUND",
                item_id="X",
                quantity_boxes=20,
                required_at=REFERENCE,
            )
        ],
        current_lots=[],
        simulation_events=[
            {
                "item_id": "X",
                "quantity_delta_boxes": 20,
                "at": REFERENCE + timedelta(minutes=10),
            }
        ],
    )
    item = result.item_results[0]
    assert item.available_quantity_boxes == 0
    assert item.earliest_full_fulfillment_at is None

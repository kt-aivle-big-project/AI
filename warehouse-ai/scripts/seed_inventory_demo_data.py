"""Seed deterministic inventory fixtures for local development only.

The script is intentionally manual.  It never runs migrations and refuses to
run outside APP_ENV=dev/development/local/test.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import text

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.repositories.postgres import PostgresRepository
from scripts.reset_demo_data import assert_development_environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="개발 환경에 BOX 단위 재고·입출고 재현 데이터를 넣습니다."
    )
    parser.add_argument("--warehouse-id", type=int, required=True)
    parser.add_argument("--storage-node-id", type=int, default=2)
    parser.add_argument("--outbound-node-id", type=int, default=4)
    parser.add_argument("--confirm", action="store_true", required=True)
    return parser


def seed_inventory(
    repository: PostgresRepository,
    *,
    warehouse_id: int,
    storage_node_id: int,
    outbound_node_id: int,
    reference_time: datetime,
    warehouse_timezone: str = "Asia/Seoul",
) -> dict[str, int]:
    timezone = ZoneInfo(warehouse_timezone or "Asia/Seoul")
    local_reference = reference_time.astimezone(timezone)

    def next_local(hour: int, minute: int = 0) -> datetime:
        candidate = local_reference.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local_reference:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    inbound_arrival_at = next_local(7)
    inbound_available_at = inbound_arrival_at + timedelta(minutes=10)
    available_at = reference_time - timedelta(hours=1)
    expires = reference_time + timedelta(days=30)
    item_quantities = {"A": 40, "B": 20, "C": 60, "D": 15, "E": 120, "F": 30}
    with repository.engine.begin() as connection:
        for item_id, quantity in item_quantities.items():
            connection.execute(
                text(
                    """
                    INSERT INTO inventory_item (
                        item_id, item_name, base_unit, active, created_at, updated_at
                    ) VALUES (
                        :item_id, :item_name, 'BOX', true, :now, :now
                    )
                    ON CONFLICT (item_id) DO UPDATE
                    SET item_name = EXCLUDED.item_name,
                        base_unit = 'BOX', active = true, updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "item_id": item_id,
                    "item_name": f"Demo item {item_id}",
                    "now": reference_time,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO warehouse_items (
                        warehouse_item_id, warehouse_id, item_id, lot_id, node_id,
                        quantity, reserved_quantity, expiry_date, version, status,
                        received_at, available_at, expiration_at, base_unit
                    ) VALUES (
                        :warehouse_item_id, :warehouse_id, :item_id, :lot_id,
                        :node_id, :quantity, 0, CAST(:expiry_date AS date), 1,
                        'AVAILABLE', :available_at, :available_at, :expiration_at,
                        'BOX'
                    )
                    ON CONFLICT (warehouse_item_id) DO UPDATE
                    SET quantity = EXCLUDED.quantity,
                        reserved_quantity = 0,
                        status = 'AVAILABLE',
                        received_at = EXCLUDED.received_at,
                        available_at = EXCLUDED.available_at,
                        expiration_at = EXCLUDED.expiration_at,
                        base_unit = 'BOX',
                        version = warehouse_items.version + 1
                    """
                ),
                {
                    "warehouse_item_id": f"DEMO-INV-{warehouse_id}-{item_id}",
                    "warehouse_id": warehouse_id,
                    "item_id": item_id,
                    "lot_id": f"DEMO-LOT-{item_id}-01",
                    "node_id": storage_node_id,
                    "quantity": quantity,
                    "expiry_date": expires.date(),
                    "available_at": available_at,
                    "expiration_at": expires,
                },
            )

        for item_id, quantity in (("A", 50), ("B", 100), ("F", 20)):
            connection.execute(
                text(
                    """
                    INSERT INTO inbound_order_line (
                        inbound_id, warehouse_id, item_id, quantity_boxes,
                        expected_arrival_at, expected_available_at, status,
                        storage_node_id, lot_id, created_at, updated_at
                    ) VALUES (
                        :inbound_id, :warehouse_id, :item_id, :quantity,
                        :arrival_at, :available_at, 'INSPECTING',
                        :storage_node_id, :lot_id, :now, :now
                    )
                    ON CONFLICT (inbound_id) DO UPDATE
                    SET quantity_boxes = EXCLUDED.quantity_boxes,
                        expected_arrival_at = EXCLUDED.expected_arrival_at,
                        expected_available_at = EXCLUDED.expected_available_at,
                        actual_arrival_at = NULL,
                        actual_available_at = NULL,
                        status = 'INSPECTING',
                        storage_node_id = EXCLUDED.storage_node_id,
                        warehouse_item_id = NULL,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "inbound_id": f"DEMO-IN-{warehouse_id}-{item_id}",
                    "warehouse_id": warehouse_id,
                    "item_id": item_id,
                    "quantity": quantity,
                    "arrival_at": inbound_arrival_at,
                    "available_at": inbound_available_at,
                    "storage_node_id": storage_node_id,
                    "lot_id": f"DEMO-LOT-{item_id}-02",
                    "now": reference_time,
                },
            )
        for item_id, quantity in (("A", 30), ("F", 50)):
            required_at = next_local(1, 30) if item_id == "A" else inbound_arrival_at
            connection.execute(
                text(
                    """
                    INSERT INTO outbound_order_line (
                        outbound_id, warehouse_id, item_id,
                        requested_quantity_boxes, required_by, priority,
                        allow_partial_fulfillment, status, work_id,
                        created_at, updated_at
                    ) VALUES (
                        :outbound_id, :warehouse_id, :item_id, :quantity,
                        :required_by, :priority, false, 'OPEN', :work_id,
                        :now, :now
                    )
                    ON CONFLICT (outbound_id) DO UPDATE
                    SET requested_quantity_boxes = EXCLUDED.requested_quantity_boxes,
                        required_by = EXCLUDED.required_by,
                        priority = EXCLUDED.priority,
                        allow_partial_fulfillment = false,
                        status = 'OPEN', work_id = EXCLUDED.work_id,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "outbound_id": f"DEMO-OUT-{warehouse_id}-{item_id}",
                    "warehouse_id": warehouse_id,
                    "item_id": item_id,
                    "quantity": quantity,
                    "required_by": required_at,
                    "priority": "EMERGENCY" if item_id == "F" else "NORMAL",
                    "work_id": f"DEMO-W-OUT-{warehouse_id}-{item_id}",
                    "now": reference_time,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO works (
                        work_id, warehouse_id, task_code, item_id, quantity,
                        source_node, target_node, priority, status,
                        assigned_robot_id, scheduled_start, scheduled_end,
                        version, operation_type, quantity_boxes, required_at,
                        allow_partial_fulfillment, inventory_order_id
                    ) VALUES (
                        :work_id, :warehouse_id, 'OUTBOUND', :item_id, :quantity,
                        :source_node, :target_node, :priority_number, 'NEW',
                        NULL, :scheduled_start, :scheduled_end, 1, 'OUTBOUND',
                        :quantity, :required_at, false, :outbound_id
                    )
                    ON CONFLICT (work_id) DO UPDATE
                    SET item_id = EXCLUDED.item_id,
                        quantity = EXCLUDED.quantity,
                        source_node = EXCLUDED.source_node,
                        target_node = EXCLUDED.target_node,
                        priority = EXCLUDED.priority,
                        status = 'NEW', assigned_robot_id = NULL,
                        scheduled_start = EXCLUDED.scheduled_start,
                        scheduled_end = EXCLUDED.scheduled_end,
                        operation_type = 'OUTBOUND',
                        quantity_boxes = EXCLUDED.quantity_boxes,
                        required_at = EXCLUDED.required_at,
                        allow_partial_fulfillment = false,
                        inventory_order_id = EXCLUDED.inventory_order_id,
                        version = COALESCE(works.version, 0) + 1
                    """
                ),
                {
                    "work_id": f"DEMO-W-OUT-{warehouse_id}-{item_id}",
                    "warehouse_id": warehouse_id,
                    "item_id": item_id,
                    "quantity": quantity,
                    "source_node": storage_node_id,
                    "target_node": outbound_node_id,
                    "priority_number": 1 if item_id == "F" else 10,
                    "scheduled_start": required_at - timedelta(minutes=5),
                    "scheduled_end": required_at,
                    "required_at": required_at,
                    "outbound_id": f"DEMO-OUT-{warehouse_id}-{item_id}",
                },
            )
    return {"items": len(item_quantities), "inbounds": 3, "outbounds": 2, "works": 2}


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    assert_development_environment(settings.app_env)
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL이 필요합니다.")
    repository = PostgresRepository(settings.database_url)
    repository.healthcheck()
    result = seed_inventory(
        repository,
        warehouse_id=args.warehouse_id,
        storage_node_id=args.storage_node_id,
        outbound_node_id=args.outbound_node_id,
        reference_time=datetime.now(UTC),
        warehouse_timezone=settings.warehouse_timezone or "Asia/Seoul",
    )
    print(
        json.dumps(
            {
                "status": "INVENTORY_DEMO_DATA_SEEDED",
                "environment": settings.app_env,
                "warehouse_id": args.warehouse_id,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

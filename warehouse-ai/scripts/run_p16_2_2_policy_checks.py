from __future__ import annotations

import json
from types import SimpleNamespace

from app.models import CommandInterpretation
from app.planning import nodes


def main() -> int:
    original = nodes.get_settings
    try:
        nodes.get_settings = lambda: SimpleNamespace(min_robot_battery=20.0)
        robot_interpretation = CommandInterpretation(
            command_kind="QUERY",
            intent="ROBOT_QUERY",
            objective="로봇 상태와 최소 운용 배터리 알려줘",
            query_target="ROBOT",
            query_action="STATUS",
            execution_mode="PLAN_ONLY",
            summary="로봇 정책 확인",
        )
        robot_answer, robot_data = nodes.query_report(
            robot_interpretation,
            {
                "sql": {
                    "robots": [
                        {
                            "robot_id": "R2-01",
                            "robot_code": "R2-01",
                            "status": "IDLE",
                            "node_id": 2146,
                            "battery": 100,
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
        inventory_interpretation = CommandInterpretation(
            command_kind="QUERY",
            intent="INVENTORY_QUERY",
            objective="모든 상품과 재고 알려줘",
            query_target="INVENTORY",
            query_action="COUNT",
            load_open_inventory_orders=True,
            execution_mode="PLAN_ONLY",
            summary="미등록 정책 확인",
        )
        inventory_answer, inventory_data = nodes.query_report(
            inventory_interpretation,
            {
                "sql": {
                    "inventory_items": [
                        {"item_id": "A", "item_name": "A", "base_unit": "BOX"},
                        {"item_id": "B", "item_name": "B", "base_unit": "BOX"},
                    ],
                    "inventory": [
                        {"item_id": "A", "available_quantity": 10, "lot_id": "A-1"}
                    ],
                    "inbound_orders": [],
                    "outbound_orders": [],
                },
                "redis": {"inventory_reservations": []},
                "graph": {"nodes": []},
            },
        )
        checks = {
            "robot_details_visible": "현재 노드 2146" in robot_answer
            and "현재 배터리 100%" in robot_answer,
            "minimum_policy_source_visible": robot_data["minimum_battery_policy"]["source"]
            == "SYSTEM_DEFAULT",
            "registered_zero_stock_visible": "B: 현재 가용 재고 없음" in inventory_answer,
            "all_registered_items_included": inventory_data["item_ids"] == ["A", "B"],
        }
    finally:
        nodes.get_settings = original
    result = {"all_passed": all(checks.values()), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

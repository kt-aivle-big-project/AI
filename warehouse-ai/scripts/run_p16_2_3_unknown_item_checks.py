from __future__ import annotations

import json
from datetime import UTC, datetime

from app.models import CommandInterpretation, InventoryOperationRequest
from app.planning import graph, nodes
from app.services.user_reporting import build_user_report_summary


REFERENCE = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)


def snapshot(item_ids: list[str]) -> dict:
    return {
        "captured_at": REFERENCE.isoformat(),
        "sql": {
            "inventory_items": [
                {"item_id": value, "item_name": value, "base_unit": "BOX"}
                for value in item_ids
            ],
            "inventory": [],
            "inbound_orders": [],
            "outbound_orders": [],
            "works": [],
            "storage_capacity": [],
        },
        "redis": {"inventory_reservations": []},
        "graph": {"nodes": [], "edges": []},
    }


def interpretation(item_id: str) -> CommandInterpretation:
    operation = InventoryOperationRequest(
        operation_id=f"OP-{item_id}",
        operation_type="OUTBOUND",
        item_id=item_id,
        quantity_boxes=20,
        required_at=REFERENCE,
    )
    return CommandInterpretation(
        command_kind="PLAN",
        intent="OUTBOUND",
        objective=f"{item_id} 20 BOX 출고",
        item_ids=[item_id],
        quantity=20,
        inventory_operations=[operation],
        execution_mode="SIMULATE_ONLY",
        summary="미등록 품목 정책 검사",
    )


def main() -> int:
    rejected = nodes.inventory_precheck_node(
        {
            "interpretation": interpretation("Z").model_dump(mode="json"),
            "snapshot": snapshot(["A", "B"]),
        }
    )
    item = rejected["inventory_feasibility"]["item_results"][0]
    report = build_user_report_summary(
        rejected,
        {
            "execution_mode": "SIMULATE_ONLY",
            "inventory_feasibility": rejected["inventory_feasibility"],
            "emergency_review_items": rejected["emergency_review_items"],
            "inventory_unknown_item_ids": rejected["inventory_unknown_item_ids"],
            "warnings": rejected["inventory_feasibility"]["warnings"],
        },
        report_level="SUMMARY",
    )
    candidate = nodes.inventory_precheck_node(
        {
            "interpretation": interpretation("ITEM A").model_dump(mode="json"),
            "snapshot": snapshot(["ITEM-A"]),
        }
    )
    checks = {
        "unknown_item_rejected_without_clarification": (
            rejected["final_status"] == "EMERGENCY_REVIEW_REQUIRED"
            and not rejected["interpretation"]["missing_information"]
            and graph.after_inventory_precheck(rejected) == "report"
        ),
        "no_invented_fulfillment_time": item["earliest_full_fulfillment_at"] is None,
        "no_task_quantity_planned": item["planned_quantity_boxes"] == 0,
        "explicit_unregistered_report": (
            report.primary_message
            == "Z 품목은 시스템에 등록되지 않아 작업 계획을 생성하지 않았습니다."
        ),
        "similar_candidate_clarification": (
            candidate["final_status"] == "CLARIFICATION_REQUIRED"
            and candidate["inventory_item_candidates"] == {"ITEM A": ["ITEM-A"]}
            and graph.after_inventory_precheck(candidate) == "clarification"
        ),
    }
    result = {"all_passed": all(checks.values()), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

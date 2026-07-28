from datetime import UTC, datetime
from types import SimpleNamespace

from app.models import CommandInterpretation, InventoryOperationRequest, NaturalLanguageCommand
from app.planning import graph, nodes
from app.services.user_reporting import build_user_report_summary


REFERENCE = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)


def _snapshot(item_ids: list[str]) -> dict:
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


def _interpretation(item_id: str) -> CommandInterpretation:
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
        summary="미등록 품목 출고",
    )


def test_unknown_item_without_candidate_rejects_without_clarification() -> None:
    update = nodes.inventory_precheck_node(
        {
            "interpretation": _interpretation("Z").model_dump(mode="json"),
            "snapshot": _snapshot(["A", "B"]),
        }
    )

    assert update["final_status"] == "EMERGENCY_REVIEW_REQUIRED"
    assert update["inventory_unknown_item_ids"] == ["Z"]
    assert update["inventory_item_candidates"] == {}
    assert update["interpretation"]["missing_information"] == []
    assert update["inventory_projection"] == []
    assert update["inventory_feasibility"]["status"] == "FAILED"
    item = update["inventory_feasibility"]["item_results"][0]
    assert item["item_id"] == "Z"
    assert item["planned_quantity_boxes"] == 0
    assert item["available_quantity_boxes"] == 0
    assert item["shortage_quantity_boxes"] == 20
    assert item["earliest_full_fulfillment_at"] is None
    assert graph.after_inventory_precheck(update) == "report"


def test_unknown_item_report_states_registration_failure() -> None:
    update = nodes.inventory_precheck_node(
        {
            "interpretation": _interpretation("Z").model_dump(mode="json"),
            "snapshot": _snapshot(["A"]),
        }
    )
    summary = build_user_report_summary(
        update,
        {
            "execution_mode": "SIMULATE_ONLY",
            "inventory_feasibility": update["inventory_feasibility"],
            "emergency_review_items": update["emergency_review_items"],
            "inventory_unknown_item_ids": update["inventory_unknown_item_ids"],
            "warnings": update["inventory_feasibility"]["warnings"],
        },
        report_level="SUMMARY",
    )

    assert summary.outcome == "FAILED"
    assert summary.title == "미등록 품목으로 계획을 생성하지 않았습니다."
    assert summary.primary_message == (
        "Z 품목은 시스템에 등록되지 않아 작업 계획을 생성하지 않았습니다."
    )
    assert "등록된 품목 ID" in summary.recommended_action


def test_similar_item_candidate_keeps_clarification_path(monkeypatch) -> None:
    update = nodes.inventory_precheck_node(
        {
            "interpretation": _interpretation("ITEM A").model_dump(mode="json"),
            "snapshot": _snapshot(["ITEM-A"]),
        }
    )

    assert update["final_status"] == "CLARIFICATION_REQUIRED"
    assert update["inventory_item_candidates"] == {"ITEM A": ["ITEM-A"]}
    assert update["interpretation"]["missing_information"] == [
        "ambiguous_inventory_item:ITEM A"
    ]
    assert graph.after_inventory_precheck(update) == "clarification"

    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=object()),
    )
    clarification = nodes.clarification_node(
        {
            "command": NaturalLanguageCommand(
                warehouse_id=2,
                text="ITEM A 20 BOX 출고",
            ).model_dump(mode="json"),
            "interpretation": update["interpretation"],
            "snapshot": _snapshot(["ITEM-A"]),
            "inventory_item_candidates": update["inventory_item_candidates"],
        }
    )
    assert clarification["clarification"]["reason_code"] == (
        "AMBIGUOUS_INVENTORY_ITEM"
    )
    assert clarification["clarification"]["options"][0]["value"] == "ITEM-A"


def test_final_response_warnings_are_deduplicated(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            warehouse_timezone="Asia/Seoul",
            time_step_seconds=5,
            openai_api_key="",
        ),
    )
    interpretation = _interpretation("Z")
    precheck = nodes.inventory_precheck_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": _snapshot(["A"]),
        }
    )
    state = {
        "command": NaturalLanguageCommand(
            warehouse_id=2,
            text="Z 20 BOX 출고",
            requested_execution_mode="SIMULATE_ONLY",
        ).model_dump(mode="json"),
        "interpretation": precheck["interpretation"],
        "supervisor_decision": {},
        "snapshot": _snapshot(["A"]),
        "validation": {"valid": True, "errors": [], "warnings": []},
        "inventory_operations": precheck["inventory_operations"],
        "inventory_feasibility": precheck["inventory_feasibility"],
        "inventory_projection": [],
        "capacity_feasibility": precheck["capacity_feasibility"],
        "emergency_review_items": precheck["emergency_review_items"],
        "inventory_unknown_item_ids": ["Z"],
        "inventory_item_candidates": {},
        "warnings": [
            "DEFAULT_WAREHOUSE_TIMEZONE_USED",
            "DEFAULT_WAREHOUSE_TIMEZONE_USED",
        ],
        "errors": [],
        "trace": [],
        "final_status": "EMERGENCY_REVIEW_REQUIRED",
    }
    result = nodes.generate_final_report_node(state)
    assert result["response"]["warnings"] == [
        "DEFAULT_WAREHOUSE_TIMEZONE_USED"
    ]

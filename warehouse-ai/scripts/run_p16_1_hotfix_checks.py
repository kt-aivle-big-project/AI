"""Run deterministic P16.1 daily-schedule/outbound hotfix checks.

No database, Redis, Neo4j, or LLM connection is required.  The check covers
operation-bound time windows, same-node multi-lot trip consolidation, and the
empty execution plan verification guard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import CommandInterpretation, InventoryOperationRequest
from app.planning.nodes import build_verification_evidence
from app.services.command_language import parse_deterministic_command
from app.services.task_splitting import capacity_trip_groups


WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)


def _time_window_check() -> dict[str, Any]:
    interpretation = parse_deterministic_command(
        "2026년 7월 24일 오전 7시 15분을 기준으로 "
        "오전 9시부터 오전 11시 사이에 A상품 30 BOX와 B상품 20 BOX를 "
        "출고 노드 2146으로 이동하는 계획을 시뮬레이션해줘.",
        reference_time=datetime(2026, 7, 24, 6, 0, tzinfo=UTC),
        warehouse_timezone=None,
    )
    constraints = interpretation.scheduled_task_constraints
    passed = bool(
        len(interpretation.inventory_operations) == 2
        and len(constraints) == 2
        and all(row.earliest_start == WINDOW_START for row in constraints)
        and all(row.latest_finish == WINDOW_END for row in constraints)
        and all(
            operation.required_at == WINDOW_END
            for operation in interpretation.inventory_operations
        )
    )
    return {
        "passed": passed,
        "operation_count": len(interpretation.inventory_operations),
        "constraint_count": len(constraints),
        "earliest_start": constraints[0].earliest_start.isoformat() if constraints else None,
        "latest_finish": constraints[0].latest_finish.isoformat() if constraints else None,
    }


def _lot_consolidation_check() -> dict[str, Any]:
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
    passed = bool(
        len(pairs) == 1
        and pairs[0]["quantity_boxes"] == 30
        and len(pairs[0]["allocations"]) == 2
    )
    return {
        "passed": passed,
        "transport_pair_count": len(pairs),
        "quantity_boxes": pairs[0]["quantity_boxes"] if pairs else 0,
        "lot_count": len(pairs[0]["allocations"]) if pairs else 0,
    }


def _empty_plan_guard_check() -> dict[str, Any]:
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
        summary="P16.1 check",
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
    codes = {row["code"] for row in evidence}
    return {
        "passed": "EMPTY_EXECUTION_PLAN" in codes
        and "DETERMINISTIC_VALIDATION_PASSED" not in codes,
        "evidence_codes": sorted(codes),
    }


def run_all_checks() -> dict[str, Any]:
    checks = {
        "operation_time_window": _time_window_check(),
        "same_node_lot_consolidation": _lot_consolidation_check(),
        "empty_execution_plan_guard": _empty_plan_guard_check(),
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

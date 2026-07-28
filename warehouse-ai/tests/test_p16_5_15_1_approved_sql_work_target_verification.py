from __future__ import annotations

from app.planning.nodes import build_verification_evidence


WORK_ID = "P16-W-OUT-2-C-001"
TARGET_NODE = 2146


def _state(*, operation: dict | None = None) -> dict:
    operation = operation or {
        "operation_id": f"work:{WORK_ID}",
        "work_id": WORK_ID,
        "operation_type": "OUTBOUND",
        "item_id": "C",
        "quantity_boxes": 1,
    }
    return {
        "interpretation": {
            "target_node_ids": [TARGET_NODE],
            "inventory_operations": [operation],
        },
        "supervisor_decision": {"requires_clarification": False},
        "validation": {"valid": True, "errors": [], "warnings": []},
        "simulation": {
            "success": True,
            "valid": True,
            "status": "SUCCESS",
            "issues": [],
            "errors": [],
            "warnings": [],
        },
        "optimization_problem": {"nodes": [], "time_step_seconds": 5},
        "simulation_metrics": {},
        "inventory_feasibility": {
            "status": "PASS",
            "valid": True,
            "item_results": [
                {
                    "operation_id": f"work:{WORK_ID}",
                    "work_id": WORK_ID,
                    "operation_type": "OUTBOUND",
                    "planned_quantity_boxes": 1,
                    "status": "PASS",
                }
            ],
        },
        "cuopt_plan": {
            "scheduled_tasks": [],
            "unassigned_task_ids": [],
            "metadata": {},
        },
        "robot_command_batches": [],
        "warnings": [],
    }


def _codes(state: dict) -> list[str]:
    return [row["code"] for row in build_verification_evidence(state)]


def test_approved_sql_outbound_move_satisfies_requested_target() -> None:
    state = _state()
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": f"{WORK_ID}:move",
            "work_id": WORK_ID,
            "action": "MOVE",
            "item_id": "C",
            "source_node": 2088,
            "target_node": TARGET_NODE,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" not in _codes(state)


def test_work_namespace_operation_id_is_linked_to_legacy_move() -> None:
    state = _state(
        operation={
            "operation_id": f"work:{WORK_ID}",
            "work_id": None,
            "operation_type": "OUTBOUND",
            "item_id": "C",
            "quantity_boxes": 1,
        }
    )
    state["inventory_feasibility"]["item_results"] = [
        {
            "operation_id": f"work:{WORK_ID}",
            "work_id": None,
            "operation_type": "OUTBOUND",
            "planned_quantity_boxes": 1,
            "status": "PASS",
        }
    ]
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": f"{WORK_ID}:move",
            "work_id": WORK_ID,
            "action": "MOVE",
            "target_node": TARGET_NODE,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" not in _codes(state)


def test_approved_sql_outbound_move_with_wrong_target_is_blocked() -> None:
    state = _state()
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": f"{WORK_ID}:move",
            "work_id": WORK_ID,
            "action": "MOVE",
            "target_node": 9999,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" in _codes(state)


def test_unrelated_relocation_move_does_not_satisfy_outbound_target() -> None:
    state = _state()
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": "relocation:R2-01:move",
            "work_id": "relocation:R2-01",
            "action": "MOVE",
            "target_node": TARGET_NODE,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" in _codes(state)


def test_matching_work_move_requires_canonical_move_task_identity() -> None:
    state = _state()
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": f"{WORK_ID}:relocate",
            "work_id": WORK_ID,
            "action": "MOVE",
            "target_node": TARGET_NODE,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" in _codes(state)

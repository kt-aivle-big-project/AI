from app.planning.nodes import build_verification_evidence


def _base_state() -> dict:
    return {
        "interpretation": {
            "target_node_ids": [2146],
            "inventory_operations": [
                {
                    "operation_id": "OP-A",
                    "operation_type": "OUTBOUND",
                    "item_id": "A",
                },
                {
                    "operation_id": "OP-B",
                    "operation_type": "OUTBOUND",
                    "item_id": "B",
                },
                {
                    "operation_id": "OP-C",
                    "operation_type": "INBOUND",
                    "item_id": "C",
                    "storage_node_id": 2088,
                },
            ],
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
        "optimization_problem": {
            "nodes": [],
            "time_step_seconds": 5,
        },
        "simulation_metrics": {},
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


def test_target_check_is_skipped_when_all_outbound_operations_are_inventory_blocked() -> None:
    state = _base_state()
    state["inventory_feasibility"] = {
        "status": "PARTIAL_SUCCESS",
        "item_results": [
            {
                "operation_id": "OP-A",
                "operation_type": "OUTBOUND",
                "planned_quantity_boxes": 0,
                "status": "EMERGENCY_REVIEW_REQUIRED",
            },
            {
                "operation_id": "OP-B",
                "operation_type": "OUTBOUND",
                "planned_quantity_boxes": 0,
                "status": "EMERGENCY_REVIEW_REQUIRED",
            },
            {
                "operation_id": "OP-C",
                "operation_type": "INBOUND",
                "planned_quantity_boxes": 50,
                "status": "PASS",
            },
        ],
    }
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": "OP-C:1:drop",
            "work_id": "OP-C",
            "action": "DROP",
            "target_node": 2088,
            "robot_id": "R2-03",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" not in _codes(state)


def test_target_check_still_blocks_when_planned_outbound_uses_wrong_destination() -> None:
    state = _base_state()
    state["inventory_feasibility"] = {
        "status": "PARTIAL_SUCCESS",
        "item_results": [
            {
                "operation_id": "OP-A",
                "operation_type": "OUTBOUND",
                "planned_quantity_boxes": 0,
                "status": "EMERGENCY_REVIEW_REQUIRED",
            },
            {
                "operation_id": "OP-B",
                "operation_type": "OUTBOUND",
                "planned_quantity_boxes": 20,
                "status": "PASS",
            },
        ],
    }
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": "OP-B:1:drop",
            "work_id": "OP-B",
            "action": "DROP",
            "target_node": 9999,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" in _codes(state)


def test_target_check_passes_when_planned_outbound_uses_requested_destination() -> None:
    state = _base_state()
    state["inventory_feasibility"] = {
        "status": "PARTIAL_SUCCESS",
        "item_results": [
            {
                "operation_id": "OP-A",
                "operation_type": "OUTBOUND",
                "planned_quantity_boxes": 0,
                "status": "EMERGENCY_REVIEW_REQUIRED",
            },
            {
                "operation_id": "OP-B",
                "operation_type": "OUTBOUND",
                "planned_quantity_boxes": 20,
                "status": "PASS",
            },
        ],
    }
    state["cuopt_plan"]["scheduled_tasks"] = [
        {
            "task_id": "OP-B:1:drop",
            "work_id": "OP-B",
            "action": "DROP",
            "target_node": 2146,
            "robot_id": "R2-01",
        }
    ]

    assert "TARGET_NODE_NOT_APPLIED" not in _codes(state)

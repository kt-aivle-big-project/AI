from __future__ import annotations

from scripts.v13_21_complex_support import evaluate_expectations


def test_failure_reason_prevents_false_positive() -> None:
    scenario = {
        "initial_request": {
            "runtime_snapshot": {
                "robot_states": [
                    {
                        "robot_id": "R001",
                        "current_node": "R0_0",
                        "status": "fault",
                        "battery_pct": 80,
                    }
                ]
            }
        },
        "expected": {
            "allowed_status": ["failed", "workflow_hold"],
            "expected_reason_codes": ["ALL_CANDIDATES_UNAVAILABLE"],
            "forbidden_reason_codes": ["missing_simulation_runtime"],
            "must_not_assign_ineligible_robot": True,
        },
    }
    wrong_failure = {
        "status": "failed",
        "errors": [
            {
                "stage": "robot_runtime",
                "code": "missing_simulation_runtime",
                "message": "wrong failure",
            }
        ],
        "plan": None,
    }

    errors = evaluate_expectations(scenario, wrong_failure, phase="initial")

    assert any("expected reason codes" in value for value in errors)
    assert any("forbidden reason codes" in value for value in errors)


def test_expected_workflow_hold_reason_passes() -> None:
    scenario = {
        "initial_request": {},
        "expected": {
            "allowed_status": ["workflow_hold"],
            "expected_reason_codes": ["ALL_CANDIDATES_UNAVAILABLE"],
        },
    }
    response = {
        "status": "workflow_hold",
        "workflow_hold": {
            "reason_code": "ALL_CANDIDATES_UNAVAILABLE",
            "message": "none",
            "required_actions": [],
        },
        "errors": [],
        "plan": None,
    }

    assert evaluate_expectations(scenario, response, phase="initial") == []

from scripts.v13_21_complex_support import execution_requires_openai


def test_execution_requires_openai_uses_actual_server_mode() -> None:
    structured = {"initial_request": {"events": [{"type": "new_order"}]}}
    natural = {"initial_request": {"user_command": "ORD-001 처리"}}

    assert execution_requires_openai(
        structured, {"default_planning_mode": "llm_router"}
    )
    assert not execution_requires_openai(
        structured, {"default_planning_mode": "force_rule"}
    )
    assert execution_requires_openai(
        natural, {"default_planning_mode": "force_rule"}
    )

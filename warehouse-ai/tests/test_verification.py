from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import VerificationDecision
from app.planning import nodes


def _settings(*, api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_model="test-model",
    )


def _validation_result(
    *,
    valid: bool = True,
    issues: list[dict] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    issues = issues or []
    return {
        "success": valid,
        "valid": valid,
        "status": "SUCCESS" if valid else "FAILED",
        "issues": issues,
        "errors": [str(issue["message"]) for issue in issues],
        "warnings": warnings or [],
    }


def _state(result: dict) -> dict:
    return {
        "command": {
            "command_id": "CMD-1",
            "text": "창고 작업을 계획해줘",
        },
        "interpretation": {
            "execution_mode": "PLAN_ONLY",
        },
        "supervisor_decision": {
            "requires_clarification": False,
            "allow_replan": True,
        },
        "validation": {"valid": True, "errors": [], "warnings": []},
        "cuopt_plan": {"scheduled_tasks": [], "unassigned_task_ids": []},
        "collision_plan": {"routes": []},
        "plan_validation": result,
        "errors": [],
        "warnings": [],
        "trace": [],
        "final_status": "PLAN_READY" if result["valid"] else "PLAN_VALIDATION_FAILED",
    }


class _StructuredLLM:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def with_structured_output(self, _schema, **_kwargs):
        return self

    def invoke(self, _messages):
        if self.error:
            raise self.error
        return self.result


def test_verification_decision_rejects_unknown_decision() -> None:
    with pytest.raises(ValidationError):
        VerificationDecision(
            decision="APPROVE",
            requires_replan=False,
            replan_scope="NO_REPLAN",
            summary="invalid",
        )


def test_deterministic_error_cannot_be_overridden_to_pass(monkeypatch) -> None:
    state = _state(
        _validation_result(
            valid=False,
            issues=[
                {
                    "code": "VERTEX_CONFLICT",
                    "message": "R1과 R2가 같은 노드를 점유합니다.",
                    "robot_ids": ["R1", "R2"],
                    "task_ids": ["T1"],
                }
            ],
        )
    )
    hallucinated = VerificationDecision(
        decision="PASS",
        requires_replan=False,
        replan_scope="NO_REPLAN",
        affected_robot_ids=["R999"],
        affected_task_ids=["T999"],
        blocking_findings=[],
        confidence=0.99,
        evidence_ids=["made-up"],
        summary="문제없음",
    )
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings(api_key="key"))
    monkeypatch.setattr(
        nodes,
        "build_verification_llm",
        lambda: _StructuredLLM(hallucinated),
    )

    result = nodes.verification_agent_node(state)

    decision = result["verification_decision"]
    assert decision["decision"] == "REPLAN_LOCAL"
    assert decision["affected_robot_ids"] == ["R1", "R2"]
    assert decision["affected_task_ids"] == ["T1"]
    assert "R999" not in decision["affected_robot_ids"]
    assert "T999" not in decision["affected_task_ids"]
    assert decision["evidence_ids"] != ["made-up"]


def test_valid_warning_becomes_pass_with_warning(monkeypatch) -> None:
    state = _state(
        _validation_result(
            warnings=["작업 T1이 마감보다 1 time-step 늦습니다."],
        )
    )
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings())

    result = nodes.verification_agent_node(state)

    decision = result["verification_decision"]
    assert decision["decision"] == "PASS_WITH_WARNING"
    assert decision["warning_findings"] == [
        "작업 T1이 마감보다 1 time-step 늦습니다."
    ]
    assert result["final_status"] == "PLAN_READY"


def test_disconnected_edge_requires_global_replan(monkeypatch) -> None:
    state = _state(
        _validation_result(
            valid=False,
            issues=[
                {
                    "code": "DISCONNECTED_OR_CLOSED_EDGE",
                    "message": "간선 1→2를 사용할 수 없습니다.",
                    "robot_ids": ["R1"],
                    "task_ids": ["T1"],
                    "node_ids": [1, 2],
                }
            ],
        )
    )
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings())

    result = nodes.verification_agent_node(state)

    assert result["verification_decision"]["decision"] == "REPLAN_GLOBAL"
    assert result["verification_decision"]["replan_scope"] == "GLOBAL_REPLAN"
    assert result["final_status"] == "REPLAN_REQUIRED"


def test_structured_output_failure_uses_deterministic_fallback(monkeypatch) -> None:
    state = _state(_validation_result())
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings(api_key="key"))
    monkeypatch.setattr(
        nodes,
        "build_verification_llm",
        lambda: _StructuredLLM(error=ValueError("invalid structured output")),
    )

    result = nodes.verification_agent_node(state)

    assert result["verification_decision"]["decision"] == "PASS"
    assert result["verification_source"] == "deterministic_fallback"
    assert any("invalid structured output" in warning for warning in result["warnings"])
    assert [row["node"] for row in result["trace"]] == [
        "verification_started",
        "verification_fallback_used",
        "verification_completed",
    ]


def test_llm_cannot_invent_failure_for_valid_plan(monkeypatch) -> None:
    state = _state(_validation_result())
    invented = VerificationDecision(
        decision="FAIL",
        requires_replan=False,
        replan_scope="NO_REPLAN",
        affected_robot_ids=["R404"],
        affected_task_ids=["T404"],
        blocking_findings=["존재하지 않는 충돌"],
        confidence=0.2,
        evidence_ids=["fake"],
        summary="실패",
    )
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings(api_key="key"))
    monkeypatch.setattr(
        nodes,
        "build_verification_llm",
        lambda: _StructuredLLM(invented),
    )

    result = nodes.verification_agent_node(state)

    decision = result["verification_decision"]
    assert decision["decision"] == "PASS"
    assert decision["affected_robot_ids"] == []
    assert decision["affected_task_ids"] == []
    assert decision["blocking_findings"] == []
    assert "fake" not in decision["evidence_ids"]


def test_execution_precheck_rejects_non_pass_verification(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(robot_gateway_url="http://gateway"),
    )
    state = {
        "simulation": {"valid": True},
        "verification_decision": {"decision": "REPLAN_LOCAL"},
    }

    result = nodes.execution_precheck_node(state)

    assert result["execution_ready"] is False
    assert result["final_status"] == "EXECUTION_BLOCKED"



def test_excluded_robot_assignment_is_blocking(monkeypatch) -> None:
    state = _state(_validation_result())
    state["interpretation"]["excluded_robot_ids"] = ["R2-03"]
    state["cuopt_plan"]["scheduled_tasks"] = [
        {"task_id": "T-1", "robot_id": "R2-03"}
    ]
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings())

    result = nodes.verification_agent_node(state)

    decision = result["verification_decision"]
    assert decision["decision"] == "REPLAN_LOCAL"
    assert decision["affected_robot_ids"] == ["R2-03"]
    assert decision["affected_task_ids"] == ["T-1"]
    assert any(
        row["code"] == "EXCLUDED_ROBOT_ASSIGNED"
        for row in result["verification_evidence"]
    )

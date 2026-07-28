from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import (
    CommandInterpretation,
    NaturalLanguageCommand,
    SupervisorDecision,
)
from app.planning import graph as graph_module
from app.planning import nodes


def settings(*, api_key: str = "test-key", max_replan_count: int = 3):
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_model="test-supervisor-model",
        request_timeout_seconds=1,
        max_replan_count=max_replan_count,
        freeze_horizon_seconds=15,
        time_step_seconds=5,
        warehouse_timezone="Asia/Seoul",
    )


def command(mode: str = "PLAN_ONLY") -> NaturalLanguageCommand:
    return NaturalLanguageCommand(
        command_id="COMMAND-SUPERVISOR-1",
        warehouse_id=1,
        text="미완료 작업을 안전하게 계획해줘",
        requested_execution_mode=mode,
    )


def interpretation(
    *,
    command_kind: str = "PLAN",
    execution_mode: str = "PLAN_ONLY",
    missing_information: list[str] | None = None,
) -> CommandInterpretation:
    return CommandInterpretation(
        command_kind=command_kind,
        intent="INVENTORY_QUERY" if command_kind == "QUERY" else "DAILY_PLAN",
        objective="테스트",
        execution_mode=execution_mode,
        missing_information=missing_information or [],
        summary="테스트 해석",
    )


def state(
    command_value: NaturalLanguageCommand,
    interpretation_value: CommandInterpretation,
) -> dict:
    return {
        "command": command_value.model_dump(mode="json"),
        "interpretation": interpretation_value.model_dump(mode="json"),
        "final_status": "INTERPRETED",
    }


class FakeStructuredSupervisor:
    def __init__(self, decision: SupervisorDecision):
        self.decision = decision

    def with_structured_output(self, schema, **_kwargs):
        assert schema is SupervisorDecision
        return self

    def invoke(self, _messages):
        return self.decision.model_copy(deep=True)


def raw_decision(**overrides) -> SupervisorDecision:
    values = {
        "intent": "DAILY_PLAN",
        "command_kind": "PLAN",
        "execution_mode": "PLAN_ONLY",
        "required_tools": ["SNAPSHOT"],
        "plan_mode": "INITIAL_PLAN",
        "requires_clarification": False,
        "risk_level": "LOW",
        "allow_replan": True,
        "max_replan_attempts": 2,
        "reasoning_summary": "짧은 테스트 근거",
    }
    values.update(overrides)
    return SupervisorDecision.model_validate(values)


def test_supervisor_decision_rejects_unknown_tool() -> None:
    with pytest.raises(ValidationError):
        SupervisorDecision.model_validate(
            {
                **raw_decision().model_dump(),
                "required_tools": ["SNAPSHOT", "INVENTED_TOOL"],
            }
        )


def test_execute_safety_normalization_requires_all_safe_tools(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: settings())
    command_value = command("EXECUTE")
    # Interpreter와 명시 요청이 충돌해도 사용자의 EXECUTE 요청은 안전 도구를
    # 모두 거치는 경로로 정규화되어야 합니다.
    interpretation_value = interpretation(execution_mode="PLAN_ONLY")
    unsafe = raw_decision(
        execution_mode="PLAN_ONLY",
        required_tools=["SNAPSHOT"],
        risk_level="LOW",
    )

    decision = nodes.normalize_supervisor_decision(
        unsafe,
        command_value,
        interpretation_value,
    )

    assert decision.command_kind == "EXECUTE"
    assert decision.execution_mode == "EXECUTE"
    assert decision.risk_level == "HIGH"
    assert decision.required_tools == [
        "SNAPSHOT",
        "OPTIMIZER",
        "ROUTING",
        "SIMULATION",
        "VERIFICATION",
        "EXECUTION",
    ]


def test_query_supervisor_uses_snapshot_only_and_disables_replan(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: settings())
    decision = nodes.deterministic_supervisor_decision(
        command("PLAN_ONLY"),
        interpretation(command_kind="QUERY"),
    )

    assert decision.command_kind == "QUERY"
    assert decision.execution_mode == "PLAN_ONLY"
    assert decision.required_tools == ["SNAPSHOT"]
    assert decision.plan_mode == "NO_REPLAN"
    assert decision.allow_replan is False
    assert decision.max_replan_attempts == 0


def test_supervisor_node_uses_deterministic_fallback_on_llm_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: settings())
    monkeypatch.setattr(
        nodes,
        "build_supervisor_llm",
        lambda: (_ for _ in ()).throw(RuntimeError("supervisor unavailable")),
    )

    update = nodes.supervisor_node(
        state(command(), interpretation())
    )
    trace_names = [row["node"] for row in update["trace"]]

    assert update["supervisor_source"] == "deterministic_fallback"
    assert update["supervisor_warnings"]
    assert trace_names == [
        "supervisor_started",
        "supervisor_fallback_used",
        "supervisor_completed",
    ]
    assert update["supervisor_decision"]["required_tools"] == [
        "SNAPSHOT",
        "OPTIMIZER",
        "ROUTING",
        "VERIFICATION",
    ]


def test_supervisor_node_uses_llm_and_caps_replan_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: settings(max_replan_count=1),
    )
    monkeypatch.setattr(
        nodes,
        "build_supervisor_llm",
        lambda: FakeStructuredSupervisor(
            raw_decision(max_replan_attempts=3)
        ),
    )

    update = nodes.supervisor_node(state(command(), interpretation()))
    trace_names = [row["node"] for row in update["trace"]]

    assert update["supervisor_source"] == "llm"
    assert update["supervisor_decision"]["max_replan_attempts"] == 1
    assert trace_names == ["supervisor_started", "supervisor_completed"]


def test_missing_information_routes_to_report_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: settings(api_key=""))
    update = nodes.supervisor_node(
        state(
            command(),
            interpretation(missing_information=["대상 작업 ID가 필요합니다."]),
        )
    )

    assert update["supervisor_decision"]["requires_clarification"] is True
    assert update["supervisor_decision"]["next_node"] == "REPORT"
    assert graph_module.after_supervisor(update) == "report"


def test_route_by_command_uses_supervisor_decision() -> None:
    supervisor = raw_decision(
        command_kind="QUERY",
        execution_mode="PLAN_ONLY",
        required_tools=["SNAPSHOT"],
        plan_mode="NO_REPLAN",
        allow_replan=False,
        max_replan_attempts=0,
    )
    update = nodes.route_by_command_node(
        {
            "interpretation": interpretation().model_dump(mode="json"),
            "supervisor_decision": supervisor.model_dump(mode="json"),
            "supervisor_source": "llm",
        }
    )

    assert update["scope"]["plan_mode"] == "NO_REPLAN"
    assert update["trace"][0]["branch"] == "QUERY"
    assert update["trace"][0]["optimizer_called"] is False


def scope_state(*, active_plan: bool) -> dict:
    return {
        "command": command().model_dump(mode="json"),
        "interpretation": interpretation().model_dump(mode="json"),
        "supervisor_decision": raw_decision(
            plan_mode="GLOBAL_REPLAN"
        ).model_dump(mode="json"),
        "snapshot": {
            "captured_at": "2026-07-22T00:00:00+00:00",
            "sql": {"inventory": [], "robots": [], "works": []},
            "graph": {"nodes": [], "edges": []},
            "redis": {
                "active_plan_version": "PLAN-ACTIVE" if active_plan else None,
                "executing_task_ids": [],
                "planned_task_ids": ["W-001"] if active_plan else [],
                "robots": [],
            },
        },
    }


def test_scope_normalizes_new_plan_mode_and_supervisor_to_initial(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: settings())

    update = nodes.decide_scope_node(scope_state(active_plan=False))

    assert update["scope"]["plan_mode"] == "INITIAL_PLAN"
    assert update["supervisor_decision"]["plan_mode"] == "INITIAL_PLAN"
    decide_trace = next(row for row in update["trace"] if row["node"] == "decide_scope")
    assert decide_trace["plan_mode"] == "INITIAL_PLAN"


def test_scope_keeps_global_replan_when_active_plan_exists(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: settings())

    state_value = scope_state(active_plan=True)
    update = nodes.decide_scope_node(state_value)

    assert update["scope"]["plan_mode"] == "GLOBAL_REPLAN"
    assert "supervisor_decision" not in update
    assert state_value["supervisor_decision"]["plan_mode"] == "GLOBAL_REPLAN"

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import api
from app.models import ClarificationResponse, NaturalLanguageCommand
from app.planning import nodes
from app.planning.graph import run_planning
from tests.test_pipeline import install_fakes, trace_nodes


@pytest.mark.parametrize(
    ("text", "reason_code"),
    [
        ("빠르게 계획해줘", "AMBIGUOUS_OPTIMIZATION_OBJECTIVE"),
        ("효율적으로 처리해줘", "AMBIGUOUS_EXECUTION_MODE"),
        ("그 로봇을 빼줘", "AMBIGUOUS_TARGET"),
        ("아까 작업을 다시 해줘", "AMBIGUOUS_TARGET"),
        ("R-02 고장 났어", "AMBIGUOUS_EVENT_CONTEXT"),
        ("어느 게 좋은지 골라줘", "MISSING_COMPARISON_BASIS"),
    ],
)
def test_ambiguous_commands_stop_at_clarification(
    monkeypatch,
    text,
    reason_code,
) -> None:
    services = install_fakes(monkeypatch)
    settings = nodes.get_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)

    result = run_planning(NaturalLanguageCommand(warehouse_id=1, text=text))

    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["clarification"]["reason_code"] == reason_code
    called = set(trace_nodes(result))
    assert "clarification_required" in called
    assert not called.intersection(
        {
            "build_optimization_problem",
            "local_optimize",
            "build_routes",
            "simulation",
            "activate_plan",
            "dispatch_plan",
        }
    )
    assert services.redis.activation_count == 0
    assert result["optimization_plan"] == {}
    assert result["collision_plan"] == {}
    assert result["simulation"] == {}


def test_target_options_come_only_from_snapshot(monkeypatch) -> None:
    install_fakes(monkeypatch)
    settings = nodes.get_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    result = run_planning(
        NaturalLanguageCommand(warehouse_id=1, text="그 로봇을 제외해줘")
    )
    values = {row["value"] for row in result["clarification"]["options"]}
    assert values == {"R1", "R2", "R3", "W1", "W2", "W3"}


class ClarificationRepository:
    def __init__(self) -> None:
        self.row = {
            "clarification_id": "CL-1",
            "conversation_id": "CONV-1",
            "command_id": "CMD-1",
            "warehouse_id": 1,
            "status": "CLARIFICATION_REQUIRED",
            "original_text": "빠르게 계획해줘",
            "resolved_command_id": None,
        }
        self.resolve_calls = 0

    def get_clarification_request(self, clarification_id):
        return deepcopy(self.row) if clarification_id == "CL-1" else None

    def resolve_clarification_request(
        self,
        clarification_id,
        *,
        response,
        resolved_command_id,
    ):
        self.resolve_calls += 1
        if self.row["status"] == "RESOLVED":
            return deepcopy(self.row)
        self.row.update(
            status="RESOLVED",
            response=deepcopy(response),
            resolved_command_id=resolved_command_id,
        )
        return deepcopy(self.row)


def test_clarification_continuation_and_idempotency(monkeypatch) -> None:
    repository = ClarificationRepository()
    captured = []
    monkeypatch.setattr(
        api,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )

    def fake_run(command):
        captured.append(command)
        return {
            "status": "PLAN_READY",
            "command_id": command.command_id,
            "conversation_id": command.conversation_id,
        }

    monkeypatch.setattr(api, "run_planning", fake_run)
    response = ClarificationResponse(
        selected_value="MINIMIZE_TARDINESS",
        conversation_id="CONV-1",
    )

    first = api.respond_to_clarification("CL-1", response)
    second = api.respond_to_clarification("CL-1", response)

    assert first["status"] == "PLAN_READY"
    assert second["status"] == "ALREADY_RESOLVED"
    assert second["command_id"] == first["command_id"]
    assert repository.resolve_calls == 1
    assert len(captured) == 1
    assert captured[0].parent_command_id == "CMD-1"
    assert captured[0].clarification_id == "CL-1"
    assert "지연 최소화" in captured[0].text


def test_cross_conversation_clarification_is_blocked(monkeypatch) -> None:
    repository = ClarificationRepository()
    monkeypatch.setattr(
        api,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )
    with pytest.raises(HTTPException) as exc_info:
        api.respond_to_clarification(
            "CL-1",
            ClarificationResponse(
                selected_value="PLAN_ONLY",
                conversation_id="CONV-OTHER",
            ),
        )
    assert exc_info.value.status_code == 409
    assert repository.resolve_calls == 0


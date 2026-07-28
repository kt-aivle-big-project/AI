import json
from copy import deepcopy

import pytest

from app import api
from app.models import CommandInterpretation, NaturalLanguageCommand
from app.planning import nodes
from app.planning.graph import run_planning
from app.services.conversation import (
    ConversationAccessError,
    apply_conversation_inheritance,
    compact_conversation_summary,
)
from tests.test_pipeline import FakePostgres, install_fakes


class ConversationPostgres(FakePostgres):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: dict[str, dict] = {}
        self.links: dict[str, list[dict]] = {}

    def create_or_get_conversation(self, conversation_id, warehouse_id):
        row = self.sessions.setdefault(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "warehouse_id": warehouse_id,
                "status": "ACTIVE",
                "active_command_id": None,
                "active_plan_version": None,
                "active_simulation_id": None,
                "active_clarification_id": None,
                "resolved_constraints": {},
                "summary": {},
            },
        )
        if row["warehouse_id"] != warehouse_id:
            raise ValueError("conversation warehouse mismatch")
        return deepcopy(row)

    def get_conversation_command_link(self, conversation_id, command_id):
        return next(
            (
                deepcopy(row)
                for row in self.links.get(conversation_id, [])
                if row["command_id"] == command_id
            ),
            None,
        )

    def link_conversation_command(
        self,
        *,
        conversation_id,
        command_id,
        parent_command_id,
    ):
        rows = self.links.setdefault(conversation_id, [])
        existing = next((row for row in rows if row["command_id"] == command_id), None)
        if existing:
            return deepcopy(existing)
        row = {
            "conversation_id": conversation_id,
            "command_id": command_id,
            "parent_command_id": parent_command_id,
            "sequence_number": len(rows) + 1,
        }
        rows.append(row)
        return deepcopy(row)

    def update_command_parent(self, command_id, parent_command_id):
        self.command_history[command_id]["parent_command_id"] = parent_command_id

    def update_conversation_session(self, conversation_id, values):
        self.sessions[conversation_id].update(deepcopy(values))
        return deepcopy(self.sessions[conversation_id])

    def get_conversation(self, conversation_id):
        row = self.sessions.get(conversation_id)
        return deepcopy(row) if row else None

    def list_conversation_commands(self, conversation_id, *, limit=50, offset=0):
        rows = self.links.get(conversation_id, [])[offset : offset + limit]
        return [
            {**deepcopy(row), **deepcopy(self.command_history.get(row["command_id"], {}))}
            for row in rows
        ]

    def get_clarification_request(self, _clarification_id):
        return None


def install_conversation_fakes(monkeypatch):
    services = install_fakes(monkeypatch)
    services.postgres = ConversationPostgres()
    settings = nodes.get_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    return services


def conversation_trace(result: dict, node: str) -> dict:
    return next(row for row in result["trace"] if row["node"] == node)


def test_robot_limit_inherits_and_is_overridden(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    first = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-C1-1",
            conversation_id="CONV-1",
            warehouse_id=1,
            text="로봇 3대로 계획해줘",
        )
    )
    second = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-C1-2",
            conversation_id="CONV-1",
            warehouse_id=1,
            text="이번에는 2대로 계획해줘",
        )
    )

    assert first["command_id"] != second["command_id"]
    assert second["parent_command_id"] == first["command_id"]
    assert services.postgres.sessions["CONV-1"]["resolved_constraints"][
        "robot_limit"
    ] == 2
    assert [row["sequence_number"] for row in services.postgres.links["CONV-1"]] == [1, 2]


def test_excluded_robot_can_be_removed_in_followup(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            command_id="CMD-EX-1",
            conversation_id="CONV-EX",
            warehouse_id=1,
            text="R-02를 제외하고 계획해줘",
        )
    )
    assert services.postgres.sessions["CONV-EX"]["resolved_constraints"][
        "excluded_robot_ids"
    ] == ["R-02"]
    run_planning(
        NaturalLanguageCommand(
            command_id="CMD-EX-2",
            conversation_id="CONV-EX",
            warehouse_id=1,
            text="R-02를 다시 포함시켜 계획해줘",
        )
    )
    assert services.postgres.sessions["CONV-EX"]["resolved_constraints"][
        "excluded_robot_ids"
    ] == []


def test_optimization_priority_override_preserves_other_constraints(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            command_id="CMD-OPT-1",
            conversation_id="CONV-OPT",
            warehouse_id=1,
            text="로봇 2대로 거리 우선 계획해줘",
        )
    )
    run_planning(
        NaturalLanguageCommand(
            command_id="CMD-OPT-2",
            conversation_id="CONV-OPT",
            warehouse_id=1,
            text="이번에는 시간 우선으로 계획해줘",
        )
    )
    constraints = services.postgres.sessions["CONV-OPT"]["resolved_constraints"]
    assert constraints["robot_limit"] == 2
    assert constraints["optimization_priority"] == "MINIMIZE_MAKESPAN"


def test_followup_optimization_priority_replaces_previous_profile(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    first = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-OPT-OVERRIDE-1",
            conversation_id="CONV-OPT-OVERRIDE",
            warehouse_id=1,
            text="로봇 2대로 전체 완료시간 우선으로 시뮬레이션해줘",
        )
    )
    second = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-OPT-OVERRIDE-2",
            conversation_id="CONV-OPT-OVERRIDE",
            warehouse_id=1,
            text="이번에는 이동거리 우선으로 해봐",
        )
    )
    constraints = services.postgres.sessions["CONV-OPT-OVERRIDE"][
        "resolved_constraints"
    ]

    assert first["optimization_profile"] == "MINIMIZE_MAKESPAN"
    assert second["optimization_profile"] == "MINIMIZE_DISTANCE"
    assert constraints["robot_limit"] == 2
    assert constraints["optimization_priority"] == "MINIMIZE_DISTANCE"
    assert constraints["optimization_weights"]["total_distance"] == 5.0
    assert constraints["optimization_weights"]["makespan"] == 1.0
    assert second["optimization_weights"]["total_distance"] == 5.0
    assert second["optimization_weights"]["makespan"] == 1.0


def test_same_conditions_inherit_task_scope_and_create_new_simulation(monkeypatch) -> None:
    install_conversation_fakes(monkeypatch)
    first = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-SCOPE-1",
            conversation_id="CONV-SCOPE",
            warehouse_id=1,
            text=(
                "W-003 작업을 로봇 최대 2대로 배정하고 전체 작업 완료시간을 "
                "최소화하는 가상 시뮬레이션을 실행해줘"
            ),
        )
    )
    second = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-SCOPE-2",
            conversation_id="CONV-SCOPE",
            warehouse_id=1,
            text=(
                "이번에는 같은 조건에서 이동거리를 최소화하는 "
                "가상 시뮬레이션을 실행해줘"
            ),
        )
    )
    loaded = conversation_trace(second, "conversation_context_loaded")
    resolved = conversation_trace(second, "conversation_context_resolved")
    selected = conversation_trace(second, "select_required_tasks")
    assigned_task_ids = {
        row["task_id"] for row in second["data"]["task_assignments"]
    }

    assert second["parent_command_id"] == first["command_id"]
    assert second["interpretation"]["target_task_ids"] == ["W-003"]
    assert second["interpretation"]["robot_limit"] == 2
    assert second["interpretation"]["optimization_priority"] == "MINIMIZE_DISTANCE"
    assert second["optimization_profile"] == "MINIMIZE_DISTANCE"
    assert second["simulation_id"] != first["simulation_id"]
    assert second["verification_decision"]["decision"] in {
        "PASS",
        "PASS_WITH_WARNING",
    }
    assert {"target_task_ids", "robot_limit"}.issubset(
        loaded["inherited_fields"]
    )
    assert {"target_task_ids", "robot_limit"}.issubset(
        resolved["inherited_fields"]
    )
    assert {"optimization_priority", "optimization_weights"}.issubset(
        resolved["overridden_fields"]
    )
    assert selected["task_count"] == 1
    assert assigned_task_ids == {"W3:move"}


def test_explicit_task_scope_overrides_inherited_task(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-TASK-OVERRIDE",
            warehouse_id=1,
            text="W-003 작업만 가상 시뮬레이션해줘",
        )
    )
    result = run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-TASK-OVERRIDE",
            warehouse_id=1,
            text="이번에는 W-002로 바꿔서 가상 시뮬레이션해줘",
        )
    )

    assert result["interpretation"]["target_task_ids"] == ["W-002"]
    assert services.postgres.sessions["CONV-TASK-OVERRIDE"][
        "resolved_constraints"
    ]["target_task_ids"] == ["W-002"]
    assert conversation_trace(result, "select_required_tasks")["task_count"] == 1
    assert {
        row["task_id"] for row in result["data"]["task_assignments"]
    } == {"W2:move"}


def test_explicit_all_tasks_clears_inherited_task_scope(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-TASK-CLEAR",
            warehouse_id=1,
            text="W-003 작업만 가상 시뮬레이션해줘",
        )
    )
    result = run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-TASK-CLEAR",
            warehouse_id=1,
            text="모든 작업으로 바꿔서 가상 시뮬레이션해줘",
        )
    )

    assert result["interpretation"]["target_task_ids"] == []
    assert "target_task_ids" not in services.postgres.sessions[
        "CONV-TASK-CLEAR"
    ]["resolved_constraints"]
    assert conversation_trace(result, "conversation_context_resolved")[
        "overridden_fields"
    ] == ["target_task_ids"]
    assert conversation_trace(result, "select_required_tasks")["task_count"] == 3


def test_same_conditions_without_previous_task_scope_requires_clarification(
    monkeypatch,
) -> None:
    install_conversation_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-MISSING-SCOPE",
            warehouse_id=1,
            text=(
                "같은 조건에서 이동거리를 최소화하는 "
                "가상 시뮬레이션을 실행해줘"
            ),
        )
    )
    trace_nodes = {row["node"] for row in result["trace"]}

    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["clarification"]["reason_code"] == "MISSING_INHERITED_TASK_SCOPE"
    assert "target_task_scope" in result["interpretation"]["missing_information"]
    assert "select_required_tasks" not in trace_nodes
    assert "build_optimization_problem" not in trace_nodes
    assert "simulation" not in trace_nodes


def test_same_conditions_inherit_other_scope_constraints() -> None:
    previous = {
        "target_task_ids": ["W-003"],
        "target_robot_ids": ["R-01"],
        "robot_limit": 2,
        "excluded_robot_ids": ["R-02"],
        "included_robot_ids": ["R-01"],
        "excluded_node_ids": [6],
        "excluded_edge_ids": ["10->11"],
        "fixed_robot_assignments": [
            {"task_id": "W-003", "robot_id": "R-01"}
        ],
    }
    current = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="이번에는 같은 조건에서 이동거리 우선으로 시뮬레이션해줘",
        execution_mode="SIMULATE_ONLY",
        optimization_priority="MINIMIZE_DISTANCE",
        summary="후속 명령",
    )
    resolved, inherited, overridden = apply_conversation_inheritance(
        current,
        previous,
        active_plan_version="PLAN-1",
        active_simulation_id="SIM-1",
    )

    assert resolved.target_task_ids == ["W-003"]
    assert resolved.target_robot_ids == ["R-01"]
    assert resolved.robot_limit == 2
    assert resolved.excluded_robot_ids == ["R-02"]
    assert resolved.included_robot_ids == ["R-01"]
    assert resolved.excluded_node_ids == [6]
    assert resolved.excluded_edge_ids == ["10->11"]
    assert [row.model_dump() for row in resolved.fixed_robot_assignments] == [
        {"task_id": "W-003", "robot_id": "R-01"}
    ]
    assert {"target_task_ids", "target_robot_ids", "robot_limit"}.issubset(
        inherited
    )
    assert {"optimization_priority", "optimization_weights"}.issubset(
        overridden
    )


def test_task_scope_is_not_inherited_across_conversations(monkeypatch) -> None:
    install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-SOURCE",
            warehouse_id=1,
            text="W-003 작업만 가상 시뮬레이션해줘",
        )
    )
    result = run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-OTHER",
            warehouse_id=1,
            text="같은 조건에서 이동거리 우선으로 시뮬레이션해줘",
        )
    )

    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["interpretation"]["target_task_ids"] == []


def test_previous_plan_reference_resolves_without_clarification(monkeypatch) -> None:
    install_conversation_fakes(monkeypatch)
    planned = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-REF-1",
            conversation_id="CONV-REF",
            warehouse_id=1,
            text="전체 작업을 계획해줘",
        )
    )
    simulated = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-REF-2",
            conversation_id="CONV-REF",
            warehouse_id=1,
            text="이 계획을 시뮬레이션해줘",
        )
    )
    assert planned["plan_version"]
    assert simulated["status"] == "SIMULATION_SUCCESS"
    assert simulated["parent_command_id"] == planned["command_id"]


def test_conversations_are_isolated_and_cross_warehouse_is_blocked(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-A",
            warehouse_id=1,
            text="R-02 제외하고 계획해줘",
        )
    )
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-B",
            warehouse_id=1,
            text="로봇 2대로 계획해줘",
        )
    )
    assert "excluded_robot_ids" not in services.postgres.sessions["CONV-B"][
        "resolved_constraints"
    ]
    with pytest.raises(ConversationAccessError):
        run_planning(
            NaturalLanguageCommand(
                conversation_id="CONV-A",
                warehouse_id=2,
                text="전체 작업을 계획해줘",
            )
        )


def test_execute_is_not_inherited() -> None:
    previous = {
        "robot_limit": 2,
        "execution_mode": "EXECUTE",
        "reset_approved": True,
    }
    current = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="계획",
        execution_mode="PLAN_ONLY",
        summary="계획",
    )
    resolved, inherited, _ = apply_conversation_inheritance(
        current,
        previous,
        active_plan_version=None,
        active_simulation_id=None,
    )
    assert resolved.execution_mode == "PLAN_ONLY"
    assert resolved.robot_limit == 2
    assert "execution_mode" not in inherited
    assert "reset_approved" not in inherited


def test_conversation_summary_is_compact() -> None:
    state = {
        "command": {"warehouse_id": 1},
        "interpretation": {"intent": "DAILY_PLAN", "execution_mode": "PLAN_ONLY"},
        "resolved_constraints": {"excluded_robot_ids": ["R-02"]},
        "report_data": {
            "total_distance": 27.8,
            "makespan_seconds": 35,
            "robot_routes": ["x" * 100_000],
        },
        "verification_decision": {"decision": "PASS"},
    }
    summary = compact_conversation_summary(state)
    encoded = json.dumps(summary, ensure_ascii=False)
    assert len(encoded) < 8192
    assert "robot_routes" not in encoded


def test_request_without_conversation_id_remains_compatible(monkeypatch) -> None:
    install_conversation_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(warehouse_id=1, text="전체 작업을 계획해줘")
    )
    assert result["status"] == "PLAN_READY"
    assert result["conversation_id"]


def test_conversation_api_returns_metadata_and_ordered_commands(monkeypatch) -> None:
    services = install_conversation_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            command_id="CMD-API-1",
            conversation_id="CONV-API",
            warehouse_id=1,
            text="전체 작업을 계획해줘",
        )
    )
    monkeypatch.setattr(api, "get_services", lambda: services)
    detail = api.get_conversation("CONV-API")
    commands = api.get_conversation_commands("CONV-API", limit=50, offset=0)
    assert detail["conversation"]["warehouse_id"] == 1
    assert detail["active_command_id"] == "CMD-API-1"
    assert commands["commands"][0]["sequence_number"] == 1

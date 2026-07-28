from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from app.models import CommandInterpretation, NaturalLanguageCommand
from app.planning import nodes
from app.planning.graph import run_planning
from app.services.base_plan import active_plan_base, base_plan_from_evidence
from app.services.conversation import apply_conversation_inheritance
from app.services.command_language import parse_deterministic_command
from tests.test_conversation_context import (
    ConversationPostgres,
    install_conversation_fakes,
)


class PersistedPlanPostgres(ConversationPostgres):
    def __init__(self) -> None:
        super().__init__()
        self.plan_evidence: dict[str, dict] = {}

    def snapshot(self, warehouse_id: int, _item_ids: list[str]) -> dict:
        assert warehouse_id == 1
        return {
            "inventory": [],
            "robots": [
                {
                    "robot_id": "R-01",
                    "node_id": 1,
                    "battery": 90,
                    "status": "IDLE",
                    "max_load": 100,
                    "current_load": 0,
                },
                {
                    "robot_id": "R-02",
                    "node_id": 4,
                    "battery": 85,
                    "status": "IDLE",
                    "max_load": 100,
                    "current_load": 0,
                },
            ],
            "works": [
                {
                    "work_id": "W-001",
                    "status": "NEW",
                    "source_node": 1,
                    "target_node": 2,
                    "priority": 1,
                },
                {
                    "work_id": "W-002",
                    "status": "NEW",
                    "source_node": 2,
                    "target_node": 3,
                    "priority": 2,
                },
                {
                    "work_id": "W-003",
                    "status": "NEW",
                    "source_node": 4,
                    "target_node": 3,
                    "priority": 1,
                },
            ],
            "work_dependencies": [],
            "work_schedule_constraints": [],
        }

    def record_simulation(self, state: dict) -> None:
        self.recorded += 1
        command_id = state["command"]["command_id"]
        output = {
            "interpretation": deepcopy(state.get("interpretation", {})),
            "verification_decision": deepcopy(
                state.get("verification_decision", {})
            ),
            "scope": deepcopy(state.get("scope", {})),
            "required_tasks": deepcopy(state.get("required_tasks", [])),
            "cuopt_plan": deepcopy(state.get("cuopt_plan", {})),
            "collision_plan": deepcopy(state.get("collision_plan", {})),
            "ready_task_ids": deepcopy(state.get("ready_task_ids", [])),
            "waiting_task_ids": deepcopy(state.get("waiting_task_ids", [])),
            "blocked_task_ids": deepcopy(state.get("blocked_task_ids", [])),
            "reference_time": state.get("optimization_problem", {}).get(
                "reference_time"
            ),
        }
        self.plan_evidence[command_id] = {
            "command_id": command_id,
            "plan_version": state.get("plan_version"),
            "output_payload": output,
        }

    def get_latest_command_plan_evidence(self, command_id: str):
        return deepcopy(self.plan_evidence.get(command_id))

    def get_plan_evidence_by_version(
        self, *, warehouse_id: int, conversation_id: str, plan_version: str
    ):
        if warehouse_id != self.sessions.get(conversation_id, {}).get(
            "warehouse_id"
        ):
            return None
        linked = {
            row["command_id"] for row in self.links.get(conversation_id, [])
        }
        return next(
            (
                deepcopy(row)
                for command_id, row in self.plan_evidence.items()
                if command_id in linked and row.get("plan_version") == plan_version
            ),
            None,
        )


def install_persisted_plan_fakes(monkeypatch):
    services = install_conversation_fakes(monkeypatch)
    services.postgres = PersistedPlanPostgres()
    return services


def schedule_by_id(result: dict) -> dict[str, dict]:
    return {
        str(row["task_id"]): row
        for row in result["optimization_plan"]["scheduled_tasks"]
    }


def test_simulated_parent_plan_is_preserved_when_urgent_task_is_inserted(
    monkeypatch,
) -> None:
    services = install_persisted_plan_fakes(monkeypatch)
    first = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-BASE-1",
            conversation_id="CONV-BASE",
            warehouse_id=1,
            text=(
                "내일 오전 9시부터 10시까지 W-001 작업을 처리하고, "
                "W-001이 완료되면 W-002 작업을 처리해줘. "
                "전체 계획을 가상 시뮬레이션해줘."
            ),
        )
    )
    second = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-BASE-2",
            conversation_id="CONV-BASE",
            warehouse_id=1,
            text=(
                "그 일정은 그대로 유지하고 급하게 W-003 작업을 지금 먼저 넣어줘. "
                "기존 작업은 중단하지 말고 이후 일정을 다시 맞춰서 "
                "가상 시뮬레이션해줘."
            ),
        )
    )

    before = schedule_by_id(first)
    after = schedule_by_id(second)
    before_daily = {row["task_id"]: row for row in first["daily_schedule"]}
    after_daily = {row["task_id"]: row for row in second["daily_schedule"]}
    assert first["status"] == "SIMULATION_SUCCESS"
    assert second["status"] == "SIMULATION_SUCCESS"
    assert second["parent_command_id"] == first["command_id"]
    assert second["plan_mode"] == "INSERT_TASK"
    assert second["supervisor_decision"]["plan_mode"] == "INSERT_TASK"
    assert second["base_plan_source"] == "PARENT_SIMULATION_PLAN"
    assert second["base_plan_version"] == first["plan_version"]
    assert second["active_plan_version"] is None
    assert second["base_plan_is_simulated"] is True
    assert second["original_plan_version"] == first["plan_version"]
    assert second["current_plan_version"] == second["plan_version"]
    assert second["plan_version"] != first["plan_version"]
    assert set(after) == {"W-001:move", "W-002:move", "W-003:move"}
    assert second["interpretation"]["target_task_ids"] == [
        "W-001",
        "W-002",
        "W-003",
    ]
    assert second["interpretation"]["task_dependencies"] == [
        {
            "predecessor_work_id": "W-001",
            "successor_work_id": "W-002",
            "dependency_type": "FINISH_TO_START",
            "lag_seconds": 0,
        }
    ]
    w1_window = next(
        row
        for row in second["interpretation"]["scheduled_task_constraints"]
        if row["work_id"] == "W-001"
    )
    assert w1_window["time_constraint_type"] == "HARD_WINDOW"
    for task_id in ("W-001:move", "W-002:move"):
        assert after[task_id]["robot_id"] == before[task_id]["robot_id"]
        assert after_daily[task_id]["planned_start_at"] == before_daily[task_id][
            "planned_start_at"
        ]
        assert after_daily[task_id]["planned_end_at"] == before_daily[task_id][
            "planned_end_at"
        ]
    insertion = second["insertion_result"]
    assert insertion["inserted_task_ids"] == ["W-003:move"]
    assert set(insertion["preserved_task_ids"]) == {
        "W-001:move",
        "W-002:move",
    }
    assert second["interpretation"]["preemption_policy"] == "NON_PREEMPTIVE"
    assert second["interpretation"]["insertion_policy"] == "URGENT"
    assert insertion["insertion_reason"] == "URGENT"
    assert insertion["replan_scope"] == "INSERT_TASK"
    assert insertion["previous_plan_version"] == first["plan_version"]
    assert insertion["new_plan_version"] == second["plan_version"]
    assert insertion["hard_window_violation"] is False
    assert second["data"]["tardiness"] == 0
    assert second["data"]["conflict_count"] == 0
    assert second["verification_decision"]["decision"] in {
        "PASS",
        "PASS_WITH_WARNING",
    }
    assert services.redis.activation_count == 0


def test_explicit_discard_phrase_creates_w003_only_plan(monkeypatch) -> None:
    install_persisted_plan_fakes(monkeypatch)
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-DISCARD",
            warehouse_id=1,
            text="W-001과 W-002 작업을 가상 시뮬레이션해줘",
        )
    )
    result = run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-DISCARD",
            warehouse_id=1,
            text="기존 일정은 취소하고 W-003으로 교체해서 가상 시뮬레이션해줘",
        )
    )
    assert result["base_plan_version"] is None
    assert result["plan_mode"] == "INITIAL_PLAN"
    assert set(schedule_by_id(result)) == {"W-003:move"}


def test_execute_does_not_implicitly_activate_simulated_base(monkeypatch) -> None:
    services = install_persisted_plan_fakes(monkeypatch)
    settings = nodes.get_settings()
    settings.robot_gateway_url = "http://mock-gateway"
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-EXEC-SAFE",
            warehouse_id=1,
            text="W-001 작업을 가상 시뮬레이션해줘",
        )
    )
    result = run_planning(
        NaturalLanguageCommand(
            conversation_id="CONV-EXEC-SAFE",
            warehouse_id=1,
            text="그 일정 그대로 W-003 긴급 작업을 추가해서 실제 실행해줘",
            requested_execution_mode="EXECUTE",
        )
    )
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["clarification"]["reason_code"] == (
        "SIMULATED_BASE_PLAN_REQUIRES_EXECUTION_CONFIRMATION"
    )
    assert services.redis.activation_count == 0
    assert "select_required_tasks" not in {
        row["node"] for row in result["trace"]
    }


def test_insert_inheritance_merges_targets_and_keeps_constraints() -> None:
    previous = {
        "target_task_ids": ["W-001", "W-002"],
        "excluded_robot_ids": ["R-03"],
        "excluded_node_ids": [9],
        "task_dependencies": [
            {
                "predecessor_work_id": "W-001",
                "successor_work_id": "W-002",
                "dependency_type": "FINISH_TO_START",
                "lag_seconds": 0,
            }
        ],
    }
    current = CommandInterpretation(
        command_kind="PLAN",
        intent="INSERT_TASK",
        objective="긴급 W-003 작업을 추가해줘",
        execution_mode="SIMULATE_ONLY",
        target_task_ids=["W-003"],
        insertion_policy="URGENT",
        priority="EMERGENCY",
        summary="insert",
    )
    resolved, inherited, overridden = apply_conversation_inheritance(
        current,
        previous,
        active_plan_version="P-1",
        active_simulation_id="S-1",
    )
    assert resolved.target_task_ids == ["W-001", "W-002", "W-003"]
    assert resolved.excluded_robot_ids == ["R-03"]
    assert resolved.excluded_node_ids == [9]
    assert len(resolved.task_dependencies) == 1
    assert inherited["target_task_ids"] == ["W-001", "W-002"]
    assert overridden["target_task_ids"] == ["W-003"]


def test_base_plan_evidence_rejects_failed_and_marks_simulation() -> None:
    failed = {
        "plan_version": "P-FAIL",
        "output_payload": {
            "verification_decision": {"decision": "FAIL"},
            "cuopt_plan": {"scheduled_tasks": [{"task_id": "W-001:move"}]},
        },
    }
    passed = {
        "command_id": "C-1",
        "plan_version": "P-SIM",
        "output_payload": {
            "verification_decision": {"decision": "PASS"},
            "interpretation": {"execution_mode": "SIMULATE_ONLY"},
            "cuopt_plan": {"scheduled_tasks": [{"task_id": "W-001:move"}]},
        },
    }
    assert base_plan_from_evidence(failed) is None
    base = base_plan_from_evidence(passed)
    assert base is not None
    assert base["base_plan_is_simulated"] is True
    assert base["candidate_plan"] is True


def test_active_plan_base_is_never_marked_simulated() -> None:
    base = active_plan_base(
        {
            "plan_version": "P-ACTIVE",
            "cuopt_plan": {"scheduled_tasks": [{"task_id": "W-001:move"}]},
        }
    )
    assert base is not None
    assert base["base_plan_is_simulated"] is False
    assert base["candidate_plan"] is False


@pytest.mark.parametrize("decision", ["PASS", "PASS_WITH_WARNING"])
def test_successful_verification_decisions_are_valid_base_plans(decision) -> None:
    base = base_plan_from_evidence(
        {
            "plan_version": f"P-{decision}",
            "output_payload": {
                "verification_decision": {"decision": decision},
                "interpretation": {"execution_mode": "PLAN_ONLY"},
                "cuopt_plan": {
                    "scheduled_tasks": [{"task_id": "W-001:move"}]
                },
            },
        }
    )
    assert base is not None
    assert base["candidate_plan"] is True
    assert base["base_plan_is_simulated"] is False


def test_base_plan_requires_at_least_one_scheduled_task() -> None:
    assert base_plan_from_evidence(
        {
            "plan_version": "P-EMPTY",
            "output_payload": {
                "verification_decision": {"decision": "PASS"},
                "cuopt_plan": {"scheduled_tasks": []},
            },
        }
    ) is None


def test_json_serialized_output_payload_can_be_reloaded_as_base() -> None:
    evidence = eligible_evidence("C-JSON", "P-JSON")
    evidence["output_payload"] = json.dumps(evidence["output_payload"])
    base = base_plan_from_evidence(evidence)
    assert base is not None
    assert base["plan_version"] == "P-JSON"


def test_active_plan_without_schedule_is_not_a_usable_base() -> None:
    assert active_plan_base(
        {"plan_version": "P-EMPTY", "cuopt_plan": {"scheduled_tasks": []}}
    ) is None


@pytest.mark.parametrize(
    "text",
    [
        "기존 일정은 취소하고 W-003만 계획해줘",
        "모든 작업을 빼고 W-003으로 교체해줘",
        "이전 계획을 폐기하고 W-003만 계획해줘",
    ],
)
def test_explicit_discard_phrases_do_not_merge_previous_targets(text) -> None:
    current = CommandInterpretation(
        command_kind="PLAN",
        intent="INSERT_TASK",
        objective=text,
        execution_mode="PLAN_ONLY",
        target_task_ids=["W-003"],
        summary="discard",
    )
    resolved, inherited, _ = apply_conversation_inheritance(
        current,
        {"target_task_ids": ["W-001", "W-002"], "robot_limit": 2},
        active_plan_version="P-OLD",
        active_simulation_id=None,
    )
    assert resolved.target_task_ids == ["W-003"]
    assert "target_task_ids" not in inherited


def test_explicit_plan_version_is_extracted_for_planning_command() -> None:
    parsed = parse_deterministic_command(
        "계획 버전 plan-123 기준으로 W-003 작업을 시뮬레이션해줘"
    )
    assert parsed.target_plan_versions == ["plan-123"]


def eligible_evidence(command_id: str, version: str, *, simulated=True) -> dict:
    return {
        "command_id": command_id,
        "plan_version": version,
        "output_payload": {
            "verification_decision": {"decision": "PASS"},
            "interpretation": {
                "execution_mode": "SIMULATE_ONLY" if simulated else "PLAN_ONLY"
            },
            "cuopt_plan": {
                "scheduled_tasks": [{"task_id": "W-001:move"}]
            },
        },
    }


class SelectionRepository:
    def __init__(self) -> None:
        self.by_command: dict[str, dict] = {}
        self.by_version: dict[str, dict] = {}
        self.commands: list[dict] = []

    def get_latest_command_plan_evidence(self, command_id):
        return deepcopy(self.by_command.get(command_id))

    def get_plan_evidence_by_version(self, **kwargs):
        return deepcopy(self.by_version.get(kwargs["plan_version"]))

    def list_conversation_commands(self, _conversation_id, **_kwargs):
        return deepcopy(self.commands)


def selection_interpretation(*, mode="SIMULATE_ONLY", versions=None):
    return CommandInterpretation(
        command_kind="EXECUTE" if mode == "EXECUTE" else "PLAN",
        intent="INSERT_TASK",
        objective="W-003 추가",
        execution_mode=mode,
        target_task_ids=["W-003"],
        target_plan_versions=versions or [],
        summary="selection",
    )


def selection_state(active_plan=None):
    return {
        "command": {
            "command_id": "CURRENT",
            "conversation_id": "CONV-1",
            "parent_command_id": "PARENT",
            "warehouse_id": 1,
        },
        "snapshot": {
            "redis": {
                "active_plan": active_plan,
                "active_plan_version": (
                    active_plan.get("plan_version") if active_plan else None
                ),
            }
        },
    }


def test_explicit_plan_version_precedes_parent_plan(monkeypatch) -> None:
    repository = SelectionRepository()
    repository.by_version["P-EXPLICIT"] = eligible_evidence(
        "EXPLICIT", "P-EXPLICIT"
    )
    repository.by_command["PARENT"] = eligible_evidence("PARENT", "P-PARENT")
    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )
    base, source = nodes._select_base_plan(
        selection_state(),
        selection_interpretation(versions=["P-EXPLICIT"]),
    )
    assert source == "EXPLICIT_PLAN_VERSION"
    assert base["plan_version"] == "P-EXPLICIT"


def test_latest_conversation_plan_is_used_when_parent_has_no_plan(
    monkeypatch,
) -> None:
    repository = SelectionRepository()
    repository.commands = [{"command_id": "OLDER"}, {"command_id": "LATEST"}]
    repository.by_command["LATEST"] = eligible_evidence("LATEST", "P-LATEST")
    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )
    base, source = nodes._select_base_plan(
        selection_state(), selection_interpretation()
    )
    assert source == "CONVERSATION_PLAN"
    assert base["plan_version"] == "P-LATEST"


def test_actual_active_plan_is_fallback_for_insert(monkeypatch) -> None:
    repository = SelectionRepository()
    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )
    active = {
        "plan_version": "P-ACTIVE",
        "cuopt_plan": {"scheduled_tasks": [{"task_id": "W-001:move"}]},
    }
    base, source = nodes._select_base_plan(
        selection_state(active), selection_interpretation()
    )
    assert source == "ACTIVE_PLAN"
    assert base["plan_version"] == "P-ACTIVE"
    assert base["base_plan_is_simulated"] is False


def test_execute_prefers_actual_active_plan_over_simulated_parent(
    monkeypatch,
) -> None:
    repository = SelectionRepository()
    repository.by_command["PARENT"] = eligible_evidence("PARENT", "P-SIM")
    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )
    active = {
        "plan_version": "P-ACTIVE",
        "cuopt_plan": {"scheduled_tasks": [{"task_id": "W-001:move"}]},
    }
    base, source = nodes._select_base_plan(
        selection_state(active), selection_interpretation(mode="EXECUTE")
    )
    assert source == "ACTIVE_PLAN"
    assert base["plan_version"] == "P-ACTIVE"


def test_explicit_plan_lookup_is_scoped_to_conversation_and_warehouse() -> None:
    repository = PersistedPlanPostgres()
    repository.create_or_get_conversation("CONV-A", 1)
    repository.create_or_get_conversation("CONV-B", 1)
    repository.links["CONV-A"] = [
        {
            "conversation_id": "CONV-A",
            "command_id": "CMD-A",
            "parent_command_id": None,
            "sequence_number": 1,
        }
    ]
    repository.plan_evidence["CMD-A"] = {
        "command_id": "CMD-A",
        "plan_version": "P-A",
        "output_payload": {},
    }
    assert repository.get_plan_evidence_by_version(
        warehouse_id=1, conversation_id="CONV-A", plan_version="P-A"
    ) is not None
    assert repository.get_plan_evidence_by_version(
        warehouse_id=1, conversation_id="CONV-B", plan_version="P-A"
    ) is None
    assert repository.get_plan_evidence_by_version(
        warehouse_id=2, conversation_id="CONV-A", plan_version="P-A"
    ) is None

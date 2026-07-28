from copy import deepcopy
from types import SimpleNamespace

from app.models import ScopeDecision, VerificationDecision
from app.planning import graph as graph_module
from app.planning import nodes


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        freeze_horizon_seconds=10,
        time_step_seconds=5,
    )


def _decision(
    decision: str,
    *,
    robot_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> dict:
    is_replan = decision in {"REPLAN_LOCAL", "REPLAN_GLOBAL"}
    return VerificationDecision(
        decision=decision,
        requires_replan=is_replan,
        replan_scope=(
            "LOCAL_REPLAN"
            if decision == "REPLAN_LOCAL"
            else "GLOBAL_REPLAN"
            if decision == "REPLAN_GLOBAL"
            else "NO_REPLAN"
        ),
        affected_robot_ids=robot_ids or [],
        affected_task_ids=task_ids or [],
        blocking_findings=["검증 실패"] if is_replan else [],
        evidence_ids=["verification:001"],
        summary="검증 결과",
    ).model_dump(mode="json")


def _state(decision: dict) -> dict:
    tasks = [
        {
            "task_id": "W1:move",
            "work_id": "W1",
            "action": "MOVE",
            "source_candidates": [1],
            "target_candidates": [2],
            "frozen": False,
        },
        {
            "task_id": "W2:move",
            "work_id": "W2",
            "action": "MOVE",
            "source_candidates": [2],
            "target_candidates": [3],
            "frozen": False,
        },
    ]
    return {
        "command": {"command_id": "C1", "warehouse_id": 1, "text": "plan"},
        "interpretation": {"execution_mode": "PLAN_ONLY"},
        "supervisor_decision": {"max_replan_attempts": 3},
        "snapshot": {
            "captured_at": "2026-07-21T00:00:00+00:00",
            "sql": {
                "robots": [
                    {"robot_id": "R1"},
                    {"robot_id": "R2"},
                ],
                "works": [
                    {"work_id": "W1", "status": "NEW"},
                    {"work_id": "W2", "status": "NEW"},
                ],
            },
            "redis": {
                "executing_task_ids": [],
                "active_plan": None,
            },
        },
        "scope": ScopeDecision(
            plan_mode="INITIAL_PLAN",
            freeze_horizon_seconds=10,
            optimization_goal="minimum cost",
            reason_summary="initial",
        ).model_dump(mode="json"),
        "required_tasks": tasks,
        "cuopt_plan": {
            "scheduled_tasks": [
                {"task_id": "W1:move", "robot_id": "R1"},
                {"task_id": "W2:move", "robot_id": "R2"},
            ]
        },
        "collision_plan": {
            "time_step_seconds": 5,
            "routes": [],
        },
        "verification_decision": decision,
        "verification_evidence": [
            {
                "evidence_id": "verification:001",
                "source": "DETERMINISTIC_VALIDATION",
                "severity": "BLOCKING",
                "code": "VERTEX_CONFLICT",
                "message": "검증 실패",
                "robot_ids": decision.get("affected_robot_ids", []),
                "task_ids": decision.get("affected_task_ids", []),
            }
        ],
        "replan_attempt": 0,
        "max_replan_attempts": 3,
        "replan_history": [],
        "repeated_failure_signatures": {},
        "plan_version": "P0",
        "original_plan_version": "P0",
        "current_plan_version": "P0",
    }


def test_local_replan_only_changes_affected_tasks(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(
        _decision(
            "REPLAN_LOCAL",
            robot_ids=["R2"],
            task_ids=["W2:move"],
        )
    )

    update = nodes.prepare_replan_node(state)

    assert update["replan_ready"] is True
    assert update["scope"]["plan_mode"] == "LOCAL_REPLAN"
    assert update["scope"]["changeable_task_ids"] == ["W2:move"]
    assert update["scope"]["fixed_task_ids"] == ["W1:move"]
    frozen = {row["task_id"]: row["frozen"] for row in update["required_tasks"]}
    assert frozen == {"W1:move": True, "W2:move": False}


def test_global_replan_changes_all_unprotected_tasks(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(_decision("REPLAN_GLOBAL", task_ids=["W1:move"]))

    update = nodes.prepare_replan_node(state)

    assert update["replan_ready"] is True
    assert update["scope"]["plan_mode"] == "GLOBAL_REPLAN"
    assert update["scope"]["changeable_task_ids"] == ["W1:move", "W2:move"]
    assert update["scope"]["fixed_task_ids"] == []


def test_replan_without_target_fails(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(
        _decision(
            "REPLAN_LOCAL",
            robot_ids=["UNKNOWN"],
            task_ids=["UNKNOWN:task"],
        )
    )

    update = nodes.prepare_replan_node(state)

    assert update["replan_ready"] is False
    assert update["verification_decision"]["decision"] == "FAIL"
    assert update.get("replan_attempt", 0) == 0


def test_executing_task_is_protected(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(_decision("REPLAN_GLOBAL", task_ids=["W1:move"]))
    state["snapshot"]["sql"]["works"][0]["status"] = "EXECUTING"
    state["snapshot"]["redis"]["executing_task_ids"] = ["W1:move"]

    update = nodes.prepare_replan_node(state)

    assert "W1:move" in update["scope"]["fixed_task_ids"]
    assert "W1:move" not in update["scope"]["changeable_task_ids"]
    assert update["replan_history"][0]["protected_task_ids"] == ["W1:move"]


def test_freeze_horizon_task_is_protected(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(_decision("REPLAN_GLOBAL", task_ids=["W1:move"]))
    state["snapshot"]["redis"]["active_plan"] = {
        "activated_at": "2026-07-21T00:00:00+00:00",
        "collision_plan": {
            "time_step_seconds": 5,
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": ["W1:move"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 1},
                        {"node_id": 2, "time_step": 2},
                    ],
                }
            ],
        },
    }

    update = nodes.prepare_replan_node(state)

    assert "W1:move" in update["scope"]["fixed_task_ids"]
    assert update["scope"]["changeable_task_ids"] == ["W2:move"]


def test_replan_preparation_does_not_mutate_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(
        _decision("REPLAN_LOCAL", robot_ids=["R1"], task_ids=["W1:move"])
    )
    before = deepcopy(state["snapshot"])

    nodes.prepare_replan_node(state)

    assert state["snapshot"] == before


def test_pass_and_fail_do_not_enter_replan() -> None:
    assert graph_module.after_verification(
        {"verification_decision": {"decision": "PASS"}, "replan_attempt": 0}
    ) == "persist"
    assert graph_module.after_verification(
        {"verification_decision": {"decision": "FAIL"}, "replan_attempt": 0}
    ) == "persist"


def test_replan_maximum_is_capped_at_three(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", _settings)
    state = _state(
        _decision("REPLAN_LOCAL", robot_ids=["R1"], task_ids=["W1:move"])
    )
    state["max_replan_attempts"] = 99
    state["supervisor_decision"]["max_replan_attempts"] = 99

    update = nodes.prepare_replan_node(state)

    assert update["max_replan_attempts"] == 3

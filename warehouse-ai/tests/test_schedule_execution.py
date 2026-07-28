from types import SimpleNamespace

import app.execution.graph as execution
from app.models import RobotEvent


class ScheduleRedis:
    def __init__(self) -> None:
        self.event_count = 0

    def update_from_event(self, _event) -> None:
        self.event_count += 1

    def emit_replan_required(self, _event) -> str:
        return "STREAM-1"

    def live_snapshot(self, _warehouse_id):
        return {"active_plan": None, "active_plan_version": None}


class SchedulePostgres:
    def __init__(self, transition):
        self.transition = transition
        self.completion_count = 0
        self.failure_count = 0
        self.transition_count = 0
        self.started_count = 0
        self.duplicate = False

    def commit_completion(self, _event):
        self.completion_count += 1
        return {"committed": True, "idempotent_replay": self.duplicate}

    def commit_failure(self, _event):
        self.failure_count += 1
        return {"committed": True, "idempotent_replay": self.duplicate}

    def record_task_started(self, _event):
        self.started_count += 1
        return {"committed": True, "idempotent_replay": self.duplicate}

    def transition_successors(self, *_args, **_kwargs):
        self.transition_count += 1
        return self.transition


def install(monkeypatch, transition):
    services = SimpleNamespace(
        redis=ScheduleRedis(), postgres=SchedulePostgres(transition)
    )
    monkeypatch.setattr(execution, "get_services", lambda: services)
    monkeypatch.setattr(
        execution,
        "get_settings",
        lambda: SimpleNamespace(
            robot_gateway_url="", request_timeout_seconds=1
        ),
    )
    return services


def event(event_type: str, event_id: str = "E1") -> RobotEvent:
    return RobotEvent(
        event_id=event_id,
        warehouse_id=1,
        robot_id="R1",
        work_id="W-001",
        task_id="W-001:move",
        event_type=event_type,
        execution_context="REAL",
    )


def test_completion_unlocks_successor(monkeypatch) -> None:
    services = install(
        monkeypatch,
        {"ready": ["W-002"], "waiting": [], "blocked": []},
    )
    result = execution.handle_robot_event(event("TASK_COMPLETED"))
    assert result["schedule_transition"]["ready"] == ["W-002"]
    assert result["final_status"] == "SUCCESSOR_READY"
    assert result["trace"][-1]["node"] == "successor_unlocked"
    assert services.postgres.transition_count == 1


def test_duplicate_completion_does_not_unlock_twice(monkeypatch) -> None:
    services = install(
        monkeypatch,
        {"ready": ["W-002"], "waiting": [], "blocked": []},
    )
    services.postgres.duplicate = True
    result = execution.handle_robot_event(event("TASK_COMPLETED"))
    assert result["schedule_transition"]["duplicate"] is True
    assert services.postgres.transition_count == 0


def test_failure_blocks_direct_successor_and_requests_replan(monkeypatch) -> None:
    services = install(
        monkeypatch,
        {"ready": [], "waiting": [], "blocked": ["W-002"]},
    )
    result = execution.handle_robot_event(event("TASK_FAILED"))
    assert result["schedule_transition"]["blocked"] == ["W-002"]
    assert result["final_status"] == "REPLAN_REQUIRED"
    assert services.postgres.failure_count == 1


def test_failure_keeps_independent_ready_work_available(monkeypatch) -> None:
    services = install(
        monkeypatch,
        {"ready": ["W-003"], "waiting": [], "blocked": ["W-002"]},
    )
    result = execution.handle_robot_event(event("TASK_FAILED"))
    assert result["schedule_transition"]["ready"] == ["W-003"]
    assert result["schedule_transition"]["blocked"] == ["W-002"]
    assert result["successor_dispatch_result"]["status"] == "READY_NOT_DISPATCHED"
    assert result["final_status"] == "REPLAN_REQUIRED"
    assert {row["node"] for row in result["trace"]} >= {
        "successor_unlocked",
        "successor_blocked",
    }
    assert services.postgres.failure_count == 1


def test_started_event_is_persisted_without_successor_transition(monkeypatch) -> None:
    services = install(
        monkeypatch,
        {"ready": [], "waiting": [], "blocked": []},
    )
    result = execution.handle_robot_event(event("TASK_STARTED"))
    assert result["final_status"] == "EXECUTING"
    assert services.postgres.started_count == 1
    assert services.postgres.transition_count == 0

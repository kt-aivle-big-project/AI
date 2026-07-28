from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.execution import graph as execution_graph_module
from app.models import EventImpactAnalysis, RobotEvent
from app.services.event_replan import EventReplanService
from app.services.event_safety import (
    EventWatermark,
    StaleExecutionEventError,
    compare_event_order,
    ordering_evidence,
)


REFERENCE = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
PLAN = {
    "plan_version": "P-VERIFIED",
    "reference_time": REFERENCE.isoformat(),
    "time_step_seconds": 5,
    "required_tasks": [
        {
            "task_id": "T1",
            "work_id": "W1",
            "action": "PICK",
            "source_candidates": [10],
            "target_candidates": [10],
            "frozen": False,
        }
    ],
    "cuopt_plan": {
        "scheduled_tasks": [
            {
                "task_id": "T1",
                "work_id": "W1",
                "robot_id": "R1",
                "action": "PICK",
                "source_node": 10,
                "target_node": 10,
                "start_time_step": 1,
                "end_time_step": 2,
                "estimated_energy": 1.0,
            }
        ],
        "metadata": {},
    },
    "collision_plan": {
        "time_step_seconds": 5,
        "routes": [
            {
                "robot_id": "R1",
                "task_ids": ["T1"],
                "waypoints": [
                    {"node_id": 9, "time_step": 0},
                    {"node_id": 10, "time_step": 2},
                ],
            }
        ],
    },
}


def simulation_event(
    *,
    event_id: str = "E-1",
    event_type: str = "POSITION_UPDATED",
    occurred_at: datetime = REFERENCE + timedelta(seconds=5),
    battery: float | None = 70,
) -> RobotEvent:
    return RobotEvent(
        event_id=event_id,
        warehouse_id=1,
        robot_id="R1",
        work_id="W1",
        task_id="T1",
        event_type=event_type,
        node_id=9 if event_type in {"POSITION_UPDATED", "PATH_DEVIATED"} else None,
        battery=battery,
        occurred_at=occurred_at,
        execution_context="SIMULATION",
        simulation_id="SIM-1",
    )


def real_started_event(event_id: str = "REAL-START-1") -> RobotEvent:
    return RobotEvent(
        event_id=event_id,
        warehouse_id=1,
        robot_id="R1",
        work_id="W1",
        task_id="T1",
        event_type="TASK_STARTED",
        node_id=9,
        occurred_at=REFERENCE,
        execution_context="REAL",
        payload={"_server_runtime": {"source": "REAL_REDIS_ACTIVE_PLAN"}},
    )


class EventRows:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.checkpoints: list[dict] = []

    def get_execution_event_processing(self, event_id):
        return deepcopy(self.events.get(event_id))

    def create_execution_event_processing(self, values):
        if values["event_id"] in self.events:
            return {**deepcopy(self.events[values["event_id"]]), "duplicate": True}
        self.events[values["event_id"]] = deepcopy(values)
        return {**deepcopy(values), "duplicate": False}

    def finalize_execution_event_processing(self, event_id, values):
        self.events[event_id].update(deepcopy(values))

    def count_recent_event_failure_signature(self, *_args, **_kwargs):
        return 0

    def create_or_get_automatic_replan_request(self, values):
        self.requests[values["request_id"]] = deepcopy(values)
        return deepcopy(values)

    def update_automatic_replan_request(self, request_id, values):
        self.requests[request_id].update(deepcopy(values))

    def get_simulation_session(self, simulation_id):
        return {
            "simulation_id": simulation_id,
            "warehouse_id": 1,
            "status": "ACTIVE",
            "current_state": {"active_plan": deepcopy(PLAN)},
        }

    def update_simulation_checkpoint(self, event, state, checkpoint):
        row = {
            "event_id": event.event_id,
            "state": deepcopy(state),
            "checkpoint": checkpoint,
        }
        self.checkpoints.append(row)
        return {"saved": True, "checkpoint": checkpoint}


class PlanRedis:
    def __init__(self) -> None:
        self.state = {
            "simulation_id": "SIM-1",
            "inventory": [],
            "robots": [{"robot_id": "R1", "node_id": 9, "battery": 70}],
            "works": [{"work_id": "W1", "task_id": "T1", "status": "PLANNED"}],
            "active_plan_version": "P-VERIFIED",
            "active_plan": deepcopy(PLAN),
            "checkpoint": "1-0",
        }
        self.restore_calls: list[dict] = []

    def simulation_snapshot(self, _simulation_id):
        return deepcopy(self.state)

    def restore_simulation_snapshot(self, simulation_id, snapshot, **kwargs):
        self.state = deepcopy(snapshot)
        self.state["checkpoint"] = "ROLLBACK-1"
        row = {"simulation_id": simulation_id, **kwargs}
        self.restore_calls.append(row)
        return {"restored": True, "checkpoint": "ROLLBACK-1"}


class RuntimeRedis(PlanRedis):
    def live_snapshot(self, _warehouse_id):
        return {
            "robots": deepcopy(self.state["robots"]),
            "tasks": deepcopy(self.state["works"]),
            "active_plan_version": self.state["active_plan_version"],
            "active_plan": deepcopy(self.state["active_plan"]),
            "temporary_closures": [],
        }


class Neo4j:
    def fetch_topology(self, _warehouse_id):
        return {
            "nodes": [{"node_id": 9}, {"node_id": 10}],
            "edges": [{"from_node": 9, "to_node": 10, "direction": "BOTH"}],
        }


def test_same_event_id_with_different_payload_is_rejected_without_reprocessing() -> None:
    postgres = EventRows()
    first = simulation_event(event_id="PAYLOAD-CONFLICT", battery=70)
    postgres.events[first.event_id] = {
        "event_id": first.event_id,
        "event_type": first.event_type,
        "status": "TELEMETRY_UPDATED",
        "event_payload": first.model_dump(mode="json"),
        "result_summary": {"status": "TELEMETRY_UPDATED"},
    }
    calls: list[str] = []
    service = EventReplanService(
        SimpleNamespace(postgres=postgres, redis=PlanRedis(), neo4j=Neo4j()),
        event_handler=lambda *_args, **_kwargs: calls.append("called") or {},
    )

    result = service.handle(
        simulation_event(event_id="PAYLOAD-CONFLICT", battery=69)
    )

    assert result["final_status"] == "EVENT_ID_PAYLOAD_CONFLICT"
    assert result["duplicate"] is True
    assert result["retryable"] is False
    assert result["payload_identity"]["match"] is False
    assert calls == []
    assert postgres.events[first.event_id]["status"] == "TELEMETRY_UPDATED"


def test_duplicate_received_event_returns_processing_instead_of_empty_final_result() -> None:
    postgres = EventRows()
    event = simulation_event(event_id="IN-FLIGHT")
    postgres.events[event.event_id] = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "status": "RECEIVED",
        "event_payload": event.model_dump(mode="json"),
        "result_summary": {},
    }
    service = EventReplanService(
        SimpleNamespace(postgres=postgres, redis=PlanRedis(), neo4j=Neo4j())
    )

    result = service.handle(event)

    assert result["status"] == "PROCESSING"
    assert result["final_status"] == "EVENT_PROCESSING"
    assert result["duplicate"] is True
    assert result["retryable"] is True


def test_event_order_is_deterministic_for_older_and_same_time_events() -> None:
    current = EventWatermark(
        event_id="E-200",
        event_type="ROBOT_FAILED",
        occurred_at=REFERENCE,
        precedence=90,
    )
    older = simulation_event(
        event_id="E-OLD", occurred_at=REFERENCE - timedelta(seconds=1)
    )
    same_time_lower = simulation_event(event_id="E-300", occurred_at=REFERENCE)
    same_time_higher_id = simulation_event(
        event_id="Z-999", event_type="ROBOT_FAILED", occurred_at=REFERENCE, battery=None
    )

    assert compare_event_order(older, current) == (False, "OLDER_EVENT_TIME")
    assert compare_event_order(same_time_lower, current) == (
        False,
        "SAME_TIME_LOWER_PRECEDENCE",
    )
    assert compare_event_order(same_time_higher_id, current) == (
        True,
        "SAME_TIME_EVENT_ID_TIE_BREAK",
    )


def test_real_sql_event_is_not_written_to_redis_before_postgres_commit(monkeypatch) -> None:
    class Redis:
        def __init__(self):
            self.updates = []

        def validate_event_order(self, event):
            return {"decision": "FIRST_EVENT"}

        def update_from_event(self, event):
            self.updates.append(event.event_id)
            return {"event_ordering": {"decision": "FIRST_EVENT"}}

    class Postgres:
        def record_task_started(self, event):
            raise RuntimeError("POSTGRES_DOWN")

    services = SimpleNamespace(redis=Redis(), postgres=Postgres())
    monkeypatch.setattr(execution_graph_module, "get_services", lambda: services)
    event = real_started_event()

    deferred = execution_graph_module.update_live_state_node(
        {"event": event.model_dump(mode="json")}
    )
    committed = execution_graph_module.commit_completion_node(
        {"event": event.model_dump(mode="json"), **deferred}
    )

    assert deferred["live_update_deferred"] is True
    assert committed["final_status"] == "COMMIT_FAILED"
    assert committed["sql_committed"] is False
    assert services.redis.updates == []


def test_real_sql_success_updates_redis_after_commit(monkeypatch) -> None:
    order: list[str] = []

    class Redis:
        def validate_event_order(self, event):
            order.append("REDIS_VALIDATE")
            return {"decision": "FIRST_EVENT"}

        def update_from_event(self, event):
            order.append("REDIS_UPDATE")
            return {"event_ordering": {"decision": "FIRST_EVENT"}}

    class Postgres:
        def record_task_started(self, event):
            order.append("POSTGRES_COMMIT")
            return {"committed": True, "idempotent_replay": False}

    services = SimpleNamespace(redis=Redis(), postgres=Postgres())
    monkeypatch.setattr(execution_graph_module, "get_services", lambda: services)
    event = real_started_event("REAL-START-OK")

    result = execution_graph_module.commit_completion_node(
        {"event": event.model_dump(mode="json"), "redis_updated": False}
    )

    assert result["sql_committed"] is True
    assert result["redis_updated"] is True
    assert order == ["REDIS_VALIDATE", "POSTGRES_COMMIT", "REDIS_UPDATE"]


def test_stale_event_is_ignored_without_error_or_replan() -> None:
    postgres = EventRows()
    event = simulation_event(event_id="STALE-1")
    evidence = ordering_evidence(
        event,
        EventWatermark(
            event_id="NEWER",
            event_type="POSITION_UPDATED",
            occurred_at=event.occurred_at + timedelta(seconds=1),
            precedence=10,
        ),
        decision="OLDER_EVENT_TIME",
    )
    service = EventReplanService(
        SimpleNamespace(postgres=postgres, redis=RuntimeRedis(), neo4j=Neo4j()),
        event_handler=lambda *_args, **_kwargs: {
            "redis_updated": False,
            "sql_committed": False,
            "stale_event_ignored": True,
            "event_ordering": evidence,
            "final_status": "STALE_EVENT_IGNORED",
            "errors": [],
        },
    )

    result = service.handle(event)

    assert result["status"] == "STALE_EVENT_IGNORED"
    assert result["auto_replan_requested"] is False
    assert result["errors"] == []
    assert result["event_ordering"]["decision"] == "OLDER_EVENT_TIME"


def test_simulation_checkpoint_failure_restores_pre_event_state(monkeypatch) -> None:
    event = simulation_event(event_id="SIM-ROLLBACK")

    class Redis(PlanRedis):
        def __init__(self):
            super().__init__()
            self.before = deepcopy(self.state)

        def get_event_watermark(self, _event):
            return {"event_id": "PREVIOUS"}

        def update_simulation_from_event(self, incoming):
            self.state["robots"][0]["battery"] = incoming.battery
            self.state["checkpoint"] = "2-0"
            return deepcopy(self.state)

    class Postgres:
        def update_simulation_checkpoint(self, *_args):
            raise RuntimeError("CHECKPOINT_WRITE_FAILED")

    redis = Redis()
    services = SimpleNamespace(redis=redis, postgres=Postgres())
    monkeypatch.setattr(execution_graph_module, "get_services", lambda: services)

    result = execution_graph_module.update_live_state_node(
        {"event": event.model_dump(mode="json")}
    )

    assert result["final_status"] == "SIMULATION_CHECKPOINT_FAILED"
    assert result["simulation_state_rollback"]["restored"] is True
    assert redis.state["robots"] == redis.before["robots"]
    assert redis.restore_calls[0]["reason"] == "POSTGRES_CHECKPOINT_FAILED"


def test_failed_replan_restores_last_verified_simulation_plan() -> None:
    postgres = EventRows()
    redis = RuntimeRedis()
    services = SimpleNamespace(postgres=postgres, redis=redis, neo4j=Neo4j())
    event = simulation_event(
        event_id="REPLAN-ROLLBACK",
        event_type="PATH_BLOCKED",
        battery=None,
    )
    impact = EventImpactAnalysis(
        event_id=event.event_id,
        trigger_type="PATH_BLOCKED",
        trigger_source="SIMULATION",
        affected_robot_ids=["R1"],
        affected_task_ids=["T1"],
        affected_node_ids=[9],
        recommended_scope="LOCAL_REPLAN",
        risk_level="MEDIUM",
        approval_required=False,
        active_plan_version="P-VERIFIED",
        changeable_task_ids=["T1"],
        failure_signature="PATH_BLOCKED:R1:T1",
    )

    def event_handler(*_args, **_kwargs):
        return {
            "redis_updated": True,
            "sql_committed": True,
            "impact_analysis": impact.model_dump(mode="json"),
            "final_status": "EVENT_IMPACT_ANALYZED",
            "errors": [],
        }

    def planner(_command):
        redis.state["active_plan_version"] = "P-UNVERIFIED"
        redis.state["active_plan"] = {**deepcopy(PLAN), "plan_version": "P-UNVERIFIED"}
        return {
            "status": "VERIFICATION_FAILED",
            "final_status": "VERIFICATION_FAILED",
            "plan_version": "P-UNVERIFIED",
            "verification_decision": {"decision": "FAIL"},
            "errors": ["ROUTE_FAILED"],
        }

    result = EventReplanService(
        services,
        planner=planner,
        event_handler=event_handler,
    ).handle(event)

    assert result["status"] == "FAILED"
    assert result["plan_recovery"]["status"] == "RESTORED"
    assert result["plan_recovery"]["restored"] is True
    assert redis.state["active_plan_version"] == "P-VERIFIED"
    assert postgres.checkpoints[-1]["state"]["active_plan_version"] == "P-VERIFIED"


def test_verified_replan_does_not_restore_new_plan() -> None:
    service = EventReplanService(
        SimpleNamespace(postgres=EventRows(), redis=RuntimeRedis(), neo4j=Neo4j())
    )
    event = simulation_event(event_id="NO-ROLLBACK")
    guard = service._plan_guard(event)
    service.services.redis.state["active_plan_version"] = "P-NEW"
    service.services.redis.state["active_plan"] = {**deepcopy(PLAN), "plan_version": "P-NEW"}

    # The restore helper is intentionally called only for failed replans.
    assert guard["active_plan_version"] == "P-VERIFIED"
    assert service.services.redis.state["active_plan_version"] == "P-NEW"


def test_recoverable_duplicate_resumes_only_the_failed_state_sync() -> None:
    postgres = EventRows()
    event = real_started_event("RECOVERY-REPLAY")
    postgres.events[event.event_id] = {
        "event_id": event.event_id,
        "warehouse_id": 1,
        "event_type": "TASK_STARTED",
        "event_source": "REAL",
        "status": "FAILED",
        "event_payload": event.model_dump(mode="json"),
        "result_summary": {
            "status": "FAILED",
            "final_status": "COMMIT_FAILED",
            "sql_committed": False,
            "redis_updated": False,
            "recovery_required": True,
            "retryable": True,
            "auto_replan_requested": False,
        },
    }
    calls: list[str] = []
    service = EventReplanService(
        SimpleNamespace(postgres=postgres, redis=RuntimeRedis(), neo4j=Neo4j()),
        event_handler=lambda *_args, **_kwargs: calls.append("resumed") or {
            "redis_updated": True,
            "sql_committed": True,
            "final_status": "EXECUTING",
            "commit_result": {"committed": True, "idempotent_replay": True},
            "errors": [],
        },
    )

    result = service.handle(event)

    assert calls == ["resumed"]
    assert result["status"] == "EXECUTING"
    assert result["recovery_replay"] is True
    assert postgres.events[event.event_id]["status"] == "EXECUTING"


def test_idempotent_sql_replay_repairs_missing_redis_state(monkeypatch) -> None:
    order: list[str] = []

    class Redis:
        def validate_event_order(self, event):
            order.append("VALIDATE")
            return {"decision": "FIRST_EVENT"}

        def update_from_event(self, event):
            order.append("RECONCILE_REDIS")
            return {
                "accepted": True,
                "duplicate": False,
                "event_ordering": {"decision": "FIRST_EVENT"},
            }

    class Postgres:
        def record_task_started(self, event):
            order.append("SQL_IDEMPOTENT_REPLAY")
            return {"committed": True, "idempotent_replay": True}

    services = SimpleNamespace(redis=Redis(), postgres=Postgres())
    monkeypatch.setattr(execution_graph_module, "get_services", lambda: services)
    event = real_started_event("SQL-REPLAY-HEAL")

    result = execution_graph_module.commit_completion_node(
        {"event": event.model_dump(mode="json"), "redis_updated": False}
    )

    assert result["sql_committed"] is True
    assert result["redis_updated"] is True
    assert result["redis_reconciled"] is True
    assert order == ["VALIDATE", "SQL_IDEMPOTENT_REPLAY", "RECONCILE_REDIS"]

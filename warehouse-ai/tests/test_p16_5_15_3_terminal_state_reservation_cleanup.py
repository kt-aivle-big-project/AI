from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import (
    ExecutionDispatchRetryRequest,
    PlanExecutionApprovalRequest,
    RobotCommandAckRequest,
)
from app.services.execution_delivery import (
    ExecutionConflictError,
    ExecutionDeliveryService,
    ExecutionRetryExhaustedError,
)


class MemoryPostgres:
    def __init__(self) -> None:
        self.approvals: dict[str, dict] = {}
        self.dispatches: dict[str, dict] = {}

    def approve_execution_plan(self, values: dict) -> dict:
        self.approvals.setdefault(values["plan_version"], deepcopy(values))
        return deepcopy(self.approvals[values["plan_version"]])

    def get_execution_plan_approval(self, plan_version: str):
        row = self.approvals.get(plan_version)
        return deepcopy(row) if row else None

    def create_or_get_execution_dispatch(self, values: dict):
        row = self.dispatches.get(values["dispatch_id"])
        if row is not None:
            return deepcopy(row), False
        self.dispatches[values["dispatch_id"]] = deepcopy(values)
        return deepcopy(values), True

    def get_execution_dispatch(self, dispatch_id: str):
        row = self.dispatches.get(dispatch_id)
        return deepcopy(row) if row else None

    def update_execution_dispatch(self, dispatch_id: str, values: dict) -> None:
        self.dispatches[dispatch_id].update(deepcopy(values))


class MemoryRedis:
    def __init__(self, plan: dict) -> None:
        self.plan = deepcopy(plan)
        self.active_plan_version = str(plan["plan_version"])
        self.rollback_calls: list[tuple] = []
        self.release_calls: list[dict] = []

    def live_snapshot(self, warehouse_id: int) -> dict:
        assert warehouse_id == self.plan["warehouse_id"]
        return {
            "active_plan_version": self.active_plan_version,
            "active_plan": deepcopy(self.plan),
        }

    def rollback_plan_activation(
        self, warehouse_id: int, failed_plan_version: str, previous: str | None
    ) -> bool:
        self.rollback_calls.append((warehouse_id, failed_plan_version, previous))
        if self.active_plan_version != failed_plan_version:
            return False
        self.active_plan_version = previous
        return True

    def update_inventory_reservations(self, warehouse_id: int, **kwargs):
        self.release_calls.append({"warehouse_id": warehouse_id, **kwargs})
        return [
            {
                "reservation_id": "RES-1",
                "warehouse_id": warehouse_id,
                "plan_version": kwargs["plan_version"],
                "status": kwargs["status"],
            }
        ]


class Gateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def dispatch_identity(self, plan_version: str, batches: list[dict]) -> dict:
        return {
            "dispatch_id": "GW-15-3",
            "idempotency_key": "GW-15-3",
            "payload_fingerprint": "fp",
        }

    def dispatch(self, plan_version: str, batches: list[dict]) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("gateway timeout")
        return {
            "accepted": True,
            "status": "DISPATCH_ACCEPTED",
            "dispatch_id": "GW-15-3",
            "plan_version": plan_version,
        }


def plan_payload() -> dict:
    return {
        "plan_version": "PLAN-15-3",
        "command_id": "CMD-15-3",
        "warehouse_id": 1,
        "scope": {"plan_mode": "INITIAL_PLAN"},
        "required_tasks": [],
        "cuopt_plan": {"scheduled_tasks": []},
        "collision_plan": {"routes": []},
        "inventory_operations": [],
        "charger_node_ids": [],
        "execution_task_dependencies": [],
        "scheduled_task_constraints": [],
        "ready_task_ids": ["W1:move"],
        "waiting_task_ids": [],
        "blocked_task_ids": [],
    }


def batches() -> list[dict]:
    return [
        {
            "plan_version": "PLAN-15-3",
            "warehouse_id": 1,
            "robot_id": "R1",
            "command_count": 2,
            "commands": [
                {
                    "command_id": "C1",
                    "sequence": 1,
                    "plan_version": "PLAN-15-3",
                    "warehouse_id": 1,
                    "robot_id": "R1",
                    "task_id": None,
                    "work_id": None,
                    "action": "START",
                    "node_id": 10,
                    "time_step": 0,
                    "time_step_seconds": 5,
                    "payload": {},
                },
                {
                    "command_id": "C2",
                    "sequence": 2,
                    "plan_version": "PLAN-15-3",
                    "warehouse_id": 1,
                    "robot_id": "R1",
                    "task_id": "W1:move",
                    "work_id": "W1",
                    "action": "MOVE",
                    "node_id": 20,
                    "time_step": 1,
                    "time_step_seconds": 5,
                    "payload": {},
                },
            ],
        }
    ]


def install(*, fail: bool = False, max_attempts: int = 2):
    plan = plan_payload()
    postgres = MemoryPostgres()
    redis = MemoryRedis(plan)
    gateway = Gateway(fail=fail)
    service = ExecutionDeliveryService(
        SimpleNamespace(postgres=postgres, redis=redis), gateway=gateway
    )
    service.approve_plan(
        plan_version=plan["plan_version"],
        command_id=plan["command_id"],
        warehouse_id=1,
        verification_decision="PASS",
        plan_payload=plan,
        request=PlanExecutionApprovalRequest(
            warehouse_id=1,
            actor_id="verifier",
            reason="verified",
        ),
    )
    result = service.dispatch(
        plan_version=plan["plan_version"],
        warehouse_id=1,
        command_id=plan["command_id"],
        batches=batches(),
        previous_active_plan_version="PLAN-OLD",
        max_attempts=max_attempts,
    )
    return service, postgres, redis, gateway, result


def test_ack_before_command_sent_is_rejected() -> None:
    service, postgres, _redis, _gateway, result = install()
    row = postgres.get_execution_dispatch(result["dispatch_id"])
    sent_at = datetime.fromisoformat(
        row["command_states"][0]["sent_at"].replace("Z", "+00:00")
    )
    with pytest.raises(ExecutionConflictError, match="ACK_BEFORE_COMMAND_SENT"):
        service.acknowledge(
            result["dispatch_id"],
            RobotCommandAckRequest(
                ack_id="ACK-EARLY",
                plan_version="PLAN-15-3",
                robot_id="R1",
                command_id="C1",
                sequence=1,
                status="ACKED",
                occurred_at=sent_at - timedelta(seconds=30),
            ),
        )


def test_retry_exhaustion_terminalizes_commands_persists_rollback_and_releases() -> None:
    service, postgres, redis, _gateway, first = install(fail=True)
    assert first["status"] == "DISPATCH_TIMEOUT"
    with pytest.raises(ExecutionRetryExhaustedError):
        service.retry(
            first["dispatch_id"],
            ExecutionDispatchRetryRequest(actor_id="system", reason="retry"),
        )
    row = postgres.get_execution_dispatch(first["dispatch_id"])
    assert row["status"] == "RETRY_EXHAUSTED"
    assert row["attempt_count"] == 2
    assert {state["status"] for state in row["command_states"]} == {
        "DISPATCH_FAILED"
    }
    assert {
        state["error_code"] for state in row["command_states"]
    } == {"DISPATCH_RETRY_EXHAUSTED"}
    assert row["result_summary"]["retryable"] is False
    assert row["result_summary"]["rollback"]["restored"] is True
    release = row["result_summary"]["rollback"][
        "inventory_reservation_release"
    ]
    assert release["status"] == "RELEASED"
    assert release["released_count"] == 1
    assert redis.active_plan_version == "PLAN-OLD"
    assert redis.release_calls[0]["plan_version"] == "PLAN-15-3"
    assert redis.release_calls[0]["status"] == "RELEASED"


def test_terminal_retry_exhausted_dispatch_rejects_late_ack() -> None:
    service, postgres, _redis, _gateway, first = install(fail=True)
    with pytest.raises(ExecutionRetryExhaustedError):
        service.retry(
            first["dispatch_id"],
            ExecutionDispatchRetryRequest(actor_id="system", reason="retry"),
        )
    row = postgres.get_execution_dispatch(first["dispatch_id"])
    assert row["status"] == "RETRY_EXHAUSTED"
    with pytest.raises(ExecutionConflictError, match="ACK_DISPATCH_NOT_ACTIVE"):
        service.acknowledge(
            first["dispatch_id"],
            RobotCommandAckRequest(
                ack_id="ACK-LATE",
                plan_version="PLAN-15-3",
                robot_id="R1",
                command_id="C1",
                sequence=1,
                status="ACKED",
                occurred_at=datetime.now(UTC),
            ),
        )

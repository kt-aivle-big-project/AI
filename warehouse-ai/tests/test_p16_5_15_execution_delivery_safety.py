from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.models import (
    ExecutionDispatchCancelRequest,
    ExecutionDispatchRetryRequest,
    PlanExecutionApprovalRequest,
    RobotCommandAckRequest,
)
from app.services.execution_delivery import (
    ExecutionApprovalError,
    ExecutionConflictError,
    ExecutionDeliveryService,
    ExecutionSequenceError,
)


class MemoryPostgres:
    def __init__(self) -> None:
        self.approvals: dict[str, dict] = {}
        self.dispatches: dict[str, dict] = {}

    def approve_execution_plan(self, values: dict) -> dict:
        existing = self.approvals.get(values["plan_version"])
        if existing is None:
            existing = deepcopy(values)
            self.approvals[values["plan_version"]] = existing
        return deepcopy(existing)

    def get_execution_plan_approval(self, plan_version: str):
        value = self.approvals.get(plan_version)
        return deepcopy(value) if value else None

    def create_or_get_execution_dispatch(self, values: dict):
        existing = next(
            (
                row
                for row in self.dispatches.values()
                if row["idempotency_key"] == values["idempotency_key"]
            ),
            None,
        )
        if existing is not None:
            return deepcopy(existing), False
        self.dispatches[values["dispatch_id"]] = deepcopy(values)
        return deepcopy(values), True

    def get_execution_dispatch(self, dispatch_id: str):
        value = self.dispatches.get(dispatch_id)
        return deepcopy(value) if value else None

    def update_execution_dispatch(self, dispatch_id: str, values: dict) -> None:
        self.dispatches[dispatch_id].update(deepcopy(values))


class MemoryRedis:
    def __init__(self, plan: dict) -> None:
        self.plan = deepcopy(plan)
        self.active_plan_version = plan["plan_version"]
        self.rollback_calls: list[tuple] = []

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


class RecordingGateway:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.dispatch_calls = 0
        self.cancel_calls: list[tuple] = []

    def dispatch(self, plan_version: str, batches: list[dict]) -> dict:
        self.dispatch_calls += 1
        if self.dispatch_calls <= self.failures:
            raise RuntimeError("gateway timeout")
        return {
            "accepted": True,
            "status": "DISPATCH_ACCEPTED",
            "plan_version": plan_version,
            "received_robot_count": len(batches),
        }

    def cancel(self, dispatch_id: str, plan_version: str, *, reason: str) -> dict:
        self.cancel_calls.append((dispatch_id, plan_version, reason))
        return {"accepted": True, "status": "CANCELED"}


def plan_payload() -> dict:
    return {
        "plan_version": "PLAN-15",
        "command_id": "CMD-15",
        "warehouse_id": 1,
        "scope": {"plan_mode": "INITIAL_PLAN"},
        "required_tasks": [
            {
                "task_id": "W1:pick",
                "work_id": "W1",
                "action": "PICK",
                "item_id": "C",
                "quantity": 1,
                "source_candidates": [10],
                "target_candidates": [10],
            },
            {
                "task_id": "W1:drop",
                "work_id": "W1",
                "action": "DROP",
                "item_id": "C",
                "quantity": 1,
                "source_candidates": [10],
                "target_candidates": [20],
            },
        ],
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "W1:pick",
                    "work_id": "W1",
                    "action": "PICK",
                    "robot_id": "R1",
                    "source_node": 10,
                    "target_node": 10,
                    "start_time_step": 0,
                    "end_time_step": 1,
                },
                {
                    "task_id": "W1:drop",
                    "work_id": "W1",
                    "action": "DROP",
                    "robot_id": "R1",
                    "source_node": 10,
                    "target_node": 20,
                    "start_time_step": 1,
                    "end_time_step": 2,
                },
            ]
        },
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": ["W1:pick", "W1:drop"],
                    "waypoints": [
                        {"node_id": 10, "time_step": 0, "action": "MOVE"},
                        {"node_id": 10, "time_step": 1, "action": "MOVE"},
                        {"node_id": 20, "time_step": 2, "action": "MOVE"},
                    ],
                }
            ]
        },
        "inventory_operations": [],
        "charger_node_ids": [],
        "execution_task_dependencies": [],
        "scheduled_task_constraints": [],
        "ready_task_ids": ["W1:pick", "W1:drop"],
        "waiting_task_ids": [],
        "blocked_task_ids": [],
        "activated_at": "volatile-value-not-covered-by-approval",
    }


def batches() -> list[dict]:
    commands = [
        ("C1", 1, "START", None),
        ("C2", 2, "MOVE", "W1:pick"),
        ("C3", 3, "PICKUP", "W1:pick"),
        ("C4", 4, "MOVE", "W1:drop"),
        ("C5", 5, "DROPOFF", "W1:drop"),
        ("C6", 6, "STOP", None),
    ]
    return [
        {
            "plan_version": "PLAN-15",
            "warehouse_id": 1,
            "robot_id": "R1",
            "command_count": len(commands),
            "commands": [
                {
                    "command_id": command_id,
                    "sequence": sequence,
                    "plan_version": "PLAN-15",
                    "warehouse_id": 1,
                    "robot_id": "R1",
                    "task_id": task_id,
                    "work_id": "W1" if task_id else None,
                    "action": action,
                    "node_id": 10 if sequence <= 3 else 20,
                    "time_step": sequence - 1,
                    "time_step_seconds": 5,
                    "payload": {},
                }
                for command_id, sequence, action, task_id in commands
            ],
        }
    ]


def install(*, failures: int = 0):
    plan = plan_payload()
    postgres = MemoryPostgres()
    redis = MemoryRedis(plan)
    gateway = RecordingGateway(failures=failures)
    services = SimpleNamespace(postgres=postgres, redis=redis)
    service = ExecutionDeliveryService(services, gateway=gateway)
    service.approve_plan(
        plan_version="PLAN-15",
        command_id="CMD-15",
        warehouse_id=1,
        verification_decision="PASS",
        plan_payload=plan,
        request=PlanExecutionApprovalRequest(
            warehouse_id=1,
            actor_id="verifier",
            reason="검증 통과",
            expected_active_plan_version="PLAN-OLD",
        ),
    )
    return service, postgres, redis, gateway


def dispatched(service: ExecutionDeliveryService) -> dict:
    return service.dispatch(
        plan_version="PLAN-15",
        warehouse_id=1,
        command_id="CMD-15",
        batches=batches(),
        previous_active_plan_version="PLAN-OLD",
        max_attempts=2,
    )


def ack(command_id: str, sequence: int, *, status: str = "ACKED", ack_id: str | None = None):
    return RobotCommandAckRequest(
        ack_id=ack_id or f"ACK-{command_id}-{status}",
        plan_version="PLAN-15",
        robot_id="R1",
        command_id=command_id,
        sequence=sequence,
        status=status,
        error_code="MOTOR_FAULT" if status == "FAILED" else None,
        error_message="stopped" if status == "FAILED" else None,
    )


def test_unverified_plan_cannot_be_approved() -> None:
    plan = plan_payload()
    services = SimpleNamespace(postgres=MemoryPostgres(), redis=MemoryRedis(plan))
    with pytest.raises(ExecutionApprovalError, match="VERIFICATION_NOT_APPROVED"):
        ExecutionDeliveryService(services).approve_plan(
            plan_version="PLAN-15",
            command_id="CMD-15",
            warehouse_id=1,
            verification_decision="FAIL",
            plan_payload=plan,
        )


def test_active_plan_payload_must_match_approved_version() -> None:
    service, _postgres, redis, _gateway = install()
    redis.plan["required_tasks"][0]["quantity"] = 999
    with pytest.raises(ExecutionConflictError, match="ACTIVE_PLAN_PAYLOAD_NOT_APPROVED"):
        dispatched(service)


def test_dispatch_is_idempotent_and_does_not_send_twice() -> None:
    service, _postgres, _redis, gateway = install()
    first = dispatched(service)
    second = dispatched(service)
    assert first["status"] == "AWAITING_ACK"
    assert second["duplicate"] is True
    assert second["dispatch_id"] == first["dispatch_id"]
    assert gateway.dispatch_calls == 1


def test_command_batch_sequence_is_strict() -> None:
    service, _postgres, _redis, _gateway = install()
    invalid = batches()
    invalid[0]["commands"][1]["sequence"] = 9
    with pytest.raises(ExecutionSequenceError, match="COMMAND_SEQUENCE_INVALID"):
        service.dispatch(
            plan_version="PLAN-15",
            warehouse_id=1,
            command_id="CMD-15",
            batches=invalid,
            previous_active_plan_version="PLAN-OLD",
            max_attempts=2,
        )


def test_ack_out_of_order_is_rejected_and_duplicate_ack_is_idempotent() -> None:
    service, _postgres, _redis, _gateway = install()
    result = dispatched(service)
    dispatch_id = result["dispatch_id"]
    with pytest.raises(ExecutionSequenceError, match="ACK_OUT_OF_ORDER"):
        service.acknowledge(dispatch_id, ack("C2", 2))
    first = service.acknowledge(dispatch_id, ack("C1", 1))
    duplicate = service.acknowledge(dispatch_id, ack("C1", 1))
    assert first["status"] == "PARTIAL_ACK"
    assert duplicate["duplicate"] is True


def test_failure_before_physical_progress_cancels_and_rolls_back() -> None:
    service, postgres, redis, gateway = install()
    result = dispatched(service)
    failed = service.acknowledge(
        result["dispatch_id"], ack("C1", 1, status="FAILED")
    )
    assert failed["status"] == "ROLLED_BACK"
    assert failed["result_summary"]["manual_recovery_required"] is False
    assert redis.active_plan_version == "PLAN-OLD"
    assert redis.rollback_calls
    assert gateway.cancel_calls
    assert {
        row["status"] for row in postgres.dispatches[result["dispatch_id"]]["command_states"]
    } <= {"FAILED", "CANCELED"}


def test_partial_failure_after_move_requires_manual_recovery() -> None:
    service, _postgres, redis, gateway = install()
    result = dispatched(service)
    dispatch_id = result["dispatch_id"]
    service.acknowledge(dispatch_id, ack("C1", 1))
    service.acknowledge(dispatch_id, ack("C2", 2))
    failed = service.acknowledge(
        dispatch_id, ack("C3", 3, status="FAILED")
    )
    assert failed["status"] == "PARTIAL_FAILURE"
    assert failed["result_summary"]["manual_recovery_required"] is True
    assert failed["result_summary"]["rollback"]["restored"] is False
    assert redis.active_plan_version == "PLAN-15"
    assert gateway.cancel_calls


def test_cancel_before_progress_rolls_back_and_is_idempotent() -> None:
    service, _postgres, redis, _gateway = install()
    result = dispatched(service)
    canceled = service.cancel(
        result["dispatch_id"],
        ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소"),
    )
    duplicate = service.cancel(
        result["dispatch_id"],
        ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소"),
    )
    assert canceled["status"] == "ROLLED_BACK"
    assert duplicate["duplicate"] is True
    assert redis.active_plan_version == "PLAN-OLD"


def test_timeout_retry_reuses_same_dispatch_identity() -> None:
    service, postgres, _redis, gateway = install(failures=1)
    timed_out = dispatched(service)
    assert timed_out["status"] == "DISPATCH_TIMEOUT"
    assert timed_out["retryable"] is True
    dispatch_id = timed_out["dispatch_id"]
    retried = service.retry(
        dispatch_id,
        ExecutionDispatchRetryRequest(actor_id="system", reason="timeout retry"),
    )
    assert retried["dispatch_id"] == dispatch_id
    assert retried["status"] == "AWAITING_ACK"
    assert retried["attempt_count"] == 2
    assert gateway.dispatch_calls == 2


def test_planning_execute_nodes_use_durable_approval_and_dispatch(monkeypatch) -> None:
    import app.planning.nodes as nodes

    state = {
        "plan_version": "PLAN-15",
        "command": {"command_id": "CMD-15", "warehouse_id": 1},
        "simulation": {"valid": True},
        "verification_decision": {"decision": "PASS"},
        "snapshot": {"redis": {"active_plan_version": None}},
        "scope": {"plan_mode": "INITIAL_PLAN"},
        "required_tasks": deepcopy(plan_payload()["required_tasks"]),
        "cuopt_plan": deepcopy(plan_payload()["cuopt_plan"]),
        "collision_plan": deepcopy(plan_payload()["collision_plan"]),
        "inventory_operations": [],
        "interpretation": {
            "scheduled_task_constraints": [],
            "task_dependencies": [],
        },
        "ready_task_ids": ["W1:pick", "W1:drop"],
        "waiting_task_ids": [],
        "blocked_task_ids": [],
        "optimization_problem": {
            "time_step_seconds": 5,
            "nodes": [],
        },
    }
    active_plan = nodes.plan_payload(state, "PLAN-15")
    postgres = MemoryPostgres()
    redis = MemoryRedis(active_plan)
    services = SimpleNamespace(postgres=postgres, redis=redis)

    class NodeGateway:
        send_count = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dispatch(self, plan_version: str, command_batches: list[dict]) -> dict:
            NodeGateway.send_count += 1
            return {
                "accepted": True,
                "status": "DISPATCH_ACCEPTED",
                "plan_version": plan_version,
                "received_robot_count": len(command_batches),
            }

        def cancel(self, *_args, **_kwargs) -> dict:
            return {"accepted": True, "status": "CANCELED"}

    settings = SimpleNamespace(
        robot_gateway_url="http://gateway",
        request_timeout_seconds=1,
        robot_gateway_max_attempts=3,
        robot_gateway_retry_backoff_seconds=0,
        time_step_seconds=5,
    )
    monkeypatch.setattr(nodes, "get_services", lambda: services)
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    monkeypatch.setattr(nodes, "RobotGateway", NodeGateway)

    precheck = nodes.execution_precheck_node(state)
    assert precheck["execution_ready"] is True
    assert precheck["execution_approval"]["status"] == "APPROVED"
    state.update(precheck)
    result = nodes.dispatch_plan_node(state)
    assert result["final_status"] == "DISPATCHED"
    assert result["dispatch_result"]["status"] == "AWAITING_ACK"
    assert result["dispatch_result"]["dispatch_id"]
    assert NodeGateway.send_count == 1


class FlakyCancelGateway(RecordingGateway):
    def __init__(self, *, cancel_failures: int = 1, accepted: bool = True) -> None:
        super().__init__()
        self.cancel_failures = cancel_failures
        self.cancel_attempts = 0
        self.accepted = accepted

    def cancel(self, dispatch_id: str, plan_version: str, *, reason: str) -> dict:
        self.cancel_attempts += 1
        self.cancel_calls.append((dispatch_id, plan_version, reason))
        if self.cancel_attempts <= self.cancel_failures:
            raise RuntimeError("gateway cancel unavailable")
        if not self.accepted:
            return {"accepted": False, "status": "REJECTED"}
        return {
            "accepted": True,
            "status": "CANCELED",
            "dispatch_id": dispatch_id,
            "plan_version": plan_version,
            "duplicate": self.cancel_attempts > self.cancel_failures + 1,
        }


class FlakyRollbackRedis(MemoryRedis):
    def __init__(self, plan: dict, *, failures: int = 1) -> None:
        super().__init__(plan)
        self.failures = failures

    def rollback_plan_activation(
        self, warehouse_id: int, failed_plan_version: str, previous: str | None
    ) -> bool:
        self.rollback_calls.append((warehouse_id, failed_plan_version, previous))
        if len(self.rollback_calls) <= self.failures:
            raise RuntimeError("redis rollback unavailable")
        if self.active_plan_version != failed_plan_version:
            return False
        self.active_plan_version = previous
        return True


def install_custom(gateway, redis=None):
    plan = plan_payload()
    postgres = MemoryPostgres()
    redis = redis or MemoryRedis(plan)
    services = SimpleNamespace(postgres=postgres, redis=redis)
    service = ExecutionDeliveryService(services, gateway=gateway)
    service.approve_plan(
        plan_version="PLAN-15",
        command_id="CMD-15",
        warehouse_id=1,
        verification_decision="PASS",
        plan_payload=plan,
        request=PlanExecutionApprovalRequest(
            warehouse_id=1,
            actor_id="verifier",
            reason="검증 통과",
            expected_active_plan_version="PLAN-OLD",
        ),
    )
    return service, postgres, redis, gateway


def test_cancel_unconfirmed_blocks_rollback_and_marks_cancel_pending() -> None:
    service, _postgres, redis, gateway = install_custom(
        FlakyCancelGateway(cancel_failures=1)
    )
    result = dispatched(service)
    canceled = service.cancel(
        result["dispatch_id"],
        ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소"),
    )
    assert canceled["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert canceled["retryable"] is True
    assert canceled["result_summary"]["gateway_cancel_confirmed"] is False
    assert canceled["result_summary"]["reason_code"] == "GATEWAY_CANCEL_UNCONFIRMED"
    assert canceled["result_summary"]["rollback"]["status"] == "NOT_ATTEMPTED"
    assert canceled["result_summary"]["manual_recovery_required"] is True
    assert redis.active_plan_version == "PLAN-15"
    assert redis.rollback_calls == []
    assert {row["status"] for row in canceled["command_states"]} == {
        "CANCEL_PENDING"
    }
    assert gateway.cancel_attempts == 1


def test_cancel_replay_confirms_gateway_then_rolls_back() -> None:
    service, _postgres, redis, gateway = install_custom(
        FlakyCancelGateway(cancel_failures=1)
    )
    result = dispatched(service)
    request = ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소")
    first = service.cancel(result["dispatch_id"], request)
    second = service.cancel(result["dispatch_id"], request)
    duplicate = service.cancel(result["dispatch_id"], request)
    assert first["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert second["status"] == "ROLLED_BACK"
    assert second["recovery_replay"] is True
    assert second["result_summary"]["gateway_cancel_confirmed"] is True
    assert second["result_summary"]["rollback"]["restored"] is True
    assert {row["status"] for row in second["command_states"]} == {"CANCELED"}
    assert redis.active_plan_version == "PLAN-OLD"
    assert gateway.cancel_attempts == 2
    assert duplicate["duplicate"] is True


def test_gateway_rejection_is_not_cancel_confirmation() -> None:
    service, _postgres, redis, _gateway = install_custom(
        FlakyCancelGateway(cancel_failures=0, accepted=False)
    )
    result = dispatched(service)
    canceled = service.cancel(
        result["dispatch_id"],
        ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소"),
    )
    assert canceled["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert canceled["result_summary"]["gateway_cancel_confirmed"] is False
    assert canceled["result_summary"]["reason_code"] == "GATEWAY_CANCEL_UNCONFIRMED"
    assert redis.active_plan_version == "PLAN-15"
    assert redis.rollback_calls == []


def test_confirmed_cancel_rollback_failure_is_retryable() -> None:
    plan = plan_payload()
    redis = FlakyRollbackRedis(plan, failures=1)
    service, _postgres, redis, gateway = install_custom(
        FlakyCancelGateway(cancel_failures=0), redis=redis
    )
    result = dispatched(service)
    request = ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소")
    first = service.cancel(result["dispatch_id"], request)
    second = service.cancel(result["dispatch_id"], request)
    assert first["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert first["result_summary"]["gateway_cancel_confirmed"] is True
    assert first["result_summary"]["reason_code"] == "ROLLBACK_FAILED"
    assert first["retryable"] is True
    assert redis.active_plan_version == "PLAN-OLD"
    assert second["status"] == "ROLLED_BACK"
    assert second["recovery_replay"] is True
    assert second["result_summary"]["rollback"]["restored"] is True
    assert gateway.cancel_attempts == 2


def test_confirmed_cancel_after_move_requires_manual_recovery_without_rollback() -> None:
    service, _postgres, redis, _gateway = install_custom(
        FlakyCancelGateway(cancel_failures=0)
    )
    result = dispatched(service)
    dispatch_id = result["dispatch_id"]
    service.acknowledge(dispatch_id, ack("C1", 1))
    service.acknowledge(dispatch_id, ack("C2", 2))
    canceled = service.cancel(
        dispatch_id,
        ExecutionDispatchCancelRequest(actor_id="operator", reason="진행 후 취소"),
    )
    assert canceled["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert canceled["retryable"] is False
    assert canceled["result_summary"]["gateway_cancel_confirmed"] is True
    assert canceled["result_summary"]["physical_progress"] is True
    assert canceled["result_summary"]["manual_recovery_required"] is True
    assert canceled["result_summary"]["reason_code"] == "PHYSICAL_PROGRESS_ALREADY_ACKED"
    assert redis.active_plan_version == "PLAN-15"
    assert redis.rollback_calls == []


def test_command_failure_cancel_unconfirmed_does_not_rollback() -> None:
    service, _postgres, redis, _gateway = install_custom(
        FlakyCancelGateway(cancel_failures=1)
    )
    result = dispatched(service)
    failed = service.acknowledge(
        result["dispatch_id"], ack("C1", 1, status="FAILED")
    )
    assert failed["status"] == "PARTIAL_FAILURE"
    assert failed["result_summary"]["gateway_cancel_confirmed"] is False
    assert failed["result_summary"]["reason_code"] == "GATEWAY_CANCEL_UNCONFIRMED"
    assert failed["result_summary"]["retryable"] is True
    assert failed["result_summary"]["manual_recovery_required"] is True
    assert failed["result_summary"]["rollback"]["status"] == "NOT_ATTEMPTED"
    assert redis.active_plan_version == "PLAN-15"
    assert redis.rollback_calls == []
    statuses = {row["status"] for row in failed["command_states"]}
    assert statuses == {"FAILED", "CANCEL_PENDING"}


def test_command_failure_cancel_replay_can_finish_safe_rollback() -> None:
    service, _postgres, redis, gateway = install_custom(
        FlakyCancelGateway(cancel_failures=1)
    )
    result = dispatched(service)
    dispatch_id = result["dispatch_id"]
    failed = service.acknowledge(dispatch_id, ack("C1", 1, status="FAILED"))
    recovered = service.cancel(
        dispatch_id,
        ExecutionDispatchCancelRequest(actor_id="operator", reason="실패 취소 재확인"),
    )
    assert failed["status"] == "PARTIAL_FAILURE"
    assert recovered["status"] == "ROLLED_BACK"
    assert recovered["recovery_replay"] is True
    assert recovered["result_summary"]["gateway_cancel_confirmed"] is True
    assert recovered["result_summary"]["rollback"]["restored"] is True
    assert redis.active_plan_version == "PLAN-OLD"
    assert gateway.cancel_attempts == 2


def test_ack_after_unconfirmed_cancel_preserves_recovery_state() -> None:
    service, _postgres, redis, gateway = install_custom(
        FlakyCancelGateway(cancel_failures=1)
    )
    result = dispatched(service)
    dispatch_id = result["dispatch_id"]
    canceled = service.cancel(
        dispatch_id,
        ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소"),
    )
    start_ack = service.acknowledge(dispatch_id, ack("C1", 1))
    move_ack = service.acknowledge(dispatch_id, ack("C2", 2))
    assert canceled["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert start_ack["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert start_ack["result_summary"]["physical_progress"] is False
    assert move_ack["status"] == "CANCELED_PARTIAL_EXECUTION"
    assert move_ack["result_summary"]["physical_progress"] is True
    assert move_ack["result_summary"]["reason_code"] == "GATEWAY_CANCEL_UNCONFIRMED"
    assert move_ack["result_summary"]["manual_recovery_required"] is True
    assert redis.active_plan_version == "PLAN-15"
    assert redis.rollback_calls == []
    assert gateway.cancel_attempts == 1


class RemoteIdentityGateway(RecordingGateway):
    def dispatch(self, plan_version: str, batches: list[dict]) -> dict:
        self.dispatch_calls += 1
        return {
            "accepted": True,
            "status": "DISPATCH_ACCEPTED",
            "plan_version": plan_version,
            "received_robot_count": len(batches),
            "dispatch_id": "REMOTE-GATEWAY-DISPATCH-15",
        }


def test_cancel_uses_gateway_dispatch_identity_not_service_identity() -> None:
    service, _postgres, redis, gateway = install_custom(RemoteIdentityGateway())
    result = dispatched(service)
    assert result["dispatch_id"] != "REMOTE-GATEWAY-DISPATCH-15"
    canceled = service.cancel(
        result["dispatch_id"],
        ExecutionDispatchCancelRequest(actor_id="operator", reason="안전 취소"),
    )
    assert canceled["status"] == "ROLLED_BACK"
    assert gateway.cancel_calls[0][0] == "REMOTE-GATEWAY-DISPATCH-15"
    assert canceled["result_summary"]["cancel_result"]["service_dispatch_id"] == result["dispatch_id"]
    assert canceled["result_summary"]["cancel_result"]["gateway_dispatch_id"] == "REMOTE-GATEWAY-DISPATCH-15"
    assert redis.active_plan_version == "PLAN-OLD"


class TimeoutWithKnownIdentityGateway(RecordingGateway):
    def dispatch_identity(self, plan_version: str, batches: list[dict]) -> dict:
        return {
            "dispatch_id": "REMOTE-TIMEOUT-DISPATCH-15",
            "idempotency_key": "REMOTE-TIMEOUT-DISPATCH-15",
            "payload_fingerprint": "REMOTE-FINGERPRINT-15",
        }

    def dispatch(self, plan_version: str, batches: list[dict]) -> dict:
        self.dispatch_calls += 1
        raise RuntimeError("response timeout after gateway acceptance")


def test_timeout_cancel_uses_precomputed_gateway_identity() -> None:
    service, postgres, redis, gateway = install_custom(
        TimeoutWithKnownIdentityGateway()
    )
    timed_out = dispatched(service)
    assert timed_out["status"] == "DISPATCH_TIMEOUT"
    stored = postgres.dispatches[timed_out["dispatch_id"]]
    assert stored["gateway_result"]["dispatch_id"] == "REMOTE-TIMEOUT-DISPATCH-15"
    canceled = service.cancel(
        timed_out["dispatch_id"],
        ExecutionDispatchCancelRequest(actor_id="operator", reason="timeout 취소"),
    )
    assert canceled["status"] == "ROLLED_BACK"
    assert gateway.cancel_calls[0][0] == "REMOTE-TIMEOUT-DISPATCH-15"
    assert canceled["result_summary"]["gateway_cancel_confirmed"] is True
    assert redis.active_plan_version == "PLAN-OLD"

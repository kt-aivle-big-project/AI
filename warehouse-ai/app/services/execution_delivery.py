"""P16.5.15 durable, idempotent robot command delivery lifecycle."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.models import (
    ExecutionDispatchCancelRequest,
    ExecutionDispatchRetryRequest,
    PlanExecutionApprovalRequest,
    RobotCommandAckRequest,
)


class ExecutionDeliveryError(RuntimeError):
    code = "EXECUTION_DELIVERY_ERROR"


class ExecutionApprovalError(ExecutionDeliveryError):
    code = "PLAN_NOT_APPROVED"


class ExecutionConflictError(ExecutionDeliveryError):
    code = "EXECUTION_CONFLICT"


class ExecutionNotFoundError(ExecutionDeliveryError):
    code = "EXECUTION_NOT_FOUND"


class ExecutionSequenceError(ExecutionDeliveryError):
    code = "COMMAND_SEQUENCE_INVALID"


class ExecutionRetryExhaustedError(ExecutionDeliveryError):
    code = "DISPATCH_RETRY_EXHAUSTED"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def execution_plan_core(plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable operational fields covered by execution approval."""

    return {
        key: deepcopy(plan_payload.get(key))
        for key in (
            "plan_version",
            "command_id",
            "warehouse_id",
            "scope",
            "required_tasks",
            "cuopt_plan",
            "collision_plan",
            "inventory_operations",
            "charger_node_ids",
            "execution_task_dependencies",
            "scheduled_task_constraints",
            "ready_task_ids",
            "waiting_task_ids",
            "blocked_task_ids",
        )
    }


def execution_plan_fingerprint(plan_payload: dict[str, Any]) -> str:
    return payload_fingerprint(execution_plan_core(plan_payload))


def deterministic_dispatch_id(
    *, plan_version: str, warehouse_id: int, fingerprint: str
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"p16.5.15:{warehouse_id}:{plan_version}:{fingerprint}",
        )
    )


def validate_command_batches(
    plan_version: str,
    batches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate command identities and build initial durable command states."""

    seen_command_ids: set[str] = set()
    command_states: list[dict[str, Any]] = []
    for batch in sorted(batches, key=lambda row: str(row.get("robot_id") or "")):
        robot_id = str(batch.get("robot_id") or "")
        if not robot_id:
            raise ExecutionSequenceError("ROBOT_ID_REQUIRED")
        if str(batch.get("plan_version") or "") != plan_version:
            raise ExecutionSequenceError(
                f"BATCH_PLAN_VERSION_MISMATCH:{robot_id}"
            )
        commands = list(batch.get("commands") or [])
        if int(batch.get("command_count") or 0) != len(commands):
            raise ExecutionSequenceError(f"COMMAND_COUNT_MISMATCH:{robot_id}")
        for expected, command in enumerate(commands, start=1):
            command_id = str(command.get("command_id") or "")
            if not command_id:
                raise ExecutionSequenceError(
                    f"COMMAND_ID_REQUIRED:{robot_id}:{expected}"
                )
            if command_id in seen_command_ids:
                raise ExecutionSequenceError(f"COMMAND_ID_DUPLICATE:{command_id}")
            seen_command_ids.add(command_id)
            sequence = int(command.get("sequence") or 0)
            if sequence != expected:
                raise ExecutionSequenceError(
                    f"COMMAND_SEQUENCE_INVALID:{robot_id}:{sequence}:{expected}"
                )
            if str(command.get("plan_version") or "") != plan_version:
                raise ExecutionSequenceError(
                    f"COMMAND_PLAN_VERSION_MISMATCH:{command_id}"
                )
            if str(command.get("robot_id") or "") != robot_id:
                raise ExecutionSequenceError(
                    f"COMMAND_ROBOT_MISMATCH:{command_id}"
                )
            command_states.append(
                {
                    "command_id": command_id,
                    "robot_id": robot_id,
                    "sequence": sequence,
                    "action": str(command.get("action") or ""),
                    "task_id": command.get("task_id"),
                    "work_id": command.get("work_id"),
                    "status": "PENDING",
                    "attempt_count": 0,
                    "ack_id": None,
                    "error_code": None,
                    "error_message": None,
                    "sent_at": None,
                    "acked_at": None,
                }
            )
    if not command_states:
        raise ExecutionSequenceError("EMPTY_COMMAND_BATCH")
    return command_states


def _is_handling_action(action: str) -> bool:
    return str(action).upper() in {"PICKUP", "DROPOFF", "CHARGE"}


def _is_physical_progress(action: str) -> bool:
    return str(action).upper() not in {"START"}


_CANCEL_CONFIRMED_STATUSES = {
    "CANCELED",
    "CANCELLED",
    "ALREADY_CANCELED",
    "ALREADY_CANCELLED",
}


def _gateway_cancel_confirmed(result: dict[str, Any]) -> bool:
    """Require explicit gateway acceptance before logical rollback."""

    return bool(result.get("accepted")) and str(
        result.get("status") or ""
    ).upper() in _CANCEL_CONFIRMED_STATUSES


def _set_unfinished_command_status(
    states: list[dict[str, Any]],
    status: str,
) -> None:
    for state in states:
        if state.get("status") in {"PENDING", "SENT", "CANCEL_PENDING"}:
            state["status"] = status


ACK_CLOCK_SKEW_TOLERANCE_SECONDS = 5


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mark_delivery_terminal(
    states: list[dict[str, Any]],
    *,
    error_message: str,
) -> None:
    """Close commands that can no longer be delivered after retry exhaustion."""

    for state in states:
        if state.get("status") in {"PENDING", "SENT", "CANCEL_PENDING"}:
            state["status"] = "DISPATCH_FAILED"
            state["error_code"] = "DISPATCH_RETRY_EXHAUSTED"
            state["error_message"] = error_message


class ExecutionDeliveryService:
    """Coordinates PostgreSQL audit, Redis plan authority and robot gateway."""

    def __init__(self, services: Any, gateway: Any | None = None):
        self.services = services
        self.gateway = gateway

    def approve_plan(
        self,
        *,
        plan_version: str,
        command_id: str | None,
        warehouse_id: int,
        verification_decision: str,
        plan_payload: dict[str, Any],
        request: PlanExecutionApprovalRequest | None = None,
    ) -> dict[str, Any]:
        decision = str(verification_decision or "").upper()
        if decision not in {"PASS", "PASS_WITH_WARNING"}:
            raise ExecutionApprovalError(
                f"VERIFICATION_NOT_APPROVED:{decision or 'MISSING'}"
            )
        request = request or PlanExecutionApprovalRequest(
            warehouse_id=warehouse_id,
            actor_id="SYSTEM_VERIFICATION",
            reason=f"Verification Agent decision={decision}",
        )
        if int(request.warehouse_id) != int(warehouse_id):
            raise ExecutionConflictError("APPROVAL_WAREHOUSE_MISMATCH")
        fingerprint = execution_plan_fingerprint(plan_payload)
        values = {
            "plan_version": plan_version,
            "warehouse_id": warehouse_id,
            "command_id": command_id,
            "verification_decision": decision,
            "status": "APPROVED",
            "plan_fingerprint": fingerprint,
            "expected_active_plan_version": request.expected_active_plan_version,
            "approved_by": request.actor_id,
            "approval_reason": request.reason,
            "approved_at": datetime.now(UTC),
        }
        stored = self.services.postgres.approve_execution_plan(values)
        if int(stored.get("warehouse_id") or 0) != int(warehouse_id):
            raise ExecutionConflictError("PLAN_VERSION_WAREHOUSE_CONFLICT")
        if str(stored.get("plan_fingerprint") or "") != fingerprint:
            raise ExecutionConflictError("PLAN_VERSION_PAYLOAD_CONFLICT")
        if str(stored.get("status") or "").upper() != "APPROVED":
            raise ExecutionApprovalError("PLAN_APPROVAL_NOT_ACTIVE")
        return {
            "status": "APPROVED",
            "plan_version": plan_version,
            "warehouse_id": warehouse_id,
            "verification_decision": decision,
            "plan_fingerprint": fingerprint,
            "approved_by": stored.get("approved_by"),
            "approval_reason": stored.get("approval_reason"),
            "approved_at": stored.get("approved_at"),
        }

    def get_approval(self, plan_version: str) -> dict[str, Any]:
        row = self.services.postgres.get_execution_plan_approval(plan_version)
        if row is None:
            raise ExecutionNotFoundError("PLAN_APPROVAL_NOT_FOUND")
        return row

    def dispatch(
        self,
        *,
        plan_version: str,
        warehouse_id: int,
        command_id: str | None,
        batches: list[dict[str, Any]],
        previous_active_plan_version: str | None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        approval = self.get_approval(plan_version)
        if str(approval.get("status") or "").upper() != "APPROVED":
            raise ExecutionApprovalError("PLAN_APPROVAL_NOT_ACTIVE")
        if int(approval.get("warehouse_id") or 0) != int(warehouse_id):
            raise ExecutionConflictError("APPROVED_PLAN_WAREHOUSE_MISMATCH")

        live = self.services.redis.live_snapshot(warehouse_id)
        active_version = str(live.get("active_plan_version") or "")
        if active_version != plan_version:
            raise ExecutionConflictError(
                f"ACTIVE_PLAN_VERSION_MISMATCH:{active_version or 'NONE'}:{plan_version}"
            )
        active_plan = live.get("active_plan") or {}
        active_fingerprint = execution_plan_fingerprint(active_plan)
        if str(approval.get("plan_fingerprint") or "") != active_fingerprint:
            raise ExecutionConflictError("ACTIVE_PLAN_PAYLOAD_NOT_APPROVED")

        fingerprint = payload_fingerprint(batches)
        dispatch_id = deterministic_dispatch_id(
            plan_version=plan_version,
            warehouse_id=warehouse_id,
            fingerprint=fingerprint,
        )
        command_states = validate_command_batches(plan_version, batches)
        gateway_identity = self._gateway_dispatch_identity(plan_version, batches)
        now = datetime.now(UTC)
        row, created = self.services.postgres.create_or_get_execution_dispatch(
            {
                "dispatch_id": dispatch_id,
                "idempotency_key": dispatch_id,
                "warehouse_id": warehouse_id,
                "command_id": command_id,
                "plan_version": plan_version,
                "approved_plan_fingerprint": approval.get("plan_fingerprint"),
                "payload_fingerprint": fingerprint,
                "previous_active_plan_version": previous_active_plan_version,
                "status": "PREPARED",
                "attempt_count": 0,
                "max_attempts": max(1, int(max_attempts)),
                "command_batches": batches,
                "command_states": command_states,
                "gateway_result": gateway_identity,
                "result_summary": {},
                "created_at": now,
                "updated_at": now,
            }
        )
        if str(row.get("payload_fingerprint") or "") != fingerprint:
            raise ExecutionConflictError("DISPATCH_ID_PAYLOAD_CONFLICT")
        if not created and str(row.get("status") or "").upper() in {
            "AWAITING_ACK",
            "PARTIAL_ACK",
            "COMPLETED",
            "PARTIAL_FAILURE",
            "ROLLED_BACK",
            "CANCELED",
            "CANCELED_PARTIAL_EXECUTION",
        }:
            return {
                "accepted": str(row.get("status") or "").upper()
                not in {"PARTIAL_FAILURE", "ROLLED_BACK", "CANCELED", "CANCELED_PARTIAL_EXECUTION"},
                "duplicate": True,
                "dispatch_id": dispatch_id,
                "plan_version": plan_version,
                "status": row.get("status"),
                "attempt_count": row.get("attempt_count", 0),
                "max_attempts": row.get("max_attempts", max_attempts),
                "command_states": row.get("command_states") or [],
                "gateway_result": row.get("gateway_result") or {},
            }
        return self._send_existing(row, duplicate=not created)

    def _send_existing(
        self,
        row: dict[str, Any],
        *,
        duplicate: bool,
    ) -> dict[str, Any]:
        if self.gateway is None:
            raise ExecutionDeliveryError("ROBOT_GATEWAY_REQUIRED")
        attempt_count = int(row.get("attempt_count") or 0)
        max_attempts = int(row.get("max_attempts") or 1)
        if attempt_count >= max_attempts:
            raise ExecutionRetryExhaustedError(
                f"DISPATCH_RETRY_EXHAUSTED:{attempt_count}:{max_attempts}"
            )
        attempt_count += 1
        now = datetime.now(UTC)
        states = deepcopy(row.get("command_states") or [])
        for state in states:
            if state.get("status") in {"PENDING", "SENT"}:
                state["attempt_count"] = int(state.get("attempt_count") or 0) + 1
        self.services.postgres.update_execution_dispatch(
            str(row["dispatch_id"]),
            {
                "status": "DISPATCHING",
                "attempt_count": attempt_count,
                "command_states": states,
                "updated_at": now,
                "last_error": None,
            },
        )
        try:
            gateway_result = self.gateway.dispatch(
                str(row["plan_version"]),
                deepcopy(row.get("command_batches") or []),
            )
            accepted = bool(gateway_result.get("accepted", False))
            if not accepted:
                raise ExecutionDeliveryError(
                    f"GATEWAY_REJECTED:{gateway_result.get('status') or 'UNKNOWN'}"
                )
            sent_at = datetime.now(UTC).isoformat()
            for state in states:
                if state.get("status") in {"PENDING", "SENT"}:
                    state["status"] = "SENT"
                    state["sent_at"] = sent_at
            update = {
                "status": "AWAITING_ACK",
                "attempt_count": attempt_count,
                "command_states": states,
                "gateway_result": gateway_result,
                "dispatched_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "last_error": None,
                "result_summary": {
                    "policy": "APPROVED_PLAN_IDEMPOTENT_COMMAND_DELIVERY",
                    "awaiting_ack_count": len(
                        [state for state in states if state["status"] == "SENT"]
                    ),
                },
            }
            self.services.postgres.update_execution_dispatch(
                str(row["dispatch_id"]), update
            )
            return {
                **gateway_result,
                "accepted": True,
                "duplicate": duplicate or bool(gateway_result.get("duplicate", False)),
                "dispatch_id": row["dispatch_id"],
                "plan_version": row["plan_version"],
                "status": "AWAITING_ACK",
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "command_states": states,
                "ack_policy": "STRICT_PER_ROBOT_SEQUENCE",
            }
        except Exception as exc:
            exhausted = attempt_count >= max_attempts
            failure_status = "RETRY_EXHAUSTED" if exhausted else "DISPATCH_TIMEOUT"
            if exhausted:
                _mark_delivery_terminal(states, error_message=str(exc))
            summary = {
                "retryable": not exhausted,
                "failure": str(exc),
            }
            self.services.postgres.update_execution_dispatch(
                str(row["dispatch_id"]),
                {
                    "status": failure_status,
                    "attempt_count": attempt_count,
                    "command_states": states,
                    "last_error": str(exc),
                    "updated_at": datetime.now(UTC),
                    "result_summary": summary,
                },
            )
            if exhausted:
                rollback = self._rollback_if_safe(
                    {
                        **row,
                        "attempt_count": attempt_count,
                        "command_states": states,
                        "status": failure_status,
                    },
                    reason="DISPATCH_RETRY_EXHAUSTED",
                )
                cleanup = rollback.get("inventory_reservation_release") or {}
                cleanup_failed = str(cleanup.get("status") or "").upper() == "FAILED"
                summary = {
                    "retryable": False,
                    "failure": str(exc),
                    "rollback": rollback,
                    "manual_recovery_required": bool(cleanup_failed)
                    or not bool(rollback.get("restored")),
                    "reason_code": (
                        "INVENTORY_RESERVATION_RELEASE_FAILED"
                        if cleanup_failed
                        else None
                    ),
                }
                self.services.postgres.update_execution_dispatch(
                    str(row["dispatch_id"]),
                    {
                        "status": failure_status,
                        "attempt_count": attempt_count,
                        "command_states": states,
                        "last_error": str(exc),
                        "updated_at": datetime.now(UTC),
                        "completed_at": datetime.now(UTC),
                        "result_summary": summary,
                    },
                )
                raise ExecutionRetryExhaustedError(
                    f"{exc}; rollback={rollback}"
                ) from exc
            return {
                "accepted": False,
                "duplicate": duplicate,
                "retryable": True,
                "dispatch_id": row["dispatch_id"],
                "plan_version": row["plan_version"],
                "status": "DISPATCH_TIMEOUT",
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "command_states": states,
                "error": str(exc),
                "ack_policy": "STRICT_PER_ROBOT_SEQUENCE",
            }

    def acknowledge(
        self,
        dispatch_id: str,
        request: RobotCommandAckRequest,
    ) -> dict[str, Any]:
        row = self.services.postgres.get_execution_dispatch(dispatch_id)
        if row is None:
            raise ExecutionNotFoundError("DISPATCH_NOT_FOUND")
        if str(row.get("plan_version")) != request.plan_version:
            raise ExecutionConflictError("ACK_PLAN_VERSION_MISMATCH")
        row_status = str(row.get("status") or "").upper()
        prior_summary = deepcopy(row.get("result_summary") or {})
        cancellation_ack_window = (
            row_status in {"CANCELED_PARTIAL_EXECUTION", "PARTIAL_FAILURE"}
            and str(prior_summary.get("reason_code") or "").upper()
            == "GATEWAY_CANCEL_UNCONFIRMED"
        )
        if row_status not in {"AWAITING_ACK", "PARTIAL_ACK"} and not cancellation_ack_window:
            raise ExecutionConflictError(f"ACK_DISPATCH_NOT_ACTIVE:{row_status or 'UNKNOWN'}")
        states = deepcopy(row.get("command_states") or [])
        target = next(
            (state for state in states if state.get("command_id") == request.command_id),
            None,
        )
        if target is None:
            raise ExecutionNotFoundError("COMMAND_NOT_FOUND")
        if str(target.get("robot_id")) != request.robot_id:
            raise ExecutionConflictError("ACK_ROBOT_MISMATCH")
        if int(target.get("sequence") or 0) != request.sequence:
            raise ExecutionConflictError("ACK_SEQUENCE_IDENTITY_MISMATCH")

        sent_at = _as_utc_datetime(target.get("sent_at"))
        if sent_at is not None and request.occurred_at < (
            sent_at - timedelta(seconds=ACK_CLOCK_SKEW_TOLERANCE_SECONDS)
        ):
            raise ExecutionConflictError(
                "ACK_BEFORE_COMMAND_SENT:"
                f"{request.command_id}:{request.occurred_at.isoformat()}:{sent_at.isoformat()}"
            )

        terminal_status = str(target.get("status") or "")
        if terminal_status in {"ACKED", "FAILED"}:
            if target.get("ack_id") == request.ack_id and terminal_status == request.status:
                return {
                    "status": row.get("status"),
                    "dispatch_id": dispatch_id,
                    "duplicate": True,
                    "command": target,
                }
            raise ExecutionConflictError("ACK_PAYLOAD_CONFLICT")
        if terminal_status == "CANCELED":
            raise ExecutionConflictError("COMMAND_ALREADY_CANCELED")

        pending_for_robot = sorted(
            [
                state
                for state in states
                if state.get("robot_id") == request.robot_id
                and state.get("status") not in {"ACKED", "FAILED", "CANCELED"}
            ],
            key=lambda state: int(state.get("sequence") or 0),
        )
        if not pending_for_robot or pending_for_robot[0].get("command_id") != request.command_id:
            expected = pending_for_robot[0].get("sequence") if pending_for_robot else None
            raise ExecutionSequenceError(
                f"ACK_OUT_OF_ORDER:{request.robot_id}:{request.sequence}:{expected}"
            )

        target["status"] = request.status
        target["ack_id"] = request.ack_id
        target["acked_at"] = request.occurred_at.isoformat()
        target["error_code"] = request.error_code
        target["error_message"] = request.error_message

        if request.status == "FAILED":
            physical_progress = any(
                state.get("status") == "ACKED"
                and _is_physical_progress(str(state.get("action") or ""))
                for state in states
            )
            handling_progress = any(
                state.get("status") == "ACKED"
                and _is_handling_action(str(state.get("action") or ""))
                for state in states
            )
            cancel_result = self._cancel_gateway(row, reason="COMMAND_FAILED")
            cancel_confirmed = _gateway_cancel_confirmed(cancel_result)
            if cancel_confirmed:
                _set_unfinished_command_status(states, "CANCELED")
            else:
                _set_unfinished_command_status(states, "CANCEL_PENDING")

            if not cancel_confirmed:
                rollback = {
                    "status": "NOT_ATTEMPTED",
                    "restored": False,
                    "previous_active_plan_version": row.get(
                        "previous_active_plan_version"
                    ),
                    "reason": "GATEWAY_CANCEL_UNCONFIRMED",
                }
                dispatch_status = "PARTIAL_FAILURE"
                reason_code = "GATEWAY_CANCEL_UNCONFIRMED"
                retryable = True
                manual_recovery_required = True
            elif physical_progress:
                rollback = {
                    "status": "MANUAL_RECOVERY_REQUIRED",
                    "restored": False,
                    "previous_active_plan_version": row.get(
                        "previous_active_plan_version"
                    ),
                    "reason": "PHYSICAL_PROGRESS_ALREADY_ACKED",
                }
                dispatch_status = "PARTIAL_FAILURE"
                reason_code = "PHYSICAL_PROGRESS_ALREADY_ACKED"
                retryable = False
                manual_recovery_required = True
            else:
                rollback = self._rollback_if_safe(
                    {**row, "command_states": states},
                    reason="COMMAND_FAILED_BEFORE_PHYSICAL_PROGRESS",
                )
                if rollback.get("restored"):
                    dispatch_status = "ROLLED_BACK"
                    reason_code = None
                    retryable = False
                    manual_recovery_required = False
                else:
                    dispatch_status = "PARTIAL_FAILURE"
                    reason_code = "ROLLBACK_FAILED"
                    retryable = True
                    manual_recovery_required = True

            result_summary = {
                "failed_command_id": request.command_id,
                "physical_progress": physical_progress,
                "handling_progress": handling_progress,
                "cancel_result": cancel_result,
                "gateway_cancel_confirmed": cancel_confirmed,
                "rollback": rollback,
                "manual_recovery_required": manual_recovery_required,
                "retryable": retryable,
                "reason_code": reason_code,
            }
        else:
            remaining = [
                state
                for state in states
                if state.get("status") not in {"ACKED", "FAILED", "CANCELED"}
            ]
            failed = [state for state in states if state.get("status") == "FAILED"]
            cancel_unconfirmed = (
                row_status in {"CANCELED_PARTIAL_EXECUTION", "PARTIAL_FAILURE"}
                and str(prior_summary.get("reason_code") or "").upper()
                == "GATEWAY_CANCEL_UNCONFIRMED"
            )
            if cancel_unconfirmed:
                physical_progress = any(
                    state.get("status") == "ACKED"
                    and _is_physical_progress(str(state.get("action") or ""))
                    for state in states
                )
                handling_progress = any(
                    state.get("status") == "ACKED"
                    and _is_handling_action(str(state.get("action") or ""))
                    for state in states
                )
                dispatch_status = row_status
                result_summary = {
                    **prior_summary,
                    "physical_progress": physical_progress,
                    "handling_progress": handling_progress,
                    "manual_recovery_required": True,
                    "retryable": True,
                    "reason_code": "GATEWAY_CANCEL_UNCONFIRMED",
                    "acked_count": len(
                        [state for state in states if state.get("status") == "ACKED"]
                    ),
                    "remaining_count": len(remaining),
                    "last_ack_command_id": request.command_id,
                }
            else:
                if failed:
                    dispatch_status = "PARTIAL_FAILURE"
                elif not remaining:
                    dispatch_status = "COMPLETED"
                else:
                    dispatch_status = "PARTIAL_ACK"
                result_summary = {
                    "acked_count": len(
                        [state for state in states if state.get("status") == "ACKED"]
                    ),
                    "remaining_count": len(remaining),
                }

        self.services.postgres.update_execution_dispatch(
            dispatch_id,
            {
                "status": dispatch_status,
                "command_states": states,
                "result_summary": result_summary,
                "updated_at": datetime.now(UTC),
                "completed_at": (
                    datetime.now(UTC)
                    if dispatch_status in {"COMPLETED", "ROLLED_BACK", "PARTIAL_FAILURE"}
                    and not bool(result_summary.get("retryable"))
                    else None
                ),
            },
        )
        return {
            "status": dispatch_status,
            "dispatch_id": dispatch_id,
            "duplicate": False,
            "command": target,
            "result_summary": result_summary,
            "command_states": states,
        }

    def retry(
        self,
        dispatch_id: str,
        request: ExecutionDispatchRetryRequest,
    ) -> dict[str, Any]:
        del request
        row = self.services.postgres.get_execution_dispatch(dispatch_id)
        if row is None:
            raise ExecutionNotFoundError("DISPATCH_NOT_FOUND")
        status = str(row.get("status") or "").upper()
        if status not in {"DISPATCH_TIMEOUT", "RETRY_EXHAUSTED", "DISPATCHING"}:
            raise ExecutionConflictError(f"DISPATCH_NOT_RETRYABLE:{status}")
        if int(row.get("attempt_count") or 0) >= int(row.get("max_attempts") or 1):
            raise ExecutionRetryExhaustedError("DISPATCH_RETRY_EXHAUSTED")
        return self._send_existing(row, duplicate=True)

    def cancel(
        self,
        dispatch_id: str,
        request: ExecutionDispatchCancelRequest,
    ) -> dict[str, Any]:
        row = self.services.postgres.get_execution_dispatch(dispatch_id)
        if row is None:
            raise ExecutionNotFoundError("DISPATCH_NOT_FOUND")
        prior_status = str(row.get("status") or "").upper()
        if prior_status in {
            "COMPLETED",
            "CANCELED",
            "ROLLED_BACK",
        }:
            return {
                "status": row.get("status"),
                "dispatch_id": dispatch_id,
                "duplicate": True,
            }

        states = deepcopy(row.get("command_states") or [])
        physical_progress = any(
            state.get("status") == "ACKED"
            and _is_physical_progress(str(state.get("action") or ""))
            for state in states
        )
        cancel_result = self._cancel_gateway(row, reason=request.reason)
        cancel_confirmed = _gateway_cancel_confirmed(cancel_result)
        recovery_replay = prior_status in {
            "CANCELED_PARTIAL_EXECUTION",
            "PARTIAL_FAILURE",
        }

        if not cancel_confirmed:
            _set_unfinished_command_status(states, "CANCEL_PENDING")
            rollback = {
                "status": "NOT_ATTEMPTED",
                "restored": False,
                "previous_active_plan_version": row.get(
                    "previous_active_plan_version"
                ),
                "reason": "GATEWAY_CANCEL_UNCONFIRMED",
            }
            status = "CANCELED_PARTIAL_EXECUTION"
            reason_code = "GATEWAY_CANCEL_UNCONFIRMED"
            retryable = True
            manual_recovery_required = True
        else:
            _set_unfinished_command_status(states, "CANCELED")
            if physical_progress:
                rollback = {
                    "status": "MANUAL_RECOVERY_REQUIRED",
                    "restored": False,
                    "previous_active_plan_version": row.get(
                        "previous_active_plan_version"
                    ),
                    "reason": "PHYSICAL_PROGRESS_ALREADY_ACKED",
                }
                status = "CANCELED_PARTIAL_EXECUTION"
                reason_code = "PHYSICAL_PROGRESS_ALREADY_ACKED"
                retryable = False
                manual_recovery_required = True
            else:
                rollback = self._rollback_if_safe(
                    {**row, "command_states": states},
                    reason=f"CANCELED_BY:{request.actor_id}",
                )
                if rollback.get("restored"):
                    status = "ROLLED_BACK"
                    reason_code = None
                    retryable = False
                    manual_recovery_required = False
                else:
                    status = "CANCELED_PARTIAL_EXECUTION"
                    reason_code = "ROLLBACK_FAILED"
                    retryable = True
                    manual_recovery_required = True

        summary = {
            "actor_id": request.actor_id,
            "reason": request.reason,
            "physical_progress": physical_progress,
            "cancel_result": cancel_result,
            "gateway_cancel_confirmed": cancel_confirmed,
            "rollback": rollback,
            "manual_recovery_required": manual_recovery_required,
            "retryable": retryable,
            "reason_code": reason_code,
            "recovery_replay": recovery_replay,
        }
        self.services.postgres.update_execution_dispatch(
            dispatch_id,
            {
                "status": status,
                "command_states": states,
                "result_summary": summary,
                "updated_at": datetime.now(UTC),
                "completed_at": (
                    datetime.now(UTC)
                    if status == "ROLLED_BACK"
                    or (status == "CANCELED_PARTIAL_EXECUTION" and not retryable)
                    else None
                ),
            },
        )
        return {
            "status": status,
            "dispatch_id": dispatch_id,
            "duplicate": False,
            "retryable": retryable,
            "recovery_replay": recovery_replay,
            "result_summary": summary,
            "command_states": states,
        }

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        row = self.services.postgres.get_execution_dispatch(dispatch_id)
        if row is None:
            raise ExecutionNotFoundError("DISPATCH_NOT_FOUND")
        return row

    def _gateway_dispatch_identity(
        self, plan_version: str, batches: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if self.gateway is None or not hasattr(self.gateway, "dispatch_identity"):
            return {}
        try:
            identity = self.gateway.dispatch_identity(
                str(plan_version), deepcopy(batches)
            )
            return {**identity, "precomputed": True}
        except Exception as exc:
            return {
                "identity_precompute_status": "FAILED",
                "identity_precompute_error": str(exc),
            }

    def _cancel_gateway(self, row: dict[str, Any], *, reason: str) -> dict[str, Any]:
        service_dispatch_id = str(row.get("dispatch_id") or "")
        gateway_result = row.get("gateway_result") or {}
        gateway_dispatch_id = str(
            gateway_result.get("dispatch_id") or service_dispatch_id
        )
        identity = {
            "service_dispatch_id": service_dispatch_id,
            "gateway_dispatch_id": gateway_dispatch_id,
        }
        if self.gateway is None or not hasattr(self.gateway, "cancel"):
            return {
                "status": "NOT_SUPPORTED",
                "reason": reason,
                **identity,
            }
        try:
            result = self.gateway.cancel(
                gateway_dispatch_id,
                str(row.get("plan_version")),
                reason=reason,
            )
            return {**result, **identity}
        except Exception as exc:
            return {
                "status": "FAILED",
                "error": str(exc),
                "reason": reason,
                **identity,
            }

    def _release_rolled_back_plan_reservations(
        self,
        *,
        warehouse_id: int,
        plan_version: str,
    ) -> dict[str, Any]:
        updater = getattr(self.services.redis, "update_inventory_reservations", None)
        if not callable(updater):
            return {
                "status": "NOT_SUPPORTED",
                "released_count": 0,
                "plan_version": plan_version,
            }
        try:
            rows = updater(
                warehouse_id,
                plan_version=plan_version,
                from_statuses={"RESERVED"},
                status="RELEASED",
            ) or []
            return {
                "status": "RELEASED" if rows else "NO_CHANGE",
                "released_count": len(rows),
                "plan_version": plan_version,
                "reservations": rows,
            }
        except Exception as exc:
            return {
                "status": "FAILED",
                "released_count": 0,
                "plan_version": plan_version,
                "error": str(exc),
            }

    def _rollback_if_safe(
        self,
        row: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        previous = row.get("previous_active_plan_version")
        plan_version = str(row.get("plan_version") or "")
        warehouse_id = int(row.get("warehouse_id") or 0)
        try:
            restored = self.services.redis.rollback_plan_activation(
                warehouse_id,
                plan_version,
                previous,
            )
            release = (
                self._release_rolled_back_plan_reservations(
                    warehouse_id=warehouse_id,
                    plan_version=plan_version,
                )
                if restored
                else {
                    "status": "NOT_ATTEMPTED",
                    "released_count": 0,
                    "plan_version": plan_version,
                }
            )
            return {
                "status": "RESTORED" if restored else "NO_CHANGE",
                "restored": bool(restored),
                "previous_active_plan_version": previous,
                "reason": reason,
                "inventory_reservation_release": release,
            }
        except Exception as exc:
            return {
                "status": "ROLLBACK_FAILED",
                "restored": False,
                "previous_active_plan_version": previous,
                "reason": reason,
                "inventory_reservation_release": {
                    "status": "NOT_ATTEMPTED",
                    "released_count": 0,
                    "plan_version": plan_version,
                },
                "error": str(exc),
            }

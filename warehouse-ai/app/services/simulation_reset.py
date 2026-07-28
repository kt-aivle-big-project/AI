import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.models import SimulationResetRequest
from app.services.audit import AuditService, sanitize_log_details


logger = logging.getLogger(__name__)


class SimulationNotFoundError(RuntimeError):
    def __init__(self, simulation_id: str, command_id: str):
        super().__init__(f"simulation_id를 찾을 수 없습니다: {simulation_id}")
        self.simulation_id = simulation_id
        self.command_id = command_id


def summarize_simulation_state(
    state: dict[str, Any] | None,
    redis_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state or {}
    redis_summary = redis_summary or {}
    return {
        "work_count": len(state.get("works", [])),
        "robot_count": len(state.get("robots", [])),
        "inventory_record_count": len(state.get("inventory", [])),
        "event_count": int(redis_summary.get("event_count") or 0),
        "checkpoint": redis_summary.get("checkpoint") or state.get("checkpoint"),
    }


class SimulationResetService:
    def __init__(self, services: Any):
        self.services = services

    @staticmethod
    def _stage(
        sequence: int,
        node_name: str,
        *,
        status: str = "SUCCESS",
        details: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return sanitize_log_details(
            {
                "sequence": sequence,
                "node_name": node_name,
                "attempt": 1,
                "status": status,
                "message": message,
                "details": details or {},
                "created_at": datetime.now(UTC),
            }
        )

    def _start_command(
        self,
        *,
        warehouse_id: int,
        simulation_id: str | None,
        actor_id: str | None,
        reason: str,
        target_type: str,
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        command_id = str(uuid4())
        command = {
            "command_id": command_id,
            "warehouse_id": warehouse_id,
            "requested_execution_mode": None,
            "source": "USER",
            "text": f"{target_type} RESET: {reason}",
            "actor_id": actor_id,
            "simulation_id": simulation_id,
            "received_at": datetime.now(UTC),
        }
        stages = [
            self._stage(
                1,
                "COMMAND_RECEIVED",
                details={"actor_id": actor_id, "simulation_id": simulation_id},
            ),
            self._stage(
                2,
                "RESET_REQUESTED",
                details={
                    "actor_id": actor_id,
                    "reason": reason,
                    "simulation_id": simulation_id,
                    "target_type": target_type,
                    "actual_operational_data_changed": False,
                },
            ),
        ]
        warnings: list[str] = []
        try:
            audit = AuditService(self.services.postgres)
            audit.create_or_get_command_history(command)
            audit.persist_stage_logs(command_id, stages)
        except Exception as exc:
            warning = f"RESET 명령 감사 시작 저장 실패: {sanitize_log_details(str(exc))}"
            logger.warning(warning)
            warnings.append(warning)
        return command_id, stages, warnings

    def _finalize_command(
        self,
        *,
        command_id: str,
        simulation_id: str | None,
        status: str,
        result_summary: dict[str, Any],
        error_summary: dict[str, Any] | None,
        stages: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        try:
            self.services.postgres.finalize_command_audit(
                sanitize_log_details(
                    {
                        "command_id": command_id,
                        "command_type": "RESET",
                        "resolved_execution_mode": None,
                        "status": status,
                        "simulation_id": simulation_id,
                        "plan_version": None,
                        "completed_at": datetime.now(UTC),
                        "result_summary": result_summary,
                        "error_summary": error_summary,
                    }
                ),
                sanitize_log_details(stages),
            )
        except Exception as exc:
            warning = f"RESET 명령 감사 완료 저장 실패: {sanitize_log_details(str(exc))}"
            logger.warning(warning)
            warnings.append(warning)

    def _create_reset_audit(
        self,
        *,
        reset_id: str,
        command_id: str,
        warehouse_id: int,
        target_type: str,
        simulation_id: str | None,
        actor_id: str | None,
        reason: str,
        warnings: list[str],
    ) -> None:
        try:
            self.services.postgres.create_reset_audit(
                sanitize_log_details(
                    {
                        "reset_id": reset_id,
                        "command_id": command_id,
                        "warehouse_id": warehouse_id,
                        "target_type": target_type,
                        "target_simulation_id": simulation_id,
                        "actor_id": actor_id,
                        "reason": reason,
                        "status": "PROCESSING",
                        "affected_simulation_count": 0,
                        "before_summary": None,
                        "after_summary": None,
                        "failure_summary": None,
                        "created_at": datetime.now(UTC),
                        "completed_at": None,
                    }
                )
            )
        except Exception as exc:
            warning = f"RESET audit 시작 저장 실패: {sanitize_log_details(str(exc))}"
            logger.warning(warning)
            warnings.append(warning)

    def _finalize_reset_audit(
        self,
        reset_id: str,
        *,
        status: str,
        affected_count: int,
        before_summary: dict[str, Any] | None,
        after_summary: dict[str, Any] | None,
        failure_summary: dict[str, Any] | None,
        warnings: list[str],
    ) -> None:
        try:
            self.services.postgres.finalize_reset_audit(
                reset_id,
                sanitize_log_details(
                    {
                        "status": status,
                        "affected_simulation_count": affected_count,
                        "before_summary": before_summary,
                        "after_summary": after_summary,
                        "failure_summary": failure_summary,
                        "completed_at": datetime.now(UTC),
                    }
                ),
            )
        except Exception as exc:
            warning = f"RESET audit 완료 저장 실패: {sanitize_log_details(str(exc))}"
            logger.warning(warning)
            warnings.append(warning)

    def _redis_summary(self, simulation_id: str) -> dict[str, Any]:
        try:
            return self.services.redis.simulation_state_summary(simulation_id)
        except Exception:
            return {}

    def _reset_one_core(
        self,
        *,
        session: dict[str, Any],
        command_id: str,
        actor_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        simulation_id = str(session["simulation_id"])
        warehouse_id = int(session["warehouse_id"])
        before_summary = summarize_simulation_state(
            session.get("current_state"),
            self._redis_summary(simulation_id),
        )
        pending = self.services.postgres.mark_simulation_reset_pending(
            simulation_id,
            command_id,
        )
        if pending is None:
            latest = self.services.postgres.get_simulation_session(simulation_id)
            if latest and latest.get("status") == "RESET":
                return {
                    "status": "ALREADY_RESET",
                    "simulation_id": simulation_id,
                    "warehouse_id": warehouse_id,
                    "before_summary": before_summary,
                    "after_summary": {
                        "session_status": "RESET",
                        "redis_state_exists": False,
                    },
                    "affected_redis_keys": [],
                    "deleted_redis_key_count": 0,
                }
            raise RuntimeError(f"RESET_PENDING 전환 실패: {simulation_id}")

        try:
            redis_result = self.services.redis.remove_simulation_state(
                warehouse_id,
                simulation_id,
            )
            reset_at = datetime.now(UTC)
            self.services.postgres.complete_simulation_reset(
                simulation_id=simulation_id,
                status="RESET",
                command_id=command_id,
                actor_id=actor_id,
                reason=reason,
                reset_at=reset_at,
            )
            return {
                "status": "RESET_COMPLETED",
                "simulation_id": simulation_id,
                "warehouse_id": warehouse_id,
                "before_summary": before_summary,
                "after_summary": {
                    "session_status": "RESET",
                    "redis_state_exists": False,
                },
                "affected_redis_keys": redis_result["affected_redis_keys"],
                "deleted_redis_key_count": redis_result[
                    "deleted_redis_key_count"
                ],
                "reset_at": reset_at,
            }
        except Exception as exc:
            reset_at = datetime.now(UTC)
            try:
                self.services.postgres.complete_simulation_reset(
                    simulation_id=simulation_id,
                    status="RESET_FAILED",
                    command_id=command_id,
                    actor_id=actor_id,
                    reason=reason,
                    reset_at=reset_at,
                )
            except Exception as state_exc:
                logger.warning(
                    "RESET_FAILED 상태 저장 실패: %s",
                    sanitize_log_details(str(state_exc)),
                )
            return {
                "status": "RESET_FAILED",
                "simulation_id": simulation_id,
                "warehouse_id": warehouse_id,
                "before_summary": before_summary,
                "after_summary": {
                    "session_status": "RESET_FAILED",
                    "redis_state_exists": True,
                },
                "affected_redis_keys": [],
                "deleted_redis_key_count": 0,
                "failure": sanitize_log_details(str(exc)),
                "reset_at": reset_at,
            }

    def reset_simulation(
        self,
        simulation_id: str,
        request: SimulationResetRequest,
    ) -> dict[str, Any]:
        safe_reason = str(sanitize_log_details(request.reason))
        safe_actor = (
            str(sanitize_log_details(request.actor_id))
            if request.actor_id is not None
            else None
        )
        session = self.services.postgres.get_simulation_session(simulation_id)
        warehouse_id = (
            int(session["warehouse_id"])
            if session
            else int(request.warehouse_id or 0)
        )
        command_id, stages, warnings = self._start_command(
            warehouse_id=warehouse_id,
            simulation_id=simulation_id,
            actor_id=safe_actor,
            reason=safe_reason,
            target_type="SIMULATION",
        )
        reset_id = str(uuid4())
        self._create_reset_audit(
            reset_id=reset_id,
            command_id=command_id,
            warehouse_id=warehouse_id,
            target_type="SIMULATION",
            simulation_id=simulation_id,
            actor_id=safe_actor,
            reason=safe_reason,
            warnings=warnings,
        )

        if session is None:
            failure = {"error": f"simulation_id를 찾을 수 없습니다: {simulation_id}"}
            stages.extend(
                [
                    self._stage(3, "RESET_FAILED", status="FAILED", details=failure),
                    self._stage(4, "COMMAND_FAILED", status="FAILED", details=failure),
                ]
            )
            self._finalize_reset_audit(
                reset_id,
                status="FAILED",
                affected_count=0,
                before_summary=None,
                after_summary=None,
                failure_summary=failure,
                warnings=warnings,
            )
            self._finalize_command(
                command_id=command_id,
                simulation_id=simulation_id,
                status="FAILED",
                result_summary={"status": "RESET_FAILED"},
                error_summary=failure,
                stages=stages,
                warnings=warnings,
            )
            raise SimulationNotFoundError(simulation_id, command_id)

        stages.append(
            self._stage(
                3,
                "RESET_VALIDATED",
                details={"simulation_id": simulation_id, "warehouse_id": warehouse_id},
            )
        )
        if session.get("status") == "RESET":
            result = {
                "status": "ALREADY_RESET",
                "reset_id": reset_id,
                "command_id": command_id,
                "warehouse_id": warehouse_id,
                "simulation_id": simulation_id,
                "actor_id": safe_actor,
                "reason": safe_reason,
                "affected_redis_keys": [],
                "deleted_redis_key_count": 0,
                "actual_operational_data_changed": False,
                "logs_preserved": True,
                "audit_warnings": warnings,
            }
            stages.extend(
                [
                    self._stage(4, "SIMULATION_RESET", details=result),
                    self._stage(5, "COMMAND_COMPLETED", details=result),
                ]
            )
            self._finalize_reset_audit(
                reset_id,
                status="ALREADY_RESET",
                affected_count=0,
                before_summary=None,
                after_summary={"session_status": "RESET"},
                failure_summary=None,
                warnings=warnings,
            )
            self._finalize_command(
                command_id=command_id,
                simulation_id=simulation_id,
                status="SUCCESS",
                result_summary=result,
                error_summary=None,
                stages=stages,
                warnings=warnings,
            )
            return result

        core = self._reset_one_core(
            session=session,
            command_id=command_id,
            actor_id=safe_actor,
            reason=safe_reason,
        )
        stages.append(
            self._stage(
                4,
                "RESET_STATE_CAPTURED",
                details={
                    "simulation_id": simulation_id,
                    "before_summary": core["before_summary"],
                },
            )
        )
        success = core["status"] == "RESET_COMPLETED"
        if success:
            stages.extend(
                [
                    self._stage(
                        5,
                        "REDIS_SIMULATION_STATE_REMOVED",
                        details={
                            "simulation_id": simulation_id,
                            "redis_deleted_keys": core["affected_redis_keys"],
                            "actual_operational_data_changed": False,
                        },
                    ),
                    self._stage(6, "SIMULATION_RESET", details=core),
                    self._stage(7, "COMMAND_COMPLETED", details=core),
                ]
            )
        else:
            stages.extend(
                [
                    self._stage(5, "RESET_FAILED", status="FAILED", details=core),
                    self._stage(6, "COMMAND_FAILED", status="FAILED", details=core),
                ]
            )
        response = {
            **core,
            "reset_id": reset_id,
            "command_id": command_id,
            "actor_id": safe_actor,
            "reason": safe_reason,
            "actual_operational_data_changed": False,
            "logs_preserved": True,
            "audit_warnings": warnings,
        }
        failure = {"error": core.get("failure")} if not success else None
        self._finalize_reset_audit(
            reset_id,
            status="SUCCESS" if success else "FAILED",
            affected_count=1 if success else 0,
            before_summary=core["before_summary"],
            after_summary=core["after_summary"],
            failure_summary=failure,
            warnings=warnings,
        )
        self._finalize_command(
            command_id=command_id,
            simulation_id=simulation_id,
            status="SUCCESS" if success else "FAILED",
            result_summary=response,
            error_summary=failure,
            stages=stages,
            warnings=warnings,
        )
        response["audit_warnings"] = warnings
        return response

    def reset_all_simulations(
        self,
        warehouse_id: int,
        request: SimulationResetRequest,
    ) -> dict[str, Any]:
        safe_reason = str(sanitize_log_details(request.reason))
        safe_actor = (
            str(sanitize_log_details(request.actor_id))
            if request.actor_id is not None
            else None
        )
        command_id, stages, warnings = self._start_command(
            warehouse_id=warehouse_id,
            simulation_id=None,
            actor_id=safe_actor,
            reason=safe_reason,
            target_type="ALL_SIMULATIONS",
        )
        reset_id = str(uuid4())
        self._create_reset_audit(
            reset_id=reset_id,
            command_id=command_id,
            warehouse_id=warehouse_id,
            target_type="ALL_SIMULATIONS",
            simulation_id=None,
            actor_id=safe_actor,
            reason=safe_reason,
            warnings=warnings,
        )
        sessions = self.services.postgres.list_resettable_simulation_sessions(
            warehouse_id
        )
        stages.extend(
            [
                self._stage(
                    3,
                    "RESET_VALIDATED",
                    details={"warehouse_id": warehouse_id},
                ),
                self._stage(
                    4,
                    "RESET_TARGETS_SELECTED",
                    details={
                        "warehouse_id": warehouse_id,
                        "simulation_ids": [row["simulation_id"] for row in sessions],
                    },
                ),
            ]
        )
        if not sessions:
            response = {
                "status": "NO_ACTIVE_SIMULATIONS",
                "reset_id": reset_id,
                "command_id": command_id,
                "warehouse_id": warehouse_id,
                "affected_simulation_count": 0,
                "success_simulation_ids": [],
                "failed_simulations": [],
                "deleted_redis_key_count": 0,
                "actual_operational_data_changed": False,
                "logs_preserved": True,
                "audit_warnings": warnings,
            }
            stages.extend(
                [
                    self._stage(5, "ALL_SIMULATIONS_RESET", details=response),
                    self._stage(6, "COMMAND_COMPLETED", details=response),
                ]
            )
            self._finalize_reset_audit(
                reset_id,
                status="SUCCESS",
                affected_count=0,
                before_summary={"target_count": 0},
                after_summary={"reset_count": 0},
                failure_summary=None,
                warnings=warnings,
            )
            self._finalize_command(
                command_id=command_id,
                simulation_id=None,
                status="SUCCESS",
                result_summary=response,
                error_summary=None,
                stages=stages,
                warnings=warnings,
            )
            return response

        successes: list[str] = []
        failures: list[dict[str, Any]] = []
        deleted_count = 0
        before: dict[str, Any] = {}
        sequence = 5
        for session in sessions:
            core = self._reset_one_core(
                session=session,
                command_id=command_id,
                actor_id=safe_actor,
                reason=safe_reason,
            )
            simulation_id = str(session["simulation_id"])
            before[simulation_id] = core["before_summary"]
            deleted_count += int(core["deleted_redis_key_count"])
            if core["status"] in {"RESET_COMPLETED", "ALREADY_RESET"}:
                successes.append(simulation_id)
                stage_status = "SUCCESS"
            else:
                failures.append(
                    {
                        "simulation_id": simulation_id,
                        "error": core.get("failure"),
                    }
                )
                stage_status = "FAILED"
            stages.append(
                self._stage(
                    sequence,
                    "SIMULATION_RESET",
                    status=stage_status,
                    details=core,
                )
            )
            sequence += 1

        if not failures:
            response_status = "RESET_ALL_COMPLETED"
            audit_status = "SUCCESS"
            command_status = "SUCCESS"
        elif successes:
            response_status = "RESET_ALL_PARTIAL"
            audit_status = "PARTIAL_SUCCESS"
            command_status = "PARTIAL_SUCCESS"
        else:
            response_status = "RESET_ALL_FAILED"
            audit_status = "FAILED"
            command_status = "FAILED"
        response = {
            "status": response_status,
            "reset_id": reset_id,
            "command_id": command_id,
            "warehouse_id": warehouse_id,
            "affected_simulation_count": len(sessions),
            "success_simulation_ids": successes,
            "failed_simulations": failures,
            "deleted_redis_key_count": deleted_count,
            "actual_operational_data_changed": False,
            "logs_preserved": True,
            "audit_warnings": warnings,
        }
        stages.extend(
            [
                self._stage(
                    sequence,
                    "ALL_SIMULATIONS_RESET",
                    status="FAILED" if audit_status == "FAILED" else "SUCCESS",
                    details=response,
                ),
                self._stage(
                    sequence + 1,
                    "COMMAND_FAILED" if command_status == "FAILED" else "COMMAND_COMPLETED",
                    status=command_status,
                    details=response,
                ),
            ]
        )
        failure_summary = {"failed_simulations": failures} if failures else None
        self._finalize_reset_audit(
            reset_id,
            status=audit_status,
            affected_count=len(sessions),
            before_summary=before,
            after_summary={"reset_simulation_ids": successes},
            failure_summary=failure_summary,
            warnings=warnings,
        )
        self._finalize_command(
            command_id=command_id,
            simulation_id=None,
            status=command_status,
            result_summary=response,
            error_summary=failure_summary,
            stages=stages,
            warnings=warnings,
        )
        response["audit_warnings"] = warnings
        return response

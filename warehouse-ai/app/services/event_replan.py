"""Idempotent event-driven replanning with explicit approval before execution."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from app.execution import handle_robot_event
from app.models import (
    EventImpactAnalysis,
    EventReplanDecisionRequest,
    NaturalLanguageCommand,
    RobotEvent,
    ScenarioDefinition,
)
from app.planning.graph import run_planning
from app.services.event_impact import analyze_event_impact
from app.services.event_safety import payload_identity_evidence
from app.services.runtime_authority import (
    RuntimeAuthorityError,
    bind_runtime_context,
    derive_low_battery_event,
    resolve_runtime_context,
)


FINAL_REPLAN_STATUSES = {
    "REPLAN_NOT_REQUIRED",
    "REPLAN_VERIFIED",
    "APPROVAL_REQUIRED",
    "APPROVED",
    "REJECTED",
    "EXECUTED",
    "FAILED",
    "STALE_PLAN",
    "MANUAL_RECOVERY_REQUIRED",
}

REPLAN_TRIGGER_EVENTS = {
    "ROBOT_DELAYED",
    "ROBOT_FAILED",
    "LOW_BATTERY",
    "PATH_BLOCKED",
    "PATH_DEVIATED",
    "TASK_FAILED",
}

# A repeated local route failure is allowed to expand once to a global
# candidate. Other repeated operational failures keep the existing stop guard.
ROUTE_FAILURE_ESCALATION_EVENTS = {
    "PATH_BLOCKED",
    "PATH_DEVIATED",
}

SQL_COMMIT_EVENTS = {
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "INBOUND_AVAILABLE",
}


class EventReplanConflictError(RuntimeError):
    pass


class EventReplanNotFoundError(RuntimeError):
    pass


def _scenario_for_event(
    event: RobotEvent,
    impact: EventImpactAnalysis,
) -> ScenarioDefinition:
    excluded_robots = (
        [event.robot_id] if event.event_type == "ROBOT_FAILED" else []
    )
    excluded_nodes = (
        impact.affected_node_ids
        if event.event_type == "PATH_BLOCKED"
        and not impact.affected_edge_ids
        else []
    )
    runtime = event.payload.get("_server_runtime") or {}
    source_plan = deepcopy(runtime.get("active_plan") or {})
    if source_plan:
        source_plan.setdefault("plan_version", impact.active_plan_version)
        if event.execution_context == "SIMULATION":
            source_plan["base_plan_is_simulated"] = True
            source_plan["candidate_plan"] = True
            source_plan["execution_mode"] = "SIMULATE_ONLY"
    scheduled = list((source_plan.get("cuopt_plan") or {}).get("scheduled_tasks") or [])
    protected = set(impact.frozen_task_ids)
    fixed_assignments = {
        str(row["task_id"]): str(row["robot_id"])
        for row in scheduled
        if row.get("task_id") in protected and row.get("robot_id")
    }
    if event.event_type == "LOW_BATTERY":
        battery = event.battery
        if battery is None and event.payload.get("battery") is not None:
            battery = float(event.payload["battery"])
        minimum = float(event.payload.get("minimum_battery") or 20.0)
        recoverable_on_same_robot = bool(
            event.payload.get("server_derived")
            and battery is not None
            and float(battery) > minimum
        )
        if recoverable_on_same_robot:
            # The task timing must be changeable so CHARGE can be inserted
            # before the current mission, but the active pickup/drop chain must
            # remain on the same robot while its battery is still above the
            # hard emergency-stop threshold.
            affected = set(impact.affected_task_ids)
            for row in scheduled:
                task_id = str(row.get("task_id") or "")
                robot_id = str(row.get("robot_id") or "")
                if task_id in affected and robot_id == str(event.robot_id):
                    fixed_assignments[task_id] = robot_id
    hypothetical_events: list[dict[str, Any]] = []
    if event.event_type == "LOW_BATTERY":
        battery = event.battery
        if battery is None and event.payload.get("battery") is not None:
            battery = float(event.payload["battery"])
        hypothetical_events.append(
            {
                "event_type": "LOW_BATTERY",
                "target_ids": [event.robot_id],
                "parameters": {"battery_percent": battery},
            }
        )
    return ScenarioDefinition(
        scenario_id=f"event-{event.event_id}",
        name=f"{event.event_type} 실시간 부분 재계획",
        description=(
            "완료 작업과 freeze horizon을 고정하고 실시간 로봇 상태를 "
            "반영해 변경 가능 작업만 다시 계획합니다."
        ),
        excluded_robot_ids=excluded_robots,
        excluded_node_ids=excluded_nodes,
        excluded_edge_ids=(
            impact.affected_edge_ids
            if event.event_type == "PATH_BLOCKED"
            else []
        ),
        fixed_robot_assignments=fixed_assignments,
        hypothetical_events=hypothetical_events,
        source_plan_version=impact.active_plan_version,
        source_plan_snapshot=source_plan or None,
        affected_robot_ids=impact.affected_robot_ids,
        affected_task_ids=impact.affected_task_ids,
        protected_task_ids=impact.frozen_task_ids,
        changeable_task_ids=impact.changeable_task_ids,
        freeze_horizon_seconds=impact.freeze_horizon_seconds,
        robot_state_overrides=impact.robot_state_overrides,
        robot_failure_recovery=deepcopy(impact.robot_failure_recovery),
        recovery_tasks=deepcopy(
            impact.robot_failure_recovery.get("recovery_tasks") or []
        ),
        recovery_replace_task_ids=list(
            impact.robot_failure_recovery.get("replace_task_ids") or []
        ),
    )


def _server_derived_event_evidence(
    reported_event: RobotEvent,
    effective_event: RobotEvent,
) -> dict[str, Any]:
    if reported_event.event_type == effective_event.event_type:
        return {}
    keys = (
        "derived_from_event_type",
        "battery_detection_policy",
        "low_battery_threshold",
        "minimum_battery",
        "battery_safety_margin_percent",
        "remaining_planned_energy_percent",
    )
    evidence = {
        key: effective_event.payload.get(key)
        for key in keys
        if effective_event.payload.get(key) is not None
    }
    if effective_event.battery is not None:
        evidence["reported_battery"] = float(effective_event.battery)
    return evidence


def _replan_text(event: RobotEvent, impact: EventImpactAnalysis) -> str:
    scope = (
        "전체 재계획"
        if impact.recommended_scope == "GLOBAL_REPLAN"
        else "로컬 재계획"
    )
    targets = []
    if impact.affected_robot_ids:
        targets.append("로봇 " + ", ".join(impact.affected_robot_ids))
    if impact.affected_task_ids:
        targets.append("작업 " + ", ".join(impact.affected_task_ids))
    target_text = " / ".join(targets) or "영향 범위"
    changeable = ", ".join(impact.changeable_task_ids) or "없음"
    protected = ", ".join(impact.frozen_task_ids) or "없음"
    recovery = impact.robot_failure_recovery or {}
    recovery_text = (
        f" 로봇 고장 복구 전략은 {recovery.get('strategy')}이며 "
        f"복구 작업 {recovery.get('recovery_task_ids') or []}을 사용하세요."
        if recovery
        else ""
    )
    return (
        f"운영 이벤트 {event.event_type}에 대해 {target_text}를 대상으로 {scope}하고, "
        f"변경 가능 작업 [{changeable}]만 재계획하세요. "
        f"보호 작업 [{protected}]과 완료 구간, freeze horizon "
        f"{impact.freeze_horizon_seconds}초는 유지하세요.{recovery_text} "
        "실제 반영하지 말고 시뮬레이션해줘"
    )


def _planning_failure_debug(planning_response: dict[str, Any]) -> dict[str, Any]:
    """Return bounded optimizer/routing evidence for failed auto replans."""

    data = planning_response.get("data") or {}
    cuopt_plan = planning_response.get("cuopt_plan") or data.get("cuopt_plan") or {}
    metadata = cuopt_plan.get("metadata") or {}
    optimizer_execution = (
        planning_response.get("optimizer_execution")
        or data.get("optimizer_execution")
        or metadata.get("optimizer_execution")
        or {}
    )
    scheduled = list(cuopt_plan.get("scheduled_tasks") or [])
    charge_tasks = [
        {
            key: row.get(key)
            for key in (
                "task_id",
                "work_id",
                "robot_id",
                "action",
                "source_node",
                "target_node",
                "start_time_step",
                "end_time_step",
                "charge_target_battery",
                "charged_percent",
            )
        }
        for row in scheduled
        if str(row.get("action") or "").upper() == "CHARGE"
    ]
    assignment_application = (
        metadata.get("cuopt_assignment_application")
        or (data.get("optimizer_postprocessing") or {}).get(
            "cuopt_assignment_application"
        )
        or {}
    )
    trace_rows = list(planning_response.get("trace") or data.get("trace") or [])
    return {
        "optimizer_execution": optimizer_execution,
        "charge_visit_two_pass": optimizer_execution.get(
            "charge_visit_two_pass"
        ),
        "low_battery_charge_retention": (
            optimizer_execution.get("low_battery_charge_retention")
            or metadata.get("low_battery_charge_retention")
        ),
        "scheduled_charge_tasks": charge_tasks,
        "changeable_robot_bound_task_ids": assignment_application.get(
            "changeable_robot_bound_task_ids", []
        ),
        "route_energy_reconciliation": (
            planning_response.get("route_energy_reconciliation")
            or data.get("route_energy_reconciliation")
            or metadata.get("route_energy_reconciliation")
            or {}
        ),
        "schedule_validation": (
            planning_response.get("schedule_validation")
            or data.get("schedule_validation")
            or {}
        ),
        "stale_route_eviction": _stale_route_eviction_evidence(
            planning_response
        ),
        "trace_tail": trace_rows[-12:],
    }


def _stale_route_eviction_evidence(
    planning_response: dict[str, Any],
) -> dict[str, Any]:
    """Return bounded evidence that failed/excluded active routes were removed."""

    data = planning_response.get("data") or {}
    collision_plan = (
        planning_response.get("collision_plan")
        or data.get("collision_plan")
        or {}
    )
    metadata = collision_plan.get("metadata") or {}
    evidence = metadata.get("stale_route_eviction") or {}
    return deepcopy(evidence) if isinstance(evidence, dict) else {}


def _robot_failure_recovery_result(
    planning_response: dict[str, Any],
    impact: EventImpactAnalysis,
) -> dict[str, Any]:
    recovery = impact.robot_failure_recovery or {}
    if not recovery:
        return {}
    data = planning_response.get("data") or {}
    cuopt_plan = planning_response.get("cuopt_plan") or data.get("cuopt_plan") or {}
    scheduled = list(cuopt_plan.get("scheduled_tasks") or data.get("scheduled_tasks") or [])
    if not scheduled:
        return {
            "version": "p16.5.14",
            "strategy": str(recovery.get("strategy") or ""),
            "status": "NOT_EVALUATED",
            "reason": "PLANNING_RESPONSE_DID_NOT_INCLUDE_SCHEDULED_TASKS",
            "errors": [],
        }
    by_id = {
        str(row.get("task_id")): row
        for row in scheduled
        if isinstance(row, dict) and row.get("task_id")
    }
    strategy = str(recovery.get("strategy") or "")
    failed_robot_id = str(recovery.get("failed_robot_id") or "")
    expected_ids = [str(value) for value in recovery.get("recovery_task_ids") or []]
    if strategy == "HANDOVER_SECURED_LOAD":
        rows = [by_id.get(task_id) for task_id in expected_ids]
        missing = [task_id for task_id, row in zip(expected_ids, rows) if row is None]
        robot_ids = {
            str(row.get("robot_id"))
            for row in rows
            if isinstance(row, dict) and row.get("robot_id")
        }
        same_replacement = len(robot_ids) == 1 and failed_robot_id not in robot_ids
        ordered = False
        if len(rows) == 2 and all(isinstance(row, dict) for row in rows):
            ordered = int(rows[0].get("end_time_step") or 0) <= int(
                rows[1].get("start_time_step") or 0
            )
        valid = not missing and same_replacement and ordered
        return {
            "version": "p16.5.14",
            "strategy": strategy,
            "status": "PASS" if valid else "FAIL",
            "failed_robot_id": failed_robot_id,
            "expected_recovery_task_ids": expected_ids,
            "missing_recovery_task_ids": missing,
            "replacement_robot_ids": sorted(robot_ids),
            "same_replacement_robot": same_replacement,
            "handover_order_valid": ordered,
            "assignments": [
                {
                    "task_id": str(row.get("task_id")),
                    "action": row.get("action"),
                    "robot_id": row.get("robot_id"),
                    "source_node": row.get("source_node"),
                    "target_node": row.get("target_node"),
                    "start_time_step": row.get("start_time_step"),
                    "end_time_step": row.get("end_time_step"),
                }
                for row in rows
                if isinstance(row, dict)
            ],
            "errors": [] if valid else ["ROBOT_FAILURE_HANDOVER_RETENTION_FAILED"],
        }
    if strategy == "REASSIGN_UNPICKED_CHAIN":
        affected = [str(value) for value in impact.changeable_task_ids]
        rows = [by_id[task_id] for task_id in affected if task_id in by_id]
        assigned = {str(row.get("robot_id")) for row in rows if row.get("robot_id")}
        valid = bool(rows) and failed_robot_id not in assigned
        return {
            "version": "p16.5.14",
            "strategy": strategy,
            "status": "PASS" if valid else "FAIL",
            "failed_robot_id": failed_robot_id,
            "replacement_robot_ids": sorted(assigned),
            "reassigned_task_ids": [str(row.get("task_id")) for row in rows],
            "errors": [] if valid else ["ROBOT_FAILURE_REASSIGNMENT_FAILED"],
        }
    return {
        "version": "p16.5.14",
        "strategy": strategy,
        "status": recovery.get("status") or "NOT_APPLICABLE",
        "errors": [],
    }


class EventReplanService:
    def __init__(
        self,
        services: Any,
        *,
        planner: Callable[[NaturalLanguageCommand], dict[str, Any]] = run_planning,
        event_handler: Callable[..., dict[str, Any]] = handle_robot_event,
        impact_analyzer: Callable[[RobotEvent, Any], EventImpactAnalysis] = analyze_event_impact,
    ):
        self.services = services
        self.planner = planner
        self.event_handler = event_handler
        self.impact_analyzer = impact_analyzer

    @staticmethod
    def _result_from_stored(row: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(row.get("result_summary") or {})
        event_type = str(row.get("event_type") or "").upper()
        stored_status = str(row.get("status") or "").upper()
        final_status = str(result.get("final_status") or "").upper()
        commit_result = result.get("commit_result")
        if not isinstance(commit_result, dict):
            commit_result = {}

        # Legacy rows can contain an old response status even though the
        # completion transaction was committed. A duplicate must describe the
        # persisted result, never retry the transaction to discover it.
        completed = event_type == "TASK_COMPLETED" and (
            final_status == "COMPLETED"
            or stored_status == "COMPLETED"
            or commit_result.get("committed") is True
        )
        if completed:
            result["status"] = "COMPLETED"
            result["final_status"] = "COMPLETED"
            result["sql_committed"] = True
            result["commit_result"] = {
                **commit_result,
                "committed": True,
                "idempotent_replay": True,
            }
        else:
            result.setdefault("status", row.get("status"))
            result.setdefault("final_status", row.get("status"))

        result.setdefault("event_id", row.get("event_id"))
        result.setdefault("replan_request_id", row.get("replan_request_id"))
        result["duplicate"] = True
        return result

    @staticmethod
    def _execution_result_status(
        event: RobotEvent,
        execution_result: dict[str, Any],
    ) -> str:
        if execution_result.get("stale_event_ignored") is True:
            return "STALE_EVENT_IGNORED"
        errors = [
            value for value in (execution_result.get("errors") or []) if value
        ]
        final_status = str(execution_result.get("final_status") or "").upper()
        redis_updated = execution_result.get("redis_updated") is True
        sql_committed = execution_result.get("sql_committed") is True
        validation_failed = (
            execution_result.get("valid") is False
            or execution_result.get("validation_failed") is True
            or (
                isinstance(execution_result.get("validation_result"), dict)
                and execution_result["validation_result"].get("valid") is False
            )
        )
        failed_final_statuses = {
            "FAILED",
            "LIVE_UPDATE_FAILED",
            "COMMIT_FAILED",
            "SCHEDULE_TRANSITION_FAILED",
            "EVENT_IMPACT_ANALYSIS_FAILED",
            "VALIDATION_FAILED",
        }
        if (
            errors
            or not redis_updated
            or validation_failed
            or (
                event.event_type in SQL_COMMIT_EVENTS
                and not sql_committed
            )
            or final_status in failed_final_statuses
        ):
            return "FAILED"
        return final_status or "SUCCESS"

    def _finalize_event(
        self,
        event: RobotEvent,
        *,
        status: str,
        result: dict[str, Any],
        impact: EventImpactAnalysis | None = None,
        request_id: str | None = None,
        command_id: str | None = None,
        plan_version: str | None = None,
    ) -> dict[str, Any]:
        repository = self.services.postgres
        if hasattr(repository, "finalize_execution_event_processing"):
            repository.finalize_execution_event_processing(
                event.event_id,
                {
                    "status": status,
                    "impact_summary": impact.model_dump(mode="json") if impact else {},
                    "failure_signature": impact.failure_signature if impact else None,
                    "generated_command_id": command_id,
                    "generated_plan_version": plan_version,
                    "replan_request_id": request_id,
                    "approval_required": impact.approval_required if impact else False,
                    "result_summary": result,
                    "processed_at": datetime.now(UTC),
                },
            )
        return result

    def _persist_event_stages(
        self,
        command_id: str,
        stages: list[tuple[str, dict[str, Any]]],
    ) -> None:
        repository = self.services.postgres
        if not hasattr(repository, "persist_stage_logs"):
            return
        try:
            repository.persist_stage_logs(
                command_id,
                [
                {
                    "sequence": 10_000 + index,
                    "node_name": name,
                    "attempt": 1,
                    "status": (
                        "FAILED"
                        if name == "EVENT_REPLAN_FAILED"
                        else "SUCCESS"
                    ),
                    "message": None,
                    "details": details,
                    "created_at": datetime.now(UTC),
                }
                    for index, (name, details) in enumerate(stages, start=1)
                ],
            )
        except Exception:
            # 단계 로그는 감사 보조 정보이며 검증된 재계획 결과를 실패시키지 않는다.
            return

    @staticmethod
    def _in_progress_duplicate(row: dict[str, Any]) -> bool:
        return (
            str(row.get("status") or "").upper() in {"RECEIVED", "PROCESSING"}
            and not bool(row.get("result_summary"))
        )

    @staticmethod
    def _recoverable_stored_event(row: dict[str, Any]) -> bool:
        result = row.get("result_summary") or {}
        event_type = str(row.get("event_type") or "").upper()
        return (
            event_type in SQL_COMMIT_EVENTS
            and bool(result.get("recovery_required"))
            and bool(result.get("retryable", True))
            and not bool(result.get("auto_replan_requested"))
        ) or (
            str(result.get("final_status") or "").upper()
            == "SIMULATION_CHECKPOINT_FAILED"
            and bool(result.get("retryable", True))
        )

    def _plan_guard(self, event: RobotEvent) -> dict[str, Any] | None:
        if event.execution_context != "SIMULATION" or not event.simulation_id:
            return None
        try:
            snapshot = self.services.redis.simulation_snapshot(event.simulation_id)
        except Exception:
            return None
        return {
            "simulation_id": event.simulation_id,
            "active_plan_version": snapshot.get("active_plan_version"),
            "snapshot": deepcopy(snapshot),
        }

    def _restore_plan_guard(
        self,
        event: RobotEvent,
        guard: dict[str, Any] | None,
        *,
        reason: str,
    ) -> dict[str, Any]:
        evidence = {
            "policy": "FAILED_REPLAN_RETAINS_LAST_VERIFIED_PLAN",
            "required": False,
            "restored": False,
            "reason": reason,
            "previous_plan_version": (guard or {}).get("active_plan_version"),
            "observed_plan_version": None,
        }
        if not guard or not event.simulation_id:
            evidence["status"] = "NOT_APPLICABLE"
            return evidence
        try:
            current = self.services.redis.simulation_snapshot(event.simulation_id)
            evidence["observed_plan_version"] = current.get("active_plan_version")
            if current.get("active_plan_version") == guard.get("active_plan_version"):
                evidence["status"] = "PREVIOUS_PLAN_RETAINED"
                return evidence
        except Exception:
            current = None
        evidence["required"] = True
        restorer = getattr(self.services.redis, "restore_simulation_snapshot", None)
        if not callable(restorer):
            evidence["status"] = "RESTORE_UNAVAILABLE"
            return evidence
        try:
            restored = restorer(
                event.simulation_id,
                guard["snapshot"],
                event=event,
                reason=reason,
            )
            durable_checkpoint = None
            if hasattr(self.services.postgres, "update_simulation_checkpoint"):
                snapshot = self.services.redis.simulation_snapshot(event.simulation_id)
                durable_checkpoint = self.services.postgres.update_simulation_checkpoint(
                    event, snapshot, str(snapshot.get("checkpoint") or "")
                )
            evidence.update(
                {
                    "restored": True,
                    "status": "RESTORED",
                    "restore_result": restored,
                    "durable_checkpoint": durable_checkpoint,
                }
            )
        except Exception as exc:
            evidence.update({"status": "RESTORE_FAILED", "error": str(exc)})
        return evidence

    def handle(self, event: RobotEvent) -> dict[str, Any]:
        repository = self.services.postgres
        reported_event = event
        recovery_replay = False
        if hasattr(repository, "get_execution_event_processing"):
            existing = repository.get_execution_event_processing(event.event_id)
            if existing:
                identity = payload_identity_evidence(
                    event, existing.get("event_payload") or {}
                )
                if not identity["match"]:
                    return {
                        "event_id": event.event_id,
                        "duplicate": True,
                        "retryable": False,
                        "status": "REJECTED",
                        "final_status": "EVENT_ID_PAYLOAD_CONFLICT",
                        "failure_reason": "EVENT_ID_PAYLOAD_CONFLICT",
                        "payload_identity": identity,
                        "errors": [
                            "같은 event_id에 서로 다른 이벤트 본문을 사용할 수 없습니다."
                        ],
                    }
                if self._in_progress_duplicate(existing):
                    return {
                        "event_id": event.event_id,
                        "duplicate": True,
                        "retryable": True,
                        "status": "PROCESSING",
                        "final_status": "EVENT_PROCESSING",
                        "payload_identity": identity,
                        "errors": [],
                    }
                if self._recoverable_stored_event(existing):
                    recovery_replay = True
                else:
                    command_id = existing.get("generated_command_id")
                    if command_id and hasattr(repository, "persist_stage_logs"):
                        try:
                            repository.persist_stage_logs(
                                str(command_id),
                                [
                                    {
                                        "sequence": 19_999,
                                        "node_name": "DUPLICATE_EVENT_DETECTED",
                                        "attempt": 1,
                                        "status": "SUCCESS",
                                        "message": None,
                                        "details": {"event_id": event.event_id},
                                        "created_at": datetime.now(UTC),
                                    }
                                ],
                            )
                        except Exception:
                            pass
                    result = self._result_from_stored(existing)
                    result["payload_identity"] = identity
                    return result
                command_id = existing.get("generated_command_id")

        if (
            not recovery_replay
            and hasattr(repository, "create_execution_event_processing")
        ):
            inserted = repository.create_execution_event_processing(
                {
                    "event_id": event.event_id,
                    "warehouse_id": event.warehouse_id,
                    "event_type": event.event_type,
                    "event_source": event.execution_context,
                    "status": "RECEIVED",
                    # Store only the client-reported event. The authoritative
                    # plan is server state and must not be copied from payload.
                    "event_payload": event.model_dump(mode="json"),
                    "impact_summary": {},
                    "result_summary": {},
                    "approval_required": False,
                    "created_at": datetime.now(UTC),
                }
            )
            if inserted.get("duplicate"):
                identity = payload_identity_evidence(
                    event, inserted.get("event_payload") or {}
                )
                if not identity["match"]:
                    return {
                        "event_id": event.event_id,
                        "duplicate": True,
                        "retryable": False,
                        "status": "REJECTED",
                        "final_status": "EVENT_ID_PAYLOAD_CONFLICT",
                        "failure_reason": "EVENT_ID_PAYLOAD_CONFLICT",
                        "payload_identity": identity,
                        "errors": [
                            "같은 event_id에 서로 다른 이벤트 본문을 사용할 수 없습니다."
                        ],
                    }
                if self._in_progress_duplicate(inserted):
                    return {
                        "event_id": event.event_id,
                        "duplicate": True,
                        "retryable": True,
                        "status": "PROCESSING",
                        "final_status": "EVENT_PROCESSING",
                        "payload_identity": identity,
                        "errors": [],
                    }
                return self._result_from_stored(inserted)
            if inserted.get("status") != "RECEIVED":
                return self._result_from_stored(inserted)

        try:
            runtime = resolve_runtime_context(reported_event, self.services)
            event = bind_runtime_context(reported_event, runtime)
        except RuntimeAuthorityError as exc:
            result = {
                "event_id": reported_event.event_id,
                "duplicate": False,
                "auto_replan_requested": False,
                "status": "FAILED",
                "final_status": "RUNTIME_CONTEXT_FAILED",
                "failure_reason": exc.code,
                "errors": [str(exc)],
            }
            return self._finalize_event(
                reported_event,
                status="FAILED",
                result=result,
            )
        except Exception as exc:
            result = {
                "event_id": reported_event.event_id,
                "duplicate": False,
                "auto_replan_requested": False,
                "status": "FAILED",
                "final_status": "RUNTIME_CONTEXT_FAILED",
                "failure_reason": "RUNTIME_CONTEXT_RESOLUTION_FAILED",
                "errors": [str(exc)],
            }
            return self._finalize_event(
                reported_event,
                status="FAILED",
                result=result,
            )

        runtime_summary = runtime.public_summary()
        effective_event = event
        if reported_event.event_type == "POSITION_UPDATED":
            # POSITION_UPDATED mutates telemetry only.  A low-battery anomaly,
            # when present, is derived after that mutation by the server.
            execution_result = self.event_handler(
                event,
                auto_replan=False,
                analyze_impact=False,
            )
            if recovery_replay:
                execution_result = {
                    **execution_result,
                    "recovery_replay": True,
                    "duplicate": True,
                }
            if execution_result.get("stale_event_ignored") is True:
                result = {
                    **execution_result,
                    "event_id": reported_event.event_id,
                    "duplicate": False,
                    "retryable": False,
                    "auto_replan_requested": False,
                    "reported_event_type": "POSITION_UPDATED",
                    "effective_event_type": "POSITION_UPDATED",
                    "server_derived_event": False,
                    "runtime_context": runtime_summary,
                    "status": "STALE_EVENT_IGNORED",
                    "final_status": "STALE_EVENT_IGNORED",
                    "errors": [],
                }
                return self._finalize_event(
                    reported_event,
                    status="STALE_EVENT_IGNORED",
                    result=result,
                )
            execution_status = self._execution_result_status(
                reported_event,
                execution_result,
            )
            if execution_status == "FAILED":
                result = {
                    **execution_result,
                    "event_id": reported_event.event_id,
                    "duplicate": False,
                    "auto_replan_requested": False,
                    "reported_event_type": "POSITION_UPDATED",
                    "effective_event_type": "POSITION_UPDATED",
                    "runtime_context": runtime_summary,
                    "status": "FAILED",
                }
                return self._finalize_event(
                    reported_event,
                    status="FAILED",
                    result=result,
                )
            derived = derive_low_battery_event(event, runtime)
            if derived is None:
                result = {
                    **execution_result,
                    "event_id": reported_event.event_id,
                    "duplicate": False,
                    "auto_replan_requested": False,
                    "reported_event_type": "POSITION_UPDATED",
                    "effective_event_type": "POSITION_UPDATED",
                    "server_derived_event": False,
                    "runtime_context": runtime_summary,
                    "status": "TELEMETRY_UPDATED",
                    "final_status": "TELEMETRY_UPDATED",
                }
                return self._finalize_event(
                    reported_event,
                    status="TELEMETRY_UPDATED",
                    result=result,
                )
            effective_event = derived
            try:
                impact = self.impact_analyzer(effective_event, self.services)
                execution_result = {
                    **execution_result,
                    "impact_analysis": impact.model_dump(mode="json"),
                    "final_status": "EVENT_IMPACT_ANALYZED",
                }
            except Exception as exc:
                result = {
                    **execution_result,
                    "event_id": reported_event.event_id,
                    "duplicate": False,
                    "auto_replan_requested": False,
                    "reported_event_type": "POSITION_UPDATED",
                    "effective_event_type": "LOW_BATTERY",
                    "server_derived_event": True,
                    "server_derived_event_evidence": _server_derived_event_evidence(
                        reported_event, effective_event
                    ),
                    "runtime_context": runtime_summary,
                    "status": "FAILED",
                    "final_status": "EVENT_IMPACT_ANALYSIS_FAILED",
                    "errors": [str(exc)],
                }
                return self._finalize_event(
                    reported_event,
                    status="FAILED",
                    result=result,
                )
        else:
            execution_result = self.event_handler(
                event,
                auto_replan=False,
                analyze_impact=True,
            )
            if recovery_replay:
                execution_result = {
                    **execution_result,
                    "recovery_replay": True,
                    "duplicate": True,
                }

        if execution_result.get("stale_event_ignored") is True:
            result = {
                **execution_result,
                "event_id": reported_event.event_id,
                "duplicate": False,
                "retryable": False,
                "auto_replan_requested": False,
                "reported_event_type": reported_event.event_type,
                "effective_event_type": effective_event.event_type,
                "server_derived_event": False,
                "runtime_context": runtime_summary,
                "status": "STALE_EVENT_IGNORED",
                "final_status": "STALE_EVENT_IGNORED",
                "errors": [],
            }
            return self._finalize_event(
                reported_event,
                status="STALE_EVENT_IGNORED",
                result=result,
            )

        impact_payload = execution_result.get("impact_analysis") or {}
        if not impact_payload:
            status = self._execution_result_status(effective_event, execution_result)
            if effective_event.event_type in REPLAN_TRIGGER_EVENTS:
                status = "FAILED"
            result = {
                **execution_result,
                "event_id": reported_event.event_id,
                "duplicate": False,
                "auto_replan_requested": False,
                "reported_event_type": reported_event.event_type,
                "effective_event_type": effective_event.event_type,
                "server_derived_event": (
                    reported_event.event_type != effective_event.event_type
                ),
                "runtime_context": runtime_summary,
                "status": status,
            }
            return self._finalize_event(
                reported_event,
                status=status,
                result=result,
            )
        impact = EventImpactAnalysis.model_validate(impact_payload)
        base_result = {
            **execution_result,
            "event_id": reported_event.event_id,
            "duplicate": False,
            "reported_event_type": reported_event.event_type,
            "effective_event_type": effective_event.event_type,
            "server_derived_event": (
                reported_event.event_type != effective_event.event_type
            ),
            "server_derived_event_evidence": _server_derived_event_evidence(
                reported_event, effective_event
            ),
            "runtime_context": runtime_summary,
            "impact_analysis": impact.model_dump(mode="json"),
            "scope": impact.recommended_scope,
            "approval_required": impact.approval_required,
            "partial_replan": {
                "version": "p16.5.12.1",
                "policy": impact.partial_replan_policy,
                "completed_task_ids": impact.completed_task_ids,
                "protected_task_ids": impact.frozen_task_ids,
                "changeable_task_ids": impact.changeable_task_ids,
                "freeze_horizon_seconds": impact.freeze_horizon_seconds,
                "robot_state_overrides": impact.robot_state_overrides,
                "current_time_step": runtime.current_time_step,
                "runtime_source": runtime.source,
            },
            "robot_failure_recovery": deepcopy(
                impact.robot_failure_recovery
            ),
        }
        if (
            effective_event.event_type == "ROBOT_FAILED"
            and impact.robot_failure_recovery.get("requires_manual_recovery")
        ):
            result = {
                **base_result,
                "auto_replan_requested": False,
                "status": "MANUAL_RECOVERY_REQUIRED",
                "final_status": "MANUAL_RECOVERY_REQUIRED",
                "recovery_required": True,
                "retryable": False,
                "failure_reason": impact.robot_failure_recovery.get("strategy"),
            }
            return self._finalize_event(
                reported_event,
                status="MANUAL_RECOVERY_REQUIRED",
                result=result,
                impact=impact,
            )
        if impact.recommended_scope == "NO_REPLAN":
            result = {
                **base_result,
                "auto_replan_requested": False,
                "status": "REPLAN_NOT_REQUIRED",
                "final_status": "REPLAN_NOT_REQUIRED",
            }
            return self._finalize_event(
                reported_event,
                status="REPLAN_NOT_REQUIRED",
                result=result,
                impact=impact,
            )

        repeat_count = 0
        if hasattr(repository, "count_recent_event_failure_signature"):
            repeat_count = repository.count_recent_event_failure_signature(
                reported_event.warehouse_id,
                impact.failure_signature,
                exclude_event_id=reported_event.event_id,
                window_seconds=3600,
            )
        original_scope = impact.recommended_scope
        escalated_from_local = False
        if repeat_count >= 1:
            can_escalate_once = (
                repeat_count == 1
                and impact.recommended_scope == "LOCAL_REPLAN"
                and effective_event.event_type in ROUTE_FAILURE_ESCALATION_EVENTS
            )
            if can_escalate_once:
                escalated_from_local = True
                impact = impact.model_copy(
                    update={
                        "recommended_scope": "GLOBAL_REPLAN",
                        "evidence": [
                            *impact.evidence,
                            (
                                "동일한 국소 경로 실패가 반복되어 로봇 전체의 "
                                "우선순위와 시작 시차를 다시 탐색하는 전역 재계획으로 "
                                "한 번 확장합니다."
                            ),
                        ],
                    }
                )
                base_result = {
                    **base_result,
                    "impact_analysis": impact.model_dump(mode="json"),
                    "scope": "GLOBAL_REPLAN",
                    "original_scope": original_scope,
                    "escalated_from_local": True,
                    "repeat_count": repeat_count,
                }
            else:
                result = {
                    **base_result,
                    "auto_replan_requested": False,
                    "status": "FAILED",
                    "final_status": "FAILED",
                    "failure_reason": "REPEATED_FAILURE_DETECTED",
                    "failure_signature": impact.failure_signature,
                    "repeat_count": repeat_count,
                }
                return self._finalize_event(
                    reported_event,
                    status="FAILED",
                    result=result,
                    impact=impact,
                )
        else:
            base_result = {
                **base_result,
                "original_scope": original_scope,
                "escalated_from_local": False,
                "repeat_count": 0,
            }

        request_id = str(
            uuid5(NAMESPACE_URL, f"event-replan:{reported_event.event_id}")
        )
        command_id = str(
            uuid5(NAMESPACE_URL, f"event-replan-command:{reported_event.event_id}")
        )
        scenario = _scenario_for_event(effective_event, impact)
        command_text = _replan_text(effective_event, impact)
        request_values = {
            "request_id": request_id,
            "event_id": reported_event.event_id,
            "command_id": command_id,
            "warehouse_id": reported_event.warehouse_id,
            "scope": impact.recommended_scope,
            "status": "REPLAN_SIMULATION_RUNNING",
            "execution_context": reported_event.execution_context,
            "affected_robot_ids": impact.affected_robot_ids,
            "affected_task_ids": impact.affected_task_ids,
            "expected_active_plan_version": impact.active_plan_version,
            "approval_required": impact.approval_required,
            "result_summary": {
                "command_text": command_text,
                "scenario_definition": scenario.model_dump(mode="json"),
                "reported_event": reported_event.model_dump(mode="json"),
                "effective_event": effective_event.model_dump(mode="json"),
                "runtime_context": runtime_summary,
            },
            "created_at": datetime.now(UTC),
        }
        if hasattr(repository, "create_or_get_automatic_replan_request"):
            repository.create_or_get_automatic_replan_request(request_values)

        command = NaturalLanguageCommand(
            command_id=command_id,
            warehouse_id=reported_event.warehouse_id,
            text=command_text,
            requested_execution_mode="SIMULATE_ONLY",
            simulation_id=reported_event.simulation_id,
            source="SYSTEM_EVENT",
            scenario_definition=scenario.model_dump(mode="json"),
        )
        stages: list[tuple[str, dict[str, Any]]] = [
            (
                "EXECUTION_EVENT_RECEIVED",
                {
                    "event_id": reported_event.event_id,
                    "reported_event_type": reported_event.event_type,
                    "effective_event_type": effective_event.event_type,
                },
            ),
            (
                "SERVER_RUNTIME_CONTEXT_RESOLVED",
                runtime_summary,
            ),
            (
                "EVENT_IMPACT_ANALYZED",
                {
                    "event_id": reported_event.event_id,
                    "scope": impact.recommended_scope,
                    "risk_level": impact.risk_level,
                    "affected_robot_ids": impact.affected_robot_ids,
                    "affected_task_ids": impact.affected_task_ids,
                    "original_scope": original_scope,
                    "escalated_from_local": escalated_from_local,
                    "repeat_count": repeat_count,
                    "current_time_step": runtime.current_time_step,
                },
            ),
            ("AUTO_REPLAN_REQUESTED", {"request_id": request_id}),
            ("AUTO_REPLAN_SIMULATION_STARTED", {"request_id": request_id}),
        ]
        plan_guard = self._plan_guard(reported_event)
        try:
            planning_response = self.planner(command)
            verification = (
                planning_response.get("verification_decision") or {}
            ).get("decision")
            planning_status = str(planning_response.get("status") or "").upper()
            planning_final_status = str(
                planning_response.get("final_status") or ""
            ).upper()
            planning_errors = [
                str(value)
                for value in (planning_response.get("errors") or [])
                if value not in (None, "")
            ]
            if verification not in {"PASS", "PASS_WITH_WARNING"} and not planning_errors:
                failure_code = (
                    planning_final_status
                    or planning_status
                    or "VERIFICATION_DECISION_MISSING"
                )
                planning_errors = [f"AUTO_REPLAN_PLANNING_FAILED:{failure_code}"]
            robot_failure_result = _robot_failure_recovery_result(
                planning_response, impact
            )
            if robot_failure_result.get("status") == "FAIL":
                planning_errors.extend(robot_failure_result.get("errors") or [])
            verified = (
                verification in {"PASS", "PASS_WITH_WARNING"}
                and not planning_errors
            )
            plan_recovery = (
                {
                    "policy": "FAILED_REPLAN_RETAINS_LAST_VERIFIED_PLAN",
                    "required": False,
                    "restored": False,
                    "status": "NEW_PLAN_VERIFIED",
                    "previous_plan_version": (plan_guard or {}).get(
                        "active_plan_version"
                    ),
                    "observed_plan_version": planning_response.get("plan_version"),
                }
                if verified
                else self._restore_plan_guard(
                    reported_event,
                    plan_guard,
                    reason="AUTO_REPLAN_VERIFICATION_FAILED",
                )
            )
            status = (
                "APPROVAL_REQUIRED"
                if verified and impact.approval_required
                else "REPLAN_VERIFIED"
                if verified
                else "FAILED"
            )
            stages.append(
                (
                    "AUTO_REPLAN_VERIFIED" if verified else "EVENT_REPLAN_FAILED",
                    {
                        "request_id": request_id,
                        "verification_decision": verification,
                        "plan_version": planning_response.get("plan_version"),
                    },
                )
            )
            if status == "APPROVAL_REQUIRED":
                stages.append(
                    (
                        "EVENT_REPLAN_APPROVAL_REQUIRED",
                        {"request_id": request_id},
                    )
                )
            result_summary = {
                **request_values["result_summary"],
                "planning_status": planning_response.get("status"),
                "planning_final_status": planning_response.get("final_status"),
                "planning_errors": planning_errors,
                "verification_decision": verification,
                "simulation_id": planning_response.get("simulation_id"),
                "plan_version": planning_response.get("plan_version"),
                "planning_command_id": planning_response.get("command_id"),
                "plan_recovery": plan_recovery,
                "robot_failure_recovery_result": robot_failure_result,
                "stale_route_eviction": _stale_route_eviction_evidence(
                    planning_response
                ),
            }
            if hasattr(repository, "update_automatic_replan_request"):
                repository.update_automatic_replan_request(
                    request_id,
                    {
                        "status": status,
                        "generated_plan_version": planning_response.get("plan_version"),
                        "simulation_id": planning_response.get("simulation_id"),
                        "verification_decision": verification,
                        "result_summary": result_summary,
                        "completed_at": datetime.now(UTC),
                    },
                )
            self._persist_event_stages(command_id, stages)
            result = {
                **base_result,
                "auto_replan_requested": True,
                "replan_request_id": request_id,
                "generated_command_id": command_id,
                "generated_plan_version": planning_response.get("plan_version"),
                "simulation_id": planning_response.get("simulation_id"),
                "verification_decision": verification,
                "plan_recovery": plan_recovery,
                "robot_failure_recovery_result": robot_failure_result,
                "stale_route_eviction": _stale_route_eviction_evidence(
                    planning_response
                ),
                "status": status,
                "final_status": status,
                **(
                    {
                        "failure_reason": planning_errors[0],
                        "errors": planning_errors,
                        "planning_status": planning_response.get("status"),
                        "planning_final_status": planning_response.get("final_status"),
                        "planning_debug": _planning_failure_debug(
                            planning_response
                        ),
                    }
                    if not verified
                    else {}
                ),
            }
            return self._finalize_event(
                reported_event,
                status=status,
                result=result,
                impact=impact,
                request_id=request_id,
                command_id=command_id,
                plan_version=planning_response.get("plan_version"),
            )
        except Exception as exc:
            plan_recovery = self._restore_plan_guard(
                reported_event,
                plan_guard,
                reason="AUTO_REPLAN_EXCEPTION",
            )
            stages.append(
                ("EVENT_REPLAN_FAILED", {"request_id": request_id, "reason": str(exc)})
            )
            self._persist_event_stages(command_id, stages)
            if hasattr(repository, "update_automatic_replan_request"):
                repository.update_automatic_replan_request(
                    request_id,
                    {
                        "status": "FAILED",
                        "verification_decision": "FAIL",
                        "result_summary": {
                            **request_values["result_summary"],
                            "error": str(exc),
                            "plan_recovery": plan_recovery,
                        },
                        "completed_at": datetime.now(UTC),
                    },
                )
            result = {
                **base_result,
                "auto_replan_requested": True,
                "replan_request_id": request_id,
                "generated_command_id": command_id,
                "plan_recovery": plan_recovery,
                "status": "FAILED",
                "final_status": "FAILED",
                "errors": [str(exc)],
            }
            return self._finalize_event(
                reported_event,
                status="FAILED",
                result=result,
                impact=impact,
                request_id=request_id,
                command_id=command_id,
            )

    def approve(
        self,
        request_id: str,
        decision: EventReplanDecisionRequest,
    ) -> dict[str, Any]:
        repository = self.services.postgres
        row = repository.get_automatic_replan_request(request_id)
        if row is None:
            raise EventReplanNotFoundError(request_id)
        if row.get("status") == "EXECUTED":
            return {**row, "duplicate": True}
        if row.get("status") in {"REJECTED", "FAILED", "STALE_PLAN"}:
            raise EventReplanConflictError(
                f"현재 상태에서는 승인할 수 없습니다: {row.get('status')}"
            )
        if row.get("execution_context") != "REAL":
            raise EventReplanConflictError(
                "SIMULATION 이벤트 재계획은 실제 실행으로 승인할 수 없습니다."
            )
        if row.get("verification_decision") not in {"PASS", "PASS_WITH_WARNING"}:
            raise EventReplanConflictError("PASS 계열 Verification 결과가 필요합니다.")

        live = self.services.redis.live_snapshot(int(row["warehouse_id"]))
        expected = row.get("expected_active_plan_version")
        current = live.get("active_plan_version")
        if current != expected:
            repository.update_automatic_replan_request(
                request_id,
                {
                    "status": "STALE_PLAN",
                    "result_summary": {
                        **(row.get("result_summary") or {}),
                        "stale_expected_plan_version": expected,
                        "stale_current_plan_version": current,
                    },
                    "completed_at": datetime.now(UTC),
                },
            )
            raise EventReplanConflictError("활성 계획 버전이 변경되어 승인을 중단했습니다.")

        repository.update_automatic_replan_request(
            request_id,
            {
                "status": "APPROVED",
                "approved_by": decision.actor_id,
                "approval_reason": decision.reason,
                "approved_at": datetime.now(UTC),
                "result_summary": row.get("result_summary") or {},
            },
        )
        summary = row.get("result_summary") or {}
        scenario = summary.get("scenario_definition")
        execution_command_id = str(
            uuid5(NAMESPACE_URL, f"event-replan-approval:{request_id}")
        )
        command = NaturalLanguageCommand(
            command_id=execution_command_id,
            warehouse_id=int(row["warehouse_id"]),
            text=str(summary.get("command_text") or "이벤트 재계획")
            + " 최신 상태를 검증한 뒤 실제 실행해줘",
            requested_execution_mode="EXECUTE",
            source="SYSTEM_EVENT",
            scenario_definition=scenario,
        )
        try:
            response = self.planner(command)
        except Exception as exc:
            failure_summary = {
                **summary,
                "execution_command_id": execution_command_id,
                "execution_status": "FAILED",
                "error": str(exc),
            }
            repository.update_automatic_replan_request(
                request_id,
                {
                    "status": "FAILED",
                    "result_summary": failure_summary,
                    "completed_at": datetime.now(UTC),
                },
            )
            if hasattr(repository, "update_execution_event_status"):
                repository.update_execution_event_status(
                    str(row["event_id"]),
                    status="FAILED",
                    result_summary=failure_summary,
                )
            self._persist_event_stages(
                execution_command_id,
                [("EVENT_REPLAN_FAILED", {"request_id": request_id, "reason": str(exc)})],
            )
            return {
                "request_id": request_id,
                "status": "FAILED",
                "generated_command_id": execution_command_id,
                "errors": [str(exc)],
            }
        verification = (response.get("verification_decision") or {}).get("decision")
        executed = (
            verification in {"PASS", "PASS_WITH_WARNING"}
            and response.get("status") == "DISPATCHED"
        )
        status = "EXECUTED" if executed else "FAILED"
        result_summary = {
            **summary,
            "execution_command_id": execution_command_id,
            "execution_plan_version": response.get("plan_version"),
            "execution_status": response.get("status"),
            "execution_verification_decision": verification,
        }
        repository.update_automatic_replan_request(
            request_id,
            {
                "status": status,
                "generated_plan_version": response.get("plan_version"),
                "verification_decision": verification,
                "result_summary": result_summary,
                "completed_at": datetime.now(UTC),
            },
        )
        if hasattr(repository, "update_execution_event_status"):
            repository.update_execution_event_status(
                str(row["event_id"]),
                status=status,
                result_summary=result_summary,
            )
        self._persist_event_stages(
            execution_command_id,
            [
                ("EVENT_REPLAN_APPROVED", {"request_id": request_id}),
                (
                    "EVENT_REPLAN_EXECUTED" if executed else "EVENT_REPLAN_FAILED",
                    {
                        "request_id": request_id,
                        "plan_version": response.get("plan_version"),
                        "status": response.get("status"),
                    },
                ),
            ],
        )
        return {
            "request_id": request_id,
            "status": status,
            "generated_command_id": execution_command_id,
            "generated_plan_version": response.get("plan_version"),
            "verification_decision": verification,
        }

    def reject(
        self,
        request_id: str,
        decision: EventReplanDecisionRequest,
    ) -> dict[str, Any]:
        repository = self.services.postgres
        row = repository.get_automatic_replan_request(request_id)
        if row is None:
            raise EventReplanNotFoundError(request_id)
        if row.get("status") == "REJECTED":
            return {**row, "duplicate": True}
        if row.get("status") in {"APPROVED", "EXECUTED"}:
            raise EventReplanConflictError(
                f"현재 상태에서는 거부할 수 없습니다: {row.get('status')}"
            )
        repository.update_automatic_replan_request(
            request_id,
            {
                "status": "REJECTED",
                "rejected_by": decision.actor_id,
                "rejection_reason": decision.reason,
                "rejected_at": datetime.now(UTC),
                "result_summary": row.get("result_summary") or {},
                "completed_at": datetime.now(UTC),
            },
        )
        if hasattr(repository, "update_execution_event_status"):
            repository.update_execution_event_status(
                str(row["event_id"]),
                status="REJECTED",
                result_summary=row.get("result_summary") or {},
            )
        command_id = row.get("command_id")
        if command_id:
            self._persist_event_stages(
                str(command_id),
                [("EVENT_REPLAN_REJECTED", {"request_id": request_id})],
            )
        return {"request_id": request_id, "status": "REJECTED"}

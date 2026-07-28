import re
from datetime import UTC, datetime
from typing import Any


SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "database_url",
    "neo4j_password",
    "openai_api_key",
    "password",
    "redis_url",
    "secret",
    "token",
)

TRACE_STAGE_NAMES = {
    "interpret_command": "COMMAND_INTERPRETED",
    "conversation_context_loaded": "CONVERSATION_CONTEXT_LOADED",
    "conversation_context_resolved": "CONVERSATION_CONTEXT_RESOLVED",
    "conversation_context_updated": "CONVERSATION_CONTEXT_UPDATED",
    "supervisor_started": "SUPERVISOR_STARTED",
    "supervisor_completed": "SUPERVISOR_COMPLETED",
    "supervisor_fallback_used": "SUPERVISOR_FALLBACK_USED",
    "build_snapshot": "SNAPSHOT_CREATED",
    "clarification_required": "CLARIFICATION_REQUIRED",
    "clarification_response_received": "CLARIFICATION_RESPONSE_RECEIVED",
    "clarification_resolved": "CLARIFICATION_RESOLVED",
    "clarification_expired": "CLARIFICATION_EXPIRED",
    "route_by_command": "COMMAND_ROUTED",
    "decide_scope": "SCOPE_DECIDED",
    "select_required_tasks": "TASKS_SELECTED",
    "build_optimization_problem": "OPTIMIZATION_PROBLEM_BUILT",
    "local_optimize": "OPTIMIZATION_COMPLETED",
    "cuopt_optimize": "OPTIMIZATION_COMPLETED",
    "optimization_candidates_evaluated": "OPTIMIZATION_CANDIDATES_EVALUATED",
    "objective_breakdown_created": "OBJECTIVE_BREAKDOWN_CREATED",
    "route_evidence_created": "ROUTE_EVIDENCE_CREATED",
    "reservation_evidence_created": "RESERVATION_EVIDENCE_CREATED",
    "distance_comparison_created": "DISTANCE_COMPARISON_CREATED",
    "build_routes": "ROUTING_COMPLETED",
    "validate_plan": "PLAN_VALIDATED",
    "validate_simulation": "SIMULATION_VALIDATED",
    "verification_started": "VERIFICATION_STARTED",
    "verification_fallback_used": "VERIFICATION_FALLBACK_USED",
    "verification_completed": "VERIFICATION_COMPLETED",
    "replan_requested": "REPLAN_REQUESTED",
    "local_replan_started": "LOCAL_REPLAN_STARTED",
    "global_replan_started": "GLOBAL_REPLAN_STARTED",
    "replan_completed": "REPLAN_COMPLETED",
    "replan_failed": "REPLAN_FAILED",
    "replan_limit_reached": "REPLAN_LIMIT_REACHED",
    "repeated_failure_detected": "REPEATED_FAILURE_DETECTED",
    "persist_result": "RESULT_PERSISTED",
    "execution_precheck": "EXECUTION_PRECHECKED",
    "activate_plan": "PLAN_ACTIVATED",
    "dispatch_plan": "PLAN_DISPATCHED",
    "generate_final_report": "REPORT_GENERATED",
    "evidence_report_generated": "EVIDENCE_REPORT_GENERATED",
    "report_template_fallback_used": "REPORT_TEMPLATE_FALLBACK_USED",
}

FAILURE_MARKERS = ("FAILED", "BLOCKED", "INVALID", "ERROR")


def _sanitize_string(value: str) -> str:
    value = re.sub(
        r"(?i)(://[^:/\s]+:)[^@\s]+@",
        r"\1***@",
        value,
    )
    value = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,]+", r"\1***", value)
    value = re.sub(
        r"(?i)\b(openai_api_key|api[-_ ]?key|database_url|neo4j_password|"
        r"redis_url|password|secret|token)\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2***",
        value,
    )
    return value


def sanitize_log_details(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = sanitize_log_details(raw_value)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [sanitize_log_details(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_string(str(value))


def _stage_status(details: dict[str, Any]) -> str:
    if details.get("success") is False or details.get("valid") is False:
        return "FAILED"
    return "SUCCESS"


def _stage_name(trace_row: dict[str, Any]) -> str:
    node = str(trace_row.get("node") or "UNKNOWN")
    if node == "simulation":
        return "SIMULATION_COMPLETED" if trace_row.get("success") else "SIMULATION_FAILED"
    return TRACE_STAGE_NAMES.get(node, node.upper()[:80])


def _stage_message(details: dict[str, Any]) -> str | None:
    reason = details.get("reason")
    if isinstance(reason, list):
        return "; ".join(str(item) for item in reason[:3]) or None
    return str(reason) if reason else None


def _command_type(state: dict[str, Any]) -> str | None:
    command = state.get("command", {})
    if command.get("source") == "SYSTEM_EVENT":
        return "SYSTEM_EVENT"
    interpretation = state.get("interpretation", {})
    if interpretation.get("command_kind") == "QUERY":
        return "QUERY"
    return interpretation.get("execution_mode") or command.get("requested_execution_mode")


def _business_status(state: dict[str, Any]) -> str:
    response = state.get("response", {})
    raw_status = str(response.get("status") or state.get("final_status") or "").upper()
    has_failure_status = any(marker in raw_status for marker in FAILURE_MARKERS)
    errors = [str(item) for item in state.get("errors", []) if item]
    if raw_status == "CLARIFICATION_REQUIRED":
        return "CLARIFICATION_REQUIRED"
    if has_failure_status:
        return "FAILED"
    if errors:
        return "PARTIAL_SUCCESS" if response else "FAILED"
    return "SUCCESS"


def _result_summary(state: dict[str, Any]) -> dict[str, Any]:
    response = state.get("response", {})
    data = response.get("data", {})
    summary_keys = {
        "available_robot_count",
        "conflict_count",
        "executing_robot_count",
        "inventory_row_count",
        "item_count",
        "makespan_seconds",
        "robot_count",
        "task_count",
        "tardiness",
        "total_available_quantity",
        "total_distance",
        "valid",
        "work_count",
    }
    compact_data = {
        key: value
        for key, value in data.items()
        if key in summary_keys
    }
    supervisor = state.get("supervisor_decision", {})
    verification = state.get("verification_decision", {})
    return sanitize_log_details(
        {
            "status": response.get("status") or state.get("final_status"),
            "correlation_ids": _correlation_ids(state),
            "message": response.get("message"),
            "answer": response.get("answer"),
            "intent": response.get("intent"),
            "plan_mode": response.get("plan_mode"),
            "data": compact_data,
            "supervisor": {
                "source": state.get("supervisor_source"),
                "prompt_version": state.get("supervisor_prompt_version"),
                "command_kind": supervisor.get("command_kind"),
                "execution_mode": supervisor.get("execution_mode"),
                "plan_mode": supervisor.get("plan_mode"),
                "risk_level": supervisor.get("risk_level"),
                "fallback_used": state.get("supervisor_source") != "llm",
            },
            "verification": {
                "decision": verification.get("decision"),
                "requires_replan": verification.get("requires_replan"),
                "replan_scope": verification.get("replan_scope"),
                "affected_robot_ids": verification.get("affected_robot_ids", []),
                "affected_task_ids": verification.get("affected_task_ids", []),
                "evidence_ids": verification.get("evidence_ids", []),
                "source": state.get("verification_source"),
                "prompt_version": state.get("verification_prompt_version"),
                "fallback_used": state.get("verification_source") != "llm",
            },
            "replan": {
                "attempt": state.get("replan_attempt", 0),
                "max_attempts": state.get("max_replan_attempts", 0),
                "reason": state.get("replan_reason"),
                "original_plan_version": state.get("original_plan_version"),
                "current_plan_version": state.get("current_plan_version"),
                "history": state.get("replan_history", []),
            },
            "evidence": {
                "summary": state.get("response", {}).get("evidence_summary", {}),
                "report_source": state.get("report_source"),
                "report_prompt_version": state.get("report_prompt_version"),
            },
        }
    )


def _correlation_ids(state: dict[str, Any]) -> dict[str, Any]:
    """Return the stable identifiers used to join command and planning logs."""

    command = state.get("command", {})
    return {
        "command_id": command.get("command_id"),
        "conversation_id": command.get("conversation_id"),
        "parent_command_id": command.get("parent_command_id"),
        "plan_version": state.get("plan_version")
        or state.get("current_plan_version"),
        "simulation_id": state.get("simulation_id"),
    }


class AuditService:
    def __init__(self, repository: Any):
        self.repository = repository

    def create_or_get_command_history(self, command: dict[str, Any]) -> dict[str, Any] | None:
        payload = sanitize_log_details(
            {
                "command_id": command["command_id"],
                "warehouse_id": command["warehouse_id"],
                "requested_execution_mode": command.get("requested_execution_mode"),
                "source": command.get("source"),
                "original_text": command.get("text"),
                "actor_id": command.get("actor_id"),
                "status": "PROCESSING",
                "simulation_id": command.get("simulation_id"),
                "parent_command_id": command.get("parent_command_id"),
                "received_at": command.get("received_at") or datetime.now(UTC),
            }
        )
        return self.repository.create_or_get_command_history(payload)

    def update_command_history(self, values: dict[str, Any]) -> None:
        self.repository.update_command_history(sanitize_log_details(values))

    def persist_stage_logs(self, command_id: str, stages: list[dict[str, Any]]) -> None:
        self.repository.persist_stage_logs(
            command_id,
            sanitize_log_details(stages),
        )

    def _build_stage_logs(self, state: dict[str, Any], outcome: str) -> list[dict[str, Any]]:
        command = state["command"]
        correlation_ids = _correlation_ids(state)
        rows: list[dict[str, Any]] = [
            {
                "sequence": 1,
                "node_name": "COMMAND_RECEIVED",
                "attempt": 1,
                "status": "SUCCESS",
                "message": None,
                "details": {
                    "requested_execution_mode": command.get("requested_execution_mode"),
                    "source": command.get("source"),
                    "correlation_ids": correlation_ids,
                },
                "created_at": command.get("received_at") or datetime.now(UTC),
            }
        ]
        for sequence, trace_row in enumerate(state.get("trace", []), start=2):
            details = {
                key: value
                for key, value in trace_row.items()
                if key not in {"node", "at", "attempt"}
            }
            details["correlation_ids"] = correlation_ids
            rows.append(
                {
                    "sequence": sequence,
                    "node_name": _stage_name(trace_row),
                    "attempt": int(trace_row.get("attempt") or 1),
                    "status": _stage_status(details),
                    "message": _stage_message(details),
                    "details": details,
                    "created_at": trace_row.get("at") or datetime.now(UTC),
                }
            )
        rows.append(
            {
                "sequence": len(rows) + 1,
                "node_name": "COMMAND_FAILED" if outcome == "FAILED" else "COMMAND_COMPLETED",
                "attempt": 1,
                "status": outcome,
                "message": None,
                "details": {
                    "final_status": state.get("response", {}).get("status"),
                    "correlation_ids": correlation_ids,
                },
                "created_at": datetime.now(UTC),
            }
        )
        return sanitize_log_details(rows)

    def finalize_command_audit(self, state: dict[str, Any]) -> str:
        outcome = _business_status(state)
        interpretation = state.get("interpretation", {})
        command = state["command"]
        errors = [str(item) for item in state.get("errors", []) if item]
        history = sanitize_log_details(
            {
                "command_id": command["command_id"],
                "command_type": _command_type(state),
                "resolved_execution_mode": interpretation.get("execution_mode"),
                "status": outcome,
                "simulation_id": state.get("simulation_id"),
                "plan_version": state.get("plan_version"),
                "completed_at": datetime.now(UTC),
                "result_summary": _result_summary(state),
                "error_summary": {"errors": errors} if errors else None,
            }
        )
        stages = self._build_stage_logs(state, outcome)
        self.repository.finalize_command_audit(history, stages)
        return outcome

    def finalize_unhandled_failure(self, command: dict[str, Any], error: Exception) -> None:
        state = {
            "command": command,
            "errors": [str(error)],
            "trace": [],
            "final_status": "UNHANDLED_FAILED",
            "response": {"status": "UNHANDLED_FAILED"},
        }
        self.finalize_command_audit(state)

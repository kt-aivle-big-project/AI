"""Query, no-action, review, failure, persistence, and dashboard nodes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.node_observability import observe_node
from app.domain.schemas import (
    ClarificationResult,
    DashboardEvent,
    HumanReviewResult,
    InputRejectionResult,
    InventoryContext,
    PersistenceResult,
    QueryResponse,
    WorkflowError,
    WorkflowFailureResult,
    WorkflowHoldResult,
    WorkflowValidationIssue,
)
from app.graph.node_support import trace_update
from app.graph.state import LaroGraphState


def _jsonable(value: Any) -> Any:
    """Convert nested Pydantic values into JSON-compatible objects."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


@observe_node("query_response", purpose="조회 Context를 Mission이나 Optimizer 없이 구조화 응답으로 조립")
def query_response_node(state: LaroGraphState) -> dict:
    """Build a compact query-only response."""

    details: dict[str, Any] = {}
    if state.get("inventory_context") is not None:
        inventory = state["inventory_context"]
        details["inventory"] = _jsonable(inventory.overview or inventory)
    if state.get("map_context") is not None:
        details["map"] = _jsonable(state["map_context"])
    if state.get("robot_context") is not None:
        details["robots"] = _jsonable(state["robot_context"])
    return {
        "query_response": QueryResponse(summary="Requested warehouse context was collected.", details=details),
        "workflow_status": "query_completed",
        **trace_update("query_response"),
    }


@observe_node("no_action", purpose="실행할 이벤트나 명령이 없음을 정상 종료 결과로 기록")
def no_action_node(state: LaroGraphState) -> dict:
    """Return a normal no-action state."""

    return {"workflow_status": "no_action", **trace_update("no_action")}


@observe_node(
    "incident_handled",
    purpose="일반 운영 사고의 즉시 안전조치와 비차단 현장 알림을 완료하고 Rule/Agent 없이 정상 종료",
)
def incident_handled_node(state: LaroGraphState) -> dict:
    """Terminate an incident-only request after deterministic impact handling."""

    return {"workflow_status": "incident_handled", **trace_update("incident_handled")}



@observe_node(
    "input_rejected",
    purpose="코드 중심 입력 계약을 위반한 요청을 HITL 없이 구조화된 거절 결과로 종료",
)
def input_rejected_node(state: LaroGraphState) -> dict:
    """Reject malformed mission input without opening a human-decision checkpoint."""

    existing = state.get("input_rejection")
    if existing is not None:
        rejection = (
            existing
            if isinstance(existing, InputRejectionResult)
            else InputRejectionResult.model_validate(existing)
        )
    else:
        gate = state.get("request_gate_decision")
        reasons = list(getattr(gate, "reasons", []) if gate is not None else [])
        rejection = InputRejectionResult(
            reason_code="INVALID_MISSION_INPUT",
            message=(
                reasons[-1]
                if reasons
                else "Mission input must use canonical warehouse identifiers."
            ),
        )
    return {
        "input_rejection": rejection,
        "workflow_status": "input_rejected",
        **trace_update("input_rejected"),
    }


@observe_node("clarification_required", purpose="시스템 조회로 해결할 수 없는 사용자 모호성을 질문 형태로 정상 종료")
def clarification_required_node(state: LaroGraphState) -> dict:
    """Return a conversational clarification terminal distinct from safety review."""

    existing = state.get("clarification")
    if existing is not None:
        clarification = (
            existing
            if isinstance(existing, ClarificationResult)
            else ClarificationResult.model_validate(existing)
        )
    else:
        questions: list[str] = []
        issues = [
            value if isinstance(value, WorkflowValidationIssue) else WorkflowValidationIssue.model_validate(value)
            for value in state.get("validation_issues", [])
        ]
        questions.extend(
            value.message
            for value in issues
            if value.requires_user_clarification
        )
        issue_codes = {
            value.code
            for value in issues
            if value.requires_user_clarification
        }
        if issue_codes and issue_codes <= {"ENTITY_REFERENCE_NOT_FOUND"}:
            reason = "A requested warehouse identifier was not found in authoritative data."
        elif "ENTITY_REFERENCE_AMBIGUOUS" in issue_codes:
            reason = "Multiple authoritative warehouse entities matched the operator reference."
        else:
            reason = "The request contains ambiguity that authoritative warehouse data cannot resolve safely."
        clarification = ClarificationResult(
            reason=reason,
            questions=list(dict.fromkeys(questions)) or ["Please clarify the intended warehouse operation."],
        )
    return {
        "clarification": clarification,
        "workflow_status": "clarification_required",
        **trace_update("clarification_required"),
    }


@observe_node(
    "workflow_hold",
    purpose="승인된 예외 선택에 따라 자동화를 안전하게 보류하고 후속 사람 작업을 기록",
)
def workflow_hold_node(state: LaroGraphState) -> dict:
    """Terminate with an auditable hold instead of rerunning Agent/Solver work."""

    existing = state.get("workflow_hold")
    if existing is not None:
        hold = (
            existing
            if isinstance(existing, WorkflowHoldResult)
            else WorkflowHoldResult.model_validate(existing)
        )
    else:
        gate = state.get("request_gate_decision")
        gate_hold = getattr(gate, "workflow_hold", None) if gate is not None else None
        if isinstance(gate, dict):
            gate_hold = gate.get("workflow_hold")
        hold = (
            gate_hold
            if isinstance(gate_hold, WorkflowHoldResult)
            else WorkflowHoldResult.model_validate(gate_hold)
        )
    return {
        "workflow_hold": hold,
        "workflow_status": "held_for_human_action",
        **trace_update("workflow_hold"),
    }


@observe_node("human_review", purpose="정책·Optimizer·Traffic 또는 Recovery의 업무 검토 사유 조립")
def human_review_node(state: LaroGraphState) -> dict:
    """Collect business reasons that require operator judgment."""

    existing = state.get("human_review")
    if existing is not None:
        review = existing if isinstance(existing, HumanReviewResult) else HumanReviewResult.model_validate(existing)
    else:
        details: list[str] = []
        policy = state.get("policy_validation")
        if policy is not None:
            details.extend(value.message for value in policy.violations)
        optimizer = state.get("optimizer_result")
        if optimizer is not None and optimizer.reason:
            details.append(optimizer.reason)
        traffic = state.get("traffic_schedule")
        if traffic is not None:
            details.extend(traffic.conflicts)
        issues = [
            value if isinstance(value, WorkflowValidationIssue) else WorkflowValidationIssue.model_validate(value)
            for value in state.get("validation_issues", [])
        ]
        details.extend(value.message for value in issues if value.requires_human_review)
        review = HumanReviewResult(
            reason="Workflow needs an operator decision before execution.",
            details=list(dict.fromkeys(details)) or ["The selected route could not be completed automatically."],
        )
    return {"human_review": review, "workflow_status": "human_review", **trace_update("human_review")}


@observe_node("workflow_failure", purpose="기술 오류를 구조화된 실패 결과로 변환")
def workflow_failure_node(state: LaroGraphState) -> dict:
    """Build a technical failure terminal."""

    errors = [
        value if isinstance(value, WorkflowError) else WorkflowError.model_validate(value)
        for value in state.get("errors", [])
    ]
    return {
        "failure": WorkflowFailureResult(stage=state.get("failure_stage", "unknown"), errors=errors),
        "workflow_status": "failed",
        **trace_update("workflow_failure"),
    }


@observe_node("payload_ready", purpose="검증된 다중 Task cuOpt Payload를 Solver 호출 없이 내보낼 준비 상태로 확정")
def payload_ready_node(state: LaroGraphState) -> dict:
    """Terminate payload-only mode after all payload and candidate guards pass."""

    return {"workflow_status": "ready_for_cuopt", **trace_update("payload_ready")}


@observe_node("persist_result", purpose="최종 계획과 검증 결과를 runtime_outputs JSON 파일로 실제 저장")
def persist_result_node(state: LaroGraphState) -> dict:
    """Persist one execution artifact to the configured output directory."""

    if state.get("evaluation_shadow_mode", False):
        return {
            "persistence": PersistenceResult(
                status="skipped",
                reason="Deferred Rule/Agent comparison is read-only and stores results only in the evaluation directory.",
            ),
            **trace_update("persist_result"),
        }

    try:
        output_dir = get_settings().output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = output_dir / f"{state['simulation_id']}_{stamp}.json"
        payload = {
            key: _jsonable(value)
            for key, value in state.items()
            if key not in {"graph_nodes", "graph_node_types", "graph_arcs"}
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        result = PersistenceResult(status="stored", path=str(path))
    except Exception as exc:
        result = PersistenceResult(status="failed", reason=str(exc))
    return {"persistence": result, **trace_update("persist_result")}


@observe_node("dashboard_event", purpose="최종 Workflow 상태를 Dashboard/Stream envelope로 준비")
def dashboard_event_node(state: LaroGraphState) -> dict:
    """Prepare a dashboard event after persistence."""

    frontend = state.get("frontend_summary")
    headline = getattr(frontend, "headline", None) if frontend is not None else None
    summary_text = getattr(frontend, "summary_text", None) if frontend is not None else None
    next_action = getattr(frontend, "next_action", None) if frontend is not None else None
    if isinstance(frontend, dict):
        headline = frontend.get("headline")
        summary_text = frontend.get("summary_text")
        next_action = frontend.get("next_action")
    return {
        "dashboard_event": DashboardEvent(
            event_type="orchestration_completed",
            workflow_status=state.get("workflow_status", "failed"),
            headline=headline,
            summary_text=summary_text,
            next_action=next_action,
        ),
        **trace_update("dashboard_event"),
    }

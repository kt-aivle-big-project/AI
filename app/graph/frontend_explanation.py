"""Build a front-end execution summary from authoritative graph state."""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.llm_gateway import get_default_llm_gateway
from app.core.node_observability import observe_node
from app.domain.schemas import (
    FrontendExecutionSummary,
    FrontendNarrativeText,
    FrontendTimelineItem,
    LLMNodeSummary,
)
from app.graph.node_support import llm_summary, trace_update
from app.graph.state import LaroGraphState
from app.prompts.frontend_explanation import FRONTEND_EXPLANATION_SYSTEM, PROMPT_VERSION


def _value(value: Any, key: str, default: Any = None) -> Any:
    """Read one attribute from either a Pydantic object or a mapping."""

    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _unique(values: list[str]) -> list[str]:
    """Preserve display order while removing empty duplicates."""

    return list(dict.fromkeys(value for value in values if value))


def _status_text(status: str) -> tuple[str, str, str]:
    """Return deterministic headline, status label, and next action."""

    values = {
        "ready_for_cuopt": (
            "cuOpt 입력 검증 완료",
            "최적화 입력 준비 완료",
            "실제 cuOpt 연결을 사용하려면 optimization_backend=cuopt로 실행하세요.",
        ),
        "plan_validated": (
            "다중 로봇 실행 계획 검증 완료",
            "실행 계획 검증 완료",
            "검증된 계획을 WCS 실행 계층에 전달할 수 있습니다.",
        ),
        "clarification_required": (
            "추가 확인이 필요합니다",
            "사용자 확인 필요",
            "표시된 질문에 답한 뒤 동일 요청을 다시 실행하세요.",
        ),
        "input_rejected": (
            "입력 계약을 확인해 주세요",
            "요청 거절",
            "주문·입고·로봇·통로의 canonical ID를 사용해 요청을 다시 제출하세요.",
        ),
        "awaiting_clarification": (
            "사용자 답변을 기다리고 있습니다",
            "HITL 확인 대기",
            "HITL 카드의 선택지를 제출하면 저장된 요청이 이어서 실행됩니다.",
        ),
        "awaiting_human_approval": (
            "운영자 승인을 기다리고 있습니다",
            "HITL 승인 대기",
            "영향과 근거를 검토한 뒤 승인·거절 또는 대안을 선택하세요.",
        ),
        "cancelled": (
            "요청이 취소되었습니다",
            "취소됨",
            "새 요청을 제출하거나 취소 사유를 확인하세요.",
        ),
        "human_review": (
            "운영자 검토가 필요합니다",
            "사람 검토 필요",
            "정책·자원·안전 사유를 검토한 뒤 승인 또는 수정하세요.",
        ),
        "query_completed": (
            "창고 조회가 완료되었습니다",
            "조회 완료",
            "조회 결과를 확인하세요.",
        ),
        "no_action": (
            "실행할 작업이 없습니다",
            "작업 없음",
            "새 이벤트 또는 작업 명령을 기다립니다.",
        ),
        "incident_handled": (
            "운영 사고 안전조치가 반영되었습니다",
            "사고 자동 대응 완료",
            "현장 작업 알림과 영향 자원 상태를 확인하세요.",
        ),
        "held_for_human_action": (
            "자동화가 안전하게 보류되었습니다",
            "현장 확인 작업 대기",
            "재실사·복구·승인 작업이 완료된 뒤 새 Snapshot으로 다시 실행하세요.",
        ),
        "failed": (
            "워크플로 실행에 실패했습니다",
            "기술 오류",
            "오류 단계와 코드를 확인한 뒤 재시도하세요.",
        ),
    }
    return values.get(status, ("창고 오케스트레이션 완료", status, "결과를 확인하세요."))


def _timeline(state: LaroGraphState) -> list[FrontendTimelineItem]:
    """Convert actual node records and LLM summaries into front-end timeline rows."""

    rows: list[FrontendTimelineItem] = []
    summaries_by_node: dict[str, list[Any]] = {}
    for summary in state.get("llm_node_summaries", []) or []:
        summaries_by_node.setdefault(str(_value(summary, "node_name", "unknown")), []).append(summary)
    summary_cursor: dict[str, int] = {}
    for record in state.get("node_execution_log", []):
        name = _value(record, "node_name", "unknown")
        purpose = _value(record, "purpose", "")
        detail = str(purpose)
        if bool(_value(record, "llm_used", False)):
            values = summaries_by_node.get(str(name), [])
            cursor = summary_cursor.get(str(name), 0)
            if cursor < len(values):
                summary = values[cursor]
                summary_cursor[str(name)] = cursor + 1
                detail = (
                    f"{_value(summary, 'task_summary', purpose)} → "
                    f"{_value(summary, 'output_summary', '')}"
                ).strip(" →")
        if name in {"persist_result", "dashboard_event"}:
            phase = "OUTPUT"
        elif _value(record, "llm_used", False):
            phase = "LLM"
        elif "retrieval" in name or name in {"query_key_resolver", "agent_context_materializer"}:
            phase = "RETRIEVAL"
        elif "cuopt" in name or "optimizer" in name or "payload" in name:
            phase = "OPTIMIZATION"
        elif "mapf" in name or "route" in name or "traffic" in name:
            phase = "ROUTING"
        elif "validation" in name or "guard" in name or "validator" in name:
            phase = "VALIDATION"
        else:
            phase = "ORCHESTRATION"
        rows.append(
            FrontendTimelineItem(
                phase=phase,
                label=name,
                status=_value(record, "status", "success"),
                duration_ms=float(_value(record, "duration_ms", 0.0)),
                detail=detail,
                llm_used=bool(_value(record, "llm_used", False)),
            )
        )
    return rows


def _deterministic_summary(state: LaroGraphState) -> FrontendExecutionSummary:
    """Build factual cards and conservative prose without another LLM call."""

    status = str(state.get("workflow_status", "failed"))
    headline, status_label, next_action = _status_text(status)
    completed: list[str] = []
    resources: list[str] = []
    constraints: list[str] = []
    validations: list[str] = []
    warnings: list[str] = []

    normalized = state.get("normalized_request")
    operations = list(_value(normalized, "operations", []) or [])
    if operations:
        completed.append(f"요청 작업 {len(operations)}건을 공통 스키마로 해석했습니다.")
        resources.extend(str(_value(value, "operation_id", "")) for value in operations)

    incident_plan = state.get("incident_response_plan")
    if incident_plan is not None:
        incidents = list(_value(incident_plan, "incidents", []) or [])
        actions = list(_value(incident_plan, "immediate_actions", []) or [])
        if incidents:
            completed.append(
                f"운영 사고 {len(incidents)}건을 사건 종류가 아닌 영향 기준으로 정규화했습니다."
            )
        for action in actions:
            action_name = str(_value(action, "action", "NONE"))
            incident_id = str(_value(action, "incident_id", "incident"))
            execution_status = str(_value(action, "execution_status", "PLANNED"))
            if execution_status == "APPLIED":
                completed.append(
                    f"{incident_id}: 즉시 안전조치 {action_name}을 현재 계획 Overlay에 반영했습니다."
                )
            else:
                completed.append(f"{incident_id}: 즉시 안전조치 {action_name}을 적용 예정으로 기록했습니다.")
            resources.extend(
                f"영향 자원 {value}"
                for value in (_value(action, "affected_resource_ids", []) or [])
            )

    for notice in state.get("operator_notifications", []) or []:
        notice_type = str(_value(notice, "notification_type", "INFO"))
        title = str(_value(notice, "title", "운영 알림"))
        message = str(_value(notice, "message", ""))
        requires_response = bool(_value(notice, "requires_response", False))
        prefix = "사람 결정 필요" if requires_response else "현장 알림"
        warnings.append(f"[{prefix}/{notice_type}] {title}: {message}")

    observations = list(state.get("retrieval_observations", []) or [])
    if observations:
        tools = _unique([str(_value(value, "tool_name", "")) for value in observations])
        completed.append(f"읽기 전용 조회 도구 {len(tools)}종으로 상황 근거를 수집했습니다.")

    graph = state.get("warehouse_situation_graph")
    if graph is not None:
        completed.append(
            "Warehouse Situation Graph를 생성했습니다 "
            f"(노드 {len(_value(graph, 'nodes', []) or [])}, 관계 {len(_value(graph, 'relations', []) or [])}, "
            f"경로 근거 {len(_value(graph, 'path_evidence', []) or [])})."
        )

    draft = state.get("cuopt_dynamic_input_draft")
    if draft is not None:
        tasks = list(_value(draft, "tasks", []) or [])
        fleet = _value(draft, "fleet")
        included = list(_value(fleet, "included_robot_ids", []) or [])
        excluded = list(_value(fleet, "excluded_robot_ids", []) or [])
        completed.append(
            f"cuOpt 동적 입력에 작업 {len(tasks)}건과 후보 로봇 {len(included)}대를 구성했습니다."
        )
        for task in tasks:
            resources.append(
                f"{_value(task, 'order_id')} : {_value(task, 'pickup_node')} → {_value(task, 'delivery_node')}"
            )
        resources.extend(f"후보 로봇 {value}" for value in included)
        resources.extend(f"제외 로봇 {value}" for value in excluded)
        map_constraints = _value(draft, "map_constraints")
        constraints.extend(
            f"차단 Edge {value}" for value in (_value(map_constraints, "blocked_edge_ids", []) or [])
        )
        constraints.extend(
            f"Soft Avoid Edge {value}" for value in (_value(map_constraints, "soft_penalty_edge_ids", []) or [])
        )

    g2p = state.get("goods_to_person_compilation") or state.get("goods_to_person_plan")
    if g2p is not None:
        batches = list(_value(g2p, "batches", []) or [])
        station_actions = list(_value(g2p, "station_actions", []) or [])
        completed.append(
            f"출고 주문을 Handling Unit 기준 G2P Cycle {len(batches)}건으로 컴파일했습니다."
        )
        for batch in batches:
            resources.append(
                f"{_value(batch, 'handling_unit_id')} : "
                f"{_value(batch, 'source_access_node')} → "
                f"{_value(batch, 'station_access_node')} → "
                f"{_value(batch, 'post_station_node')}"
            )
            resources.append(
                f"논리 출고지: {', '.join(_value(batch, 'logical_destination_ids', []) or [])}"
            )
            if bool(_value(batch, "return_required", False)):
                constraints.append("잔량 Handling Unit은 같은 AMR이 원래 Rack Access로 반환")
            else:
                constraints.append("소진된 BOX는 고정 출고 설비에서 처리 완료되고 빈 BOX는 남지 않음")
        if station_actions:
            completed.append(
                f"고정 Station Robot 작업 {len(station_actions)}단계를 실행 계획에 포함했습니다."
            )
        warnings.extend(list(_value(g2p, "warnings", []) or []))
        warnings.extend(list(_value(g2p, "errors", []) or []))

        route_enrichment = state.get("goods_to_person_route_enrichment")
        if route_enrichment is not None and bool(_value(route_enrichment, "applied", False)):
            assignments = _value(route_enrichment, "batch_robot_assignments", {}) or {}
            completed.append(
                f"Solver 배정 이후 같은 AMR 후속 이동 {len(assignments)}건을 공통 MAPF 입력에 추가했습니다."
            )
            resources.extend(
                f"{batch_id} 후속 이동 담당 {robot_id}"
                for batch_id, robot_id in assignments.items()
            )
            warnings.extend(list(_value(route_enrichment, "warnings", []) or []))
            warnings.extend(list(_value(route_enrichment, "errors", []) or []))

    enrichment = state.get("cuopt_evidence_enrichment")
    if enrichment is not None and bool(_value(enrichment, "applied", False)):
        added = sum(len(values) for values in (_value(enrichment, "added_task_evidence", {}) or {}).values())
        completed.append(f"선택 결과는 유지한 채 누락된 근거 링크 {added}개를 기계적으로 보완했습니다.")
        warnings.extend(list(_value(enrichment, "warnings", []) or []))

    for key, label in [
        ("situation_graph_validation", "상황 그래프"),
        ("cuopt_dynamic_input_validation", "cuOpt 동적 입력"),
        ("payload_validation", "cuOpt Payload"),
        ("candidate_space_validation", "후보 공간"),
        ("optimizer_assignment_validation", "Optimizer 배정"),
        ("route_validation", "정적 경로"),
        ("mapf_validation", "MAPF"),
    ]:
        result = state.get(key)
        if result is None:
            continue
        valid = bool(_value(result, "valid", False))
        validations.append(f"{label}: {'통과' if valid else '실패'}")
        warnings.extend(list(_value(result, "warnings", []) or []))
        warnings.extend(list(_value(result, "errors", []) or []) if not valid else [])

    clarification = state.get("clarification")
    if clarification is not None:
        warnings.extend(list(_value(clarification, "questions", []) or []))
    input_rejection = state.get("input_rejection")
    if input_rejection is not None:
        warnings.append(str(_value(input_rejection, "message", "Invalid mission input.")))
        invalid_refs = list(_value(input_rejection, "invalid_references", []) or [])
        required_ids = list(_value(input_rejection, "required_identifier_types", []) or [])
        resources.extend(f"거절된 참조: {value}" for value in invalid_refs)
        constraints.extend(f"필요 코드: {value}" for value in required_ids)
    review = state.get("human_review")
    if review is not None:
        warnings.extend(list(_value(review, "details", []) or []))
    pending_interaction = state.get("pending_human_interaction")
    if pending_interaction is not None:
        prompt = str(_value(pending_interaction, "prompt", "") or "")
        if prompt:
            warnings.append(prompt)
        options = list(_value(pending_interaction, "options", []) or [])
        if options:
            resources.extend(
                f"HITL 선택지: {_value(value, 'label', _value(value, 'option_id', 'option'))}"
                for value in options
            )
        next_action = (
            "POST /hitl/{interaction_id}/respond로 선택 또는 승인을 제출하세요."
        )
    optimizer = state.get("optimizer_result")
    optimizer_estimated_makespan: float | None = None
    if optimizer is not None:
        optimizer_status = str(_value(optimizer, "status", "unknown"))
        optimizer_name = str(_value(optimizer, "optimizer", "optimizer"))
        if optimizer_status == "success":
            validations.append(f"{optimizer_name}: 통과")
            global_cost = _value(optimizer, "global_objective_cost", None)
            optimizer_estimated_makespan = _value(
                optimizer,
                "estimated_makespan_ms",
                None,
            )
            if global_cost is not None:
                validations.append(f"cuOpt 전역 목적값: {float(global_cost):.3f} cost units")
            if optimizer_estimated_makespan is not None:
                validations.append(
                    "Optimizer 예상 makespan: "
                    f"{float(optimizer_estimated_makespan):.0f}ms"
                )
            route_values = list(_value(optimizer, "routes", []) or [])
            for route in route_values:
                completion = _value(route, "completion_ms", None)
                arrival = _value(route, "last_task_arrival_ms", None)
                if completion is None and arrival is None:
                    continue
                vehicle_id = str(_value(route, "vehicle_id", "robot"))
                resources.append(
                    f"{vehicle_id}: 마지막 도착={arrival}ms, 완료={completion}ms"
                )
        else:
            validations.append(f"{optimizer_name}: 실패 ({optimizer_status})")
            reason = str(_value(optimizer, "reason", "") or "")
            if reason:
                warnings.append(reason)

    schedule = state.get("traffic_schedule")
    if schedule is not None:
        service_ms = int(_value(schedule, "total_service_ms", 0) or 0)
        wait_ms = int(_value(schedule, "total_wait_ms", 0) or 0)
        makespan_ms = int(_value(schedule, "makespan_ms", 0) or 0)
        completed.append(
            "실행 시간표에 물품 Pickup/Drop 처리시간 "
            f"{service_ms}ms와 대기시간 {wait_ms}ms를 반영했습니다."
        )
        validations.append(f"MAPF makespan: {makespan_ms}ms")
        if optimizer_estimated_makespan is not None:
            validations.append(
                "MAPF 추가 지연: "
                f"{float(makespan_ms) - float(optimizer_estimated_makespan):.0f}ms"
            )

    failure = state.get("failure")
    if failure is not None:
        failure_stage = str(_value(failure, "stage", "unknown"))
        failure_errors = list(_value(failure, "errors", []) or [])
        for value in failure_errors:
            code = str(_value(value, "code", "workflow_error"))
            message = str(_value(value, "message", value))
            warnings.append(f"[{failure_stage}/{code}] {message}")
        if failure_stage == "optimizer":
            headline = "NVIDIA cuOpt 최적화 요청에 실패했습니다"
            status_label = "최적화 서비스 오류"
            next_action = "NVIDIA 응답 본문과 cuOpt 입력 모델을 확인한 뒤 다시 실행하세요."

    llm_attempts = sum(
        1
        for record in state.get("node_execution_log", []) or []
        if bool(_value(record, "llm_used", False))
    )
    retrieval_steps = int(state.get("retrieval_agent_step_count", 0))
    formulation_retries = int(state.get("formulation_retry_count", 0))
    debug_note = (
        f"LLM 노드 시도 {llm_attempts}회, 조회 Agent 단계 {retrieval_steps}회, "
        f"cuOpt 정식화 수정 {formulation_retries}회; 최종 상태={status}."
    )
    summary_text = " ".join(completed[:4]) or "워크플로가 종료되었으며 상세 결과는 타임라인에서 확인할 수 있습니다."
    return FrontendExecutionSummary(
        generation_source="deterministic",
        language=get_settings().frontend_explanation_language,
        headline=headline,
        status_label=status_label,
        summary_text=summary_text,
        completed_actions=_unique(completed),
        selected_resources=_unique(resources),
        applied_constraints=_unique(constraints),
        validation_summary=_unique(validations),
        warnings=_unique(warnings),
        next_action=next_action,
        debug_note=debug_note,
        timeline=_timeline(state),
    )


@observe_node(
    "frontend_explanation",
    purpose="실제 실행·검증 사실을 프론트용 카드·타임라인과 자연어 설명으로 조립",
    llm_used=False,
)
def frontend_explanation_node(state: LaroGraphState) -> dict:
    """Create an operator-facing summary; prose failure never alters planning."""

    deterministic = _deterministic_summary(state)
    settings = get_settings()
    mode = settings.frontend_explanation_mode.casefold()
    summaries: list[LLMNodeSummary] = []
    used_llm = False
    if mode == "off":
        result = deterministic.model_copy(update={"generation_source": "off"})
    elif mode == "deterministic":
        result = deterministic
    else:
        try:
            used_llm = True
            narrative = get_default_llm_gateway().invoke_structured(
                system_prompt=FRONTEND_EXPLANATION_SYSTEM,
                user_payload={
                    "status": state.get("workflow_status", "failed"),
                    "completed_actions": deterministic.completed_actions,
                    "selected_resources": deterministic.selected_resources,
                    "applied_constraints": deterministic.applied_constraints,
                    "validation_summary": deterministic.validation_summary,
                    "warnings": deterministic.warnings,
                    "next_action": deterministic.next_action,
                    "debug_note": deterministic.debug_note,
                },
                output_model=FrontendNarrativeText,
                trace_name="LARO::frontend_explanation",
                tags=["node:frontend_explanation", f"prompt-v{PROMPT_VERSION}"],
                metadata={
                    "laro_node": "frontend_explanation",
                    "simulation_id": state["simulation_id"],
                    "workflow_status": state.get("workflow_status", "failed"),
                },
            )
            result = deterministic.model_copy(
                update={
                    "generation_source": "llm",
                    "headline": narrative.headline,
                    "summary_text": narrative.summary_text,
                    "next_action": narrative.next_action,
                    "debug_note": narrative.debug_note,
                }
            )
            summaries.append(
                llm_summary(
                    node_name="frontend_explanation",
                    prompt_version=PROMPT_VERSION,
                    task_summary="검증된 실행 사실을 운영자용 자연어 결과로 설명",
                    input_summary=(
                        f"status={state.get('workflow_status')}, timeline={len(deterministic.timeline)}, "
                        f"warnings={len(deterministic.warnings)}"
                    ),
                    output_summary=f"headline={narrative.headline}",
                )
            )
        except Exception as exc:
            result = deterministic.model_copy(
                update={
                    "generation_source": "deterministic_fallback",
                    "debug_note": deterministic.debug_note + f" Frontend LLM fallback: {type(exc).__name__}.",
                }
            )
    update: dict[str, Any] = {
        "_llm_used": used_llm,
        "frontend_summary": result,
        **trace_update("frontend_explanation"),
    }
    if summaries:
        update["llm_node_summaries"] = summaries
    return update

from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query

from app.backend_integration import (
    BackendOptimizationRequest,
    BackendOptimizationResponse,
    BackendReoptimizationRequest,
    BackendReoptimizationResponse,
    load_backend_map,
    optimize_for_backend,
    reoptimize_for_backend,
)
from app.config import get_settings
from app.execution import handle_robot_event
from app.models import (
    ClarificationResponse,
    EventReplanDecisionRequest,
    ExecutionDispatchCancelRequest,
    ExecutionDispatchRetryRequest,
    NaturalLanguageCommand,
    PlanExecutionApprovalRequest,
    PlanExecutionDispatchRequest,
    RobotCommandAckRequest,
    RobotEvent,
    ScenarioComparisonRequest,
    SimulationResetRequest,
)
from app.planning import run_planning
from app.services.container import get_services
from app.services.conversation import ConversationAccessError
from app.services.command_language import parse_deterministic_command
from app.services.scenario_comparison import (
    ScenarioComparisonLimitError,
    ScenarioComparisonService,
)
from app.services.event_replan import (
    EventReplanConflictError,
    EventReplanNotFoundError,
    EventReplanService,
)
from app.services.response_view import shape_planning_response
from app.services.integration_views import (
    build_debug_view,
    build_execution_status_view,
    build_planning_ui_view,
    build_simulation_view,
)
from app.integration_models import (
    DebugPlanningResponse,
    ExecutionStatusResponse,
    PlanningUiResponse,
    SimulationViewResponse,
)
from app.services.execution_delivery import (
    ExecutionApprovalError,
    ExecutionConflictError,
    ExecutionDeliveryService,
    ExecutionNotFoundError,
    ExecutionRetryExhaustedError,
    ExecutionSequenceError,
)
from app.services.robot_adapter import RobotAdapter
from app.services.robot_gateway import RobotGateway
from app.services.schedule_dispatcher import ready_only_plan_payload
from app.services.simulation_reset import (
    SimulationNotFoundError,
    SimulationResetService,
    summarize_simulation_state,
)


app = FastAPI(
    title="Warehouse Planning Supervisor",
    version="2.5.15.3",
    swagger_ui_parameters={
        "syntaxHighlight": False,
        "defaultModelsExpandDepth": -1,
    },
)


def _stored_plan_payload(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
    output = row.get("output_payload") or {}
    verification = output.get("verification_decision") or {}
    decision = str(verification.get("decision") or "")
    payload = {
        "plan_version": row.get("plan_version"),
        "command_id": row.get("command_id"),
        "warehouse_id": row.get("warehouse_id"),
        "scope": output.get("scope") or {},
        "required_tasks": output.get("required_tasks") or [],
        "cuopt_plan": output.get("cuopt_plan") or {},
        "collision_plan": output.get("collision_plan") or {},
        "inventory_operations": output.get("inventory_operations") or [],
        "charger_node_ids": output.get("charger_node_ids") or [],
        "execution_task_dependencies": output.get(
            "execution_task_dependencies"
        ) or [],
        "scheduled_task_constraints": output.get(
            "scheduled_task_constraints"
        ) or [],
        "ready_task_ids": output.get("ready_task_ids") or [],
        "waiting_task_ids": output.get("waiting_task_ids") or [],
        "blocked_task_ids": output.get("blocked_task_ids") or [],
        "reference_time": output.get("reference_time"),
        "time_step_seconds": output.get("time_step_seconds")
        or get_settings().time_step_seconds,
    }
    return payload, decision


def _execution_gateway() -> RobotGateway:
    settings = get_settings()
    return RobotGateway(
        settings.robot_gateway_url,
        settings.request_timeout_seconds,
        max_attempts=1,
        retry_backoff_seconds=settings.robot_gateway_retry_backoff_seconds,
    )


def _raise_execution_http(exc: Exception) -> None:
    if isinstance(exc, ExecutionNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (ExecutionApprovalError, ExecutionConflictError, ExecutionSequenceError),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ExecutionRetryExhaustedError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    missing = settings.missing_for_connections()
    if missing:
        return {"status": "not_configured", "missing": missing}
    try:
        dependencies = get_services().healthcheck()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "unavailable",
                "error_type": exc.__class__.__name__,
            },
        ) from None
    status = (
        "ok"
        if all(item.get("ok") is True for item in dependencies.values())
        else "degraded"
    )
    return {"status": status, "dependencies": dependencies}


@app.post(
    "/optimize",
    response_model=BackendOptimizationResponse,
    response_model_by_alias=True,
)
def backend_optimize(
    request: BackendOptimizationRequest,
) -> BackendOptimizationResponse:
    """Spring BE-main compatible initial route optimization endpoint."""

    try:
        return optimize_for_backend(request, get_settings())
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/reoptimize",
    response_model=BackendReoptimizationResponse,
    response_model_by_alias=True,
)
def backend_reoptimize(
    request: BackendReoptimizationRequest,
) -> BackendReoptimizationResponse:
    """Spring BE-main compatible runtime reoptimization endpoint."""

    settings = get_settings()
    try:
        backend_map = load_backend_map(request.warehouse_id, settings)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Backend warehouse map is unavailable",
        ) from exc
    try:
        return reoptimize_for_backend(request, settings, backend_map)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/planning/commands")
def planning_command(command: NaturalLanguageCommand) -> dict[str, Any]:
    try:
        interpretation = parse_deterministic_command(command.text)
        if interpretation.intent == "SCENARIO_COMPARISON":
            return ScenarioComparisonService(get_services()).execute(
                ScenarioComparisonRequest(
                    comparison_id=command.command_id,
                    warehouse_id=command.warehouse_id,
                    conversation_id=command.conversation_id,
                    text=command.text,
                )
            )
        result = run_planning(command)
        return shape_planning_response(result, command.response_view)
    except ScenarioComparisonLimitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConversationAccessError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/scenario-comparisons")
def create_scenario_comparison(
    request: ScenarioComparisonRequest,
) -> dict[str, Any]:
    try:
        return ScenarioComparisonService(get_services()).execute(request)
    except ScenarioComparisonLimitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/scenario-comparisons")
def list_scenario_comparisons(
    warehouse_id: int | None = None,
    conversation_id: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        rows = get_services().postgres.list_scenario_comparisons(
            warehouse_id=warehouse_id,
            conversation_id=conversation_id,
            status=status,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
        return {
            "comparisons": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/scenario-comparisons/{comparison_id}")
def get_scenario_comparison(comparison_id: str) -> dict[str, Any]:
    try:
        row = get_services().postgres.get_scenario_comparison(comparison_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="comparison_id를 찾을 수 없습니다.")
    return row


@app.get(
    "/v1/scenario-comparisons/{comparison_id}/scenarios/{scenario_id}"
)
def get_scenario_comparison_run(
    comparison_id: str,
    scenario_id: str,
) -> dict[str, Any]:
    try:
        row = get_services().postgres.get_scenario_comparison_run(
            comparison_id,
            scenario_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="scenario_id를 찾을 수 없습니다.")
    return row


def _clarification_followup_text(
    original_text: str,
    response: ClarificationResponse,
) -> tuple[str, str]:
    selected = response.selected_value
    requested_mode = "AUTO"
    phrases = {
        "PLAN_ONLY": "실행하지 말고 계획만 만들어줘",
        "SIMULATE_ONLY": "실제 반영하지 말고 시뮬레이션해줘",
        "EXECUTE": "최신 상태를 검증한 뒤 실제 실행해줘",
        "MINIMIZE_DISTANCE": "이동거리 최소화로 계획해줘",
        "MINIMIZE_MAKESPAN": "완료시간 최소화로 계획해줘",
        "MINIMIZE_TARDINESS": "마감 준수와 지연 최소화로 계획해줘",
        "MINIMIZE_ENERGY": "에너지 최소화로 계획해줘",
    }
    if selected in {"PLAN_ONLY", "SIMULATE_ONLY", "EXECUTE"}:
        requested_mode = selected
    cleaned = original_text
    if selected and (selected.startswith("R-") or selected.startswith("W-")):
        for phrase in (
            "그 로봇",
            "아까 작업",
            "그 작업",
            "저 계획",
            "이 계획",
            "문제 있는 작업",
        ):
            cleaned = cleaned.replace(phrase, selected)
    answer_text = response.text.strip() if response.text else ""
    resolution = phrases.get(selected or "", answer_text or str(selected or ""))
    return f"{cleaned}\n추가 답변: {resolution}".strip(), requested_mode


@app.post("/v1/clarifications/{clarification_id}/responses")
def respond_to_clarification(
    clarification_id: str,
    response: ClarificationResponse,
) -> dict[str, Any]:
    try:
        repository = get_services().postgres
        request_row = repository.get_clarification_request(clarification_id)
        if request_row is None:
            raise HTTPException(status_code=404, detail="clarification_id를 찾을 수 없습니다.")
        expected_conversation = request_row.get("conversation_id")
        if (
            response.conversation_id
            and expected_conversation
            and response.conversation_id != expected_conversation
        ):
            raise HTTPException(
                status_code=409,
                detail="다른 conversation의 clarification에는 응답할 수 없습니다.",
            )
        if request_row.get("status") == "RESOLVED":
            return {
                "status": "ALREADY_RESOLVED",
                "clarification_id": clarification_id,
                "conversation_id": expected_conversation,
                "command_id": request_row.get("resolved_command_id"),
            }
        if request_row.get("status") == "EXPIRED":
            raise HTTPException(status_code=409, detail="만료된 clarification입니다.")

        new_command_id = str(uuid4())
        resolved = repository.resolve_clarification_request(
            clarification_id,
            response=response.model_dump(mode="json"),
            resolved_command_id=new_command_id,
        )
        if resolved is None:
            raise HTTPException(status_code=404, detail="clarification_id를 찾을 수 없습니다.")
        if resolved.get("resolved_command_id") != new_command_id:
            return {
                "status": "ALREADY_RESOLVED",
                "clarification_id": clarification_id,
                "conversation_id": expected_conversation,
                "command_id": resolved.get("resolved_command_id"),
            }
        if response.selected_value == "REAL_EVENT":
            return {
                "status": "REAL_EVENT_API_REQUIRED",
                "clarification_id": clarification_id,
                "conversation_id": expected_conversation,
                "command_id": new_command_id,
                "message": (
                    "실제 운영 이벤트는 자연어 계획 명령으로 자동 반영하지 않습니다. "
                    "검증된 이벤트를 /v1/execution/events로 전송해 주세요."
                ),
            }
        text_value, requested_mode = _clarification_followup_text(
            str(request_row["original_text"]),
            response,
        )
        command = NaturalLanguageCommand(
            command_id=new_command_id,
            warehouse_id=int(request_row["warehouse_id"]),
            text=text_value,
            requested_execution_mode=requested_mode,
            conversation_id=expected_conversation,
            parent_command_id=str(request_row["command_id"]),
            clarification_id=clarification_id,
        )
        result = run_planning(command)
        return shape_planning_response(result, command.response_view)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/commands")
def list_commands(
    warehouse_id: int | None = None,
    actor_id: str | None = None,
    status: str | None = None,
    requested_execution_mode: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        commands = get_services().postgres.list_command_history(
            warehouse_id=warehouse_id,
            actor_id=actor_id,
            status=status,
            requested_execution_mode=requested_execution_mode,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        return {
            "commands": commands,
            "limit": limit,
            "offset": offset,
            "count": len(commands),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/commands/{command_id}")
def get_command(command_id: str) -> dict[str, Any]:
    try:
        history = get_services().postgres.get_command_history(command_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if history is None:
        raise HTTPException(status_code=404, detail="command_id를 찾을 수 없습니다.")
    return {
        "command_history": history,
        "simulation_id": history.get("simulation_id"),
        "plan_version": history.get("plan_version"),
        "result_summary": history.get("result_summary"),
        "error_summary": history.get("error_summary"),
    }


@app.get(
    "/v1/commands/{command_id}/result",
    response_model=PlanningUiResponse,
)
def get_command_result(command_id: str) -> PlanningUiResponse:
    try:
        postgres = get_services().postgres
        history = postgres.get_command_history(command_id)
        if history is None:
            raise HTTPException(status_code=404, detail="command_id를 찾을 수 없습니다.")
        stored = postgres.get_latest_command_plan_evidence(command_id)
        output = (stored or {}).get("output_payload") or {}
        return build_planning_ui_view(history, output)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get(
    "/v1/commands/{command_id}/debug",
    response_model=DebugPlanningResponse,
)
def get_command_debug(command_id: str) -> DebugPlanningResponse:
    try:
        postgres = get_services().postgres
        history = postgres.get_command_history(command_id)
        if history is None:
            raise HTTPException(status_code=404, detail="command_id를 찾을 수 없습니다.")
        stored = postgres.get_latest_command_plan_evidence(command_id)
        output = (stored or {}).get("output_payload") or {}
        return build_debug_view(history, output)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/commands/{command_id}/stages")
def get_command_stages(command_id: str) -> dict[str, Any]:
    try:
        history = get_services().postgres.get_command_history(command_id)
        if history is None:
            raise HTTPException(status_code=404, detail="command_id를 찾을 수 없습니다.")
        stages = get_services().postgres.list_planning_stage_logs(command_id)
        return {"command_id": command_id, "stages": stages}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/commands/{command_id}/plan-evidence")
def get_command_plan_evidence(
    command_id: str,
    include_candidates: bool = False,
    include_routes: bool = True,
    include_reservations: bool = True,
) -> dict[str, Any]:
    try:
        postgres = get_services().postgres
        history = postgres.get_command_history(command_id)
        if history is None:
            raise HTTPException(
                status_code=404,
                detail="command_id를 찾을 수 없습니다.",
            )
        stored = postgres.get_latest_command_plan_evidence(command_id)
        if stored is None:
            return {"status": "NO_PLAN_EVIDENCE", "command_id": command_id}
        output = stored.get("output_payload") or {}
        optimization = output.get("optimization_evidence") or []
        routing = output.get("routing_evidence") or {}
        reservations = output.get("reservation_evidence") or {}
        comparison = output.get("distance_comparison") or {}
        if not any((optimization, routing, reservations, comparison)):
            return {"status": "NO_PLAN_EVIDENCE", "command_id": command_id}
        if not include_candidates:
            optimization = [
                {
                    key: value
                    for key, value in row.items()
                    if key != "candidates"
                }
                for row in optimization
            ]
        return {
            "status": "AVAILABLE",
            "command_id": command_id,
            "plan_version": stored.get("plan_version")
            or history.get("plan_version"),
            "optimization_evidence": optimization,
            "objective_breakdown": output.get("objective_breakdown") or {},
            "operational_objective": output.get("operational_objective") or {},
            "routing_evidence": routing if include_routes else {},
            "reservation_evidence": reservations if include_reservations else {},
            "distance_comparison": comparison,
            "verification_evidence": output.get("verification_evidence") or [],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    try:
        repository = get_services().postgres
        conversation = repository.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(
                status_code=404,
                detail="conversation_id를 찾을 수 없습니다.",
            )
        commands = repository.list_conversation_commands(
            conversation_id,
            limit=20,
            offset=0,
        )
        clarification = None
        if conversation.get("active_clarification_id"):
            clarification = repository.get_clarification_request(
                conversation["active_clarification_id"]
            )
        return {
            "conversation": conversation,
            "active_constraints": conversation.get("resolved_constraints") or {},
            "active_command_id": conversation.get("active_command_id"),
            "active_plan_version": conversation.get("active_plan_version"),
            "active_simulation_id": conversation.get("active_simulation_id"),
            "recent_commands": commands,
            "clarification": clarification,
            "result_summary": conversation.get("summary") or {},
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/conversations/{conversation_id}/commands")
def get_conversation_commands(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        repository = get_services().postgres
        if repository.get_conversation(conversation_id) is None:
            raise HTTPException(
                status_code=404,
                detail="conversation_id를 찾을 수 없습니다.",
            )
        commands = repository.list_conversation_commands(
            conversation_id,
            limit=limit,
            offset=offset,
        )
        return {
            "conversation_id": conversation_id,
            "commands": commands,
            "count": len(commands),
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/execution/events")
def execution_event(event: RobotEvent) -> dict[str, Any]:
    try:
        return EventReplanService(get_services()).handle(event)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/execution/events/{event_id}")
def get_execution_event(event_id: str) -> dict[str, Any]:
    try:
        row = get_services().postgres.get_execution_event_processing(event_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="event_id를 찾을 수 없습니다.")
    return row


@app.get("/v1/event-replans/{request_id}")
def get_event_replan(request_id: str) -> dict[str, Any]:
    try:
        row = get_services().postgres.get_automatic_replan_request(request_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="request_id를 찾을 수 없습니다.")
    return row


@app.get("/v1/warehouses/{warehouse_id}/event-replans")
def list_warehouse_event_replans(
    warehouse_id: int,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        rows = get_services().postgres.list_automatic_replan_requests(
            warehouse_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {
            "warehouse_id": warehouse_id,
            "event_replans": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/event-replans/{request_id}/approve")
def approve_event_replan(
    request_id: str,
    decision: EventReplanDecisionRequest,
) -> dict[str, Any]:
    try:
        return EventReplanService(get_services()).approve(request_id, decision)
    except EventReplanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EventReplanConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/event-replans/{request_id}/reject")
def reject_event_replan(
    request_id: str,
    decision: EventReplanDecisionRequest,
) -> dict[str, Any]:
    try:
        return EventReplanService(get_services()).reject(request_id, decision)
    except EventReplanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EventReplanConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/execution/plans/{plan_version}/approve")
def approve_execution_plan(
    plan_version: str,
    request: PlanExecutionApprovalRequest,
) -> dict[str, Any]:
    try:
        services = get_services()
        row = services.postgres.get_plan_run_by_version(
            plan_version, warehouse_id=request.warehouse_id
        )
        if row is None:
            raise ExecutionNotFoundError("PLAN_VERSION_NOT_FOUND")
        payload, decision = _stored_plan_payload(row)
        return ExecutionDeliveryService(services).approve_plan(
            plan_version=plan_version,
            command_id=row.get("command_id"),
            warehouse_id=request.warehouse_id,
            verification_decision=decision,
            plan_payload=payload,
            request=request,
        )
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.get("/v1/execution/plans/{plan_version}/approval")
def get_execution_plan_approval(plan_version: str) -> dict[str, Any]:
    try:
        return ExecutionDeliveryService(get_services()).get_approval(plan_version)
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.get(
    "/v1/execution/plans/{plan_version}/status",
    response_model=ExecutionStatusResponse,
)
def get_execution_plan_status(plan_version: str) -> ExecutionStatusResponse:
    try:
        postgres = get_services().postgres
        run = postgres.get_plan_run_by_version(plan_version)
        if run is None:
            raise HTTPException(status_code=404, detail="plan_version을 찾을 수 없습니다.")
        approval = postgres.get_execution_plan_approval(plan_version)
        dispatch = postgres.get_latest_execution_dispatch_by_plan_version(plan_version)
        return build_execution_status_view(
            run,
            approval=approval,
            dispatch=dispatch,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/execution/plans/{plan_version}/dispatch")
def dispatch_approved_execution_plan(
    plan_version: str,
    request: PlanExecutionDispatchRequest,
) -> dict[str, Any]:
    try:
        services = get_services()
        live = services.redis.live_snapshot(request.warehouse_id)
        active_version = str(live.get("active_plan_version") or "")
        if active_version != plan_version:
            raise ExecutionConflictError(
                f"ACTIVE_PLAN_VERSION_MISMATCH:{active_version or 'NONE'}:{plan_version}"
            )
        plan = live.get("active_plan") or {}
        ready_task_ids = list(plan.get("ready_task_ids") or [])
        if not ready_task_ids:
            return {
                "accepted": True,
                "status": "WAITING_FOR_READY_TASK",
                "plan_version": plan_version,
                "received_robot_count": 0,
            }
        payload = ready_only_plan_payload(plan, ready_task_ids)
        adapter = RobotAdapter(
            time_step_seconds=int(
                plan.get("time_step_seconds")
                or get_settings().time_step_seconds
            )
        )
        batches, validation = adapter.adapt(plan_version, payload)
        if not validation.get("valid") or not batches:
            raise ExecutionSequenceError(
                "ROBOT_ADAPTER_VALIDATION_FAILED:"
                + ",".join(validation.get("errors") or ["EMPTY_COMMAND_BATCH"])
            )
        result = ExecutionDeliveryService(
            services, gateway=_execution_gateway()
        ).dispatch(
            plan_version=plan_version,
            warehouse_id=request.warehouse_id,
            command_id=plan.get("command_id"),
            batches=[batch.model_dump(mode="json") for batch in batches],
            previous_active_plan_version=plan.get(
                "previous_active_plan_version"
            ),
            max_attempts=request.max_attempts,
        )
        return {**result, "adapter_validation": validation}
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.get("/v1/execution/dispatches/{dispatch_id}")
def get_execution_dispatch(dispatch_id: str) -> dict[str, Any]:
    try:
        return ExecutionDeliveryService(get_services()).get_dispatch(dispatch_id)
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.post("/v1/execution/dispatches/{dispatch_id}/acks")
def acknowledge_robot_command(
    dispatch_id: str,
    request: RobotCommandAckRequest,
) -> dict[str, Any]:
    try:
        return ExecutionDeliveryService(
            get_services(), gateway=_execution_gateway()
        ).acknowledge(dispatch_id, request)
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.post("/v1/execution/dispatches/{dispatch_id}/retry")
def retry_execution_dispatch(
    dispatch_id: str,
    request: ExecutionDispatchRetryRequest,
) -> dict[str, Any]:
    try:
        return ExecutionDeliveryService(
            get_services(), gateway=_execution_gateway()
        ).retry(dispatch_id, request)
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.post("/v1/execution/dispatches/{dispatch_id}/cancel")
def cancel_execution_dispatch(
    dispatch_id: str,
    request: ExecutionDispatchCancelRequest,
) -> dict[str, Any]:
    try:
        return ExecutionDeliveryService(
            get_services(), gateway=_execution_gateway()
        ).cancel(dispatch_id, request)
    except Exception as exc:
        _raise_execution_http(exc)
        raise AssertionError("unreachable")


@app.post("/v1/simulations/{simulation_id}/reset")
def reset_simulation(
    simulation_id: str,
    request: SimulationResetRequest,
) -> dict[str, Any]:
    try:
        return SimulationResetService(get_services()).reset_simulation(
            simulation_id,
            request,
        )
    except SimulationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"message": str(exc), "command_id": exc.command_id},
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/warehouses/{warehouse_id}/simulations/reset-all")
def reset_all_simulations(
    warehouse_id: int,
    request: SimulationResetRequest,
) -> dict[str, Any]:
    try:
        return SimulationResetService(get_services()).reset_all_simulations(
            warehouse_id,
            request,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/simulations")
def list_simulations(
    warehouse_id: int | None = None,
    status: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        rows = get_services().postgres.list_simulation_sessions(
            warehouse_id=warehouse_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        return {"simulations": rows, "count": len(rows), "limit": limit, "offset": offset}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/simulations/{simulation_id}")
def get_simulation(simulation_id: str) -> dict[str, Any]:
    try:
        postgres = get_services().postgres
        session = postgres.get_simulation_session(simulation_id)
        if session is None:
            raise HTTPException(status_code=404, detail="simulation_id를 찾을 수 없습니다.")
        summary = {
            key: value
            for key, value in session.items()
            if key not in {"base_state", "current_state"}
        }
        return {
            "simulation_session": summary,
            "base_state_summary": summarize_simulation_state(session.get("base_state")),
            "current_state_summary": summarize_simulation_state(
                session.get("current_state")
            ),
            "related_command_ids": list(
                dict.fromkeys(
                    value
                    for value in (
                        session.get("created_by_command_id"),
                        session.get("last_command_id"),
                    )
                    if value
                )
            ),
            "latest_run": postgres.get_latest_simulation_run(simulation_id),
            "reset_audits": postgres.list_simulation_reset_audits(
                simulation_id=simulation_id,
                limit=20,
                offset=0,
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get(
    "/v1/simulations/{simulation_id}/view",
    response_model=SimulationViewResponse,
    response_model_exclude_none=True,
)
def get_simulation_view(simulation_id: str) -> SimulationViewResponse:
    try:
        postgres = get_services().postgres
        run = postgres.get_latest_simulation_run(simulation_id)
        if run is None:
            raise HTTPException(status_code=404, detail="simulation_id를 찾을 수 없습니다.")
        return build_simulation_view(run)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/simulations/{simulation_id}/state")
def get_simulation_state(simulation_id: str) -> dict[str, Any]:
    try:
        session = get_services().postgres.get_simulation_session(simulation_id)
        if session is None:
            raise HTTPException(status_code=404, detail="simulation_id를 찾을 수 없습니다.")
        return {
            "simulation_id": simulation_id,
            "base_state": session.get("base_state"),
            "current_state": session.get("current_state"),
            "checkpoint": session.get("checkpoint"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/simulations/{simulation_id}/runs")
def get_simulation_runs(
    simulation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        postgres = get_services().postgres
        if postgres.get_simulation_session(simulation_id) is None:
            raise HTTPException(status_code=404, detail="simulation_id를 찾을 수 없습니다.")
        rows = postgres.list_simulation_runs(
            simulation_id,
            limit=limit,
            offset=offset,
        )
        return {
            "simulation_id": simulation_id,
            "runs": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/simulations/{simulation_id}/logs")
def get_simulation_logs(simulation_id: str) -> dict[str, Any]:
    try:
        postgres = get_services().postgres
        if postgres.get_simulation_session(simulation_id) is None:
            raise HTTPException(status_code=404, detail="simulation_id를 찾을 수 없습니다.")
        return {"simulation_id": simulation_id, **postgres.list_simulation_logs(simulation_id)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/warehouses/{warehouse_id}/simulation-reset-logs")
def get_warehouse_simulation_reset_logs(
    warehouse_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        rows = get_services().postgres.list_simulation_reset_audits(
            warehouse_id=warehouse_id,
            limit=limit,
            offset=offset,
        )
        return {
            "warehouse_id": warehouse_id,
            "reset_logs": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

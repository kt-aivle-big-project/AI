"""Optimization, route expansion, and traffic scheduling graph nodes."""
from __future__ import annotations

from app.core.config import get_settings
from app.core.node_observability import observe_node
from app.domain.schemas import (
    ContextSnapshot,
    CuOptPayload,
    MapContext,
    MissionSpec,
    OptimizationRequest,
    OptimizerResult,
    PolicyValidationResult,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.optimization_service import (
    CuOptPayloadBuilder,
    CuOptPayloadValidator,
    ExternalCuOptGateway,
    ORToolsRoutingOptimizer,
    OptimizerAssignmentValidator,
    build_optimization_request,
)
from app.services.route_service import StaticRouteValidator, WaypointRouteExpander
from app.services.traffic_manager import TrafficManagerService, TrafficScheduleValidator


@observe_node("optimization_request", purpose="정책 승인 Task와 로봇을 Solver 중립 OptimizationRequest로 변환")
def optimization_request_node(state: LaroGraphState) -> dict:
    """Build one solver-neutral optimization request."""

    try:
        policy = model_from_state(state, "policy_validation", PolicyValidationResult)
        mission = model_from_state(state, "effective_mission_spec", MissionSpec)
        request = build_optimization_request(policy, mission)
        return {"optimization_request": request, **trace_update("optimization_request")}
    except Exception as exc:
        return error_update(stage="optimization_request", code="optimization_request_failed", message=str(exc))


@observe_node("cuopt_payload", purpose="실제 220-node 창고 Route Graph를 cuOpt index·비용·이동시간 배열로 변환")
def cuopt_payload_node(state: LaroGraphState) -> dict:
    """Build the shared payload consumed by local and external optimizers."""

    try:
        request = model_from_state(state, "optimization_request", OptimizationRequest)
        payload = CuOptPayloadBuilder().build(
            request=request,
            graph_nodes=list(state["graph_nodes"]),
            graph_arcs=list(state["graph_arcs"]),
            time_limit_seconds=(
                get_settings().ortools_time_limit_seconds
                if state["optimization_backend"] == "ortools"
                else get_settings().cuopt_time_limit_seconds
            ),
        )
        return {"cuopt_payload": payload, **trace_update("cuopt_payload")}
    except Exception as exc:
        return error_update(stage="cuopt_payload", code="cuopt_payload_failed", message=str(exc))


@observe_node("cuopt_schema_validator", purpose="Optimizer 호출 전에 index·배열·capacity·directed reachability 검증")
def cuopt_schema_validator_node(state: LaroGraphState) -> dict:
    """Validate the indexed payload."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        validation = CuOptPayloadValidator().validate(payload)
        return {"payload_validation": validation, **trace_update("cuopt_schema_validator")}
    except Exception as exc:
        return error_update(stage="cuopt_schema_validator", code="payload_validation_failed", message=str(exc))


@observe_node("optimizer", purpose="OR-Tools 또는 외부 cuOpt로 Robot별 다중 Pickup·Delivery 배정과 순서를 계산")
def optimizer_node(state: LaroGraphState) -> dict:
    """Execute the selected real optimization backend; no fallback is used."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        backend = state["optimization_backend"]
        runtime_overrides = state.get("runtime_overrides")
        relocate_robot_ids = list(
            getattr(runtime_overrides, "relocate_idle_robot_ids", []) or []
        )
        if not payload.task_data.task_ids and relocate_robot_ids:
            # A rolling-horizon low-battery handover can legitimately finish
            # every business operation before the new horizon starts.  In that
            # case the only remaining work is the execution-only CHARGE/PARK
            # goal appended by TerminalRelocationEnricher.  External cuOpt does
            # not accept an empty order-location array, and there is no
            # assignment problem for it to solve, so publish the mathematically
            # exact empty assignment and continue through the normal independent
            # assignment, relocation, MAPF, and plan validators.
            result = OptimizerResult(
                backend=backend,
                status="success",
                optimizer="terminal-relocation-empty-assignment",
                global_objective_cost=0.0,
                estimated_makespan_ms=0.0,
                warnings=[
                    "Business-task assignment skipped because only terminal relocation remains."
                ],
            )
            return {"optimizer_result": result, **trace_update("optimizer")}
        if backend == "ortools":
            result = ORToolsRoutingOptimizer().solve(payload)
        elif backend == "cuopt":
            # Evaluation repeats may retry a transient transport failure so one
            # flaky sample does not abort an entire comparison suite.  Keep the
            # normal planning path on the gateway's production constructor and
            # retry policy; this also preserves the public gateway contract for
            # integrations that provide their own implementation.
            gateway = (
                ExternalCuOptGateway(transient_retries_enabled=True)
                if state.get("evaluation_shadow_mode", False)
                else ExternalCuOptGateway()
            )
            result = gateway.solve(payload)
        else:
            raise ValueError("cuopt_payload_only must terminate before optimizer execution")

        if result.status in {"unavailable", "failed"}:
            code = result.errors[0] if result.errors else "optimizer_service_unavailable"
            update = error_update(
                stage="optimizer",
                code=code,
                message=result.reason or "The selected optimizer did not return a usable solution.",
                retryable=result.status == "unavailable",
            )
            update["optimizer_result"] = result
            return update
        return {"optimizer_result": result, **trace_update("optimizer")}
    except Exception as exc:
        return error_update(stage="optimizer", code="optimizer_failed", message=str(exc), retryable=True)


@observe_node("optimizer_assignment_validator", purpose="Optimizer의 Task 누락·중복·고정 로봇·Pickup-before-Drop 검증")
def optimizer_assignment_validator_node(state: LaroGraphState) -> dict:
    """Validate optimizer assignment independently."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        result = model_from_state(state, "optimizer_result", OptimizerResult)
        validation = OptimizerAssignmentValidator().validate(payload=payload, result=result)
        return {"optimizer_assignment_validation": validation, **trace_update("optimizer_assignment_validator")}
    except Exception as exc:
        return error_update(stage="optimizer_assignment_validator", code="assignment_validation_failed", message=str(exc))


@observe_node("waypoint_route_expander", purpose="Task 방문 순서를 실제 directed node·edge 경로로 확장")
def waypoint_route_expander_node(state: LaroGraphState) -> dict:
    """Expand task ordering into the supplied warehouse graph."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        result = model_from_state(state, "optimizer_result", OptimizerResult)
        expansion = WaypointRouteExpander().expand(payload=payload, result=result)
        return {"waypoint_route_expansion": expansion, **trace_update("waypoint_route_expander")}
    except Exception as exc:
        return error_update(stage="waypoint_route_expander", code="route_expansion_failed", message=str(exc))


@observe_node("route_static_validator", purpose="확장 경로의 Edge 존재·연속성·차단·비용·이동시간 재검증")
def route_static_validator_node(state: LaroGraphState) -> dict:
    """Validate static route structure."""

    try:
        payload_key = "execution_payload" if state.get("execution_payload") is not None else "cuopt_payload"
        payload = model_from_state(state, payload_key, CuOptPayload)
        expansion = model_from_state(state, "waypoint_route_expansion", WaypointRouteExpansionResult)
        validation = StaticRouteValidator().validate(
            payload=payload,
            expansion=expansion,
            node_types=dict(state.get("graph_node_types") or {}),
        )
        return {"route_validation": validation, **trace_update("route_static_validator")}
    except Exception as exc:
        return error_update(stage="route_static_validator", code="route_validation_failed", message=str(exc))


@observe_node("traffic_schedule_builder", purpose="점유·예약을 반영해 실제 ms 기반 MOVE/WAIT 시간표 생성")
def traffic_schedule_builder_node(state: LaroGraphState) -> dict:
    """Create an initial traffic-safe schedule and insert waits at safe route nodes."""

    try:
        expansion = model_from_state(state, "waypoint_route_expansion", WaypointRouteExpansionResult)
        map_context = model_from_state(state, "map_context", MapContext)
        schedule = TrafficManagerService().schedule(
            expansion=expansion,
            map_context=map_context,
            node_types=dict(state["graph_node_types"]),
        )
        return {"traffic_schedule": schedule, **trace_update("traffic_schedule_builder")}
    except Exception as exc:
        return error_update(stage="traffic_schedule_builder", code="traffic_schedule_failed", message=str(exc))


@observe_node("traffic_schedule_validator", purpose="Edge 시간 중첩·안전하지 않은 WAIT·시간 순서 검증")
def traffic_schedule_validator_node(state: LaroGraphState) -> dict:
    """Validate the traffic schedule and set final plan status."""

    try:
        schedule = model_from_state(state, "traffic_schedule", TrafficScheduleResult)
        map_context = model_from_state(state, "map_context", MapContext)
        validated = TrafficScheduleValidator().validate(
            schedule=schedule,
            map_context=map_context,
            node_types=dict(state["graph_node_types"]),
        )
        return {
            "traffic_schedule": validated,
            "workflow_status": "plan_validated" if validated.valid else "human_review",
            **trace_update("traffic_schedule_validator"),
        }
    except Exception as exc:
        return error_update(stage="traffic_schedule_validator", code="traffic_schedule_validation_failed", message=str(exc))

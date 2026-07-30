"""v9 batch allocation, route resolution, solver, and MAPF graph nodes."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import (
    CandidateSpaceValidation,
    CuOptPayload,
    GoodsToPersonCompilationResult,
    MAPFValidationResult,
    MapContext,
    MissionIntent,
    MissionSpec,
    OptimizationRequest,
    OptimizerResult,
    PhysicalProblemProfile,
    PlanningRouteResolution,
    PolicyValidationResult,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.inventory_allocation_service import GlobalInventoryAllocator
from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.optimization_service import CandidateSpaceGuard, OneToOneRuleOptimizer
from app.services.physical_problem_service import PhysicalProblemProfiler, PlanningRouteResolver


@observe_node(
    "global_inventory_allocator",
    purpose="배치 전체 재고 수량을 보존하며 주문별 Pickup Rack을 확정하고 Robot 선택은 Solver에 남김",
)
def global_inventory_allocator_node(state: LaroGraphState) -> dict:
    """Allocate stock globally and create canonical solver tasks."""

    try:
        mission = model_from_state(state, "effective_mission_spec", MissionSpec)
        policy = model_from_state(state, "policy_validation", PolicyValidationResult)
        updated = GlobalInventoryAllocator().allocate(
            mission=mission,
            policy=policy,
            graph_arcs=list(state["graph_arcs"]),
        )
        return {"policy_validation": updated, **trace_update("global_inventory_allocator")}
    except Exception as exc:
        return error_update(
            stage="global_inventory_allocator",
            code="global_inventory_allocation_failed",
            message=str(exc),
        )


@observe_node(
    "candidate_space_guard",
    purpose="권위 OptimizationRequest와 Solver Payload의 Task·Vehicle 후보 공간이 동일한지 검증",
)
def candidate_space_guard_node(state: LaroGraphState) -> dict:
    """Reject silent task or vehicle pruning before the solver."""

    try:
        request = model_from_state(state, "optimization_request", OptimizationRequest)
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        result = CandidateSpaceGuard().validate(request=request, payload=payload)
        return {"candidate_space_validation": result, **trace_update("candidate_space_guard")}
    except Exception as exc:
        return error_update(
            stage="candidate_space_guard",
            code="candidate_space_validation_failed",
            message=str(exc),
        )


@observe_node(
    "physical_problem_profiler",
    purpose="1:1 기준선의 유예·WAIT를 측정해 전역 Multi-task Solver 강제 조건을 계산",
)
def physical_problem_profiler_node(state: LaroGraphState) -> dict:
    """Build the deterministic problem profile and baseline artifacts."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        map_context = model_from_state(state, "map_context", MapContext)
        profile, baseline_result, baseline_expansion, baseline_schedule = PhysicalProblemProfiler().profile(
            payload=payload,
            map_context=map_context,
            node_types=dict(state["graph_node_types"]),
        )
        return {
            "physical_problem_profile": profile,
            "baseline_optimizer_result": baseline_result,
            "baseline_waypoint_route_expansion": baseline_expansion,
            "baseline_traffic_schedule": baseline_schedule,
            **trace_update("physical_problem_profiler"),
        }
    except Exception as exc:
        return error_update(
            stage="physical_problem_profiler",
            code="physical_problem_profile_failed",
            message=str(exc),
        )


@observe_node(
    "planning_route_resolver",
    purpose="LLM의 RULE/GLOBAL_SOLVER 추천을 물리적 복잡성 Guard와 Backend 선택으로 최종 확정",
)
def planning_route_resolver_node(state: LaroGraphState) -> dict:
    """Resolve the planning route recommendation."""

    try:
        profile = model_from_state(state, "physical_problem_profile", PhysicalProblemProfile)
        intent = state.get("mission_intent")
        if intent is not None and not isinstance(intent, MissionIntent):
            intent = MissionIntent.model_validate(intent)
        result = PlanningRouteResolver().resolve(
            profile=profile,
            intent=intent,
            optimization_backend=state["optimization_backend"],
        )
        return {"planning_route_resolution": result, **trace_update("planning_route_resolver")}
    except Exception as exc:
        return error_update(
            stage="planning_route_resolver",
            code="planning_route_resolution_failed",
            message=str(exc),
        )


@observe_node(
    "one_to_one_rule_optimizer",
    purpose="가벼운 Batch에 Robot 1대당 신규 Task 1개를 배정하는 결정론적 기준선 실행",
)
def one_to_one_rule_optimizer_node(state: LaroGraphState) -> dict:
    """Run the one-to-one light-path baseline selected by the resolver."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        result = OneToOneRuleOptimizer().solve(payload, allow_partial=False)
        return {"optimizer_result": result, **trace_update("one_to_one_rule_optimizer")}
    except Exception as exc:
        return error_update(
            stage="one_to_one_rule_optimizer",
            code="one_to_one_rule_optimizer_failed",
            message=str(exc),
        )


@observe_node(
    "prioritized_mapf_planner",
    purpose="Solver의 Robot별 다중 Task 순서를 공유 Safe-Interval Calendar 위 충돌 없는 MOVE·WAIT·SERVICE 경로로 변환",
)
def prioritized_mapf_planner_node(state: LaroGraphState) -> dict:
    """Build the multi-goal prioritized MAPF plan."""

    try:
        payload_key = "execution_payload" if state.get("execution_payload") is not None else "cuopt_payload"
        result_key = (
            "execution_optimizer_result"
            if state.get("execution_optimizer_result") is not None
            else "optimizer_result"
        )
        payload = model_from_state(state, payload_key, CuOptPayload)
        result = model_from_state(state, result_key, OptimizerResult)
        map_context = model_from_state(state, "map_context", MapContext)
        compilation_value = state.get("goods_to_person_compilation")
        batches = []
        if compilation_value is not None:
            compilation = (
                compilation_value
                if isinstance(compilation_value, GoodsToPersonCompilationResult)
                else GoodsToPersonCompilationResult.model_validate(compilation_value)
            )
            batches = list(compilation.batches)
        runtime_overrides = state.get("runtime_overrides")
        expansion, schedule = PrioritizedSIPPPlanner().plan(
            payload=payload,
            result=result,
            map_context=map_context,
            node_types=dict(state["graph_node_types"]),
            g2p_batches=batches,
            preserved_node_reservations=list(
                getattr(runtime_overrides, "preserved_node_reservations", []) or []
            ),
            preserved_station_reservations=list(
                getattr(runtime_overrides, "preserved_station_reservations", []) or []
            ),
        )
        return {
            "waypoint_route_expansion": expansion,
            "traffic_schedule": schedule,
            **trace_update("prioritized_mapf_planner"),
        }
    except Exception as exc:
        return error_update(
            stage="prioritized_mapf_planner",
            code="mapf_planning_failed",
            message=str(exc),
        )


@observe_node(
    "mapf_plan_validator",
    purpose="MAPF 시간 순서·통로 Headway·Node WAIT/SERVICE 충돌·최대 대기 정책을 독립 검증",
)
def mapf_plan_validator_node(state: LaroGraphState) -> dict:
    """Validate the timed MAPF plan and set the terminal planning status."""

    try:
        schedule = model_from_state(state, "traffic_schedule", TrafficScheduleResult)
        map_context = model_from_state(state, "map_context", MapContext)
        request = model_from_state(state, "optimization_request", OptimizationRequest)
        payload_key = "execution_payload" if state.get("execution_payload") is not None else "cuopt_payload"
        payload = model_from_state(state, payload_key, CuOptPayload)
        validation = MAPFPlanValidator().validate(
            schedule=schedule,
            map_context=map_context,
            node_types=dict(state["graph_node_types"]),
            max_edge_wait_ms=request.max_edge_wait_ms,
            payload=payload,
            preserved_node_reservations=list(
                getattr(state.get("runtime_overrides"), "preserved_node_reservations", [])
                or []
            ),
        )
        validated_schedule = schedule.model_copy(
            update={
                "valid": validation.valid,
                "conflicts": [*schedule.conflicts, *validation.errors],
                "warnings": [*schedule.warnings, *validation.warnings],
            }
        )
        return {
            "mapf_validation": validation,
            "traffic_schedule": validated_schedule,
            "workflow_status": "plan_validated" if validation.valid else "human_review",
            **trace_update("mapf_plan_validator"),
        }
    except Exception as exc:
        return error_update(
            stage="mapf_plan_validator",
            code="mapf_validation_failed",
            message=str(exc),
        )

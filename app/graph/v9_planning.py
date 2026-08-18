"""v9 batch allocation, route resolution, solver, and MAPF graph nodes."""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.console import safe_console_print
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
    WorkflowError,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.inventory_allocation_service import GlobalInventoryAllocator
from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.optimization_service import CandidateSpaceGuard, OneToOneRuleOptimizer
from app.services.physical_problem_service import PhysicalProblemProfiler, PlanningRouteResolver


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read one field from a Pydantic model or its serialized dictionary."""

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapf_error_summary(errors: list[str]) -> str:
    """Translate the first deterministic MAPF error into an operator-facing reason."""

    if not errors:
        return "충전소 복귀 경로가 MAPF 안전 검증을 통과하지 못했습니다."
    first = errors[0]
    translations = (
        ("violates headway", "다른 로봇 또는 기존 예약과의 안전 간격이 충돌했습니다."),
        ("non-monotonic MAPF steps", "경로 단계의 시간 순서가 올바르지 않습니다."),
        ("waits at unsafe node", "안전 대기 노드가 아닌 위치에 대기 단계가 생성되었습니다."),
        ("exceeds max_edge_wait_ms", "경로의 대기 시간이 허용 한도를 초과했습니다."),
        ("SERVICE step without task_id", "작업 식별자가 없는 실행 단계가 생성되었습니다."),
        ("services unknown task", "현재 계획에 없는 작업 단계가 경로에 포함되었습니다."),
        ("service duration", "작업 처리 시간이 원본 계획과 일치하지 않습니다."),
        ("Mandatory handling steps are missing", "필수 작업 단계가 경로에서 누락되었습니다."),
        ("Handling steps must occur exactly once", "같은 작업 단계가 중복되었거나 누락되었습니다."),
        ("capacity=1 overlap", "충전소 또는 작업 스테이션 사용 시간이 다른 로봇과 겹쳤습니다."),
        ("total_service_ms does not match", "전체 작업 시간과 세부 실행 단계의 합계가 일치하지 않습니다."),
        ("No safe ordered-goal path", "기존 작업 경로와 예약을 모두 만족하는 충돌 없는 경로를 찾지 못했습니다."),
    )
    return next((message for marker, message in translations if marker in first), first)


def _mapf_failed_robot_ids(
    errors: list[str], known_robot_ids: set[str]
) -> list[str]:
    """Return robot IDs explicitly referenced by deterministic MAPF errors."""

    referenced = {
        robot_id
        for robot_id in known_robot_ids
        if any(
            re.search(
                rf"(?<![\w-]){re.escape(robot_id)}(?![\w-])",
                error,
            )
            for error in errors
        )
    }
    if referenced:
        return sorted(referenced)

    for error in errors:
        match = re.search(r"No safe ordered-goal path for ([^:\s]+):", error)
        if match:
            referenced.add(match.group(1))
    return sorted(referenced)


def _mapf_failure_diagnostics(
    state: LaroGraphState,
    schedule: TrafficScheduleResult,
    validation: MAPFValidationResult,
) -> dict[str, Any]:
    """Build one compact failure record suitable for CloudWatch inspection."""

    runtime = state.get("runtime_overrides")
    robot_states = list(_field(runtime, "robot_states", []) or [])
    low_battery_states = [
        value
        for value in robot_states
        if str(_field(value, "status", "")).casefold() == "low_battery"
    ]
    low_battery_ids = {
        str(_field(value, "robot_id", ""))
        for value in low_battery_states
        if _field(value, "robot_id")
    }
    known_robot_ids = {
        str(_field(value, "robot_id", ""))
        for value in robot_states
        if _field(value, "robot_id")
    }
    known_robot_ids.update(route.robot_id for route in schedule.routes)

    relocation = state.get("terminal_relocation")
    relocation_records = [
        {
            "robot_id": str(_field(value, "robot_id", "")),
            "policy": str(_field(value, "policy", "")),
            "from_node": _field(value, "from_node"),
            "to_node": _field(value, "to_node"),
            "task_id": _field(value, "task_id"),
            "reason": _field(value, "reason"),
        }
        for value in (_field(relocation, "relocations", []) or [])
        if not low_battery_ids
        or str(_field(value, "robot_id", "")) in low_battery_ids
    ]
    route_records = []
    for route in schedule.routes:
        if low_battery_ids and route.robot_id not in low_battery_ids:
            continue
        charge_steps = [
            step
            for step in route.steps
            if step.step_type == "SERVICE" and step.service_kind == "CHARGE"
        ]
        route_records.append(
            {
                "robot_id": route.robot_id,
                "step_count": len(route.steps),
                "finish_at_ms": route.finish_at_ms,
                "last_node": next(
                    (
                        step.node_id or step.to_node
                        for step in reversed(route.steps)
                        if step.node_id or step.to_node
                    ),
                    None,
                ),
                "charge_task_ids": [step.task_id for step in charge_steps],
            }
        )

    return {
        "event": "mapf_validation_failed",
        "simulation_id": state.get("simulation_id"),
        "workflow_status": "human_review",
        "operator_summary": _mapf_error_summary(list(validation.errors)),
        "errors": list(validation.errors),
        "warnings": list(validation.warnings),
        "failed_robot_ids": _mapf_failed_robot_ids(
            list(validation.errors), known_robot_ids
        ),
        "low_battery_robots": [
            {
                "robot_id": _field(value, "robot_id"),
                "battery_pct": _field(value, "battery_pct"),
                "current_node": _field(value, "current_node"),
                "current_edge": _field(value, "current_edge"),
                "active_task_id": _field(value, "active_task_id"),
                "current_load_units": _field(value, "current_load_units"),
                "sim_time_ms": _field(value, "sim_time_ms"),
            }
            for value in low_battery_states
        ],
        "terminal_relocations": relocation_records,
        "mapf_routes": route_records,
        "schedule": {
            "route_count": len(schedule.routes),
            "reservation_count": len(schedule.reservations),
            "station_reservation_count": len(schedule.station_reservations),
            "total_wait_ms": schedule.total_wait_ms,
            "total_service_ms": schedule.total_service_ms,
            "makespan_ms": schedule.makespan_ms,
        },
    }


def _mapf_operator_message(diagnostics: dict[str, Any]) -> str:
    """Describe the robots that actually failed MAPF in one UI-friendly sentence."""

    robots = diagnostics["low_battery_robots"]
    relocations = diagnostics["terminal_relocations"]
    low_battery_ids = {
        str(value["robot_id"]) for value in robots if value["robot_id"]
    }
    failed_robot_ids = [
        str(value) for value in diagnostics.get("failed_robot_ids", []) if value
    ]
    failed_low_battery_ids = [
        robot_id for robot_id in failed_robot_ids if robot_id in low_battery_ids
    ]
    if failed_robot_ids and not failed_low_battery_ids:
        return (
            f"로봇 {', '.join(failed_robot_ids)}의 충돌 없는 실행 경로를 "
            f"생성하지 못했습니다. {diagnostics['operator_summary']}"
        )

    robot_ids = ", ".join(failed_low_battery_ids or sorted(low_battery_ids))
    charge_targets = ", ".join(
        str(value["to_node"])
        for value in relocations
        if value["policy"] == "CHARGE"
        and value["to_node"]
        and (
            not failed_low_battery_ids
            or str(value["robot_id"]) in failed_low_battery_ids
        )
    )
    if not robot_ids:
        return (
            "생성된 로봇 경로를 실행하지 못했습니다. "
            f"{diagnostics['operator_summary']}"
        )
    target = (
        f"에서 충전소 {charge_targets}까지의 복귀 경로"
        if charge_targets
        else "의 복귀 경로"
    )
    return (
        f"배터리 부족 로봇 {robot_ids}{target}를 실행하지 못했습니다. "
        f"{diagnostics['operator_summary']}"
    )


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
        update = {
            "waypoint_route_expansion": expansion,
            "traffic_schedule": schedule,
            **trace_update("prioritized_mapf_planner"),
        }
        if expansion.status != "expanded" or not schedule.valid:
            planner_errors = list(
                dict.fromkeys([*expansion.errors, *schedule.conflicts])
            )
            validation = MAPFValidationResult(
                valid=False,
                errors=planner_errors
                or ["Prioritized MAPF could not build an executable route."],
                warnings=list(schedule.warnings),
            )
            failure_context = _mapf_failure_diagnostics(
                state, schedule, validation
            )
            operator_message = _mapf_operator_message(failure_context)
            safe_console_print(
                "[prioritized_mapf_planner 진단] "
                + json.dumps(failure_context, ensure_ascii=False, default=str)
            )
            update["traffic_schedule"] = schedule.model_copy(
                update={
                    "valid": False,
                    "conflicts": list(
                        dict.fromkeys(
                            [operator_message, *schedule.conflicts, *expansion.errors]
                        )
                    ),
                }
            )
            # The graph still routes to human_review so the operator can see
            # the concrete conflict.  Exposing the same reason as a workflow
            # error also prevents a compact BE response with plan=null from
            # being mistaken for a completed command cycle.
            update["errors"] = [
                WorkflowError(
                    stage="prioritized_mapf_planner",
                    code="mapf_route_expansion_failed",
                    message=operator_message,
                    retryable=True,
                )
            ]
        return update
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
        operator_message = None
        if not validation.valid:
            failure_context = _mapf_failure_diagnostics(state, schedule, validation)
            operator_message = _mapf_operator_message(failure_context)
            safe_console_print(
                "[mapf_plan_validator 진단] "
                + json.dumps(failure_context, ensure_ascii=False, default=str)
            )
        validated_schedule = schedule.model_copy(
            update={
                "valid": validation.valid,
                "conflicts": [
                    *schedule.conflicts,
                    *([operator_message] if operator_message else []),
                    *validation.errors,
                ],
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

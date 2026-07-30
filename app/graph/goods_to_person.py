"""Goods-to-person compiler and post-solver route enrichment nodes.

The main orchestration graph owns payload serialization, optimization, MAPF,
validation, and terminal persistence.  This module contributes only two G2P
specific transformations:

1. compile canonical outbound order tasks into physical handling-unit cycles;
2. after solver assignment, append the same-AMR return/empty-tote goal.

No solver or traffic planner is called from this module.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.node_observability import observe_node
from app.domain.schemas import (
    CuOptPayload,
    GoodsToPersonCompilationResult,
    GoodsToPersonOptions,
    GoodsToPersonRouteEnrichmentResult,
    InputRejectionResult,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    OptimizerResult,
    OptimizerRoute,
    TaskData,
)
from app.graph.node_support import error_update, model_from_state, require_locked_route, trace_update
from app.graph.state import LaroGraphState
from app.services.goods_to_person_compiler_service import IntegratedGoodsToPersonCompiler
from app.services.goods_to_person_service import GoodsToPersonPlanningError


def _locked_mission_route(state: LaroGraphState) -> str:
    plan = state.get("orchestration_plan")
    route = getattr(plan, "route", None) if plan is not None else None
    if isinstance(plan, dict):
        route = plan.get("route")
    if route not in {"RULE_MISSION_PIPELINE", "AGENT_MISSION_PIPELINE"}:
        raise ValueError("G2P compilation requires a locked Rule or Agent mission route.")
    require_locked_route(state, expected_route=str(route))
    return str(route)


@observe_node(
    "goods_to_person_compiler",
    purpose=(
        "검증된 출고 Order Task를 Handling Unit Cycle로 변환하고 Solver 실행은 공통 "
        "cuOpt/OR-Tools·MAPF 노드에 남김"
    ),
)
def goods_to_person_compiler_node(state: LaroGraphState) -> dict:
    """Compile outbound work, or pass the canonical request through unchanged."""

    try:
        _locked_mission_route(state)
        request = model_from_state(state, "optimization_request", OptimizationRequest)
        normalized_value = state.get("normalized_request")
        normalized = None
        if normalized_value is not None:
            normalized = (
                normalized_value
                if isinstance(normalized_value, NormalizedWarehouseRequest)
                else NormalizedWarehouseRequest.model_validate(normalized_value)
            )

        if get_settings().outbound_fulfillment_mode != "goods_to_person":
            compilation = GoodsToPersonCompilationResult(
                applied=False,
                original_task_ids=[value.task_id for value in request.tasks],
                compiled_task_ids=[value.task_id for value in request.tasks],
                preserved_task_ids=[value.task_id for value in request.tasks],
                optimization_request=request,
                summary="Legacy order-task fulfillment mode left the optimization request unchanged.",
            )
            return {
                "goods_to_person_compilation": compilation,
                "optimization_request": request,
                **trace_update("goods_to_person_compiler"),
            }

        options_value = state.get("goods_to_person_options")
        options = (
            options_value
            if isinstance(options_value, GoodsToPersonOptions)
            else GoodsToPersonOptions.model_validate(options_value or {})
        )
        compilation = IntegratedGoodsToPersonCompiler().compile(
            simulation_id=str(state["simulation_id"]),
            normalized_request=normalized,
            optimization_request=request,
            graph_arcs=list(state["graph_arcs"]),
            options=options,
        )
        if compilation.errors or compilation.optimization_request is None:
            message = compilation.errors[0] if compilation.errors else "G2P compilation produced no request."
            return {
                "goods_to_person_compilation": compilation,
                "input_rejection": InputRejectionResult(
                    reason_code="G2P_COMPILATION_REJECTED",
                    message=message,
                    invalid_references=list(compilation.source_order_ids),
                ),
                **trace_update("goods_to_person_compiler"),
            }
        return {
            "goods_to_person_compilation": compilation,
            "optimization_request": compilation.optimization_request,
            **trace_update("goods_to_person_compiler"),
        }
    except GoodsToPersonPlanningError as exc:
        return {
            "input_rejection": InputRejectionResult(
                reason_code="G2P_COMPILATION_REJECTED",
                message=str(exc),
                invalid_references=[],
            ),
            **trace_update("goods_to_person_compiler"),
        }
    except Exception as exc:
        return error_update(
            stage="goods_to_person_compiler",
            code="goods_to_person_compilation_failed",
            message=str(exc),
        )


@observe_node(
    "goods_to_person_execution_enricher",
    purpose=(
        "Solver가 배정한 Handling Unit Cycle 뒤에 같은 AMR의 원선반 반환 또는 빈 Tote "
        "이동 Goal을 실행 전용 Payload·Route에 추가"
    ),
)
def goods_to_person_execution_enricher_node(state: LaroGraphState) -> dict:
    """Append deterministic post-station goals without modifying solver evidence."""

    try:
        payload = model_from_state(state, "cuopt_payload", CuOptPayload)
        result = model_from_state(state, "optimizer_result", OptimizerResult)
        compilation_value = state.get("goods_to_person_compilation")
        if compilation_value is None:
            enrichment = GoodsToPersonRouteEnrichmentResult(applied=False)
            return {
                "execution_payload": payload,
                "execution_optimizer_result": result,
                "goods_to_person_route_enrichment": enrichment,
                **trace_update("goods_to_person_execution_enricher"),
            }
        compilation = (
            compilation_value
            if isinstance(compilation_value, GoodsToPersonCompilationResult)
            else GoodsToPersonCompilationResult.model_validate(compilation_value)
        )
        if not compilation.applied or not compilation.batches:
            enrichment = GoodsToPersonRouteEnrichmentResult(applied=False)
            return {
                "execution_payload": payload,
                "execution_optimizer_result": result,
                "goods_to_person_route_enrichment": enrichment,
                **trace_update("goods_to_person_execution_enricher"),
            }

        settings = get_settings()
        drop_to_batch = {f"{batch.batch_id}_DROP": batch for batch in compilation.batches}
        batch_to_robot: dict[str, str] = {}
        appended_task_ids: list[str] = []
        new_routes: list[OptimizerRoute] = []

        for route in result.routes:
            sequence: list[str] = []
            for task_id in route.task_sequence:
                sequence.append(task_id)
                batch = drop_to_batch.get(task_id)
                if batch is None:
                    continue
                post_id = (
                    f"{batch.batch_id}_RETURN"
                    if batch.return_required
                    else f"{batch.batch_id}_EMPTY_TOTE"
                )
                sequence.append(post_id)
                appended_task_ids.append(post_id)
                batch_to_robot[batch.batch_id] = route.vehicle_id
            new_routes.append(route.model_copy(update={"task_sequence": sequence}))

        missing = sorted(
            batch.batch_id
            for batch in compilation.batches
            if batch.batch_id not in batch_to_robot
        )
        if missing:
            enrichment = GoodsToPersonRouteEnrichmentResult(
                applied=True,
                valid=False,
                batch_robot_assignments=batch_to_robot,
                errors=[f"No assigned robot was found for G2P batches: {missing}"],
            )
            return {
                "goods_to_person_route_enrichment": enrichment,
                **error_update(
                    stage="goods_to_person_execution_enricher",
                    code="g2p_post_station_assignment_missing",
                    message=enrichment.errors[0],
                ),
            }

        task_data = payload.task_data
        task_ids = list(task_data.task_ids)
        task_locations = list(task_data.task_locations)
        demand = list(task_data.demand)
        priorities = list(task_data.priorities)
        service_times = list(task_data.service_times_ms)
        fixed_vehicle_ids = list(task_data.fixed_vehicle_ids)
        optional_task_ids = list(task_data.optional_task_ids)

        for batch in compilation.batches:
            post_id = (
                f"{batch.batch_id}_RETURN"
                if batch.return_required
                else f"{batch.batch_id}_EMPTY_TOTE"
            )
            if batch.post_station_node not in payload.location_index_map:
                raise ValueError(
                    f"Post-station node {batch.post_station_node} is absent from the route graph."
                )
            task_ids.append(post_id)
            task_locations.append(payload.location_index_map[batch.post_station_node])
            demand.append(0)
            priorities.append(0)
            service_times.append(
                settings.handling_unit_return_service_ms
                if batch.return_required
                else settings.empty_tote_buffer_service_ms
            )
            fixed_vehicle_ids.append(batch_to_robot[batch.batch_id])

        assigned_batches = [
            batch.model_copy(update={"mobile_robot_id": batch_to_robot[batch.batch_id]})
            for batch in compilation.batches
        ]
        assigned_compilation = compilation.model_copy(update={"batches": assigned_batches})
        execution_payload = payload.model_copy(
            update={
                "task_data": TaskData(
                    task_ids=task_ids,
                    task_locations=task_locations,
                    pickup_and_delivery_pairs=list(task_data.pickup_and_delivery_pairs),
                    demand=demand,
                    priorities=priorities,
                    service_times_ms=service_times,
                    fixed_vehicle_ids=fixed_vehicle_ids,
                    optional_task_ids=optional_task_ids,
                )
            }
        )
        execution_result = result.model_copy(update={"routes": new_routes})
        enrichment = GoodsToPersonRouteEnrichmentResult(
            applied=True,
            valid=True,
            appended_task_ids=appended_task_ids,
            batch_robot_assignments=batch_to_robot,
            warnings=[
                "Post-station goals are execution-only and do not rewrite the original solver result."
            ],
        )
        return {
            "execution_payload": execution_payload,
            "execution_optimizer_result": execution_result,
            "goods_to_person_compilation": assigned_compilation,
            "goods_to_person_route_enrichment": enrichment,
            **trace_update("goods_to_person_execution_enricher"),
        }
    except Exception as exc:
        return error_update(
            stage="goods_to_person_execution_enricher",
            code="g2p_route_enrichment_failed",
            message=str(exc),
        )

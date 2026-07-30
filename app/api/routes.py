"""FastAPI routes for orchestration, HITL, live infrastructure, and G2P waves."""
from __future__ import annotations

import json

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import get_settings
from app.domain.planning_evaluation import PlanningComparisonRequest
from app.domain.schemas import (
    AutoMissionRequest,
    PublicMissionRequest,
    PublicReplanMissionRequest,
    GoodsToPersonBatchReservationRequest,
    EventInput,
    GoodsToPersonOptions,
    GoodsToPersonPlanRequest,
    GoodsToPersonPostMoveCommitRequest,
    GoodsToPersonStationCommitRequest,
    HumanInteractionRecord,
    HumanInteractionResumeRequest,
    HumanInteractionResumeResult,
    OrchestrationResult,
    SimulationPlanResponse,
    ReplanMissionRequest,
    RobotTelemetryUpdateRequest,
    RuntimeCommandPublishRequest,
    ScenarioRuntimeBootstrapRequest,
    ScenarioRuntimeBootstrapResult,
    normalize_warehouse_id,
)
from app.infrastructure.manager import InfrastructureStartupError, get_infrastructure_manager
from app.repositories.json_repository import get_repository
from app.services.hitl_service import HumanInteractionService
from app.services.orchestration_service import OrchestrationService
from app.services.simulation_plan_service import RollingHorizonReplanService, SimulationPlanStore
from app.services.planning_evaluation_service import (
    PlanningComparisonService,
    PlanningEvaluationCaptureService,
    PlanningEvaluationStore,
)
from app.services.scenario_debug_service import (
    ScenarioDebugDisabledError,
    ScenarioRuntimeBootstrapService,
)
from app.services.native_plan_diagnostics_service import NativePlanDiagnosticsService

router = APIRouter()


def _router_llm_executed(result: OrchestrationResult) -> bool:
    return any(
        value.node_name == "request_router_llm" and value.llm_used
        for value in result.node_execution_log
    )


def _public_to_internal(request: PublicMissionRequest) -> AutoMissionRequest:
    """Infer request mode and enforce that live runtime facts come from telemetry."""

    settings = get_settings()
    if (
        request.runtime_snapshot is not None
        and request.runtime_snapshot.robot_states
        and not settings.allow_api_runtime_snapshot
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "runtime_snapshot is disabled. Send robot battery/location through "
                "the warehouse-scoped telemetry API instead."
            ),
        )
    return request.to_internal()


def _assert_warehouse_match(path_warehouse_id: str, body_warehouse_id: str) -> str:
    path_value = normalize_warehouse_id(path_warehouse_id)
    if path_value != normalize_warehouse_id(body_warehouse_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Path warehouse_id and body warehouse_id must match.",
        )
    return path_value


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    """Return non-secret application and startup diagnostics."""

    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": "13.24.0",
        "environment": settings.app_env,
        "default_warehouse_id": settings.default_warehouse_id,
        "warehouse_repository_backend": settings.warehouse_repository_backend,
        "map_repository_backend": settings.map_repository_backend,
        "optimization_backend": settings.optimization_backend,
        "default_planning_mode": settings.default_planning_mode,
        "runtime_simulation_id": settings.runtime_simulation_id,
        "openai_configured": bool(settings.openai_api_key),
        "force_agent_structured_input_router_llm": settings.force_agent_structured_input_router_llm,
        "allow_request_planning_mode_override": settings.allow_request_planning_mode_override,
        "agent_retrieval_mode": settings.agent_retrieval_mode,
        "outbound_fulfillment_mode": settings.outbound_fulfillment_mode,
        "hitl_execution_mode": settings.hitl_execution_mode,
        "hitl_store_dir": str(settings.hitl_store_dir or (settings.output_dir / "hitl")),
        "langsmith_tracing": settings.langsmith_enabled,
        "planning_evaluation_mode": settings.planning_evaluation_mode,
        "debug_scenario_api_enabled": settings.debug_scenario_api_enabled,
        "be_compat_enabled": settings.be_compat_enabled,
        "be_compat_graph_source": settings.be_compat_graph_source,
        "be_compat_graph_cache_mode": settings.be_compat_graph_cache_mode,
        "be_compat_runtime_source": settings.be_compat_runtime_source,
        "native_plan_endpoints": [
            "GET /api/v1/warehouses/{warehouse_id}/missions/plan/preflight",
            "POST /api/v1/warehouses/{warehouse_id}/missions/plan",
            "GET /api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/trace",
            "GET /api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/debug",
        ],
        "be_compat_endpoints": [
            "POST /optimize",
            "POST /reoptimize",
            "GET /compat/v2/contract",
            "GET /compat/v2/simulation-runs/{runId}/runtime",
        ],
        "infrastructure_startup": getattr(request.app.state, "infrastructure_startup", None),
    }


@router.get("/api/v1/warehouses")
def list_warehouses() -> dict[str, object]:
    """List public warehouse IDs without exposing database connection details."""

    settings = get_settings()
    if settings.warehouse_repository_backend == "json":
        ids = {normalize_warehouse_id(settings.default_warehouse_id)}
        if settings.warehouse_data_root.exists():
            ids.update(
                normalize_warehouse_id(value.name)
                for value in settings.warehouse_data_root.iterdir()
                if value.is_dir()
            )
        records = [
            {
                "warehouse_id": value,
                "label": value,
                "active": True,
                "source": "json",
            }
            for value in sorted(ids)
        ]
    else:
        try:
            records = get_infrastructure_manager().postgres.list_warehouses()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        for value in records:
            value["source"] = settings.warehouse_repository_backend
    return {
        "default_warehouse_id": normalize_warehouse_id(
            settings.default_warehouse_id
        ),
        "warehouses": records,
    }


@router.get("/infrastructure/health")
def infrastructure_health() -> dict[str, object]:
    """Ping PostgreSQL, Redis, and Neo4j concurrently in live mode."""

    return get_infrastructure_manager().health()


@router.post("/infrastructure/roundtrip")
def infrastructure_roundtrip(warehouse_id: str | None = None) -> dict[str, object]:
    """Write/read/delete one disposable record in all three live servers."""

    try:
        return get_infrastructure_manager().roundtrip(warehouse_id=warehouse_id)
    except InfrastructureStartupError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/infrastructure/bootstrap")
def bootstrap_infrastructure(
    warehouse_id: str | None = None, replace: bool = True
) -> dict[str, object]:
    """Load the configured JSON seed into PostgreSQL, Redis, and Neo4j."""

    settings = get_settings()
    try:
        resolved_warehouse = warehouse_id or settings.default_warehouse_id
        result = get_infrastructure_manager().bootstrap_from_json(
            None,
            warehouse_id=resolved_warehouse,
            replace=replace,
        )
        # A cached repository may predate the seed.  Clear it so the next mission
        # sees the freshly loaded live state.
        get_repository.cache_clear()
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@router.post(
    "/api/v1/debug/scenarios/bootstrap-runtime",
    response_model=ScenarioRuntimeBootstrapResult,
)
def bootstrap_debug_scenario_runtime(
    request: ScenarioRuntimeBootstrapRequest,
) -> ScenarioRuntimeBootstrapResult:
    """Clone the baseline Redis runtime into an isolated scenario namespace."""

    try:
        return ScenarioRuntimeBootstrapService().bootstrap(request)
    except ScenarioDebugDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@router.post("/api/v1/missions/orchestrate", response_model=OrchestrationResult)
def orchestrate(request: PublicMissionRequest) -> OrchestrationResult:
    """Run the public mission API; request_mode is inferred server-side."""

    return OrchestrationService().run(_public_to_internal(request))


@router.post(
    "/api/v1/warehouses/{warehouse_id}/missions/orchestrate",
    response_model=OrchestrationResult,
)
def orchestrate_for_warehouse(
    warehouse_id: str, request: PublicMissionRequest
) -> OrchestrationResult:
    _assert_warehouse_match(warehouse_id, request.warehouse_id)
    return OrchestrationService().run(_public_to_internal(request))


@router.post("/missions/orchestrate", response_model=OrchestrationResult, include_in_schema=False)
def orchestrate_legacy(request: AutoMissionRequest) -> OrchestrationResult:
    """Backward-compatible internal request route used by older scripts."""

    return OrchestrationService().run(request)


@router.get("/api/v1/warehouses/{warehouse_id}/missions/plan/preflight")
def native_plan_preflight(
    warehouse_id: str,
    simulation_id: str,
) -> dict[str, object]:
    """Verify native PostgreSQL, Redis, and Neo4j plan dependencies read-only."""

    try:
        return NativePlanDiagnosticsService().preflight(warehouse_id, simulation_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/api/v1/missions/plan", response_model=SimulationPlanResponse)
def create_simulation_plan(request: PublicMissionRequest) -> SimulationPlanResponse:
    """Return a compact front-end plan with server-inferred request_mode."""

    internal = _public_to_internal(request)
    result = OrchestrationService().run(internal)
    evaluation = PlanningEvaluationCaptureService().capture(
        raw_request=request,
        internal_request=internal,
        result=result,
        request_kind="PLAN",
        plan=result.simulation_plan,
    )
    return SimulationPlanResponse(
        status=result.status,
        warehouse_id=result.warehouse_id,
        simulation_id=result.simulation_id,
        request_mode=result.request_mode,
        final_route=(
            result.orchestration_plan.formulation_route
            if result.orchestration_plan
            else None
        ),
        effective_planning_mode=result.effective_planning_mode,
        planning_mode_source=result.planning_mode_source,
        router_llm_executed=_router_llm_executed(result),
        plan=result.simulation_plan,
        evaluation_id=evaluation.evaluation_id if evaluation else None,
        frontend_summary=result.frontend_summary,
        pending_human_interaction=result.pending_human_interaction,
        input_rejection=result.input_rejection,
        workflow_hold=result.workflow_hold,
        errors=result.errors,
    )


@router.post(
    "/api/v1/warehouses/{warehouse_id}/missions/plan",
    response_model=SimulationPlanResponse,
)
def create_simulation_plan_for_warehouse(
    warehouse_id: str, request: PublicMissionRequest
) -> SimulationPlanResponse:
    _assert_warehouse_match(warehouse_id, request.warehouse_id)
    return create_simulation_plan(request)


@router.post("/missions/plan", response_model=SimulationPlanResponse, include_in_schema=False)
def create_simulation_plan_legacy(request: AutoMissionRequest) -> SimulationPlanResponse:
    result = OrchestrationService().run(request)
    return SimulationPlanResponse(
        status=result.status,
        warehouse_id=result.warehouse_id,
        simulation_id=result.simulation_id,
        request_mode=result.request_mode,
        final_route=(result.orchestration_plan.formulation_route if result.orchestration_plan else None),
        effective_planning_mode=result.effective_planning_mode,
        planning_mode_source=result.planning_mode_source,
        router_llm_executed=_router_llm_executed(result),
        plan=result.simulation_plan,
        frontend_summary=result.frontend_summary,
        pending_human_interaction=result.pending_human_interaction,
        input_rejection=result.input_rejection,
        workflow_hold=result.workflow_hold,
        errors=result.errors,
    )


@router.get("/api/v1/missions/plans/{plan_id}")
@router.get("/missions/plans/{plan_id}", include_in_schema=False)
def get_simulation_plan(plan_id: str) -> dict[str, object]:
    """Read one persisted plan and its optional full debug result."""

    try:
        plan, result = SimulationPlanStore().load(plan_id)
        return {
            "plan": plan.model_dump(mode="json"),
            "orchestration_status": result.status if result else None,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/trace"
)
def get_native_plan_trace(
    warehouse_id: str,
    plan_id: str,
) -> dict[str, object]:
    """Return a compact node-by-node plan trace for integration verification."""

    try:
        value = NativePlanDiagnosticsService().trace(plan_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if normalize_warehouse_id(str(value.get("warehouse_id"))) != normalize_warehouse_id(warehouse_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Path warehouse_id does not match the persisted plan.",
        )
    return value


@router.get(
    "/api/v1/missions/plans/{plan_id}/debug",
    response_model=OrchestrationResult,
)
def get_simulation_plan_debug(plan_id: str) -> OrchestrationResult:
    """Return the persisted full solver/MAPF/debug result on an explicit endpoint."""

    try:
        _plan, result = SimulationPlanStore().load(plan_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {plan_id} has no persisted orchestration debug result.",
        )
    return result


@router.get(
    "/api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/debug",
    response_model=OrchestrationResult,
)
def get_simulation_plan_debug_for_warehouse(
    warehouse_id: str, plan_id: str
) -> OrchestrationResult:
    result = get_simulation_plan_debug(plan_id)
    expected = normalize_warehouse_id(warehouse_id)
    if result.warehouse_id != expected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {plan_id} does not belong to warehouse {expected}.",
        )
    return result


@router.get("/api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}")
def get_simulation_plan_for_warehouse(
    warehouse_id: str, plan_id: str
) -> dict[str, object]:
    """Read a persisted plan only from its owning warehouse namespace."""

    payload = get_simulation_plan(plan_id)
    plan = payload.get("plan")
    expected = normalize_warehouse_id(warehouse_id)
    observed = (
        normalize_warehouse_id(str(plan.get("warehouse_id")))
        if isinstance(plan, dict)
        else None
    )
    if observed != expected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {plan_id} does not belong to warehouse {expected}.",
        )
    return payload


@router.post(
    "/api/v1/warehouses/{warehouse_id}/missions/replan",
    response_model=SimulationPlanResponse,
)
def replan_simulation_for_warehouse(
    warehouse_id: str, request: PublicReplanMissionRequest
) -> SimulationPlanResponse:
    """Replan one active simulation without allowing cross-warehouse handover."""

    _assert_warehouse_match(warehouse_id, request.mission.warehouse_id)
    return replan_simulation(request)


@router.post("/api/v1/missions/replan", response_model=SimulationPlanResponse)
@router.post("/missions/replan", response_model=SimulationPlanResponse, include_in_schema=False)
def replan_simulation(request: PublicReplanMissionRequest) -> SimulationPlanResponse:
    """Replan at per-robot safe handovers while preserving committed work.

    Empty robots finish only the current edge/service; robots that have started
    a pickup finish the complete inbound or G2P handling cycle.  The public
    ``runtime_snapshot`` remains optional and is used only for plan deviations.
    """

    try:
        return RollingHorizonReplanService().replan(request.to_internal())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/api/v1/debug/evaluations")
def list_planning_evaluations(limit: int = 100) -> dict[str, object]:
    """List frozen Rule/Agent evaluation captures for the debug console."""

    values = PlanningEvaluationStore().list(limit=max(1, min(limit, 500)))
    return {"count": len(values), "evaluations": values}


@router.get("/api/v1/debug/evaluations/{evaluation_id}")
def get_planning_evaluation(evaluation_id: str) -> dict[str, object]:
    """Return one capture, its primary evidence, and an optional comparison."""

    try:
        return PlanningEvaluationStore().detail(evaluation_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/api/v1/debug/evaluations/{evaluation_id}/compare")
def compare_planning_evaluation(
    evaluation_id: str, request: PlanningComparisonRequest
) -> dict[str, object]:
    """Run deferred Rule/Agent replay on the frozen capture; never changes active plans."""

    try:
        return PlanningComparisonService().compare(
            evaluation_id, request
        ).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/api/v1/debug/evaluations/{evaluation_id}/comparison")
def get_planning_comparison(evaluation_id: str) -> dict[str, object]:
    """Read a previously completed comparison without re-running either branch."""

    path = PlanningEvaluationStore().comparison_path(evaluation_id)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evaluation {evaluation_id} has no completed comparison.",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/api/v1/warehouses/{warehouse_id}/maps/current")
def current_route_map_for_warehouse(
    warehouse_id: str, simulation_id: str = "SIM001"
) -> dict[str, object]:
    """Return display coordinates and physical edge metrics for one warehouse."""

    repository = get_repository(warehouse_id, simulation_id)
    settings = get_settings()
    nodes = []
    for value in repository.nodes.values():
        public_node = {
            key: item for key, item in value.items() if key != "scope_id"
        }
        render_x = float(value["x"])
        render_y = float(value["y"])
        public_node.update(
            {
                "render_x": render_x,
                "render_y": render_y,
                "x": round(
                    render_x * settings.map_meters_per_coordinate_unit, 6
                ),
                "y": round(
                    render_y * settings.map_meters_per_coordinate_unit, 6
                ),
            }
        )
        nodes.append(public_node)
    edges: list[dict[str, object]] = []
    for edge_id, value in repository.edges.items():
        distance_m, nominal_travel_time_ms = repository.base_edge_metrics(edge_id)
        public_edge = {
            key: item for key, item in value.items() if key != "scope_id"
        }
        public_edge.update(
            {
                "distance_m": distance_m,
                "nominal_travel_time_ms": nominal_travel_time_ms,
                "speed_limit_mps": float(
                    value.get("speed_limit_mps")
                    or settings.robot_nominal_speed_mps
                ),
            }
        )
        edges.append(public_edge)
    return {
        "warehouse_id": repository.warehouse_id,
        "map_version": repository.versions["graph_version"],
        "coordinate_system": {
            "type": "METERS",
            "x_field": "x",
            "y_field": "y",
            "source_render_unit": repository.graph.get(
                "coordinate_unit", "display_unit"
            ),
            "meters_per_source_unit": settings.map_meters_per_coordinate_unit,
        },
        "nodes": nodes,
        "edges": edges,
    }


@router.get("/api/v1/maps/current")
def current_route_map(
    warehouse_id: str | None = None, simulation_id: str = "SIM001"
) -> dict[str, object]:
    return current_route_map_for_warehouse(
        warehouse_id or get_settings().default_warehouse_id, simulation_id
    )


@router.get("/maps/current", include_in_schema=False)
def current_route_map_legacy() -> dict[str, object]:
    return current_route_map()


@router.post(
    "/fulfillment/goods-to-person/plan",
    response_model=OrchestrationResult,
    deprecated=True,
)
def plan_goods_to_person(request: GoodsToPersonPlanRequest) -> OrchestrationResult:
    """Compatibility wrapper over the one canonical orchestration graph.

    The endpoint no longer owns a solver or traffic pipeline.  It creates exact
    ``new_order`` events, invokes the trusted Rule route, then uses the same G2P
    compiler, payload builder, optimizer, MAPF, validators, and persistence nodes
    as ``POST /missions/orchestrate``.
    """

    return OrchestrationService().run(
        AutoMissionRequest(
            warehouse_id=request.warehouse_id,
            simulation_id=request.simulation_id,
            request_mode="event_driven",
            optimization_backend=request.optimization_backend,
            events=[
                EventInput(type="new_order", order_id=order_id)
                for order_id in request.order_ids
            ],
            goods_to_person_options=GoodsToPersonOptions(
                preferred_station_id=request.preferred_station_id,
                require_single_handling_unit=request.require_single_handling_unit,
                same_mobile_robot_round_trip=request.same_mobile_robot_round_trip,
            ),
        ),
        trusted_planning_mode="force_rule",
    )


@router.post("/fulfillment/goods-to-person/reserve")
def reserve_goods_to_person_batch(
    payload: GoodsToPersonBatchReservationRequest,
) -> dict[str, object]:
    """Reserve a planned handling unit and all order allocations transactionally."""

    settings = get_settings()
    if settings.warehouse_repository_backend not in {"embedded", "live"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Batch reservation requires WAREHOUSE_REPOSITORY_BACKEND=embedded or live.",
        )
    batch = payload.batch.model_dump(mode="json")
    batch["simulation_id"] = payload.simulation_id
    try:
        reservation_id = get_infrastructure_manager().postgres.create_batch_reservation(
            warehouse_id=payload.warehouse_id,
            batch=batch,
            allocations=[value.model_dump(mode="json") for value in payload.batch.allocations],
            expected_version=payload.batch.handling_unit_version,
        )
        return {
            "status": "reserved",
            "batch_id": payload.batch.batch_id,
            "reservation_id": reservation_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/fulfillment/goods-to-person/commit")
def commit_goods_to_person_batch(
    payload: GoodsToPersonStationCommitRequest,
) -> dict[str, object]:
    """Commit the station robot's quantity removal and order fulfillment."""

    settings = get_settings()
    if settings.warehouse_repository_backend not in {"embedded", "live"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Station commit requires WAREHOUSE_REPOSITORY_BACKEND=embedded or live.",
        )
    try:
        result = get_infrastructure_manager().postgres.commit_station_pick(
            warehouse_id=payload.warehouse_id,
            batch_id=payload.batch_id
        )
        get_repository.cache_clear()
        return {"status": "committed", **result}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/fulfillment/goods-to-person/complete-post-move")
def complete_goods_to_person_post_move(
    payload: GoodsToPersonPostMoveCommitRequest,
) -> dict[str, object]:
    """Confirm home-rack return or empty-tote-buffer placement by the same AMR."""

    settings = get_settings()
    if settings.warehouse_repository_backend not in {"embedded", "live"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Post-move commit requires WAREHOUSE_REPOSITORY_BACKEND=embedded or live.",
        )
    try:
        result = get_infrastructure_manager().postgres.complete_post_station_move(
            warehouse_id=payload.warehouse_id,
            batch_id=payload.batch_id,
            robot_id=payload.robot_id,
        )
        get_repository.cache_clear()
        return {"status": "completed", **result}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/api/v1/warehouses/{warehouse_id}/simulations/{simulation_id}/robots"
)
def runtime_robots_for_warehouse(
    warehouse_id: str, simulation_id: str
) -> list[dict[str, object]]:
    try:
        return get_infrastructure_manager().redis.all_robots(
            normalize_warehouse_id(warehouse_id), simulation_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get(
    "/api/v1/warehouses/{warehouse_id}/simulations/{simulation_id}/edges"
)
def runtime_edges_for_warehouse(
    warehouse_id: str, simulation_id: str
) -> list[dict[str, object]]:
    try:
        return get_infrastructure_manager().redis.edge_runtime(
            normalize_warehouse_id(warehouse_id), simulation_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get(
    "/api/v1/warehouses/{warehouse_id}/simulations/{simulation_id}/stations"
)
def runtime_stations_for_warehouse(
    warehouse_id: str, simulation_id: str
) -> list[dict[str, object]]:
    try:
        return get_infrastructure_manager().redis.station_runtime(
            normalize_warehouse_id(warehouse_id), simulation_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get("/runtime/{simulation_id}/robots", include_in_schema=False)
def runtime_robots(simulation_id: str) -> list[dict[str, object]]:
    """Return the latest Redis hash for every robot in one simulation."""

    try:
        return get_infrastructure_manager().redis.all_robots(get_settings().default_warehouse_id, simulation_id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/runtime/{simulation_id}/edges", include_in_schema=False)
def runtime_edges(simulation_id: str) -> list[dict[str, object]]:
    """Return current Redis edge overlays."""

    try:
        return get_infrastructure_manager().redis.edge_runtime(get_settings().default_warehouse_id, simulation_id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/runtime/{simulation_id}/stations", include_in_schema=False)
def runtime_stations(simulation_id: str) -> list[dict[str, object]]:
    """Return fixed outbound-station robot runtime records."""

    try:
        return get_infrastructure_manager().redis.station_runtime(get_settings().default_warehouse_id, simulation_id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post(
    "/api/v1/warehouses/{warehouse_id}/simulations/{simulation_id}/robots/{robot_id}/telemetry"
)
def update_robot_telemetry(
    warehouse_id: str,
    simulation_id: str,
    robot_id: str,
    payload: RobotTelemetryUpdateRequest,
) -> dict[str, object]:
    """Write current battery/location state to the warehouse-scoped runtime store."""

    resolved_warehouse = normalize_warehouse_id(warehouse_id)
    if payload.warehouse_id and normalize_warehouse_id(payload.warehouse_id) != resolved_warehouse:
        raise HTTPException(status_code=409, detail="warehouse_id mismatch")
    if payload.simulation_id and payload.simulation_id != simulation_id:
        raise HTTPException(status_code=409, detail="simulation_id mismatch")
    accepted = get_infrastructure_manager().redis.update_robot_state(
        warehouse_id=resolved_warehouse,
        simulation_id=simulation_id,
        robot_id=robot_id,
        state=payload.model_dump(
            mode="json",
            exclude={"sequence", "warehouse_id", "simulation_id"},
        ),
        sequence=payload.sequence,
    )
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telemetry sequence is stale or duplicated.",
        )
    return {
        "status": "accepted",
        "warehouse_id": resolved_warehouse,
        "simulation_id": simulation_id,
        "robot_id": robot_id,
        "sequence": payload.sequence,
        "runtime_version": get_infrastructure_manager().redis.runtime_version(
            resolved_warehouse, simulation_id
        ),
    }


@router.post("/runtime/{simulation_id}/robots/{robot_id}/telemetry", include_in_schema=False)
def update_robot_telemetry_legacy(
    simulation_id: str, robot_id: str, payload: RobotTelemetryUpdateRequest
) -> dict[str, object]:
    return update_robot_telemetry(
        payload.warehouse_id or get_settings().default_warehouse_id,
        simulation_id,
        robot_id,
        payload,
    )


@router.post(
    "/api/v1/warehouses/{warehouse_id}/simulations/{simulation_id}/commands"
)
def publish_runtime_command(
    warehouse_id: str,
    simulation_id: str,
    payload: RuntimeCommandPublishRequest,
) -> dict[str, object]:
    resolved_warehouse = normalize_warehouse_id(warehouse_id)
    if payload.warehouse_id and normalize_warehouse_id(payload.warehouse_id) != resolved_warehouse:
        raise HTTPException(status_code=409, detail="warehouse_id mismatch")
    if payload.simulation_id and payload.simulation_id != simulation_id:
        raise HTTPException(status_code=409, detail="simulation_id mismatch")
    command = payload.model_dump(
        mode="json", exclude={"warehouse_id", "simulation_id"}
    )
    stream_id = get_infrastructure_manager().redis.publish_command(
        warehouse_id=resolved_warehouse,
        simulation_id=simulation_id,
        command=command,
    )
    return {
        "status": "published",
        "warehouse_id": resolved_warehouse,
        "simulation_id": simulation_id,
        "stream_id": stream_id,
        "command": command,
    }


@router.post("/runtime/{simulation_id}/commands", include_in_schema=False)
def publish_runtime_command_legacy(
    simulation_id: str, payload: RuntimeCommandPublishRequest
) -> dict[str, object]:
    return publish_runtime_command(
        payload.warehouse_id or get_settings().default_warehouse_id,
        simulation_id,
        payload,
    )


@router.get("/hitl/pending", response_model=list[HumanInteractionRecord])
def pending_human_interactions(
    warehouse_id: str | None = None,
) -> list[HumanInteractionRecord]:
    """List unresolved cards, optionally filtered to one warehouse."""

    records = HumanInteractionService().list_pending()
    if warehouse_id is None:
        return records
    expected = normalize_warehouse_id(warehouse_id)
    return [
        record
        for record in records
        if normalize_warehouse_id(record.original_request.get("warehouse_id", "WH-001"))
        == expected
    ]


@router.get("/hitl/{interaction_id}", response_model=HumanInteractionRecord)
def get_human_interaction(interaction_id: str) -> HumanInteractionRecord:
    """Read one persisted HITL checkpoint."""

    try:
        return HumanInteractionService().get(interaction_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/hitl/{interaction_id}/respond", response_model=HumanInteractionResumeResult)
def respond_to_human_interaction(
    interaction_id: str,
    payload: HumanInteractionResumeRequest,
) -> HumanInteractionResumeResult:
    """Resolve one HITL card and resume the workflow with an auditable response."""

    try:
        return HumanInteractionService().respond(interaction_id, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

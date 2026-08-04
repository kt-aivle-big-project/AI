"""FastAPI endpoints matching the current, unmodified Spring BE records."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.domain.be_compat import (
    BeCompatContractStatus,
    BeCompatRuntimeBootstrapRequest,
    BeCompatRuntimeSnapshot,
    BeGraphSnapshot,
    BeGraphStatusResponse,
    BeOptimizationRequest,
    BeOptimizationResponse,
    BeReoptimizationRequest,
    BeReoptimizationResponse,
)
from app.repositories.be_compat_repository import (
    BeCompatGraphNotFoundError,
    BeCompatRepository,
)
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository
from app.services.be_compat_service import (
    BeCompatOptimizationService,
    BeCompatRoutingError,
)

router = APIRouter(tags=["Spring BE compatibility"])


def _assert_enabled() -> None:
    if not get_settings().be_compat_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Spring BE compatibility endpoints are disabled.",
        )


@router.post(
    "/optimize",
    response_model=BeOptimizationResponse,
    response_model_by_alias=True,
    summary="Unmodified Spring BE initial optimization contract",
)
def optimize_for_original_be(request: BeOptimizationRequest) -> BeOptimizationResponse:
    """Compute one directed shortest route per robot and persist the graph snapshot.

    This endpoint intentionally keeps the exact request/response shape expected by
    ``BE-main``'s current ``OptimizationClient``.  LARO first reuses Spring's
    shared PostgreSQL graph; when it is unavailable or different, the request
    graph is stored only in the additive ``laro_contract`` schema.  Redis keeps
    metadata by default and Neo4j remains a disposable route projection.
    """

    _assert_enabled()
    try:
        return BeCompatOptimizationService().optimize(request)
    except BeCompatRoutingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@router.post(
    "/reoptimize",
    response_model=BeReoptimizationResponse,
    response_model_by_alias=True,
    summary="Unmodified Spring BE reoptimization contract",
)
def reoptimize_for_original_be(
    request: BeReoptimizationRequest,
) -> BeReoptimizationResponse:
    """Reassign remaining tasks using Spring DB or the additive contract graph."""

    _assert_enabled()
    try:
        return BeCompatOptimizationService().reoptimize(request)
    except BeCompatGraphNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BeCompatRoutingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@router.put(
    "/compat/v1/warehouses/{warehouse_id}/graph",
    response_model=BeGraphStatusResponse,
    response_model_by_alias=True,
    summary="Manually register a Spring numeric-ID graph",
)
def register_compat_graph(
    warehouse_id: int,
    snapshot: BeGraphSnapshot,
) -> BeGraphStatusResponse:
    """Optional bootstrap endpoint when /reoptimize may precede /optimize."""

    _assert_enabled()
    if warehouse_id != snapshot.warehouse_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Path warehouse_id and body warehouseId must match.",
        )
    repository = BeCompatRepository()
    saved = repository.save_graph(
        warehouse_id=warehouse_id,
        nodes=snapshot.nodes,
        edges=snapshot.edges,
    )
    return BeGraphStatusResponse(
        warehouseId=warehouse_id,
        available=True,
        graphVersion=saved.graph_version,
        nodeCount=len(saved.nodes),
        edgeCount=len(saved.edges),
        source="request",
    )


@router.get(
    "/compat/v1/warehouses/{warehouse_id}/graph",
    response_model=BeGraphStatusResponse,
    response_model_by_alias=True,
    summary="Check whether /reoptimize has a stored graph",
)
def get_compat_graph_status(warehouse_id: int) -> BeGraphStatusResponse:
    _assert_enabled()
    snapshot, source = BeCompatRepository().graph_status(warehouse_id)
    if snapshot is None:
        return BeGraphStatusResponse(
            warehouseId=warehouse_id,
            available=False,
            graphVersion=None,
            nodeCount=0,
            edgeCount=0,
            source=None,
        )
    return BeGraphStatusResponse(
        warehouseId=warehouse_id,
        available=True,
        graphVersion=snapshot.graph_version,
        nodeCount=len(snapshot.nodes),
        edgeCount=len(snapshot.edges),
        source=source,
    )


@router.get(
    "/compat/v2/contract",
    response_model=BeCompatContractStatus,
    response_model_by_alias=True,
    summary="Shared Spring PostgreSQL/Redis compatibility contract status",
)
def get_shared_contract_status() -> BeCompatContractStatus:
    _assert_enabled()
    settings = get_settings()
    status_value = BeCompatRepository().contract_status()
    return BeCompatContractStatus(
        schema="laro_contract",
        ready=bool(status_value["ready"]),
        springTablesAvailable=bool(status_value["spring_tables_available"]),
        graphSource=settings.be_compat_graph_source,
        graphCacheMode=settings.be_compat_graph_cache_mode,
        runtimeSource=settings.be_compat_runtime_source,
        tables=list(status_value["tables"]),
        notes=[
            "BE-main source code remains unchanged.",
            "Spring public tables are preferred; laro_contract is an additive fallback.",
            "Redis full-graph duplication is disabled unless graphCacheMode=full.",
        ],
    )


@router.get(
    "/compat/v2/simulation-runs/{simulation_run_id}/runtime",
    response_model=BeCompatRuntimeSnapshot,
    response_model_by_alias=True,
    summary="Inspect the unmodified Spring Redis robot runtime",
)
def get_spring_runtime(simulation_run_id: int) -> BeCompatRuntimeSnapshot:
    _assert_enabled()
    return BeSpringRuntimeRepository().snapshot(simulation_run_id)


@router.put(
    "/compat/v2/simulation-runs/{simulation_run_id}/runtime",
    response_model=BeCompatRuntimeSnapshot,
    response_model_by_alias=True,
    summary="Bootstrap Spring-format Redis runtime for local integration tests",
)
def bootstrap_spring_runtime(
    simulation_run_id: int,
    request: BeCompatRuntimeBootstrapRequest,
) -> BeCompatRuntimeSnapshot:
    _assert_enabled()
    settings = get_settings()
    if not settings.be_compat_debug_runtime_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BE compatibility runtime bootstrap API is disabled.",
        )
    return BeSpringRuntimeRepository().bootstrap(
        simulation_run_id,
        warehouse_id=request.warehouse_id,
        robots=request.robots,
        sim_time_ms=request.sim_time_ms,
        replace=request.replace,
    )


@router.get("/compat/v1/contract", summary="Compatibility contract metadata")
def get_compat_contract() -> dict[str, object]:
    _assert_enabled()
    return {
        "spring_backend_modified": False,
        "client_endpoints": {
            "optimize": "POST /optimize",
            "reoptimize": "POST /reoptimize",
        },
        "estimatedTime_unit": "seconds",
        "totalDistance_unit": "same unit as OptimizationRequest.edges[].distance",
        "initial_graph_rule": (
            "LARO first reads Spring warehouse_node/warehouse_edge from shared PostgreSQL. "
            "If unavailable or different, POST /optimize stores a normalized additive fallback "
            "in laro_contract.route_node/route_edge."
        ),
        "spring_redis_rule": (
            "POST /reoptimize uses request robots first and can fall back to "
            "simulation:run:{runId}:robot:{robotId}:state when the request list is empty."
        ),
        "response_limit": (
            "The original Spring DTO exposes nodePath only; LARO MOVE/WAIT/SERVICE "
            "timelines remain available through the native /api/v1 mission APIs."
        ),
    }

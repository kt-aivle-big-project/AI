"""FastAPI routes required by the Spring BE."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.llm_gateway import LLMConfigurationError, LLMInvocationError
from app.domain.be_centered import (
    BeCenteredPreflightResponse,
    BeHumanInteractionResumeResponse,
    BeSimulationPlanRequest,
    BeSimulationPlanResponse,
    BeSimulationReplanRequest,
)
from app.domain.fulfillment_command import (
    FulfillmentCommandGenerateRequest,
    FulfillmentCommandGenerateResponse,
)
from app.domain.schemas import HumanInteractionResumeRequest
from app.infrastructure.be_centered_postgres import BeCenteredDataError
from app.services.be_centered_plan_service import BeCenteredPlanService
from app.services.fulfillment_command_agent_service import (
    FulfillmentCommandAgentError,
    FulfillmentCommandAgentService,
)

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    """Return the minimal liveness payload consumed by the load balancer."""

    return {
        "status": "ok",
        "version": "13.27.0",
    }


@router.get(
    "/api/v1/simulation-runs/{simulation_run_id}/missions/plan/preflight",
    response_model=BeCenteredPreflightResponse,
)
def be_centered_plan_preflight(
    simulation_run_id: int,
) -> BeCenteredPreflightResponse:
    """Check the Spring run, shared runtime, inventory, and route graph."""

    return BeCenteredPlanService().preflight(simulation_run_id)


@router.post(
    "/api/v1/simulation-runs/{simulation_run_id}/fulfillment-commands/generate",
    response_model=FulfillmentCommandGenerateResponse,
)
def generate_be_centered_fulfillment_commands(
    simulation_run_id: int,
    request: FulfillmentCommandGenerateRequest,
) -> FulfillmentCommandGenerateResponse:
    """Let the Agent choose a feasible BOX batch from authoritative BE facts."""

    try:
        return FulfillmentCommandAgentService().generate(simulation_run_id, request)
    except FulfillmentCommandAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except BeCenteredDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except LLMConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except LLMInvocationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/v1/simulation-runs/{simulation_run_id}/missions/plan",
    response_model=BeSimulationPlanResponse,
)
def create_be_centered_simulation_plan(
    simulation_run_id: int,
    request: BeSimulationPlanRequest,
) -> BeSimulationPlanResponse:
    """Plan authoritative structured operations for one Spring run."""

    try:
        return BeCenteredPlanService().plan(simulation_run_id, request)
    except BeCenteredDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/v1/simulation-runs/{simulation_run_id}/missions/replan",
    response_model=BeSimulationPlanResponse,
)
def be_centered_replan(
    simulation_run_id: int,
    request: BeSimulationReplanRequest,
) -> BeSimulationPlanResponse:
    """Replan one Spring run and persist its replacement pending plan."""

    try:
        return BeCenteredPlanService().replan(simulation_run_id, request)
    except (BeCenteredDataError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/v1/simulation-runs/{simulation_run_id}/hitl/{interaction_id}/respond",
    response_model=BeHumanInteractionResumeResponse,
)
def respond_to_be_centered_human_interaction(
    simulation_run_id: int,
    interaction_id: str,
    request: HumanInteractionResumeRequest,
) -> BeHumanInteractionResumeResponse:
    """Resolve a review decision through the same Spring run contract."""

    try:
        return BeCenteredPlanService().respond_to_human_interaction(
            simulation_run_id,
            interaction_id,
            request,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (BeCenteredDataError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

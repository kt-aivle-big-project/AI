"""Compatibility endpoints still called by the Spring optimization client."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.domain.be_compat import (
    BeOptimizationRequest,
    BeOptimizationResponse,
    BeReoptimizationRequest,
    BeReoptimizationResponse,
)
from app.repositories.be_compat_repository import BeCompatGraphNotFoundError
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
    summary="Spring BE initial optimization contract",
)
def optimize_for_original_be(request: BeOptimizationRequest) -> BeOptimizationResponse:
    """Serve the request/response shape consumed by Spring HttpOptimizationClient."""

    _assert_enabled()
    try:
        return BeCompatOptimizationService().optimize(request)
    except BeCompatRoutingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@router.post(
    "/reoptimize",
    response_model=BeReoptimizationResponse,
    response_model_by_alias=True,
    summary="Spring BE reoptimization contract",
)
def reoptimize_for_original_be(
    request: BeReoptimizationRequest,
) -> BeReoptimizationResponse:
    """Reassign the remaining Spring tasks without changing the Java contract."""

    _assert_enabled()
    try:
        return BeCompatOptimizationService().reoptimize(request)
    except (BeCompatGraphNotFoundError, BeCompatRoutingError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

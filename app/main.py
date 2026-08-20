"""FastAPI application entry point with live-infrastructure lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.api.be_compat_routes import router as be_compat_router
from app.core.config import get_settings
from app.core.http import UTF8JSONResponse
from app.infrastructure.manager import get_infrastructure_manager
from app.services.planning_evaluation_job_service import (
    shutdown_planning_evaluation_job_service,
)
from app.services.planning_scenario_suite_service import (
    shutdown_planning_scenario_suite_service,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open long-lived pools/clients once and close them on application shutdown."""

    manager = get_infrastructure_manager()
    startup = manager.start()
    app.state.infrastructure = manager
    app.state.infrastructure_startup = startup
    try:
        yield
    finally:
        shutdown_planning_scenario_suite_service()
        shutdown_planning_evaluation_job_service()
        manager.close()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="13.27.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)
app.include_router(router)
app.include_router(be_compat_router)

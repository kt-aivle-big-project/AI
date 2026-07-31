"""FastAPI application entry point with live-infrastructure lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.api.be_compat_routes import router as be_compat_router
from app.core.config import get_settings
from app.core.http import UTF8JSONResponse
from app.infrastructure.manager import get_infrastructure_manager


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
        manager.close()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="13.25.1",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)
app.include_router(router)
app.include_router(be_compat_router)

"""Configure LangSmith automatic tracing from validated settings."""
from __future__ import annotations

import os

from app.core.config import Settings


class LangSmithConfigurationError(RuntimeError):
    """Raised when tracing is enabled without an API key."""


def configure_langsmith_environment(settings: Settings) -> None:
    """Mirror validated LangSmith values into process environment variables."""

    if settings.langsmith_enabled and not settings.langsmith_api_key:
        raise LangSmithConfigurationError(
            "LANGSMITH_TRACING=true but LANGSMITH_API_KEY is not configured."
        )
    values = {
        "LANGSMITH_TRACING": "true" if settings.langsmith_enabled else "false",
        "LANGSMITH_API_KEY": settings.langsmith_api_key,
        "LANGSMITH_PROJECT": settings.langsmith_project,
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
    }
    for name, value in values.items():
        if value is not None:
            os.environ.setdefault(name, str(value))

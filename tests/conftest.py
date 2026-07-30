"""Hermetic pytest environment independent of a developer's local ``.env``.

The v12 numeric MAPF regressions intentionally use the original canonical
1,000 ms pickup/drop handling time with no per-unit increment.  Later v13
mixed-batch scenarios declare their own handling-time policy explicitly, so
fixing the test defaults here does not weaken the v13 service-time tests.

This module is imported by pytest before test modules and before module-scoped
fixtures are constructed.  Therefore the environment must be normalized at
module import time; a function-scoped autouse fixture alone is too late for
module-scoped fixtures such as ``test_v12_solver_mapf_core.problem``.
"""
from __future__ import annotations

import os
from pathlib import Path

# Canonical, offline-safe test configuration.  Environment variables override
# values loaded from a developer .env by pydantic-settings.
_TEST_ENV: dict[str, str] = {
    "NODE_CONSOLE_TRACE": "false",
    "LARO_ENV_FILE": "",
    "OPTIMIZATION_BACKEND": "ortools",
    "FRONTEND_EXPLANATION_MODE": "deterministic",
    "AGENT_RETRIEVAL_MODE": "stepwise",
    "AGENT_OPTIONAL_RETRIEVAL_PLANNER": "off",
    "ALLOW_REQUEST_PLANNING_MODE_OVERRIDE": "true",
    "DATA_DIR": str((Path(__file__).resolve().parents[1] / "data").resolve()),
    "MAP_REPOSITORY_BACKEND": "json",
    "WAREHOUSE_REPOSITORY_BACKEND": "json",
    # Historical v12-v13.14 tests retain their original per-order task model.
    # Dedicated v13.15 integration tests override this to goods_to_person.
    "OUTBOUND_FULFILLMENT_MODE": "legacy_order_tasks",
    # Preserve the original v12 numeric regression contract.
    "PICKUP_SERVICE_TIME_MS": "1000",
    "PICKUP_SERVICE_TIME_PER_UNIT_MS": "0",
    "DROP_SERVICE_TIME_MS": "1000",
    "DROP_SERVICE_TIME_PER_UNIT_MS": "0",
    # Never allow unit tests to consume real external credentials.
    "NVIDIA_API_KEY": "",
    "CUOPT_HTTP_API_KEY": "",
    "CUOPT_API_KEY": "",  # removed legacy name; blank defensively
    "CUOPT_CLIENT_SAK": "",
    "NVIDIA_IDENTITY_FEDERATION_API_KEY": "",
    "CUOPT_CLIENT_ID": "",
    "CUOPT_CLIENT_SECRET": "",
    "OPENAI_API_KEY": "",
    "LANGSMITH_API_KEY": "",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
}

for _name, _value in _TEST_ENV.items():
    os.environ[_name] = _value

import pytest

from app.core.config import get_settings
from app.repositories.json_repository import set_data_dir


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch: pytest.MonkeyPatch):
    """Reassert canonical test settings and clear cached Settings per test."""

    for name, value in _TEST_ENV.items():
        monkeypatch.setenv(name, value)
    set_data_dir(None)
    get_settings.cache_clear()
    yield
    set_data_dir(None)
    get_settings.cache_clear()

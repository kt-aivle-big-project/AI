"""Request-scoped warehouse/session context for repository selection.

The public API carries ``warehouse_id`` and ``simulation_id``.  Graph nodes keep
calling the stable ``get_repository()`` function, while this context selects the
correct cached repository instance.  Explicit repository instances are passed
into worker threads so request scope is not lost across ThreadPoolExecutor.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.domain.schemas import normalize_warehouse_id

_current_warehouse_id: ContextVar[str | None] = ContextVar(
    "laro_warehouse_id", default=None
)
_current_simulation_id: ContextVar[str | None] = ContextVar(
    "laro_simulation_id", default=None
)
_current_repository: ContextVar[Any | None] = ContextVar(
    "laro_request_repository", default=None
)


def current_warehouse_id(default: str = "WH-001") -> str:
    return normalize_warehouse_id(_current_warehouse_id.get() or default)


def current_simulation_id(default: str = "SIM001") -> str:
    return str(_current_simulation_id.get() or default)


def current_repository() -> Any | None:
    """Return the repository snapshot bound to the current orchestration request."""

    return _current_repository.get()


@contextmanager
def repository_scope(warehouse_id: str, simulation_id: str) -> Iterator[None]:
    warehouse_token = _current_warehouse_id.set(normalize_warehouse_id(warehouse_id))
    simulation_token = _current_simulation_id.set(str(simulation_id))
    try:
        yield
    finally:
        _current_warehouse_id.reset(warehouse_token)
        _current_simulation_id.reset(simulation_token)


@contextmanager
def repository_instance_scope(repository: Any) -> Iterator[None]:
    """Bind one immutable repository snapshot for all nodes in one request."""

    token = _current_repository.set(repository)
    try:
        yield
    finally:
        _current_repository.reset(token)

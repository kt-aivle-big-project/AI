"""Planning package with lazy graph loading.

Importing helper functions from ``app.planning.nodes`` should not require the
optional LangGraph runtime. The compiled graph is loaded only when requested.
"""

from __future__ import annotations

from typing import Any

__all__ = ["planning_graph", "run_planning"]


def run_planning(*args: Any, **kwargs: Any) -> dict:
    from app.planning.graph import run_planning as _run_planning

    return _run_planning(*args, **kwargs)


def __getattr__(name: str) -> Any:
    if name == "planning_graph":
        from app.planning.graph import planning_graph

        return planning_graph
    raise AttributeError(name)

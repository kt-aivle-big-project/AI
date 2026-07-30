"""Console and state-level observability for every LangGraph node."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from time import perf_counter
from typing import Any, Callable, TypeVar, cast

from app.core.config import get_settings
from app.core.console import safe_console_print
from app.domain.schemas import NodeExecutionRecord

F = TypeVar("F", bound=Callable[..., dict[str, Any]])


def _now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


def _print(message: str) -> None:
    """Print a console marker when node tracing is enabled."""

    if get_settings().node_console_trace:
        safe_console_print(message)


def observe_node(node_name: str, *, purpose: str, llm_used: bool = False) -> Callable[[F], F]:
    """Decorate a graph node with Korean start/end markers and an audit record."""

    def decorator(func: F) -> F:
        """Wrap one synchronous graph node."""

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            """Execute a node and append ``node_execution_log`` to its update."""

            started_at = _now()
            started_clock = perf_counter()
            _print(f"[{node_name} 노드 시작] 목적={purpose}")
            try:
                update = func(*args, **kwargs)
                if not isinstance(update, dict):
                    raise TypeError(f"{node_name} must return a partial-state dict")
            except Exception as exc:
                duration_ms = round((perf_counter() - started_clock) * 1000, 3)
                _print(f"[{node_name} 노드 종료] status=failed duration_ms={duration_ms} exception={exc}")
                raise
            duration_ms = round((perf_counter() - started_clock) * 1000, 3)
            actual_llm_used = bool(update.pop("_llm_used", llm_used))
            errors = update.get("errors") or []
            error_code = None
            if errors:
                latest = errors[-1]
                error_code = getattr(latest, "code", None) or (latest.get("code") if isinstance(latest, dict) else None)
            record = NodeExecutionRecord(
                node_name=node_name,
                purpose=purpose,
                status="failed" if update.get("failure_requested") else "success",
                started_at=started_at,
                ended_at=_now(),
                duration_ms=duration_ms,
                output_keys=sorted(k for k in update if k != "node_execution_log"),
                llm_used=actual_llm_used,
                error_code=error_code,
            )
            update["node_execution_log"] = [record]
            for summary in update.get("llm_node_summaries") or []:
                task = getattr(summary, "task_summary", None) or summary.get("task_summary")
                result = getattr(summary, "output_summary", None) or summary.get("output_summary")
                _print(f"[{node_name} LLM 작업 요약] task={task} | result={result}")
            _print(
                f"[{node_name} 노드 종료] status={record.status} duration_ms={duration_ms} "
                f"output_keys={record.output_keys}"
            )
            return update

        return cast(F, wrapper)

    return decorator

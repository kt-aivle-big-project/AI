"""Shared helpers for graph nodes."""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.config import get_settings
from app.domain.schemas import LLMNodeSummary, OrchestrationPlan, WorkflowError
from app.graph.state import LaroGraphState

T = TypeVar("T", bound=BaseModel)


def model_from_state(state: LaroGraphState, key: str, model: type[T]) -> T:
    """Return a validated model stored under one graph-state key."""

    value = state.get(key)
    if value is None:
        raise ValueError(f"Required state field is missing: {key}")
    return value if isinstance(value, model) else model.model_validate(value)


def require_locked_route(state: LaroGraphState, *, expected_route: str) -> OrchestrationPlan:
    """Validate the immutable pre-execution Rule/Agent branch contract.

    The request router and deterministic guard must finalize the branch before any
    repository/formulation node executes.  Downstream nodes may fail, clarify, or
    request human review, but they may never switch to the opposite branch.
    """

    plan = model_from_state(state, "orchestration_plan", OrchestrationPlan)
    if not plan.route_locked or plan.route_switch_allowed:
        raise ValueError("The orchestration route is not locked before branch execution.")
    if plan.route != expected_route:
        raise ValueError(
            f"Route-lock violation: expected {expected_route}, received {plan.route}."
        )
    return plan


def trace_update(node_name: str) -> dict[str, list[str]]:
    """Return a reducer-friendly workflow trace update."""

    return {"workflow_trace": [node_name]}


def error_update(*, stage: str, code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    """Return one technical failure update routed to workflow_failure."""

    return {
        "failure_requested": True,
        "failure_stage": stage,
        "errors": [WorkflowError(stage=stage, code=code, message=message, retryable=retryable)],
        **trace_update(stage),
    }


def llm_summary(
    *,
    node_name: str,
    prompt_version: str,
    task_summary: str,
    input_summary: str,
    output_summary: str,
    retry_count: int = 0,
) -> LLMNodeSummary:
    """Build a deterministic summary of one structured LLM call."""

    return LLMNodeSummary(
        node_name=node_name,
        prompt_version=prompt_version,
        model_name=get_settings().openai_model,
        task_summary=task_summary,
        input_summary=input_summary,
        output_summary=output_summary,
        retry_count=retry_count,
    )

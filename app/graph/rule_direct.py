"""Deterministic Rule fast-path nodes.

This path never asks an LLM which warehouse read to perform. Structured keys are
validated, the canonical repositories are read in a fixed order, and the cuOpt
dynamic draft is produced directly from typed contexts.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.node_observability import observe_node
from app.domain.schemas import (
    ContextSnapshot,
    InventoryContext,
    InputRejectionResult,
    MapContext,
    NormalizedWarehouseRequest,
    RobotRuntimeContext,
    RuntimePlanningOverrides,
)
from app.graph.node_support import error_update, model_from_state, require_locked_route, trace_update
from app.graph.state import LaroGraphState
from app.services.cuopt_formulation_service import RuleCuOptFormulator
from app.services.rule_direct_service import StructuredKeyValidator


def _time_limit(state: LaroGraphState) -> int:
    settings = get_settings()
    return (
        settings.ortools_time_limit_seconds
        if state["optimization_backend"] == "ortools"
        else settings.cuopt_time_limit_seconds
    )


@observe_node(
    "structured_key_validator",
    purpose="Rule Fast Path의 주문·로봇·Edge 정확 ID를 저장소 기준으로 검증",
)
def structured_key_validator_node(state: LaroGraphState) -> dict:
    """Reject invalid exact identifiers before any direct repository read."""

    try:
        require_locked_route(state, expected_route="RULE_MISSION_PIPELINE")
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        result = StructuredKeyValidator().validate(
            request,
            runtime_overrides=state.get("runtime_overrides"),
        )
        update = {
            "structured_key_validation": result,
            **trace_update("structured_key_validator"),
        }
        if not result.valid and result.requires_user_clarification:
            from app.domain.schemas import ClarificationResult

            update["clarification"] = ClarificationResult(
                reason="One or more structured identifiers do not exist in the authoritative warehouse data.",
                questions=[f"확인해 주세요: {value}" for value in result.errors],
            )
        elif not result.valid:
            update["input_rejection"] = InputRejectionResult(
                reason_code="STRUCTURED_IDENTIFIER_REJECTED",
                message=(
                    "One or more exact structured identifiers are unknown or "
                    "unsupported; no fuzzy execution was attempted."
                ),
                invalid_references=list(result.errors),
            )
        return update
    except Exception as exc:
        return error_update(
            stage="structured_key_validator",
            code="structured_key_validation_error",
            message=str(exc),
        )


@observe_node(
    "rule_cuopt_formulator_direct",
    purpose="Typed Inventory·Robot·Map Snapshot을 규칙으로 직접 cuOpt 동적 입력으로 변환",
)
def rule_cuopt_formulator_direct_node(state: LaroGraphState) -> dict:
    """Create the Rule draft without a retrieval plan or Situation Graph dependency."""

    try:
        require_locked_route(state, expected_route="RULE_MISSION_PIPELINE")
        runtime_overrides = state.get("runtime_overrides")
        minimum_vehicle_count = (
            runtime_overrides.minimum_task_vehicle_count
            if isinstance(runtime_overrides, RuntimePlanningOverrides)
            else 0
        )
        draft = RuleCuOptFormulator().formulate_from_contexts(
            normalized_request=model_from_state(state, "normalized_request", NormalizedWarehouseRequest),
            snapshot=model_from_state(state, "context_snapshot", ContextSnapshot),
            inventory=model_from_state(state, "inventory_context", InventoryContext),
            robots=model_from_state(state, "robot_context", RobotRuntimeContext),
            map_context=model_from_state(state, "map_context", MapContext),
            graph_arcs=list(state["graph_arcs"]),
            time_limit_seconds=_time_limit(state),
            minimum_vehicle_count=minimum_vehicle_count,
        )
        return {
            "cuopt_dynamic_input_draft": draft,
            "formulation_retry_count": 0,
            **trace_update("rule_cuopt_formulator_direct"),
        }
    except Exception as exc:
        return error_update(
            stage="rule_cuopt_formulator_direct",
            code="rule_direct_cuopt_formulation_failed",
            message=str(exc),
        )

"""LangGraph node for final executable-operation coverage validation."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import (
    CuOptDynamicInputDraft,
    NormalizedWarehouseRequest,
    SimulationPlan,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.logical_operation_validation_service import (
    LogicalOperationCoverageValidator,
)


@observe_node(
    "logical_operation_coverage_validator",
    purpose="최종 SimulationPlan이 모든 요청 작업을 정확히 한 번 Task·Robot·SERVICE에 연결했는지 독립 검증",
)
def logical_operation_coverage_validator_node(state: LaroGraphState) -> dict:
    """Fail closed when a downstream stage silently omits business work."""

    try:
        validation = LogicalOperationCoverageValidator().validate(
            request=model_from_state(
                state, "normalized_request", NormalizedWarehouseRequest
            ),
            draft=model_from_state(
                state, "cuopt_dynamic_input_draft", CuOptDynamicInputDraft
            ),
            plan=model_from_state(state, "simulation_plan", SimulationPlan),
        )
        if not validation.valid:
            return {
                "logical_operation_coverage_validation": validation,
                "simulation_plan": None,
                **error_update(
                    stage="logical_operation_coverage_validator",
                    code="plan_operation_coverage_failed",
                    message="; ".join(validation.errors),
                ),
            }
        return {
            "logical_operation_coverage_validation": validation,
            **trace_update("logical_operation_coverage_validator"),
        }
    except Exception as exc:
        return error_update(
            stage="logical_operation_coverage_validator",
            code="plan_operation_coverage_validation_failed",
            message=str(exc),
        )

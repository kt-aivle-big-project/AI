"""Append deterministic PARK/CHARGE goals before MAPF execution."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import (
    CuOptPayload,
    OptimizationRequest,
    OptimizerResult,
    RobotRuntimeContext,
    RuntimePlanningOverrides,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.terminal_relocation_service import TerminalRelocationEnricher


@observe_node(
    "terminal_relocation_enricher",
    purpose=(
        "재계획에서 기존 임무가 사라진 로봇에 PARK/CHARGE 종료 Goal을 추가하고 "
        "사용 로봇의 종료 이동비용을 실행 경로에 반영"
    ),
)
def terminal_relocation_enricher_node(state: LaroGraphState) -> dict:
    try:
        payload = model_from_state(
            state,
            "execution_payload" if state.get("execution_payload") is not None else "cuopt_payload",
            CuOptPayload,
        )
        result = model_from_state(
            state,
            "execution_optimizer_result"
            if state.get("execution_optimizer_result") is not None
            else "optimizer_result",
            OptimizerResult,
        )
        request = model_from_state(state, "optimization_request", OptimizationRequest)
        robots = model_from_state(state, "robot_context", RobotRuntimeContext)
        overrides = state.get("runtime_overrides") or RuntimePlanningOverrides()
        execution_payload, execution_result, relocation = TerminalRelocationEnricher().enrich(
            payload=payload,
            result=result,
            request=request,
            robot_context=robots,
            runtime_overrides=overrides,
            graph_arcs=list(state.get("graph_arcs", [])),
            node_types=dict(state.get("graph_node_types", {})),
        )
        if not relocation.valid:
            raise ValueError("; ".join(relocation.errors))
        return {
            "execution_payload": execution_payload,
            "execution_optimizer_result": execution_result,
            "terminal_relocation": relocation,
            **trace_update("terminal_relocation_enricher"),
        }
    except Exception as exc:
        return error_update(
            stage="terminal_relocation_enricher",
            code="terminal_relocation_failed",
            message=str(exc),
        )

"""Warehouse Situation Graph build and validation nodes."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import (
    ContextSnapshot,
    InventoryContext,
    MapContext,
    NormalizedWarehouseRequest,
    RobotRuntimeContext,
    WarehouseSituationGraph,
)
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.services.situation_graph_service import (
    WarehouseSituationGraphBuilder,
    WarehouseSituationGraphValidator,
)


@observe_node(
    "warehouse_situation_graph_builder",
    purpose="주문·재고·로봇·지도·Traffic Snapshot을 요청 단위 읽기 전용 상황 그래프로 결합",
)
def warehouse_situation_graph_builder_node(state: LaroGraphState) -> dict:
    """Materialize one request-scoped evidence graph from canonical contexts."""

    try:
        graph = WarehouseSituationGraphBuilder().build(
            normalized_request=model_from_state(state, "normalized_request", NormalizedWarehouseRequest),
            snapshot=model_from_state(state, "context_snapshot", ContextSnapshot),
            inventory=model_from_state(state, "inventory_context", InventoryContext),
            robots=model_from_state(state, "robot_context", RobotRuntimeContext),
            map_context=model_from_state(state, "map_context", MapContext),
            graph_arcs=list(state["graph_arcs"]),
            retrieval_observations=list(state.get("retrieval_observations", [])),
        )
        return {
            "warehouse_situation_graph": graph,
            **trace_update("warehouse_situation_graph_builder"),
        }
    except Exception as exc:
        return error_update(
            stage="warehouse_situation_graph_builder",
            code="situation_graph_build_failed",
            message=str(exc),
        )


@observe_node(
    "situation_graph_sufficiency_guard",
    purpose="상황 그래프의 관계·근거·경로·완전성을 독립 검증하고 LLM 정식화 준비 여부를 판정",
)
def situation_graph_sufficiency_guard_node(state: LaroGraphState) -> dict:
    """Reject incomplete or structurally invalid graph-RAG evidence."""

    try:
        graph = model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph)
        validation = WarehouseSituationGraphValidator().validate(graph)
        if not validation.valid:
            update = error_update(
                stage="situation_graph_sufficiency_guard",
                code="situation_graph_not_ready",
                message="; ".join(validation.errors) or "Situation graph is incomplete.",
            )
            return {
                "situation_graph_validation": validation,
                **update,
            }
        return {
            "situation_graph_validation": validation,
            **trace_update("situation_graph_sufficiency_guard"),
        }
    except Exception as exc:
        return error_update(
            stage="situation_graph_sufficiency_guard",
            code="situation_graph_validation_failed",
            message=str(exc),
        )

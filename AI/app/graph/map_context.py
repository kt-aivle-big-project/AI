"""Scoped map context and runtime overlay node."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.services.context_service import (
    WarehouseContextService,
    apply_runtime_map_overrides,
)


@observe_node(
    "map_context",
    purpose="창고 그래프와 혼잡·점유·예약·차단 상태를 한 번 읽어 최적화 Snapshot 생성",
)
def map_context_node(state: LaroGraphState) -> dict:
    """Build map constraints and the complete adjusted directed graph once."""

    try:
        mission = state.get("mission_spec") or state.get("effective_mission_spec")
        bundle = WarehouseContextService().build_map_context(
            inventory=state.get("inventory_context"),
            mission=mission,
            edge_ids=[event.edge_id for event in state.get("events", []) if event.edge_id],
            node_ids=[event.node_id for event in state.get("events", []) if event.node_id],
        )
        map_context = apply_runtime_map_overrides(
            bundle.context,
            state.get("runtime_overrides"),
        )
        return {
            "map_context": map_context,
            "graph_nodes": bundle.graph_nodes,
            "graph_node_types": bundle.graph_node_types,
            "graph_arcs": bundle.graph_arcs,
            "completed_context_nodes": ["map_context"],
            **trace_update("map_context"),
        }
    except Exception as exc:
        return error_update(stage="map_context", code="map_context_failed", message=str(exc))

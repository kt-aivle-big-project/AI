"""Finalize a single immutable context snapshot for the workflow."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from app.core.node_observability import observe_node
from app.domain.schemas import ContextSnapshot
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.repositories.json_repository import get_repository


@observe_node(
    "context_snapshot_finalize",
    purpose="Inventory·Map·Robot 조회 결과를 하나의 버전 고정 Snapshot으로 확정",
)
def context_snapshot_finalize_node(state: LaroGraphState) -> dict:
    """Create one snapshot identifier and reject missing selected contexts."""

    try:
        plan = state["orchestration_plan"]
        state_key = {
            "inventory_context": "inventory_context",
            "map_context": "map_context",
            "robot_runtime": "robot_context",
        }
        required_context_names = list(plan.selected_context_nodes)
        if plan.route in {"RULE_MISSION_PIPELINE", "AGENT_MISSION_PIPELINE"}:
            required_context_names = ["inventory_context", "map_context", "robot_runtime"]
        missing = [name for name in required_context_names if state.get(state_key[name]) is None]
        if missing:
            raise ValueError(f"Required context tools did not produce state: {missing}")
        versions = get_repository().versions
        warehouse_id = str(state.get("warehouse_id") or "WH-001")
        simulation_id = str(state["simulation_id"])
        seed = (
            f"{warehouse_id}:{simulation_id}:{versions['graph_version']}:"
            f"{versions['inventory_version']}:{versions['runtime_version']}"
        )
        snapshot = ContextSnapshot(
            warehouse_id=warehouse_id,
            simulation_id=simulation_id,
            snapshot_id=f"SNAP-{hashlib.sha256(seed.encode()).hexdigest()[:12]}",
            captured_at=datetime.now(timezone.utc).isoformat(),
            graph_version=versions["graph_version"],
            inventory_version=versions["inventory_version"],
            runtime_version=versions["runtime_version"],
        )
        return {"context_snapshot": snapshot, **trace_update("context_snapshot_finalize")}
    except Exception as exc:
        return error_update(stage="context_snapshot_finalize", code="context_snapshot_failed", message=str(exc))

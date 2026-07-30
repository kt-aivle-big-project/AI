"""Pure policy-materialization graph node."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import ContextSnapshot, InventoryContext, MapContext, MissionSpec, RobotRuntimeContext
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.policies.mission_policy import MissionPolicyService


@observe_node(
    "policy_validation",
    purpose="고정된 Context Snapshot만 사용해 가용 랙·로봇·경로 정책을 실행 Task로 구체화",
)
def policy_validation_node(state: LaroGraphState) -> dict:
    """Validate the mission without re-reading JSON, database, or runtime sources."""

    try:
        mission_value = state.get("effective_mission_spec") or state.get("mission_spec")
        if mission_value is None:
            raise ValueError("No effective or submitted MissionSpec is available")
        mission = mission_value if isinstance(mission_value, MissionSpec) else MissionSpec.model_validate(mission_value)
        snapshot = model_from_state(state, "context_snapshot", ContextSnapshot)
        inventory = state.get("inventory_context")
        if inventory is None:
            inventory = InventoryContext(
                query_scope={
                    "mode": "warehouse_overview",
                    "warehouse_id": "WH001",
                    "reason": "Inventory context was not selected for this non-inventory mission.",
                },
                inventory_summary="Inventory context was not selected.",
            )
        elif not isinstance(inventory, InventoryContext):
            inventory = InventoryContext.model_validate(inventory)
        map_context = model_from_state(state, "map_context", MapContext)
        robots = model_from_state(state, "robot_context", RobotRuntimeContext)
        if not state.get("graph_arcs"):
            raise ValueError("graph_arcs are missing from the context snapshot")
        result = MissionPolicyService().validate(
            mission=mission,
            snapshot=snapshot,
            inventory=inventory,
            map_context=map_context,
            robots=robots,
            graph_arcs=list(state["graph_arcs"]),
            events=list(state.get("events", [])),
        )
        update = {
            "effective_mission_spec": mission.model_copy(update={"map_constraints": map_context.map_constraints}),
            "policy_validation": result,
            **trace_update("policy_validation"),
        }
        if result.status == "repairable":
            update["replan_feedback"] = [value.message for value in result.violations]
            update["retry_count"] = int(state.get("retry_count", 0)) + 1
        return update
    except Exception as exc:
        return error_update(stage="policy_validation", code="policy_validation_failed", message=str(exc))

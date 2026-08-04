"""Robot runtime context node."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import WorkflowHoldResult
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.services.context_service import WarehouseContextService, apply_runtime_overrides


@observe_node(
    "robot_runtime",
    purpose="로봇 상태를 한 번 조회하고 상태·배터리·용량 기준으로 후보를 분류",
)
def robot_runtime_node(state: LaroGraphState) -> dict:
    """Build a warehouse/session-scoped robot snapshot and apply trusted overrides."""

    try:
        context = WarehouseContextService().build_robot_context(required_capacity=1)
        context = apply_runtime_overrides(context, state.get("runtime_overrides"))
        if not context.robots:
            return error_update(
                stage="robot_runtime",
                code="missing_simulation_runtime",
                message=(
                    f"No robot runtime is available for warehouse "
                    f"{context.warehouse_id} / simulation {context.simulation_id}. "
                    "Bootstrap the scenario runtime or supply a COMPLETE runtime_snapshot."
                ),
            )
        if not context.candidate_robot_ids:
            return {
                "robot_context": context,
                "completed_context_nodes": ["robot_runtime"],
                "workflow_hold": WorkflowHoldResult(
                    reason_code="ALL_CANDIDATES_UNAVAILABLE",
                    message=(
                        "All known robots are unavailable because of status, "
                        "battery, position, or capacity constraints."
                    ),
                    required_actions=[
                        "Restore at least one eligible robot or provide an updated runtime snapshot."
                    ],
                ),
                **trace_update("robot_runtime"),
            }
        return {
            "robot_context": context,
            "completed_context_nodes": ["robot_runtime"],
            **trace_update("robot_runtime"),
        }
    except Exception as exc:
        return error_update(
            stage="robot_runtime", code="robot_context_failed", message=str(exc)
        )

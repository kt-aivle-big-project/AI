"""Build the compact front-end simulation plan after MAPF validation."""
from __future__ import annotations

from types import SimpleNamespace

from app.core.node_observability import observe_node
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.services.simulation_plan_service import SimulationPlanBuilder


@observe_node(
    "simulation_plan_builder",
    purpose="검증된 MAPF MOVE·WAIT·SERVICE 시간표를 프론트 실행용 SimulationPlan으로 변환",
)
def simulation_plan_builder_node(state: LaroGraphState) -> dict:
    """Build without re-running optimization or changing the validated route."""

    try:
        view = SimpleNamespace(
            warehouse_id=state.get("warehouse_id", "WH-001"),
            simulation_id=state["simulation_id"],
            status=state.get("workflow_status"),
            traffic_schedule=state.get("traffic_schedule"),
            robot_context=state.get("robot_context"),
            normalized_request=state.get("normalized_request"),
            optimization_request=state.get("optimization_request"),
            goods_to_person_compilation=state.get("goods_to_person_compilation"),
            execution_optimizer_result=state.get("execution_optimizer_result"),
            optimizer_result=state.get("optimizer_result"),
            execution_payload=state.get("execution_payload"),
            cuopt_payload=state.get("cuopt_payload"),
            inventory_context=state.get("inventory_context"),
            context_snapshot=state.get("context_snapshot"),
        )
        plan = SimulationPlanBuilder().build(view)  # type: ignore[arg-type]
        if plan is None:
            raise ValueError("A validated traffic schedule is required for a simulation plan.")
        return {"simulation_plan": plan, **trace_update("simulation_plan_builder")}
    except Exception as exc:
        return error_update(
            stage="simulation_plan_builder",
            code="simulation_plan_build_failed",
            message=str(exc),
        )

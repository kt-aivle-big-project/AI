"""Deterministic execution-recovery planning node."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import MapContext, MissionSpec, RobotExecutionContext, RobotRuntimeContext, TaskRequest
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.policies.recovery_policy import RecoveryPolicyService
from app.repositories.json_repository import get_repository


@observe_node(
    "recovery_planner",
    purpose="작업 중 로봇의 적재·위치 상태로 계속 배송·반환·안전 출구 대기 결정을 생성",
)
def recovery_planner_node(state: LaroGraphState) -> dict:
    """Create a bounded recovery mission from current execution facts."""

    try:
        robot_id = next(
            (event.robot_id for event in state.get("events", []) if event.type == "robot_recovery_requested"),
            None,
        )
        if not robot_id:
            raise ValueError("robot_recovery_requested event is missing robot_id")
        robot_context = model_from_state(state, "robot_context", RobotRuntimeContext)
        map_context = model_from_state(state, "map_context", MapContext)
        robot = next((value for value in robot_context.robots if value.robot_id == robot_id), None)
        if robot is None:
            raise ValueError(f"Robot {robot_id} is absent from RobotRuntimeContext")
        raw = get_repository().robots[robot_id]
        execution = RobotExecutionContext(
            robot_id=robot_id,
            task_phase=str(raw.get("task_phase", "WAITING")),
            load_state=robot.load_state,
            quantity=int(raw.get("load_quantity", 0)),
            source_node=raw.get("source_node"),
            destination_node=raw.get("destination_node"),
            current_node=robot.current_node,
            current_edge=robot.current_edge,
            previous_safe_node=raw.get("previous_safe_node"),
            next_safe_node=raw.get("next_safe_node"),
        )
        decision = RecoveryPolicyService().decide(
            execution=execution,
            map_context=map_context,
            buffer_nodes=get_repository().buffer_nodes(),
        )
        if decision.action in {"HUMAN_REVIEW", "EMERGENCY_STOP"} or not decision.target_node:
            return {
                "human_review": {
                    "reason": "Robot recovery requires operator intervention.",
                    "details": [decision.reason],
                },
                "workflow_status": "human_review",
                **trace_update("recovery_planner"),
            }
        quantity = max(execution.quantity, 1)
        mission = MissionSpec(
            mission_type="robot_recovery",
            mission_priority="high",
            reason=[decision.reason],
            task_requests=[
                TaskRequest(
                    request_type="loaded_transfer",
                    requested_qty=quantity,
                    delivery_node=decision.target_node,
                    priority="high",
                    fixed_robot_id=robot_id,
                )
            ],
            map_constraints=map_context.map_constraints,
            warnings=[f"Recovery action: {decision.action}"],
            mission_source="recovery_policy",
        )
        return {"effective_mission_spec": mission, **trace_update("recovery_planner")}
    except Exception as exc:
        return error_update(stage="recovery_planner", code="recovery_planning_failed", message=str(exc))

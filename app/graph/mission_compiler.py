"""Deterministic mission compiler nodes for Rule and LLM Agent paths."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.domain.schemas import InventoryContext, MapContext, MissionIntent
from app.graph.node_support import error_update, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.policies.mission_compiler import compile_mission_intent, compile_structured_events


@observe_node(
    "deterministic_mission_compiler",
    purpose="알려진 구조화 이벤트를 LLM 없이 표준 MissionSpec으로 변환",
)
def deterministic_mission_compiler_node(state: LaroGraphState) -> dict:
    """Compile routine structured warehouse events into a MissionSpec."""

    try:
        inventory = model_from_state(state, "inventory_context", InventoryContext)
        map_context = model_from_state(state, "map_context", MapContext)
        mission = compile_structured_events(
            events=list(state.get("events", [])),
            inventory=inventory,
            map_context=map_context,
        )
        return {"effective_mission_spec": mission, **trace_update("deterministic_mission_compiler")}
    except Exception as exc:
        return error_update(
            stage="deterministic_mission_compiler",
            code="deterministic_mission_compile_failed",
            message=str(exc),
        )


@observe_node(
    "mission_intent_compiler",
    purpose="LLM Agent의 고수준 MissionIntent를 검증 가능한 MissionSpec으로 결정론적으로 변환",
)
def mission_intent_compiler_node(state: LaroGraphState) -> dict:
    """Compile the grounded agent intent without allowing physical resource invention."""

    try:
        intent = model_from_state(state, "mission_intent", MissionIntent)
        inventory = model_from_state(state, "inventory_context", InventoryContext)
        map_context = model_from_state(state, "map_context", MapContext)
        mission = compile_mission_intent(
            intent=intent,
            inventory=inventory,
            map_context=map_context,
            revision=int(state.get("retry_count", 0)) + 1,
        )
        return {"effective_mission_spec": mission, **trace_update("mission_intent_compiler")}
    except Exception as exc:
        return error_update(
            stage="mission_intent_compiler",
            code="mission_intent_compile_failed",
            message=str(exc),
            retryable=True,
        )

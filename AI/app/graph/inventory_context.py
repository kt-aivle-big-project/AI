"""Scoped inventory context node using the reusable read-only context service."""
from __future__ import annotations

from app.core.node_observability import observe_node
from app.graph.node_support import error_update, trace_update
from app.graph.state import LaroGraphState
from app.services.context_service import WarehouseContextService


@observe_node(
    "inventory_context",
    purpose="주문·품목 관련 랙 재고 또는 창고 집계를 한 번 조회해 State Snapshot에 저장",
)
def inventory_context_node(state: LaroGraphState) -> dict:
    """Build the inventory context selected by the deterministic or agent path."""

    try:
        order_ids = [
            event.order_id
            for event in state.get("events", [])
            if event.type == "new_order" and event.order_id
        ]
        inbound_ids = [
            event.inbound_id or event.payload.get("inbound_id") or event.payload.get("handling_unit_id")
            for event in state.get("events", [])
            if event.type == "inbound_item_arrived"
            and (event.inbound_id or event.payload.get("inbound_id") or event.payload.get("handling_unit_id"))
        ]
        normalized = state.get("normalized_request")
        if normalized is not None:
            order_ids.extend(
                operation.operation_id
                for operation in normalized.operations
                if operation.operation_type == "OUTBOUND_ORDER"
                and operation.operation_id not in order_ids
            )
            inbound_ids.extend(
                operation.operation_id
                for operation in normalized.operations
                if operation.operation_type == "INBOUND_ITEM"
                and operation.operation_id not in inbound_ids
            )
        mission = state.get("mission_spec") or state.get("effective_mission_spec")
        if mission is not None:
            order_ids.extend(
                task.order_id
                for task in mission.task_requests
                if task.order_id and task.order_id not in order_ids
            )
        intent = state.get("mission_intent")
        if intent is not None:
            order_ids.extend(
                operation.target_id
                for operation in intent.operations
                if operation.operation_type == "FULFILL_OUTBOUND_ORDER"
                and operation.target_id not in order_ids
            )
        context = WarehouseContextService().build_inventory_context(order_ids=order_ids, inbound_ids=inbound_ids)
        return {
            "inventory_context": context,
            "completed_context_nodes": ["inventory_context"],
            **trace_update("inventory_context"),
        }
    except Exception as exc:
        return error_update(stage="inventory_context", code="inventory_context_failed", message=str(exc))

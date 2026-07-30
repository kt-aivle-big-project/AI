"""Deterministic compilers from structured events or LLM MissionIntent to MissionSpec."""
from __future__ import annotations

from app.domain.schemas import (
    EventInput,
    InventoryContext,
    MapContext,
    MissionIntent,
    MissionSpec,
    Priority,
    TaskRequest,
)

_PRIORITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _authoritative_priority(order_priority: Priority, intent_priority: Priority) -> tuple[Priority, str | None]:
    """Return the effective task priority with the order record as the source of truth.

    The LLM may only raise urgency (asymmetric rule): an operator saying
    "handle this urgently" is honored and logged, but the LLM can never
    demote a high-priority order below its recorded priority.
    """

    if _PRIORITY_RANK[intent_priority] > _PRIORITY_RANK[order_priority]:
        return intent_priority, (
            f"LLM intent raised priority {order_priority} -> {intent_priority}; order record remains authoritative baseline."
        )
    if intent_priority != order_priority:
        return order_priority, (
            f"LLM intent priority {intent_priority} was ignored; order priority {order_priority} is authoritative."
        )
    return order_priority, None


def compile_structured_events(
    *,
    events: list[EventInput],
    inventory: InventoryContext,
    map_context: MapContext,
) -> MissionSpec:
    """Compile known structured events into a deterministic mission."""

    order_ids = [event.order_id for event in events if event.type == "new_order" and event.order_id]
    tasks: list[TaskRequest] = []
    reasons: list[str] = []
    for order_id in order_ids:
        need = next((value for value in inventory.task_needs if value.order_id == order_id), None)
        if need is None:
            continue
        tasks.append(
            TaskRequest(
                request_type="outbound_pick",
                order_id=need.order_id,
                item_id=need.item_id,
                requested_qty=need.required_qty,
                delivery_node=need.delivery_node,
                priority=need.priority,
            )
        )
        reasons.append(
            f"Structured new_order {need.order_id} was compiled to an outbound task for "
            f"{need.required_qty} unit(s) of {need.item_id}."
        )
    priority: Priority = max(
        (task.priority for task in tasks),
        key={"low": 0, "medium": 1, "high": 2}.get,
        default="medium",
    )
    return MissionSpec(
        mission_type="order_fulfillment" if tasks else "no_op",
        mission_priority=priority,
        reason=reasons or ["No structured mission task was materialized."],
        task_requests=tasks,
        map_constraints=map_context.map_constraints,
        optional_order_ids=[],
        objective_profile="MIN_COMPLETION_TIME",
        warnings=[],
        mission_source="rule_compiler",
        revision=1,
    )


def compile_mission_intent(
    *,
    intent: MissionIntent,
    inventory: InventoryContext,
    map_context: MapContext,
    revision: int = 1,
) -> MissionSpec:
    """Compile grounded LLM intent into an executable mission contract."""

    tasks: list[TaskRequest] = []
    reasons: list[str] = []
    priority_warnings: list[str] = []
    for operation in intent.operations:
        if operation.operation_type == "FULFILL_OUTBOUND_ORDER":
            need = next(
                (value for value in inventory.task_needs if value.order_id == operation.target_id),
                None,
            )
            if need is None:
                raise ValueError(
                    f"MissionIntent references order {operation.target_id} without an InventoryContext record."
                )
            effective_priority, priority_note = _authoritative_priority(need.priority, operation.priority)
            tasks.append(
                TaskRequest(
                    request_type="outbound_pick",
                    order_id=need.order_id,
                    item_id=need.item_id,
                    requested_qty=need.required_qty,
                    delivery_node=need.delivery_node,
                    priority=effective_priority,
                )
            )
            reasons.append(operation.reason)
            if priority_note is not None:
                priority_warnings.append(priority_note)
        elif operation.operation_type == "REQUEST_HUMAN_REVIEW":
            raise ValueError("REQUEST_HUMAN_REVIEW must terminate before MissionSpec compilation.")
        elif operation.operation_type in {
            "DEFER_OPERATION",
            "KEEP_ROBOT_CHARGING",
            "CONTINUE_ACTIVE_TASK",
            "PUTAWAY_INBOUND_ITEM",
        }:
            reasons.append(
                f"Non-motion intent {operation.operation_type} for {operation.target_id}: {operation.reason}"
            )
        else:
            raise ValueError(
                f"Operation {operation.operation_type} is recognized but not materialized in the current PoC."
            )
    handled_targets = {operation.target_id for operation in intent.operations}
    coverage_warnings = [
        (
            f"uncovered_pending_order: {need.order_id} ({need.required_qty}x {need.item_id}) "
            "is pending in InventoryContext but has no operation in the MissionIntent."
        )
        for need in inventory.task_needs
        if need.order_id not in handled_targets
    ]
    priority: Priority = max(
        (task.priority for task in tasks),
        key={"low": 0, "medium": 1, "high": 2}.get,
        default="medium",
    )
    return MissionSpec(
        mission_type="order_fulfillment" if tasks else "no_op",
        mission_priority=priority,
        reason=reasons or [intent.mission_goal],
        task_requests=tasks,
        map_constraints=map_context.map_constraints,
        excluded_robot_ids=list(dict.fromkeys(intent.excluded_robot_ids)),
        optional_order_ids=list(dict.fromkeys(intent.optional_operation_ids)),
        objective_profile=intent.objective_profile,
        max_edge_wait_ms=intent.max_edge_wait_ms,
        soft_avoid_edge_ids=list(dict.fromkeys(intent.soft_avoid_edge_ids)),
        warnings=[*intent.assumptions, *priority_warnings, *coverage_warnings],
        mission_source="llm_agent",
        revision=revision,
    )

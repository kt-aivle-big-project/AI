from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.models import NaturalLanguageCommand, RobotEvent
from app.planning.graph import run_planning
from app.services.container import get_services
from app.services.event_impact import analyze_event_impact
from app.config import get_settings
from app.services.robot_gateway import RobotGateway
from app.services.robot_adapter import RobotAdapter
from app.services.schedule_dispatcher import ready_only_plan_payload
from app.services.event_safety import StaleExecutionEventError
from app.state import ExecutionState


SQL_COMMIT_EVENTS = {
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "INBOUND_AVAILABLE",
}


ANOMALY_EVENTS = {
    "ROBOT_DELAYED",
    "ROBOT_FAILED",
    "LOW_BATTERY",
    "PATH_BLOCKED",
    "PATH_DEVIATED",
    "TASK_FAILED",
}


def update_live_state_node(state: ExecutionState) -> dict[str, Any]:
    event = RobotEvent.model_validate(state["event"])
    services = get_services()
    try:
        if event.execution_context == "REAL":
            # PostgreSQL owns task/inventory lifecycle transitions. Defer every
            # SQL-backed event so a failed transaction cannot leave Redis ahead.
            if event.event_type in SQL_COMMIT_EVENTS:
                return {
                    "redis_updated": False,
                    "live_update_deferred": True,
                    "final_status": "SQL_COMMIT_PENDING",
                }
            update_result = services.redis.update_from_event(event) or {}
            return {
                "redis_updated": True,
                "event_ordering": update_result.get("event_ordering"),
                "final_status": "LIVE_UPDATED",
            }

        before_state = services.redis.simulation_snapshot(event.simulation_id)
        previous_watermark = (
            services.redis.get_event_watermark(event)
            if hasattr(services.redis, "get_event_watermark")
            else None
        )
        current_state = services.redis.update_simulation_from_event(event)
        try:
            checkpoint_result = services.postgres.update_simulation_checkpoint(
                event,
                current_state,
                str(current_state["checkpoint"]),
            )
        except Exception as checkpoint_exc:
            rollback = None
            if hasattr(services.redis, "restore_simulation_snapshot"):
                rollback = services.redis.restore_simulation_snapshot(
                    str(event.simulation_id),
                    before_state,
                    event=event,
                    previous_watermark=previous_watermark,
                    reason="POSTGRES_CHECKPOINT_FAILED",
                )
            return {
                "redis_updated": False,
                "sql_committed": False,
                "simulation_state_rollback": rollback,
                "recovery_required": True,
                "retryable": True,
                "final_status": "SIMULATION_CHECKPOINT_FAILED",
                "errors": [str(checkpoint_exc)],
            }
        return {
            "redis_updated": True,
            "sql_committed": True,
            "commit_result": checkpoint_result,
            "simulation_current_state": current_state,
            "event_ordering": current_state.get("event_ordering"),
            "stream_id": current_state["checkpoint"],
            "final_status": (
                "SIMULATION_COMPLETED"
                if event.event_type == "TASK_COMPLETED"
                else "SIMULATION_UPDATED"
            ),
        }
    except StaleExecutionEventError as exc:
        return {
            "redis_updated": False,
            "sql_committed": False,
            "stale_event_ignored": True,
            "event_ordering": exc.evidence,
            "final_status": "STALE_EVENT_IGNORED",
        }
    except Exception as exc:
        return {
            "redis_updated": False,
            "recovery_required": True,
            "retryable": True,
            "final_status": "LIVE_UPDATE_FAILED",
            "errors": [str(exc)],
        }


def commit_completion_node(state: ExecutionState) -> dict[str, Any]:
    event = RobotEvent.model_validate(state["event"])
    try:
        if event.execution_context == "REAL":
            redis_repository = get_services().redis
            event_ordering = (
                redis_repository.validate_event_order(event)
                if hasattr(redis_repository, "validate_event_order")
                else None
            )
            if (
                event.event_type == "TASK_COMPLETED"
                and hasattr(redis_repository, "list_inventory_reservations")
            ):
                if not event.inventory_deltas:
                    raise ValueError("OUTBOUND_COMPLETION_INVENTORY_DELTAS_REQUIRED")
                active_rows = redis_repository.list_inventory_reservations(
                    event.warehouse_id,
                    scope="ACTIVE_PLAN",
                    statuses={"RESERVED"},
                )
                matching = [
                    row
                    for row in active_rows
                    if str(row.get("work_id")) == str(event.work_id)
                ]
                if not matching:
                    raise ValueError("ACTIVE_PLAN_RESERVATION_NOT_FOUND")
                reserved_by_lot: dict[str, int] = {}
                reservation_plan_versions = {
                    str(row.get("plan_version"))
                    for row in matching
                    if row.get("plan_version")
                }
                for row in matching:
                    for allocation in row.get("lot_allocations", []):
                        key = str(allocation.get("warehouse_item_id"))
                        reserved_by_lot[key] = reserved_by_lot.get(key, 0) + int(
                            allocation.get("quantity_boxes") or 0
                        )
                requested_by_lot: dict[str, int] = {}
                for delta in event.inventory_deltas:
                    if delta.quantity_delta >= 0:
                        raise ValueError("OUTBOUND_COMPLETION_DELTA_MUST_BE_NEGATIVE")
                    requested_by_lot[delta.warehouse_item_id] = (
                        requested_by_lot.get(delta.warehouse_item_id, 0)
                        + abs(delta.quantity_delta)
                    )
                if requested_by_lot != reserved_by_lot:
                    raise ValueError("OUTBOUND_COMPLETION_RESERVATION_MISMATCH")
                requested_plan_version = event.payload.get("plan_version")
                if (
                    requested_plan_version
                    and reservation_plan_versions
                    and str(requested_plan_version) not in reservation_plan_versions
                ):
                    raise ValueError("PLAN_VERSION_MISMATCH")
            if event.event_type == "TASK_STARTED":
                result = get_services().postgres.record_task_started(event)
                status = "EXECUTING"
            elif event.event_type == "TASK_FAILED":
                result = get_services().postgres.commit_failure(event)
                status = "FAILED"
            elif event.event_type == "INBOUND_AVAILABLE":
                result = get_services().postgres.commit_inbound_available(event)
                status = "AVAILABLE"
            else:
                result = get_services().postgres.commit_completion(event)
                status = "COMPLETED"
        else:
            current_state = state.get("simulation_current_state")
            if current_state is None:
                current_state = get_services().redis.simulation_snapshot(
                    event.simulation_id
                )
            result = get_services().postgres.update_simulation_checkpoint(
                event,
                current_state,
                str(current_state["checkpoint"]),
            )
            status = "SIMULATION_COMPLETED"
        reservation_updates: list[dict[str, Any]] = []
        reservation_warning: str | None = None
        redis_updated = state.get("redis_updated") is True
        redis_reconciled = False
        event_ordering = locals().get("event_ordering")
        if event.execution_context == "REAL":
            redis_repository = get_services().redis
            idempotent_replay = bool(result.get("idempotent_replay"))
            try:
                if event.work_id and not idempotent_replay:
                    if event.event_type == "TASK_COMPLETED":
                        if event.inventory_deltas and hasattr(
                            redis_repository, "consume_inventory_reservations"
                        ):
                            reservation_updates = (
                                redis_repository.consume_inventory_reservations(
                                    event.warehouse_id,
                                    work_id=event.work_id,
                                    inventory_deltas=[
                                        row.model_dump(mode="json")
                                        for row in event.inventory_deltas
                                    ],
                                )
                            )
                        elif hasattr(redis_repository, "update_inventory_reservations"):
                            reservation_updates = (
                                redis_repository.update_inventory_reservations(
                                    event.warehouse_id,
                                    work_id=event.work_id,
                                    from_statuses={"RESERVED"},
                                    status="CONSUMED",
                                )
                            )
                    elif event.event_type == "TASK_FAILED" and hasattr(
                        redis_repository, "update_inventory_reservations"
                    ):
                        reservation_updates = (
                            redis_repository.update_inventory_reservations(
                                event.warehouse_id,
                                work_id=event.work_id,
                                from_statuses={"RESERVED"},
                                status="RELEASED",
                            )
                        )

                # Apply or heal Redis only after PostgreSQL committed. This also
                # reconciles a prior SQL-success/Redis-failure on idempotent replay.
                if hasattr(redis_repository, "update_from_event"):
                    redis_result = redis_repository.update_from_event(event) or {}
                    event_ordering = redis_result.get("event_ordering") or event_ordering
                    redis_updated = True
                    redis_reconciled = idempotent_replay and not bool(
                        redis_result.get("duplicate")
                    )
            except StaleExecutionEventError as exc:
                # SQL did not run for stale events because validate_event_order is
                # called before the transaction. This branch is only a race guard.
                return {
                    "sql_committed": False,
                    "redis_updated": False,
                    "stale_event_ignored": True,
                    "event_ordering": exc.evidence,
                    "final_status": "STALE_EVENT_IGNORED",
                }
            except Exception as reservation_exc:
                reservation_warning = str(reservation_exc)
        response = {
            "sql_committed": True,
            "redis_updated": redis_updated,
            "redis_reconciled": redis_reconciled,
            "event_ordering": event_ordering,
            "final_status": status,
            "commit_result": result,
            "inventory_reservation_updates": reservation_updates,
            "redis_reservation_consumed": bool(reservation_updates),
            "outbound_order_completion": result.get("outbound_order_completion"),
        }
        if reservation_warning:
            response["errors"] = [
                "SQL 반영은 완료됐으나 Redis 상태 조정에 실패했습니다: "
                + reservation_warning
            ]
            response["recovery_required"] = True
            response["retryable"] = True
        return response
    except StaleExecutionEventError as exc:
        return {
            "sql_committed": False,
            "redis_updated": False,
            "stale_event_ignored": True,
            "event_ordering": exc.evidence,
            "final_status": "STALE_EVENT_IGNORED",
        }
    except Exception as exc:
        explicit_code = getattr(exc, "code", None)
        error_code = explicit_code or str(exc)
        validation_failed = bool(explicit_code) or (
            isinstance(exc, ValueError)
            and str(error_code).isupper()
            and "_" in str(error_code)
        )
        return {
            "sql_committed": False,
            "redis_updated": False,
            "recovery_required": not validation_failed,
            "retryable": not validation_failed,
            "final_status": (
                "VALIDATION_FAILED" if validation_failed else "COMMIT_FAILED"
            ),
            "validation_failed": validation_failed,
            "errors": [str(error_code)],
        }


def transition_schedule_node(state: ExecutionState) -> dict[str, Any]:
    event = RobotEvent.model_validate(state["event"])
    result = state.get("commit_result", {})
    if result.get("idempotent_replay"):
        return {
            "schedule_transition": {
                "ready": [], "waiting": [], "blocked": [], "duplicate": True
            },
            "final_status": state.get("final_status", "COMPLETED"),
            "trace": [
                {
                    "node": "successor_transition_duplicate",
                    "event_id": event.event_id,
                    "work_id": event.work_id,
                }
            ],
        }
    repository = get_services().postgres
    if not hasattr(repository, "transition_successors"):
        return {
            "schedule_transition": {"ready": [], "waiting": [], "blocked": []},
            "final_status": state.get("final_status", "COMPLETED"),
        }
    try:
        transition = repository.transition_successors(
            str(event.work_id),
            occurred_at=event.occurred_at,
            predecessor_failed=event.event_type == "TASK_FAILED",
        )
        return {
            "schedule_transition": transition,
            "final_status": (
                "SUCCESSOR_BLOCKED"
                if transition.get("blocked")
                else "SUCCESSOR_UNLOCKED"
                if transition.get("ready")
                else state.get("final_status", "COMPLETED")
            ),
            "trace": [
                *(
                    [
                        {
                            "node": "successor_unlocked",
                            "event_id": event.event_id,
                            "work_id": event.work_id,
                            "successor_work_ids": transition.get("ready", []),
                        }
                    ]
                    if transition.get("ready")
                    else []
                ),
                *(
                    [
                        {
                            "node": "successor_blocked",
                            "event_id": event.event_id,
                            "work_id": event.work_id,
                            "successor_work_ids": transition.get("blocked", []),
                        }
                    ]
                    if transition.get("blocked")
                    else []
                ),
            ],
        }
    except Exception as exc:
        return {
            "schedule_transition": {},
            "final_status": "SCHEDULE_TRANSITION_FAILED",
            "errors": [str(exc)],
        }


def dispatch_successors_node(state: ExecutionState) -> dict[str, Any]:
    event = RobotEvent.model_validate(state["event"])
    ready_work_ids = set(state.get("schedule_transition", {}).get("ready", []))
    if not ready_work_ids:
        return {"successor_dispatch_result": {}, "final_status": state.get("final_status")}
    settings = get_settings()
    if not settings.robot_gateway_url:
        return {
            "successor_dispatch_result": {
                "status": "READY_NOT_DISPATCHED",
                "ready_work_ids": sorted(ready_work_ids),
            },
            "final_status": "SUCCESSOR_READY",
        }
    live = get_services().redis.live_snapshot(event.warehouse_id)
    active_plan = live.get("active_plan") or {}
    ready_task_ids = [
        str(row.get("task_id"))
        for row in active_plan.get("cuopt_plan", {}).get("scheduled_tasks", [])
        if str(row.get("work_id")) in ready_work_ids
    ]
    if not ready_task_ids:
        return {
            "successor_dispatch_result": {
                "status": "READY_TASK_NOT_IN_ACTIVE_PLAN",
                "ready_work_ids": sorted(ready_work_ids),
            },
            "final_status": "SUCCESSOR_READY",
        }
    plan_version = str(live.get("active_plan_version") or active_plan.get("plan_version"))
    payload = ready_only_plan_payload(active_plan, ready_task_ids)
    adapter = RobotAdapter(time_step_seconds=settings.time_step_seconds)
    batches, validation = adapter.adapt(plan_version, payload)
    if not validation["valid"] or not batches:
        return {
            "successor_dispatch_result": {
                "status": "ROBOT_ADAPTER_VALIDATION_FAILED",
                "adapter_validation": validation,
            },
            "final_status": "SUCCESSOR_DISPATCH_FAILED",
        }
    gateway = RobotGateway(
        settings.robot_gateway_url, settings.request_timeout_seconds
    ).dispatch(plan_version, [batch.model_dump(mode="json") for batch in batches])
    return {
        "successor_dispatch_result": {
            **gateway,
            "dispatched_robot_count": len(batches),
            "dispatched_command_count": sum(batch.command_count for batch in batches),
            "robot_command_batches": [batch.model_dump(mode="json") for batch in batches],
            "adapter_validation": validation,
        },
        "final_status": "SUCCESSOR_DISPATCHED",
    }
def emit_replan_node(state: ExecutionState) -> dict[str, Any]:
    event = RobotEvent.model_validate(state["event"])
    stream_id = (
        get_services().redis.emit_replan_required(event)
        if event.execution_context == "REAL"
        else state.get("stream_id", "")
    )
    command = NaturalLanguageCommand(
        warehouse_id=event.warehouse_id,
        text=(
            f"운영 이벤트 {event.event_type}가 발생했습니다. "
            f"로봇 {event.robot_id}, 작업 {event.task_id or event.work_id}의 "
            "완료 구간과 freeze horizon을 유지하면서 필요한 범위만 재계획하세요."
        ),
        requested_execution_mode=(
            "SIMULATE_ONLY"
        ),
        simulation_id=event.simulation_id,
        source="SYSTEM_EVENT",
    )
    return {
        "replan_command": command.model_dump(mode="json"),
        "final_status": "REPLAN_REQUIRED",
        "stream_id": stream_id,
    }


def analyze_impact_node(state: ExecutionState) -> dict[str, Any]:
    event = RobotEvent.model_validate(state["event"])
    try:
        impact = analyze_event_impact(event, get_services())
        return {
            "impact_analysis": impact.model_dump(mode="json"),
            "final_status": "EVENT_IMPACT_ANALYZED",
        }
    except Exception as exc:
        return {
            "impact_analysis": {},
            "final_status": "EVENT_IMPACT_ANALYSIS_FAILED",
            "errors": [str(exc)],
        }


def after_live_update(
    state: ExecutionState,
) -> Literal["commit", "impact", "replan", "end"]:
    event_type = state["event"]["event_type"]
    if state.get("live_update_deferred"):
        return "commit"
    if not state.get("redis_updated"):
        return "end"
    if state["event"].get("execution_context", "REAL") == "SIMULATION":
        if event_type in ANOMALY_EVENTS:
            return "impact" if state.get("analyze_impact") else "replan"
        return "end"
    if event_type in {
        "TASK_STARTED",
        "TASK_COMPLETED",
        "TASK_FAILED",
        "INBOUND_AVAILABLE",
    }:
        return "commit"
    if event_type in ANOMALY_EVENTS:
        return "impact" if state.get("analyze_impact") else "replan"
    return "end"


def after_impact(state: ExecutionState) -> Literal["replan", "end"]:
    return "replan" if state.get("impact_analysis") else "end"


def after_commit(state: ExecutionState) -> Literal["transition", "end"]:
    event_type = state["event"]["event_type"]
    if (
        not state.get("sql_committed")
        or event_type not in {"TASK_COMPLETED", "TASK_FAILED"}
    ):
        return "end"
    return "transition"


def after_transition(
    state: ExecutionState,
) -> Literal["dispatch", "dispatch_replan", "replan", "end"]:
    if state["event"]["event_type"] == "TASK_FAILED":
        return (
            "dispatch_replan"
            if state.get("schedule_transition", {}).get("ready")
            else "replan"
        )
    return "dispatch" if state.get("schedule_transition", {}).get("ready") else "end"


builder = StateGraph(ExecutionState)
builder.add_node("update_live", update_live_state_node)
builder.add_node("commit", commit_completion_node)
builder.add_node("impact", analyze_impact_node)
builder.add_node("replan", emit_replan_node)
builder.add_node("transition", transition_schedule_node)
builder.add_node("dispatch_successors", dispatch_successors_node)
builder.add_node("dispatch_independent_successors", dispatch_successors_node)
builder.add_edge(START, "update_live")
builder.add_conditional_edges(
    "update_live",
    after_live_update,
    {"commit": "commit", "impact": "impact", "replan": "replan", "end": END},
)
builder.add_conditional_edges(
    "impact",
    after_impact,
    {"replan": "replan", "end": END},
)
builder.add_conditional_edges(
    "commit",
    after_commit,
    {"transition": "transition", "end": END},
)
builder.add_conditional_edges(
    "transition",
    after_transition,
    {
        "dispatch": "dispatch_successors",
        "dispatch_replan": "dispatch_independent_successors",
        "replan": "replan",
        "end": END,
    },
)
builder.add_edge("dispatch_successors", END)
builder.add_edge("dispatch_independent_successors", "replan")
builder.add_edge("replan", END)
execution_graph = builder.compile()


def handle_robot_event(
    event: RobotEvent,
    auto_replan: bool = False,
    analyze_impact: bool = False,
) -> dict[str, Any]:
    result = execution_graph.invoke(
        {
            "event": event.model_dump(mode="json"),
            "analyze_impact": analyze_impact,
            "errors": [],
            "trace": [],
        }
    )
    if auto_replan and result.get("replan_command"):
        result["planning_response"] = run_planning(
            NaturalLanguageCommand.model_validate(result["replan_command"])
        )
    return result

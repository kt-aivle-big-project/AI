from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
import os
from threading import Lock
import time
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
import httpx
from pydantic import BaseModel, Field, model_validator

from app.models import RobotCommandBatch


class DispatchRequest(BaseModel):
    dispatch_id: str | None = None
    idempotency_key: str | None = None
    payload_fingerprint: str | None = None
    plan_version: str = Field(min_length=1)
    batches: list[RobotCommandBatch] = Field(default_factory=list)
    # Temporary compatibility for already recorded/manual gateway requests.
    plan: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_batches_or_legacy_plan(self) -> "DispatchRequest":
        if not self.batches and self.plan is None:
            raise ValueError("batches is required")
        return self


class CancelDispatchRequest(BaseModel):
    plan_version: str = Field(min_length=1)
    reason: str = Field(min_length=1)


app = FastAPI(title="Mock Robot Gateway", version="1.1.0")

_received_plans: list[dict[str, Any]] = []
_received_by_idempotency: dict[str, dict[str, Any]] = {}
_received_plans_lock = Lock()


def _robot_route_counts(plan: dict[str, Any]) -> dict[str, int]:
    collision_plan = plan.get("collision_plan")
    routes = collision_plan.get("routes", []) if isinstance(collision_plan, dict) else []
    robot_ids = (
        str(route["robot_id"])
        for route in routes
        if isinstance(route, dict) and route.get("robot_id") not in (None, "")
    )
    return dict(sorted(Counter(robot_ids).items()))


def _real_execution_events(plan: dict[str, Any]) -> list[dict[str, Any]]:
    required_tasks = {
        str(task.get("task_id")): task
        for task in plan.get("required_tasks", [])
        if isinstance(task, dict) and task.get("task_id")
    }
    queue: list[tuple[int, int, str, dict[str, Any]]] = []
    for schedule in plan.get("cuopt_plan", {}).get("scheduled_tasks", []):
        task_id = str(schedule["task_id"])
        work_id = schedule.get("work_id")
        robot_id = str(schedule["robot_id"])
        start_step = int(schedule.get("start_time_step") or 0)
        end_step = int(schedule.get("end_time_step") or start_step)
        base = {
            "warehouse_id": int(plan.get("warehouse_id") or 1),
            "robot_id": robot_id,
            "work_id": str(work_id) if work_id is not None else None,
            "task_id": task_id,
            "execution_context": "REAL",
            "simulation_id": None,
        }
        queue.append(
            (
                start_step,
                0,
                task_id,
                {
                    **base,
                    "event_id": str(uuid4()),
                    "event_type": "TASK_STARTED",
                    "node_id": schedule.get("source_node"),
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
        )
        if work_id is not None:
            task = required_tasks.get(task_id, {})
            inventory_deltas = (
                [
                    {
                        "warehouse_item_id": str(
                            allocation["warehouse_item_id"]
                        ),
                        "quantity_delta": -int(allocation["quantity"]),
                    }
                    for allocation in task.get("inventory_allocations", [])
                ]
                if task.get("action") in {"PICK", "MOVE"}
                else []
            )
            queue.append(
                (
                    end_step,
                    2,
                    task_id,
                    {
                        **base,
                        "event_id": str(uuid4()),
                        "event_type": "TASK_COMPLETED",
                        "node_id": schedule.get("target_node"),
                        "inventory_deltas": inventory_deltas,
                        "occurred_at": datetime.now(UTC).isoformat(),
                    },
                )
            )

    for route in plan.get("collision_plan", {}).get("routes", []):
        robot_id = str(route["robot_id"])
        for waypoint in route.get("waypoints", []):
            time_step = int(waypoint.get("time_step") or 0)
            queue.append(
                (
                    time_step,
                    1,
                    robot_id,
                    {
                        "event_id": str(uuid4()),
                        "warehouse_id": int(plan.get("warehouse_id") or 1),
                        "robot_id": robot_id,
                        "event_type": "POSITION_UPDATED",
                        "node_id": waypoint.get("node_id"),
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "execution_context": "REAL",
                        "simulation_id": None,
                    },
                )
            )
    return [row[3] for row in sorted(queue, key=lambda row: row[:3])]


def _real_execution_events_from_batches(
    batches: list[RobotCommandBatch],
) -> list[dict[str, Any]]:
    """Translate standard robot command batches into REAL execution events.

    The production-facing gateway contract sends command batches rather than the
    legacy internal planning payload.  The mock gateway still needs to replay
    realistic callbacks, so it derives one TASK_STARTED/TASK_COMPLETED pair per
    work and aggregates all PICKUP lot allocations into the completion event.
    """

    queue: list[tuple[int, int, str, dict[str, Any]]] = []
    work_commands: dict[str, list[Any]] = {}

    for batch in batches:
        for command in batch.commands:
            # Every standard command carries the robot's authoritative node at
            # that time step.  Replaying it keeps Redis live position in sync.
            queue.append(
                (
                    int(command.time_step),
                    1,
                    f"{command.robot_id}:{command.sequence:08d}",
                    {
                        "event_id": str(uuid4()),
                        "warehouse_id": int(command.warehouse_id),
                        "robot_id": str(command.robot_id),
                        "event_type": "POSITION_UPDATED",
                        "node_id": int(command.node_id),
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "execution_context": "REAL",
                        "simulation_id": None,
                    },
                )
            )
            if command.work_id:
                work_commands.setdefault(str(command.work_id), []).append(command)

    for work_id, commands in sorted(work_commands.items()):
        ordered = sorted(commands, key=lambda row: (row.time_step, row.sequence))
        first = ordered[0]
        last = ordered[-1]
        task_id = next(
            (str(row.task_id) for row in ordered if row.task_id),
            None,
        )
        base = {
            "warehouse_id": int(first.warehouse_id),
            "robot_id": str(first.robot_id),
            "work_id": work_id,
            "task_id": task_id,
            "execution_context": "REAL",
            "simulation_id": None,
        }
        queue.append(
            (
                int(first.time_step),
                0,
                work_id,
                {
                    **base,
                    "event_id": str(uuid4()),
                    "event_type": "TASK_STARTED",
                    "node_id": int(first.node_id),
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "payload": {"plan_version": str(first.plan_version)},
                },
            )
        )

        delta_by_item: dict[str, int] = {}
        for command in ordered:
            if command.action != "PICKUP":
                continue
            for allocation in command.payload.get("lot_allocations", []):
                warehouse_item_id = allocation.get("warehouse_item_id")
                if warehouse_item_id in (None, ""):
                    continue
                quantity = int(
                    allocation.get("quantity_boxes")
                    or allocation.get("quantity")
                    or 0
                )
                if quantity <= 0:
                    continue
                key = str(warehouse_item_id)
                delta_by_item[key] = delta_by_item.get(key, 0) - quantity

        completion = {
            **base,
            "event_id": str(uuid4()),
            "event_type": "TASK_COMPLETED",
            "node_id": int(last.node_id),
            "inventory_deltas": [
                {
                    "warehouse_item_id": warehouse_item_id,
                    "quantity_delta": quantity_delta,
                }
                for warehouse_item_id, quantity_delta in sorted(delta_by_item.items())
            ],
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {"plan_version": str(last.plan_version)},
        }
        queue.append((int(last.time_step), 2, work_id, completion))

    return [row[3] for row in sorted(queue, key=lambda row: row[:3])]


def _send_real_execution_events(
    record: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    event_url = os.getenv(
        "SUPERVISOR_EVENT_URL",
        "http://127.0.0.1:8000/v1/execution/events",
    )
    delay_seconds = max(0.0, float(os.getenv("MOCK_EVENT_DELAY_SECONDS", "0")))
    delivered = 0
    try:
        with httpx.Client(timeout=10.0) as client:
            for event in events:
                response = client.post(event_url, json=event)
                response.raise_for_status()
                delivered += 1
                if delay_seconds:
                    time.sleep(delay_seconds)
        delivery_status = "COMPLETED"
        delivery_error = None
    except Exception as exc:
        delivery_status = "FAILED"
        delivery_error = str(exc)
    with _received_plans_lock:
        record["event_delivery"] = {
            "status": delivery_status,
            "delivered_event_count": delivered,
            "total_event_count": len(events),
            "error": delivery_error,
        }


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "mock-robot-gateway",
    }


@app.post("/dispatch")
def dispatch(
    request: DispatchRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    legacy_plan = request.plan or {}
    route_counts = (
        {batch.robot_id: 1 for batch in request.batches}
        if request.batches
        else _robot_route_counts(legacy_plan)
    )
    auto_execute = os.getenv("MOCK_GATEWAY_AUTO_EXECUTE", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    record = {
        "dispatch_id": request.dispatch_id,
        "idempotency_key": request.idempotency_key,
        "payload_fingerprint": request.payload_fingerprint,
        "plan_version": request.plan_version,
        "command_id": legacy_plan.get("command_id"),
        "received_at": datetime.now(UTC).isoformat(),
        "received_robot_count": len(route_counts),
        "robot_route_counts": route_counts,
        "robot_command_batches": [batch.model_dump(mode="json") for batch in request.batches],
        "plan": deepcopy(legacy_plan) if request.plan is not None else None,
        "auto_execute": auto_execute,
        "status": "ACCEPTED",
    }
    duplicate = False
    with _received_plans_lock:
        if request.idempotency_key:
            existing = _received_by_idempotency.get(request.idempotency_key)
            if existing is not None:
                if (
                    existing.get("payload_fingerprint")
                    != request.payload_fingerprint
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="IDEMPOTENCY_KEY_PAYLOAD_CONFLICT",
                    )
                duplicate = True
                record = existing
            else:
                _received_by_idempotency[request.idempotency_key] = record
                _received_plans.append(record)
        else:
            _received_plans.append(record)
    if auto_execute and not duplicate:
        events = (
            _real_execution_events(legacy_plan)
            if request.plan is not None
            else _real_execution_events_from_batches(request.batches)
        )
        record["generated_event_count"] = len(events)
        record["event_delivery"] = {
            "status": "PENDING",
            "delivered_event_count": 0,
            "total_event_count": len(events),
            "error": None,
        }
        background_tasks.add_task(
            _send_real_execution_events,
            record,
            events,
        )

    response = {
        "accepted": True,
        "status": "DISPATCH_ACCEPTED",
        "plan_version": request.plan_version,
        "received_robot_count": len(route_counts),
        "message": "Mock Robot Gateway가 계획을 정상 수신했습니다.",
    }
    if request.batches:
        response["received_command_count"] = sum(
            batch.command_count for batch in request.batches
        )
    if request.idempotency_key:
        response.update(
            {
                "dispatch_id": request.dispatch_id,
                "idempotency_key": request.idempotency_key,
                "payload_fingerprint": request.payload_fingerprint,
                "duplicate": duplicate,
                "ack_policy": "STRICT_PER_ROBOT_SEQUENCE",
            }
        )
    return response


@app.post("/dispatches/{dispatch_id}/cancel")
def cancel_dispatch(
    dispatch_id: str, request: CancelDispatchRequest
) -> dict[str, Any]:
    with _received_plans_lock:
        record = next(
            (
                row
                for row in _received_plans
                if row.get("dispatch_id") == dispatch_id
            ),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="DISPATCH_NOT_FOUND")
        if record.get("plan_version") != request.plan_version:
            raise HTTPException(
                status_code=409, detail="PLAN_VERSION_MISMATCH"
            )
        duplicate = record.get("status") == "CANCELED"
        record["status"] = "CANCELED"
        record["canceled_at"] = datetime.now(UTC).isoformat()
        record["cancel_reason"] = request.reason
    return {
        "accepted": True,
        "status": "CANCELED",
        "dispatch_id": dispatch_id,
        "plan_version": request.plan_version,
        "duplicate": duplicate,
    }


@app.get("/received-plans")
def received_plans() -> dict[str, Any]:
    with _received_plans_lock:
        plans = deepcopy(_received_plans)
    return {
        "count": len(plans),
        "plans": plans,
    }


@app.delete("/received-plans")
def clear_received_plans() -> dict[str, Any]:
    with _received_plans_lock:
        deleted_count = len(_received_plans)
        _received_plans.clear()
        _received_by_idempotency.clear()
    return {
        "status": "cleared",
        "deleted_count": deleted_count,
    }

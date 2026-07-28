"""Server-authoritative runtime context for execution events.

Client events may report telemetry, but they may not choose the active plan or
runtime clock.  REAL events resolve the active plan from the warehouse Redis
state.  SIMULATION events resolve it from the simulation session identified by
``simulation_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.models import RobotEvent
from app.time_utils import as_utc_datetime


CLIENT_RUNTIME_FIELDS = {
    "active_plan",
    "active_plan_version",
    "current_time_step",
    "reference_time",
    "activated_at",
    "time_step_seconds",
    "server_runtime",
    "_server_runtime",
}


class RuntimeAuthorityError(RuntimeError):
    """Raised when a server-owned execution context cannot be resolved."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RuntimeContext:
    execution_context: str
    source: str
    active_plan_version: str | None
    active_plan: dict[str, Any] | None
    current_time_step: int
    clock_available: bool
    clock_anchor: str | None
    time_step_seconds: int
    simulation_id: str | None
    ignored_client_fields: tuple[str, ...]
    robot_state: dict[str, Any]

    def internal_payload(self) -> dict[str, Any]:
        return {
            "execution_context": self.execution_context,
            "source": self.source,
            "active_plan_version": self.active_plan_version,
            "active_plan": self.active_plan,
            "current_time_step": self.current_time_step,
            "clock_available": self.clock_available,
            "clock_anchor": self.clock_anchor,
            "time_step_seconds": self.time_step_seconds,
            "simulation_id": self.simulation_id,
            "ignored_client_fields": list(self.ignored_client_fields),
            "robot_state": self.robot_state,
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "execution_context": self.execution_context,
            "source": self.source,
            "active_plan_version": self.active_plan_version,
            "current_time_step": self.current_time_step,
            "clock_available": self.clock_available,
            "clock_anchor": self.clock_anchor,
            "time_step_seconds": self.time_step_seconds,
            "simulation_id": self.simulation_id,
            "ignored_client_fields": list(self.ignored_client_fields),
            "plan_loaded": bool(self.active_plan),
        }


def _robot_state(rows: list[dict[str, Any]], robot_id: str) -> dict[str, Any]:
    return next(
        (
            dict(row)
            for row in rows
            if str(row.get("robot_id")) == str(robot_id)
        ),
        {},
    )


def _plan_from_run(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    output = row.get("output_payload") or {}
    if not isinstance(output, dict):
        return None
    plan_version = row.get("plan_version") or output.get("current_plan_version")
    cuopt_plan = output.get("cuopt_plan")
    collision_plan = output.get("collision_plan")
    if not isinstance(cuopt_plan, dict) or not isinstance(collision_plan, dict):
        return None
    return {
        "plan_version": plan_version,
        "warehouse_id": row.get("warehouse_id"),
        "command_id": row.get("command_id"),
        "scope": output.get("scope") or {},
        "required_tasks": output.get("required_tasks") or [],
        "cuopt_plan": cuopt_plan,
        "collision_plan": collision_plan,
        "ready_task_ids": output.get("ready_task_ids") or [],
        "waiting_task_ids": output.get("waiting_task_ids") or [],
        "blocked_task_ids": output.get("blocked_task_ids") or [],
        "reference_time": output.get("reference_time"),
        "execution_mode": "SIMULATE_ONLY",
        "base_plan_is_simulated": True,
        "candidate_plan": True,
    }


def _simulation_source(event: RobotEvent, services: Any) -> tuple[dict[str, Any], str]:
    if not event.simulation_id:
        raise RuntimeAuthorityError(
            "SIMULATION_ID_REQUIRED",
            "SIMULATION 이벤트에는 simulation_id가 필요합니다.",
        )

    session = None
    repository = getattr(services, "postgres", None)
    if repository is not None and hasattr(repository, "get_simulation_session"):
        session = repository.get_simulation_session(event.simulation_id)
        if session is None:
            raise RuntimeAuthorityError(
                "SIMULATION_SESSION_NOT_FOUND",
                f"simulation_id를 찾을 수 없습니다: {event.simulation_id}",
            )
        if int(session.get("warehouse_id")) != int(event.warehouse_id):
            raise RuntimeAuthorityError(
                "SIMULATION_WAREHOUSE_MISMATCH",
                "다른 warehouse의 simulation_id를 사용할 수 없습니다.",
            )
        if str(session.get("status") or "ACTIVE").upper() in {
            "RESET",
            "RESET_PENDING",
        }:
            raise RuntimeAuthorityError(
                "SIMULATION_SESSION_NOT_ACTIVE",
                f"현재 simulation 상태에서는 이벤트를 처리할 수 없습니다: {session.get('status')}",
            )

    try:
        snapshot = services.redis.simulation_snapshot(event.simulation_id)
        source = "SIMULATION_REDIS_SESSION"
    except Exception:
        # Redis simulation state can expire or be reset independently of the
        # PostgreSQL audit/session record.  Continue with the durable server
        # state instead of falling back to client payload.
        snapshot = {
            "simulation_id": event.simulation_id,
            "inventory": [],
            "robots": [],
            "works": [],
            "checkpoint": None,
        }
        source = "SIMULATION_REDIS_MISSING"
    plan = snapshot.get("active_plan")
    if not isinstance(plan, dict) or not plan:
        current_state = (session or {}).get("current_state") or {}
        plan = current_state.get("active_plan")
        if isinstance(plan, dict) and plan:
            source = "SIMULATION_POSTGRES_SESSION"
    if (not isinstance(plan, dict) or not plan) and repository is not None:
        if hasattr(repository, "get_latest_simulation_runtime_plan"):
            plan = _plan_from_run(
                repository.get_latest_simulation_runtime_plan(event.simulation_id)
            )
            if plan:
                source = "SIMULATION_RUN_FALLBACK"
    if not isinstance(plan, dict) or not plan:
        raise RuntimeAuthorityError(
            "SIMULATION_ACTIVE_PLAN_NOT_FOUND",
            f"simulation_id에 저장된 활성 계획이 없습니다: {event.simulation_id}",
        )
    return {**snapshot, "active_plan": plan}, source


def _clock(
    event: RobotEvent,
    plan: dict[str, Any] | None,
    *,
    execution_context: str,
    time_step_seconds: int,
) -> tuple[int, bool, str | None]:
    if not plan:
        return 0, False, None
    if execution_context == "REAL":
        anchor = plan.get("activated_at") or plan.get("reference_time")
    else:
        anchor = plan.get("reference_time") or plan.get("activated_at")
    if not anchor:
        return 0, False, None
    try:
        anchor_at = as_utc_datetime(anchor, field_name="runtime_clock_anchor")
        elapsed = max(0.0, (event.occurred_at - anchor_at).total_seconds())
        return (
            int(elapsed // max(1, int(time_step_seconds))),
            True,
            anchor_at.isoformat(),
        )
    except (TypeError, ValueError):
        return 0, False, None


def resolve_runtime_context(event: RobotEvent, services: Any) -> RuntimeContext:
    """Resolve the plan and clock exclusively from server-owned state."""

    settings = get_settings()
    ignored = tuple(
        sorted(key for key in event.payload if key in CLIENT_RUNTIME_FIELDS)
    )
    if event.execution_context == "SIMULATION":
        snapshot, source = _simulation_source(event, services)
        plan = snapshot.get("active_plan")
        active_version = (
            snapshot.get("active_plan_version")
            or (plan or {}).get("plan_version")
        )
        robots = list(snapshot.get("robots") or [])
        simulation_id = event.simulation_id
    else:
        snapshot = services.redis.live_snapshot(event.warehouse_id)
        source = "REAL_REDIS_ACTIVE_PLAN"
        plan = snapshot.get("active_plan")
        active_version = snapshot.get("active_plan_version") or (
            (plan or {}).get("plan_version") if isinstance(plan, dict) else None
        )
        robots = list(snapshot.get("robots") or [])
        simulation_id = None

    if not isinstance(plan, dict) or not plan:
        plan = None
    elif active_version and not plan.get("plan_version"):
        plan = {**plan, "plan_version": str(active_version)}

    time_step_seconds = int(
        (plan or {}).get("time_step_seconds")
        or ((plan or {}).get("collision_plan") or {}).get("time_step_seconds")
        or settings.time_step_seconds
    )
    current_step, clock_available, anchor = _clock(
        event,
        plan,
        execution_context=event.execution_context,
        time_step_seconds=time_step_seconds,
    )
    state = _robot_state(robots, event.robot_id)
    if event.node_id is not None:
        state["node_id"] = int(event.node_id)
    if event.battery is not None:
        state["battery"] = float(event.battery)

    return RuntimeContext(
        execution_context=event.execution_context,
        source=source,
        active_plan_version=(str(active_version) if active_version else None),
        active_plan=plan,
        current_time_step=current_step,
        clock_available=clock_available,
        clock_anchor=anchor,
        time_step_seconds=max(1, time_step_seconds),
        simulation_id=simulation_id,
        ignored_client_fields=ignored,
        robot_state=state,
    )


def bind_runtime_context(event: RobotEvent, context: RuntimeContext) -> RobotEvent:
    """Return an internal event copy with client runtime controls removed."""

    cleaned = {
        key: value
        for key, value in event.payload.items()
        if key not in CLIENT_RUNTIME_FIELDS
    }
    cleaned["_server_runtime"] = context.internal_payload()
    return event.model_copy(update={"payload": cleaned})


def derive_low_battery_event(
    event: RobotEvent,
    context: RuntimeContext,
) -> RobotEvent | None:
    """Convert low-battery telemetry to an internal anomaly event.

    A POSITION_UPDATED event always performs telemetry mutation only.  The
    server may then derive a separate LOW_BATTERY impact from the resulting
    reported state.
    """

    if event.event_type != "POSITION_UPDATED":
        return None
    battery = event.battery
    if battery is None and context.robot_state.get("battery") is not None:
        battery = float(context.robot_state["battery"])
    if battery is None:
        return None
    settings = get_settings()
    minimum = float(settings.min_robot_battery)
    safety_margin = float(settings.battery_safety_margin_percent)
    current_step = int(context.current_time_step)
    remaining_energy = 0.0
    scheduled = list(
        ((context.active_plan or {}).get("cuopt_plan") or {}).get(
            "scheduled_tasks", []
        )
    )
    for task in scheduled:
        if str(task.get("robot_id")) != str(event.robot_id):
            continue
        if int(task.get("end_time_step") or 0) < current_step:
            continue
        remaining_energy += max(0.0, float(task.get("estimated_energy") or 0.0))
    threshold = min(100.0, minimum + safety_margin + remaining_energy)
    if float(battery) > threshold:
        return None
    payload = dict(event.payload)
    payload.update(
        {
            "server_derived": True,
            "derived_from_event_type": "POSITION_UPDATED",
            "low_battery_threshold": threshold,
            "minimum_battery": minimum,
            "battery_safety_margin_percent": safety_margin,
            "remaining_planned_energy_percent": remaining_energy,
            "battery_detection_policy": "MINIMUM_PLUS_MARGIN_AND_REMAINING_PLAN_ENERGY",
        }
    )
    return event.model_copy(
        update={
            "event_type": "LOW_BATTERY",
            "battery": float(battery),
            "payload": payload,
        }
    )

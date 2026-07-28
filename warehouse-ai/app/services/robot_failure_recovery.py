"""Deterministic robot-failure and carried-load recovery planning.

The event layer must distinguish a robot that failed before pickup from one
that stopped while carrying inventory.  Reassigning the original PICK after a
confirmed pickup would duplicate inventory movement, while preserving the
failed robot's DROP would strand the load.  This module derives an auditable
recovery contract and, for a secured carried load, materializes a synthetic
handover PICK/DROP chain at the failed robot's stop node.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.models import RobotEvent

FAILED_STATUSES = {"FAILED", "ROBOT_FAILED", "OFFLINE", "MAINTENANCE"}
ACTIVE_STATUSES = {"IDLE", "READY", "EXECUTING", "RUNNING", "CHARGING"}


def _text(value: object) -> str:
    return str(value or "")


def _work_id(row: dict[str, Any]) -> str:
    task_id = _text(row.get("task_id"))
    return _text(row.get("work_id")) or task_id.split(":", 1)[0]


def _runtime_step(event: RobotEvent) -> int | None:
    runtime = event.payload.get("_server_runtime") or {}
    value = runtime.get("current_time_step")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _source_required(active_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _text(row.get("task_id")): deepcopy(row)
        for row in active_plan.get("required_tasks") or []
        if isinstance(row, dict) and row.get("task_id")
    }


def _scheduled(active_plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        deepcopy(row)
        for row in (active_plan.get("cuopt_plan") or {}).get("scheduled_tasks") or []
        if isinstance(row, dict) and row.get("task_id")
    ]


def _robot_rows(sql: dict[str, Any], live: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in [*sql.get("robots", []), *live.get("robots", [])]:
        robot_id = _text(row.get("robot_id"))
        if not robot_id:
            continue
        rows.setdefault(robot_id, {}).update(deepcopy(row))
    return rows


def _relevant_work(
    event: RobotEvent,
    failed_schedule: list[dict[str, Any]],
    current_step: int | None,
) -> str | None:
    if event.work_id:
        return str(event.work_id)
    if event.task_id:
        return str(event.task_id).split(":", 1)[0]
    if current_step is not None:
        active = [
            row
            for row in failed_schedule
            if int(row.get("start_time_step") or 0)
            <= current_step
            < int(row.get("end_time_step") or row.get("start_time_step") or 0)
        ]
        if active:
            active.sort(key=lambda row: (int(row.get("start_time_step") or 0), _text(row.get("task_id"))))
            return _work_id(active[0])
    if failed_schedule:
        failed_schedule.sort(key=lambda row: (int(row.get("start_time_step") or 0), _text(row.get("task_id"))))
        return _work_id(failed_schedule[0])
    return None


def _chain_rows(
    failed_schedule: list[dict[str, Any]],
    work_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not work_id:
        return None, None
    rows = [row for row in failed_schedule if _work_id(row) == work_id]
    pick = next((row for row in rows if _text(row.get("action")).upper() == "PICK"), None)
    drop = next((row for row in rows if _text(row.get("action")).upper() == "DROP"), None)
    return pick, drop


def _explicit_carried_load(event: RobotEvent) -> tuple[dict[str, Any] | None, str | None]:
    raw = event.payload.get("carried_load")
    if isinstance(raw, dict):
        quantity = raw.get("quantity_boxes", raw.get("quantity"))
        return (
            {
                "item_id": raw.get("item_id"),
                "quantity_boxes": int(quantity or 0),
                "lot_id": raw.get("lot_id"),
                "work_id": raw.get("work_id") or event.work_id,
                "pickup_task_id": raw.get("pickup_task_id"),
                "load_secured": bool(raw.get("load_secured", event.payload.get("load_secured", False))),
                "source": "EVENT_CARRIED_LOAD",
            },
            "CARRYING",
        )
    carrying = event.payload.get("carrying_load")
    if carrying is None:
        carrying = event.payload.get("has_load")
    if carrying is False:
        return None, "EMPTY"
    if carrying is True:
        quantity = event.payload.get("carried_quantity_boxes", event.payload.get("quantity_boxes"))
        return (
            {
                "item_id": event.payload.get("carried_item_id", event.payload.get("item_id")),
                "quantity_boxes": int(quantity or 0),
                "lot_id": event.payload.get("carried_lot_id", event.payload.get("lot_id")),
                "work_id": event.payload.get("carried_work_id") or event.work_id,
                "pickup_task_id": event.payload.get("carried_pickup_task_id"),
                "load_secured": bool(event.payload.get("load_secured", False)),
                "source": "EVENT_CARRYING_FLAG",
            },
            "CARRYING",
        )
    return None, None


def _infer_load(
    event: RobotEvent,
    current_step: int | None,
    pick: dict[str, Any] | None,
    drop: dict[str, Any] | None,
    required_by_task: dict[str, dict[str, Any]],
    robot_state: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    explicit, explicit_state = _explicit_carried_load(event)
    if explicit_state == "EMPTY":
        return None, "EMPTY"
    if explicit_state == "CARRYING":
        return explicit, "CARRYING"

    current_load = robot_state.get("current_load")
    try:
        has_current_load = float(current_load or 0) > 0
    except (TypeError, ValueError):
        has_current_load = False

    carrying_by_clock = False
    if current_step is not None and pick and drop:
        pick_end = int(pick.get("end_time_step") or pick.get("start_time_step") or 0)
        drop_end = int(drop.get("end_time_step") or drop.get("start_time_step") or 0)
        carrying_by_clock = pick_end <= current_step < drop_end
        if current_step < pick_end:
            return None, "EMPTY"
        if current_step >= drop_end:
            return None, "EMPTY"

    if not (has_current_load or carrying_by_clock):
        if pick is not None and current_step is None:
            return None, "UNKNOWN"
        return None, "EMPTY"

    pick_required = required_by_task.get(_text((pick or {}).get("task_id")), {})
    quantity = (
        pick_required.get("quantity")
        or pick_required.get("quantity_boxes")
        or (pick or {}).get("quantity")
        or (pick or {}).get("quantity_boxes")
        or current_load
        or 0
    )
    allocations = list(pick_required.get("inventory_allocations") or [])
    first = allocations[0] if allocations else {}
    return (
        {
            "item_id": pick_required.get("item_id") or first.get("item_id"),
            "quantity_boxes": int(float(quantity or 0)),
            "lot_id": first.get("lot_id"),
            "work_id": pick_required.get("work_id") or _work_id(pick or {}),
            "pickup_task_id": _text((pick or {}).get("task_id")) or None,
            "load_secured": bool(event.payload.get("load_secured", False)),
            "source": "SERVER_PLAN_CLOCK" if carrying_by_clock else "ROBOT_CURRENT_LOAD",
        },
        "CARRYING",
    )


def _replacement_candidates(
    event: RobotEvent,
    sql: dict[str, Any],
    live: dict[str, Any],
    quantity: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for robot_id, row in sorted(_robot_rows(sql, live).items()):
        if robot_id == event.robot_id:
            continue
        status = _text(row.get("last_event") or row.get("status")).upper()
        if status in FAILED_STATUSES:
            continue
        max_load = float(row.get("max_load") or 0)
        current_load = float(row.get("current_load") or 0)
        load_capacity_known = max_load > 0
        residual = max_load - current_load if load_capacity_known else None
        if residual is not None and residual + 1e-9 < quantity:
            continue
        candidates.append(
            {
                "robot_id": robot_id,
                "node_id": row.get("node_id"),
                "battery": row.get("battery"),
                "status": row.get("status"),
                "residual_load_capacity": residual,
            }
        )
    return candidates


def _handover_allocation(
    *,
    event: RobotEvent,
    node_id: int,
    item_id: str | None,
    quantity: int,
    lot_id: str | None,
) -> list[dict[str, Any]]:
    return [
        {
            "warehouse_item_id": f"ROBOT-HANDOVER:{event.event_id}",
            "item_id": item_id,
            "lot_id": lot_id or f"HANDOVER-{event.event_id}",
            "node_id": node_id,
            "storage_node_id": node_id,
            "quantity": quantity,
            "quantity_boxes": quantity,
            "available_at": event.occurred_at.isoformat(),
            "source_type": "ROBOT_HANDOVER",
            "inbound_source_id": event.robot_id,
        }
    ]


def _handover_tasks(
    event: RobotEvent,
    *,
    carried: dict[str, Any],
    pick: dict[str, Any] | None,
    drop: dict[str, Any] | None,
    required_by_task: dict[str, dict[str, Any]],
    node_id: int,
) -> list[dict[str, Any]]:
    work_id = _text(carried.get("work_id")) or _work_id(drop or pick or {})
    item_id = carried.get("item_id")
    quantity = int(carried.get("quantity_boxes") or 0)
    original_drop_id = _text((drop or {}).get("task_id"))
    original_drop = required_by_task.get(original_drop_id, {})
    if item_id is None:
        item_id = original_drop.get("item_id")
    if quantity <= 0:
        quantity = int(original_drop.get("quantity") or original_drop.get("quantity_boxes") or 0)
    target_candidates = list(original_drop.get("target_candidates") or [])
    if not target_candidates and (drop or {}).get("target_node") is not None:
        target_candidates = [int((drop or {})["target_node"])]
    prefix = f"{work_id}:handover:{event.event_id}"
    pick_id = f"{prefix}:pick"
    drop_id = f"{prefix}:drop"
    deadline = original_drop.get("deadline")
    latest_finish = original_drop.get("latest_finish")
    allocations = _handover_allocation(
        event=event,
        node_id=node_id,
        item_id=item_id,
        quantity=quantity,
        lot_id=carried.get("lot_id"),
    )
    common = {
        "work_id": work_id,
        "item_id": item_id,
        "quantity": quantity,
        "priority": int(original_drop.get("priority") or 1),
        "deadline": deadline,
        "earliest_start": event.occurred_at.isoformat(),
        "latest_finish": latest_finish,
        "time_constraint_type": original_drop.get("time_constraint_type") or "ASAP",
        "same_robot_group": prefix,
        "frozen": False,
        "assigned_robot_id": None,
        "inventory_allocations": allocations,
        "inventory_transition_policy": "NO_STOCK_DELTA",
    }
    return [
        {
            **common,
            "task_id": pick_id,
            "action": "PICK",
            "source_candidates": [node_id],
            "target_candidates": [node_id],
            "predecessors": [],
            "dependencies": [],
        },
        {
            **common,
            "task_id": drop_id,
            "action": "DROP",
            "source_candidates": [node_id],
            "target_candidates": target_candidates,
            "predecessors": [pick_id],
            "dependencies": [],
        },
    ]


def derive_robot_failure_recovery(
    event: RobotEvent,
    *,
    active_plan: dict[str, Any],
    sql: dict[str, Any],
    live: dict[str, Any],
) -> dict[str, Any]:
    """Return the server-authoritative recovery contract for ROBOT_FAILED."""

    if event.event_type != "ROBOT_FAILED":
        return {}
    scheduled = _scheduled(active_plan)
    failed_schedule = [
        row for row in scheduled if _text(row.get("robot_id")) == event.robot_id
    ]
    current_step = _runtime_step(event)
    work_id = _relevant_work(event, failed_schedule, current_step)
    pick, drop = _chain_rows(failed_schedule, work_id)
    required_by_task = _source_required(active_plan)
    robots = _robot_rows(sql, live)
    failed_state = robots.get(event.robot_id, {})
    node_id = event.node_id
    if node_id is None:
        runtime = event.payload.get("_server_runtime") or {}
        robot_state = runtime.get("robot_state") or {}
        node_id = robot_state.get("node_id", failed_state.get("node_id"))
    carried, load_state = _infer_load(
        event,
        current_step,
        pick,
        drop,
        required_by_task,
        failed_state,
    )
    quantity = int((carried or {}).get("quantity_boxes") or 0)
    candidates = _replacement_candidates(event, sql, live, quantity)
    safe_stop = bool(event.payload.get("safe_stop_confirmed", False))
    load_secured = bool((carried or {}).get("load_secured", False))
    affected_chain_ids = [
        _text(row.get("task_id"))
        for row in (pick, drop)
        if row and row.get("task_id")
    ]

    strategy = "REASSIGN_UNPICKED_CHAIN"
    status = "READY"
    recovery_tasks: list[dict[str, Any]] = []
    replace_task_ids: list[str] = []
    reason = "고장 시점이 PICK 이전이므로 남은 PICK/DROP 체인을 대체 로봇에 재배정합니다."
    if not candidates:
        strategy = "GLOBAL_CAPACITY_RECOVERY_REQUIRED"
        status = "BLOCKED"
        reason = "적재 용량과 상태 조건을 만족하는 대체 로봇이 없습니다."
    elif load_state == "UNKNOWN":
        strategy = "MANUAL_LOAD_STATE_CONFIRMATION_REQUIRED"
        status = "BLOCKED"
        reason = "고장 시점의 적재 여부를 서버 계획 또는 이벤트에서 확정할 수 없습니다."
    elif load_state == "CARRYING":
        if node_id is None:
            strategy = "MANUAL_FAILURE_POSITION_REQUIRED"
            status = "BLOCKED"
            reason = "적재물을 인계할 고장 정지 노드를 확인할 수 없습니다."
        elif not safe_stop or not load_secured:
            strategy = "MANUAL_LOAD_RECOVERY_REQUIRED"
            status = "BLOCKED"
            reason = "안전 정지와 적재물 고정이 모두 확인되지 않아 자동 인계를 차단합니다."
        elif not carried or not work_id or drop is None:
            strategy = "MANUAL_LOAD_RECOVERY_REQUIRED"
            status = "BLOCKED"
            reason = "적재물 또는 목적지 작업 정보를 복원할 수 없어 자동 인계를 차단합니다."
        else:
            strategy = "HANDOVER_SECURED_LOAD"
            replace_task_ids = affected_chain_ids
            recovery_tasks = _handover_tasks(
                event,
                carried=carried,
                pick=pick,
                drop=drop,
                required_by_task=required_by_task,
                node_id=int(node_id),
            )
            reason = (
                "PICK 완료 후 적재물을 운반 중이므로 원래 PICK을 반복하지 않고 "
                "고장 정지 노드에서 대체 로봇 인계 PICK/DROP 체인을 생성합니다."
            )

    return {
        "version": "p16.5.14",
        "status": status,
        "strategy": strategy,
        "failed_robot_id": event.robot_id,
        "failed_node_id": int(node_id) if node_id is not None else None,
        "current_time_step": current_step,
        "work_id": work_id,
        "load_state": load_state,
        "safe_stop_confirmed": safe_stop,
        "load_secured": load_secured,
        "carried_load": deepcopy(carried),
        "original_pick_task_id": _text((pick or {}).get("task_id")) or None,
        "original_drop_task_id": _text((drop or {}).get("task_id")) or None,
        "replace_task_ids": replace_task_ids,
        "recovery_tasks": recovery_tasks,
        "recovery_task_ids": [row["task_id"] for row in recovery_tasks],
        "replacement_candidates": candidates,
        "replacement_candidate_ids": [row["robot_id"] for row in candidates],
        "requires_manual_recovery": status == "BLOCKED",
        "reason": reason,
    }

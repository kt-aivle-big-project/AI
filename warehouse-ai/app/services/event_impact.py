"""Deterministic impact analysis for execution anomaly events."""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from app.config import get_settings
from app.models import EventImpactAnalysis, RobotEvent
from app.services.robot_failure_recovery import derive_robot_failure_recovery
from app.time_utils import as_utc_datetime


TERMINAL_TASK_STATUSES = {"COMPLETED", "CANCELLED"}
EXECUTING_TASK_STATUSES = {"EXECUTING", "IN_PROGRESS", "STARTED", "RUNNING"}
ROUTE_INVALIDATING_EVENTS = {
    "PATH_BLOCKED",
    "PATH_DEVIATED",
    "ROBOT_FAILED",
    "TASK_FAILED",
}


def _route_pairs(route: dict[str, Any]) -> set[tuple[int, int]]:
    waypoints = route.get("waypoints") or []
    return {
        (int(left["node_id"]), int(right["node_id"]))
        for left, right in zip(waypoints, waypoints[1:])
        if left.get("node_id") is not None and right.get("node_id") is not None
    }


def _reachable(
    graph: dict[str, Any],
    start: int,
    targets: set[int],
) -> bool:
    if start in targets:
        return True
    adjacency: dict[int, set[int]] = {}
    for edge in graph.get("edges", []):
        left = int(edge["from_node"])
        right = int(edge["to_node"])
        adjacency.setdefault(left, set()).add(right)
        if str(edge.get("direction", "ONE_WAY")).upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            adjacency.setdefault(right, set()).add(left)
    queue = deque([start])
    seen = {start}
    while queue:
        node = queue.popleft()
        for target in sorted(adjacency.get(node, set())):
            if target in targets:
                return True
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return False


def _snapshots(
    event: RobotEvent,
    services: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if event.execution_context == "SIMULATION":
        runtime = event.payload.get("_server_runtime") or {}
        try:
            virtual = services.redis.simulation_snapshot(event.simulation_id)
        except Exception:
            robot_state = dict(runtime.get("robot_state") or {})
            if robot_state and not robot_state.get("robot_id"):
                robot_state["robot_id"] = event.robot_id
            virtual = {
                "inventory": [],
                "robots": [robot_state] if robot_state else [],
                "works": [],
                "temporary_closures": [],
            }
        active_plan = runtime.get("active_plan") or virtual.get("active_plan")
        active_version = runtime.get("active_plan_version") or virtual.get(
            "active_plan_version"
        )
        sql = {
            "inventory": virtual.get("inventory", []),
            "robots": virtual.get("robots", []),
            "works": virtual.get("works", []),
        }
        live = {
            "robots": virtual.get("robots", []),
            "tasks": virtual.get("works", []),
            "active_plan_version": active_version,
            "active_plan": active_plan,
            "temporary_closures": virtual.get("temporary_closures", []),
        }
    else:
        sql = services.postgres.snapshot(event.warehouse_id, [])
        live = services.redis.live_snapshot(event.warehouse_id)
    graph = services.neo4j.fetch_topology(event.warehouse_id)
    return sql, live, graph


def _runtime_clock(
    event: RobotEvent,
    active_plan: dict[str, Any],
    *,
    time_step_seconds: int,
) -> tuple[int, bool]:
    runtime = event.payload.get("_server_runtime") or {}
    explicit = runtime.get("current_time_step")
    if explicit is not None and runtime.get("clock_available") is not False:
        return max(0, int(explicit)), True
    activated_at = runtime.get("clock_anchor") or active_plan.get("activated_at") or active_plan.get("reference_time")
    if activated_at:
        try:
            elapsed = max(
                0.0,
                (
                    event.occurred_at
                    - as_utc_datetime(activated_at, field_name="activated_at")
                ).total_seconds(),
            )
            return int(elapsed // max(1, time_step_seconds)), True
        except (TypeError, ValueError):
            pass
    return 0, False


def _status_indexes(
    sql: dict[str, Any],
    live: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    task_status: dict[str, str] = {}
    work_status: dict[str, str] = {}
    for row in [*sql.get("works", []), *live.get("tasks", [])]:
        status = str(row.get("status") or "").upper()
        task_id = row.get("task_id")
        work_id = row.get("work_id")
        if task_id:
            task_status[str(task_id)] = status
        if work_id:
            work_status[str(work_id)] = status
    return task_status, work_status


def _scheduled_status(
    row: dict[str, Any],
    task_status: dict[str, str],
    work_status: dict[str, str],
) -> str:
    task_id = str(row.get("task_id") or "")
    work_id = str(row.get("work_id") or task_id.split(":", 1)[0])
    return task_status.get(task_id) or work_status.get(work_id) or ""


def _robot_state_override(
    event: RobotEvent,
    sql: dict[str, Any],
    live: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    source = next(
        (
            row
            for row in [*live.get("robots", []), *sql.get("robots", [])]
            if str(row.get("robot_id")) == event.robot_id
        ),
        {},
    )
    state: dict[str, Any] = {
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat(),
    }
    for field in ("node_id", "battery", "status", "current_load", "max_load"):
        if source.get(field) is not None:
            state[field] = source.get(field)
    if event.node_id is not None:
        state["node_id"] = int(event.node_id)
    event_battery = event.battery
    if event_battery is None and event.payload.get("battery") is not None:
        event_battery = float(event.payload["battery"])
    if event_battery is not None:
        state["battery"] = float(event_battery)
    if event.event_type == "ROBOT_FAILED":
        state["status"] = "FAILED"
    if event.payload.get("delay_seconds") is not None:
        state["delay_seconds"] = max(0, int(event.payload["delay_seconds"]))
    if event.payload.get("safe_stop_confirmed") is not None:
        state["safe_stop_confirmed"] = bool(
            event.payload.get("safe_stop_confirmed")
        )
    return {event.robot_id: state}


def _runtime_partial_scope(
    event: RobotEvent,
    active_plan: dict[str, Any],
    sql: dict[str, Any],
    live: dict[str, Any],
    affected_tasks: set[str],
    scope: str,
) -> tuple[list[str], list[str], list[str], int, list[str]]:
    settings = get_settings()
    time_step_seconds = max(1, int(getattr(settings, "time_step_seconds", 5)))
    freeze_horizon_seconds = max(
        int(getattr(settings, "freeze_horizon_seconds", 15)),
        int(event.payload.get("freeze_horizon_seconds") or 0),
    )
    freeze_steps = math.ceil(freeze_horizon_seconds / time_step_seconds)
    scheduled = list((active_plan.get("cuopt_plan") or {}).get("scheduled_tasks") or [])
    scheduled_ids = {
        str(row.get("task_id"))
        for row in scheduled
        if row.get("task_id") is not None
    }
    if not scheduled_ids:
        return [], [], sorted(affected_tasks), freeze_horizon_seconds, []

    task_status, work_status = _status_indexes(sql, live)
    current_step, clock_available = _runtime_clock(
        event,
        active_plan,
        time_step_seconds=time_step_seconds,
    )
    completed: set[str] = set()
    near_term_or_executing: set[str] = set()
    evidence: list[str] = []
    for row in scheduled:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        start_step = int(row.get("start_time_step") or 0)
        end_step = int(row.get("end_time_step") or start_step)

        # A simulation session stores the fully replayed result for audit, so
        # its Redis work rows may all be COMPLETED even when an event is
        # intentionally injected at an earlier point in the plan.  For
        # simulation events the server-owned plan clock is therefore the
        # authoritative task-status source.  REAL execution continues to use
        # persisted/live task status first.
        if event.execution_context == "SIMULATION" and clock_available:
            if end_step <= current_step:
                completed.add(task_id)
                continue
            if start_step <= current_step < end_step:
                near_term_or_executing.add(task_id)
                continue
            if start_step <= current_step + freeze_steps:
                near_term_or_executing.add(task_id)
            continue

        status = _scheduled_status(row, task_status, work_status)
        if status in TERMINAL_TASK_STATUSES:
            completed.add(task_id)
            continue
        if status in EXECUTING_TASK_STATUSES:
            near_term_or_executing.add(task_id)
            continue
        if clock_available and start_step <= current_step + freeze_steps:
            near_term_or_executing.add(task_id)

    explicit_task = str(event.task_id or "")
    if event.event_type in ROUTE_INVALIDATING_EVENTS:
        # A blocked/deviated path, failed task, or failed robot cannot remain
        # frozen merely because it is currently executing. Completed work is
        # still immutable.
        near_term_or_executing.difference_update(affected_tasks)
    elif event.event_type == "ROBOT_DELAYED":
        delay_seconds = max(0, int(event.payload.get("delay_seconds") or 0))
        if delay_seconds > freeze_horizon_seconds:
            near_term_or_executing.difference_update(affected_tasks)
    elif event.event_type == "LOW_BATTERY":
        battery = event.battery
        if battery is None and event.payload.get("battery") is not None:
            battery = float(event.payload["battery"])
        minimum = float(getattr(settings, "min_robot_battery", 20.0))
        derived_threshold = event.payload.get("low_battery_threshold")
        threshold = (
            float(derived_threshold)
            if derived_threshold not in (None, "")
            else minimum
        )
        # A server-derived LOW_BATTERY event means the currently frozen route
        # can no longer finish while preserving the configured reserve.  In
        # that case keeping the executing task frozen prevents the optimizer
        # from inserting a safety charge before that task.  Release only the
        # affected chain from the freeze horizon; unrelated work remains fixed.
        safety_replan_required = bool(event.payload.get("server_derived")) or (
            battery is not None and float(battery) <= threshold
        )
        if bool(event.payload.get("safe_stop_confirmed")) or safety_replan_required:
            near_term_or_executing.difference_update(affected_tasks)
            evidence.append(
                "저배터리 안전 예외로 영향 작업의 freeze horizon을 해제해 "
                "충전 방문을 현재 작업보다 앞에 삽입할 수 있도록 했습니다."
            )
    if event.event_type == "TASK_FAILED" and explicit_task:
        near_term_or_executing.discard(explicit_task)

    if scope == "GLOBAL_REPLAN":
        changeable = scheduled_ids - completed - near_term_or_executing
        protected = near_term_or_executing
    else:
        changeable = (
            affected_tasks & scheduled_ids
        ) - completed - near_term_or_executing
        protected = scheduled_ids - completed - changeable

    if completed:
        evidence.append(f"완료 작업 {len(completed)}건은 재계획 대상에서 제외했습니다.")
    if protected:
        evidence.append(
            f"실행 중·freeze horizon·비영향 작업 {len(protected)}건을 고정했습니다."
        )
    if clock_available:
        evidence.append(
            f"현재 step {current_step}, freeze horizon {freeze_horizon_seconds}초를 적용했습니다."
        )
    return (
        sorted(completed),
        sorted(protected),
        sorted(changeable),
        freeze_horizon_seconds,
        evidence,
    )


def analyze_event_impact(
    event: RobotEvent,
    services: Any,
) -> EventImpactAnalysis:
    sql, live, graph = _snapshots(event, services)
    active_plan = live.get("active_plan") or {}
    collision = active_plan.get("collision_plan") or {}
    optimization = active_plan.get("cuopt_plan") or {}
    routes = collision.get("routes") or []
    scheduled = optimization.get("scheduled_tasks") or []
    known_robot_ids = {
        str(row.get("robot_id"))
        for row in [*sql.get("robots", []), *live.get("robots", [])]
        if row.get("robot_id") is not None
    }
    known_task_ids = {
        str(row.get("task_id") or row.get("work_id"))
        for row in [*sql.get("works", []), *live.get("tasks", [])]
        if row.get("task_id") is not None or row.get("work_id") is not None
    }
    known_task_ids.update(
        str(row["task_id"])
        for row in scheduled
        if row.get("task_id") is not None
    )
    affected_robots: set[str] = set()
    affected_tasks: set[str] = set()
    affected_nodes: set[int] = set()
    affected_edges: set[str] = set()
    evidence: list[str] = []
    robot_failure_recovery: dict[str, Any] = {}

    if event.robot_id in known_robot_ids:
        affected_robots.add(event.robot_id)
    explicit_task = event.task_id or event.work_id
    if explicit_task and str(explicit_task) in known_task_ids:
        affected_tasks.add(str(explicit_task))

    robot_routes = [
        route for route in routes if str(route.get("robot_id")) == event.robot_id
    ]
    for route in robot_routes:
        affected_tasks.update(
            str(value)
            for value in route.get("task_ids", [])
            if str(value) in known_task_ids
        )
    affected_tasks.update(
        str(row["task_id"])
        for row in scheduled
        if str(row.get("robot_id")) == event.robot_id
        and str(row.get("task_id")) in known_task_ids
    )

    scope = "NO_REPLAN"
    risk = "LOW"
    if event.event_type == "ROBOT_FAILED":
        risk = "HIGH"
        robot_failure_recovery = derive_robot_failure_recovery(
            event,
            active_plan=active_plan,
            sql=sql,
            live=live,
        )
        replacement_ids = list(
            robot_failure_recovery.get("replacement_candidate_ids") or []
        )
        if robot_failure_recovery.get("status") == "BLOCKED":
            scope = "NO_REPLAN"
        else:
            scope = (
                "LOCAL_REPLAN"
                if replacement_ids and affected_tasks
                else "GLOBAL_REPLAN"
            )
        evidence.append(
            f"Snapshot에서 고장 로봇 {event.robot_id}의 관련 작업 {len(affected_tasks)}건을 확인했습니다."
        )
        evidence.append(f"대체 가능한 다른 로봇 후보는 {len(replacement_ids)}대입니다.")
        if robot_failure_recovery.get("reason"):
            evidence.append(str(robot_failure_recovery["reason"]))
    elif event.event_type == "LOW_BATTERY":
        settings = get_settings()
        battery = event.battery
        if battery is None:
            battery = float(event.payload.get("battery"))
        minimum = float(getattr(settings, "min_robot_battery", 20.0))
        target = float(getattr(settings, "charge_target_battery", 80.0))
        risk = "HIGH" if battery <= minimum else "MEDIUM"
        if affected_tasks and battery < target:
            scope = "LOCAL_REPLAN"
        evidence.append(
            f"실시간 배터리 {battery:.3f}%를 최소 {minimum:.3f}% 및 작업 투입 목표 {target:.3f}%와 비교했습니다."
        )
    elif event.event_type == "ROBOT_DELAYED":
        delay_seconds = max(0, int(event.payload.get("delay_seconds") or 0))
        freeze_horizon = int(get_settings().freeze_horizon_seconds)
        if affected_tasks and delay_seconds > freeze_horizon:
            scope = "LOCAL_REPLAN"
            risk = "MEDIUM"
        evidence.append(
            f"보고된 지연 {delay_seconds}초와 freeze horizon {freeze_horizon}초를 비교했습니다."
        )
    elif event.event_type == "TASK_FAILED":
        risk = "HIGH"
        scope = "LOCAL_REPLAN" if affected_tasks else "GLOBAL_REPLAN"
        evidence.append(
            f"실패 작업과 동일 로봇의 남은 관련 작업 {len(affected_tasks)}건을 재검토했습니다."
        )
    elif event.event_type == "PATH_BLOCKED":
        risk = "HIGH"
        blocked_node = event.node_id or event.payload.get("node_id")
        edge_id = event.payload.get("edge_id")
        from_node = event.payload.get("from_node")
        to_node = event.payload.get("to_node")
        impacted_routes: list[dict[str, Any]] = []
        if blocked_node is not None:
            blocked_node = int(blocked_node)
            affected_nodes.add(blocked_node)
            impacted_routes = [
                route
                for route in routes
                if blocked_node
                in {
                    int(row["node_id"])
                    for row in route.get("waypoints", [])
                    if row.get("node_id") is not None
                }
            ]
        elif from_node is not None and to_node is not None:
            pair = (int(from_node), int(to_node))
            affected_nodes.update(pair)
            affected_edges.add(str(edge_id or f"{pair[0]}->{pair[1]}"))
            impacted_routes = [route for route in routes if pair in _route_pairs(route)]
        elif edge_id is not None:
            affected_edges.add(str(edge_id))
            graph_edge = next(
                (
                    row
                    for row in graph.get("edges", [])
                    if str(row.get("edge_id")) == str(edge_id)
                ),
                None,
            )
            if graph_edge:
                pair = (int(graph_edge["from_node"]), int(graph_edge["to_node"]))
                affected_nodes.update(pair)
                impacted_routes = [route for route in routes if pair in _route_pairs(route)]
        impacted_robot_ids = {
            str(route.get("robot_id"))
            for route in impacted_routes
            if route.get("robot_id")
        }
        affected_robots.update(impacted_robot_ids & known_robot_ids)
        for route in impacted_routes:
            affected_tasks.update(
                str(value)
                for value in route.get("task_ids", [])
                if str(value) in known_task_ids
            )
        if len(impacted_robot_ids) == 1:
            scope = "LOCAL_REPLAN"
        elif len(impacted_robot_ids) > 1:
            scope = "GLOBAL_REPLAN"
        evidence.append(
            f"활성 계획 경로 {len(routes)}개 중 차단 자원을 사용하는 경로 {len(impacted_routes)}개를 확인했습니다."
        )
    elif event.event_type == "POSITION_UPDATED":
        # Position telemetry is authoritative state input, not a replanning
        # request.  Low battery is derived by the server as a separate internal
        # LOW_BATTERY anomaly; route divergence must use PATH_DEVIATED.
        risk = "LOW"
        scope = "NO_REPLAN"
        evidence.append("POSITION_UPDATED는 서버 상태 갱신 전용으로 처리했습니다.")
    elif event.event_type == "PATH_DEVIATED":
        risk = "MEDIUM"
        graph_nodes = {
            int(row["node_id"])
            for row in graph.get("nodes", [])
            if row.get("node_id") is not None
        }
        current_node = int(event.node_id) if event.node_id is not None else None
        route_nodes = {
            int(row["node_id"])
            for route in robot_routes
            for row in route.get("waypoints", [])
            if row.get("node_id") is not None
        }
        if current_node is None or current_node not in graph_nodes:
            scope = "GLOBAL_REPLAN"
            evidence.append("현재 위치가 지도 Snapshot에서 확인되지 않아 전역 재계획이 필요합니다.")
        else:
            affected_nodes.add(current_node)
            can_return = bool(route_nodes) and _reachable(graph, current_node, route_nodes)
            scope = "LOCAL_REPLAN" if can_return else "GLOBAL_REPLAN"
            evidence.append(
                f"현재 노드 {current_node}에서 기존 남은 경로로 복귀 가능 여부={can_return}입니다."
            )

    (
        completed_task_ids,
        frozen_task_ids,
        changeable_task_ids,
        freeze_horizon_seconds,
        runtime_evidence,
    ) = _runtime_partial_scope(
        event,
        active_plan,
        sql,
        live,
        affected_tasks,
        scope,
    )
    evidence.extend(runtime_evidence)
    if (
        event.event_type == "ROBOT_FAILED"
        and robot_failure_recovery.get("strategy") == "HANDOVER_SECURED_LOAD"
    ):
        replacement_ids = set(robot_failure_recovery.get("replace_task_ids") or [])
        recovery_ids = set(robot_failure_recovery.get("recovery_task_ids") or [])
        changeable_task_ids = sorted(
            set(changeable_task_ids) | replacement_ids | recovery_ids
        )
        frozen_task_ids = sorted(
            set(frozen_task_ids) - replacement_ids - recovery_ids
        )
        completed_task_ids = sorted(
            set(completed_task_ids) - replacement_ids - recovery_ids
        )
        evidence.append(
            f"적재물 인계용 대체 작업 {len(recovery_ids)}건을 변경 가능 범위에 추가했습니다."
        )
    if scope != "NO_REPLAN" and not changeable_task_ids:
        scope = "NO_REPLAN"
        evidence.append("완료·동결 구간을 제외한 변경 가능 작업이 없어 재계획하지 않습니다.")

    approval_required = (
        event.execution_context == "REAL"
        and (
            risk in {"MEDIUM", "HIGH"}
            or event.event_type in {"ROBOT_FAILED", "PATH_BLOCKED", "LOW_BATTERY"}
        )
        and scope != "NO_REPLAN"
    )
    state_overrides = _robot_state_override(event, sql, live)
    state_signature = state_overrides.get(event.robot_id, {})
    signature_parts = [
        event.event_type,
        event.robot_id,
        ",".join(sorted(changeable_task_ids or affected_tasks)),
        ",".join(str(value) for value in sorted(affected_nodes)),
        ",".join(sorted(affected_edges)),
        str(state_signature.get("node_id") or ""),
        str(state_signature.get("battery") or ""),
        str(robot_failure_recovery.get("strategy") or ""),
        str(robot_failure_recovery.get("load_state") or ""),
    ]
    return EventImpactAnalysis(
        event_id=event.event_id,
        trigger_type=event.event_type,
        trigger_source=event.execution_context,
        affected_robot_ids=sorted(affected_robots),
        affected_task_ids=sorted(affected_tasks),
        affected_node_ids=sorted(affected_nodes),
        affected_edge_ids=sorted(affected_edges),
        recommended_scope=scope,
        risk_level=risk,
        approval_required=approval_required,
        evidence=evidence,
        active_plan_version=(
            live.get("active_plan_version") or active_plan.get("plan_version")
        ),
        completed_task_ids=completed_task_ids,
        frozen_task_ids=frozen_task_ids,
        changeable_task_ids=changeable_task_ids,
        freeze_horizon_seconds=freeze_horizon_seconds,
        robot_state_overrides=state_overrides,
        robot_failure_recovery=robot_failure_recovery,
        failure_signature="|".join(signature_parts),
    )

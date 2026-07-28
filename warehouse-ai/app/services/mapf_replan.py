from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAPF_REPLAN_VERSION = "p16.5.12.1"

_NON_RETRYABLE_TOKENS = (
    "지원하지 않는 ROUTING_BACKEND",
    "ROUTING_BACKEND=mapf에는 MAPF_URL",
    "cuOpt 결과의 robot_id가 Snapshot에 없습니다",
    "ROBOT_TASK_DEPENDENCY_CYCLE",
    "RESOURCE_DEPENDENCY_ORDER_CONFLICT",
    "EXECUTION_DEPENDENCY_CYCLE",
)

_GLOBAL_TOPOLOGY_TOKENS = (
    "INBOUND_ROUTE_NOT_FOUND",
    "NO_PATH",
    "PATH_NOT_FOUND",
    "DISCONNECTED",
    "INVALID_OR_CLOSED_NODE",
    "INVALID_OR_CLOSED_EDGE",
    "그래프가 연결",
    "이동 가능한 통로",
)

_LOCAL_CONFLICT_TOKENS = (
    "RESERVATION_CONFLICT",
    "예약 충돌",
    "활성화될 수 없습니다",
    "EMPTY_ROUTE_SEGMENT",
    "RESOURCE_SCHEDULER_DID_NOT_CONVERGE",
    "RESOURCE_ROUTE_ENERGY_RECONCILIATION_DID_NOT_CONVERGE",
    "SHARED_RESOURCE_SCHEDULING_FAILED",
    "RESOURCE_DELAY_HARD_WINDOW_VIOLATION",
    "LONG_IDLE_ON_PROHIBITED_NODE",
    "EDGE_SWAP",
    "VERTEX",
    "충돌",
)

_BACKEND_FAILURE_TOKENS = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "HTTPStatusError",
    "Connection refused",
    "외부 MAPF 실패",
)


def _scheduled_rows(cuopt_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(cuopt_plan, dict):
        return []
    rows = cuopt_plan.get("scheduled_tasks")
    return rows if isinstance(rows, list) else []


def _extract_known_ids(
    reason: str,
    *,
    problem: dict[str, Any] | None,
    cuopt_plan: dict[str, Any] | None,
) -> tuple[list[str], list[str], list[int]]:
    rows = _scheduled_rows(cuopt_plan)
    known_task_ids = [str(row.get("task_id")) for row in rows if row.get("task_id")]
    known_robot_ids = {
        str(row.get("robot_id")) for row in rows if row.get("robot_id")
    }
    if isinstance(problem, dict):
        known_robot_ids.update(
            str(row.get("robot_id"))
            for row in problem.get("robots", [])
            if row.get("robot_id")
        )

    task_ids = sorted(task_id for task_id in known_task_ids if task_id in reason)
    robot_ids = sorted(robot_id for robot_id in known_robot_ids if robot_id in reason)

    # Some routing errors contain only a task ID. Resolve its robot deterministically.
    if task_ids:
        task_set = set(task_ids)
        robot_ids = sorted(
            set(robot_ids)
            | {
                str(row.get("robot_id"))
                for row in rows
                if str(row.get("task_id")) in task_set and row.get("robot_id")
            }
        )

    node_ids = sorted(
        {
            int(value)
            for value in re.findall(
                r"(?:노드|충전소|node(?:_id)?)[^0-9]{0,8}(\d+)",
                reason,
                flags=re.IGNORECASE,
            )
        }
    )
    return robot_ids, task_ids, node_ids


def _stable_signature(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_mapf_failure(
    reason: str,
    *,
    error_code: str,
    routing_backend: str,
    problem: dict[str, Any] | None,
    cuopt_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """Turn an unstructured routing exception into deterministic replan evidence."""

    text = str(reason or "ROUTE_FAILED")
    robot_ids, task_ids, node_ids = _extract_known_ids(
        text,
        problem=problem,
        cuopt_plan=cuopt_plan,
    )

    if any(token in text for token in _NON_RETRYABLE_TOKENS):
        category = "CONFIGURATION_OR_CONTRACT"
        code = "MAPF_CONFIGURATION_FAILURE"
        retryable = False
        recommended_scope = "NO_REPLAN"
    elif any(token in text for token in _BACKEND_FAILURE_TOKENS):
        category = "BACKEND_UNAVAILABLE"
        code = "MAPF_BACKEND_UNAVAILABLE"
        retryable = False
        recommended_scope = "NO_REPLAN"
    elif any(token in text for token in _GLOBAL_TOPOLOGY_TOKENS):
        category = "TOPOLOGY"
        code = "MAPF_TOPOLOGY_FAILURE"
        retryable = True
        recommended_scope = "GLOBAL_REPLAN"
    elif any(token in text for token in _LOCAL_CONFLICT_TOKENS):
        category = "RESERVATION_OR_RESOURCE_CONFLICT"
        if robot_ids or task_ids:
            code = "MAPF_LOCAL_CONFLICT"
            retryable = True
            recommended_scope = "LOCAL_REPLAN"
        else:
            # Without a concrete affected target, freezing arbitrary tasks would
            # be unsafe. Widen deterministically to the whole future plan.
            code = "MAPF_GLOBAL_CONFLICT"
            retryable = True
            recommended_scope = "GLOBAL_REPLAN"
    else:
        category = "UNCLASSIFIED"
        code = "MAPF_UNCLASSIFIED_FAILURE"
        retryable = False
        recommended_scope = "NO_REPLAN"

    signature_payload = {
        "version": MAPF_REPLAN_VERSION,
        "category": category,
        "code": code,
        "routing_backend": routing_backend,
        "error_code": error_code,
        "robot_ids": robot_ids,
        "task_ids": task_ids,
        "node_ids": node_ids,
        # Keep only the stable machine-readable prefix. Volatile UUIDs are
        # already represented through task_ids above.
        "reason_prefix": text.split(":", 1)[0][:160],
    }
    return {
        "version": MAPF_REPLAN_VERSION,
        "category": category,
        "code": code,
        "error_code": error_code,
        "routing_backend": routing_backend,
        "reason": text,
        "retryable": retryable,
        "recommended_scope": recommended_scope,
        "affected_robot_ids": robot_ids,
        "affected_task_ids": task_ids,
        "affected_node_ids": node_ids,
        "failure_signature": _stable_signature(signature_payload),
    }


def build_mapf_replan_policy(
    *,
    attempt: int,
    scope: str,
    affected_robot_ids: list[str],
    escalated_from_local: bool = False,
) -> dict[str, Any]:
    """Create a bounded, deterministic routing-order perturbation policy."""

    normalized_scope = str(scope or "LOCAL_REPLAN").upper()
    affected = sorted({str(value) for value in affected_robot_ids if value})
    if normalized_scope == "GLOBAL_REPLAN" or escalated_from_local:
        strategy = "ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER"
        base_stagger_steps = min(3, max(1, int(attempt)))
    else:
        strategy = "AFFECTED_ROBOTS_FIRST"
        base_stagger_steps = 0
    return {
        "version": MAPF_REPLAN_VERSION,
        "enabled": True,
        "attempt": int(attempt),
        "scope": normalized_scope,
        "strategy": strategy,
        "affected_robot_ids": affected,
        "base_stagger_steps": base_stagger_steps,
        "escalated_from_local": bool(escalated_from_local),
        "max_global_escalation_count": 1,
    }


def order_robot_ids(
    robot_ids: list[str],
    policy: dict[str, Any] | None,
) -> list[str]:
    baseline = list(dict.fromkeys(str(value) for value in robot_ids))
    if not policy or not policy.get("enabled") or len(baseline) <= 1:
        return baseline
    strategy = str(policy.get("strategy") or "")
    affected = set(str(value) for value in policy.get("affected_robot_ids", []))
    if strategy == "AFFECTED_ROBOTS_FIRST":
        return [value for value in baseline if value in affected] + [
            value for value in baseline if value not in affected
        ]
    if strategy == "ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER":
        offset = int(policy.get("attempt") or 1) % len(baseline)
        return baseline[offset:] + baseline[:offset]
    return baseline


def start_delay_steps(robot_id: str, ordered_robot_ids: list[str], policy: dict[str, Any] | None) -> int:
    if not policy or not policy.get("enabled"):
        return 0
    if str(policy.get("strategy") or "") != "ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER":
        return 0
    base = max(0, int(policy.get("base_stagger_steps") or 0))
    try:
        index = ordered_robot_ids.index(str(robot_id))
    except ValueError:
        return 0
    return index * base

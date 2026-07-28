from __future__ import annotations

import heapq
import math
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import httpx

from app.models import AtomicTask, CuOptPlan
from app.services.local_optimizer import LocalOptimizer
from app.time_utils import planning_reference_time


class CuOptRestError(RuntimeError):
    """NVIDIA managed cuOpt REST 호출 또는 응답 변환 실패입니다."""


@dataclass(frozen=True)
class CuOptRestResult:
    plan: CuOptPlan
    request_id: str | None
    solver_status: int
    solution_cost: float | None
    raw_task_order_by_robot: dict[str, list[str]]


def _graph(problem: dict[str, Any]) -> dict[int, list[tuple[int, float, float]]]:
    graph: dict[int, list[tuple[int, float, float]]] = {}
    closed_nodes = {
        int(row["node_id"])
        for row in problem.get("temporary_closures", [])
        if row.get("node_id") is not None
    }
    closed_edges: set[tuple[int, int]] = set()
    for row in problem.get("temporary_closures", []):
        if row.get("from_node") is None or row.get("to_node") is None:
            continue
        edge = (int(row["from_node"]), int(row["to_node"]))
        closed_edges.add(edge)
        if bool(row.get("bidirectional")) or str(row.get("direction") or "").upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            closed_edges.add((edge[1], edge[0]))

    for edge in problem.get("edges", []):
        start = int(edge["from_node"])
        end = int(edge["to_node"])
        if start in closed_nodes or end in closed_nodes or (start, end) in closed_edges:
            continue
        distance = max(0.0, float(edge.get("distance") or 0.0))
        seconds = max(
            0.0,
            float(edge.get("travel_seconds") or edge.get("travel_time") or distance),
        )
        graph.setdefault(start, []).append((end, distance, seconds))
        if str(edge.get("direction") or "").upper() in {"BOTH", "BIDIRECTIONAL"}:
            if (end, start) not in closed_edges:
                graph.setdefault(end, []).append((start, distance, seconds))
    return graph


def _shortest(
    graph: dict[int, list[tuple[int, float, float]]],
    start: int,
    goal: int,
) -> tuple[float, float] | None:
    if start == goal:
        return 0.0, 0.0
    queue: list[tuple[float, float, int]] = [(0.0, 0.0, start)]
    best: dict[int, tuple[float, float]] = {start: (0.0, 0.0)}
    while queue:
        distance, seconds, node = heapq.heappop(queue)
        if node == goal:
            return distance, seconds
        if best.get(node) != (distance, seconds):
            continue
        for neighbor, edge_distance, edge_seconds in graph.get(node, []):
            candidate = (distance + edge_distance, seconds + edge_seconds)
            if candidate < best.get(neighbor, (math.inf, math.inf)):
                best[neighbor] = candidate
                heapq.heappush(queue, (*candidate, neighbor))
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _seconds_from_reference(value: Any, reference: datetime) -> int | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    return max(0, math.floor((parsed - reference).total_seconds()))


def _task_location(task: AtomicTask) -> int:
    if task.action in {"PICK", "CHARGE"} and task.source_candidates:
        return int(sorted(set(task.source_candidates))[0])
    if task.target_candidates:
        return int(sorted(set(task.target_candidates))[0])
    if task.source_candidates:
        return int(sorted(set(task.source_candidates))[0])
    raise CuOptRestError(f"CUOPT_TASK_LOCATION_MISSING:{task.task_id}")


def build_cuopt_routing_payload(
    problem: dict[str, Any],
    *,
    solver_time_limit_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """내부 최적화 문제를 managed cuOpt VRP 입력으로 변환합니다.

    cuOpt는 로봇별 작업 배정과 순서를 결정합니다. P16.5.10에서는 1차
    일정에서 선택된 충전 방문도 명시적 CHARGE 작업으로 포함합니다.
    충전량·공유 자원 용량·최종 배터리는 LocalOptimizer와 창고 스케줄러가
    정규화합니다.
    """

    tasks = [AtomicTask.model_validate(row) for row in problem.get("tasks", [])]
    robots = [
        row
        for row in sorted(problem.get("robots", []), key=lambda item: str(item["robot_id"]))
        if row.get("node_id") is not None
        and str(row.get("status") or "").upper()
        not in {"FAILED", "ROBOT_FAILED", "OFFLINE", "MAINTENANCE", "DISABLED"}
        and str(row.get("live_status") or "").upper()
        not in {"FAILED", "ROBOT_FAILED", "OFFLINE", "MAINTENANCE", "DISABLED"}
    ]
    if not tasks:
        raise CuOptRestError("CUOPT_NO_TASKS")
    if not robots:
        raise CuOptRestError("CUOPT_NO_AVAILABLE_ROBOTS")

    task_locations_by_id = {task.task_id: _task_location(task) for task in tasks}
    relevant_nodes = sorted(
        {
            *(int(row["node_id"]) for row in robots),
            *task_locations_by_id.values(),
        }
    )
    node_to_index = {node_id: index for index, node_id in enumerate(relevant_nodes)}
    graph = _graph(problem)
    unreachable_penalty = 1_000_000_000.0
    distance_matrix: list[list[float]] = []
    travel_matrix: list[list[float]] = []
    for start in relevant_nodes:
        distance_row: list[float] = []
        travel_row: list[float] = []
        for end in relevant_nodes:
            shortest = _shortest(graph, start, end)
            if shortest is None:
                distance_row.append(unreachable_penalty)
                travel_row.append(unreachable_penalty)
            else:
                distance_row.append(round(float(shortest[0]), 6))
                travel_row.append(round(float(shortest[1]), 6))
        distance_matrix.append(distance_row)
        travel_matrix.append(travel_row)

    weights = problem.get("weights") or {}
    distance_weight = float(weights.get("total_distance", 1.0))
    energy_weight = float(weights.get("energy", 1.0))
    congestion_weight = float(weights.get("congestion", 1.0))
    charger_visit_weight = float(weights.get("charger_visit", 1.0))
    energy_per_distance = max(0.0, float(problem.get("energy_per_distance") or 0.0))
    congestion_penalty = max(0.0, float(problem.get("congestion_penalty_steps") or 0.0))
    congestion_nodes = {int(value) for value in problem.get("congestion_node_ids", [])}
    node_by_id = {
        int(row["node_id"]): row
        for row in problem.get("nodes", [])
        if row.get("node_id") is not None
    }
    composite_cost_matrix: list[list[float]] = []
    for row_index, start in enumerate(relevant_nodes):
        composite_row: list[float] = []
        for column_index, end in enumerate(relevant_nodes):
            distance = float(distance_matrix[row_index][column_index])
            if distance >= unreachable_penalty:
                composite_row.append(unreachable_penalty)
                continue
            value = distance * (distance_weight + energy_per_distance * energy_weight)
            if end in congestion_nodes:
                value += congestion_penalty * congestion_weight
            end_row = node_by_id.get(end, {})
            if str(end_row.get("node_type") or "").upper() == "CHARGER":
                configured_cost = end_row.get("charging_cost")
                value += charger_visit_weight
                if configured_cost not in (None, ""):
                    value += max(0.0, float(configured_cost)) * charger_visit_weight
            composite_row.append(round(value, 6))
        composite_cost_matrix.append(composite_row)

    reference = planning_reference_time(problem)
    horizon = max(
        86_400,
        max(
            (
                _seconds_from_reference(task.latest_finish, reference) or 0
                for task in tasks
            ),
            default=0,
        )
        + 3_600,
    )
    task_time_windows: list[list[int]] = []
    for task in tasks:
        earliest = _seconds_from_reference(task.earliest_start, reference) or 0
        latest = _seconds_from_reference(task.latest_finish, reference) or horizon
        task_time_windows.append([earliest, max(earliest, latest)])

    robot_ids = [str(row["robot_id"]) for row in robots]
    robot_index = {robot_id: index for index, robot_id in enumerate(robot_ids)}
    order_vehicle_match: list[dict[str, Any]] = []
    for order_id, task in enumerate(tasks):
        if task.assigned_robot_id and task.assigned_robot_id in robot_index:
            order_vehicle_match.append(
                {
                    "order_id": order_id,
                    "vehicle_ids": [robot_index[task.assigned_robot_id]],
                }
            )

    task_index = {task.task_id: index for index, task in enumerate(tasks)}
    pickup_delivery_pairs: list[list[int]] = []
    for task in tasks:
        for predecessor in task.predecessors:
            if predecessor not in task_index:
                continue
            predecessor_task = tasks[task_index[predecessor]]
            if predecessor_task.action == "PICK" and task.action == "DROP":
                pickup_delivery_pairs.append([task_index[predecessor], task_index[task.task_id]])

    demands = [
        int(task.quantity)
        if task.action == "PICK"
        else -int(task.quantity)
        if task.action == "DROP"
        else 0
        for task in tasks
    ]
    # NVIDIA API Catalog의 현재 OptimizedRoutingData 스키마는 task_data에
    # 사용자 정의 task_ids, task priorities, mandatory_task_ids를 허용하지
    # 않는 배포가 있습니다. 내부 task_id는 응답의 task index를 통해 다시
    # 매핑하고, 작업 누락 여부는 응답 후처리에서 엄격하게 검증합니다.
    service_step_seconds = max(1, int(problem.get("time_step_seconds") or 1))
    explicit_charge_specs = problem.get("explicit_charge_task_specs") or {}

    def service_seconds(task: AtomicTask) -> int:
        if task.action in {"PICK", "DROP"}:
            return service_step_seconds
        if task.action == "CHARGE":
            spec = explicit_charge_specs.get(task.task_id) or {}
            return max(service_step_seconds, int(spec.get("charge_duration_seconds") or 0))
        return 0

    task_data: dict[str, Any] = {
        "task_locations": [node_to_index[task_locations_by_id[task.task_id]] for task in tasks],
        "task_time_windows": task_time_windows,
        "service_times": [service_seconds(task) for task in tasks],
    }
    if any(demands):
        task_data["demand"] = [demands]
    if order_vehicle_match:
        task_data["order_vehicle_match"] = order_vehicle_match
    pickup_delivery_pairs_enabled = not bool(
        problem.get("cuopt_disable_pickup_delivery_pairs")
    )
    if pickup_delivery_pairs and pickup_delivery_pairs_enabled:
        task_data["pickup_and_delivery_pairs"] = pickup_delivery_pairs

    payload = {
        "cost_matrix_data": {"data": {"0": composite_cost_matrix}},
        "travel_time_matrix_data": {"data": {"0": travel_matrix}},
        "task_data": task_data,
        "fleet_data": {
            "vehicle_locations": [
                [node_to_index[int(row["node_id"])], node_to_index[int(row["node_id"])]]
                for row in robots
            ],
            "vehicle_ids": robot_ids,
            "vehicle_time_windows": [[0, horizon] for _ in robots],
            "drop_return_trips": [True for _ in robots],
            **(
                {
                    "capacities": [[
                        max(
                            0,
                            int(float(row.get("max_load") or 0))
                            - int(float(row.get("current_load") or 0)),
                        )
                        for row in robots
                    ]]
                }
                if any(demands)
                else {}
            ),
        },
        "solver_config": {
            "time_limit": max(1, int(solver_time_limit_seconds)),
        },
    }
    context = {
        "tasks": tasks,
        "task_ids": [task.task_id for task in tasks],
        "robot_ids": robot_ids,
        "relevant_nodes": relevant_nodes,
        "node_to_index": node_to_index,
        "explicit_charge_task_ids": [
            task.task_id for task in tasks if task.task_id in explicit_charge_specs
        ],
        "cuopt_objective_contract": {
            "matrix_mode": "DISTANCE_ENERGY_CONGESTION_CHARGER_VISIT_COMPOSITE",
            "distance_weight": distance_weight,
            "energy_weight": energy_weight,
            "congestion_weight": congestion_weight,
            "charger_visit_weight": charger_visit_weight,
            "charge_service_times_included": bool(explicit_charge_specs),
            "pickup_delivery_pairs_enabled": pickup_delivery_pairs_enabled,
            "pickup_delivery_pair_count": (
                len(pickup_delivery_pairs)
                if pickup_delivery_pairs_enabled
                else 0
            ),
            "pickup_delivery_pairs_omitted_reason": (
                "STANDALONE_CHARGE_MOVE_TASKS_REQUIRE_ROBOT_BOUND_SECOND_PASS"
                if pickup_delivery_pairs and not pickup_delivery_pairs_enabled
                else None
            ),
        },
    }
    return payload, context


def _extract_route_assignments(
    solution: dict[str, Any],
    *,
    expected_task_ids: list[str],
    robot_ids: list[str],
) -> tuple[dict[str, list[str]], int, float | None]:
    response = solution.get("response") or {}
    solver_response = response.get("solver_response") or {}
    if not solver_response and response.get("solver_infeasible_response"):
        solver_response = response["solver_infeasible_response"]
    status = int(solver_response.get("status", -1))
    if status != 0:
        raise CuOptRestError(f"CUOPT_SOLVER_STATUS_{status}")
    dropped = solver_response.get("dropped_tasks") or {}
    dropped_ids = [str(value) for value in dropped.get("task_id", [])]
    if dropped_ids:
        raise CuOptRestError("CUOPT_DROPPED_TASKS:" + ",".join(dropped_ids))

    expected = set(expected_task_ids)
    assignments: dict[str, list[str]] = {}
    seen: set[str] = set()
    for vehicle_key, route in (solver_response.get("vehicle_data") or {}).items():
        raw_robot_id = str(vehicle_key)
        if raw_robot_id in robot_ids:
            robot_id = raw_robot_id
        else:
            try:
                robot_id = robot_ids[int(vehicle_key)]
            except (ValueError, IndexError):
                robot_id = raw_robot_id
        ordered: list[str] = []
        for task_id in route.get("task_id", []):
            task_text = str(task_id)
            if task_text.lower() == "depot":
                continue
            if task_text not in expected:
                try:
                    task_text = expected_task_ids[int(task_text)]
                except (ValueError, IndexError):
                    continue
            if task_text in expected and task_text not in seen:
                ordered.append(task_text)
                seen.add(task_text)
        if ordered:
            assignments[robot_id] = ordered

    missing = sorted(expected - seen)
    if missing:
        raise CuOptRestError("CUOPT_MISSING_TASK_ASSIGNMENTS:" + ",".join(missing))
    solution_cost = solver_response.get("solution_cost")
    return assignments, status, float(solution_cost) if solution_cost is not None else None


def _apply_assignments(
    problem: dict[str, Any],
    assignments: dict[str, list[str]],
) -> dict[str, Any]:
    """Apply the managed cuOpt result before warehouse-specific scheduling.

    By default the historical behaviour is preserved: cuOpt vehicle assignments
    become hard robot constraints for the local schedule normalizer.  A daily
    multi-robot plan may opt into ``allow_local_robot_rebalance``.  In that
    mode cuOpt still supplies the global visit order, while unconstrained work
    pairs are redistributed locally so overlapping windows can run in parallel.

    Explicit user assignments, preserved/frozen work and fixed task scope are
    never relaxed by this compatibility layer. A changeable LOCAL_REPLAN task
    may remain robot-bound while its timing stays unfrozen so the local
    optimizer can insert safety charging before the affected chain.
    """

    normalized = deepcopy(problem)
    task_by_id = {
        str(row["task_id"]): dict(row)
        for row in normalized.get("tasks", [])
        if row.get("task_id")
    }
    fixed_ids = {str(value) for value in normalized.get("fixed_task_ids", [])}
    changeable_ids = {
        str(value) for value in normalized.get("changeable_task_ids", [])
    }
    plan_mode = str(normalized.get("plan_mode") or "")
    rebalance_requested = bool(normalized.get("allow_local_robot_rebalance"))
    available_robot_count = len(normalized.get("robots", []))
    rebalance_enabled = rebalance_requested and available_robot_count > 1
    relaxed_task_ids: list[str] = []
    fixed_task_ids: list[str] = []
    changeable_robot_bound_task_ids: list[str] = []

    sequence = 1
    for robot_id, task_ids in assignments.items():
        for task_id in task_ids:
            row = task_by_id[task_id]
            work_id = str(row.get("work_id") or "")
            scope_fixed = task_id in fixed_ids or work_id in fixed_ids
            scope_changeable = task_id in changeable_ids or work_id in changeable_ids
            explicitly_constrained = bool(
                row.get("assigned_robot_id")
                or row.get("frozen")
                or scope_fixed
            )
            row["priority"] = min(100, sequence)
            sequence += 1

            # A LOCAL_REPLAN may pin the affected chain to the reporting robot
            # without freezing its timing.  Treating assigned_robot_id as an
            # automatic freeze blocks LocalOptimizer from inserting a safety
            # CHARGE before the changeable PICK/DROP chain.  Scope-fixed tasks
            # still win if malformed input marks a task both fixed and
            # changeable.
            if plan_mode == "LOCAL_REPLAN" and scope_changeable and not scope_fixed:
                row["assigned_robot_id"] = str(
                    row.get("assigned_robot_id") or robot_id
                )
                row["frozen"] = False
                changeable_robot_bound_task_ids.append(task_id)
                continue

            if rebalance_enabled and not explicitly_constrained:
                # Keep the cuOpt ordering, but do not turn a managed solver's
                # single-vehicle choice into a warehouse-wide hard lock.
                row.pop("assigned_robot_id", None)
                row["frozen"] = False
                relaxed_task_ids.append(task_id)
                continue

            row["assigned_robot_id"] = str(
                row.get("assigned_robot_id") or robot_id
            )
            # LocalOptimizer may recalculate exact timing/charging while the
            # vehicle identity remains fixed by cuOpt or by an explicit rule.
            row["frozen"] = True
            fixed_task_ids.append(task_id)

    normalized["tasks"] = [
        task_by_id[str(row["task_id"])] for row in normalized.get("tasks", [])
    ]
    normalized["cuopt_assignment_application"] = {
        "mode": (
            "GLOBAL_ORDER_LOCAL_MULTI_ROBOT_REBALANCE"
            if rebalance_enabled
            else "HARD_CUOPT_VEHICLE_ASSIGNMENT"
        ),
        "relaxed_task_ids": relaxed_task_ids,
        "fixed_task_ids": fixed_task_ids,
        "changeable_robot_bound_task_ids": changeable_robot_bound_task_ids,
        "raw_assignments": assignments,
    }
    return normalized




DEFAULT_CUOPT_REST_URL = "https://optimize.api.nvidia.com/v1/nvidia/cuopt"
DEFAULT_CUOPT_STATUS_URL = "https://optimize.api.nvidia.com/v1/status/{request_id}"
MAX_INLINE_REQUEST_BYTES = 240_000


def _request_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("reqId") or payload.get("requestId") or payload.get("request_id")
    return str(value) if value else None


def _normalize_solution(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the response envelopes used by NVIDIA API Catalog and NVCF."""
    if "response" in payload:
        return payload
    if "solver_response" in payload or "solver_infeasible_response" in payload:
        return {"response": payload, **({"reqId": _request_id(payload)} if _request_id(payload) else {})}
    for key in ("data", "result", "output"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            normalized = _normalize_solution(nested)
            request_id = _request_id(payload)
            if request_id and not normalized.get("reqId"):
                normalized["reqId"] = request_id
            return normalized
    return payload




_EXTRA_FORBIDDEN_PATH_RE = re.compile(
    r"(?m)^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\n"
    r"\s+Extra inputs are not permitted"
)


def _extra_forbidden_paths(response: httpx.Response) -> list[str]:
    """Extract Pydantic extra_forbidden field paths from a 422 response."""
    try:
        body = response.json()
    except Exception:
        return []
    if not isinstance(body, dict):
        return []
    message = body.get("error") or body.get("detail") or ""
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    paths: list[str] = []
    for path in _EXTRA_FORBIDDEN_PATH_RE.findall(str(message)):
        if path not in paths:
            paths.append(path)
    return paths


def _remove_payload_path(payload: dict[str, Any], path: str) -> bool:
    """Remove a dotted field path reported relative to OptimizedRoutingData."""
    parts = [part for part in path.split(".") if part]
    if parts and parts[0] == "data":
        parts = parts[1:]
    if not parts:
        return False
    cursor: Any = payload
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return False
    del cursor[parts[-1]]
    return True


def _request_size_bytes(request_body: dict[str, Any]) -> int:
    return len(
        json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _response_excerpt(response: httpx.Response) -> str:
    try:
        value = response.json()
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = response.text
    return text.replace("\n", " ")[:2000]


class CuOptRestOptimizer:
    """NVIDIA API Catalog의 managed cuOpt REST 엔드포인트 어댑터입니다.

    별도 cuOpt Python 패키지, CUDA 또는 로컬 GPU가 필요하지 않습니다.
    cuOpt는 로봇 배정과 방문 순서를 결정하고 기존 LocalOptimizer가
    배터리·충전·작업 시간을 창고 내부 계약으로 정규화합니다.
    """

    def __init__(
        self,
        *,
        api_key: str,
        rest_url: str = DEFAULT_CUOPT_REST_URL,
        status_url: str = DEFAULT_CUOPT_STATUS_URL,
        request_timeout_seconds: float = 30.0,
        poll_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
        solver_time_limit_seconds: int = 10,
        local_optimizer: LocalOptimizer,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key.strip():
            raise CuOptRestError("CUOPT_API_KEY_MISSING")
        self.api_key = api_key.strip()
        self.rest_url = (rest_url or DEFAULT_CUOPT_REST_URL).strip()
        self.status_url = (status_url or DEFAULT_CUOPT_STATUS_URL).strip()
        self.request_timeout_seconds = request_timeout_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.solver_time_limit_seconds = solver_time_limit_seconds
        self.local_optimizer = local_optimizer
        self.client_factory = client_factory

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _client(self) -> Any:
        factory = self.client_factory or httpx.Client
        try:
            return factory(timeout=self.request_timeout_seconds, headers=self.headers)
        except TypeError:
            return factory()

    def _decode(self, response: httpx.Response, *, operation: str) -> dict[str, Any]:
        if response.status_code not in {200, 202}:
            raise CuOptRestError(
                f"CUOPT_{operation}_HTTP_{response.status_code}:{_response_excerpt(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CuOptRestError(f"CUOPT_{operation}_INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise CuOptRestError(f"CUOPT_{operation}_INVALID_RESPONSE_TYPE")
        return payload

    def _poll(self, client: Any, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_solution(payload)
        if "response" in normalized:
            return normalized
        request_id = _request_id(payload)
        if not request_id:
            raise CuOptRestError("CUOPT_PENDING_RESPONSE_WITHOUT_REQUEST_ID")
        deadline = time.monotonic() + max(1.0, self.poll_timeout_seconds)
        url = self.status_url.format(request_id=request_id)
        while time.monotonic() < deadline:
            time.sleep(max(0.1, self.poll_interval_seconds))
            try:
                response = client.get(url)
            except Exception as exc:
                raise CuOptRestError(
                    f"CUOPT_STATUS_REQUEST_FAILED:{type(exc).__name__}"
                ) from exc
            result = self._decode(response, operation="STATUS")
            normalized = _normalize_solution(result)
            if response.status_code == 200 or "response" in normalized:
                if request_id and not normalized.get("reqId"):
                    normalized["reqId"] = request_id
                return normalized
        raise CuOptRestError(f"CUOPT_POLL_TIMEOUT:reqId={request_id}")

    def optimize(self, problem: dict[str, Any]) -> CuOptRestResult:
        payload, context = build_cuopt_routing_payload(
            problem,
            solver_time_limit_seconds=self.solver_time_limit_seconds,
        )
        request_body = {
            "action": "cuOpt_OptimizedRouting",
            "data": payload,
            "client_version": "custom",
        }
        request_size = _request_size_bytes(request_body)
        if request_size > MAX_INLINE_REQUEST_BYTES:
            raise CuOptRestError(
                f"CUOPT_INLINE_PAYLOAD_TOO_LARGE:{request_size}>{MAX_INLINE_REQUEST_BYTES}"
            )

        compatibility_removed_fields: list[str] = []
        schema_retry_count = 0
        client = self._client()
        close = getattr(client, "close", None)
        try:
            solution: dict[str, Any] | None = None
            for submit_attempt in range(3):
                try:
                    response = client.post(self.rest_url, json=request_body)
                except Exception as exc:
                    raise CuOptRestError(
                        f"CUOPT_SUBMIT_REQUEST_FAILED:{type(exc).__name__}"
                    ) from exc

                if response.status_code == 422 and submit_attempt < 2:
                    removed_now: list[str] = []
                    for path in _extra_forbidden_paths(response):
                        if _remove_payload_path(payload, path):
                            removed_now.append(path)
                    if removed_now:
                        compatibility_removed_fields.extend(removed_now)
                        schema_retry_count += 1
                        request_body["data"] = payload
                        request_size = _request_size_bytes(request_body)
                        if request_size > MAX_INLINE_REQUEST_BYTES:
                            raise CuOptRestError(
                                f"CUOPT_INLINE_PAYLOAD_TOO_LARGE:{request_size}>{MAX_INLINE_REQUEST_BYTES}"
                            )
                        continue

                solution = self._decode(response, operation="SUBMIT")
                if response.status_code == 202 or "response" not in _normalize_solution(solution):
                    solution = self._poll(client, solution)
                else:
                    solution = _normalize_solution(solution)
                break
            if solution is None:
                raise CuOptRestError("CUOPT_SUBMIT_RETRY_EXHAUSTED")
        finally:
            if callable(close):
                close()

        assignments, status, solution_cost = _extract_route_assignments(
            solution,
            expected_task_ids=context["task_ids"],
            robot_ids=context["robot_ids"],
        )
        normalized_problem = _apply_assignments(problem, assignments)
        plan = self.local_optimizer.optimize(normalized_problem)
        if plan.unassigned_task_ids:
            raise CuOptRestError(
                "CUOPT_POSTPROCESS_UNASSIGNED:" + ",".join(plan.unassigned_task_ids)
            )
        request_id = _request_id(solution)
        metadata = dict(plan.metadata)
        metadata.update(
            {
                "backend": "cuopt_rest",
                "primary_solver": "NVIDIA_CUOPT_MANAGED_REST",
                "schedule_postprocessor": "LOCAL_WAREHOUSE_NORMALIZER",
                "cuopt_request_id": request_id,
                "cuopt_solver_status": status,
                "cuopt_solution_cost": solution_cost,
                "cuopt_task_order_by_robot": assignments,
                "cuopt_rest_url": self.rest_url,
                "cuopt_request_bytes": request_size,
                "cuopt_payload_schema": "NVIDIA_OPTIMIZED_ROUTING_P16_5_10_EXPLICIT_CHARGE_VISITS",
                "cuopt_explicit_charge_visit_count": len(
                    context.get("explicit_charge_task_ids", [])
                ),
                "cuopt_explicit_charge_task_ids": list(
                    context.get("explicit_charge_task_ids", [])
                ),
                "cuopt_objective_contract": dict(
                    context.get("cuopt_objective_contract", {})
                ),
                "cuopt_schema_retry_count": schema_retry_count,
                "cuopt_schema_removed_fields": compatibility_removed_fields,
            }
        )
        plan.metadata = metadata
        return CuOptRestResult(
            plan=plan,
            request_id=request_id,
            solver_status=status,
            solution_cost=solution_cost,
            raw_task_order_by_robot=assignments,
        )


# P16.4 import compatibility. These aliases no longer load cuopt-thin-client.
CuOptManagedError = CuOptRestError
ManagedCuOptResult = CuOptRestResult
CuOptManagedOptimizer = CuOptRestOptimizer
build_managed_routing_payload = build_cuopt_routing_payload

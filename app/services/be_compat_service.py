"""Route and assignment services for the unmodified Spring BE API contract."""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from math import inf
from typing import Iterable
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.domain.be_compat import (
    BeEdgeInput,
    BeGraphSnapshot,
    BeOptimizationRequest,
    BeOptimizationResponse,
    BeReoptimizationRequest,
    BeReoptimizationResponse,
    BeRobotRoute,
    BeRobotStateInput,
    BeTaskAssignment,
    BeTaskInput,
)
from app.repositories.be_compat_repository import BeCompatRepository
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository


class BeCompatRoutingError(RuntimeError):
    """Raised when the numeric graph cannot satisfy a requested route."""


@dataclass(frozen=True)
class _Arc:
    to_node: int
    distance: float
    edge_id: int


@dataclass
class _RobotWork:
    robot: BeRobotStateInput
    current_node: int
    node_path: list[int] = field(default_factory=list)
    total_distance: float = 0.0
    assigned_task_ids: list[int] = field(default_factory=list)


class _DirectedGraph:
    def __init__(
        self,
        snapshot: BeGraphSnapshot,
        *,
        blocked_edge_ids: Iterable[int] = (),
    ) -> None:
        self.nodes = {value.node_id for value in snapshot.nodes}
        self.blocked_edge_ids = {int(value) for value in blocked_edge_ids}
        self.adjacency: dict[int, list[_Arc]] = {value: [] for value in self.nodes}
        for edge in snapshot.edges:
            if edge.edge_id in self.blocked_edge_ids:
                continue
            if edge.direction_type in {"BOTH", "A_TO_B"}:
                self.adjacency[edge.from_node_id].append(
                    _Arc(edge.to_node_id, edge.distance, edge.edge_id)
                )
            if edge.direction_type in {"BOTH", "B_TO_A"}:
                self.adjacency[edge.to_node_id].append(
                    _Arc(edge.from_node_id, edge.distance, edge.edge_id)
                )
        for values in self.adjacency.values():
            values.sort(key=lambda value: (value.to_node, value.edge_id))

    def shortest_path(self, start: int, goal: int) -> tuple[list[int], float]:
        if start not in self.nodes:
            raise BeCompatRoutingError(f"Unknown start nodeId={start}.")
        if goal not in self.nodes:
            raise BeCompatRoutingError(f"Unknown target nodeId={goal}.")
        if start == goal:
            return [start], 0.0

        distances: dict[int, float] = {start: 0.0}
        previous: dict[int, int] = {}
        queue: list[tuple[float, int]] = [(0.0, start)]
        visited: set[int] = set()

        while queue:
            distance, node_id = heapq.heappop(queue)
            if node_id in visited:
                continue
            visited.add(node_id)
            if node_id == goal:
                break
            for arc in self.adjacency.get(node_id, []):
                candidate = distance + arc.distance
                current = distances.get(arc.to_node, inf)
                if candidate < current - 1e-12:
                    distances[arc.to_node] = candidate
                    previous[arc.to_node] = node_id
                    heapq.heappush(queue, (candidate, arc.to_node))
                elif abs(candidate - current) <= 1e-12:
                    # Stable tie-break so repeated calls return identical paths.
                    if node_id < previous.get(arc.to_node, node_id + 1):
                        previous[arc.to_node] = node_id
                        heapq.heappush(queue, (candidate, arc.to_node))

        if goal not in distances:
            blocked = sorted(self.blocked_edge_ids)
            raise BeCompatRoutingError(
                f"No directed path exists from nodeId={start} to nodeId={goal}. "
                f"blockedEdgeIds={blocked}"
            )

        path = [goal]
        cursor = goal
        while cursor != start:
            cursor = previous[cursor]
            path.append(cursor)
        path.reverse()
        return path, float(distances[goal])


class BeCompatOptimizationService:
    """Expose route-only outputs that the current Spring records can deserialize."""

    def __init__(
        self,
        repository: BeCompatRepository | None = None,
        settings: Settings | None = None,
        runtime_repository: BeSpringRuntimeRepository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or BeCompatRepository(self.settings)
        self.runtime_repository = runtime_repository
        self.last_runtime_source = "request"

    def _seconds(self, distance: float) -> float:
        speed = self.settings.be_compat_robot_speed_distance_per_second
        return round(distance / speed, 6)

    @staticmethod
    def _append_path(base: list[int], addition: list[int]) -> list[int]:
        if not base:
            return list(addition)
        if not addition:
            return base
        if base[-1] == addition[0]:
            base.extend(addition[1:])
        else:
            base.extend(addition)
        return base

    def optimize(self, request: BeOptimizationRequest) -> BeOptimizationResponse:
        snapshot = self.repository.save_graph(
            warehouse_id=request.warehouse_id,
            nodes=request.nodes,
            edges=request.edges,
        )
        graph = _DirectedGraph(snapshot)
        routes: list[BeRobotRoute] = []
        for robot in sorted(request.robots, key=lambda value: value.robot_id):
            start = int(robot.current_node_id)  # validated by request model
            goal = int(robot.target_node_id) if robot.target_node_id is not None else start
            path, distance = graph.shortest_path(start, goal)
            routes.append(
                BeRobotRoute(
                    robotId=robot.robot_id,
                    nodePath=path,
                    totalDistance=round(distance, 6),
                    estimatedTime=self._seconds(distance),
                )
            )

        request_id = f"OPT-W{request.warehouse_id}-{uuid4().hex[:16].upper()}"
        response = BeOptimizationResponse(
            requestId=request_id,
            status="success",
            routes=routes,
        )
        self.repository.record_run(
            request_id=request_id,
            request_type="optimize",
            warehouse_id=request.warehouse_id,
            simulation_run_id=None,
            status=response.status,
            request_payload=request.model_dump(by_alias=True, mode="json"),
            response_payload=response.model_dump(by_alias=True, mode="json"),
        )
        return response

    def _eligible_robots(
        self,
        request: BeReoptimizationRequest,
        robots: list[BeRobotStateInput],
    ) -> list[BeRobotStateInput]:
        excluded_statuses = {"ERROR", "OFFLINE"}
        excluded_ids: set[int] = set()
        if request.trigger_robot_id is not None and request.reason in {
            "ROBOT_FAILURE",
            "LOW_BATTERY",
        }:
            excluded_ids.add(request.trigger_robot_id)

        values: list[BeRobotStateInput] = []
        for robot in robots:
            status = robot.status.upper()
            if robot.robot_id in excluded_ids or status in excluded_statuses:
                continue
            if (
                robot.battery_level is not None
                and robot.battery_level < self.settings.be_compat_min_battery_pct
            ):
                continue
            values.append(robot)
        return sorted(values, key=lambda value: value.robot_id)

    def _runtime_repo(self) -> BeSpringRuntimeRepository:
        if self.runtime_repository is None:
            self.runtime_repository = BeSpringRuntimeRepository(self.settings)
        return self.runtime_repository

    def _resolve_runtime(
        self,
        request: BeReoptimizationRequest,
    ) -> tuple[list[BeRobotStateInput], list[int]]:
        mode = self.settings.be_compat_runtime_source
        robots = list(request.robots)
        blocked = set(request.blocked_edge_ids or [])
        self.last_runtime_source = "request" if robots else "none"

        if mode == "request_only":
            return robots, sorted(blocked)

        if mode == "redis_only" or (mode == "request_then_redis" and not robots):
            snapshot = self._runtime_repo().snapshot(request.simulation_run_id)
            robots = self._runtime_repo().as_reoptimization_inputs(snapshot.robots)
            blocked.update(snapshot.blocked_edge_ids)
            self.last_runtime_source = (
                "spring_redis_full" if snapshot.mode == "FULL" else "spring_redis_compatibility"
            )
        elif mode == "request_then_redis":
            try:
                blocked.update(
                    self._runtime_repo().blocked_edge_ids(request.simulation_run_id)
                )
                if blocked != set(request.blocked_edge_ids or []):
                    self.last_runtime_source = "request+spring_redis_edges"
            except Exception:
                # The Spring request already contains the authoritative robot
                # snapshot.  A missing optional edge-runtime namespace must not
                # invalidate that request.
                pass
        return robots, sorted(blocked)

    @staticmethod
    def _task_order(task: BeTaskInput) -> tuple[int, int]:
        priority = {"IN_PROGRESS": 0, "ASSIGNED": 1, "PENDING": 2}
        return priority.get(task.status.upper(), 3), task.task_id

    @staticmethod
    def _task_is_active(task: BeTaskInput) -> bool:
        return task.status.upper() in {"PENDING", "ASSIGNED", "IN_PROGRESS"}

    def _candidate_cost(
        self,
        graph: _DirectedGraph,
        robot: _RobotWork,
        task: BeTaskInput,
    ) -> tuple[float, list[int], float, list[int], float] | None:
        try:
            to_pickup, pickup_distance = graph.shortest_path(
                robot.current_node, task.start_node_id
            )
            delivery, delivery_distance = graph.shortest_path(
                task.start_node_id, task.end_node_id
            )
        except BeCompatRoutingError:
            return None
        return (
            pickup_distance + delivery_distance,
            to_pickup,
            pickup_distance,
            delivery,
            delivery_distance,
        )

    def reoptimize(self, request: BeReoptimizationRequest) -> BeReoptimizationResponse:
        snapshot = self.repository.require_graph(request.warehouse_id)
        runtime_robots, blocked_edge_ids = self._resolve_runtime(request)
        graph = _DirectedGraph(
            snapshot,
            blocked_edge_ids=blocked_edge_ids,
        )
        robots = self._eligible_robots(request, runtime_robots)
        request_id = (
            f"REOPT-S{request.simulation_run_id}-W{request.warehouse_id}-"
            f"{uuid4().hex[:14].upper()}"
        )

        active_tasks = sorted(
            (value for value in request.remaining_tasks if self._task_is_active(value)),
            key=self._task_order,
        )
        if not active_tasks:
            response = BeReoptimizationResponse(
                requestId=request_id,
                status="success",
                assignments=[],
                routes=[],
            )
            self._record_reoptimization(request, response)
            return response
        if not robots:
            response = BeReoptimizationResponse(
                requestId=request_id,
                status="no_eligible_robot",
                assignments=[],
                routes=[],
            )
            self._record_reoptimization(request, response)
            return response

        work_by_robot = {
            robot.robot_id: _RobotWork(
                robot=robot,
                current_node=robot.current_node_id,
                node_path=[robot.current_node_id],
            )
            for robot in robots
        }
        assignments: list[BeTaskAssignment] = []
        unassigned_count = 0

        for task in active_tasks:
            pinned_robot: _RobotWork | None = None
            if (
                task.status.upper() == "IN_PROGRESS"
                and task.assigned_robot_id in work_by_robot
            ):
                pinned_robot = work_by_robot[task.assigned_robot_id]

            candidates = [pinned_robot] if pinned_robot is not None else list(work_by_robot.values())
            scored: list[
                tuple[float, int, _RobotWork, list[int], float, list[int], float]
            ] = []
            for robot_work in candidates:
                if robot_work is None:
                    continue
                value = self._candidate_cost(graph, robot_work, task)
                if value is None:
                    continue
                total, to_pickup, pickup_distance, delivery, delivery_distance = value
                # Cumulative route length is a stable load-balancing secondary cost.
                scored.append(
                    (
                        total + robot_work.total_distance,
                        robot_work.robot.robot_id,
                        robot_work,
                        to_pickup,
                        pickup_distance,
                        delivery,
                        delivery_distance,
                    )
                )

            if not scored:
                unassigned_count += 1
                continue
            (
                _,
                _,
                selected,
                to_pickup,
                pickup_distance,
                delivery,
                delivery_distance,
            ) = min(scored, key=lambda value: (value[0], value[1]))

            self._append_path(selected.node_path, to_pickup)
            self._append_path(selected.node_path, delivery)
            selected.total_distance += pickup_distance + delivery_distance
            selected.current_node = task.end_node_id
            selected.assigned_task_ids.append(task.task_id)
            assignments.append(
                BeTaskAssignment(taskId=task.task_id, robotId=selected.robot.robot_id)
            )

        routes = [
            BeRobotRoute(
                robotId=value.robot.robot_id,
                nodePath=value.node_path,
                totalDistance=round(value.total_distance, 6),
                estimatedTime=self._seconds(value.total_distance),
            )
            for value in sorted(work_by_robot.values(), key=lambda item: item.robot.robot_id)
            if value.assigned_task_ids
        ]
        status = "success" if unassigned_count == 0 else "partial_success"
        response = BeReoptimizationResponse(
            requestId=request_id,
            status=status,
            assignments=assignments,
            routes=routes,
        )
        self._record_reoptimization(request, response)
        return response

    def _record_reoptimization(
        self,
        request: BeReoptimizationRequest,
        response: BeReoptimizationResponse,
    ) -> None:
        self.repository.record_run(
            request_id=response.request_id,
            request_type="reoptimize",
            warehouse_id=request.warehouse_id,
            simulation_run_id=request.simulation_run_id,
            status=response.status,
            request_payload=request.model_dump(by_alias=True, mode="json"),
            response_payload=response.model_dump(by_alias=True, mode="json"),
            runtime_source=self.last_runtime_source,
        )

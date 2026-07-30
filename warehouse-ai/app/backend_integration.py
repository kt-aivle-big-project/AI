"""Compatibility API contract used by the Spring BE-main service.

The backend owns the HTTP payloads for ``/optimize`` and ``/reoptimize``.
This module converts those camelCase records into the AI planner's internal
graph/task representation and converts collision-free routes back to the
backend response contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings
from app.models import CollisionFreePlan, CuOptPlan
from app.repositories.postgres_adapters import BackendLaroPostgresAdapter
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner


def _to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class BackendContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class BackendRobotInput(BackendContractModel):
    robot_id: int
    current_node_id: int
    target_node_id: int | None = None
    battery_level: float | None = Field(default=None, ge=0, le=100)


class BackendNodeInput(BackendContractModel):
    node_id: int
    x: float | None = None
    y: float | None = None


class BackendEdgeInput(BackendContractModel):
    edge_id: int
    from_node_id: int
    to_node_id: int
    distance: float = Field(gt=0)
    direction_type: str


class BackendOptimizationRequest(BackendContractModel):
    warehouse_id: int
    robots: list[BackendRobotInput] = Field(default_factory=list)
    nodes: list[BackendNodeInput] = Field(default_factory=list)
    edges: list[BackendEdgeInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identifiers(self) -> "BackendOptimizationRequest":
        _require_unique(
            [robot.robot_id for robot in self.robots],
            source="robots.robotId",
        )
        node_ids = [node.node_id for node in self.nodes]
        _require_unique(node_ids, source="nodes.nodeId")
        _require_unique(
            [edge.edge_id for edge in self.edges],
            source="edges.edgeId",
        )
        known_nodes = set(node_ids)
        for robot in self.robots:
            if robot.current_node_id not in known_nodes:
                raise ValueError(
                    f"robotId={robot.robot_id} currentNodeId is not in nodes"
                )
            if (
                robot.target_node_id is not None
                and robot.target_node_id not in known_nodes
            ):
                raise ValueError(
                    f"robotId={robot.robot_id} targetNodeId is not in nodes"
                )
        _validate_edge_nodes(self.edges, known_nodes)
        return self


class BackendRobotStateInput(BackendContractModel):
    robot_id: int
    current_node_id: int
    battery_level: float | None = Field(default=None, ge=0, le=100)
    status: str


class BackendTaskInput(BackendContractModel):
    task_id: int
    assigned_robot_id: int | None = None
    start_node_id: int
    end_node_id: int
    task_type: str
    status: str


class BackendReoptimizationRequest(BackendContractModel):
    simulation_run_id: int
    warehouse_id: int
    reason: str
    trigger_robot_id: int | None = None
    blocked_edge_ids: list[int] = Field(default_factory=list)
    description: str | None = None
    robots: list[BackendRobotStateInput] = Field(default_factory=list)
    remaining_tasks: list[BackendTaskInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identifiers(self) -> "BackendReoptimizationRequest":
        _require_unique(
            [robot.robot_id for robot in self.robots],
            source="robots.robotId",
        )
        _require_unique(
            [task.task_id for task in self.remaining_tasks],
            source="remainingTasks.taskId",
        )
        return self


class BackendRobotRoute(BackendContractModel):
    robot_id: int
    node_path: list[int]
    total_distance: float
    estimated_time: float


class BackendOptimizationResponse(BackendContractModel):
    request_id: str
    status: Literal["COMPLETED", "PARTIAL"]
    routes: list[BackendRobotRoute] = Field(default_factory=list)


class BackendTaskAssignment(BackendContractModel):
    task_id: int
    robot_id: int


class BackendReoptimizationResponse(BackendContractModel):
    request_id: str
    status: Literal["COMPLETED", "PARTIAL"]
    assignments: list[BackendTaskAssignment] = Field(default_factory=list)
    routes: list[BackendRobotRoute] = Field(default_factory=list)


def _require_unique(values: list[int], *, source: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{source} contains duplicate identifiers")


def _validate_edge_nodes(
    edges: list[BackendEdgeInput],
    known_nodes: set[int],
) -> None:
    for edge in edges:
        if (
            edge.from_node_id not in known_nodes
            or edge.to_node_id not in known_nodes
        ):
            raise ValueError(
                f"edgeId={edge.edge_id} references a node not present in nodes"
            )


def _normalize_edge(
    *,
    edge_id: int | str,
    from_node: int,
    to_node: int,
    distance: float,
    direction_type: str,
) -> dict[str, Any]:
    direction_type = direction_type.upper()
    if direction_type in {"BOTH", "BIDIRECTIONAL"}:
        direction = "BOTH"
    elif direction_type in {"A_TO_B", "ONE_WAY"}:
        direction = "ONE_WAY"
    elif direction_type == "B_TO_A":
        from_node, to_node = to_node, from_node
        direction = "ONE_WAY"
    else:
        raise ValueError(
            f"edgeId={edge_id} has unsupported directionType={direction_type}"
        )
    return {
        "edge_id": str(edge_id),
        "from_node": int(from_node),
        "to_node": int(to_node),
        "distance": float(distance),
        "travel_seconds": float(distance),
        "direction": direction,
        "active": True,
    }


def _run_problem(
    problem: dict[str, Any],
    settings: Settings,
) -> tuple[CuOptPlan, CollisionFreePlan]:
    optimizer = LocalOptimizer(
        time_step_seconds=settings.time_step_seconds,
        min_robot_battery=settings.min_robot_battery,
        energy_per_distance=settings.energy_per_distance,
        charge_target_battery=settings.charge_target_battery,
        charge_rate_percent_per_minute=(
            settings.charge_rate_percent_per_minute
        ),
        battery_safety_margin_percent=(
            settings.battery_safety_margin_percent
        ),
    )
    optimization_plan = optimizer.optimize(problem)
    collision_plan = PrioritizedTimeExpandedPlanner(
        problem,
        settings.time_step_seconds,
        settings.max_mapf_time_steps,
    ).solve(optimization_plan)
    return optimization_plan, collision_plan


def _response_routes(
    collision_plan: CollisionFreePlan,
) -> list[BackendRobotRoute]:
    routes: list[BackendRobotRoute] = []
    for route in collision_plan.routes:
        if not route.waypoints:
            continue
        first_step = route.waypoints[0].time_step
        last_step = route.waypoints[-1].time_step
        routes.append(
            BackendRobotRoute(
                robot_id=int(route.robot_id),
                node_path=[
                    int(waypoint.node_id) for waypoint in route.waypoints
                ],
                total_distance=round(float(route.distance), 6),
                estimated_time=float(
                    max(0, last_step - first_step)
                    * collision_plan.time_step_seconds
                ),
            )
        )
    return sorted(routes, key=lambda route: route.robot_id)


def optimize_for_backend(
    request: BackendOptimizationRequest,
    settings: Settings,
) -> BackendOptimizationResponse:
    nodes = [
        {
            "node_id": node.node_id,
            "warehouse_id": request.warehouse_id,
            "node_type": "ROUTE",
            "x": node.x,
            "y": node.y,
            "active": True,
        }
        for node in request.nodes
    ]
    edges = [
        _normalize_edge(
            edge_id=edge.edge_id,
            from_node=edge.from_node_id,
            to_node=edge.to_node_id,
            distance=edge.distance,
            direction_type=edge.direction_type,
        )
        for edge in request.edges
    ]
    robots = [
        {
            "robot_id": str(robot.robot_id),
            "node_id": robot.current_node_id,
            "battery": (
                robot.battery_level
                if robot.battery_level is not None
                else 100.0
            ),
            "status": "AVAILABLE",
            "max_load": 1_000_000_000.0,
            "current_load": 0.0,
        }
        for robot in request.robots
    ]
    tasks = [
        {
            "task_id": f"backend-opt-{robot.robot_id}",
            "work_id": f"backend-opt-{robot.robot_id}",
            "action": "MOVE",
            "source_candidates": [robot.current_node_id],
            "target_candidates": [robot.target_node_id],
            "priority": 5,
            "assigned_robot_id": str(robot.robot_id),
        }
        for robot in request.robots
        if robot.target_node_id is not None
    ]
    problem = {
        "warehouse_id": request.warehouse_id,
        "reference_time": datetime.now(UTC),
        "nodes": nodes,
        "edges": edges,
        "robots": robots,
        "tasks": tasks,
        "temporary_closures": [],
    }
    optimization_plan, collision_plan = _run_problem(problem, settings)
    return BackendOptimizationResponse(
        request_id=str(uuid4()),
        status=(
            "PARTIAL"
            if optimization_plan.unassigned_task_ids
            else "COMPLETED"
        ),
        routes=_response_routes(collision_plan),
    )


def load_backend_map(
    warehouse_id: int,
    settings: Settings,
) -> dict[str, list[dict[str, Any]]]:
    if not settings.database_url:
        raise RuntimeError(
            "Backend reoptimization requires a PostgreSQL connection"
        )
    if settings.postgres_schema_profile != "backend_laro":
        raise RuntimeError(
            "Backend reoptimization requires "
            "POSTGRES_SCHEMA_PROFILE=backend_laro"
        )
    repository = BackendLaroPostgresAdapter(settings.database_url)
    try:
        return repository.fetch_map(warehouse_id)
    finally:
        repository.close()


def reoptimize_for_backend(
    request: BackendReoptimizationRequest,
    settings: Settings,
    backend_map: dict[str, list[dict[str, Any]]],
) -> BackendReoptimizationResponse:
    blocked_edge_ids = {str(edge_id) for edge_id in request.blocked_edge_ids}
    nodes = list(backend_map.get("nodes") or [])
    edges = [
        edge
        for edge in list(backend_map.get("edges") or [])
        if str(edge.get("edge_id")) not in blocked_edge_ids
    ]
    known_node_ids = {int(node["node_id"]) for node in nodes}
    for robot in request.robots:
        if robot.current_node_id not in known_node_ids:
            raise ValueError(
                f"robotId={robot.robot_id} currentNodeId is not in warehouse map"
            )
    for task in request.remaining_tasks:
        if (
            task.start_node_id not in known_node_ids
            or task.end_node_id not in known_node_ids
        ):
            raise ValueError(
                f"taskId={task.task_id} references a node not in warehouse map"
            )

    robots = [
        {
            "robot_id": str(robot.robot_id),
            "node_id": robot.current_node_id,
            "battery": (
                robot.battery_level
                if robot.battery_level is not None
                else 100.0
            ),
            "status": robot.status,
            "max_load": 1_000_000_000.0,
            "current_load": 0.0,
        }
        for robot in request.robots
    ]
    tasks = [
        {
            "task_id": str(task.task_id),
            "work_id": str(task.task_id),
            "action": "MOVE",
            "source_candidates": [task.start_node_id],
            "target_candidates": [task.end_node_id],
            "priority": 1 if task.status.upper() == "IN_PROGRESS" else 5,
            "assigned_robot_id": (
                str(task.assigned_robot_id)
                if task.status.upper() == "IN_PROGRESS"
                and task.assigned_robot_id is not None
                else None
            ),
        }
        for task in request.remaining_tasks
    ]
    problem = {
        "warehouse_id": request.warehouse_id,
        "reference_time": datetime.now(UTC),
        "nodes": nodes,
        "edges": edges,
        "robots": robots,
        "tasks": tasks,
        "temporary_closures": [],
        "allow_local_robot_rebalance": True,
    }
    optimization_plan, collision_plan = _run_problem(problem, settings)

    task_ids = {str(task.task_id) for task in request.remaining_tasks}
    assignments_by_task = {
        task.task_id: task.robot_id
        for task in optimization_plan.scheduled_tasks
        if task.task_id in task_ids
    }
    assignments = [
        BackendTaskAssignment(
            task_id=int(task_id),
            robot_id=int(assignments_by_task[task_id]),
        )
        for task_id in sorted(assignments_by_task, key=int)
    ]
    return BackendReoptimizationResponse(
        request_id=str(uuid4()),
        status=(
            "PARTIAL"
            if optimization_plan.unassigned_task_ids
            else "COMPLETED"
        ),
        assignments=assignments,
        routes=_response_routes(collision_plan),
    )

"""Compatibility contracts for the unmodified Spring BE optimization client.

The Java records in ``BE-main`` serialize camelCase fields.  These models keep
that wire contract exactly while allowing the rest of LARO to use snake_case
internally.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BeCompatModel(BaseModel):
    """Strict camelCase-compatible model shared by request and response DTOs."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class BeRobotInput(BeCompatModel):
    robot_id: int = Field(alias="robotId")
    current_node_id: int | None = Field(default=None, alias="currentNodeId")
    target_node_id: int | None = Field(default=None, alias="targetNodeId")
    battery_level: float | None = Field(default=None, alias="batteryLevel")


class BeNodeInput(BeCompatModel):
    node_id: int = Field(alias="nodeId")
    x: float | None = None
    y: float | None = None


class BeEdgeInput(BeCompatModel):
    edge_id: int = Field(alias="edgeId")
    from_node_id: int = Field(alias="fromNodeId")
    to_node_id: int = Field(alias="toNodeId")
    distance: float
    direction_type: Literal["BOTH", "A_TO_B", "B_TO_A"] = Field(alias="directionType")

    @field_validator("distance")
    @classmethod
    def validate_distance(cls, value: float) -> float:
        if value < 0:
            raise ValueError("distance must be greater than or equal to zero")
        return float(value)


class BeOptimizationRequest(BeCompatModel):
    warehouse_id: int = Field(alias="warehouseId")
    robots: list[BeRobotInput] = Field(default_factory=list)
    nodes: list[BeNodeInput] = Field(default_factory=list)
    edges: list[BeEdgeInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "BeOptimizationRequest":
        node_ids = [value.node_id for value in self.nodes]
        edge_ids = [value.edge_id for value in self.edges]
        robot_ids = [value.robot_id for value in self.robots]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("nodes contains duplicate nodeId values")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("edges contains duplicate edgeId values")
        if len(robot_ids) != len(set(robot_ids)):
            raise ValueError("robots contains duplicate robotId values")
        known_nodes = set(node_ids)
        for edge in self.edges:
            if edge.from_node_id not in known_nodes or edge.to_node_id not in known_nodes:
                raise ValueError(
                    f"edgeId={edge.edge_id} references a node that is not present in nodes"
                )
        for robot in self.robots:
            if robot.current_node_id is None:
                raise ValueError(f"robotId={robot.robot_id} requires currentNodeId")
            if robot.current_node_id not in known_nodes:
                raise ValueError(
                    f"robotId={robot.robot_id} currentNodeId={robot.current_node_id} is unknown"
                )
            if robot.target_node_id is not None and robot.target_node_id not in known_nodes:
                raise ValueError(
                    f"robotId={robot.robot_id} targetNodeId={robot.target_node_id} is unknown"
                )
        return self


class BeRobotStateInput(BeCompatModel):
    robot_id: int = Field(alias="robotId")
    current_node_id: int = Field(alias="currentNodeId")
    battery_level: float | None = Field(default=None, alias="batteryLevel")
    status: str


class BeTaskInput(BeCompatModel):
    task_id: int = Field(alias="taskId")
    assigned_robot_id: int | None = Field(default=None, alias="assignedRobotId")
    start_node_id: int = Field(alias="startNodeId")
    end_node_id: int = Field(alias="endNodeId")
    task_type: str = Field(alias="taskType")
    status: str


class BeReoptimizationRequest(BeCompatModel):
    simulation_run_id: int = Field(alias="simulationRunId")
    warehouse_id: int = Field(alias="warehouseId")
    reason: Literal[
        "ROBOT_TASK_COMPLETED",
        "ROBOT_FAILURE",
        "LOW_BATTERY",
        "OBSTACLE_DETECTED",
        "NEW_TASK_ADDED",
        "MANUAL_REQUEST",
    ]
    trigger_robot_id: int | None = Field(default=None, alias="triggerRobotId")
    blocked_edge_ids: list[int] | None = Field(default=None, alias="blockedEdgeIds")
    description: str | None = None
    robots: list[BeRobotStateInput] = Field(default_factory=list)
    remaining_tasks: list[BeTaskInput] = Field(default_factory=list, alias="remainingTasks")

    @model_validator(mode="after")
    def validate_runtime(self) -> "BeReoptimizationRequest":
        robot_ids = [value.robot_id for value in self.robots]
        task_ids = [value.task_id for value in self.remaining_tasks]
        if len(robot_ids) != len(set(robot_ids)):
            raise ValueError("robots contains duplicate robotId values")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("remainingTasks contains duplicate taskId values")
        return self


class BeRobotRoute(BeCompatModel):
    robot_id: int = Field(alias="robotId")
    node_path: list[int] = Field(alias="nodePath")
    total_distance: float = Field(alias="totalDistance")
    estimated_time: float = Field(alias="estimatedTime")


class BeOptimizationResponse(BeCompatModel):
    request_id: str = Field(alias="requestId")
    status: str
    routes: list[BeRobotRoute] = Field(default_factory=list)


class BeTaskAssignment(BeCompatModel):
    task_id: int = Field(alias="taskId")
    robot_id: int = Field(alias="robotId")


class BeReoptimizationResponse(BeCompatModel):
    request_id: str = Field(alias="requestId")
    status: str
    assignments: list[BeTaskAssignment] = Field(default_factory=list)
    routes: list[BeRobotRoute] = Field(default_factory=list)


class BeGraphSnapshot(BeCompatModel):
    warehouse_id: int = Field(alias="warehouseId")
    graph_version: str = Field(alias="graphVersion")
    nodes: list[BeNodeInput]
    edges: list[BeEdgeInput]


class BeGraphStatusResponse(BeCompatModel):
    warehouse_id: int = Field(alias="warehouseId")
    available: bool
    graph_version: str | None = Field(default=None, alias="graphVersion")
    node_count: int = Field(default=0, alias="nodeCount")
    edge_count: int = Field(default=0, alias="edgeCount")
    source: str | None = None


class BeCompatRuntimeRobot(BeCompatModel):
    """Robot state read from the unmodified Spring Redis JSON document.

    ``compatibilityMode`` means the optional LARO extension document is absent;
    the route-only API can still reassign from the current node, but exact
    rolling-horizon handover semantics are unavailable.
    """

    model_config = ConfigDict(
        populate_by_name=True, extra="ignore", str_strip_whitespace=True
    )

    robot_id: int = Field(alias="robotId")
    warehouse_id: int | None = Field(default=None, alias="warehouseId")
    current_node_id: int | None = Field(default=None, alias="currentNodeId")
    current_node_code: str | None = Field(default=None, alias="currentNodeCode")
    next_node_id: int | None = Field(default=None, alias="nextNodeId")
    next_node_code: str | None = Field(default=None, alias="nextNodeCode")
    arrival_in_seconds: float | None = Field(default=None, alias="arrivalInSeconds")
    battery_level: float | None = Field(default=None, alias="batteryLevel")
    status: str
    current_task_id: int | None = Field(default=None, alias="currentTaskId")
    carrying_load: bool | None = Field(default=None, alias="carryingLoad")
    updated_at: str | None = Field(default=None, alias="updatedAt")

    schema_version: int = Field(default=1, alias="schemaVersion")
    sim_time_ms: int | None = Field(default=None, alias="simTimeMs")
    # The current Spring RobotState record uses this longer field name.  Keep
    # accepting the older LARO extension name above for backward compatibility.
    simulation_time_millis: int | None = Field(
        default=None, alias="simulationTimeMillis"
    )
    state_version: int | None = Field(default=None, alias="stateVersion")
    active_plan_id: str | None = Field(default=None, alias="activePlanId")
    active_plan_version: int | None = Field(default=None, alias="activePlanVersion")
    current_step_id: str | None = Field(default=None, alias="currentStepId")
    current_step_type: str | None = Field(default=None, alias="currentStepType")
    step_start_at_ms: int | None = Field(default=None, alias="stepStartAtMs")
    step_end_at_ms: int | None = Field(default=None, alias="stepEndAtMs")
    current_edge_code: str | None = Field(default=None, alias="currentEdgeCode")
    from_node_code: str | None = Field(default=None, alias="fromNodeCode")
    to_node_code: str | None = Field(default=None, alias="toNodeCode")
    capacity_units: int | None = Field(default=None, alias="capacityUnits")
    current_load_units: int | None = Field(default=None, alias="currentLoadUnits")
    handling_unit_code: str | None = Field(default=None, alias="handlingUnitCode")
    active_task_code: str | None = Field(default=None, alias="activeTaskCode")
    compatibility_mode: bool = Field(default=True, alias="compatibilityMode")


class BeCompatEdgeRuntime(BeCompatModel):
    model_config = ConfigDict(
        populate_by_name=True, extra="ignore", str_strip_whitespace=True
    )

    edge_id: int = Field(alias="edgeId")
    edge_code: str | None = Field(default=None, alias="edgeCode")
    status: str = "OPEN"
    cost_multiplier: float = Field(default=1.0, alias="costMultiplier")
    travel_time_multiplier: float = Field(default=1.0, alias="travelTimeMultiplier")
    occupied_by_robot_id: int | None = Field(default=None, alias="occupiedByRobotId")
    blocked_until_ms: int | None = Field(default=None, alias="blockedUntilMs")
    state_version: int | None = Field(default=None, alias="stateVersion")


class BeCompatRunMeta(BeCompatModel):
    model_config = ConfigDict(
        populate_by_name=True, extra="ignore", str_strip_whitespace=True
    )

    simulation_run_id: int = Field(alias="simulationRunId")
    warehouse_id: int | None = Field(default=None, alias="warehouseId")
    status: str | None = None
    sim_time_ms: int | None = Field(default=None, alias="simTimeMs")
    playback_speed: float | None = Field(default=None, alias="playbackSpeed")
    active_plan_id: str | None = Field(default=None, alias="activePlanId")
    active_plan_version: int | None = Field(default=None, alias="activePlanVersion")
    map_version: str | None = Field(default=None, alias="mapVersion")
    runtime_version: int | None = Field(default=None, alias="runtimeVersion")
    compatibility_mode: bool = Field(default=True, alias="compatibilityMode")


class BeCompatRuntimeSnapshot(BeCompatModel):
    simulation_run_id: int = Field(alias="simulationRunId")
    mode: Literal["FULL", "COMPATIBILITY", "NOT_INITIALIZED"]
    meta: BeCompatRunMeta | None = None
    robots: list[BeCompatRuntimeRobot] = Field(default_factory=list)
    blocked_edge_ids: list[int] = Field(default_factory=list, alias="blockedEdgeIds")
    warnings: list[str] = Field(default_factory=list)


class BeCompatRuntimeBootstrapRequest(BeCompatModel):
    warehouse_id: int = Field(alias="warehouseId")
    robots: list[BeRobotStateInput] = Field(default_factory=list)
    sim_time_ms: int = Field(default=0, alias="simTimeMs", ge=0)
    replace: bool = True


class BeCompatContractStatus(BeCompatModel):
    schema_name: str = Field(alias="schema")
    ready: bool
    spring_tables_available: bool = Field(alias="springTablesAvailable")
    graph_source: str = Field(alias="graphSource")
    graph_cache_mode: str = Field(alias="graphCacheMode")
    runtime_source: str = Field(alias="runtimeSource")
    tables: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

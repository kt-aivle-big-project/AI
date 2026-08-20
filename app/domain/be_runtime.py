"""Spring BE Redis runtime contracts used by the native planning API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BeRuntimeModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )


class BeRuntimeRobot(BeRuntimeModel):
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
    wait_started_at_ms: int | None = Field(default=None, alias="waitStartedAtMillis")
    compatibility_mode: bool = Field(default=True, alias="compatibilityMode")


class BeRuntimeEdge(BeRuntimeModel):
    edge_id: int = Field(alias="edgeId")
    edge_code: str | None = Field(default=None, alias="edgeCode")
    status: str = "OPEN"
    cost_multiplier: float = Field(default=1.0, alias="costMultiplier")
    travel_time_multiplier: float = Field(default=1.0, alias="travelTimeMultiplier")
    occupied_by_robot_id: int | None = Field(default=None, alias="occupiedByRobotId")
    blocked_until_ms: int | None = Field(default=None, alias="blockedUntilMs")
    state_version: int | None = Field(default=None, alias="stateVersion")


class BeRuntimeRunMeta(BeRuntimeModel):
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


class BeRuntimeSnapshot(BeRuntimeModel):
    simulation_run_id: int = Field(alias="simulationRunId")
    mode: Literal["FULL", "COMPATIBILITY", "NOT_INITIALIZED"]
    meta: BeRuntimeRunMeta | None = None
    robots: list[BeRuntimeRobot] = Field(default_factory=list)
    blocked_edge_ids: list[int] = Field(default_factory=list, alias="blockedEdgeIds")
    warnings: list[str] = Field(default_factory=list)

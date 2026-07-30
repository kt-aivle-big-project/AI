"""Public integration contracts for backend, frontend, and simulator consumers.

These models deliberately sit outside the internal LangGraph state.  Internal
planning evidence can continue to evolve while the public integration contracts
remain small and versioned.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class IntegrationResourceLinks(BaseModel):
    simulation_view: str | None = None
    execution_status: str | None = None
    plan_evidence: str | None = None
    stage_logs: str | None = None
    debug_view: str | None = None


class PlanningUiResponse(BaseModel):
    schema_version: str = "planning-ui.v1"
    command_id: str
    conversation_id: str | None = None
    warehouse_id: int | None = None
    status: str
    plan_version: str | None = None
    simulation_id: str | None = None
    execution_mode: str | None = None
    intent: str | None = None
    plan_mode: str | None = None
    message: str | None = None
    answer: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Any] = Field(default_factory=list)
    errors: list[Any] = Field(default_factory=list)
    resources: IntegrationResourceLinks


class RoutePlanStep(BaseModel):
    step_type: Literal["MOVE", "WAIT", "SERVICE"]
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(ge=0)
    edge_id: str | None = None
    from_node: str | None = None
    to_node: str | None = None
    node_id: str | None = None
    reason: str | None = None
    task_id: str | None = None
    service_kind: Literal["PICKUP", "DROPOFF", "CHARGE"] | None = None


class RoutePlanRobotRoute(BaseModel):
    robot_id: str
    steps: list[RoutePlanStep] = Field(default_factory=list)
    finish_at_ms: int = Field(ge=0)


class SimulationViewResponse(BaseModel):
    valid: bool
    planner: str
    routes: list[RoutePlanRobotRoute] = Field(default_factory=list)
    reservations: list[dict[str, Any]] = Field(default_factory=list)
    station_reservations: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    total_wait_ms: int = Field(ge=0)
    total_service_ms: int = Field(ge=0)
    makespan_ms: int = Field(ge=0)


class ExecutionStatusResponse(BaseModel):
    schema_version: str = "execution-status.v1"
    plan_version: str
    command_id: str | None = None
    warehouse_id: int | None = None
    simulation_id: str | None = None
    planning_status: str | None = None
    execution_mode: str | None = None
    execution_requested: bool = False
    execution_state: str = "NOT_REQUESTED"
    approval: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    inventory_reservations: dict[str, Any] = Field(default_factory=dict)


class RobotDispatchContract(BaseModel):
    """Documentation model for the existing Robot Gateway payload."""

    schema_version: str = "robot-command.v1"
    dispatch_id: str
    plan_version: str
    warehouse_id: int
    command_id: str | None = None
    robot_command_batches: list[dict[str, Any]] = Field(default_factory=list)


class DebugPlanningResponse(BaseModel):
    schema_version: str = "planning-debug.v1"
    command_id: str
    plan_version: str | None = None
    simulation_id: str | None = None
    status: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    resources: IntegrationResourceLinks

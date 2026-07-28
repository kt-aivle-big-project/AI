"""Public integration contracts for backend, frontend, and simulator consumers.

These models deliberately sit outside the internal LangGraph state.  Internal
planning evidence can continue to evolve while the public integration contracts
remain small and versioned.
"""

from typing import Any

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


class SimulationViewResponse(BaseModel):
    schema_version: str = "simulation-view.v1"
    simulation_id: str
    command_id: str | None = None
    warehouse_id: int | None = None
    plan_version: str | None = None
    status: str | None = None
    time_step_seconds: int = 5
    robots: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Any] = Field(default_factory=list)
    errors: list[Any] = Field(default_factory=list)


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

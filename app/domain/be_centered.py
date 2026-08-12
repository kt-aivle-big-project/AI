"""Public contracts for planning from the existing Spring BE data model.

A numeric Spring ``simulation_run_id`` selects the warehouse and live Redis
namespace.  The request owns the business operations; LARO does not read or
persist a separate order master and does not require a handling-unit master.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.domain.schemas import (
    HumanInteractionResumeOutcome,
    HumanInteractionStatus,
    OptimizationBackend,
    PublicRuntimeSnapshot,
    SimulationPlanResponse,
    StrictModel,
    StructuredMissionInput,
    WarehouseId,
    ReplanReason,
    WorkflowHoldResult,
    WorkflowStatus,
)


class BeSimulationPlanRequest(StrictModel):
    """Plan request sent by the Spring BE for one existing simulation run.

    ``structured_input`` is authoritative. ``user_command`` is optional and is
    used only for policy/objective interpretation; it must not replace or invent
    operation facts absent from the structured input.
    """

    structured_input: StructuredMissionInput
    user_command: str | None = Field(default=None, max_length=4000)
    optimization_backend: OptimizationBackend | None = None
    runtime_snapshot: PublicRuntimeSnapshot | None = None

    @model_validator(mode="after")
    def validate_command(self) -> "BeSimulationPlanRequest":
        if self.user_command is not None and not self.user_command.strip():
            self.user_command = None
        return self


class BeSimulationPlanResponse(StrictModel):
    """Native LARO result annotated with the owning Spring simulation run."""

    api_version: Literal["v1"] = "v1"
    simulation_run_id: int = Field(ge=1)
    warehouse_id: WarehouseId
    warehouse_numeric_id: int = Field(ge=1)
    request_id: str | None = None
    result: SimulationPlanResponse
    trace_url: str | None = None
    debug_url: str | None = None


class BeHumanInteractionResumeResponse(StrictModel):
    """HITL result scoped to one Spring simulation run."""

    interaction_id: str
    interaction_status: HumanInteractionStatus
    resume_outcome: HumanInteractionResumeOutcome
    message: str
    terminal_status: WorkflowStatus | None = None
    workflow_hold: WorkflowHoldResult | None = None
    plan_response: BeSimulationPlanResponse | None = None


class BeSimulationReplanRequest(BeSimulationPlanRequest):
    """Rolling-horizon request scoped to one Spring simulation run."""

    active_plan_id: str
    active_plan_version: int | None = Field(default=None, ge=1)
    replan_at_sim_time_ms: int = Field(ge=0)
    reason: ReplanReason = "NEW_ORDER"
    activation_policy: Literal["PER_ROBOT_HANDOVER", "ALL_ROBOTS_READY"] = (
        "ALL_ROBOTS_READY"
    )


class BeCenteredPreflightResponse(StrictModel):
    """Read-only contract check for the BE-centered planning path."""

    status: Literal["READY", "NOT_READY"]
    ready: bool
    simulation_run_id: int = Field(ge=1)
    warehouse_id: WarehouseId | None = None
    warehouse_numeric_id: int | None = Field(default=None, ge=1)
    sources: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    runtime_mode: str | None = None
    problems: list[str] = Field(default_factory=list)

"""Contracts for deferred Rule/Agent planning comparisons."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.domain.schemas import StrictModel

EvaluationStatus = Literal["CAPTURED", "COMPARING", "COMPARISON_READY", "FAILED"]
BranchApplicability = Literal["APPLICABLE", "NOT_APPLICABLE", "UNKNOWN"]


class PlanningComparisonRequest(StrictModel):
    backend: Literal["ortools", "cuopt_payload_only", "cuopt"] = "ortools"
    depth: Literal["formulation", "payload", "solve", "mapf"] = "mapf"
    agent_repeats: int = Field(default=1, ge=1, le=5)


class PlanningEvaluationReference(StrictModel):
    evaluation_id: str
    status: EvaluationStatus
    detail_url: str
    compare_url: str


class BranchQualityMetrics(StrictModel):
    route: Literal["RULE_FORMULATION", "AGENT_FORMULATION"]
    applicability: BranchApplicability = "UNKNOWN"
    workflow_status: str | None = None
    operation_ids: list[str] = Field(default_factory=list)
    missing_operation_ids: list[str] = Field(default_factory=list)
    hallucinated_operation_ids: list[str] = Field(default_factory=list)
    optimization_task_count: int = 0
    deferred_task_ids: list[str] = Field(default_factory=list)
    candidate_robot_count: int = 0
    payload_valid: bool | None = None
    solver_status: str | None = None
    unassigned_task_count: int = 0
    mapf_valid: bool | None = None
    makespan_ms: int | None = None
    total_distance_m: float | None = None
    total_wait_ms: int | None = None
    total_service_ms: int | None = None
    used_robot_count: int = 0
    operation_preservation_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    completed_task_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    max_robot_wait_ms: int | None = None
    station_reservation_count: int = 0
    g2p_batch_count: int = 0
    terminal_relocation_count: int = 0
    charge_relocation_count: int = 0
    park_relocation_count: int = 0
    route_locked: bool | None = None
    hitl_required: bool = False
    input_rejected: bool = False
    validation_issue_count: int = 0
    policy_violation_count: int = 0
    total_latency_ms: float = 0.0
    llm_latency_ms: float = 0.0
    llm_call_count: int = 0
    errors: list[str] = Field(default_factory=list)


class PlanningComparisonReport(StrictModel):
    evaluation_id: str
    backend: str
    depth: str
    expected_operation_ids: list[str] = Field(default_factory=list)
    rule: BranchQualityMetrics
    agent_runs: list[BranchQualityMetrics] = Field(default_factory=list)
    agent_stability: dict[str, object] = Field(default_factory=dict)
    comparison: dict[str, object] = Field(default_factory=dict)
    created_at: str

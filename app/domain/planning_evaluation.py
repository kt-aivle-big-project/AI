"""Contracts for local Rule/Agent planning comparisons."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.domain.schemas import ObjectiveProfile, StrictModel

EvaluationStatus = Literal["CAPTURED", "COMPARING", "COMPARISON_READY", "FAILED"]
BranchApplicability = Literal["APPLICABLE", "NOT_APPLICABLE", "UNKNOWN"]
class PlanningComparisonRequest(StrictModel):
    # Operational Rule/Agent evaluations are deliberately solver-fixed.  A
    # payload-only or OR-Tools run is a useful unit test, but it is not evidence
    # for the production cuOpt comparison represented by this API.
    backend: Literal["cuopt"] = "cuopt"
    depth: Literal["formulation", "payload", "solve", "mapf"] = "mapf"
    agent_repeats: int = Field(default=5, ge=1, le=5)
    min_valid_agent_runs: int = Field(default=3, ge=1, le=5)
    cost_tie_tolerance_pct: float = Field(default=0.5, ge=0.0, le=100.0)
    max_agent_cost_regression_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    operational_tie_tolerance_pct: float = Field(default=3.0, ge=0.0, le=100.0)
    min_agent_makespan_improvement_pct: float = Field(
        default=5.0, ge=0.0, le=100.0
    )
    max_agent_distance_regression_pct: float = Field(default=20.0, ge=0.0, le=500.0)
    max_agent_fleet_effort_regression_pct: float = Field(
        default=25.0, ge=0.0, le=500.0
    )
    max_agent_wait_regression_pct: float = Field(default=25.0, ge=0.0, le=500.0)
    required_objective_profile: ObjectiveProfile = "BALANCED"
    require_mapf_gate: bool = True

    @model_validator(mode="after")
    def validate_valid_run_threshold(self) -> "PlanningComparisonRequest":
        if self.min_valid_agent_runs > self.agent_repeats:
            raise ValueError("min_valid_agent_runs cannot exceed agent_repeats")
        if self.required_objective_profile == "MIN_TOTAL_COST":
            raise ValueError(
                "MIN_TOTAL_COST is distance-only and produces a trivial one-robot "
                "baseline; use BALANCED or another operational objective profile"
            )
        return self


class PlanningScenarioSuiteRequest(PlanningComparisonRequest):
    """Build isolated operational captures and optionally compare them locally."""

    scenario_ids: list[str] = Field(default_factory=list, max_length=30)
    scenario_groups: list[
        Literal["INITIAL", "REPLAN", "HUMAN_REVIEW"]
    ] = Field(default_factory=list, max_length=3)
    materialize_only: bool = False

    def comparison_request(self) -> PlanningComparisonRequest:
        return PlanningComparisonRequest.model_validate(
            self.model_dump(
                exclude={
                    "scenario_ids",
                    "scenario_groups",
                    "materialize_only",
                }
            )
        )


class PlanningEvaluationReference(StrictModel):
    evaluation_id: str
    status: EvaluationStatus
    artifact_path: str


class EvaluationGateFailure(StrictModel):
    """One machine-readable reason why a branch is ineligible for cost scoring."""

    code: str
    message: str


class BranchQualityMetrics(StrictModel):
    route: Literal["RULE_FORMULATION", "AGENT_FORMULATION"]
    repeat_index: int = Field(default=1, ge=1)
    applicability: BranchApplicability = "UNKNOWN"
    workflow_status: str | None = None
    snapshot_id: str | None = None
    objective_profile: str | None = None
    operation_ids: list[str] = Field(default_factory=list)
    missing_operation_ids: list[str] = Field(default_factory=list)
    hallucinated_operation_ids: list[str] = Field(default_factory=list)
    optimization_task_count: int = 0
    optimization_task_ids: list[str] = Field(default_factory=list)
    mandatory_task_ids: list[str] = Field(default_factory=list)
    optional_task_ids: list[str] = Field(default_factory=list)
    deferred_task_ids: list[str] = Field(default_factory=list)
    candidate_robot_count: int = 0
    payload_valid: bool | None = None
    solver_backend: str | None = None
    solver_status: str | None = None
    unassigned_task_count: int = 0
    unassigned_task_ids: list[str] = Field(default_factory=list)
    mapf_valid: bool | None = None
    global_objective_cost: float | None = None
    objective_values: dict[str, float] = Field(default_factory=dict)
    optimizer_estimated_makespan_ms: float | None = None
    makespan_ms: int | None = None
    total_distance_m: float | None = None
    total_wait_ms: int | None = None
    total_service_ms: int | None = None
    used_robot_count: int = 0
    fleet_effort_robot_ms: int | None = None
    throughput_operations_per_hour: float | None = None
    distance_per_operation_m: float | None = None
    wait_per_operation_ms: float | None = None
    operations_per_used_robot: float | None = None
    # One physical cycle is one optimizer pickup-delivery pair.  Include every
    # candidate robot with zero for an unused route so a one-robot solution does
    # not appear perfectly balanced merely because only used routes were sampled.
    physical_cycle_count_by_robot: dict[str, int] = Field(default_factory=dict)
    min_physical_cycles_per_robot: int | None = None
    max_physical_cycles_per_robot: int | None = None
    physical_cycle_count_range: int | None = None
    physical_cycle_count_standard_deviation: float | None = None
    physical_cycle_count_coefficient_of_variation: float | None = None
    physical_cycle_count_gini_coefficient: float | None = None
    scheduled_work_ms_by_robot: dict[str, int] = Field(default_factory=dict)
    scheduled_work_time_range_ms: int | None = None
    scheduled_work_time_standard_deviation_ms: float | None = None
    scheduled_work_time_coefficient_of_variation: float | None = None
    route_finish_at_ms_by_robot: dict[str, float] = Field(default_factory=dict)
    max_robot_finish_at_ms: float | None = None
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
    hard_gate_passed: bool = False
    hard_gate_failures: list[EvaluationGateFailure] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AgentCostStatistics(StrictModel):
    requested_runs: int = 0
    valid_runs: int = 0
    invalid_runs: int = 0
    valid_run_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    costs: list[float] = Field(default_factory=list)
    minimum_cost: float | None = None
    maximum_cost: float | None = None
    mean_cost: float | None = None
    median_cost: float | None = None
    standard_deviation: float | None = None
    coefficient_of_variation: float | None = None
    wins: int = 0
    ties: int = 0
    losses: int = 0
    win_rate: float = Field(default=0.0, ge=0.0, le=1.0)


CostComparisonVerdict = Literal[
    "BASELINE_INVALID",
    "INSUFFICIENT_VALID_AGENT_RUNS",
    "NOT_COMPARABLE",
    "AGENT_WIN",
    "RULE_WIN",
    "TIE",
]


class PlanningCostComparison(StrictModel):
    comparable: bool = False
    reasons: list[str] = Field(default_factory=list)
    verdict: CostComparisonVerdict
    rule_cost: float | None = None
    agent_median_cost: float | None = None
    delta_agent_minus_rule: float | None = None
    improvement_pct: float | None = None
    regression_within_limit: bool | None = None
    min_valid_agent_runs: int = 3
    tie_tolerance_pct: float = 0.5
    max_regression_pct: float = 5.0
    agent_statistics: AgentCostStatistics = Field(default_factory=AgentCostStatistics)


OperationalComparisonVerdict = Literal[
    "BASELINE_INVALID",
    "INSUFFICIENT_VALID_AGENT_RUNS",
    "NOT_COMPARABLE",
    "AGENT_OPERATIONAL_WIN",
    "RULE_OPERATIONAL_WIN",
    "TRADEOFF",
    "TIE",
]


class PlanningOperationalComparison(StrictModel):
    """Business-facing comparison of speed, fleet effort, travel, and waiting."""

    comparable: bool = False
    reasons: list[str] = Field(default_factory=list)
    verdict: OperationalComparisonVerdict
    strict_pass: bool = False
    requested_agent_runs: int = 0
    valid_agent_runs: int = 0
    min_valid_agent_runs: int = 3

    rule_makespan_ms: float | None = None
    agent_median_makespan_ms: float | None = None
    makespan_improvement_pct: float | None = None
    rule_throughput_operations_per_hour: float | None = None
    agent_median_throughput_operations_per_hour: float | None = None
    throughput_improvement_pct: float | None = None

    rule_used_robot_count: int = 0
    agent_median_used_robot_count: float | None = None
    used_robot_delta_agent_minus_rule: float | None = None
    rule_fleet_effort_robot_ms: float | None = None
    agent_median_fleet_effort_robot_ms: float | None = None
    fleet_effort_improvement_pct: float | None = None

    rule_total_distance_m: float | None = None
    agent_median_total_distance_m: float | None = None
    distance_improvement_pct: float | None = None
    rule_total_wait_ms: float | None = None
    agent_median_total_wait_ms: float | None = None
    wait_improvement_pct: float | None = None

    # Workload-distribution diagnostics are reported separately from the
    # calibrated operational verdict/guardrails. Lower values are better.
    rule_physical_cycle_count_range: float | None = None
    agent_median_physical_cycle_count_range: float | None = None
    physical_cycle_count_range_improvement_pct: float | None = None
    rule_physical_cycle_count_standard_deviation: float | None = None
    agent_median_physical_cycle_count_standard_deviation: float | None = None
    physical_cycle_count_standard_deviation_improvement_pct: float | None = None
    rule_physical_cycle_count_coefficient_of_variation: float | None = None
    agent_median_physical_cycle_count_coefficient_of_variation: float | None = None
    physical_cycle_count_cv_improvement_pct: float | None = None
    rule_physical_cycle_count_gini_coefficient: float | None = None
    agent_median_physical_cycle_count_gini_coefficient: float | None = None
    physical_cycle_count_gini_improvement_pct: float | None = None
    rule_scheduled_work_time_range_ms: float | None = None
    agent_median_scheduled_work_time_range_ms: float | None = None
    scheduled_work_time_range_improvement_pct: float | None = None
    rule_scheduled_work_time_standard_deviation_ms: float | None = None
    agent_median_scheduled_work_time_standard_deviation_ms: float | None = None
    scheduled_work_time_standard_deviation_improvement_pct: float | None = None
    rule_scheduled_work_time_coefficient_of_variation: float | None = None
    agent_median_scheduled_work_time_coefficient_of_variation: float | None = None
    scheduled_work_time_cv_improvement_pct: float | None = None
    rule_max_robot_finish_at_ms: float | None = None
    agent_median_max_robot_finish_at_ms: float | None = None
    max_robot_finish_at_improvement_pct: float | None = None

    distance_guardrail_passed: bool | None = None
    fleet_effort_guardrail_passed: bool | None = None
    wait_guardrail_passed: bool | None = None
    all_resource_guardrails_passed: bool | None = None
    operational_tie_tolerance_pct: float = 3.0
    min_makespan_improvement_pct: float = 5.0
    max_distance_regression_pct: float = 20.0
    max_fleet_effort_regression_pct: float = 25.0
    max_wait_regression_pct: float = 25.0


class PlanningComparisonReport(StrictModel):
    evaluation_id: str
    backend: str
    depth: str
    expected_operation_ids: list[str] = Field(default_factory=list)
    rule: BranchQualityMetrics
    agent_runs: list[BranchQualityMetrics] = Field(default_factory=list)
    agent_stability: dict[str, object] = Field(default_factory=dict)
    comparison: dict[str, object] = Field(default_factory=dict)
    operational_comparison: PlanningOperationalComparison
    cost_comparison: PlanningCostComparison
    created_at: str

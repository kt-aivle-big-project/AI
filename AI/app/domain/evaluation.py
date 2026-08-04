"""Contracts for repeatable live-LLM and HITL evaluation scenarios."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.domain.schemas import (
    AutoMissionRequest,
    HumanInteractionResumeRequest,
    RequestFinalRoute,
    RequestGateAction,
    StrictModel,
)

EvaluationCategory = Literal[
    "ROUTER_RULE",
    "ROUTER_AGENT",
    "PRE_ROUTE_HITL",
    "IN_ROUTE_HITL",
    "PRE_OPTIMIZATION_HITL",
    "INPUT_REJECTION",
    "HITL_EXCEPTION",
    "INCIDENT_AUTOMATION",
    "ADVERSARIAL",
]
EvaluationStage = Literal["ROUTER", "FULL_AGENT", "HITL_RESUME"]


class LLMEvaluationExpected(StrictModel):
    """Machine-checkable invariants instead of brittle prose matching."""

    gate_action: RequestGateAction | None = None
    final_route: RequestFinalRoute | None = None
    hitl_stage: Literal["PRE_ROUTE", "IN_ROUTE", "PRE_OPTIMIZATION"] | None = None
    reason_code: str | None = None
    input_rejection_reason_code: str | None = None
    # ``initial_status`` validates the state before a HITL response.
    # ``resume_status`` validates the state after a bounded operator response.
    # ``final_status`` remains as a backward-compatible fallback for non-HITL cases.
    initial_status: list[str] = Field(default_factory=list)
    resume_status: list[str] = Field(default_factory=list)
    final_status: list[str] = Field(default_factory=list)
    preserve_operation_ids: list[str] = Field(default_factory=list)
    must_call_nodes: list[str] = Field(default_factory=list)
    must_not_call_nodes: list[str] = Field(default_factory=list)
    no_downstream_hallucinated_ids: bool = True
    route_must_remain_locked_after_resume: bool = True
    incident_handling_modes: list[str] = Field(default_factory=list)
    expected_immediate_actions: list[str] = Field(default_factory=list)
    expected_notification_types: list[str] = Field(default_factory=list)


class LLMEvaluationScenario(StrictModel):
    """One hard warehouse-language evaluation case."""

    scenario_id: str
    title: str
    category: EvaluationCategory
    evaluation_stage: EvaluationStage
    difficulty: int = Field(ge=1, le=5)
    argument: str
    why_rule_is_or_is_not_enough: str
    request: AutoMissionRequest
    data_dir: str | None = None
    expected: LLMEvaluationExpected
    auto_response: HumanInteractionResumeRequest | None = None
    tags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hitl_expectation(self) -> "LLMEvaluationScenario":
        if self.expected.gate_action in {"ASK_CLARIFICATION", "REQUIRE_HUMAN_APPROVAL"}:
            if self.expected.hitl_stage is None or self.expected.reason_code is None:
                raise ValueError("HITL gate scenarios require hitl_stage and reason_code.")
        if self.expected.gate_action == "REJECT_INPUT":
            if not self.expected.input_rejection_reason_code:
                raise ValueError("Input-rejection scenarios require input_rejection_reason_code.")
            if self.expected.hitl_stage is not None or self.auto_response is not None:
                raise ValueError("Invalid input must not open or resume HITL.")
        if self.category == "INPUT_REJECTION":
            if not self.expected.input_rejection_reason_code:
                raise ValueError("INPUT_REJECTION scenarios require input_rejection_reason_code.")
            if self.expected.hitl_stage is not None or self.auto_response is not None:
                raise ValueError("Invalid input must not open or resume HITL.")
        if self.category in {"HITL_EXCEPTION", "PRE_OPTIMIZATION_HITL"}:
            if self.expected.hitl_stage is None or self.auto_response is None:
                raise ValueError("Exception-HITL scenarios require a stage and a bounded response.")
        if self.evaluation_stage == "HITL_RESUME" and self.auto_response is None:
            raise ValueError("HITL_RESUME scenarios require auto_response.")
        if self.evaluation_stage == "HITL_RESUME" and not (
            self.expected.resume_status or self.expected.final_status
        ):
            raise ValueError("HITL_RESUME scenarios require resume_status or final_status.")
        return self


class LLMEvaluationRun(StrictModel):
    """One measured execution of a scenario."""

    scenario_id: str
    repeat: int
    passed: bool
    duration_ms: float
    observed_gate_action: str | None = None
    observed_final_route: str | None = None
    observed_hitl_stage: str | None = None
    observed_reason_code: str | None = None
    observed_input_rejection_reason_code: str | None = None
    observed_status: str | None = None
    preserved_operation_ids: bool = False
    schema_valid: bool = True
    errors: list[str] = Field(default_factory=list)
    output_path: str | None = None


class LLMEvaluationSummary(StrictModel):
    """Aggregate live evaluation metrics for reports and dashboards."""

    version: str
    mode: str
    scenario_count: int
    run_count: int
    pass_count: int
    fail_count: int
    pass_rate: float
    route_accuracy: float
    authoritative_id_preservation_rate: float
    unnecessary_hitl_count: int
    p50_duration_ms: float
    p95_duration_ms: float
    runs: list[LLMEvaluationRun]

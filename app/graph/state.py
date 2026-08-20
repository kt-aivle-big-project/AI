"""Typed LangGraph input, internal, and output state schemas."""
from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from app.domain.schemas import (
    CuOptDynamicInputDraft,
    CuOptDynamicInputValidationResult,
    CuOptEvidenceEnrichmentResult,
    FrontendExecutionSummary,
    FormulationDecision,
    GoodsToPersonCompilationResult,
    GoodsToPersonOptions,
    GoodsToPersonRouteEnrichmentResult,
    RequestGateDecision,
    IncidentResponsePlan,
    OperatorNotification,
    HumanInteractionRequest,
    HumanInteractionResponse,
    EntryRouteDecision,
    RetrievalAgentStep,
    ParallelRetrievalPlan,
    ParallelRetrievalExecutionResult,
    RetrievalToolRequest,
    ResolvedToolRequest,
    RetrievalToolCallValidationResult,
    StructuredKeyValidationResult,
    EntityResolutionResult,
    RetrievalObservation,
    RetrievalContextSufficiencyResult,
    WorkflowValidationIssue,
    CandidateSpaceValidation,
    ClarificationResult,
    InputRejectionResult,
    ContextSnapshot,
    NormalizedWarehouseRequest,
    CuOptPayload,
    DashboardEvent,
    EventInput,
    HumanReviewResult,
    MissionIntent,
    InventoryContext,
    LLMNodeSummary,
    MapContext,
    MAPFValidationResult,
    LogicalOperationCoverageValidationResult,
    MissionSpec,
    NodeExecutionRecord,
    OptimizationRequest,
    OptimizerAssignmentValidation,
    OptimizerResult,
    OrchestrationPlan,
    PlanningMode,
    PlanningModeSource,
    PayloadValidationResult,
    PersistenceResult,
    PhysicalProblemProfile,
    PlanningRouteResolution,
    PolicyValidationResult,
    QueryResponse,
    RobotRuntimeContext,
    RouteValidationResult,
    TrafficScheduleResult,
    TerminalRelocationResult,
    SimulationPlan,
    RuntimePlanningOverrides,
    StructuredMissionInput,
    SituationGraphValidationResult,
    WarehouseSituationGraph,
    WaypointRouteExpansionResult,
    WorkflowError,
    WorkflowFailureResult,
    WorkflowHoldResult,
)


class LaroInputState(TypedDict):
    """Fields accepted when a workflow starts."""

    warehouse_id: str
    simulation_id: str
    request_mode: str
    optimization_backend: str
    planning_mode: PlanningMode
    requested_planning_mode: PlanningMode | None
    planning_mode_source: PlanningModeSource
    max_agent_steps: int
    goods_to_person_options: GoodsToPersonOptions
    runtime_overrides: RuntimePlanningOverrides
    human_responses: list[HumanInteractionResponse]
    parent_interaction_id: str | None
    evaluation_shadow_mode: bool
    events: list[EventInput]
    structured_input: StructuredMissionInput | None
    user_command: str | None
    # A Human Review resume and deterministic evaluation may carry a frozen
    # normalization result. It must be part of the graph input schema; fields
    # declared only on the internal state are discarded by LangGraph at START.
    normalized_request_override: NormalizedWarehouseRequest | None
    mission_spec: NotRequired[MissionSpec]
    max_planner_retries: int
    retry_count: int
    workflow_trace: Annotated[list[str], operator.add]
    node_execution_log: Annotated[list[NodeExecutionRecord], operator.add]
    llm_node_summaries: Annotated[list[LLMNodeSummary], operator.add]
    errors: Annotated[list[WorkflowError], operator.add]
    completed_context_nodes: Annotated[list[str], operator.add]
    workflow_status: str
    failure_requested: bool


class LaroGraphState(LaroInputState, total=False):
    """Full internal workflow state."""

    entry_route_decision: EntryRouteDecision
    normalized_request: NormalizedWarehouseRequest
    normalized_request_override: NormalizedWarehouseRequest
    request_gate_decision: RequestGateDecision
    incident_response_plan: IncidentResponsePlan
    operator_notifications: list[OperatorNotification]
    pending_human_interaction: HumanInteractionRequest | None
    pre_optimization_approval_cleared: bool
    formulation_decision: FormulationDecision
    structured_key_validation: StructuredKeyValidationResult
    retrieval_agent_step: RetrievalAgentStep
    canonical_retrieval_plan: ParallelRetrievalPlan
    parallel_retrieval_plan: ParallelRetrievalPlan
    retrieval_planner_skipped: bool
    retrieval_plan_source: str
    parallel_retrieval_execution: ParallelRetrievalExecutionResult
    retrieval_agent_step_count: int
    retrieval_agent_retry_count: int
    retrieval_agent_finished: bool
    pending_retrieval_tool_request: RetrievalToolRequest | None
    resolved_retrieval_tool_request: ResolvedToolRequest | None
    resolved_tool_requests: Annotated[list[ResolvedToolRequest], operator.add]
    retrieval_tool_call_validation: RetrievalToolCallValidationResult | None
    # Current-step values use replace semantics.  The previous v12 reducer used
    # ``operator.add`` here, so every resolver pass re-appended all historical
    # values and produced dozens of duplicate/cross-domain records in live runs.
    current_entity_resolutions: list[EntityResolutionResult]
    entity_resolution_history: Annotated[list[EntityResolutionResult], operator.add]
    current_ambiguous_references: list[str]
    current_not_found_references: list[str]
    current_user_not_found_references: list[str]
    retrieval_observations: Annotated[list[RetrievalObservation], operator.add]
    completed_retrieval_tools: Annotated[list[str], operator.add]
    retrieval_context_sufficiency: RetrievalContextSufficiencyResult
    retrieval_tool_error: WorkflowValidationIssue | None
    # Active issues are replaced whenever the responsible node succeeds or
    # emits a new diagnosis.  A separate append-only history remains available
    # for LangSmith/debugging without allowing a repaired error to keep routing
    # the live workflow.
    validation_issues: list[WorkflowValidationIssue]
    validation_issue_history: Annotated[list[WorkflowValidationIssue], operator.add]
    query_plan_retry_count: int
    retrieval_retry_count: int
    retrieval_tool_retry_count: int
    warehouse_situation_graph: WarehouseSituationGraph
    situation_graph_validation: SituationGraphValidationResult
    cuopt_dynamic_input_draft: CuOptDynamicInputDraft
    cuopt_evidence_enrichment: CuOptEvidenceEnrichmentResult
    cuopt_dynamic_input_validation: CuOptDynamicInputValidationResult
    cuopt_dynamic_input_validation_history: Annotated[list[CuOptDynamicInputValidationResult], operator.add]
    formulation_retry_count: int
    mission_intent: MissionIntent
    orchestration_plan: OrchestrationPlan
    inventory_context: InventoryContext
    map_context: MapContext
    robot_context: RobotRuntimeContext
    context_snapshot: ContextSnapshot
    graph_nodes: list[str]
    graph_node_types: dict[str, str]
    graph_arcs: list[dict[str, Any]]
    effective_mission_spec: MissionSpec
    policy_validation: PolicyValidationResult
    optimization_request: OptimizationRequest
    goods_to_person_compilation: GoodsToPersonCompilationResult
    goods_to_person_route_enrichment: GoodsToPersonRouteEnrichmentResult
    terminal_relocation: TerminalRelocationResult
    execution_payload: CuOptPayload
    execution_optimizer_result: OptimizerResult
    cuopt_payload: CuOptPayload
    payload_validation: PayloadValidationResult
    candidate_space_validation: CandidateSpaceValidation
    physical_problem_profile: PhysicalProblemProfile
    planning_route_resolution: PlanningRouteResolution
    baseline_optimizer_result: OptimizerResult
    baseline_waypoint_route_expansion: WaypointRouteExpansionResult
    baseline_traffic_schedule: TrafficScheduleResult
    optimizer_result: OptimizerResult
    optimizer_assignment_validation: OptimizerAssignmentValidation
    waypoint_route_expansion: WaypointRouteExpansionResult
    route_validation: RouteValidationResult
    traffic_schedule: TrafficScheduleResult
    mapf_validation: MAPFValidationResult
    logical_operation_coverage_validation: LogicalOperationCoverageValidationResult
    query_response: QueryResponse
    clarification: ClarificationResult
    input_rejection: InputRejectionResult
    workflow_hold: WorkflowHoldResult
    human_review: HumanReviewResult
    failure: WorkflowFailureResult
    persistence: PersistenceResult
    frontend_summary: FrontendExecutionSummary
    dashboard_event: DashboardEvent
    simulation_plan: SimulationPlan
    replan_feedback: list[str]
    failure_stage: str


class LaroOutputState(TypedDict, total=False):
    """Fields exposed after terminal execution."""

    warehouse_id: str
    simulation_id: str
    request_mode: str
    optimization_backend: str
    planning_mode: PlanningMode
    requested_planning_mode: PlanningMode | None
    planning_mode_source: PlanningModeSource
    max_agent_steps: int
    goods_to_person_options: GoodsToPersonOptions
    runtime_overrides: RuntimePlanningOverrides
    human_responses: list[HumanInteractionResponse]
    parent_interaction_id: str | None
    events: list[EventInput]
    structured_input: StructuredMissionInput | None
    workflow_status: str
    workflow_trace: list[str]
    node_execution_log: list[NodeExecutionRecord]
    llm_node_summaries: list[LLMNodeSummary]
    errors: list[WorkflowError]
    entry_route_decision: EntryRouteDecision
    normalized_request: NormalizedWarehouseRequest
    request_gate_decision: RequestGateDecision
    incident_response_plan: IncidentResponsePlan
    operator_notifications: list[OperatorNotification]
    pending_human_interaction: HumanInteractionRequest | None
    pre_optimization_approval_cleared: bool
    formulation_decision: FormulationDecision
    structured_key_validation: StructuredKeyValidationResult
    retrieval_agent_step: RetrievalAgentStep
    canonical_retrieval_plan: ParallelRetrievalPlan
    parallel_retrieval_plan: ParallelRetrievalPlan
    retrieval_planner_skipped: bool
    retrieval_plan_source: str
    parallel_retrieval_execution: ParallelRetrievalExecutionResult
    retrieval_agent_step_count: int
    retrieval_agent_retry_count: int
    retrieval_agent_finished: bool
    pending_retrieval_tool_request: RetrievalToolRequest | None
    resolved_retrieval_tool_request: ResolvedToolRequest | None
    resolved_tool_requests: list[ResolvedToolRequest]
    retrieval_tool_call_validation: RetrievalToolCallValidationResult | None
    current_entity_resolutions: list[EntityResolutionResult]
    entity_resolution_history: list[EntityResolutionResult]
    current_ambiguous_references: list[str]
    current_not_found_references: list[str]
    current_user_not_found_references: list[str]
    retrieval_observations: list[RetrievalObservation]
    completed_retrieval_tools: list[str]
    retrieval_context_sufficiency: RetrievalContextSufficiencyResult
    retrieval_tool_error: WorkflowValidationIssue | None
    validation_issues: list[WorkflowValidationIssue]
    validation_issue_history: list[WorkflowValidationIssue]
    query_plan_retry_count: int
    retrieval_retry_count: int
    retrieval_tool_retry_count: int
    warehouse_situation_graph: WarehouseSituationGraph
    situation_graph_validation: SituationGraphValidationResult
    cuopt_dynamic_input_draft: CuOptDynamicInputDraft
    cuopt_evidence_enrichment: CuOptEvidenceEnrichmentResult
    cuopt_dynamic_input_validation: CuOptDynamicInputValidationResult
    cuopt_dynamic_input_validation_history: list[CuOptDynamicInputValidationResult]
    formulation_retry_count: int
    mission_intent: MissionIntent
    orchestration_plan: OrchestrationPlan
    inventory_context: InventoryContext
    map_context: MapContext
    robot_context: RobotRuntimeContext
    context_snapshot: ContextSnapshot
    effective_mission_spec: MissionSpec
    policy_validation: PolicyValidationResult
    optimization_request: OptimizationRequest
    goods_to_person_compilation: GoodsToPersonCompilationResult
    goods_to_person_route_enrichment: GoodsToPersonRouteEnrichmentResult
    terminal_relocation: TerminalRelocationResult
    execution_payload: CuOptPayload
    execution_optimizer_result: OptimizerResult
    cuopt_payload: CuOptPayload
    payload_validation: PayloadValidationResult
    candidate_space_validation: CandidateSpaceValidation
    physical_problem_profile: PhysicalProblemProfile
    planning_route_resolution: PlanningRouteResolution
    baseline_optimizer_result: OptimizerResult
    baseline_waypoint_route_expansion: WaypointRouteExpansionResult
    baseline_traffic_schedule: TrafficScheduleResult
    optimizer_result: OptimizerResult
    optimizer_assignment_validation: OptimizerAssignmentValidation
    waypoint_route_expansion: WaypointRouteExpansionResult
    route_validation: RouteValidationResult
    traffic_schedule: TrafficScheduleResult
    mapf_validation: MAPFValidationResult
    logical_operation_coverage_validation: LogicalOperationCoverageValidationResult
    query_response: QueryResponse
    clarification: ClarificationResult
    input_rejection: InputRejectionResult
    workflow_hold: WorkflowHoldResult
    human_review: HumanReviewResult
    failure: WorkflowFailureResult
    persistence: PersistenceResult
    frontend_summary: FrontendExecutionSummary
    dashboard_event: DashboardEvent
    simulation_plan: SimulationPlan

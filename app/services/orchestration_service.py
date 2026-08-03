"""Thin API adapter around the typed LangGraph workflow."""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.langsmith_config import configure_langsmith_environment
from app.domain.schemas import (
    ClarificationResult,
    CuOptDynamicInputDraft,
    CuOptDynamicInputValidationResult,
    CuOptEvidenceEnrichmentResult,
    FrontendExecutionSummary,
    GoodsToPersonCompilationResult,
    GoodsToPersonPlanResult,
    GoodsToPersonRouteEnrichmentResult,
    FormulationDecision,
    RequestGateDecision,
    IncidentResponsePlan,
    OperatorNotification,
    HumanInteractionRequest,
    HumanInteractionResponse,
    EntryRouteDecision,
    RetrievalAgentStep,
    ParallelRetrievalPlan,
    ParallelRetrievalExecutionResult,
    RetrievalToolCallValidationResult,
    StructuredKeyValidationResult,
    ResolvedToolRequest,
    EntityResolutionResult,
    RetrievalObservation,
    RetrievalContextSufficiencyResult,
    WorkflowValidationIssue,
    AutoMissionRequest,
    ContextSnapshot,
    NormalizedWarehouseRequest,
    CandidateSpaceValidation,
    CuOptPayload,
    DashboardEvent,
    EventInput,
    HumanReviewResult,
    InputRejectionResult,
    InventoryContext,
    LLMNodeSummary,
    MapContext,
    MAPFValidationResult,
    LogicalOperationCoverageValidationResult,
    MissionIntent,
    MissionSpec,
    NodeExecutionRecord,
    OptimizationRequest,
    OptimizerAssignmentValidation,
    OptimizerResult,
    OrchestrationPlan,
    OrchestrationResult,
    PayloadValidationResult,
    PersistenceResult,
    PhysicalProblemProfile,
    PlanningMode,
    PlanningModeSource,
    PlanningRouteResolution,
    PolicyValidationResult,
    QueryResponse,
    RobotRuntimeContext,
    RouteValidationResult,
    TrafficScheduleResult,
    TerminalRelocationResult,
    SimulationPlan,
    SituationGraphValidationResult,
    WarehouseSituationGraph,
    WaypointRouteExpansionResult,
    WorkflowError,
    WorkflowFailureResult,
    WorkflowHoldResult,
)
from app.graph.build_graph import get_laro_graph
from app.policies.routing_policy import resolve_effective_planning_mode
from app.repositories.context import repository_instance_scope, repository_scope
from app.repositories.json_repository import create_request_repository

T = TypeVar("T", bound=BaseModel)


def _optional(value: object, model: type[T]) -> T | None:
    """Validate an optional graph output model."""

    if value is None:
        return None
    return value if isinstance(value, model) else model.model_validate(value)


class OrchestrationService:
    """Convert request DTOs to graph state and graph output to an API result."""

    def __init__(self) -> None:
        """Load settings and expose .env LangSmith settings before graph invocation."""

        self.settings = get_settings()
        configure_langsmith_environment(self.settings)

    def _resolve_planning_mode(
        self,
        request: AutoMissionRequest,
        *,
        trusted_planning_mode: PlanningMode | None = None,
    ) -> tuple[PlanningMode, PlanningModeSource]:
        """Resolve the effective mode.

        ``trusted_planning_mode`` is available only to the server-side HITL
        resume service so an already locked route cannot be changed by a client.
        """

        if trusted_planning_mode is not None:
            return trusted_planning_mode, "request_override"
        mode, source = resolve_effective_planning_mode(
            requested_mode=request.planning_mode,
            default_mode=self.settings.default_planning_mode,
            allow_request_override=self.settings.allow_request_planning_mode_override,
        )
        return mode, source

    def _initial_state(
        self,
        request: AutoMissionRequest,
        *,
        trusted_planning_mode: PlanningMode | None = None,
    ) -> dict:
        """Create explicit graph state without hidden business decisions."""

        backend = request.optimization_backend or self.settings.optimization_backend
        planning_mode, planning_mode_source = self._resolve_planning_mode(
            request, trusted_planning_mode=trusted_planning_mode
        )
        events = list(request.events)
        state = {
            "warehouse_id": request.warehouse_id,
            "simulation_id": request.simulation_id,
            "simulation_run_id": request.simulation_run_id,
            "request_mode": request.request_mode,
            "optimization_backend": backend,
            "planning_mode": planning_mode,
            "requested_planning_mode": request.planning_mode,
            "planning_mode_source": planning_mode_source,
            "max_agent_steps": request.max_agent_steps,
            "goods_to_person_options": request.goods_to_person_options,
            "runtime_overrides": request.runtime_overrides,
            "human_responses": list(request.human_responses),
            "parent_interaction_id": request.parent_interaction_id,
            "evaluation_shadow_mode": request.evaluation_shadow_mode,
            "pending_human_interaction": None,
            "incident_response_plan": None,
            "operator_notifications": [],
            "pre_optimization_approval_cleared": False,
            "events": events,
            "user_command": request.user_command,
            "normalized_request_override": request.normalized_request_override,
            "max_planner_retries": request.max_planner_retries,
            "retry_count": 0,
            "workflow_trace": [],
            "node_execution_log": [],
            "llm_node_summaries": [],
            "errors": [],
            "completed_context_nodes": [],
            "retrieval_observations": [],
            "parallel_retrieval_plan": None,
            "parallel_retrieval_execution": None,
            "completed_retrieval_tools": [],
            "validation_issues": [],
            "validation_issue_history": [],
            "query_plan_retry_count": 0,
            "retrieval_retry_count": 0,
            "retrieval_agent_step_count": 0,
            "retrieval_agent_retry_count": 0,
            "retrieval_tool_retry_count": 0,
            "pending_retrieval_tool_request": None,
            "resolved_retrieval_tool_request": None,
            "resolved_tool_requests": [],
            "retrieval_tool_call_validation": None,
            "retrieval_tool_error": None,
            "current_entity_resolutions": [],
            "entity_resolution_history": [],
            "current_ambiguous_references": [],
            "current_not_found_references": [],
            "current_user_not_found_references": [],
            "formulation_retry_count": 0,
            "cuopt_dynamic_input_validation_history": [],
            "workflow_status": "running",
            "failure_requested": False,
        }
        if request.mission_spec is not None:
            state["mission_spec"] = request.mission_spec
        return state

    def run(
        self,
        request: AutoMissionRequest,
        *,
        trusted_planning_mode: PlanningMode | None = None,
        persist_simulation_plan: bool = True,
    ) -> OrchestrationResult:
        """Invoke one complete workflow and return a typed terminal result."""

        backend = request.optimization_backend or self.settings.optimization_backend
        planning_mode, planning_mode_source = self._resolve_planning_mode(
            request, trusted_planning_mode=trusted_planning_mode
        )
        with repository_scope(request.warehouse_id, request.simulation_id):
            request_repository = create_request_repository(
                request.warehouse_id,
                request.simulation_id,
                request.simulation_run_id,
            )
            with repository_instance_scope(request_repository):
                final = get_laro_graph().invoke(
                    self._initial_state(request, trusted_planning_mode=trusted_planning_mode),
                    config={
                "run_name": "LARO::orchestration",
                "tags": [
                    "laro",
                    f"warehouse:{request.warehouse_id}",
                    f"request-mode:{request.request_mode}",
                    f"optimizer:{backend}",
                    f"planning-mode:{planning_mode}",
                    f"environment:{self.settings.app_env}",
                ],
                "metadata": {
                    "warehouse_id": request.warehouse_id,
                    "simulation_id": request.simulation_id,
                    "simulation_run_id": request.simulation_run_id,
                    "request_mode": request.request_mode,
                    "optimization_backend": backend,
                    "planning_mode": planning_mode,
                    "requested_planning_mode": request.planning_mode,
                    "planning_mode_source": planning_mode_source,
                    "max_agent_steps": request.max_agent_steps,
                    "event_types": [event.type for event in request.events],
                    "mission_spec_supplied": request.mission_spec is not None,
                },
                    },
                )
        errors = [
            value if isinstance(value, WorkflowError) else WorkflowError.model_validate(value)
            for value in final.get("errors", [])
        ]
        node_log = [
            value if isinstance(value, NodeExecutionRecord) else NodeExecutionRecord.model_validate(value)
            for value in final.get("node_execution_log", [])
        ]
        llm_summaries = [
            value if isinstance(value, LLMNodeSummary) else LLMNodeSummary.model_validate(value)
            for value in final.get("llm_node_summaries", [])
        ]
        events = [
            value if isinstance(value, EventInput) else EventInput.model_validate(value)
            for value in final.get("events", [])
        ]
        retrieval_observations = [
            value if isinstance(value, RetrievalObservation) else RetrievalObservation.model_validate(value)
            for value in final.get("retrieval_observations", [])
        ]
        resolved_tool_requests = [
            value if isinstance(value, ResolvedToolRequest) else ResolvedToolRequest.model_validate(value)
            for value in final.get("resolved_tool_requests", [])
        ]
        validation_issues = [
            value if isinstance(value, WorkflowValidationIssue) else WorkflowValidationIssue.model_validate(value)
            for value in final.get("validation_issues", [])
        ]
        validation_issue_history = [
            value if isinstance(value, WorkflowValidationIssue) else WorkflowValidationIssue.model_validate(value)
            for value in final.get("validation_issue_history", [])
        ]
        dynamic_validation_history = [
            value
            if isinstance(value, CuOptDynamicInputValidationResult)
            else CuOptDynamicInputValidationResult.model_validate(value)
            for value in final.get("cuopt_dynamic_input_validation_history", [])
        ]
        plan = _optional(final.get("orchestration_plan"), OrchestrationPlan)
        result = OrchestrationResult(
            warehouse_id=request.warehouse_id,
            simulation_id=request.simulation_id,
            simulation_run_id=request.simulation_run_id,
            request_mode=request.request_mode,
            optimization_backend=backend,
            planning_mode=plan.planning_mode if plan is not None else planning_mode,
            effective_planning_mode=plan.planning_mode if plan is not None else planning_mode,
            requested_planning_mode=request.planning_mode,
            planning_mode_source=planning_mode_source,
            status=final.get("workflow_status", "failed"),
            workflow_trace=list(final.get("workflow_trace", [])),
            node_execution_log=node_log,
            llm_node_summaries=llm_summaries,
            errors=errors,
            events=events,
            entry_route_decision=_optional(final.get("entry_route_decision"), EntryRouteDecision),
            orchestration_plan=plan,
            normalized_request=_optional(final.get("normalized_request"), NormalizedWarehouseRequest),
            request_gate_decision=_optional(final.get("request_gate_decision"), RequestGateDecision),
            incident_response_plan=_optional(final.get("incident_response_plan"), IncidentResponsePlan),
            operator_notifications=[
                value if isinstance(value, OperatorNotification) else OperatorNotification.model_validate(value)
                for value in final.get("operator_notifications", [])
            ],
            pending_human_interaction=_optional(
                final.get("pending_human_interaction"), HumanInteractionRequest
            ),
            human_responses=[
                value if isinstance(value, HumanInteractionResponse) else HumanInteractionResponse.model_validate(value)
                for value in final.get("human_responses", [])
            ],
            formulation_decision=_optional(final.get("formulation_decision"), FormulationDecision),
            structured_key_validation=_optional(
                final.get("structured_key_validation"), StructuredKeyValidationResult
            ),
            retrieval_agent_step=_optional(final.get("retrieval_agent_step"), RetrievalAgentStep),
            parallel_retrieval_plan=_optional(
                final.get("parallel_retrieval_plan"), ParallelRetrievalPlan
            ),
            parallel_retrieval_execution=_optional(
                final.get("parallel_retrieval_execution"), ParallelRetrievalExecutionResult
            ),
            retrieval_tool_call_validation=_optional(
                final.get("retrieval_tool_call_validation"), RetrievalToolCallValidationResult
            ),
            resolved_retrieval_tool_request=_optional(
                final.get("resolved_retrieval_tool_request"), ResolvedToolRequest
            ),
            resolved_tool_requests=resolved_tool_requests,
            current_entity_resolutions=[
                value if isinstance(value, EntityResolutionResult) else EntityResolutionResult.model_validate(value)
                for value in final.get("current_entity_resolutions", [])
            ],
            entity_resolution_history=[
                value if isinstance(value, EntityResolutionResult) else EntityResolutionResult.model_validate(value)
                for value in final.get("entity_resolution_history", [])
            ],
            retrieval_agent_step_count=int(final.get("retrieval_agent_step_count", 0)),
            retrieval_agent_retry_count=int(final.get("retrieval_agent_retry_count", 0)),
            retrieval_tool_retry_count=int(final.get("retrieval_tool_retry_count", 0)),
            retrieval_observations=retrieval_observations,
            retrieval_context_sufficiency=_optional(
                final.get("retrieval_context_sufficiency"), RetrievalContextSufficiencyResult
            ),
            validation_issues=validation_issues,
            validation_issue_history=validation_issue_history,
            warehouse_situation_graph=_optional(
                final.get("warehouse_situation_graph"), WarehouseSituationGraph
            ),
            situation_graph_validation=_optional(
                final.get("situation_graph_validation"), SituationGraphValidationResult
            ),
            cuopt_dynamic_input_draft=_optional(
                final.get("cuopt_dynamic_input_draft"), CuOptDynamicInputDraft
            ),
            cuopt_evidence_enrichment=_optional(
                final.get("cuopt_evidence_enrichment"), CuOptEvidenceEnrichmentResult
            ),
            cuopt_dynamic_input_validation=_optional(
                final.get("cuopt_dynamic_input_validation"), CuOptDynamicInputValidationResult
            ),
            cuopt_dynamic_input_validation_history=dynamic_validation_history,
            mission_intent=_optional(final.get("mission_intent"), MissionIntent),
            context_snapshot=_optional(final.get("context_snapshot"), ContextSnapshot),
            inventory_context=_optional(final.get("inventory_context"), InventoryContext),
            map_context=_optional(final.get("map_context"), MapContext),
            robot_context=_optional(final.get("robot_context"), RobotRuntimeContext),
            effective_mission_spec=_optional(
                final.get("effective_mission_spec") or final.get("mission_spec"), MissionSpec
            ),
            policy_validation=_optional(final.get("policy_validation"), PolicyValidationResult),
            optimization_request=_optional(final.get("optimization_request"), OptimizationRequest),
            goods_to_person_compilation=_optional(
                final.get("goods_to_person_compilation"), GoodsToPersonCompilationResult
            ),
            goods_to_person_route_enrichment=_optional(
                final.get("goods_to_person_route_enrichment"), GoodsToPersonRouteEnrichmentResult
            ),
            terminal_relocation=_optional(
                final.get("terminal_relocation"), TerminalRelocationResult
            ),
            execution_payload=_optional(final.get("execution_payload"), CuOptPayload),
            execution_optimizer_result=_optional(
                final.get("execution_optimizer_result"), OptimizerResult
            ),
            cuopt_payload=_optional(final.get("cuopt_payload"), CuOptPayload),
            payload_validation=_optional(final.get("payload_validation"), PayloadValidationResult),
            candidate_space_validation=_optional(
                final.get("candidate_space_validation"), CandidateSpaceValidation
            ),
            physical_problem_profile=_optional(
                final.get("physical_problem_profile"), PhysicalProblemProfile
            ),
            planning_route_resolution=_optional(
                final.get("planning_route_resolution"), PlanningRouteResolution
            ),
            optimizer_result=_optional(final.get("optimizer_result"), OptimizerResult),
            optimizer_assignment_validation=_optional(
                final.get("optimizer_assignment_validation"), OptimizerAssignmentValidation
            ),
            waypoint_route_expansion=_optional(
                final.get("waypoint_route_expansion"), WaypointRouteExpansionResult
            ),
            route_validation=_optional(final.get("route_validation"), RouteValidationResult),
            traffic_schedule=_optional(final.get("traffic_schedule"), TrafficScheduleResult),
            mapf_validation=_optional(final.get("mapf_validation"), MAPFValidationResult),
            logical_operation_coverage_validation=_optional(
                final.get("logical_operation_coverage_validation"),
                LogicalOperationCoverageValidationResult,
            ),
            goods_to_person_plan=_optional(
                final.get("goods_to_person_plan"), GoodsToPersonPlanResult
            ),
            query_response=_optional(final.get("query_response"), QueryResponse),
            clarification=_optional(final.get("clarification"), ClarificationResult),
            input_rejection=_optional(final.get("input_rejection"), InputRejectionResult),
            workflow_hold=_optional(final.get("workflow_hold"), WorkflowHoldResult),
            human_review=_optional(final.get("human_review"), HumanReviewResult),
            failure=_optional(final.get("failure"), WorkflowFailureResult),
            frontend_summary=_optional(final.get("frontend_summary"), FrontendExecutionSummary),
            persistence=_optional(final.get("persistence"), PersistenceResult),
            dashboard_event=_optional(final.get("dashboard_event"), DashboardEvent),
            simulation_plan=_optional(final.get("simulation_plan"), SimulationPlan),
        )
        if (
            result.status == "plan_validated"
            and result.simulation_plan is not None
            and persist_simulation_plan
        ):
            from app.services.simulation_plan_service import SimulationPlanStore
            SimulationPlanStore().save(result.simulation_plan, result)
        return result

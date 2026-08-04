"""Direct Rule-key validation and the stepwise Agent retrieval loop."""
from __future__ import annotations

from app.core.llm_gateway import get_default_llm_gateway
from app.core.node_observability import observe_node
from app.domain.schemas import (
    ClarificationResult,
    HumanReviewResult,
    InputRejectionResult,
    HumanInteractionResponse,
    NormalizedWarehouseRequest,
    ResolvedToolRequest,
    RetrievalAgentStep,
    RetrievalContextSufficiencyResult,
    RetrievalToolCallValidationResult,
    RetrievalToolRequest,
    WorkflowValidationIssue,
)
from app.graph.node_support import error_update, llm_summary, model_from_state, require_locked_route, trace_update
from app.graph.state import LaroGraphState
from app.prompts.retrieval_agent import PROMPT_VERSION, RETRIEVAL_AGENT_SYSTEM
from app.services.context_service import (
    apply_runtime_map_overrides,
    apply_runtime_overrides,
)
from app.services.stepwise_retrieval_service import (
    ObservationContextMaterializer,
    RetrievalToolCallValidator,
    StepwiseQueryKeyResolver,
    StepwiseRetrievalSufficiencyValidator,
    WarehouseReadToolExecutor,
)


def _tool_catalog() -> list[dict[str, str]]:
    return [
        {"name": "find_orders", "purpose": "List or search orders for query-only workflows; never infer an executable order from item text."},
        {"name": "get_order_facts", "purpose": "Load authoritative item, quantity, priority, status, and destination for resolved order IDs."},
        {"name": "get_inventory_candidates", "purpose": "Load every positive-quantity stock/rack candidate for resolved orders or items."},
        {"name": "get_robot_candidates", "purpose": "Load the complete authoritative robot runtime and baseline eligibility without pruning solver candidates."},
        {"name": "resolve_map_entities", "purpose": "Validate canonical node, outbound, rack, or edge IDs and expand typed map relationships."},
        {"name": "get_connecting_subgraph", "purpose": "Build directed robot-to-stock and stock-to-destination path evidence from prior observations."},
        {"name": "get_runtime_constraints", "purpose": "Load congestion, occupancy, reservations, and blockage affecting relevant paths."},
        {"name": "get_active_operations", "purpose": "Load active or loaded robots for recovery decisions."},
    ]


@observe_node(
    "llm_retrieval_agent",
    purpose="이전 Observation을 보고 다음 읽기 Tool 하나 또는 조회 종료를 선택",
    llm_used=True,
)
def llm_retrieval_agent_node(state: LaroGraphState) -> dict:
    """Select one bounded read-only Tool call per LLM invocation."""

    try:
        require_locked_route(state, expected_route="AGENT_MISSION_PIPELINE")
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        observations = list(state.get("retrieval_observations", []))
        sufficiency = state.get("retrieval_context_sufficiency")
        step_count = int(state.get("retrieval_agent_step_count", 0))
        if step_count >= int(state.get("max_agent_steps", 6)):
            return {
                "human_review": HumanReviewResult(
                    reason="The retrieval agent reached its bounded step limit.",
                    details=[value.summary for value in observations],
                ),
                **trace_update("llm_retrieval_agent"),
            }
        gateway = get_default_llm_gateway()
        step = gateway.invoke_structured(
            system_prompt=RETRIEVAL_AGENT_SYSTEM,
            user_payload={
                "normalized_request": request.model_dump(mode="json"),
                "tool_catalog": _tool_catalog(),
                "previous_observations": [
                    {
                        "observation_id": value.observation_id,
                        "tool_name": value.tool_name,
                        "summary": value.summary,
                        "canonical_entity_ids": value.canonical_entity_ids,
                        "data": value.data,
                    }
                    for value in observations
                ],
                "sufficiency": (
                    sufficiency.model_dump(mode="json")
                    if hasattr(sufficiency, "model_dump")
                    else sufficiency
                ),
                "validation_issues": [
                    value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                    for value in state.get("validation_issues", [])[-6:]
                ],
                "step_count": step_count,
                "max_steps": int(state.get("max_agent_steps", 6)),
            },
            output_model=RetrievalAgentStep,
            trace_name="LARO::llm_retrieval_agent",
            tags=["node:llm_retrieval_agent", f"prompt-v{PROMPT_VERSION}"],
            metadata={
                "simulation_id": state["simulation_id"],
                "step_count": step_count,
                "observation_count": len(observations),
            },
        )
        summary = llm_summary(
            node_name="llm_retrieval_agent",
            prompt_version=PROMPT_VERSION,
            task_summary="현재 Observation을 보고 다음 Tool 한 개 또는 조회 종료 선택",
            input_summary=f"observations={len(observations)}, step={step_count}",
            output_summary=(
                f"action={step.action}, tool={step.tool_request.tool_name if step.tool_request else None}"
            ),
            retry_count=int(state.get("retrieval_agent_retry_count", 0)),
        )
        update: dict = {
            "retrieval_agent_step": step,
            "retrieval_agent_step_count": step_count + 1,
            "llm_node_summaries": [summary],
            **trace_update("llm_retrieval_agent"),
        }
        if step.action == "CALL_TOOL":
            update["pending_retrieval_tool_request"] = step.tool_request
        elif step.action == "ASK_CLARIFICATION":
            update["clarification"] = ClarificationResult(
                reason="The retrieval agent found a genuine unresolved operator reference.",
                questions=step.clarification_questions,
            )
        elif step.action == "HUMAN_REVIEW":
            update["human_review"] = HumanReviewResult(
                reason=step.human_review_reason or "The retrieval agent requested operator review.",
                details=[step.rationale_summary],
            )
        return update
    except Exception as exc:
        return error_update(
            stage="llm_retrieval_agent",
            code="llm_retrieval_agent_failed",
            message=str(exc),
            retryable=True,
        )


@observe_node(
    "retrieval_tool_call_validator",
    purpose="LLM이 선택한 단일 Tool의 허용목록·인자·선행 Observation·Raw Query 금지를 검증",
)
def retrieval_tool_call_validator_node(state: LaroGraphState) -> dict:
    """Validate the current proposed Tool call only."""

    try:
        request = model_from_state(state, "pending_retrieval_tool_request", RetrievalToolRequest)
        result = RetrievalToolCallValidator().validate(
            request=request,
            observations=list(state.get("retrieval_observations", [])),
        )
        issues = [
            WorkflowValidationIssue(
                code=value.split(":", 1)[0],
                node_name="retrieval_tool_call_validator",
                message=value,
                retryable=True,
                repair_target="RETRIEVAL_AGENT",
            )
            for value in result.errors
        ]
        return {
            "retrieval_tool_call_validation": result,
            "validation_issues": issues,
            "validation_issue_history": issues,
            **trace_update("retrieval_tool_call_validator"),
        }
    except Exception as exc:
        return error_update(stage="retrieval_tool_call_validator", code="tool_call_validation_failed", message=str(exc))


@observe_node(
    "query_key_resolver",
    purpose="현재 Tool 호출에 필요한 자연어 참조만 실제 Canonical ID로 확정",
)
def query_key_resolver_node(state: LaroGraphState) -> dict:
    """Resolve only the identifiers needed by the current Tool call."""

    try:
        tool_request = model_from_state(state, "pending_retrieval_tool_request", RetrievalToolRequest)
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        hitl_selected_ids: list[str] = []
        for value in state.get("human_responses", []):
            response = (
                value
                if isinstance(value, HumanInteractionResponse)
                else HumanInteractionResponse.model_validate(value)
            )
            if response.action in {"SELECT", "APPROVE"} and response.resolution_code in {
                "ENTITY_REFERENCE_AMBIGUOUS",
                "ENTITY_REFERENCE_NOT_FOUND",
                "AUTHORITATIVE_DATA_CONFLICT",
            }:
                hitl_selected_ids.extend(response.selected_entity_ids)

        outcome = StepwiseQueryKeyResolver().resolve(
            tool_request=tool_request,
            normalized_request=request,
            observations=list(state.get("retrieval_observations", [])),
            selected_entity_ids=list(dict.fromkeys(hitl_selected_ids)),
        )
        issues: list[WorkflowValidationIssue] = []
        exact_only_request = bool(tool_request.exact_ids) and not tool_request.raw_references
        input_rejection: InputRejectionResult | None = None
        for raw in outcome.ambiguous_references:
            if exact_only_request:
                issues.append(WorkflowValidationIssue(
                    code="AUTHORITATIVE_DATA_CONFLICT",
                    node_name="query_key_resolver",
                    message=f"Canonical identifier {raw!r} maps to conflicting authoritative records.",
                    entity_ids=[raw],
                    requires_human_review=True,
                ))
            else:
                input_rejection = InputRejectionResult(
                    reason_code="CANONICAL_RESOURCE_ID_REQUIRED",
                    message="Descriptive entity references are not valid executable mission identifiers.",
                    invalid_references=list(outcome.ambiguous_references),
                    required_identifier_types=["canonical warehouse entity ID"],
                )
        for raw in outcome.not_found_references:
            user_owned = raw in outcome.user_owned_not_found_references
            if user_owned:
                input_rejection = InputRejectionResult(
                    reason_code="CANONICAL_ENTITY_NOT_FOUND",
                    message=f"Canonical warehouse identifier {raw!r} was not found.",
                    invalid_references=list(outcome.user_owned_not_found_references),
                    required_identifier_types=["existing canonical warehouse entity ID"],
                )
            else:
                issues.append(WorkflowValidationIssue(
                    code="ENTITY_REFERENCE_NOT_FOUND",
                    node_name="query_key_resolver",
                    message=f"No authoritative entity matched {raw!r}.",
                    entity_ids=[raw],
                    retryable=True,
                    repair_target="RETRIEVAL_AGENT",
                ))
        update: dict = {
            "current_entity_resolutions": outcome.entity_resolutions,
            "entity_resolution_history": outcome.entity_resolutions,
            "validation_issues": issues,
            "validation_issue_history": issues,
            "current_ambiguous_references": outcome.ambiguous_references,
            "current_not_found_references": outcome.not_found_references,
            "current_user_not_found_references": outcome.user_owned_not_found_references,
            "input_rejection": input_rejection,
            **trace_update("query_key_resolver"),
        }
        if outcome.request is not None:
            update["resolved_retrieval_tool_request"] = outcome.request
            update["resolved_tool_requests"] = [outcome.request]
        return update
    except Exception as exc:
        return error_update(stage="query_key_resolver", code="query_key_resolution_failed", message=str(exc))


@observe_node(
    "retrieval_tool_executor",
    purpose="해석된 단일 읽기 Tool을 실제 Repository Adapter로 실행하고 Observation 하나를 저장",
)
def retrieval_tool_executor_node(state: LaroGraphState) -> dict:
    """Execute exactly one resolved read Tool."""

    try:
        request = model_from_state(state, "resolved_retrieval_tool_request", ResolvedToolRequest)
        pending = model_from_state(state, "pending_retrieval_tool_request", RetrievalToolRequest)
        fingerprint = RetrievalToolCallValidator.fingerprint(pending)
        observation = WarehouseReadToolExecutor().execute(
            request=request,
            observations=list(state.get("retrieval_observations", [])),
            request_fingerprint=fingerprint,
        )
        return {
            "retrieval_observations": [observation],
            "completed_retrieval_tools": [request.tool_name],
            "retrieval_tool_retry_count": 0,
            "retrieval_tool_error": None,
            "pending_retrieval_tool_request": None,
            "resolved_retrieval_tool_request": None,
            "validation_issues": [],
            "current_ambiguous_references": [],
            "current_not_found_references": [],
            "current_user_not_found_references": [],
            **trace_update("retrieval_tool_executor"),
        }
    except Exception as exc:
        issue = WorkflowValidationIssue(
            code="RETRIEVAL_TOOL_EXECUTION_FAILED",
            node_name="retrieval_tool_executor",
            message=str(exc),
            retryable=True,
            repair_target="TOOL_EXECUTOR",
        )
        return {
            "retrieval_tool_error": issue,
            "validation_issues": [issue],
            "validation_issue_history": [issue],
            **trace_update("retrieval_tool_executor"),
        }


@observe_node(
    "retrieval_context_sufficiency_guard",
    purpose="누적 Observation이 Situation Graph 작성에 충분한지 판단하고 다음 Tool 후보를 제시",
)
def retrieval_context_sufficiency_guard_node(state: LaroGraphState) -> dict:
    """Validate accumulated observations after every Tool call or FINALIZE request."""

    try:
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        result = StepwiseRetrievalSufficiencyValidator().validate(
            request=request,
            observations=list(state.get("retrieval_observations", [])),
        )
        update: dict = {
            "retrieval_context_sufficiency": result,
            "retrieval_agent_finished": result.ready,
            "validation_issues": result.errors,
            "validation_issue_history": result.errors,
            **trace_update("retrieval_context_sufficiency_guard"),
        }
        if result.ambiguous_references:
            update["clarification"] = ClarificationResult(
                reason="Multiple authoritative warehouse entities matched the operator reference.",
                questions=[f"다음 후보 중 대상을 지정해 주세요: {value}" for value in result.ambiguous_references],
            )
        elif result.not_found_references:
            update["clarification"] = ClarificationResult(
                reason="The operator reference was not found in authoritative warehouse data.",
                questions=[f"식별자를 확인해 주세요: {value}" for value in result.not_found_references],
            )
        elif any(value.requires_human_review for value in result.errors):
            update["human_review"] = HumanReviewResult(
                reason="Authoritative retrieval found a business-state exception.",
                details=[value.message for value in result.errors if value.requires_human_review],
            )
        return update
    except Exception as exc:
        return error_update(stage="retrieval_context_sufficiency_guard", code="retrieval_sufficiency_failed", message=str(exc))


@observe_node(
    "agent_context_materializer",
    purpose="실제 Tool Observation만 사용해 Inventory·Robot·Map Typed Context와 전체 Graph 배열을 구성",
)
def agent_context_materializer_node(state: LaroGraphState) -> dict:
    """Convert accumulated observations into the common typed context contracts."""

    try:
        require_locked_route(state, expected_route="AGENT_MISSION_PIPELINE")
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        (
            canonical_request,
            inventory,
            robots,
            map_context,
            graph_nodes,
            graph_node_types,
            graph_arcs,
        ) = ObservationContextMaterializer().materialize(
            normalized_request=request,
            observations=list(state.get("retrieval_observations", [])),
            entity_resolutions=list(state.get("entity_resolution_history", [])),
        )
        robots = apply_runtime_overrides(robots, state.get("runtime_overrides"))
        map_context = apply_runtime_map_overrides(
            map_context,
            state.get("runtime_overrides"),
        )
        return {
            "normalized_request": canonical_request,
            "inventory_context": inventory,
            "robot_context": robots,
            "map_context": map_context,
            "graph_nodes": graph_nodes,
            "graph_node_types": graph_node_types,
            "graph_arcs": graph_arcs,
            "completed_context_nodes": ["inventory_context", "map_context", "robot_runtime"],
            "retrieval_agent_finished": True,
            **trace_update("agent_context_materializer"),
        }
    except Exception as exc:
        return error_update(stage="agent_context_materializer", code="agent_context_materialization_failed", message=str(exc))


@observe_node(
    "retrieval_agent_retry_prepare",
    purpose="잘못된 Tool 호출·허구 ID 오류를 구조화해 Retrieval Agent 1회 수정 준비",
)
def retrieval_agent_retry_prepare_node(state: LaroGraphState) -> dict:
    """Increment bounded semantic retrieval repair count."""

    return {
        "retrieval_agent_retry_count": int(state.get("retrieval_agent_retry_count", 0)) + 1,
        "pending_retrieval_tool_request": None,
        "resolved_retrieval_tool_request": None,
        "retrieval_tool_call_validation": None,
        "current_entity_resolutions": [],
        "current_ambiguous_references": [],
        "current_not_found_references": [],
        "current_user_not_found_references": [],
        **trace_update("retrieval_agent_retry_prepare"),
    }


@observe_node(
    "retrieval_tool_retry_prepare",
    purpose="일시적 Adapter 오류 후 동일 Tool을 한 번 다시 실행하도록 준비",
)
def retrieval_tool_retry_prepare_node(state: LaroGraphState) -> dict:
    """Increment the deterministic Tool retry count."""

    return {
        "retrieval_tool_retry_count": int(state.get("retrieval_tool_retry_count", 0)) + 1,
        "retrieval_tool_error": None,
        "validation_issues": [],
        **trace_update("retrieval_tool_retry_prepare"),
    }

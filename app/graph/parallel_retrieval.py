"""Single-LLM retrieval planning and deterministic parallel execution nodes."""
from __future__ import annotations

from app.core.config import get_settings
from app.core.llm_gateway import get_default_llm_gateway
from app.core.node_observability import observe_node
from app.domain.schemas import (
    InputRejectionResult,
    NormalizedWarehouseRequest,
    ParallelRetrievalPlan,
)
from app.graph.node_support import error_update, llm_summary, model_from_state, require_locked_route, trace_update
from app.graph.state import LaroGraphState
from app.prompts.retrieval_planner import PROMPT_VERSION, RETRIEVAL_PLANNER_SYSTEM
from app.services.parallel_retrieval_service import (
    ParallelRetrievalExecutor,
    ParallelRetrievalPlanCompiler,
    ParallelRetrievalPlanValidator,
)


def _catalog() -> list[dict[str, str]]:
    return [
        {"name": "get_order_facts", "purpose": "Authoritative facts for canonical order IDs."},
        {"name": "get_inbound_facts", "purpose": "Authoritative inbound receipt, handoff, handling-unit, and putaway facts."},
        {"name": "get_inventory_candidates", "purpose": "Stock candidates and rack access nodes."},
        {"name": "get_robot_candidates", "purpose": "Complete robot runtime and eligibility."},
        {"name": "resolve_map_entities", "purpose": "Validate canonical edge/node/access IDs."},
        {"name": "get_connecting_subgraph", "purpose": "Directed robot-access-destination path evidence."},
        {"name": "get_runtime_constraints", "purpose": "Congestion, occupancy, reservations, and blocks."},
        {"name": "get_active_operations", "purpose": "Active or loaded robots for recovery."},
        {"name": "find_orders", "purpose": "Query-only order listing; never infer an executable order by product name."},
    ]


@observe_node(
    "canonical_retrieval_key_builder",
    purpose="입력의 Canonical ID를 직접 조회 키와 파생 키 규칙으로 구조화",
)
def canonical_retrieval_key_builder_node(state: LaroGraphState) -> dict:
    """Build the deterministic base DAG before any optional retrieval-planning LLM."""

    try:
        require_locked_route(state, expected_route="AGENT_MISSION_PIPELINE")
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        settings = get_settings()
        compiler = ParallelRetrievalPlanCompiler()
        canonical = compiler.build_canonical_plan(normalized_request=request)
        invoke_optional = compiler.should_invoke_optional_planner(
            normalized_request=request,
            canonical_plan=canonical,
            mode=settings.agent_optional_retrieval_planner,
        )
        return {
            "canonical_retrieval_plan": canonical,
            "parallel_retrieval_plan": ParallelRetrievalPlan(
                requests=[],
                planning_summary=(
                    "Optional LLM retrieval planning is pending."
                    if invoke_optional
                    else "Canonical key/DAG plan is sufficient; optional LLM planner skipped."
                ),
            ),
            "retrieval_planner_skipped": not invoke_optional,
            "retrieval_plan_source": (
                "canonical_plus_optional_llm" if invoke_optional else "canonical_only"
            ),
            **trace_update("canonical_retrieval_key_builder"),
        }
    except Exception as exc:
        return error_update(
            stage="canonical_retrieval_key_builder",
            code="canonical_retrieval_key_build_failed",
            message=str(exc),
        )


@observe_node(
    "llm_retrieval_planner",
    purpose="한 번의 LLM 호출로 읽기 전용 Tool 의존성 계획을 생성",
    llm_used=True,
)
def llm_retrieval_planner_node(state: LaroGraphState) -> dict:
    """Create one bounded retrieval DAG instead of choosing tools step by step."""

    try:
        require_locked_route(state, expected_route="AGENT_MISSION_PIPELINE")
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        gateway = get_default_llm_gateway()
        plan = gateway.invoke_structured(
            system_prompt=RETRIEVAL_PLANNER_SYSTEM,
            user_payload={
                "normalized_request": request.model_dump(mode="json"),
                "canonical_retrieval_plan": (
                    state.get("canonical_retrieval_plan").model_dump(mode="json")
                    if state.get("canonical_retrieval_plan") is not None
                    else None
                ),
                "tool_catalog": _catalog(),
                "required_output": (
                    "Only optional non-redundant reads not already covered by the canonical plan. "
                    "An empty requests list is valid."
                ),
            },
            output_model=ParallelRetrievalPlan,
            trace_name="LARO::llm_retrieval_planner",
            tags=["node:llm_retrieval_planner", f"prompt-v{PROMPT_VERSION}"],
            metadata={"simulation_id": state["simulation_id"]},
        )
        summary = llm_summary(
            node_name="llm_retrieval_planner",
            prompt_version=PROMPT_VERSION,
            task_summary="Agent 조회 Tool DAG를 한 번에 계획",
            input_summary=f"operations={len(request.operations)}",
            output_summary=f"requests={len(plan.requests)}",
        )
        return {
            "parallel_retrieval_plan": plan,
            "retrieval_planner_skipped": False,
            "retrieval_plan_source": "canonical_plus_optional_llm",
            "llm_node_summaries": [summary],
            **trace_update("llm_retrieval_planner"),
        }
    except Exception as exc:
        return error_update(
            stage="llm_retrieval_planner",
            code="parallel_retrieval_planning_failed",
            message=str(exc),
            retryable=True,
        )


@observe_node(
    "parallel_retrieval_plan_validator",
    purpose="LLM 조회 계획을 Canonical 필수 조회와 병합하고 DAG·Tool 계약을 검증",
)
def parallel_retrieval_plan_validator_node(state: LaroGraphState) -> dict:
    """Complete and validate the LLM-authored dependency graph."""

    try:
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        plan = model_from_state(state, "parallel_retrieval_plan", ParallelRetrievalPlan)
        completed, issues = ParallelRetrievalPlanValidator().complete_and_validate(
            plan=plan,
            request=request,
            canonical_plan=state.get("canonical_retrieval_plan"),
        )
        if issues:
            return {
                "parallel_retrieval_plan": completed,
                "validation_issues": issues,
                "validation_issue_history": issues,
                **error_update(
                    stage="parallel_retrieval_plan_validator",
                    code="parallel_retrieval_plan_invalid",
                    message="; ".join(value.message for value in issues),
                ),
            }
        return {
            "parallel_retrieval_plan": completed,
            "validation_issues": [],
            **trace_update("parallel_retrieval_plan_validator"),
        }
    except Exception as exc:
        return error_update(
            stage="parallel_retrieval_plan_validator",
            code="parallel_retrieval_plan_validation_failed",
            message=str(exc),
        )


@observe_node(
    "parallel_retrieval_executor",
    purpose="의존성이 없는 PostgreSQL·Redis·Neo4j 조회를 같은 Wave에서 병렬 실행",
)
def parallel_retrieval_executor_node(state: LaroGraphState) -> dict:
    """Execute the whole read-only plan and expose auditable wave metrics."""

    try:
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        plan = model_from_state(state, "parallel_retrieval_plan", ParallelRetrievalPlan)
        selected_entity_ids = [
            entity_id
            for response in state.get("human_responses", [])
            for entity_id in getattr(response, "selected_entity_ids", [])
        ]
        outcome = ParallelRetrievalExecutor().execute(
            plan=plan,
            normalized_request=request,
            selected_entity_ids=selected_entity_ids,
            llm_planning_call_count=(
                0 if state.get("retrieval_planner_skipped") else 1
            ),
        )
        update: dict = {
            "retrieval_observations": list(outcome.observations),
            "completed_retrieval_tools": [value.tool_name for value in outcome.observations],
            "resolved_tool_requests": list(outcome.resolved_requests),
            "current_entity_resolutions": list(outcome.entity_resolutions),
            "entity_resolution_history": list(outcome.entity_resolutions),
            "retrieval_context_sufficiency": outcome.sufficiency,
            "retrieval_agent_finished": outcome.sufficiency.ready,
            "parallel_retrieval_execution": outcome.execution,
            "current_ambiguous_references": outcome.ambiguous_references,
            "current_not_found_references": outcome.not_found_references,
            "current_user_not_found_references": outcome.user_not_found_references,
            "validation_issues": outcome.execution.errors,
            "validation_issue_history": outcome.execution.errors,
            **trace_update("parallel_retrieval_executor"),
        }
        if outcome.user_not_found_references:
            update["input_rejection"] = InputRejectionResult(
                reason_code="CANONICAL_ENTITY_NOT_FOUND",
                message="One or more canonical warehouse identifiers do not exist.",
                invalid_references=outcome.user_not_found_references,
            )
        elif outcome.not_found_references and not outcome.sufficiency.ready:
            update["input_rejection"] = InputRejectionResult(
                reason_code="CANONICAL_ENTITY_NOT_FOUND",
                message="Required warehouse data could not be resolved.",
                invalid_references=outcome.not_found_references,
            )
        return update
    except Exception as exc:
        return error_update(
            stage="parallel_retrieval_executor",
            code="parallel_retrieval_execution_failed",
            message=str(exc),
            retryable=True,
        )

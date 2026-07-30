"""Rule and LLM cuOpt dynamic-input formulation nodes."""
from __future__ import annotations

from app.core.config import get_settings
from app.core.llm_gateway import get_default_llm_gateway
from app.core.node_observability import observe_node
from app.domain.schemas import (
    CuOptDynamicInputDraft,
    CuOptDynamicInputValidationResult,
    CuOptEvidenceEnrichmentResult,
    ContextSnapshot,
    InventoryContext,
    RobotRuntimeContext,
    MapContext,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    WarehouseSituationGraph,
)
from app.graph.node_support import error_update, llm_summary, model_from_state, trace_update
from app.graph.state import LaroGraphState
from app.prompts.cuopt_formulator import CUOPT_FORMULATOR_SYSTEM, PROMPT_VERSION
from app.repositories.json_repository import get_repository
from app.services.cuopt_formulation_service import (
    CuOptDraftEvidenceEnricher,
    CuOptDynamicInputValidator,
    DynamicInputOptimizationRequestAdapter,
)
from app.services.terminal_relocation_service import RobotTerminalPolicyService


def _time_limit(state: LaroGraphState) -> int:
    """Return the configured bounded solver time limit."""
    settings = get_settings()
    return (
        settings.ortools_time_limit_seconds
        if state["optimization_backend"] == "ortools"
        else settings.cuopt_time_limit_seconds
    )


@observe_node(
    "llm_cuopt_formulator",
    purpose="Warehouse Situation Graph를 근거로 cuOpt 동적 Task·Fleet·제약 입력을 직접 정식화",
    llm_used=True,
)
def llm_cuopt_formulator_node(state: LaroGraphState) -> dict:
    """Formulate or repair a dynamic cuOpt input using strict structured output."""

    try:
        graph = model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph)
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        previous = state.get("cuopt_dynamic_input_draft")
        validation = state.get("cuopt_dynamic_input_validation")
        retry_count = int(state.get("formulation_retry_count", 0))
        gateway = get_default_llm_gateway()
        draft = gateway.invoke_structured(
            system_prompt=CUOPT_FORMULATOR_SYSTEM,
            user_payload={
                "normalized_request": request.model_dump(mode="json"),
                "warehouse_situation_graph": graph.model_dump(mode="json"),
                "time_limit_seconds": _time_limit(state),
                "previous_draft": (
                    previous.model_dump(mode="json")
                    if isinstance(previous, CuOptDynamicInputDraft)
                    else previous
                ),
                "validation_errors": (
                    validation.errors
                    if isinstance(validation, CuOptDynamicInputValidationResult)
                    else []
                ),
                "retry_count": retry_count,
            },
            output_model=CuOptDynamicInputDraft,
            trace_name="LARO::llm_cuopt_formulator",
            tags=["node:llm_cuopt_formulator", f"prompt-v{PROMPT_VERSION}"],
            metadata={
                "laro_node": "llm_cuopt_formulator",
                "simulation_id": state["simulation_id"],
                "situation_node_count": len(graph.nodes),
                "situation_relation_count": len(graph.relations),
                "retry_count": retry_count,
            },
        )
        # GOODS_TO_PERSON is a warehouse execution contract, not an LLM
        # business choice.  The LLM may reason about objective/fleet/runtime
        # constraints, but physical handling-unit tasks are compiled
        # deterministically after validation.  Enforce that boundary before the
        # factual validator so one order can never become one AMR trip by accident.
        if graph.fulfillment_mode == "goods_to_person":
            draft = draft.model_copy(
                update={
                    "formulation_mode": "GOODS_TO_PERSON",
                    "g2p_order_ids": list(graph.g2p_order_ids),
                    "tasks": [],
                    "deferred_order_ids": [],
                    "formulation_summary": (
                        f"G2P formulation preserved {len(graph.g2p_order_ids)} canonical "
                        "order(s); the deterministic compiler will create handling-unit cycles."
                    ),
                }
            )
        # Snapshot, graph version, source, fleet, objective, and constraints are
        # independently checked by validators.
        summary = llm_summary(
            node_name="llm_cuopt_formulator",
            prompt_version=PROMPT_VERSION,
            task_summary="상황 그래프 근거로 cuOpt 동적 입력을 정식화 또는 1회 수정",
            input_summary=(
                f"nodes={len(graph.nodes)}, relations={len(graph.relations)}, "
                f"paths={len(graph.path_evidence)}, retry={retry_count}"
            ),
            output_summary=(
                f"mode={draft.formulation_mode}, tasks={len(draft.tasks)}, "
                f"g2p_orders={len(draft.g2p_order_ids)}, "
                f"robots={len(draft.fleet.included_robot_ids)}, "
                f"deferred={len(draft.deferred_order_ids)}"
            ),
            retry_count=retry_count,
        )
        return {
            "cuopt_dynamic_input_draft": draft,
            "formulation_retry_count": retry_count,
            "llm_node_summaries": [summary],
            **trace_update("llm_cuopt_formulator"),
        }
    except Exception as exc:
        return error_update(
            stage="llm_cuopt_formulator",
            code="llm_cuopt_formulation_failed",
            message=str(exc),
            retryable=True,
        )


@observe_node(
    "cuopt_evidence_enricher",
    purpose="LLM이 선택한 Task·Fleet·제약은 유지하고 상황 그래프의 경로·사실 Evidence ID만 기계적으로 보완",
)
def cuopt_evidence_enricher_node(state: LaroGraphState) -> dict:
    """Attach mechanically implied evidence without repairing business fields."""

    try:
        draft = model_from_state(state, "cuopt_dynamic_input_draft", CuOptDynamicInputDraft)
        if draft.formulation_source != "llm":
            result = CuOptEvidenceEnrichmentResult(applied=False)
            return {
                "cuopt_evidence_enrichment": result,
                **trace_update("cuopt_evidence_enricher"),
            }
        graph = model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph)
        enriched, result = CuOptDraftEvidenceEnricher().enrich(draft=draft, graph=graph)
        return {
            "cuopt_dynamic_input_draft": enriched,
            "cuopt_evidence_enrichment": result,
            **trace_update("cuopt_evidence_enricher"),
        }
    except Exception as exc:
        return error_update(
            stage="cuopt_evidence_enricher",
            code="cuopt_evidence_enrichment_failed",
            message=str(exc),
        )


@observe_node(
    "cuopt_dynamic_input_validator",
    purpose="LLM/Rule의 Task·Stock·Fleet·제약 초안을 원본 상황 그래프와 대조해 누락·환각·과할당을 검증",
)
def cuopt_dynamic_input_validator_node(state: LaroGraphState) -> dict:
    """Validate without silently replacing LLM-authored business choices."""

    try:
        draft = model_from_state(state, "cuopt_dynamic_input_draft", CuOptDynamicInputDraft)
        decision = state.get("formulation_decision")
        expected_source = "llm" if getattr(decision, "route", None) == "AGENT_FORMULATION" else "rule"
        validator = CuOptDynamicInputValidator()
        normalized_request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        if draft.formulation_source == "rule":
            validation = validator.validate_from_contexts(
                draft=draft,
                normalized_request=normalized_request,
                snapshot=model_from_state(state, "context_snapshot", ContextSnapshot),
                inventory=model_from_state(state, "inventory_context", InventoryContext),
                robots=model_from_state(state, "robot_context", RobotRuntimeContext),
                map_context=model_from_state(state, "map_context", MapContext),
                graph_arcs=list(state["graph_arcs"]),
                expected_source=expected_source,
            )
        else:
            validation = validator.validate(
                draft=draft,
                normalized_request=normalized_request,
                graph=model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph),
                expected_source=expected_source,
            )
        return {
            "cuopt_dynamic_input_validation": validation,
            "cuopt_dynamic_input_validation_history": [validation],
            **trace_update("cuopt_dynamic_input_validator"),
        }
    except Exception as exc:
        return error_update(
            stage="cuopt_dynamic_input_validator",
            code="cuopt_dynamic_input_validation_failed",
            message=str(exc),
        )


@observe_node(
    "optimization_request_from_dynamic_input",
    purpose="검증된 동적 입력에 권위 Robot 수치와 Runtime Map Overlay만 기계적으로 붙여 OptimizationRequest 생성",
)
def optimization_request_from_dynamic_input_node(state: LaroGraphState) -> dict:
    """Translate a validated draft without changing task, stock, or fleet choices."""

    try:
        validation = model_from_state(
            state,
            "cuopt_dynamic_input_validation",
            CuOptDynamicInputValidationResult,
        )
        if not validation.valid:
            raise ValueError("Cannot assemble OptimizationRequest from an invalid dynamic draft.")
        draft = model_from_state(state, "cuopt_dynamic_input_draft", CuOptDynamicInputDraft)
        adapter = DynamicInputOptimizationRequestAdapter()
        if draft.formulation_source == "rule":
            request = adapter.build_from_contexts(
                draft=draft,
                robots=model_from_state(state, "robot_context", RobotRuntimeContext),
                map_context=model_from_state(state, "map_context", MapContext),
            )
        else:
            request = adapter.build(
                draft=draft,
                graph=model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph),
                map_context=model_from_state(state, "map_context", MapContext),
            )
        penalty_map = {
            value.edge_id: (value.cost_multiplier, value.travel_time_multiplier)
            for value in request.map_constraints.edge_penalties
        }
        graph_arcs = get_repository().adjusted_arcs(
            blocked_edge_ids=set(request.map_constraints.blocked_edge_ids),
            blocked_node_ids=set(request.map_constraints.blocked_node_ids),
            edge_penalties=penalty_map,
        )
        request = RobotTerminalPolicyService().apply_to_request(
            request=request,
            runtime_overrides=state.get("runtime_overrides"),
            graph_arcs=graph_arcs,
            node_types=dict(state.get("graph_node_types", {})),
        )
        return {
            "optimization_request": request,
            "graph_arcs": graph_arcs,
            **trace_update("optimization_request_from_dynamic_input"),
        }
    except Exception as exc:
        return error_update(
            stage="optimization_request_from_dynamic_input",
            code="dynamic_input_assembly_failed",
            message=str(exc),
        )


@observe_node(
    "cuopt_formulation_retry_prepare",
    purpose="LLM cuOpt 정식화 검증 오류를 다음 1회 수정 호출에 전달",
)
def cuopt_formulation_retry_prepare_node(state: LaroGraphState) -> dict:
    """Increment the bounded formulation retry counter without changing the draft."""

    try:
        retry_count = int(state.get("formulation_retry_count", 0)) + 1
        return {
            "formulation_retry_count": retry_count,
            **trace_update("cuopt_formulation_retry_prepare"),
        }
    except Exception as exc:
        return error_update(
            stage="cuopt_formulation_retry_prepare",
            code="cuopt_formulation_retry_prepare_failed",
            message=str(exc),
        )

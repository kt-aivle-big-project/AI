"""Rule and LLM cuOpt dynamic-input formulation nodes."""
from __future__ import annotations

import json

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
    _apply_emergency_reserve,
    _objective_is_explicit,
    _objective_terms,
    _objective_terms_for_profile,
)
from app.services.cuopt_llm_context_service import CuOptLlmPlanningContextBuilder
from app.services.terminal_relocation_service import RobotTerminalPolicyService




def _required_operation_coverage(request: NormalizedWarehouseRequest) -> dict[str, list[str]]:
    """Return the exact operation partition that an LLM draft must preserve."""

    actionable = [
        value.operation_id
        for value in request.operations
        if value.operation_type in {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}
    ]
    outbound = [
        value.operation_id
        for value in request.operations
        if value.operation_type == "OUTBOUND_ORDER"
    ]
    direct = [
        value.operation_id
        for value in request.operations
        if value.operation_type in {"INBOUND_ITEM", "RECOVERY"}
    ]
    return {
        "actionable_operation_ids": actionable,
        "outbound_g2p_operation_ids": outbound,
        "direct_task_operation_ids": direct,
    }


def _enforce_outbound_fulfillment_contract(
    *,
    draft: CuOptDynamicInputDraft,
    graph: WarehouseSituationGraph,
) -> CuOptDynamicInputDraft:
    """Enforce only the outbound G2P boundary without deleting direct work.

    GOODS_TO_PERSON controls how outbound orders are compiled.  Inbound and
    recovery operations remain direct tasks.  Earlier versions replaced the
    entire task list with ``[]`` and silently dropped mixed-request operations.
    """

    if graph.fulfillment_mode != "goods_to_person":
        return draft
    direct_tasks = [
        task for task in draft.tasks if task.operation_type != "OUTBOUND_ORDER"
    ]
    return draft.model_copy(
        update={
            "formulation_mode": "GOODS_TO_PERSON",
            "g2p_order_ids": list(graph.g2p_order_ids),
            "tasks": direct_tasks,
            "formulation_summary": (
                f"G2P outbound formulation preserved {len(graph.g2p_order_ids)} "
                f"canonical order(s) and {len(direct_tasks)} direct non-outbound "
                "task(s); the deterministic compiler will create handling-unit "
                "cycles for outbound work."
            ),
        }
    )


def _enforce_authoritative_inbound_contract(
    *,
    draft: CuOptDynamicInputDraft,
    graph: WarehouseSituationGraph,
) -> CuOptDynamicInputDraft:
    """Restore immutable inbound facts and bind pickup to the Neo4j graph.

    The LLM may choose a valid putaway rack and policy, but it does not own the
    receipt identity, item, handling unit, priority, BOX demand, or inbound
    pickup point.  A hallucinated/occupied slot and a duplicate slot claim are
    not operator decisions: bind only those invalid choices to the cheapest
    unused graph-backed slot.  PICKUP_FROM relations are built from the active
    Neo4j handoff/access connection, so the AMR-side pickup node is compiled
    from those relations instead of trusting a free-form LLM node choice.
    """

    inbound_by_id = {
        str(node.attributes.get("inbound_id") or node.node_id.removeprefix("inbound:")): node
        for node in graph.nodes
        if node.node_type == "inbound"
    }
    tasks = []
    claimed_putaway_slots: set[tuple[str, int]] = set()
    for task in draft.tasks:
        if task.operation_type != "INBOUND_ITEM":
            tasks.append(task)
            continue
        inbound = inbound_by_id.get(task.order_id)
        if inbound is None:
            # The validator reports unknown/omitted canonical receipts.  Do not
            # guess which receipt an unrelated LLM task was intended to mean.
            tasks.append(task)
            continue
        attributes = inbound.attributes
        box_count = int(
            attributes.get("transport_unit_count")
            or attributes.get("quantity")
            or task.demand
        )
        putaway = _select_authoritative_putaway_assignment(
            inbound_id=task.order_id,
            requested_rack_id=task.rack_id,
            requested_rack_level=task.rack_level,
            requested_delivery_node=task.delivery_node,
            claimed_slot_keys=claimed_putaway_slots,
            graph=graph,
        )
        rack_id = task.rack_id
        rack_level = task.rack_level
        delivery_node = task.delivery_node
        if putaway is not None:
            rack_id, rack_level, delivery_node = putaway
            claimed_putaway_slots.add((rack_id, rack_level))
        pickup_node = _select_authoritative_inbound_pickup(
            inbound_id=task.order_id,
            delivery_node=delivery_node,
            graph=graph,
        )
        tasks.append(
            task.model_copy(
                update={
                    "order_id": str(
                        attributes.get("inbound_id")
                        or inbound.node_id.removeprefix("inbound:")
                    ),
                    "item_id": str(attributes.get("item_id") or task.item_id),
                    "stock_id": str(
                        attributes.get("handling_unit_id") or task.stock_id
                    ),
                    "demand": box_count,
                    "priority": str(attributes.get("priority") or task.priority),
                    "mandatory": True,
                    "rack_id": rack_id,
                    "rack_level": rack_level,
                    "delivery_node": delivery_node,
                    # No relation means the graph is incomplete. Preserve the
                    # original value in that case so the validator reports the
                    # missing contract instead of silently inventing a node.
                    "pickup_node": pickup_node or task.pickup_node,
                }
            )
        )
    return draft.model_copy(update={"tasks": tasks})


def _select_authoritative_putaway_assignment(
    *,
    inbound_id: str,
    requested_rack_id: str | None,
    requested_rack_level: int | None,
    requested_delivery_node: str,
    claimed_slot_keys: set[tuple[str, int]],
    graph: WarehouseSituationGraph,
) -> tuple[str, int, str] | None:
    """Keep a valid LLM slot, otherwise choose an unused reachable slot.

    PUTAWAY_TO and HAS_ACCESS_POINT are authoritative warehouse facts.  Route
    evidence is used only to rank valid candidates; it never invents a rack or
    access node.  This makes ordinary LLM identifier mistakes self-healing
    without adding another potentially slow LLM call or weakening validation.
    """

    source_node_id = f"inbound:{inbound_id}"
    slot_keys: set[tuple[str, int]] = set()
    for relation in graph.relations:
        if (
            relation.source_node_id != source_node_id
            or relation.relation_type != "PUTAWAY_TO"
            or not relation.target_node_id.startswith("rack_slot:")
        ):
            continue
        raw_slot = relation.target_node_id.removeprefix("rack_slot:")
        try:
            rack_id, level_text = raw_slot.rsplit(":L", 1)
            slot_keys.add((rack_id, int(level_text)))
        except (TypeError, ValueError):
            continue

    access_by_slot: dict[tuple[str, int], set[str]] = {}
    for rack_id, rack_level in slot_keys:
        slot_node_id = f"rack_slot:{rack_id}:L{rack_level}"
        rack_node_id = f"rack:{rack_id}"
        access_by_slot[(rack_id, rack_level)] = {
            relation.target_node_id.removeprefix("map:")
            for relation in graph.relations
            if relation.relation_type == "HAS_ACCESS_POINT"
            and relation.source_node_id in {slot_node_id, rack_node_id}
            and relation.target_node_id.startswith("map:")
        }

    requested_key = None
    if requested_rack_id is not None and requested_rack_level is not None:
        requested_key = (str(requested_rack_id), int(requested_rack_level))
    if (
        requested_key in slot_keys
        and requested_key not in claimed_slot_keys
        and requested_delivery_node in access_by_slot.get(requested_key, set())
    ):
        return requested_key[0], requested_key[1], requested_delivery_node

    pickup_nodes = {
        relation.target_node_id.removeprefix("map:")
        for relation in graph.relations
        if relation.source_node_id == source_node_id
        and relation.relation_type == "PICKUP_FROM"
        and relation.target_node_id.startswith("map:")
    }

    def best_robot_path(pickup_node: str):
        return min(
            (
                path
                for path in graph.path_evidence
                if path.purpose == "ROBOT_TO_PICKUP"
                and path.target_node_id == pickup_node
            ),
            key=lambda path: (path.travel_time_ms, path.cost, path.path_id),
            default=None,
        )

    def delivery_path(pickup_node: str, delivery_node: str):
        if pickup_node == delivery_node:
            return 0, 0.0, ""
        path = min(
            (
                value
                for value in graph.path_evidence
                if value.purpose == "PICKUP_TO_DELIVERY"
                and value.source_node_id == pickup_node
                and value.target_node_id == delivery_node
            ),
            key=lambda value: (value.travel_time_ms, value.cost, value.path_id),
            default=None,
        )
        if path is None:
            return None
        return path.travel_time_ms, path.cost, path.path_id

    candidates: list[tuple[tuple, str, int, str]] = []
    for rack_id, rack_level in sorted(slot_keys):
        slot_key = (rack_id, rack_level)
        if slot_key in claimed_slot_keys:
            continue
        for delivery_node in sorted(access_by_slot.get(slot_key, set())):
            route_scores = []
            for pickup_node in sorted(pickup_nodes):
                robot_path = best_robot_path(pickup_node)
                delivery = delivery_path(pickup_node, delivery_node)
                if robot_path is None or delivery is None:
                    continue
                delivery_time, delivery_cost, delivery_path_id = delivery
                route_scores.append(
                    (
                        robot_path.travel_time_ms + delivery_time,
                        robot_path.cost + delivery_cost,
                        pickup_node,
                        robot_path.path_id,
                        delivery_path_id,
                    )
                )
            if route_scores:
                best_route = min(route_scores)
                candidates.append(
                    (
                        (*best_route, rack_id, rack_level, delivery_node),
                        rack_id,
                        rack_level,
                        delivery_node,
                    )
                )

    if not candidates:
        return None
    _, rack_id, rack_level, delivery_node = min(candidates, key=lambda value: value[0])
    return rack_id, rack_level, delivery_node


def _select_authoritative_inbound_pickup(
    *,
    inbound_id: str,
    delivery_node: str,
    graph: WarehouseSituationGraph,
) -> str | None:
    """Choose one Neo4j-connected AMR pickup using deterministic path cost.

    The situation graph's PICKUP_FROM relations are materialized from the
    active Neo4j inbound service node and its adjacent traversable route node.
    When a handoff exposes two AMR-side nodes, prefer the candidate with a
    complete robot-to-pickup and pickup-to-delivery path, then the lowest total
    travel time/cost.  Stable node ID ordering makes equal-cost runs repeatable.
    """

    source_node_id = f"inbound:{inbound_id}"
    candidates = sorted({
        relation.target_node_id.removeprefix("map:")
        for relation in graph.relations
        if relation.source_node_id == source_node_id
        and relation.relation_type == "PICKUP_FROM"
        and relation.target_node_id.startswith("map:")
    })
    if not candidates:
        return None

    def best_path(purpose: str, pickup_node: str):
        paths = [
            path
            for path in graph.path_evidence
            if path.purpose == purpose
            and (
                path.target_node_id == pickup_node
                if purpose == "ROBOT_TO_PICKUP"
                else path.source_node_id == pickup_node
                and path.target_node_id == delivery_node
            )
        ]
        return min(
            paths,
            key=lambda path: (path.travel_time_ms, path.cost, path.path_id),
            default=None,
        )

    def score(pickup_node: str) -> tuple[int, int, float, str]:
        robot_path = best_path("ROBOT_TO_PICKUP", pickup_node)
        delivery_path = best_path("PICKUP_TO_DELIVERY", pickup_node)
        if pickup_node == delivery_node:
            delivery_time = 0
            delivery_cost = 0.0
            delivery_ready = True
        else:
            delivery_time = delivery_path.travel_time_ms if delivery_path else 0
            delivery_cost = delivery_path.cost if delivery_path else 0.0
            delivery_ready = delivery_path is not None
        complete = robot_path is not None and delivery_ready
        return (
            0 if complete else 1,
            (robot_path.travel_time_ms if robot_path else 0) + delivery_time,
            (robot_path.cost if robot_path else 0.0) + delivery_cost,
            pickup_node,
        )

    return min(candidates, key=score)


def _enforce_typed_policy_contract(
    *,
    draft: CuOptDynamicInputDraft,
    request: NormalizedWarehouseRequest,
    graph: WarehouseSituationGraph,
) -> CuOptDynamicInputDraft:
    """Apply typed objective/reserve constraints after LLM formulation.

    The LLM explains and composes the policy stack, while deterministic code
    owns the final candidate-space partition.  This prevents a valid natural
    language reserve policy from being silently ignored or interpreted as an
    ordinary exclusion.
    """

    explicit_exclusions = set(request.constraints.excluded_robot_ids)
    eligible_nodes = [
        node
        for node in graph.nodes
        if node.node_type == "robot" and bool(node.attributes.get("baseline_eligible"))
    ]
    candidate_ids = sorted(
        str(node.attributes["robot_id"])
        for node in eligible_nodes
        if str(node.attributes["robot_id"]) not in explicit_exclusions
    )
    included, reserved = _apply_emergency_reserve(
        candidate_robot_ids=candidate_ids,
        battery_by_robot={
            str(node.attributes["robot_id"]): float(node.attributes.get("battery_pct") or 0.0)
            for node in eligible_nodes
        },
        request=request,
    )
    fleet = draft.fleet.model_copy(
        update={
            "included_robot_ids": included,
            "excluded_robot_ids": sorted(explicit_exclusions),
            "reserved_robot_ids": reserved,
        }
    )
    objective_is_explicit = _objective_is_explicit(request)
    selected_objective_profile = (
        request.constraints.objective_profile
        if objective_is_explicit
        else draft.objective_profile
    )
    selected_objective_terms = (
        _objective_terms(request)
        if objective_is_explicit
        else list(dict.fromkeys(draft.objective_terms))
        or _objective_terms_for_profile(selected_objective_profile)
    )
    return draft.model_copy(
        update={
            "objective_profile": selected_objective_profile,
            "objective_terms": selected_objective_terms,
            "fleet": fleet,
            # Agent may express a fleet-distribution policy, but it never owns
            # robot assignment. The validator below checks this lower bound
            # against the authoritative eligible fleet and actionable work
            # before the deterministic adapter passes it to cuOpt.
            "minimum_vehicle_count": draft.minimum_vehicle_count,
        }
    )


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
    """Formulate a cuOpt draft from compact facts, then compile against the full graph."""

    try:
        graph = model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph)
        request = model_from_state(state, "normalized_request", NormalizedWarehouseRequest)
        previous = state.get("cuopt_dynamic_input_draft")
        validation = state.get("cuopt_dynamic_input_validation")
        retry_count = int(state.get("formulation_retry_count", 0))
        planning_context = CuOptLlmPlanningContextBuilder().build(
            request=request,
            snapshot=model_from_state(state, "context_snapshot", ContextSnapshot),
            inventory=model_from_state(state, "inventory_context", InventoryContext),
            robots=model_from_state(state, "robot_context", RobotRuntimeContext),
            map_context=model_from_state(state, "map_context", MapContext),
            graph=graph,
        )
        user_payload = {
            "normalized_request": request.model_dump(mode="json"),
            "cuopt_planning_context": planning_context,
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
            "required_operation_coverage": _required_operation_coverage(request),
        }
        payload_bytes = len(
            json.dumps(
                user_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        max_payload_bytes = get_settings().llm_cuopt_context_max_bytes
        if payload_bytes > max_payload_bytes:
            return error_update(
                stage="llm_cuopt_formulator",
                code="llm_cuopt_context_too_large",
                message=(
                    "Compact cuOpt formulation context exceeds the local safety "
                    f"limit: bytes={payload_bytes};limit={max_payload_bytes}."
                ),
                retryable=False,
            )
        gateway = get_default_llm_gateway()
        draft = gateway.invoke_structured(
            system_prompt=CUOPT_FORMULATOR_SYSTEM,
            user_payload=user_payload,
            output_model=CuOptDynamicInputDraft,
            trace_name="LARO::llm_cuopt_formulator",
            tags=["node:llm_cuopt_formulator", f"prompt-v{PROMPT_VERSION}"],
            metadata={
                "laro_node": "llm_cuopt_formulator",
                "simulation_id": state["simulation_id"],
                "situation_node_count": len(graph.nodes),
                "situation_relation_count": len(graph.relations),
                "llm_context_bytes": payload_bytes,
                "retry_count": retry_count,
            },
        )
        # GOODS_TO_PERSON is an outbound execution contract, not a request-wide
        # prohibition on direct tasks.  Enforce canonical outbound IDs while
        # preserving inbound/recovery tasks authored from authoritative facts.
        draft = _enforce_outbound_fulfillment_contract(draft=draft, graph=graph)
        draft = _enforce_authoritative_inbound_contract(draft=draft, graph=graph)
        draft = _enforce_typed_policy_contract(
            draft=draft,
            request=request,
            graph=graph,
        )
        # Snapshot, graph version, source, fleet, objective, and constraints are
        # independently checked by validators.
        summary = llm_summary(
            node_name="llm_cuopt_formulator",
            prompt_version=PROMPT_VERSION,
            task_summary="상황 그래프 근거로 cuOpt 동적 입력을 정식화 또는 1회 수정",
            input_summary=(
                f"full_nodes={len(graph.nodes)}, full_relations={len(graph.relations)}, "
                f"full_paths={len(graph.path_evidence)}, compact_bytes={payload_bytes}, "
                f"route_options={len(planning_context['task_route_options'])}, "
                f"retry={retry_count}"
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
        message = str(exc)
        oversized_rate_limit = (
            "rate_limit_exceeded" in message.lower()
            and "requested" in message.lower()
            and "limit" in message.lower()
        )
        return error_update(
            stage="llm_cuopt_formulator",
            code="llm_cuopt_formulation_failed",
            message=message,
            retryable=not oversized_rate_limit,
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
        context_validation = validator.validate_from_contexts(
            draft=draft,
            normalized_request=normalized_request,
            snapshot=model_from_state(state, "context_snapshot", ContextSnapshot),
            inventory=model_from_state(state, "inventory_context", InventoryContext),
            robots=model_from_state(state, "robot_context", RobotRuntimeContext),
            map_context=model_from_state(state, "map_context", MapContext),
            graph_arcs=list(state["graph_arcs"]),
            expected_source=expected_source,
        )
        if draft.formulation_source == "rule":
            validation = context_validation
        else:
            graph_validation = validator.validate(
                draft=draft,
                normalized_request=normalized_request,
                graph=model_from_state(state, "warehouse_situation_graph", WarehouseSituationGraph),
                expected_source=expected_source,
            )
            validation = CuOptDynamicInputValidationResult(
                valid=graph_validation.valid and context_validation.valid,
                repairable=bool(graph_validation.errors or context_validation.errors),
                errors=list(
                    dict.fromkeys([*graph_validation.errors, *context_validation.errors])
                ),
                warnings=list(
                    dict.fromkeys([*graph_validation.warnings, *context_validation.warnings])
                ),
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

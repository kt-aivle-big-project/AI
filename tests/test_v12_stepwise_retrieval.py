"""Regression tests for the v12 direct Rule path and stepwise Tool Agent."""
from __future__ import annotations

from pathlib import Path

from app.domain.schemas import (
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    RetrievalToolRequest,
    SemanticEntityReference,
)
from app.repositories.json_repository import set_data_dir
from app.graph.input_formulation import _canonicalize_normalized_request
from app.services.rule_direct_service import StructuredKeyValidator
from app.services.stepwise_retrieval_service import (
    ObservationContextMaterializer,
    RetrievalToolCallValidator,
    StepwiseQueryKeyResolver,
    StepwiseRetrievalSufficiencyValidator,
    WarehouseReadToolExecutor,
)

ROOT = Path(__file__).resolve().parents[1]
AMBIGUOUS_FIXTURE = ROOT / "scenarios" / "fixtures" / "S4_ambiguous_orders"


def request(*, operation_id: str = "ORD-001", raw_reference: str | None = None) -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id=operation_id,
                operation_type="OUTBOUND_ORDER",
                source_event_type="natural_language",
                raw_reference=raw_reference or operation_id,
            )
        ],
        constraints=NormalizedRequestConstraints(),
        raw_user_command=f"{raw_reference or operation_id} 처리해.",
        normalization_summary="test request",
    )


def execute_step(tool: RetrievalToolRequest, normalized, observations):
    validation = RetrievalToolCallValidator().validate(request=tool, observations=observations)
    assert validation.valid, validation.errors
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=tool,
        normalized_request=normalized,
        observations=observations,
    )
    assert not outcome.ambiguous_references
    assert not outcome.not_found_references
    assert outcome.request is not None
    observation = WarehouseReadToolExecutor().execute(
        request=outcome.request,
        observations=observations,
        request_fingerprint=RetrievalToolCallValidator.fingerprint(tool),
    )
    return [*observations, observation]


def test_rule_path_uses_direct_structured_key_validation() -> None:
    result = StructuredKeyValidator().validate(request())
    assert result.valid, result.errors


def test_rule_path_rejects_unknown_structured_order_without_query_planning() -> None:
    result = StructuredKeyValidator().validate(request(operation_id="ORD-999"))
    assert not result.valid
    assert "UNKNOWN_ORDER_ID:ORD-999" in result.errors


def test_tool_call_validator_does_not_force_a_complete_tool_program() -> None:
    call = RetrievalToolRequest(
        request_id="orders-1",
        tool_name="get_order_facts",
        exact_ids=["ORD-001"],
        purpose="Load one authoritative order.",
    )
    result = RetrievalToolCallValidator().validate(request=call, observations=[])
    assert result.valid, result.errors


def test_tool_call_validator_blocks_raw_storage_syntax() -> None:
    call = RetrievalToolRequest(
        request_id="bad",
        tool_name="find_orders",
        item_text="SELECT * FROM orders",
        purpose="bad",
    )
    result = RetrievalToolCallValidator().validate(request=call, observations=[])
    assert not result.valid
    assert "RAW_STORAGE_SYNTAX_FORBIDDEN" in result.errors


def test_stepwise_agent_executes_real_tools_and_becomes_sufficient() -> None:
    normalized = request(operation_id="bearing-order", raw_reference="산업용 베어링 주문")
    observations = []
    observations = execute_step(
        RetrievalToolRequest(
            request_id="find-orders",
            tool_name="find_orders",
            item_text="산업용 베어링",
            statuses=["pending"],
            raw_references=[
                SemanticEntityReference(
                    reference_id="order-ref",
                    raw_text="산업용 베어링 주문",
                    expected_entity_types=["ORDER"],
                )
            ],
            purpose="Find the requested order.",
        ),
        normalized,
        observations,
    )
    assert observations[-1].tool_name == "find_orders"
    assert observations[-1].data["candidate_order_ids"] == ["ORD-001"]

    observations = execute_step(
        RetrievalToolRequest(
            request_id="order-facts",
            tool_name="get_order_facts",
            derive_from_previous_results=True,
            purpose="Load order facts from the prior candidate.",
        ),
        normalized,
        observations,
    )
    observations = execute_step(
        RetrievalToolRequest(
            request_id="inventory",
            tool_name="get_inventory_candidates",
            derive_from_previous_results=True,
            purpose="Load all stock candidates.",
        ),
        normalized,
        observations,
    )
    observations = execute_step(
        RetrievalToolRequest(
            request_id="robots",
            tool_name="get_robot_candidates",
            purpose="Load complete robot runtime.",
        ),
        normalized,
        observations,
    )
    observations = execute_step(
        RetrievalToolRequest(
            request_id="subgraph",
            tool_name="get_connecting_subgraph",
            derive_from_previous_results=True,
            purpose="Connect robots, racks, and destination.",
        ),
        normalized,
        observations,
    )
    observations = execute_step(
        RetrievalToolRequest(
            request_id="runtime",
            tool_name="get_runtime_constraints",
            derive_from_previous_results=True,
            purpose="Load runtime constraints on the relevant paths.",
        ),
        normalized,
        observations,
    )

    result = StepwiseRetrievalSufficiencyValidator().validate(
        request=normalized,
        observations=observations,
    )
    assert result.ready, result.errors
    assert [value.tool_name for value in observations] == [
        "find_orders",
        "get_order_facts",
        "get_inventory_candidates",
        "get_robot_candidates",
        "get_connecting_subgraph",
        "get_runtime_constraints",
    ]

    canonical, inventory, robots, map_context, nodes, node_types, arcs = (
        ObservationContextMaterializer().materialize(
            normalized_request=normalized,
            observations=observations,
        )
    )
    assert canonical.operations[0].operation_id == "ORD-001"
    assert inventory.task_needs[0].order_id == "ORD-001"
    assert len(inventory.candidate_stocks) == 2
    assert set(robots.candidate_robot_ids) == {"R002", "R003"}
    assert map_context.node_count == 220
    assert len(nodes) == 220
    assert len(node_types) == 220
    assert len(arcs) == 356


def test_ambiguous_natural_order_is_not_selected_silently() -> None:
    set_data_dir(AMBIGUOUS_FIXTURE)
    try:
        normalized = request(operation_id="bearing-order", raw_reference="산업용 베어링 주문")
        call = RetrievalToolRequest(
            request_id="find-orders",
            tool_name="find_orders",
            item_text="산업용 베어링",
            statuses=["pending"],
            raw_references=[
                SemanticEntityReference(
                    reference_id="order-ref",
                    raw_text="산업용 베어링 주문",
                    expected_entity_types=["ORDER"],
                )
            ],
            purpose="Find order candidates.",
        )
        outcome = StepwiseQueryKeyResolver().resolve(
            tool_request=call,
            normalized_request=normalized,
            observations=[],
        )
        assert outcome.request is None
        assert outcome.ambiguous_references == ["산업용 베어링 주문"]
        assert outcome.entity_resolutions[0].status == "AMBIGUOUS"
        assert {
            value.entity_id for value in outcome.entity_resolutions[0].candidates
        } == {"ORD-001", "ORD-002"}
    finally:
        set_data_dir(None)


def test_llm_invented_id_is_retryable_but_user_id_requires_clarification() -> None:
    normalized = request(operation_id="bearing-order", raw_reference="산업용 베어링 주문")
    invented = RetrievalToolRequest(
        request_id="invented",
        tool_name="get_order_facts",
        exact_ids=["ORD-999"],
        purpose="Invented by the LLM.",
    )
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=invented,
        normalized_request=normalized,
        observations=[],
    )
    assert outcome.not_found_references == ["ORD-999"]
    assert not outcome.user_owned_not_found_references

    explicit = request(operation_id="ORD-999", raw_reference="ORD-999")
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=invented,
        normalized_request=explicit,
        observations=[],
    )
    assert outcome.user_owned_not_found_references == ["ORD-999"]


def test_semantic_robot_and_map_references_are_canonicalized_before_formulation() -> None:
    """Natural aliases become authoritative IDs only after deterministic resolution."""

    normalized = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                source_event_type="natural_language",
                raw_reference="ORD-001",
            )
        ],
        constraints=NormalizedRequestConstraints(
            excluded_robot_references=["AMR-03"],
            soft_avoid_edge_references=["D 출고 통로"],
        ),
        raw_user_command="ORD-001을 처리하되 AMR-03은 빼고 D 출고 통로는 가능하면 피해.",
        normalization_summary="semantic constraint test",
    )
    observations = []
    resolutions = []

    for call in [
        RetrievalToolRequest(
            request_id="order-facts",
            tool_name="get_order_facts",
            exact_ids=["ORD-001"],
            purpose="Load authoritative order facts.",
        ),
        RetrievalToolRequest(
            request_id="inventory",
            tool_name="get_inventory_candidates",
            derive_from_previous_results=True,
            purpose="Load all stock candidates.",
        ),
        RetrievalToolRequest(
            request_id="robots",
            tool_name="get_robot_candidates",
            raw_references=[
                SemanticEntityReference(
                    reference_id="robot-ref",
                    raw_text="AMR-03",
                    expected_entity_types=["ROBOT"],
                )
            ],
            purpose="Load robot runtime and resolve the excluded robot alias.",
        ),
        RetrievalToolRequest(
            request_id="map-ref",
            tool_name="resolve_map_entities",
            raw_references=[
                SemanticEntityReference(
                    reference_id="map-ref",
                    raw_text="D 출고 통로",
                    expected_entity_types=["OUTBOUND", "EDGE"],
                )
            ],
            allow_multiple_matches=True,
            purpose="Resolve the descriptive outbound corridor reference.",
        ),
        RetrievalToolRequest(
            request_id="subgraph",
            tool_name="get_connecting_subgraph",
            derive_from_previous_results=True,
            purpose="Build directed path evidence.",
        ),
        RetrievalToolRequest(
            request_id="runtime",
            tool_name="get_runtime_constraints",
            derive_from_previous_results=True,
            purpose="Load runtime constraints.",
        ),
    ]:
        validation = RetrievalToolCallValidator().validate(request=call, observations=observations)
        assert validation.valid, validation.errors
        outcome = StepwiseQueryKeyResolver().resolve(
            tool_request=call,
            normalized_request=normalized,
            observations=observations,
        )
        assert not outcome.ambiguous_references
        assert not outcome.not_found_references
        assert outcome.request is not None
        resolutions.extend(outcome.entity_resolutions)
        observations.append(
            WarehouseReadToolExecutor().execute(
                request=outcome.request,
                observations=observations,
                request_fingerprint=RetrievalToolCallValidator.fingerprint(call),
            )
        )

    sufficiency = StepwiseRetrievalSufficiencyValidator().validate(
        request=normalized,
        observations=observations,
    )
    assert sufficiency.ready, sufficiency.errors

    canonical, *_rest = ObservationContextMaterializer().materialize(
        normalized_request=normalized,
        observations=observations,
        entity_resolutions=resolutions,
    )
    assert canonical.constraints.excluded_robot_ids == ["R003"]
    assert canonical.constraints.excluded_robot_references == []
    assert canonical.constraints.soft_avoid_edge_references == []
    assert set(canonical.constraints.soft_avoid_edge_ids) >= {"OUT_CONNECT"}
    assert all(
        edge_id in {"OUT2", "OUT3", "OUT_CONNECT"}
        for edge_id in canonical.constraints.soft_avoid_edge_ids
    )


def test_exact_id_hints_short_circuit_map_semantic_search() -> None:
    """The exact live scenario must not re-search O_D/K1_7/K2_7 as prose."""

    normalized = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            )
        ],
        constraints=NormalizedRequestConstraints(soft_avoid_edge_ids=["H3_7"]),
        raw_user_command="ORD-001을 처리하고 H3_7을 피해.",
        normalization_summary="exact hint regression",
    )
    call = RetrievalToolRequest(
        request_id="map-exact-regression",
        tool_name="resolve_map_entities",
        exact_ids=["H3_7", "K1_7_ACCESS_A", "K1_7_ACCESS_B", "K2_7_ACCESS_A", "K2_7_ACCESS_B", "O_D"],
        raw_references=[
            SemanticEntityReference(
                reference_id="delivery",
                raw_text="ORD-001 delivery node O_D (from order facts)",
                expected_entity_types=["OUTBOUND"],
                exact_id_hint="O_D",
                required=False,
            ),
            SemanticEntityReference(
                reference_id="rack-1",
                raw_text="Rack access node K1_7_ACCESS_A (inventory candidate)",
                expected_entity_types=["RACK", "NODE"],
                exact_id_hint="K1_7_ACCESS_A",
                required=False,
            ),
            SemanticEntityReference(
                reference_id="rack-2",
                raw_text="Rack access node K2_7_ACCESS_A (inventory candidate)",
                expected_entity_types=["RACK", "NODE"],
                exact_id_hint="K2_7_ACCESS_A",
                required=False,
            ),
        ],
        expected_entity_types=["EDGE", "RACK", "NODE", "OUTBOUND"],
        purpose="Verify exact map anchors.",
    )
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=call,
        normalized_request=normalized,
        observations=[],
    )
    assert not outcome.ambiguous_references
    assert not outcome.not_found_references
    assert outcome.request is not None
    assert set(outcome.request.edge_ids) >= {"H3_7"}
    assert set(outcome.request.node_ids) >= {"K1_7_ACCESS_A", "K1_7_ACCESS_B", "K2_7_ACCESS_A", "K2_7_ACCESS_B", "O_D"}
    by_reference = {value.reference_id: value for value in outcome.entity_resolutions}
    assert by_reference["delivery"].resolved_entity_ids == ["O_D"]
    assert by_reference["rack-1"].resolved_entity_ids == ["K1_7_ACCESS_A"]
    assert by_reference["rack-2"].resolved_entity_ids == ["K2_7_ACCESS_A"]


def test_optional_ambiguous_map_reference_is_non_blocking() -> None:
    normalized = request()
    call = RetrievalToolRequest(
        request_id="optional-map",
        tool_name="resolve_map_entities",
        exact_ids=["O_D"],
        raw_references=[
            SemanticEntityReference(
                reference_id="optional-description",
                raw_text="outbound area",
                expected_entity_types=["OUTBOUND"],
                required=False,
            )
        ],
        purpose="Use an exact destination and optional prose only for explanation.",
    )
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=call,
        normalized_request=normalized,
        observations=[],
    )
    assert not outcome.ambiguous_references
    assert not outcome.not_found_references
    assert outcome.request is not None
    assert "O_D" in outcome.request.node_ids


def test_robot_resolution_is_reference_local_and_type_safe() -> None:
    """An order ID must never inherit a robot match from a neighboring reference."""

    normalized = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            )
        ],
        constraints=NormalizedRequestConstraints(excluded_robot_ids=["R003"]),
        raw_user_command="ORD-001을 처리하되 R003은 제외해.",
        normalization_summary="robot type isolation",
    )
    call = RetrievalToolRequest(
        request_id="robot-isolation",
        tool_name="get_robot_candidates",
        raw_references=[
            SemanticEntityReference(
                reference_id="operator-exclusion",
                raw_text="Exclude robot R003 per operator constraint",
                expected_entity_types=["ROBOT"],
            ),
            SemanticEntityReference(
                reference_id="bad-order-reference",
                raw_text="ORD-001",
                expected_entity_types=["ROBOT"],
                required=False,
            ),
        ],
        purpose="Load robot runtime.",
    )
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=call,
        normalized_request=normalized,
        observations=[],
    )
    assert not outcome.ambiguous_references
    assert not outcome.not_found_references
    assert outcome.request is not None
    assert outcome.request.robot_ids == ["R003"]
    by_reference = {value.reference_id: value for value in outcome.entity_resolutions}
    assert by_reference["operator-exclusion"].resolved_entity_ids == ["R003"]
    assert by_reference["bad-order-reference"].resolved_entity_ids == []
    assert all(
        candidate.entity_type == "ROBOT"
        for value in outcome.entity_resolutions
        for candidate in value.candidates
    )


def test_robot_status_phrases_are_filters_not_entities() -> None:
    raw = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="bearing-order",
                operation_type="OUTBOUND_ORDER",
                raw_reference="산업용 베어링 주문",
            )
        ],
        constraints=NormalizedRequestConstraints(
            excluded_robot_references=["충전 중인 로봇", "작업 중인 로봇"],
        ),
        raw_user_command="산업용 베어링 주문을 처리해. 충전 중이거나 작업 중인 로봇은 제외해.",
        normalization_summary="status phrase regression",
    )
    normalized = _canonicalize_normalized_request(raw)
    assert normalized.constraints.excluded_robot_references == []
    assert set(normalized.constraints.excluded_robot_statuses) == {"charging", "working"}

    call = RetrievalToolRequest(
        request_id="status-filter",
        tool_name="get_robot_candidates",
        raw_references=[
            SemanticEntityReference(
                reference_id="charging",
                raw_text="충전 중인 로봇",
                expected_entity_types=["ROBOT"],
                required=False,
            ),
            SemanticEntityReference(
                reference_id="working",
                raw_text="작업 중인 로봇",
                expected_entity_types=["ROBOT"],
                required=False,
            ),
        ],
        purpose="Load robots while applying status filters.",
    )
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=call,
        normalized_request=normalized,
        observations=[],
    )
    assert not outcome.not_found_references
    assert outcome.request is not None
    assert set(outcome.request.exclude_statuses) == {"charging", "working"}
    observation = WarehouseReadToolExecutor().execute(
        request=outcome.request,
        observations=[],
        request_fingerprint=RetrievalToolCallValidator.fingerprint(call),
    )
    assert observation.data["exclude_statuses"] == ["charging", "working"]
    assert observation.data["candidate_robot_ids"] == ["R002", "R003"]


def test_find_orders_facts_are_sufficient_without_duplicate_order_call() -> None:
    """The semantic path may continue directly from one complete find_orders observation."""

    normalized = request(operation_id="bearing-order", raw_reference="산업용 베어링 주문")
    observations = []
    for call in [
        RetrievalToolRequest(
            request_id="find",
            tool_name="find_orders",
            item_text="산업용 베어링",
            statuses=["pending"],
            purpose="Find and load complete authoritative order facts.",
        ),
        RetrievalToolRequest(
            request_id="inventory",
            tool_name="get_inventory_candidates",
            derive_from_previous_results=True,
            purpose="Load all stock candidates.",
        ),
        RetrievalToolRequest(
            request_id="robots",
            tool_name="get_robot_candidates",
            exclude_statuses=["charging", "working"],
            purpose="Load robot runtime.",
        ),
        RetrievalToolRequest(
            request_id="map-set",
            tool_name="resolve_map_entities",
            raw_references=[
                SemanticEntityReference(
                    reference_id="corridor",
                    raw_text="D 출고 통로",
                    expected_entity_types=["OUTBOUND"],
                )
            ],
            purpose="Resolve the requested outbound corridor entity set.",
        ),
        RetrievalToolRequest(
            request_id="subgraph",
            tool_name="get_connecting_subgraph",
            derive_from_previous_results=True,
            purpose="Build directed path evidence.",
        ),
        RetrievalToolRequest(
            request_id="runtime",
            tool_name="get_runtime_constraints",
            derive_from_previous_results=True,
            purpose="Load runtime constraints.",
        ),
    ]:
        observations = execute_step(call, normalized, observations)

    assert "get_order_facts" not in {value.tool_name for value in observations}
    result = StepwiseRetrievalSufficiencyValidator().validate(
        request=normalized,
        observations=observations,
    )
    assert result.ready, result.errors
    canonical, inventory, *_ = ObservationContextMaterializer().materialize(
        normalized_request=normalized,
        observations=observations,
    )
    assert canonical.operations[0].operation_id == "ORD-001"
    assert inventory.task_needs[0].delivery_node == "O_D"


def test_corridor_reference_defaults_to_entity_set_cardinality() -> None:
    normalized = request()
    call = RetrievalToolRequest(
        request_id="corridor-set",
        tool_name="resolve_map_entities",
        raw_references=[
            SemanticEntityReference(
                reference_id="d-corridor",
                raw_text="D 출고 통로",
                expected_entity_types=["OUTBOUND"],
            )
        ],
        allow_multiple_matches=False,
        purpose="Resolve the corridor even when the model omitted the set flag.",
    )
    outcome = StepwiseQueryKeyResolver().resolve(
        tool_request=call,
        normalized_request=normalized,
        observations=[],
    )
    assert not outcome.ambiguous_references
    assert outcome.request is not None
    assert "O_D" in outcome.request.node_ids
    assert set(outcome.request.edge_ids) >= {"OUT2", "OUT3", "OUT_CONNECT"}

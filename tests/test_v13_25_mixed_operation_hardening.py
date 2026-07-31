"""v13.25 mixed-operation prompt, retrieval, formulation, and final-plan guards."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.config import PROJECT_ROOT, get_settings
from app.domain.schemas import (
    ContextSnapshot,
    CuOptTaskDraft,
    FormulationRecommendation,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    SimulationLogicalOperation,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
)
from app.graph.cuopt_formulation import _enforce_outbound_fulfillment_contract
from app.graph.input_formulation import _pre_route_guard_requires_agent
from app.graph.state import LaroOutputState
from app.prompts.cuopt_formulator import CUOPT_FORMULATOR_SYSTEM
from app.repositories.json_repository import JsonWarehouseRepository, get_repository, set_data_dir
from app.repositories.live_repository import LiveWarehouseRepository
from app.services.cuopt_formulation_service import (
    CuOptDraftEvidenceEnricher,
    CuOptDynamicInputValidator,
    RuleCuOptFormulator,
)
from app.services.request_gate_service import resolve_request_gate
from app.services.logical_operation_validation_service import (
    LogicalOperationCoverageValidator,
)
from app.services.parallel_retrieval_service import (
    ParallelRetrievalExecutor,
    ParallelRetrievalPlanCompiler,
)
from app.services.situation_graph_service import (
    WarehouseSituationGraphBuilder,
    WarehouseSituationGraphValidator,
)
from app.services.stepwise_retrieval_service import ObservationContextMaterializer

FIXTURE = PROJECT_ROOT / "scenarios" / "fixtures" / "V18_mixed_inbound_outbound"


def _request() -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                source_event_type="human_command",
                raw_reference="ORD-001",
            ),
            NormalizedOperation(
                operation_id="IN-001",
                operation_type="INBOUND_ITEM",
                source_event_type="human_command",
                raw_reference="IN-001",
            ),
        ],
        constraints=NormalizedRequestConstraints(),
        normalization_summary="Canonical mixed outbound and inbound request.",
    )


def _mixed_contexts(monkeypatch):
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    get_settings.cache_clear()
    set_data_dir(FIXTURE)
    request = _request()
    plan = ParallelRetrievalPlanCompiler().build_canonical_plan(
        normalized_request=request
    )
    outcome = ParallelRetrievalExecutor().execute(
        plan=plan,
        normalized_request=request,
        llm_planning_call_count=0,
    )
    assert outcome.execution.valid, outcome.execution.errors
    assert outcome.sufficiency.ready, outcome.sufficiency.errors
    (
        normalized,
        inventory,
        robots,
        map_context,
        graph_nodes,
        graph_node_types,
        graph_arcs,
    ) = ObservationContextMaterializer().materialize(
        normalized_request=request,
        observations=outcome.observations,
        entity_resolutions=[],
    )
    repository = get_repository()
    versions = repository.versions
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V13-25-MIXED",
        captured_at=datetime.now(timezone.utc).isoformat(),
        graph_version=versions["graph_version"],
        inventory_version=versions["inventory_version"],
        runtime_version=versions["runtime_version"],
        repository_type=type(repository).__name__,
        source_manifest=dict(repository.source_manifest),
    )
    graph = WarehouseSituationGraphBuilder(repository).build(
        normalized_request=normalized,
        snapshot=snapshot,
        inventory=inventory,
        robots=robots,
        map_context=map_context,
        graph_arcs=graph_arcs,
        retrieval_observations=outcome.observations,
    )
    return {
        "request": normalized,
        "inventory": inventory,
        "robots": robots,
        "map_context": map_context,
        "graph_nodes": graph_nodes,
        "graph_node_types": graph_node_types,
        "graph_arcs": graph_arcs,
        "snapshot": snapshot,
        "graph": graph,
        "outcome": outcome,
    }


def _cleanup() -> None:
    set_data_dir(None)
    get_settings.cache_clear()


def test_prompt_contract_explicitly_preserves_mixed_operations() -> None:
    assert "Keep tasks empty" not in CUOPT_FORMULATOR_SYSTEM
    assert "GOODS_TO_PERSON applies only to outbound operations" in CUOPT_FORMULATOR_SYSTEM
    assert 'g2p_order_ids=["ORD-001"]' in CUOPT_FORMULATOR_SYSTEM
    assert 'tasks contains exactly one INBOUND_ITEM task' in CUOPT_FORMULATOR_SYSTEM
    assert "Never silently omit" in CUOPT_FORMULATOR_SYSTEM


def test_agent_canonical_retrieval_materializes_outbound_and_inbound(monkeypatch) -> None:
    try:
        values = _mixed_contexts(monkeypatch)
        tool_names = [value.tool_name for value in values["outcome"].observations]
        assert "get_order_facts" in tool_names
        assert "get_inbound_facts" in tool_names
        assert "get_connecting_subgraph" in tool_names

        inventory = values["inventory"]
        assert inventory.query_scope.mode == "mixed_operations"
        assert [value.order_id for value in inventory.task_needs] == ["ORD-001"]
        assert [value.inbound_id for value in inventory.inbound_needs] == ["IN-001"]
        assert inventory.inbound_needs[0].handling_unit_id == "HU-IN-001"
        assert {
            (value.rack_id, value.rack_level)
            for value in inventory.candidate_putaway_slots
        } == {("K3_3", 1)}

        graph = values["graph"]
        validation = WarehouseSituationGraphValidator(get_repository()).validate(graph)
        assert validation.valid, validation.errors
        assert graph.completeness.ready_for_formulation
        inbound_paths = [
            value
            for value in graph.path_evidence
            if "inbound:IN-001" in value.path_id
        ]
        assert {value.purpose for value in inbound_paths} >= {
            "ROBOT_TO_PICKUP",
            "PICKUP_TO_DELIVERY",
        }
    finally:
        _cleanup()


def test_g2p_postprocessor_removes_only_outbound_tasks(monkeypatch) -> None:
    try:
        values = _mixed_contexts(monkeypatch)
        draft = RuleCuOptFormulator().formulate_from_contexts(
            normalized_request=values["request"],
            snapshot=values["snapshot"],
            inventory=values["inventory"],
            robots=values["robots"],
            map_context=values["map_context"],
            graph_arcs=values["graph_arcs"],
            time_limit_seconds=5,
        )
        inbound_task = draft.tasks[0]
        outbound_task = CuOptTaskDraft(
            task_id="BAD-OUTBOUND-TASK",
            operation_type="OUTBOUND_ORDER",
            order_id="ORD-001",
            item_id="ITEM_BEARING",
            stock_id="STOCK-K2_7-L2-ITEM_BEARING",
            rack_id="K2_7",
            rack_level=2,
            pickup_node="K2_7_ACCESS_A",
            delivery_node="O_A",
            demand=2,
            priority="high",
        )
        mutated = draft.model_copy(
            update={
                "formulation_source": "llm",
                "tasks": [outbound_task, inbound_task],
                "g2p_order_ids": [],
            }
        )
        enforced = _enforce_outbound_fulfillment_contract(
            draft=mutated,
            graph=values["graph"],
        )
        assert enforced.g2p_order_ids == ["ORD-001"]
        assert [value.operation_type for value in enforced.tasks] == ["INBOUND_ITEM"]
        assert enforced.tasks[0].order_id == "IN-001"
    finally:
        _cleanup()


def test_agent_validator_rejects_silent_inbound_omission(monkeypatch) -> None:
    try:
        values = _mixed_contexts(monkeypatch)
        correct = RuleCuOptFormulator().formulate_from_contexts(
            normalized_request=values["request"],
            snapshot=values["snapshot"],
            inventory=values["inventory"],
            robots=values["robots"],
            map_context=values["map_context"],
            graph_arcs=values["graph_arcs"],
            time_limit_seconds=5,
        )
        broken = correct.model_copy(
            update={"formulation_source": "llm", "tasks": []}
        )
        graph_validation = CuOptDynamicInputValidator().validate(
            draft=broken,
            normalized_request=values["request"],
            graph=values["graph"],
            expected_source="llm",
        )
        context_validation = CuOptDynamicInputValidator().validate_from_contexts(
            draft=broken,
            normalized_request=values["request"],
            snapshot=values["snapshot"],
            inventory=values["inventory"],
            robots=values["robots"],
            map_context=values["map_context"],
            graph_arcs=values["graph_arcs"],
            expected_source="llm",
        )
        assert not graph_validation.valid
        assert not context_validation.valid
        assert "OPERATION_COVERAGE_MISMATCH:IN-001" in graph_validation.errors
        assert "OPERATION_COVERAGE_MISMATCH:IN-001" in context_validation.errors
    finally:
        _cleanup()


def test_agent_validator_accepts_complete_mixed_draft_after_evidence_enrichment(monkeypatch) -> None:
    try:
        values = _mixed_contexts(monkeypatch)
        draft = RuleCuOptFormulator().formulate_from_contexts(
            normalized_request=values["request"],
            snapshot=values["snapshot"],
            inventory=values["inventory"],
            robots=values["robots"],
            map_context=values["map_context"],
            graph_arcs=values["graph_arcs"],
            time_limit_seconds=5,
        ).model_copy(update={"formulation_source": "llm"})
        enriched, enrichment = CuOptDraftEvidenceEnricher().enrich(
            draft=draft,
            graph=values["graph"],
        )
        assert enrichment.applied
        graph_validation = CuOptDynamicInputValidator().validate(
            draft=enriched,
            normalized_request=values["request"],
            graph=values["graph"],
            expected_source="llm",
        )
        context_validation = CuOptDynamicInputValidator().validate_from_contexts(
            draft=enriched,
            normalized_request=values["request"],
            snapshot=values["snapshot"],
            inventory=values["inventory"],
            robots=values["robots"],
            map_context=values["map_context"],
            graph_arcs=values["graph_arcs"],
            expected_source="llm",
        )
        assert graph_validation.valid, graph_validation.errors
        assert context_validation.valid, context_validation.errors
    finally:
        _cleanup()


def test_final_plan_guard_rejects_downstream_operation_loss(monkeypatch) -> None:
    try:
        values = _mixed_contexts(monkeypatch)
        draft = RuleCuOptFormulator().formulate_from_contexts(
            normalized_request=values["request"],
            snapshot=values["snapshot"],
            inventory=values["inventory"],
            robots=values["robots"],
            map_context=values["map_context"],
            graph_arcs=values["graph_arcs"],
            time_limit_seconds=5,
        )
        plan = SimulationPlan(
            plan_id="PLAN-V13-25-BROKEN",
            plan_version=1,
            warehouse_id="WH-001",
            simulation_id="SIM-V13-25-BROKEN",
            map_version=values["snapshot"].graph_version,
            source_snapshot_id=values["snapshot"].snapshot_id,
            makespan_ms=100,
            absolute_finish_at_ms=100,
            robots=[
                SimulationRobotPlan(
                    robot_id="R003",
                    initial_node="R2_5",
                    finish_at_ms=100,
                    steps=[
                        SimulationPlanStep(
                            step_id="R003-0001",
                            sequence=1,
                            step_type="SERVICE",
                            start_at_ms=0,
                            end_at_ms=100,
                            node_id="OUT_STATION_1_ACCESS_A",
                            task_id="G2P-ORD-001_PICK",
                            service_kind="PICKUP",
                        )
                    ],
                )
            ],
            logical_operations=[
                SimulationLogicalOperation(
                    operation_id="ORD-001",
                    operation_type="OUTBOUND_ORDER",
                    assigned_robot_id="R003",
                    task_ids=["G2P-ORD-001"],
                ),
                SimulationLogicalOperation(
                    operation_id="IN-001",
                    operation_type="INBOUND_ITEM",
                    assigned_robot_id=None,
                    task_ids=[],
                ),
            ],
        )
        validation = LogicalOperationCoverageValidator().validate(
            request=values["request"],
            draft=draft,
            plan=plan,
        )
        assert not validation.valid
        assert "IN-001" in validation.operations_without_tasks
        assert "IN-001" in validation.operations_without_robots
        assert "PLAN_OPERATION_HAS_NO_TASKS:IN-001" in validation.errors
        assert "PLAN_OPERATION_HAS_NO_ROBOT:IN-001" in validation.errors
    finally:
        _cleanup()


def test_exact_canonical_natural_language_does_not_force_agent() -> None:
    request = _request().model_copy(
        update={
            "raw_user_command": "ORD-001을 출고하고 IN-001도 입고해.",
            "operations": [
                _request().operations[0].model_copy(
                    update={"raw_reference": "ORD-001을 출고해"}
                ),
                _request().operations[1].model_copy(
                    update={"raw_reference": "IN-001도 입고해"}
                ),
            ],
        }
    )
    assert not _pre_route_guard_requires_agent(request)


def test_live_repository_uses_db_snapshots_without_reading_json(monkeypatch) -> None:
    baseline = JsonWarehouseRepository(FIXTURE)
    inventory = deepcopy(baseline.inventory)
    inventory["racks"][0]["source_marker"] = "POSTGRES_LIVE"
    graph_nodes = deepcopy(baseline.graph["nodes"])
    graph_nodes[0]["source_marker"] = "NEO4J_LIVE"
    graph_nodes[0]["x"] = 9876.5
    graph_edges = deepcopy(baseline.graph["edges"])
    graph_edges[0]["source_marker"] = "NEO4J_LIVE"

    class FakePostgres:
        def load_inventory_document(self, warehouse_id):
            return deepcopy(inventory)

        def load_orders(self, warehouse_id):
            return deepcopy(list(baseline.orders.values()))

        def load_inbound_receipts(self, warehouse_id):
            return deepcopy(list(baseline.inbound_receipts.values()))

        def load_facility_document(self, warehouse_id):
            return deepcopy(baseline.facility)

        def versions(self, warehouse_id):
            return {
                "inventory_version": "pg-inventory",
                "business_version": "pg-business",
                "facility_version": "pg-facility",
            }

    class FakeRedis:
        def all_robots(self, warehouse_id, simulation_id):
            return deepcopy(list(baseline.robots.values()))

        def edge_runtime(self, warehouse_id, simulation_id):
            return []

        def existing_reservations(self, warehouse_id, simulation_id):
            return []

        def runtime_version(self, warehouse_id, simulation_id):
            return "redis-runtime"

    class FakeNeo4j:
        def fetch_route_graph(self, warehouse_id):
            return SimpleNamespace(
                nodes=deepcopy(graph_nodes),
                edges=deepcopy(graph_edges),
                summary={
                    "node_count": len(graph_nodes),
                    "edge_count": len(graph_edges),
                },
                version="neo4j-graph",
            )

    class FakeManager:
        postgres = FakePostgres()
        redis = FakeRedis()
        neo4j = FakeNeo4j()

        def start(self):
            return None

    import app.repositories.live_repository as live_module

    monkeypatch.setattr(live_module, "get_infrastructure_manager", lambda: FakeManager())
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(f"Live repository attempted JSON read: {self}")
        ),
    )
    repository = LiveWarehouseRepository(
        warehouse_id="WH-001",
        simulation_id="SIM-V13-25-LIVE",
    )
    node_id = str(graph_nodes[0]["id"])
    edge_id = str(graph_edges[0]["id"])
    rack_id = str(inventory["racks"][0]["rack_id"])
    assert repository.node(node_id)["source_marker"] == "NEO4J_LIVE"
    assert repository.edge(edge_id)["source_marker"] == "NEO4J_LIVE"
    assert repository.rack(rack_id)["source_marker"] == "POSTGRES_LIVE"
    assert repository.node(node_id)["x"] == 9876.5
    assert repository.source_manifest["route_nodes"] == "neo4j_snapshot"
    assert repository.source_manifest["racks"] == "postgres_snapshot"


def test_output_schema_exposes_logical_operation_coverage_validation() -> None:
    """LangGraph output filtering must not discard the final coverage proof."""

    assert "logical_operation_coverage_validation" in LaroOutputState.__annotations__


def test_explicit_canonical_natural_command_suppresses_hallucinated_clarification() -> None:
    """A clear ORD/IN command cannot become UNREADABLE_COMMAND by model opinion."""

    request = _request().model_copy(
        update={
            "raw_user_command": "ORD-001을 출고하고 IN-001도 입고해. 전체 완료시간을 최소화해.",
            "user_clarification_questions": [
                "Choose one of the presented options (A-E)."
            ],
        }
    )
    decision = resolve_request_gate(
        simulation_id="SIM-V13-25-CANONICAL-NATURAL",
        request=request,
        recommendation=FormulationRecommendation(
            route="AGENT_FORMULATION",
            gate_action="ASK_CLARIFICATION",
            reason_code="UNREADABLE_COMMAND",
            reasons=["model-only uncertainty"],
            prompt=(
                "Please choose options A-E or decide whether ORD-001 or IN-001 "
                "should be processed."
            ),
        ),
        original_user_command=request.raw_user_command,
        has_structured_events=False,
        planning_mode="llm_router",
        requires_agent_guard=True,
        human_responses=[],
    )
    assert decision.action == "ROUTE_RULE"
    assert decision.final_route == "RULE_FORMULATION"
    assert decision.route_locked is True
    assert decision.input_rejection is None
    assert any("explicit canonical natural-language" in value for value in decision.reasons)


def test_non_actionable_id_list_does_not_bypass_clarification() -> None:
    """Canonical tokens without an execution verb remain ambiguous."""

    request = _request().model_copy(
        update={
            "raw_user_command": "ORD-001, IN-001",
        }
    )
    decision = resolve_request_gate(
        simulation_id="SIM-V13-25-ID-LIST",
        request=request,
        recommendation=FormulationRecommendation(
            route="RULE_FORMULATION",
            gate_action="ASK_CLARIFICATION",
            reason_code="UNREADABLE_COMMAND",
            reasons=["No action verb was supplied."],
            prompt="State the intended action.",
        ),
        original_user_command=request.raw_user_command,
        has_structured_events=False,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=[],
    )
    assert decision.action == "REJECT_INPUT"
    assert decision.input_rejection is not None
    assert decision.input_rejection.reason_code == "UNREADABLE_COMMAND"

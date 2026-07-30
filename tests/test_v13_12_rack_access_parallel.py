"""v13.12 rack-access topology, deterministic conditions, and parallel reads."""
from __future__ import annotations

from datetime import datetime, timezone

from app.domain.schemas import (
    ConditionalEdgePolicy,
    ContextSnapshot,
    FormulationRecommendation,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    OperationalIncidentImpact,
    ParallelRetrievalPlan,
    RetrievalToolRequest,
    RoutedNormalizedWarehouseRequest,
)
from app.graph.input_formulation import request_router_llm_node
from app.repositories.json_repository import get_repository
from app.repositories.neo4j_map_repository import Neo4jMapRepository
from app.services.context_service import WarehouseContextService
from app.services.cuopt_formulation_service import RuleCuOptFormulator
from app.services.parallel_retrieval_service import (
    ParallelRetrievalExecutor,
    ParallelRetrievalPlanCompiler,
    ParallelRetrievalPlanValidator,
)
from app.services.situation_graph_service import WarehouseSituationGraphBuilder


class _FakeGateway:
    def __init__(self, value: RoutedNormalizedWarehouseRequest) -> None:
        self.value = value
        self.calls = 0

    def invoke_structured(self, **_kwargs):
        self.calls += 1
        return self.value


def _state(command: str) -> dict:
    return {
        "simulation_id": "SIM-V13-12",
        "request_mode": "human_command",
        "optimization_backend": "cuopt_payload_only",
        "planning_mode": "llm_router",
        "requested_planning_mode": None,
        "planning_mode_source": "environment",
        "events": [],
        "user_command": command,
        "human_responses": [],
        "workflow_trace": [],
        "node_execution_log": [],
        "llm_node_summaries": [],
        "errors": [],
        "completed_context_nodes": [],
        "workflow_status": "running",
        "failure_requested": False,
    }


def test_racks_are_master_data_and_access_nodes_are_dead_end_spurs() -> None:
    repository = get_repository()
    assert len(repository.racks) == 48
    assert len(repository.nodes) == 220
    assert len(repository.edges) == 356
    assert all(rack_id not in repository.nodes for rack_id in repository.racks)

    for rack_id, access_ids in repository.all_rack_access_mappings().items():
        assert len(access_ids) == 2
        assert access_ids == [f"{rack_id}_ACCESS_A", f"{rack_id}_ACCESS_B"]
        for access_id in access_ids:
            node = repository.node(access_id)
            assert node is not None
            assert node["type"] == "rack_access"
            assert node["rack_id"] == rack_id
            assert node["service_only"] is True
            assert node["transit_allowed"] is False
            incident = [
                edge for edge in repository.edges.values()
                if edge["source"] == access_id or edge["target"] == access_id
            ]
            peers = {
                edge["target"] if edge["source"] == access_id else edge["source"]
                for edge in incident
            }
            assert len(peers) == 1
            assert not any(peer.endswith("_ACCESS_A") or peer.endswith("_ACCESS_B") for peer in peers)

    assert not any(
        edge["source"] == "K1_7_ACCESS_A" and edge["target"] == "K1_7_ACCESS_B"
        or edge["source"] == "K1_7_ACCESS_B" and edge["target"] == "K1_7_ACCESS_A"
        for edge in repository.edges.values()
    )


def test_neo4j_route_projection_accepts_access_nodes_and_rejects_rack_nodes() -> None:
    repository = get_repository()
    nodes = [dict(value) for value in repository.nodes.values()]
    edges = [dict(value) for value in repository.edges.values()]
    Neo4jMapRepository.validate_snapshot(nodes, edges)

    invalid_nodes = [*nodes, {"id": "K1_7", "type": "rack_storage"}]
    try:
        Neo4jMapRepository.validate_snapshot(invalid_nodes, edges)
    except Exception as exc:
        assert "must not" in str(exc) or "not traversable" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Rack master data must be rejected from the route projection.")


def test_parallel_retrieval_executes_independent_data_sources_in_one_wave() -> None:
    request = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            )
        ],
        constraints=NormalizedRequestConstraints(soft_avoid_edge_ids=["H3_7"]),
        raw_user_command="ORD-001을 처리하고 H3_7을 soft avoid로 적용해.",
        normalization_summary="parallel retrieval contract",
    )
    proposed = ParallelRetrievalPlan(
        requests=[
            RetrievalToolRequest(
                request_id="LLM_ORDER",
                tool_name="get_order_facts",
                exact_ids=["ORD-001"],
                purpose="Read the canonical order.",
            )
        ],
        planning_summary="One model-authored read plan.",
    )
    plan = ParallelRetrievalPlanCompiler().compile(
        normalized_request=request,
        proposed=proposed,
    )
    validation = ParallelRetrievalPlanValidator().validate(plan)
    assert validation.valid, validation.errors

    outcome = ParallelRetrievalExecutor().execute(
        plan=plan,
        normalized_request=request,
    )
    assert outcome.execution.valid, outcome.execution.errors
    assert outcome.sufficiency.ready
    assert outcome.execution.llm_planning_call_count == 1
    assert outcome.execution.peak_parallel_width >= 4
    first_wave = outcome.execution.wave_records[0]
    assert set(first_wave.data_sources) >= {"postgres", "redis", "neo4j"}
    assert {"ORDER_FACTS", "ROBOT_RUNTIME", "EXPLICIT_MAP_ENTITIES", "EXPLICIT_EDGE_RUNTIME"}.issubset(
        set(first_wave.request_ids)
    )
    assert [value.wave_index for value in outcome.execution.wave_records] == [1, 2, 3, 4]


def test_single_typed_conditional_policy_is_resolved_by_rule_from_runtime() -> None:
    repository = get_repository()
    contexts = WarehouseContextService(repository)
    request = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            )
        ],
        constraints=NormalizedRequestConstraints(
            soft_avoid_edge_ids=["H3_7"],
            conditional_edge_policies=[
                ConditionalEdgePolicy(
                    edge_id="H3_7",
                    threshold_ms=8000,
                    when_true="HARD_AVOID",
                    when_false="SOFT_AVOID",
                    source_text="H3_7 expected wait > 8 seconds",
                )
            ],
        ),
        raw_user_command="ORD-001을 처리해. H3_7 대기가 8초를 넘으면 hard, 아니면 soft.",
        normalization_summary="single typed condition",
    )
    inventory = contexts.build_inventory_context(order_ids=["ORD-001"])
    robots = contexts.build_robot_context(required_capacity=1)
    map_bundle = contexts.build_map_context(inventory=inventory)
    versions = repository.versions
    graph = WarehouseSituationGraphBuilder(repository).build(
        normalized_request=request,
        snapshot=ContextSnapshot(
            snapshot_id="SNAP-COND",
            captured_at=datetime.now(timezone.utc).isoformat(),
            graph_version=versions["graph_version"],
            inventory_version=versions["inventory_version"],
            runtime_version=versions["runtime_version"],
        ),
        inventory=inventory,
        robots=robots,
        map_context=map_bundle.context,
        graph_arcs=map_bundle.graph_arcs,
    )
    draft = RuleCuOptFormulator().formulate(
        normalized_request=request,
        graph=graph,
        time_limit_seconds=5,
    )
    # H3_7 is congested but not occupied/reserved in the fixture, so the typed
    # predicate is false and the deterministic branch remains SOFT_AVOID.
    assert "H3_7" in draft.map_constraints.soft_penalty_edge_ids
    assert "H3_7" not in draft.map_constraints.blocked_edge_ids


def test_inventory_data_conflict_cannot_create_a_physical_incident(monkeypatch) -> None:
    import app.graph.input_formulation as module

    command = "ORD-001 처리 전 K1_7-L1 시스템 재고와 센서 수량이 불일치해."
    normalized = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            ),
            NormalizedOperation(
                operation_id="INC-CONFLICT",
                operation_type="INCIDENT",
                raw_reference="inventory conflict",
            ),
        ],
        constraints=NormalizedRequestConstraints(),
        incidents=[
            OperationalIncidentImpact(
                incident_id="INC-CONFLICT",
                description="Inventory data mismatch",
                scope="MAP_RESOURCE",
                affected_resource_ids=["K1_7"],
                observed_effect="UNKNOWN",
                handling_mode="REQUIRE_HUMAN_DECISION",
                immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
                reason_codes=["AUTHORITATIVE_DATA_CONFLICT"],
            )
        ],
        raw_user_command=command,
        normalization_summary="model incorrectly classified a data conflict as a physical incident",
    )
    fake = _FakeGateway(
        RoutedNormalizedWarehouseRequest(
            normalized_request=normalized,
            recommendation=FormulationRecommendation(
                route="HUMAN_REVIEW",
                gate_action="REQUIRE_HUMAN_APPROVAL",
                reason_code="AUTHORITATIVE_DATA_CONFLICT",
                reasons=["Inventory sources disagree."],
            ),
        )
    )
    monkeypatch.setattr(module, "get_default_llm_gateway", lambda: fake)
    update = request_router_llm_node(_state(command))

    assert fake.calls == 1
    assert update["normalized_request"].incidents == []
    assert all(value.operation_type != "INCIDENT" for value in update["normalized_request"].operations)
    plan = update.get("incident_response_plan")
    assert plan is None or plan.immediate_actions == []
    assert update["request_gate_decision"].action == "REQUIRE_HUMAN_APPROVAL"
    assert update["request_gate_decision"].human_interaction is not None
    assert update["request_gate_decision"].human_interaction.reason_code == "AUTHORITATIVE_DATA_CONFLICT"


def test_hybrid_neo4j_repository_fetches_route_projection_once(monkeypatch) -> None:
    """The hybrid adapter must not fetch Neo4j once in the base class and again in the mixin."""

    from app.repositories.json_repository import JsonWarehouseRepository, Neo4jWarehouseRepository
    from app.repositories.neo4j_map_repository import Neo4jRouteGraphSnapshot

    source = JsonWarehouseRepository()
    calls = {"fetch": 0, "close": 0}

    def fake_fetch(self):
        calls["fetch"] += 1
        return Neo4jRouteGraphSnapshot(
            nodes=[dict(value) for value in source.nodes.values()],
            edges=[dict(value) for value in source.edges.values()],
            version="TEST-NEO4J-GRAPH",
        )

    def fake_close(self):
        calls["close"] += 1

    monkeypatch.setattr(Neo4jMapRepository, "fetch_route_graph", fake_fetch)
    monkeypatch.setattr(Neo4jMapRepository, "close", fake_close)
    repository = Neo4jWarehouseRepository()

    assert calls == {"fetch": 1, "close": 1}
    assert repository.versions["graph_version"] == "TEST-NEO4J-GRAPH"
    assert "K1_7" not in repository.nodes
    assert repository.rack_access_nodes("K1_7") == [
        "K1_7_ACCESS_A",
        "K1_7_ACCESS_B",
    ]


class _FakeNeo4jResult(list):
    def consume(self):
        return self


class _FakeNeo4jSession:
    def __init__(self, nodes: list[dict], edges: list[dict]) -> None:
        self.nodes = nodes
        self.edges = edges
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, query: str, **params):
        self.calls.append((query, params))
        if "RETURN properties(n) AS value" in query:
            return _FakeNeo4jResult([{"value": value} for value in self.nodes])
        if "RETURN r{.*, source: a.id, target: b.id} AS value" in query:
            return _FakeNeo4jResult([{"value": value} for value in self.edges])
        return _FakeNeo4jResult()


class _FakeNeo4jDriver:
    def __init__(self, nodes: list[dict], edges: list[dict]) -> None:
        self.session_value = _FakeNeo4jSession(nodes, edges)

    def session(self, **_kwargs):
        return self.session_value

    def close(self):
        return None


def test_neo4j_adapter_can_fetch_and_load_the_route_projection_without_racks() -> None:
    repository = get_repository()
    nodes = [dict(value) for value in repository.nodes.values()]
    edges = [dict(value) for value in repository.edges.values()]
    driver = _FakeNeo4jDriver(nodes, edges)
    adapter = Neo4jMapRepository(driver=driver, password="unused")

    fetched = adapter.fetch_route_graph()
    assert len(fetched.nodes) == 220
    assert len(fetched.edges) == 356
    assert fetched.summary["rack_storage_nodes"] == 0
    assert fetched.summary["rack_access_nodes"] == 96

    loaded = adapter.load_route_graph(nodes=nodes, edges=edges, replace=True)
    assert len(loaded.nodes) == 220
    assert len(loaded.edges) == 356
    rendered_queries = "\n".join(query for query, _ in driver.session_value.calls)
    assert "RouteNode" in rendered_queries
    assert "RackAccess" in rendered_queries
    assert "TRAVERSES" in rendered_queries

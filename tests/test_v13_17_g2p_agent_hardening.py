"""v13.17 G2P Agent, path, infrastructure, and multi-HU hardening contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings
from app.domain.schemas import (
    ContextSnapshot,
    CuOptDynamicInputDraft,
    CuOptFleetDraft,
    CuOptMapConstraintDraft,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    OptimizationVehicle,
    OptimizerResult,
    OptimizerRoute,
)
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.context_service import WarehouseContextService
from app.services.cuopt_formulation_service import (
    CuOptDraftEvidenceEnricher,
    CuOptDynamicInputValidator,
    DynamicInputOptimizationRequestAdapter,
)
from app.services.goods_to_person_compiler_service import IntegratedGoodsToPersonCompiler
from app.services.optimization_service import (
    CuOptNativeRequestBuilder,
    CuOptPayloadBuilder,
    OptimizerAssignmentValidator,
)
from app.services.situation_graph_service import (
    WarehouseSituationGraphBuilder,
    WarehouseSituationGraphValidator,
)

FIXTURE = PROJECT_ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_multi_hu"
ORDER_IDS = [f"ORD-{index:03d}" for index in range(1, 6)]


def _request() -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id=order_id,
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
                raw_reference=order_id,
            )
            for order_id in ORDER_IDS
        ],
        constraints=NormalizedRequestConstraints(),
        normalization_summary="v13.17 G2P Agent regression wave.",
    )


def _g2p_graph():
    repository = JsonWarehouseRepository(FIXTURE)
    context = WarehouseContextService(repository)
    request = _request()
    inventory = context.build_inventory_context(order_ids=ORDER_IDS)
    robots = context.build_robot_context(required_capacity=1)
    map_bundle = context.build_map_context(inventory=inventory)
    versions = repository.versions
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V13-17-G2P",
        captured_at=datetime.now(timezone.utc).isoformat(),
        graph_version=versions["graph_version"],
        inventory_version=versions["inventory_version"],
        runtime_version=versions["runtime_version"],
    )
    graph = WarehouseSituationGraphBuilder(repository).build(
        normalized_request=request,
        snapshot=snapshot,
        inventory=inventory,
        robots=robots,
        map_context=map_bundle.context,
        graph_arcs=map_bundle.graph_arcs,
        retrieval_observations=[],
    )
    return repository, request, inventory, robots, map_bundle, graph


def test_blank_optional_database_paths_use_local_db_dir(tmp_path: Path) -> None:
    settings = Settings(
        LOCAL_DB_DIR=str(tmp_path / "local-db"),
        LOCAL_POSTGRES_PATH="",
        LOCAL_REDIS_PATH="",
        LOCAL_NEO4J_PATH="",
        HITL_STORE_DIR="",
        _env_file=None,
    )
    assert settings.local_postgres_path is None
    assert settings.local_redis_path is None
    assert settings.local_neo4j_path is None
    assert settings.hitl_store_dir is None
    assert settings.local_postgres_db_path == (tmp_path / "local-db" / "postgres.sqlite3")
    assert settings.local_redis_db_path == (tmp_path / "local-db" / "redis.sqlite3")
    assert settings.local_neo4j_db_path == (tmp_path / "local-db" / "neo4j.sqlite3")


def test_g2p_situation_graph_uses_station_physical_paths_not_o_port_paths(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    from app.core.config import get_settings
    get_settings.cache_clear()
    _, _, _, _, _, graph = _g2p_graph()
    validation = WarehouseSituationGraphValidator(JsonWarehouseRepository(FIXTURE)).validate(graph)
    assert validation.valid, validation.errors
    assert graph.fulfillment_mode == "goods_to_person"
    assert graph.g2p_order_ids == ORDER_IDS
    purposes = {value.purpose for value in graph.path_evidence}
    assert {"ROBOT_TO_PICKUP", "PICKUP_TO_STATION", "STATION_TO_POST_MOVE"} <= purposes
    assert "PICKUP_TO_DELIVERY" not in purposes
    assert all(
        not value.target_node_id.startswith("O_")
        for value in graph.path_evidence
    )
    served = {
        value.target_node_id.removeprefix("destination:")
        for value in graph.relations
        if value.relation_type == "SERVES_DESTINATION"
    }
    expected_destinations = {
        str(JsonWarehouseRepository(FIXTURE).get_order(order_id)["delivery_node"])
        for order_id in ORDER_IDS
    }
    assert served == expected_destinations
    assert all(
        set(value["served_chute_ids"]) == {f"O_{letter}" for letter in "ABCDEFG"}
        for value in JsonWarehouseRepository(FIXTURE).outbound_station_candidates(
            [f"O_{letter}" for letter in "ABCDEFG"]
        )
    )


def test_g2p_agent_draft_validates_then_compiles_physical_handling_units(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    monkeypatch.setenv("G2P_DISTINCT_ROBOT_PER_HANDLING_UNIT", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    repository, request, _, _, map_bundle, graph = _g2p_graph()
    robot_ids = sorted(
        str(node.attributes["robot_id"])
        for node in graph.nodes
        if node.node_type == "robot" and node.attributes.get("baseline_eligible")
    )
    soft_edges = sorted(
        {
            str(node.attributes["edge_id"])
            for node in graph.nodes
            if node.node_type == "runtime_constraint"
            and node.attributes.get("constraint_type") in {"CONGESTED", "REQUESTED_SOFT_AVOID"}
        }
    )
    draft = CuOptDynamicInputDraft(
        formulation_mode="GOODS_TO_PERSON",
        g2p_order_ids=ORDER_IDS,
        snapshot_id=graph.snapshot_id,
        graph_version=graph.graph_version,
        formulation_source="llm",
        objective_profile="MIN_COMPLETION_TIME",
        tasks=[],
        deferred_order_ids=[],
        fleet=CuOptFleetDraft(
            included_robot_ids=robot_ids,
            excluded_robot_ids=[],
            evidence_ids=[],
        ),
        map_constraints=CuOptMapConstraintDraft(
            soft_penalty_edge_ids=soft_edges,
            evidence_ids=[],
        ),
        time_limit_seconds=5,
        formulation_summary="Preserve the order wave; deterministic G2P compiler creates HU tasks.",
    )
    enriched, enrichment = CuOptDraftEvidenceEnricher().enrich(draft=draft, graph=graph)
    assert enrichment.applied
    validation = CuOptDynamicInputValidator().validate(
        draft=enriched,
        normalized_request=request,
        graph=graph,
        expected_source="llm",
    )
    assert validation.valid, validation.errors
    optimization_request = DynamicInputOptimizationRequestAdapter().build(
        draft=enriched,
        graph=graph,
        map_context=map_bundle.context,
    )
    assert optimization_request.tasks == []
    compilation = IntegratedGoodsToPersonCompiler(repository).compile(
        simulation_id="SIM-V13-17-AGENT-G2P",
        normalized_request=request,
        optimization_request=optimization_request,
        graph_arcs=map_bundle.graph_arcs,
    )
    assert compilation.applied
    assert len(compilation.batches) == 2
    assert len(compilation.optimization_request.tasks) == 2
    assert compilation.optimization_request.minimum_vehicle_count == 2
    assert compilation.optimization_request.max_g2p_cycles_per_vehicle == 1
    get_settings.cache_clear()


def test_native_and_assignment_contract_require_distinct_amrs_for_multi_hu(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    monkeypatch.setenv("G2P_DISTINCT_ROBOT_PER_HANDLING_UNIT", "true")
    monkeypatch.setenv("G2P_MAX_CYCLES_PER_ROBOT_PER_WAVE", "1")
    from app.core.config import get_settings

    get_settings.cache_clear()
    repository = JsonWarehouseRepository(FIXTURE)
    context = WarehouseContextService(repository)
    inventory = context.build_inventory_context(order_ids=ORDER_IDS)
    robots = context.build_robot_context(required_capacity=1)
    map_bundle = context.build_map_context(inventory=inventory)
    base = OptimizationRequest(
        snapshot_id="SNAP-V13-17-DISTINCT",
        tasks=[],
        vehicles=[
            OptimizationVehicle(
                robot_id=value.robot_id,
                start_node=value.current_node,
                capacity_units=1,
                battery_pct=value.battery_pct,
            )
            for value in robots.robots
            if value.robot_id in robots.candidate_robot_ids
        ],
        map_constraints=map_bundle.context.map_constraints,
    )
    compilation = IntegratedGoodsToPersonCompiler(repository).compile(
        simulation_id="SIM-V13-17-DISTINCT",
        normalized_request=_request(),
        optimization_request=base,
        graph_arcs=map_bundle.graph_arcs,
    )
    compiled = compilation.optimization_request
    payload = CuOptPayloadBuilder().build(
        request=compiled,
        graph_nodes=map_bundle.graph_nodes,
        graph_arcs=map_bundle.graph_arcs,
        time_limit_seconds=5,
    )
    assert payload.fleet_data.min_vehicles == 2
    assert payload.fleet_data.max_g2p_cycles_per_vehicle == 1
    native = CuOptNativeRequestBuilder().build(payload)
    assert native["fleet_data"]["min_vehicles"] == 2

    all_tasks = list(payload.task_data.task_ids)
    invalid = OptimizerResult(
        backend="cuopt",
        status="success",
        optimizer="nvidia-cuopt",
        routes=[OptimizerRoute(vehicle_id=payload.fleet_data.vehicle_ids[0], task_sequence=all_tasks)],
    )
    invalid_validation = OptimizerAssignmentValidator().validate(payload=payload, result=invalid)
    assert not invalid_validation.valid
    assert any("fewer vehicles" in value for value in invalid_validation.errors)
    assert any("maximum is 1" in value for value in invalid_validation.errors)

    pairs = payload.task_data.pickup_and_delivery_pairs
    valid_routes = []
    for vehicle_id, pair in zip(payload.fleet_data.vehicle_ids, pairs, strict=True):
        valid_routes.append(
            OptimizerRoute(
                vehicle_id=vehicle_id,
                task_sequence=[
                    payload.task_data.task_ids[pair[0]],
                    payload.task_data.task_ids[pair[1]],
                ],
            )
        )
    valid = OptimizerResult(
        backend="cuopt",
        status="success",
        optimizer="nvidia-cuopt",
        routes=valid_routes,
    )
    assert OptimizerAssignmentValidator().validate(payload=payload, result=valid).valid
    get_settings.cache_clear()


def test_postgres_schema_uses_orders_not_outbound_orders() -> None:
    schema = (PROJECT_ROOT / "db" / "postgres" / "001_schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS orders" in schema
    assert "CREATE TABLE IF NOT EXISTS order_lines" in schema
    assert "CREATE TABLE IF NOT EXISTS outbound_batches" in schema
    assert "CREATE TABLE IF NOT EXISTS outbound_batch_orders" in schema
    assert "CREATE TABLE IF NOT EXISTS outbound_orders" not in schema


def test_inventory_authority_conflict_is_not_a_physical_incident() -> None:
    from app.graph.input_formulation import _strip_nonphysical_data_conflict_incidents
    from app.domain.schemas import OperationalIncidentImpact

    request = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            ),
            NormalizedOperation(
                operation_id="INCIDENT-FAKE",
                operation_type="INCIDENT",
                raw_reference="K1_7-L1 inventory mismatch",
            ),
        ],
        incidents=[
            OperationalIncidentImpact(
                incident_id="INCIDENT-FAKE",
                description="K1_7-L1 시스템 재고와 센서 수량 불일치",
                scope="MAP_RESOURCE",
                affected_resource_ids=["K1_7"],
                observed_effect="UNKNOWN",
                handling_mode="REQUIRE_HUMAN_DECISION",
                immediate_safety_action="TEMPORARILY_BLOCK_RESOURCE",
            )
        ],
        constraints=NormalizedRequestConstraints(),
        raw_user_command="ORD-001 처리 전 K1_7-L1 시스템 재고와 센서 수량이 불일치해.",
        normalization_summary="LLM incorrectly emitted a physical incident.",
    )
    cleaned = _strip_nonphysical_data_conflict_incidents(
        {
            "events": [],
            "user_command": request.raw_user_command,
        },
        request,
    )
    assert cleaned.incidents == []
    assert all(value.operation_type != "INCIDENT" for value in cleaned.operations)
    assert "Inventory authority conflict" in cleaned.normalization_summary

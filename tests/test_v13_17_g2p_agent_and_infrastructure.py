"""v13.17 G2P Agent, distinct-AMR, path, and infrastructure regressions."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.domain.schemas import (
    ContextSnapshot,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    OptimizerResult,
    OptimizerRoute,
)
from app.repositories.json_repository import get_repository, set_data_dir
from app.repositories.neo4j_map_repository import Neo4jMapRepository
from app.services.cuopt_formulation_service import (
    CuOptDynamicInputValidator,
    DynamicInputOptimizationRequestAdapter,
    RuleCuOptFormulator,
)
from app.services.goods_to_person_compiler_service import IntegratedGoodsToPersonCompiler
from app.services.optimization_service import (
    CuOptNativeRequestBuilder,
    CuOptPayloadBuilder,
    CuOptPayloadValidator,
    OptimizerAssignmentValidator,
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

FIXTURE = PROJECT_ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_multi_hu"
ORDER_IDS = [f"ORD-{index:03d}" for index in range(1, 6)]


def _normalized_request() -> NormalizedWarehouseRequest:
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
        normalization_summary="Five canonical G2P orders.",
    )


def _agent_contexts():
    request = _normalized_request()
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
        canonical_request,
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
    versions = get_repository().versions
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V13-17-AGENT-G2P",
        captured_at=datetime.now(timezone.utc).isoformat(),
        graph_version=versions["graph_version"],
        inventory_version=versions["inventory_version"],
        runtime_version=versions["runtime_version"],
    )
    return (
        canonical_request,
        inventory,
        robots,
        map_context,
        graph_nodes,
        graph_node_types,
        graph_arcs,
        snapshot,
        outcome.observations,
    )


def test_blank_optional_database_paths_are_none(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        LOCAL_DB_DIR=str(tmp_path / "local-db"),
        LOCAL_POSTGRES_PATH="",
        LOCAL_REDIS_PATH="   ",
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


def test_agent_situation_graph_uses_g2p_physical_paths_and_compiles_hu_cycles(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    monkeypatch.setenv("G2P_DISTINCT_ROBOT_PER_HANDLING_UNIT", "true")
    monkeypatch.setenv("G2P_MAX_CYCLES_PER_ROBOT_PER_WAVE", "1")
    get_settings.cache_clear()
    set_data_dir(FIXTURE)
    try:
        (
            request,
            inventory,
            robots,
            map_context,
            _graph_nodes,
            _graph_node_types,
            graph_arcs,
            snapshot,
            observations,
        ) = _agent_contexts()
        graph = WarehouseSituationGraphBuilder().build(
            normalized_request=request,
            snapshot=snapshot,
            inventory=inventory,
            robots=robots,
            map_context=map_context,
            graph_arcs=graph_arcs,
            retrieval_observations=observations,
        )
        validation = WarehouseSituationGraphValidator().validate(graph)
        assert graph.fulfillment_mode == "goods_to_person"
        assert graph.g2p_order_ids == ORDER_IDS
        assert graph.completeness.ready_for_formulation, graph.completeness.missing_information
        assert validation.valid, validation.errors
        purposes = {value.purpose for value in graph.path_evidence}
        assert {"ROBOT_TO_PICKUP", "PICKUP_TO_STATION", "STATION_TO_POST_MOVE"} <= purposes
        assert "PICKUP_TO_DELIVERY" not in purposes
        assert any(value.node_type == "outbound_station" for value in graph.nodes)
        assert any(value.node_type == "handling_unit" for value in graph.nodes)
        assert any(value.node_type == "logical_destination" for value in graph.nodes)

        draft = RuleCuOptFormulator().formulate(
            normalized_request=request,
            graph=graph,
            time_limit_seconds=5,
        ).model_copy(update={"formulation_source": "llm"})
        draft_validation = CuOptDynamicInputValidator().validate(
            draft=draft,
            normalized_request=request,
            graph=graph,
            expected_source="llm",
        )
        assert draft.formulation_mode == "GOODS_TO_PERSON"
        assert draft.tasks == []
        assert draft.g2p_order_ids == ORDER_IDS
        assert draft_validation.valid, draft_validation.errors

        base_request = DynamicInputOptimizationRequestAdapter().build(
            draft=draft,
            graph=graph,
            map_context=map_context,
        )
        compilation = IntegratedGoodsToPersonCompiler().compile(
            simulation_id="SIM-V13-17-AGENT-G2P",
            normalized_request=request,
            optimization_request=base_request,
            graph_arcs=graph_arcs,
        )
        assert compilation.applied
        assert compilation.optimization_request is not None
        compiled = compilation.optimization_request
        assert len(compilation.batches) == 2
        assert len(compiled.tasks) == 2
        assert compiled.minimum_vehicle_count == 2
        assert compiled.max_g2p_cycles_per_vehicle == 1

        payload = CuOptPayloadBuilder().build(
            request=compiled,
            graph_nodes=list(get_repository().nodes),
            graph_arcs=graph_arcs,
            time_limit_seconds=5,
        )
        payload_validation = CuOptPayloadValidator().validate(payload)
        assert payload_validation.valid, payload_validation.errors
        assert payload.fleet_data.min_vehicles == 2
        assert payload.fleet_data.max_g2p_cycles_per_vehicle == 1
        native = CuOptNativeRequestBuilder().build(payload)
        assert native["fleet_data"]["min_vehicles"] == 2

        # A provider response that assigns both physical handling units to one
        # AMR violates the wave-level physical policy even if pickup-before-drop
        # remains valid.  This catches the exact v13.16 behavior observed in the
        # live NVIDIA result.
        first, second = [value.task_id for value in compiled.tasks]
        same_robot = OptimizerResult(
            backend="cuopt",
            status="success",
            optimizer="unit-test",
            routes=[
                OptimizerRoute(
                    vehicle_id=compiled.vehicles[0].robot_id,
                    task_sequence=[
                        f"{first}_PICK", f"{first}_DROP",
                        f"{second}_PICK", f"{second}_DROP",
                    ],
                )
            ],
        )
        assignment = OptimizerAssignmentValidator().validate(
            payload=payload,
            result=same_robot,
        )
        assert not assignment.valid
        assert any("fewer vehicles" in value for value in assignment.errors)
        assert any("maximum is 1" in value for value in assignment.errors)
    finally:
        set_data_dir(None)
        get_settings.cache_clear()


class _FakeRecord(dict):
    pass


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def single(self):
        return self.row


class _FakeSession:
    def __init__(self, queries: list[str]):
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query: str, **kwargs):
        del kwargs
        self.queries.append(query)
        return _FakeResult(_FakeRecord(ok=1))


class _FakeDriver:
    def __init__(self):
        self.queries: list[str] = []

    def session(self, **kwargs):
        del kwargs
        return _FakeSession(self.queries)


def test_neo4j_ping_does_not_reference_unseeded_route_labels() -> None:
    driver = _FakeDriver()
    result = Neo4jMapRepository(driver=driver).ping()
    assert result["ok"] is True
    assert driver.queries == ["RETURN 1 AS ok"]

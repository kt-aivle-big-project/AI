"""v13.16 canonical key building and dependency-derived retrieval contracts."""
from __future__ import annotations

from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings
from app.domain.schemas import (
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    ParallelRetrievalPlan,
    RetrievalToolRequest,
)
from app.repositories.json_repository import set_data_dir
from app.services.parallel_retrieval_service import (
    ParallelRetrievalExecutor,
    ParallelRetrievalPlanCompiler,
    ParallelRetrievalPlanValidator,
)

FIXTURE = PROJECT_ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_multi_hu"


def _request() -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id=f"ORD-{index:03d}",
                operation_type="OUTBOUND_ORDER",
            )
            for index in range(1, 6)
        ],
        constraints=NormalizedRequestConstraints(),
        normalization_summary="v13.16 derived-key test",
    )


def test_relative_data_paths_are_project_rooted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(DATA_DIR="data", OUTPUT_DIR="runtime_outputs", _env_file=None)
    assert settings.data_dir == (PROJECT_ROOT / "data").resolve()
    assert settings.output_dir == (PROJECT_ROOT / "runtime_outputs").resolve()


def test_canonical_key_builder_skips_optional_llm_for_code_first_outbound() -> None:
    compiler = ParallelRetrievalPlanCompiler()
    request = _request()
    plan = compiler.build_canonical_plan(normalized_request=request)

    assert [value.request_id for value in plan.requests] == [
        "ORDER_FACTS",
        "ROBOT_RUNTIME",
        "INVENTORY_CANDIDATES",
        "CONNECTING_SUBGRAPH",
        "PATH_RUNTIME",
    ]
    assert not compiler.should_invoke_optional_planner(
        normalized_request=request,
        canonical_plan=plan,
        mode="auto",
    )
    assert compiler.should_invoke_optional_planner(
        normalized_request=request,
        canonical_plan=plan,
        mode="always",
    )


def test_dependency_derived_map_keys_are_validated_after_materialization() -> None:
    set_data_dir(FIXTURE)
    try:
        request = _request()
        compiler = ParallelRetrievalPlanCompiler()
        canonical = compiler.build_canonical_plan(normalized_request=request)
        proposed = ParallelRetrievalPlan(
            requests=[
                RetrievalToolRequest(
                    request_id="r_order",
                    tool_name="get_order_facts",
                    exact_ids=["ORD-001"],
                    purpose="Redundant order read that the canonical compiler removes.",
                ),
                RetrievalToolRequest(
                    request_id="r_inventory",
                    tool_name="get_inventory_candidates",
                    derive_from_previous_results=True,
                    depends_on=["r_order"],
                    purpose="Redundant inventory read that maps to the canonical request.",
                ),
                RetrievalToolRequest(
                    request_id="r_map",
                    tool_name="resolve_map_entities",
                    expected_entity_types=["NODE", "EDGE", "RACK"],
                    derive_from_previous_results=True,
                    depends_on=["r_order", "r_inventory"],
                    purpose="Validate rack masters, rack access nodes, and destinations derived from prior reads.",
                ),
            ],
            planning_summary="Optional derived map validation.",
        )
        plan = compiler.compile(
            normalized_request=request,
            proposed=proposed,
            canonical_plan=canonical,
        )
        validation = ParallelRetrievalPlanValidator().validate(plan)
        assert validation.valid, validation.errors

        outcome = ParallelRetrievalExecutor().execute(
            plan=plan,
            normalized_request=request,
            llm_planning_call_count=1,
        )
        assert outcome.execution.valid, outcome.execution.errors
        assert outcome.sufficiency.ready
        assert [value.wave_index for value in outcome.execution.wave_records] == [1, 2, 3, 4]
        map_observations = [
            value for value in outcome.observations
            if value.tool_name == "resolve_map_entities"
        ]
        assert len(map_observations) == 1
        map_data = map_observations[0].data
        assert {value["rack_id"] for value in map_data["racks"]} >= {"K1_7", "K2_7"}
        assert any(value["id"].endswith("_ACCESS_A") for value in map_data["nodes"])
        assert "ORD-001" in {
            entity_id
            for observation in outcome.observations
            for entity_id in observation.canonical_entity_ids
        }
    finally:
        set_data_dir(None)


def test_dependency_derived_map_request_without_upstream_source_is_rejected() -> None:
    plan = ParallelRetrievalPlan(
        requests=[
            RetrievalToolRequest(
                request_id="BROKEN_MAP",
                tool_name="resolve_map_entities",
                derive_from_previous_results=True,
                depends_on=[],
                purpose="Invalid derived request without an upstream source.",
            )
        ],
        planning_summary="invalid",
    )
    result = ParallelRetrievalPlanValidator().validate(plan)
    assert not result.valid
    assert "DERIVED_REQUEST_REQUIRES_DEPENDENCY:BROKEN_MAP" in result.errors


def test_parallel_robot_snapshot_applies_explicit_robot_exclusion() -> None:
    from app.domain.schemas import ResolvedToolRequest
    from app.services.stepwise_retrieval_service import WarehouseReadToolExecutor

    set_data_dir(FIXTURE)
    try:
        observation = WarehouseReadToolExecutor().execute(
            request=ResolvedToolRequest(
                request_id="ROBOT_RUNTIME",
                tool_name="get_robot_candidates",
                robot_ids=["R001"],
                include_statuses=["idle"],
                purpose="Verify that authoritative robot exclusions affect candidate selection.",
            ),
            observations=[],
            request_fingerprint="test-explicit-robot-exclusion",
        )
        assert "R001" not in observation.data["candidate_robot_ids"]
        assert "R001" in observation.data["excluded_by_reason"]["explicit_robot_exclusion"]
    finally:
        set_data_dir(None)

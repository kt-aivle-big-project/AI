"""Validation tests for Warehouse Situation Graph and cuOpt dynamic formulation."""
from __future__ import annotations

from datetime import datetime, timezone

from app.domain.schemas import (
    ConditionalEdgePolicy,
    ContextSnapshot,
    CuOptTaskDraft,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    SituationRelation,
)
from app.repositories.json_repository import get_repository
from app.services.context_service import WarehouseContextService
from app.services.cuopt_formulation_service import (
    CuOptDynamicInputValidator,
    DynamicInputOptimizationRequestAdapter,
    RuleCuOptFormulator,
)
from app.services.optimization_service import CandidateSpaceGuard, CuOptPayloadBuilder, CuOptPayloadValidator
from app.services.situation_graph_service import (
    WarehouseSituationGraphBuilder,
    WarehouseSituationGraphValidator,
)


def _bundle(*, constraints: NormalizedRequestConstraints | None = None):
    repository = get_repository()
    context_service = WarehouseContextService(repository)
    normalized = NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
            )
        ],
        constraints=constraints or NormalizedRequestConstraints(),
        normalization_summary="test request",
    )
    inventory = context_service.build_inventory_context(order_ids=["ORD-001"])
    robots = context_service.build_robot_context(required_capacity=1)
    map_bundle = context_service.build_map_context(inventory=inventory)
    versions = repository.versions
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V12-TEST",
        captured_at=datetime.now(timezone.utc).isoformat(),
        graph_version=versions["graph_version"],
        inventory_version=versions["inventory_version"],
        runtime_version=versions["runtime_version"],
    )
    graph = WarehouseSituationGraphBuilder(repository).build(
        normalized_request=normalized,
        snapshot=snapshot,
        inventory=inventory,
        robots=robots,
        map_context=map_bundle.context,
        graph_arcs=map_bundle.graph_arcs,
    )
    return repository, normalized, inventory, robots, map_bundle, graph


def test_situation_graph_joins_all_sources_and_is_complete() -> None:
    repository, normalized, inventory, robots, map_bundle, graph = _bundle()
    validation = WarehouseSituationGraphValidator(repository).validate(graph)
    assert validation.valid, validation.errors
    assert graph.completeness.ready_for_formulation
    assert {value.source for value in graph.evidence_index} == {
        "request",
        "inventory_store",
        "robot_runtime",
        "traffic_runtime",
        "warehouse_graph",
    }
    node_ids = {node.node_id for node in graph.nodes}
    assert "order:ORD-001" in node_ids
    assert "stock:STOCK-K1_7-L1-ITEM_BEARING" in node_ids
    assert "stock:STOCK-K2_7-L2-ITEM_BEARING" in node_ids
    assert "robot:R002" in node_ids and "robot:R003" in node_ids
    assert "constraint:congested:H3_7" in node_ids
    assert "constraint:occupied:H3_8:R001" in node_ids
    assert any(path.purpose == "ROBOT_TO_PICKUP" for path in graph.path_evidence)
    assert any(path.purpose == "PICKUP_TO_DELIVERY" for path in graph.path_evidence)


def test_situation_graph_validator_detects_broken_relation() -> None:
    repository, *_rest, graph = _bundle()
    broken = graph.model_copy(
        update={
            "relations": [
                *graph.relations,
                SituationRelation(
                    relation_id="rel:broken",
                    source_node_id="order:ORD-001",
                    target_node_id="map:DOES-NOT-EXIST",
                    relation_type="DELIVER_TO",
                ),
            ]
        }
    )
    validation = WarehouseSituationGraphValidator(repository).validate(broken)
    assert not validation.valid
    assert any("unknown target" in error for error in validation.errors)


def test_rule_formulation_is_fact_grounded_and_payload_ready() -> None:
    repository, normalized, inventory, robots, map_bundle, graph = _bundle()
    draft = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=graph,
        time_limit_seconds=5,
    )
    validation = CuOptDynamicInputValidator().validate(
        draft=draft,
        normalized_request=normalized,
        graph=graph,
        expected_source="rule",
    )
    assert validation.valid, validation.errors
    assert len(draft.tasks) == 1
    assert draft.tasks[0].order_id == "ORD-001"
    assert draft.tasks[0].rack_id in {"K1_7", "K2_7"}
    assert draft.tasks[0].pickup_node in {
        "K1_7_ACCESS_A", "K1_7_ACCESS_B",
        "K2_7_ACCESS_A", "K2_7_ACCESS_B",
    }
    assert draft.fleet.included_robot_ids == ["R002", "R003"]
    request = DynamicInputOptimizationRequestAdapter().build(
        draft=draft,
        graph=graph,
        map_context=map_bundle.context,
    )
    penalty_map = {
        value.edge_id: (value.cost_multiplier, value.travel_time_multiplier)
        for value in request.map_constraints.edge_penalties
    }
    arcs = repository.adjusted_arcs(
        blocked_edge_ids=set(request.map_constraints.blocked_edge_ids),
        blocked_node_ids=set(request.map_constraints.blocked_node_ids),
        edge_penalties=penalty_map,
    )
    payload = CuOptPayloadBuilder().build(
        request=request,
        graph_nodes=map_bundle.graph_nodes,
        graph_arcs=arcs,
        time_limit_seconds=5,
    )
    assert CuOptPayloadValidator().validate(payload).valid
    assert CandidateSpaceGuard().validate(request=request, payload=payload).valid
    assert len(payload.location_index_map) == 220
    assert len(payload.waypoint_graph_data.edge_ids) == 356
    assert payload.task_data.pickup_and_delivery_pairs == [[0, 1]]


def test_validator_rejects_llm_fact_changes_without_correcting_them() -> None:
    _repository, normalized, _inventory, _robots, _map_bundle, graph = _bundle()
    draft = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=graph,
        time_limit_seconds=5,
    ).model_copy(update={"formulation_source": "llm"})
    task = draft.tasks[0]
    tampered = draft.model_copy(
        update={
            "tasks": [
                task.model_copy(
                    update={
                        "demand": task.demand + 1,
                        "delivery_node": "O_A",
                    }
                )
            ]
        }
    )
    validation = CuOptDynamicInputValidator().validate(
        draft=tampered,
        normalized_request=normalized,
        graph=graph,
        expected_source="llm",
    )
    assert not validation.valid
    assert validation.repairable
    assert "TASK_DEMAND_MISMATCH:ORD-001" in validation.errors
    assert "TASK_DELIVERY_MISMATCH:ORD-001" in validation.errors
    # The validator reports errors; it does not mutate the submitted draft.
    assert tampered.tasks[0].demand == 5
    assert tampered.tasks[0].delivery_node == "O_A"


def test_validator_rejects_silent_robot_candidate_pruning() -> None:
    _repository, normalized, _inventory, _robots, _map_bundle, graph = _bundle()
    draft = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=graph,
        time_limit_seconds=5,
    ).model_copy(update={"formulation_source": "llm"})
    pruned = draft.model_copy(
        update={
            "fleet": draft.fleet.model_copy(update={"included_robot_ids": ["R002"]})
        }
    )
    validation = CuOptDynamicInputValidator().validate(
        draft=pruned,
        normalized_request=normalized,
        graph=graph,
        expected_source="llm",
    )
    assert not validation.valid
    assert any(error.startswith("FLEET_CANDIDATE_SPACE_MISMATCH") for error in validation.errors)


def test_explicit_soft_avoid_is_preserved_and_penalized() -> None:
    constraints = NormalizedRequestConstraints(soft_avoid_edge_ids=["H1_5"])
    repository, normalized, _inventory, _robots, map_bundle, graph = _bundle(constraints=constraints)
    draft = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=graph,
        time_limit_seconds=5,
    )
    validation = CuOptDynamicInputValidator().validate(
        draft=draft,
        normalized_request=normalized,
        graph=graph,
        expected_source="rule",
    )
    assert validation.valid, validation.errors
    assert "H1_5" in draft.map_constraints.soft_penalty_edge_ids
    request = DynamicInputOptimizationRequestAdapter().build(
        draft=draft,
        graph=graph,
        map_context=map_bundle.context,
    )
    penalty = next(value for value in request.map_constraints.edge_penalties if value.edge_id == "H1_5")
    assert penalty.cost_multiplier == 1.25
    base_cost, base_time = repository.base_edge_metrics("H1_5")
    arcs = repository.adjusted_arcs(
        blocked_edge_ids=set(),
        blocked_node_ids=set(),
        edge_penalties={
            value.edge_id: (value.cost_multiplier, value.travel_time_multiplier)
            for value in request.map_constraints.edge_penalties
        },
    )
    arc = next(value for value in arcs if value["edge_id"] == "H1_5")
    assert arc["cost"] > base_cost
    assert arc["travel_time_ms"] > base_time



def test_conditional_edge_policy_accepts_either_declared_runtime_action() -> None:
    """A02 may resolve H3_7 to hard or soft, but never to an undeclared action."""

    constraints = NormalizedRequestConstraints(
        soft_avoid_edge_ids=["H3_7"],
        conditional_edge_policies=[
            ConditionalEdgePolicy(
                edge_id="H3_7",
                metric="EXPECTED_WAIT_MS",
                operator="GT",
                threshold_ms=8000,
                when_true="HARD_AVOID",
                when_false="SOFT_AVOID",
                source_text="H3_7 wait > 8s => hard, otherwise soft",
            )
        ],
        max_edge_wait_ms=8000,
    )
    _repository, normalized, _inventory, _robots, _map_bundle, graph = _bundle(
        constraints=constraints
    )
    soft_draft = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=graph,
        time_limit_seconds=5,
    )
    soft_validation = CuOptDynamicInputValidator().validate(
        draft=soft_draft,
        normalized_request=normalized,
        graph=graph,
        expected_source="rule",
    )
    assert soft_validation.valid, soft_validation.errors

    hard_draft = soft_draft.model_copy(
        update={
            "formulation_source": "llm",
            "map_constraints": soft_draft.map_constraints.model_copy(
                update={
                    "blocked_edge_ids": [
                        *soft_draft.map_constraints.blocked_edge_ids,
                        "H3_7",
                    ],
                    "soft_penalty_edge_ids": [
                        edge_id
                        for edge_id in soft_draft.map_constraints.soft_penalty_edge_ids
                        if edge_id != "H3_7"
                    ],
                }
            ),
        }
    )
    hard_validation = CuOptDynamicInputValidator().validate(
        draft=hard_draft,
        normalized_request=normalized,
        graph=graph,
        expected_source="llm",
    )
    assert hard_validation.valid, hard_validation.errors

    allow_draft = hard_draft.model_copy(
        update={
            "map_constraints": hard_draft.map_constraints.model_copy(
                update={
                    "blocked_edge_ids": [
                        edge_id
                        for edge_id in hard_draft.map_constraints.blocked_edge_ids
                        if edge_id != "H3_7"
                    ],
                    "soft_penalty_edge_ids": [
                        edge_id
                        for edge_id in hard_draft.map_constraints.soft_penalty_edge_ids
                        if edge_id != "H3_7"
                    ],
                }
            )
        }
    )
    allow_validation = CuOptDynamicInputValidator().validate(
        draft=allow_draft,
        normalized_request=normalized,
        graph=graph,
        expected_source="llm",
    )
    assert not allow_validation.valid
    assert any(
        error.startswith("CONDITIONAL_EDGE_POLICY_MISMATCH:H3_7")
        for error in allow_validation.errors
    )

def test_manual_rule_node_pipeline_reaches_candidate_space_validation() -> None:
    """Exercise the v12 direct Rule path without any retrieval-planning node."""

    from app.domain.schemas import AutoMissionRequest, EventInput
    from app.graph.context_snapshot import context_snapshot_finalize_node
    from app.graph.cuopt_formulation import (
        cuopt_dynamic_input_validator_node,
        optimization_request_from_dynamic_input_node,
    )
    from app.graph.entry_routing import entry_route_classifier_node
    from app.graph.input_formulation import (
        deterministic_formulation_supervisor_node,
        structured_request_normalizer_node,
    )
    from app.graph.inventory_context import inventory_context_node
    from app.graph.map_context import map_context_node
    from app.graph.optimization import cuopt_payload_node, cuopt_schema_validator_node
    from app.graph.orchestration_plan import orchestration_plan_builder_node
    from app.graph.robot_runtime import robot_runtime_node
    from app.graph.rule_direct import (
        rule_cuopt_formulator_direct_node,
        structured_key_validator_node,
    )
    from app.graph.v9_planning import candidate_space_guard_node

    request = AutoMissionRequest(
        simulation_id="SIM-V12-MANUAL",
        request_mode="event_driven",
        planning_mode="force_rule",
        optimization_backend="cuopt_payload_only",
        events=[EventInput(type="new_order", order_id="ORD-001")],
    )
    state = {
        "simulation_id": request.simulation_id,
        "request_mode": request.request_mode,
        "optimization_backend": request.optimization_backend,
        "planning_mode": request.planning_mode,
        "max_agent_steps": request.max_agent_steps,
        "events": request.events,
        "user_command": None,
        "max_planner_retries": 1,
        "retry_count": 0,
        "formulation_retry_count": 0,
        "workflow_trace": [],
        "node_execution_log": [],
        "llm_node_summaries": [],
        "errors": [],
        "completed_context_nodes": [],
        "retrieval_observations": [],
        "completed_retrieval_tools": [],
        "validation_issues": [],
        "validation_issue_history": [],
        "current_entity_resolutions": [],
        "entity_resolution_history": [],
        "workflow_status": "running",
        "failure_requested": False,
    }
    reducer_keys = {
        "workflow_trace",
        "node_execution_log",
        "llm_node_summaries",
        "errors",
        "completed_context_nodes",
        "retrieval_observations",
        "completed_retrieval_tools",
        "validation_issue_history",
        "entity_resolution_history",
    }

    def apply(update):
        for key, value in update.items():
            if key in reducer_keys:
                state[key] = [*state.get(key, []), *value]
            else:
                state[key] = value

    nodes = [
        entry_route_classifier_node,
        structured_request_normalizer_node,
        deterministic_formulation_supervisor_node,
        orchestration_plan_builder_node,
        structured_key_validator_node,
        inventory_context_node,
        map_context_node,
        robot_runtime_node,
        context_snapshot_finalize_node,
        rule_cuopt_formulator_direct_node,
        cuopt_dynamic_input_validator_node,
        optimization_request_from_dynamic_input_node,
        cuopt_payload_node,
        cuopt_schema_validator_node,
        candidate_space_guard_node,
    ]
    for node in nodes:
        apply(node(state))
        assert not state.get("failure_requested"), state.get("errors")

    assert state["entry_route_decision"].route == "NORMAL_FORMULATION"
    assert state["orchestration_plan"].route == "RULE_MISSION_PIPELINE"
    assert state["orchestration_plan"].retrieval_strategy == "DIRECT_CONTEXT"
    assert state["structured_key_validation"].valid
    assert "warehouse_situation_graph" not in state
    assert state["cuopt_dynamic_input_validation"].valid
    assert state["payload_validation"].valid
    assert state["candidate_space_validation"].valid
    assert "rule_query_planner" not in state["workflow_trace"]
    assert "query_key_resolver" not in state["workflow_trace"]
    assert "warehouse_situation_graph_builder" not in state["workflow_trace"]
    assert "rule_cuopt_formulator_direct" in state["workflow_trace"]


def test_unknown_explicit_edge_blocks_situation_graph_formulation() -> None:
    constraints = NormalizedRequestConstraints(soft_avoid_edge_ids=["EDGE-DOES-NOT-EXIST"])
    repository, _normalized, _inventory, _robots, _map_bundle, graph = _bundle(constraints=constraints)
    validation = WarehouseSituationGraphValidator(repository).validate(graph)
    assert not validation.valid
    assert not graph.completeness.ready_for_formulation
    assert any("unknown edge" in value.lower() for value in validation.errors)


def test_ten_order_situation_graph_and_dynamic_payload_preserve_full_batch() -> None:
    """Validate the multi-order formulation contract without LangGraph or a live solver."""

    from pathlib import Path

    from app.repositories.json_repository import JsonWarehouseRepository

    project_root = Path(__file__).resolve().parents[1]
    fixture = project_root / "scenarios" / "fixtures" / "V9_ten_orders_multitask"
    repository = JsonWarehouseRepository(fixture)
    service = WarehouseContextService(repository)
    order_ids = [f"ORD-{index:03d}" for index in range(1, 11)]
    normalized = NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id=order_id,
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
            )
            for order_id in order_ids
        ],
        constraints=NormalizedRequestConstraints(),
        normalization_summary="ten-order validation batch",
    )
    inventory = service.build_inventory_context(order_ids=order_ids)
    robots = service.build_robot_context(required_capacity=1)
    map_bundle = service.build_map_context(inventory=inventory)
    versions = repository.versions
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V12-TEN-ORDER",
        captured_at=datetime.now(timezone.utc).isoformat(),
        graph_version=versions["graph_version"],
        inventory_version=versions["inventory_version"],
        runtime_version=versions["runtime_version"],
    )
    situation = WarehouseSituationGraphBuilder(repository).build(
        normalized_request=normalized,
        snapshot=snapshot,
        inventory=inventory,
        robots=robots,
        map_context=map_bundle.context,
        graph_arcs=map_bundle.graph_arcs,
    )
    situation_validation = WarehouseSituationGraphValidator(repository).validate(situation)
    assert situation_validation.valid, situation_validation.errors
    assert situation.completeness.ready_for_formulation

    draft = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=situation,
        time_limit_seconds=5,
    )
    dynamic_validation = CuOptDynamicInputValidator().validate(
        draft=draft,
        normalized_request=normalized,
        graph=situation,
        expected_source="rule",
    )
    assert dynamic_validation.valid, dynamic_validation.errors
    assert len(draft.tasks) == 10
    assert not draft.deferred_order_ids
    assert len(draft.fleet.included_robot_ids) == 5
    assert sum(task.demand for task in draft.tasks) == 16

    request = DynamicInputOptimizationRequestAdapter().build(
        draft=draft,
        graph=situation,
        map_context=map_bundle.context,
    )
    arcs = repository.adjusted_arcs(
        blocked_edge_ids=set(request.map_constraints.blocked_edge_ids),
        blocked_node_ids=set(request.map_constraints.blocked_node_ids),
        edge_penalties={
            value.edge_id: (value.cost_multiplier, value.travel_time_multiplier)
            for value in request.map_constraints.edge_penalties
        },
    )
    payload = CuOptPayloadBuilder().build(
        request=request,
        graph_nodes=map_bundle.graph_nodes,
        graph_arcs=arcs,
        time_limit_seconds=5,
    )
    assert CuOptPayloadValidator().validate(payload).valid
    assert CandidateSpaceGuard().validate(request=request, payload=payload).valid
    assert len(payload.task_data.pickup_and_delivery_pairs) == 10
    assert len(payload.task_data.task_ids) == 20
    assert len(payload.fleet_data.vehicle_ids) == 5
    assert len(payload.location_index_map) == 220
    assert len(payload.waypoint_graph_data.edge_ids) == 356

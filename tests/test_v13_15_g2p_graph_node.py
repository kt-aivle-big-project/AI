"""Unit contracts for the integrated G2P compiler and execution enricher."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.domain.schemas import (
    MapConstraints,
    NormalizedOperation,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    OptimizationTask,
    OptimizationVehicle,
    OptimizerResult,
    OptimizerRoute,
    OrchestrationPlan,
)
from app.graph.goods_to_person import (
    goods_to_person_compiler_node,
    goods_to_person_execution_enricher_node,
)
from app.repositories.json_repository import JsonWarehouseRepository, set_data_dir
from app.services.context_service import WarehouseContextService
from app.services.optimization_service import CuOptPayloadBuilder

ROOT = Path(__file__).resolve().parents[1]
RETURN_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_return"
DEPLETED_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_depleted"
MULTI_HU_FIXTURE = ROOT / "scenarios" / "fixtures" / "V15_integrated_goods_to_person_multi_hu"
ORDER_IDS = [f"ORD-{index:03d}" for index in range(1, 6)]


def _plan() -> OrchestrationPlan:
    return OrchestrationPlan(
        orchestration_goal="Compile canonical outbound orders into G2P handling-unit work.",
        route="RULE_MISSION_PIPELINE",
        formulation_route="RULE_FORMULATION",
        retrieval_strategy="DIRECT_CONTEXT",
        routing_source="deterministic_event_mapping",
        planning_mode="force_rule",
        route_locked=True,
        route_switch_allowed=False,
        needs_optimization=True,
    )


def _normalized() -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id=value,
                operation_type="OUTBOUND_ORDER",
                source_event_type="new_order",
                raw_reference=value,
            )
            for value in ORDER_IDS
        ],
        normalization_summary="Five canonical outbound orders.",
    )


def _request(repository: JsonWarehouseRepository) -> OptimizationRequest:
    tasks: list[OptimizationTask] = []
    for order_id in ORDER_IDS:
        order = repository.get_order(order_id)
        assert order is not None
        tasks.append(
            OptimizationTask(
                task_id=f"TASK-{order_id}",
                pickup_node="K1_7_ACCESS_A",
                delivery_node=str(order["delivery_node"]),
                demand=int(order["required_qty"]),
                priority=str(order.get("priority", "medium")),
                operation_type="OUTBOUND_ORDER",
                order_id=order_id,
                order_ids=[order_id],
                item_id=str(order["item_id"]),
            )
        )
    vehicles = [
        OptimizationVehicle(
            robot_id=str(value["robot_id"]),
            start_node=str(value["current_node"]),
            capacity_units=int(value["capacity_units"]),
            battery_pct=float(value["battery_pct"]),
        )
        for value in repository.scenario["robots"]
        if str(value.get("status")) == "idle"
    ]
    return OptimizationRequest(
        snapshot_id="SNAP-G2P-UNIT",
        tasks=tasks,
        vehicles=vehicles,
        map_constraints=MapConstraints(),
    )


def _compile(
    monkeypatch: pytest.MonkeyPatch,
    fixture: Path,
):
    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    get_settings.cache_clear()
    set_data_dir(fixture)
    try:
        repository = JsonWarehouseRepository(fixture)
        from app.services.context_service import WarehouseContextService
        bundle = WarehouseContextService(repository).build_map_context()
        state = {
            "simulation_id": str(repository.scenario["simulation_id"]),
            "orchestration_plan": _plan(),
            "normalized_request": _normalized(),
            "optimization_request": _request(repository),
            "graph_arcs": repository.adjusted_arcs(
                blocked_edge_ids=set(),
                blocked_node_ids=set(),
                edge_penalties={},
            ),
        }
        update = goods_to_person_compiler_node(state)
        return repository, state, update
    finally:
        set_data_dir(None)
        get_settings.cache_clear()


def test_compiler_aggregates_five_orders_into_one_handling_unit_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository, _state, update = _compile(monkeypatch, RETURN_FIXTURE)

    assert not update.get("failure_requested", False), update
    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    assert compilation.applied
    assert compilation.source_order_ids == ORDER_IDS
    assert len(compilation.batches) == 1
    assert len(compiled_request.tasks) == 1
    task = compiled_request.tasks[0]
    batch = compilation.batches[0]
    assert task.operation_type == "G2P_HANDLING_UNIT"
    assert task.order_ids == ORDER_IDS
    assert task.pickup_node == batch.source_access_node
    assert task.delivery_node == batch.station_access_node
    assert batch.requested_quantity == 8
    assert batch.quantity_after == 4
    assert batch.return_required
    assert batch.post_station_node == batch.source_access_node


def test_compiler_creates_multiple_physical_cycles_for_distributed_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository, _state, update = _compile(monkeypatch, MULTI_HU_FIXTURE)

    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    assert compilation.applied
    assert len(compilation.batches) == 2
    assert len(compiled_request.tasks) == 2
    assert len({value.handling_unit_id for value in compilation.batches}) == 2
    assert sum(value.requested_quantity for value in compilation.batches) == 15


def test_execution_enricher_appends_same_robot_return_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, state, update = _compile(monkeypatch, RETURN_FIXTURE)
    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    nodes = [str(value["id"]) for value in repository.graph["nodes"]]
    payload = CuOptPayloadBuilder().build(
        request=compiled_request,
        graph_nodes=nodes,
        graph_arcs=repository.adjusted_arcs(
            blocked_edge_ids=set(),
            blocked_node_ids=set(),
            edge_penalties={},
        ),
        time_limit_seconds=5,
    )
    batch = compilation.batches[0]
    result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R002",
                task_sequence=[f"{batch.batch_id}_PICK", f"{batch.batch_id}_DROP"],
            )
        ],
    )
    enriched = goods_to_person_execution_enricher_node(
        {
            **state,
            "goods_to_person_compilation": compilation,
            "cuopt_payload": payload,
            "optimizer_result": result,
        }
    )

    assert not enriched.get("failure_requested", False), enriched
    evidence = enriched["goods_to_person_route_enrichment"]
    execution_payload = enriched["execution_payload"]
    execution_result = enriched["execution_optimizer_result"]
    post_id = f"{batch.batch_id}_RETURN"
    assert evidence.applied and evidence.valid
    assert evidence.batch_robot_assignments[batch.batch_id] == "R002"
    assert post_id in evidence.appended_task_ids
    assert execution_result.routes[0].task_sequence == [
        f"{batch.batch_id}_PICK",
        f"{batch.batch_id}_DROP",
        post_id,
    ]
    post_index = execution_payload.task_data.task_ids.index(post_id)
    assert execution_payload.task_data.fixed_vehicle_ids[post_index] == "R002"
    assert (
        execution_payload.task_data.task_locations[post_index]
        == execution_payload.location_index_map[batch.source_access_node]
    )


def test_execution_enricher_uses_empty_tote_goal_when_quantity_is_depleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, state, update = _compile(monkeypatch, DEPLETED_FIXTURE)
    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    payload = CuOptPayloadBuilder().build(
        request=compiled_request,
        graph_nodes=[str(value["id"]) for value in repository.graph["nodes"]],
        graph_arcs=repository.adjusted_arcs(
            blocked_edge_ids=set(),
            blocked_node_ids=set(),
            edge_penalties={},
        ),
        time_limit_seconds=5,
    )
    batch = compilation.batches[0]
    assert not batch.return_required
    result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R003",
                task_sequence=[f"{batch.batch_id}_PICK", f"{batch.batch_id}_DROP"],
            )
        ],
    )
    enriched = goods_to_person_execution_enricher_node(
        {
            **state,
            "goods_to_person_compilation": compilation,
            "cuopt_payload": payload,
            "optimizer_result": result,
        }
    )

    post_id = f"{batch.batch_id}_EMPTY_TOTE"
    assert post_id in enriched["goods_to_person_route_enrichment"].appended_task_ids
    assert enriched["execution_optimizer_result"].routes[0].task_sequence[-1] == post_id
    post_index = enriched["execution_payload"].task_data.task_ids.index(post_id)
    assert (
        enriched["execution_payload"].task_data.task_locations[post_index]
        == enriched["execution_payload"].location_index_map["EMPTY_TOTE_BUFFER_1_ACCESS"]
    )


def test_common_mapf_consumes_enriched_return_and_emits_station_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.context_service import WarehouseContextService
    from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner

    repository, state, update = _compile(monkeypatch, RETURN_FIXTURE)
    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    bundle = WarehouseContextService(repository).build_map_context()
    payload = CuOptPayloadBuilder().build(
        request=compiled_request,
        graph_nodes=bundle.graph_nodes,
        graph_arcs=bundle.graph_arcs,
        time_limit_seconds=5,
    )
    batch = compilation.batches[0]
    solver_result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R002",
                task_sequence=[f"{batch.batch_id}_PICK", f"{batch.batch_id}_DROP"],
            )
        ],
    )
    enriched = goods_to_person_execution_enricher_node(
        {
            **state,
            "goods_to_person_compilation": compilation,
            "cuopt_payload": payload,
            "optimizer_result": solver_result,
        }
    )
    execution_payload = enriched["execution_payload"]
    execution_result = enriched["execution_optimizer_result"]
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=execution_payload,
        result=execution_result,
        map_context=bundle.context,
        node_types=bundle.graph_node_types,
        g2p_batches=compilation.batches,
    )
    assert expansion.status == "expanded", expansion.errors
    assert schedule.valid, schedule.conflicts
    kinds = [
        step.service_kind
        for route in schedule.routes
        for step in route.steps
        if step.step_type == "SERVICE"
    ]
    assert kinds == ["PICKUP", "STATION", "RETURN"]
    assert len(schedule.station_reservations) == 1
    reservation = schedule.station_reservations[0]
    assert reservation.station_id == batch.station_id
    assert reservation.mobile_robot_id == "R002"
    validation = MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=bundle.context,
        node_types=bundle.graph_node_types,
        payload=execution_payload,
    )
    assert validation.valid, validation.errors

def test_common_mapf_consumes_depleted_empty_tote_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.context_service import WarehouseContextService
    from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner

    repository, state, update = _compile(monkeypatch, DEPLETED_FIXTURE)
    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    bundle = WarehouseContextService(repository).build_map_context()
    payload = CuOptPayloadBuilder().build(
        request=compiled_request,
        graph_nodes=bundle.graph_nodes,
        graph_arcs=bundle.graph_arcs,
        time_limit_seconds=5,
    )
    batch = compilation.batches[0]
    solver_result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R003",
                task_sequence=[f"{batch.batch_id}_PICK", f"{batch.batch_id}_DROP"],
            )
        ],
    )
    enriched = goods_to_person_execution_enricher_node(
        {
            **state,
            "goods_to_person_compilation": compilation,
            "cuopt_payload": payload,
            "optimizer_result": solver_result,
        }
    )
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=enriched["execution_payload"],
        result=enriched["execution_optimizer_result"],
        map_context=bundle.context,
        node_types=bundle.graph_node_types,
        g2p_batches=compilation.batches,
    )
    assert expansion.status == "expanded", expansion.errors
    assert schedule.valid, schedule.conflicts
    kinds = [
        step.service_kind
        for route in schedule.routes
        for step in route.steps
        if step.step_type == "SERVICE"
    ]
    assert kinds == ["PICKUP", "STATION", "EMPTY_TOTE_BUFFER"]
    assert len(schedule.station_reservations) == 1
    validation = MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=bundle.context,
        node_types=bundle.graph_node_types,
        payload=enriched["execution_payload"],
    )
    assert validation.valid, validation.errors


def test_shared_mapf_plans_station_service_and_same_amr_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common MAPF layer, not the compatibility G2P service, owns execution timing."""

    from app.services.context_service import WarehouseContextService
    from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner

    repository, state, update = _compile(monkeypatch, RETURN_FIXTURE)
    compilation = update["goods_to_person_compilation"]
    compiled_request = update["optimization_request"]
    arcs = repository.adjusted_arcs(
        blocked_edge_ids=set(),
        blocked_node_ids=set(),
        edge_penalties={},
    )
    payload = CuOptPayloadBuilder().build(
        request=compiled_request,
        graph_nodes=list(repository.nodes),
        graph_arcs=arcs,
        time_limit_seconds=5,
    )
    batch = compilation.batches[0]
    solver_result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R002",
                task_sequence=[f"{batch.batch_id}_PICK", f"{batch.batch_id}_DROP"],
            )
        ],
    )
    enriched = goods_to_person_execution_enricher_node(
        {
            **state,
            "goods_to_person_compilation": compilation,
            "cuopt_payload": payload,
            "optimizer_result": solver_result,
        }
    )
    map_bundle = WarehouseContextService(repository).build_map_context()
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=enriched["execution_payload"],
        result=enriched["execution_optimizer_result"],
        map_context=map_bundle.context,
        node_types=map_bundle.graph_node_types,
        g2p_batches=compilation.batches,
    )
    validation = MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=map_bundle.context,
        node_types=map_bundle.graph_node_types,
        max_edge_wait_ms=None,
        payload=enriched["execution_payload"],
    )

    assert expansion.status == "expanded", expansion.errors
    assert schedule.valid, schedule.conflicts
    assert validation.valid, validation.errors
    assert len(schedule.station_reservations) == 1
    assert schedule.station_reservations[0].mobile_robot_id == "R002"
    service_steps = [
        step
        for route in schedule.routes
        for step in route.steps
        if step.step_type == "SERVICE"
    ]
    assert any(step.service_kind == "STATION" for step in service_steps)
    assert service_steps[-1].service_kind == "RETURN"
    assert service_steps[-1].node_id == batch.source_access_node


def test_compiler_preserves_non_outbound_tasks_in_mixed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only outbound order rows are replaced; inbound/recovery tasks stay in the common request."""

    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    get_settings.cache_clear()
    set_data_dir(RETURN_FIXTURE)
    try:
        repository = JsonWarehouseRepository(RETURN_FIXTURE)
        request = _request(repository)
        inbound = OptimizationTask(
            task_id="TASK-IN-001",
            pickup_node="IN_HANDOFF_1_ACCESS_A",
            delivery_node="K3_3_ACCESS_A",
            demand=1,
            priority="high",
            operation_type="INBOUND_ITEM",
            item_id="ITEM_SENSOR",
            rack_id="K3_3",
            rack_level=1,
        )
        state = {
            "simulation_id": str(repository.scenario["simulation_id"]),
            "orchestration_plan": _plan(),
            "normalized_request": _normalized(),
            "optimization_request": request.model_copy(
                update={"tasks": [*request.tasks, inbound]}
            ),
            "graph_arcs": repository.adjusted_arcs(
                blocked_edge_ids=set(),
                blocked_node_ids=set(),
                edge_penalties={},
            ),
        }
        update = goods_to_person_compiler_node(state)
        compilation = update["goods_to_person_compilation"]
        compiled = update["optimization_request"]
        assert compilation.applied
        assert "TASK-IN-001" in compilation.preserved_task_ids
        assert [task.task_id for task in compiled.tasks].count("TASK-IN-001") == 1
        assert len([task for task in compiled.tasks if task.operation_type == "G2P_HANDLING_UNIT"]) == 1
    finally:
        set_data_dir(None)
        get_settings.cache_clear()


def test_execution_enricher_keeps_solver_audit_artifacts_immutable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return/empty goals are execution overlays, not mutations of cuOpt input/output evidence."""

    repository, state, update = _compile(monkeypatch, RETURN_FIXTURE)
    compilation = update["goods_to_person_compilation"]
    request = update["optimization_request"]
    payload = CuOptPayloadBuilder().build(
        request=request,
        graph_nodes=[str(value["id"]) for value in repository.graph["nodes"]],
        graph_arcs=repository.adjusted_arcs(
            blocked_edge_ids=set(), blocked_node_ids=set(), edge_penalties={}
        ),
        time_limit_seconds=5,
    )
    batch = compilation.batches[0]
    result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R002",
                task_sequence=[f"{batch.batch_id}_PICK", f"{batch.batch_id}_DROP"],
            )
        ],
    )
    original_task_ids = list(payload.task_data.task_ids)
    original_sequence = list(result.routes[0].task_sequence)
    enriched = goods_to_person_execution_enricher_node(
        {
            **state,
            "goods_to_person_compilation": compilation,
            "cuopt_payload": payload,
            "optimizer_result": result,
        }
    )
    assert payload.task_data.task_ids == original_task_ids
    assert result.routes[0].task_sequence == original_sequence
    assert enriched["execution_payload"].task_data.task_ids != original_task_ids
    assert enriched["execution_optimizer_result"].routes[0].task_sequence != original_sequence

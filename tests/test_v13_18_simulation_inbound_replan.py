from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.domain.schemas import (
    AutoMissionRequest,
    ContextSnapshot,
    EventInput,
    InboundTaskNeed,
    InventoryContext,
    InventoryQueryScope,
    NormalizedOperation,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    OptimizationTask,
    OptimizationVehicle,
    RobotRuntime,
    RobotRuntimeContext,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
)
from app.infrastructure.embedded import EmbeddedPostgresWarehouseAdapter
from app.repositories.json_repository import get_repository, set_data_dir
from app.services.simulation_plan_service import (
    RuntimeExecutionSnapshotBuilder,
    SimulationPlanBuilder,
)

ROOT = Path(__file__).resolve().parents[1]
INBOUND_FIXTURE = ROOT / "scenarios" / "fixtures" / "V18_inbound_putaway"


def _robot_context() -> RobotRuntimeContext:
    return RobotRuntimeContext(
        robots=[
            RobotRuntime(
                robot_id="R001",
                robot_code="R001",
                status="idle",
                battery_pct=95,
                capacity_units=1,
                current_node="R1_0",
            )
        ],
        candidate_robot_ids=["R001"],
        summary="one candidate",
    )


def _inbound_result_view() -> SimpleNamespace:
    normalized = NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id="IN-001",
                operation_type="INBOUND_ITEM",
                source_event_type="inbound_item_arrived",
            )
        ],
        normalization_summary="one inbound receipt",
    )
    inventory = InventoryContext(
        query_scope=InventoryQueryScope(
            mode="inbound_putaway",
            warehouse_id="WH001",
            order_ids=[],
            item_ids=["ITEM_SENSOR"],
            reason="inbound",
        ),
        inventory_summary="inbound receipt loaded",
        inbound_needs=[
            InboundTaskNeed(
                inbound_id="IN-001",
                handling_unit_id="HU-IN-001",
                item_id="ITEM_SENSOR",
                quantity=3,
                source_port_id="I_a",
                target_rack_id=None,
                target_rack_level=None,
            )
        ],
    )
    optimization = OptimizationRequest(
        snapshot_id="SNAP-1",
        tasks=[
            OptimizationTask(
                task_id="IN-001",
                pickup_node="IN_HANDOFF_1_ACCESS_A",
                delivery_node="K3_3_ACCESS_A",
                demand=3,
                priority="medium",
                operation_type="INBOUND_ITEM",
                order_id="IN-001",
                handling_unit_id="HU-IN-001",
                rack_id="K3_3",
                rack_level=1,
            )
        ],
        vehicles=[
            OptimizationVehicle(
                robot_id="R001", start_node="R1_0", capacity_units=4, battery_pct=95
            )
        ],
        map_constraints={},
    )
    schedule = TrafficScheduleResult(
        valid=True,
        routes=[
            TimedRobotRoute(
                robot_id="R001",
                finish_at_ms=5000,
                steps=[
                    TimedRouteStep(
                        step_type="MOVE",
                        start_at_ms=0,
                        end_at_ms=1000,
                        edge_id="E-1",
                        from_node="R1_0",
                        to_node="IN_HANDOFF_1_ACCESS_A",
                    ),
                    TimedRouteStep(
                        step_type="SERVICE",
                        start_at_ms=1000,
                        end_at_ms=2000,
                        node_id="IN_HANDOFF_1_ACCESS_A",
                        task_id="IN-001_PICK",
                        service_kind="PICKUP",
                    ),
                    TimedRouteStep(
                        step_type="MOVE",
                        start_at_ms=2000,
                        end_at_ms=4000,
                        edge_id="E-2",
                        from_node="IN_HANDOFF_1_ACCESS_A",
                        to_node="K3_3_ACCESS_A",
                    ),
                    TimedRouteStep(
                        step_type="SERVICE",
                        start_at_ms=4000,
                        end_at_ms=5000,
                        node_id="K3_3_ACCESS_A",
                        task_id="IN-001_DROP",
                        service_kind="DROP",
                    ),
                ],
            )
        ],
        makespan_ms=5000,
        total_service_ms=2000,
    )
    return SimpleNamespace(
        simulation_id="SIM-INBOUND",
        status="plan_validated",
        traffic_schedule=schedule,
        robot_context=_robot_context(),
        normalized_request=normalized,
        optimization_request=optimization,
        goods_to_person_compilation=None,
        execution_optimizer_result=None,
        optimizer_result=None,
        inventory_context=inventory,
        context_snapshot=ContextSnapshot(
            snapshot_id="SNAP-1",
            captured_at="2026-07-28T00:00:00Z",
            graph_version="MAP-1",
            inventory_version="INV-1",
            runtime_version="RUN-1",
        ),
    )


def test_simulation_plan_builder_projects_real_steps_and_absolute_time() -> None:
    plan = SimulationPlanBuilder().build(
        _inbound_result_view(),
        plan_version=2,
        base_plan_id="PLAN-OLD",
        effective_from_sim_time_ms=10_000,
        plan_kind="REPLAN",
        replan_reason="NEW_ORDER",
        replan_requested_at_ms=9_500,
    )
    assert plan is not None
    assert plan.plan_version == 2
    assert plan.base_plan_id == "PLAN-OLD"
    assert plan.replan_reason == "NEW_ORDER"
    assert plan.robots[0].steps
    assert plan.robots[0].steps[0].start_at_ms == 10_000
    assert plan.robots[0].steps[-1].end_at_ms == 15_000
    assert plan.logical_operations[0].operation_id == "IN-001"
    assert plan.logical_operations[0].source_port_id == "I_a"
    assert plan.logical_operations[0].handling_unit_id == "HU-IN-001"
    assert plan.logical_operations[0].target_rack_id == "K3_3"
    assert plan.logical_operations[0].target_rack_level == 1
    assert plan.logical_operations[0].delivery_node == "K3_3_ACCESS_A"


def test_runtime_snapshot_finishes_current_edge_and_started_commitment() -> None:
    plan = SimulationPlan(
        plan_id="PLAN-A",
        plan_version=1,
        simulation_id="SIM001",
        map_version="MAP-1",
        makespan_ms=6000,
        absolute_finish_at_ms=6000,
        robots=[
            SimulationRobotPlan(
                robot_id="R001",
                initial_node="A",
                finish_at_ms=6000,
                steps=[
                    SimulationPlanStep(
                        step_id="R001-1", sequence=1, step_type="MOVE",
                        start_at_ms=0, end_at_ms=1000, edge_id="E1",
                        from_node="A", to_node="B",
                    ),
                    SimulationPlanStep(
                        step_id="R001-2", sequence=2, step_type="SERVICE",
                        start_at_ms=2000, end_at_ms=3000, node_id="C",
                        task_id="TASK-1_PICK", service_kind="PICKUP",
                    ),
                    SimulationPlanStep(
                        step_id="R001-3", sequence=3, step_type="SERVICE",
                        start_at_ms=5000, end_at_ms=6000, node_id="D",
                        task_id="TASK-1_DROP", service_kind="DROP",
                    ),
                ],
            )
        ],
    )
    snapshot = RuntimeExecutionSnapshotBuilder().build(plan, 2500)
    assert snapshot.earliest_handover_at_ms == 6000
    assert snapshot.latest_handover_at_ms == 6000
    assert snapshot.handover_points[0].node_id == "D"
    assert snapshot.handover_points[0].handover_policy == "CURRENT_OPERATION_END"
    assert snapshot.robot_overrides[0].current_node == "D"
    assert snapshot.robot_overrides[0].sim_time_ms == 6000
    assert "TASK-1" in snapshot.locked_task_bases
    assert not snapshot.completed_task_bases


def test_mixed_request_requires_both_input_forms() -> None:
    request = AutoMissionRequest(
        simulation_id="SIM-MIXED",
        request_mode="mixed",
        events=[EventInput(type="new_order", order_id="ORD-001")],
        user_command="ORD-001은 우선 처리하고 R003은 제외해.",
    )
    assert request.request_mode == "mixed"
    assert request.events[0].order_id == "ORD-001"


def test_embedded_postgres_persists_inbound_receipts(tmp_path: Path) -> None:
    inventory = json.loads((INBOUND_FIXTURE / "rack_inventory.json").read_text(encoding="utf-8"))
    scenario = json.loads((INBOUND_FIXTURE / "scenario_state.json").read_text(encoding="utf-8"))
    facility = json.loads((INBOUND_FIXTURE / "facility_resources.json").read_text(encoding="utf-8"))
    adapter = EmbeddedPostgresWarehouseAdapter(path=tmp_path / "postgres.sqlite3")
    counts = adapter.seed_from_documents(
        inventory=inventory, scenario=scenario, facility=facility, replace=True
    )
    assert counts["inbound_receipts"] == 3
    assert adapter.get_inbound_receipt("IN-002")["item_id"] == "ITEM_BATTERY"
    assert len(adapter.load_inbound_receipts()) == 3


def test_embedded_postgres_accepts_inbound_without_putaway_target(tmp_path: Path) -> None:
    inventory = json.loads((INBOUND_FIXTURE / "rack_inventory.json").read_text(encoding="utf-8"))
    scenario = json.loads((INBOUND_FIXTURE / "scenario_state.json").read_text(encoding="utf-8"))
    facility = json.loads((INBOUND_FIXTURE / "facility_resources.json").read_text(encoding="utf-8"))
    scenario["inbound_receipts"][0]["target_rack_id"] = None
    scenario["inbound_receipts"][0]["target_rack_level"] = None

    adapter = EmbeddedPostgresWarehouseAdapter(path=tmp_path / "postgres.sqlite3")
    adapter.seed_from_documents(
        inventory=inventory,
        scenario=scenario,
        facility=facility,
        replace=True,
    )

    receipt = adapter.get_inbound_receipt(
        scenario["inbound_receipts"][0]["inbound_id"]
    )
    assert receipt is not None
    assert receipt["target_rack_id"] is None
    assert receipt["target_rack_level"] is None


def test_inbound_rule_path_exports_three_solver_tasks() -> None:
    import pytest
    pytest.importorskip("langgraph")
    from app.services.orchestration_service import OrchestrationService

    set_data_dir(INBOUND_FIXTURE)
    try:
        request = AutoMissionRequest(
            simulation_id="SIM-V18-INBOUND",
            request_mode="event_driven",
            optimization_backend="cuopt_payload_only",
            planning_mode="force_rule",
            events=[
                EventInput(type="inbound_item_arrived", inbound_id="IN-001"),
                EventInput(type="inbound_item_arrived", inbound_id="IN-002"),
                EventInput(type="inbound_item_arrived", inbound_id="IN-003"),
            ],
        )
        result = OrchestrationService().run(request, trusted_planning_mode="force_rule")
        assert result.status == "ready_for_cuopt"
        assert result.optimization_request is not None
        assert len(result.optimization_request.tasks) == 3
        assert {value.operation_type for value in result.optimization_request.tasks} == {"INBOUND_ITEM"}
        assert result.cuopt_payload is not None
        assert len(result.cuopt_payload.task_data.pickup_and_delivery_pairs) == 3
    finally:
        set_data_dir(None)
        get_repository.cache_clear()


def test_rolling_horizon_replan_merges_unstarted_work_and_new_event() -> None:
    from app.domain.schemas import ReplanMissionRequest, SimulationLogicalOperation
    from app.services.simulation_plan_service import RollingHorizonReplanService

    active = SimulationPlan(
        plan_id="PLAN-OLD",
        plan_version=1,
        simulation_id="SIM-RH",
        map_version="MAP-1",
        makespan_ms=10_000,
        absolute_finish_at_ms=10_000,
        robots=[
            SimulationRobotPlan(
                robot_id="R001",
                initial_node="A",
                finish_at_ms=10_000,
                steps=[
                    SimulationPlanStep(
                        step_id="R001-1", sequence=1, step_type="MOVE",
                        start_at_ms=0, end_at_ms=1000, edge_id="E1",
                        from_node="A", to_node="B",
                    )
                ],
            )
        ],
        logical_operations=[
            SimulationLogicalOperation(
                operation_id="IN-001",
                operation_type="INBOUND_ITEM",
                task_ids=["IN-001"],
            )
        ],
    )

    class MemoryStore:
        def __init__(self) -> None:
            self.saved: list[SimulationPlan] = []

        def load(self, plan_id: str):
            assert plan_id == "PLAN-OLD"
            return active, None

        def save(self, plan: SimulationPlan, result=None) -> None:
            self.saved.append(plan)

    captured: dict[str, AutoMissionRequest] = {}

    def runner(request: AutoMissionRequest):
        captured["request"] = request
        view = _inbound_result_view()
        horizon = request.runtime_overrides.planning_horizon_start_ms
        shifted_routes = []
        for route in view.traffic_schedule.routes:
            shifted_routes.append(
                route.model_copy(
                    update={
                        "finish_at_ms": route.finish_at_ms + horizon,
                        "steps": [
                            step.model_copy(
                                update={
                                    "start_at_ms": step.start_at_ms + horizon,
                                    "end_at_ms": step.end_at_ms + horizon,
                                }
                            )
                            for step in route.steps
                        ],
                    }
                )
            )
        view.traffic_schedule = view.traffic_schedule.model_copy(
            update={
                "routes": shifted_routes,
                "makespan_ms": view.traffic_schedule.makespan_ms + horizon,
            }
        )
        view.simulation_id = "SIM-RH"
        view.workflow_trace = ["fake_replan"]
        view.pending_human_interaction = None
        view.errors = []
        return view

    store = MemoryStore()
    response = RollingHorizonReplanService(store=store, runner=runner).replan(
        ReplanMissionRequest(
            active_plan_id="PLAN-OLD",
            sim_time_ms=500,
            reason="NEW_ORDER",
            mission=AutoMissionRequest(
                simulation_id="IGNORED",
                request_mode="event_driven",
                events=[EventInput(type="new_order", order_id="ORD-009")],
            ),
        )
    )
    assert response.plan is not None
    assert response.plan.plan_version == 2
    assert response.plan.base_plan_id == "PLAN-OLD"
    assert response.plan.effective_from_sim_time_ms == 500
    assert response.plan.replan_reason == "NEW_ORDER"
    event_ids = {value.order_id or value.inbound_id for value in captured["request"].events}
    assert event_ids == {"ORD-009", "IN-001"}
    assert captured["request"].runtime_overrides.robot_states[0].current_node == "B"
    assert store.saved[0].status == "SUPERSEDED"
    assert store.saved[1].plan_version == 2

from __future__ import annotations

from types import SimpleNamespace

from app.domain.schemas import (
    AutoMissionRequest,
    ContextSnapshot,
    CuOptPayload,
    EventInput,
    FleetData,
    MapConstraints,
    NormalizedOperation,
    NormalizedWarehouseRequest,
    ReplanMissionRequest,
    RobotRuntime,
    RobotRuntimeContext,
    SimulationLogicalOperation,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
    TaskData,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
    WaypointGraphData,
)
from app.services.simulation_plan_service import RollingHorizonReplanService


def test_replan_service_passes_independent_availability_and_builds_absolute_plan() -> None:
    active = SimulationPlan(
        plan_id="PLAN-OLD",
        plan_version=1,
        warehouse_id="WH-001",
        simulation_id="SIM-RH",
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
                        step_id="R001-1", sequence=1, step_type="SERVICE",
                        start_at_ms=1000, end_at_ms=1500, node_id="K1_7_ACCESS_B",
                        task_id="G2P-A_PICK", service_kind="PICKUP",
                    ),
                    SimulationPlanStep(
                        step_id="R001-2", sequence=2, step_type="MOVE",
                        start_at_ms=1500, end_at_ms=4500, edge_id="E1",
                        from_node="K1_7_ACCESS_B", to_node="OUT_STATION_1_ACCESS_A",
                    ),
                    SimulationPlanStep(
                        step_id="R001-3", sequence=3, step_type="SERVICE",
                        start_at_ms=4500, end_at_ms=6000,
                        node_id="OUT_STATION_1_ACCESS_A", task_id="G2P-A_DROP",
                        service_kind="STATION",
                    ),
                ],
            ),
            SimulationRobotPlan(
                robot_id="R002",
                initial_node="X",
                finish_at_ms=4000,
                steps=[
                    SimulationPlanStep(
                        step_id="R002-1", sequence=1, step_type="WAIT",
                        start_at_ms=0, end_at_ms=2000, node_id="X",
                    ),
                    SimulationPlanStep(
                        step_id="R002-2", sequence=2, step_type="MOVE",
                        start_at_ms=2000, end_at_ms=4000, edge_id="E2",
                        from_node="X", to_node="Y",
                    ),
                ],
            ),
            SimulationRobotPlan(
                robot_id="R003",
                initial_node="W",
                finish_at_ms=5000,
                steps=[
                    SimulationPlanStep(
                        step_id="R003-1", sequence=1, step_type="WAIT",
                        start_at_ms=0, end_at_ms=5000, node_id="W",
                    )
                ],
            ),
        ],
        logical_operations=[
            SimulationLogicalOperation(
                operation_id="ORD-OLD",
                operation_type="OUTBOUND_ORDER",
                task_ids=["OLD-UNSTARTED"],
            ),
            SimulationLogicalOperation(
                operation_id="ORD-LOCKED",
                operation_type="OUTBOUND_ORDER",
                task_ids=["G2P-A"],
            ),
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
        overrides = {value.robot_id: value for value in request.runtime_overrides.robot_states}
        payload = CuOptPayload(
            snapshot_id="SNAP-NEW",
            location_index_map={"Y": 0, "Z": 1, "W": 2, "Q": 3},
            fleet_data=FleetData(
                vehicle_ids=["R002", "R003"],
                vehicle_start_locations=[0, 2],
                vehicle_end_locations=[0, 2],
                capacities=[1, 1],
                vehicle_available_at_ms=[
                    overrides["R002"].sim_time_ms,
                    overrides["R003"].sim_time_ms,
                ],
                skip_first_trips=[False, False],
                drop_return_trips=[True, True],
            ),
            task_data=TaskData(
                task_ids=[], task_locations=[], pickup_and_delivery_pairs=[],
                demand=[], priorities=[], service_times_ms=[], fixed_vehicle_ids=[],
            ),
            waypoint_graph_data=WaypointGraphData(
                edge_ids=["E_NEW_1", "E_NEW_2"],
                from_indices=[0, 2], to_indices=[1, 3], costs=[1.0, 1.0],
                travel_times_ms=[1000, 1000],
            ),
            applied_map_constraints=MapConstraints(),
            time_limit_seconds=5,
        )
        return SimpleNamespace(
            warehouse_id="WH-001",
            simulation_id="SIM-RH",
            request_mode=request.request_mode,
            status="plan_validated",
            traffic_schedule=TrafficScheduleResult(
                valid=True,
                makespan_ms=5500,
                routes=[
                    TimedRobotRoute(
                        robot_id="R002", finish_at_ms=5500,
                        steps=[
                            TimedRouteStep(
                                step_type="MOVE", start_at_ms=4000, end_at_ms=5000,
                                edge_id="E_NEW_1", from_node="Y", to_node="Z",
                            ),
                            TimedRouteStep(
                                step_type="SERVICE", start_at_ms=5000, end_at_ms=5500,
                                node_id="Z", task_id="ORD-009_DROP", service_kind="DROP",
                            ),
                        ],
                    ),
                    TimedRobotRoute(
                        robot_id="R003", finish_at_ms=4000,
                        steps=[
                            TimedRouteStep(
                                step_type="MOVE", start_at_ms=2500, end_at_ms=3500,
                                edge_id="E_NEW_2", from_node="W", to_node="Q",
                            ),
                            TimedRouteStep(
                                step_type="SERVICE", start_at_ms=3500, end_at_ms=4000,
                                node_id="Q", task_id="ORD-010_DROP", service_kind="DROP",
                            ),
                        ],
                    ),
                ],
            ),
            robot_context=RobotRuntimeContext(
                robots=[
                    RobotRuntime(
                        robot_id="R002", robot_code="R002", status="idle",
                        battery_pct=90, capacity_units=1, current_node="Y", sim_time_ms=4000,
                    ),
                    RobotRuntime(
                        robot_id="R003", robot_code="R003", status="idle",
                        battery_pct=90, capacity_units=1, current_node="W", sim_time_ms=2500,
                    ),
                ],
                candidate_robot_ids=["R002", "R003"],
                summary="replan candidates",
            ),
            normalized_request=NormalizedWarehouseRequest(
                source="structured_events",
                operations=[
                    NormalizedOperation(
                        operation_id="ORD-009", operation_type="OUTBOUND_ORDER",
                        source_event_type="new_order",
                    )
                ],
                normalization_summary="replan",
            ),
            optimization_request=None,
            goods_to_person_compilation=None,
            execution_optimizer_result=None,
            optimizer_result=None,
            execution_payload=payload,
            cuopt_payload=payload,
            inventory_context=None,
            context_snapshot=ContextSnapshot(
                snapshot_id="SNAP-NEW", captured_at="2026-07-28T00:00:00Z",
                graph_version="MAP-2", inventory_version="INV-2", runtime_version="RUN-2",
            ),
            workflow_trace=["fake_replan"],
            frontend_summary=None,
            pending_human_interaction=None,
            input_rejection=None,
            workflow_hold=None,
            errors=[],
            orchestration_plan=None,
        )

    store = MemoryStore()
    response = RollingHorizonReplanService(store=store, runner=runner).replan(
        ReplanMissionRequest(
            active_plan_id="PLAN-OLD",
            replan_at_sim_time_ms=2500,
            reason="NEW_ORDER",
            mission=AutoMissionRequest(
                warehouse_id="WH-001",
                simulation_id="IGNORED",
                request_mode="event_driven",
                events=[EventInput(type="new_order", order_id="ORD-009")],
            ),
        )
    )

    runtime = captured["request"].runtime_overrides
    availability = {value.robot_id: value.sim_time_ms for value in runtime.robot_states}
    assert availability == {"R001": 6000, "R002": 4000, "R003": 2500}
    assert runtime.preserved_edge_reservations
    event_ids = {value.order_id for value in captured["request"].events if value.order_id}
    assert event_ids == {"ORD-009", "ORD-OLD"}
    assert "ORD-LOCKED" not in event_ids

    assert response.plan is not None
    assert response.plan.plan_version == 2
    assert response.plan.plan_start_sim_time_ms == 2500
    assert response.plan.effective_from_sim_time_ms == 2500
    assert response.plan.absolute_finish_at_ms == 6000
    assert response.plan.makespan_ms == 3500
    robot_by_id = {value.robot_id: value for value in response.plan.robots}
    assert robot_by_id["R002"].available_at_ms == 4000
    assert robot_by_id["R002"].steps[0].start_at_ms == 4000
    assert robot_by_id["R003"].available_at_ms == 2500
    assert robot_by_id["R003"].steps[0].start_at_ms == 2500
    assert store.saved[0].status == "SUPERSEDED"
    assert store.saved[1].plan_version == 2

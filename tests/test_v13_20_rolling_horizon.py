from __future__ import annotations

from app.domain.schemas import (
    CuOptPayload,
    FleetData,
    MapConstraints,
    MapContext,
    OptimizerResult,
    OptimizerRoute,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
    StationServiceReservation,
    TaskData,
    WaypointGraphData,
)
from app.services.mapf_service import PrioritizedSIPPPlanner
from app.services.simulation_plan_service import RuntimeExecutionSnapshotBuilder


def _step(
    robot: str,
    sequence: int,
    step_type: str,
    start: int,
    end: int,
    *,
    edge: str | None = None,
    source: str | None = None,
    target: str | None = None,
    node: str | None = None,
    task: str | None = None,
    service: str | None = None,
) -> SimulationPlanStep:
    return SimulationPlanStep(
        step_id=f"{robot}-{sequence:04d}",
        sequence=sequence,
        step_type=step_type,
        start_at_ms=start,
        end_at_ms=end,
        edge_id=edge,
        from_node=source,
        to_node=target,
        node_id=node,
        task_id=task,
        service_kind=service,
    )


def test_replan_uses_independent_safe_handover_policies_and_preserves_commitments() -> None:
    plan = SimulationPlan(
        plan_id="PLAN-WH-001-SIM-RH-1",
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
                    _step("R001", 1, "MOVE", 0, 1000, edge="E1", source="A", target="K1_7_ACCESS_B"),
                    _step("R001", 2, "SERVICE", 1000, 1500, node="K1_7_ACCESS_B", task="G2P-A_PICK", service="PICKUP"),
                    _step("R001", 3, "MOVE", 1500, 3000, edge="E2", source="K1_7_ACCESS_B", target="OUT_STATION_1_ACCESS_A"),
                    _step("R001", 4, "SERVICE", 3000, 4000, node="OUT_STATION_1_ACCESS_A", task="G2P-A_DROP", service="STATION"),
                    _step("R001", 5, "MOVE", 4000, 5500, edge="E3", source="OUT_STATION_1_ACCESS_A", target="EMPTY_TOTE_BUFFER_1_ACCESS"),
                    _step("R001", 6, "SERVICE", 5500, 6000, node="EMPTY_TOTE_BUFFER_1_ACCESS", task="G2P-A_EMPTY_TOTE", service="EMPTY_TOTE_BUFFER"),
                ],
            ),
            SimulationRobotPlan(
                robot_id="R002",
                initial_node="X",
                finish_at_ms=4000,
                steps=[
                    _step("R002", 1, "WAIT", 0, 2000, node="X"),
                    _step("R002", 2, "MOVE", 2000, 4000, edge="E4", source="X", target="Y"),
                ],
            ),
            SimulationRobotPlan(
                robot_id="R003",
                initial_node="W",
                finish_at_ms=5000,
                steps=[_step("R003", 1, "WAIT", 0, 5000, node="W")],
            ),
            SimulationRobotPlan(
                robot_id="R004",
                initial_node="S",
                finish_at_ms=3500,
                steps=[
                    _step("R004", 1, "WAIT", 0, 2000, node="S"),
                    _step("R004", 2, "SERVICE", 2000, 3500, node="S", task="CHECK-1_DROP", service="DROP"),
                ],
            ),
        ],
        station_reservations=[
            StationServiceReservation(
                reservation_id="STATION-G2P-A",
                station_id="OUT_STATION_1",
                station_robot_id="SR-OUT-01",
                handling_unit_id="HU-A",
                mobile_robot_id="R001",
                start_at_ms=3000,
                end_at_ms=4000,
                processed_quantity=1,
                processing_ticks=1,
            )
        ],
    )

    snapshot = RuntimeExecutionSnapshotBuilder().build(plan, 2500)
    by_robot = {value.robot_id: value for value in snapshot.handover_points}
    assert by_robot["R001"].handover_policy == "CURRENT_OPERATION_END"
    assert by_robot["R001"].handover_at_ms == 6000
    assert by_robot["R001"].node_id == "EMPTY_TOTE_BUFFER_1_ACCESS"
    assert by_robot["R001"].carrying_load
    assert "G2P-A" in snapshot.locked_task_bases

    assert by_robot["R002"].handover_policy == "NEXT_NODE"
    assert by_robot["R002"].handover_at_ms == 4000
    assert by_robot["R002"].node_id == "Y"

    assert by_robot["R003"].handover_policy == "CURRENT_NODE"
    assert by_robot["R003"].handover_at_ms == 2500
    assert by_robot["R003"].node_id == "W"

    assert by_robot["R004"].handover_policy == "CURRENT_SERVICE_END"
    assert by_robot["R004"].handover_at_ms == 3500

    override_by_robot = {value.robot_id: value for value in snapshot.robot_overrides}
    assert override_by_robot["R001"].sim_time_ms == 6000
    assert override_by_robot["R002"].sim_time_ms == 4000
    assert override_by_robot["R003"].sim_time_ms == 2500
    assert override_by_robot["R004"].sim_time_ms == 3500
    assert snapshot.earliest_handover_at_ms == 2500
    assert snapshot.latest_handover_at_ms == 6000

    # R001's committed G2P continuation and R002's current edge remain blocked.
    edge_robots = [value.robot_id for value in snapshot.preserved_edge_reservations]
    assert edge_robots.count("R001") == 2
    assert edge_robots.count("R002") == 1
    assert all(value.robot_id != "R003" for value in snapshot.preserved_node_reservations)
    assert len(snapshot.preserved_station_reservations) == 1
    assert snapshot.preserved_station_reservations[0].start_at_ms == 3000
    assert snapshot.preserved_station_reservations[0].end_at_ms == 4000


def test_mapf_starts_each_robot_at_its_independent_available_time() -> None:
    payload = CuOptPayload(
        snapshot_id="SNAP-AVAIL",
        location_index_map={"S": 0, "P": 1, "D": 2},
        fleet_data=FleetData(
            vehicle_ids=["R001"],
            vehicle_start_locations=[0],
            vehicle_end_locations=[0],
            capacities=[1],
            vehicle_available_at_ms=[5000],
            skip_first_trips=[False],
            drop_return_trips=[True],
        ),
        task_data=TaskData(
            task_ids=["T1_PICK", "T1_DROP"],
            task_locations=[1, 2],
            pickup_and_delivery_pairs=[[0, 1]],
            demand=[1, -1],
            priorities=[0, 0],
            service_times_ms=[500, 500],
            fixed_vehicle_ids=[None, None],
        ),
        waypoint_graph_data=WaypointGraphData(
            edge_ids=["S_P", "P_D"],
            from_indices=[0, 1],
            to_indices=[1, 2],
            costs=[1.0, 1.0],
            travel_times_ms=[1000, 1000],
        ),
        applied_map_constraints=MapConstraints(),
        time_limit_seconds=5,
    )
    result = OptimizerResult(
        backend="ortools",
        status="success",
        optimizer="unit-test",
        routes=[
            OptimizerRoute(
                vehicle_id="R001",
                task_sequence=["T1_PICK", "T1_DROP"],
            )
        ],
    )
    map_context = MapContext(
        graph_version="MAP-1",
        node_count=3,
        edge_count=2,
        map_constraints=MapConstraints(),
        summary="simple graph",
    )
    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=payload,
        result=result,
        map_context=map_context,
        node_types={"S": "route", "P": "route", "D": "route"},
    )
    assert expansion.status == "expanded", expansion.errors
    assert schedule.valid, schedule.conflicts
    assert schedule.routes[0].steps[0].start_at_ms == 5000
    assert schedule.routes[0].finish_at_ms == 8000

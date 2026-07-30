from __future__ import annotations

from app.core.config import get_settings
from app.domain.schemas import (
    CuOptPayload,
    FleetData,
    MapConstraints,
    OptimizationRequest,
    OptimizationVehicle,
    OptimizerResult,
    OptimizerRoute,
    RobotRuntime,
    RobotRuntimeContext,
    RuntimePlanningOverrides,
    TaskData,
    WaypointGraphData,
)
from app.services.terminal_relocation_service import (
    RobotTerminalPolicyService,
    TerminalRelocationEnricher,
)


ARCS = [
    {"edge_id": "E-A-P", "source": "A", "target": "P1", "cost": 1, "travel_time_ms": 1000},
    {"edge_id": "E-A-C", "source": "A", "target": "C01", "cost": 2, "travel_time_ms": 2000},
    {"edge_id": "E-B-P", "source": "B", "target": "P1", "cost": 1, "travel_time_ms": 1000},
    {"edge_id": "E-B-C", "source": "B", "target": "C01", "cost": 2, "travel_time_ms": 2000},
]
NODE_TYPES = {"A": "route", "B": "route", "P1": "route_charge_junction", "C01": "charging_slot"}


def test_terminal_policy_uses_charge_for_low_battery_and_park_for_healthy_robot(monkeypatch) -> None:
    monkeypatch.setenv("IDLE_ROBOT_RELOCATION_ENABLED", "true")
    monkeypatch.setenv("ROBOT_OPPORTUNISTIC_CHARGE_THRESHOLD_PCT", "45")
    monkeypatch.setenv("ROBOT_DEFAULT_TERMINAL_POLICY", "PARK")
    get_settings.cache_clear()
    service = RobotTerminalPolicyService()

    low = RobotRuntime(
        robot_id="R001", robot_code="R001", status="idle", battery_pct=20,
        capacity_units=1, current_node="A", sim_time_ms=3000,
    )
    healthy = RobotRuntime(
        robot_id="R002", robot_code="R002", status="idle", battery_pct=90,
        capacity_units=1, current_node="B", sim_time_ms=3000,
    )

    assert service.policy_for_robot(robot=low, graph_arcs=ARCS, node_types=NODE_TYPES) == ("CHARGE", "C01")
    assert service.policy_for_robot(robot=healthy, graph_arcs=ARCS, node_types=NODE_TYPES) == ("PARK", "P1")


def test_request_terminal_end_is_visible_to_solver_for_old_plan_vehicle(monkeypatch) -> None:
    monkeypatch.setenv("IDLE_ROBOT_RELOCATION_ENABLED", "true")
    monkeypatch.setenv("ROBOT_OPPORTUNISTIC_CHARGE_THRESHOLD_PCT", "45")
    monkeypatch.setenv("ROBOT_DEFAULT_TERMINAL_POLICY", "PARK")
    get_settings.cache_clear()
    request = OptimizationRequest(
        snapshot_id="SNAP",
        tasks=[],
        vehicles=[
            OptimizationVehicle(
                robot_id="R001", start_node="A", capacity_units=1,
                battery_pct=20, available_at_ms=3000,
            ),
            OptimizationVehicle(
                robot_id="R002", start_node="B", capacity_units=1,
                battery_pct=90, available_at_ms=3000,
            ),
        ],
        map_constraints=MapConstraints(),
    )

    result = RobotTerminalPolicyService().apply_to_request(
        request=request,
        runtime_overrides=RuntimePlanningOverrides(
            planning_horizon_start_ms=3000,
            relocate_idle_robot_ids=["R001", "R002"],
        ),
        graph_arcs=ARCS,
        node_types=NODE_TYPES,
    )
    vehicles = {value.robot_id: value for value in result.vehicles}
    assert vehicles["R001"].terminal_policy == "CHARGE"
    assert vehicles["R001"].end_node == "C01"
    assert vehicles["R002"].terminal_policy == "PARK"
    assert vehicles["R002"].end_node == "P1"


def test_unassigned_old_plan_robot_receives_execution_only_relocation(monkeypatch) -> None:
    monkeypatch.setenv("IDLE_ROBOT_RELOCATION_ENABLED", "true")
    monkeypatch.setenv("ROBOT_OPPORTUNISTIC_CHARGE_THRESHOLD_PCT", "45")
    monkeypatch.setenv("ROBOT_DEFAULT_TERMINAL_POLICY", "PARK")
    get_settings.cache_clear()
    locations = {"A": 0, "B": 1, "P1": 2, "C01": 3}
    payload = CuOptPayload(
        snapshot_id="SNAP",
        location_index_map=locations,
        fleet_data=FleetData(
            vehicle_ids=["R002"],
            vehicle_start_locations=[locations["B"]],
            vehicle_end_locations=[locations["B"]],
            capacities=[1],
            vehicle_available_at_ms=[3000],
            skip_first_trips=[False],
            drop_return_trips=[True],
        ),
        task_data=TaskData(
            task_ids=[], task_locations=[], pickup_and_delivery_pairs=[], demand=[],
            priorities=[], service_times_ms=[], fixed_vehicle_ids=[],
        ),
        waypoint_graph_data=WaypointGraphData(
            edge_ids=[value["edge_id"] for value in ARCS],
            from_indices=[locations[value["source"]] for value in ARCS],
            to_indices=[locations[value["target"]] for value in ARCS],
            costs=[float(value["cost"]) for value in ARCS],
            travel_times_ms=[int(value["travel_time_ms"]) for value in ARCS],
        ),
        applied_map_constraints=MapConstraints(),
        time_limit_seconds=5,
    )
    result = OptimizerResult(
        backend="ortools", status="success", optimizer="test",
        routes=[OptimizerRoute(vehicle_id="R002", task_sequence=[])],
    )
    request = OptimizationRequest(
        snapshot_id="SNAP", tasks=[],
        vehicles=[
            OptimizationVehicle(
                robot_id="R002", start_node="B", end_node="P1",
                terminal_policy="PARK", capacity_units=1, battery_pct=90,
                available_at_ms=3000,
            )
        ],
        map_constraints=MapConstraints(),
    )
    robots = RobotRuntimeContext(
        robots=[
            RobotRuntime(
                robot_id="R001", robot_code="R001", status="idle",
                battery_pct=20, capacity_units=1, current_node="A", sim_time_ms=3000,
            ),
            RobotRuntime(
                robot_id="R002", robot_code="R002", status="idle",
                battery_pct=90, capacity_units=1, current_node="B", sim_time_ms=3000,
            ),
        ],
        candidate_robot_ids=["R001", "R002"],
        summary="terminal relocation",
    )

    execution_payload, execution_result, relocation = TerminalRelocationEnricher().enrich(
        payload=payload,
        result=result,
        request=request,
        robot_context=robots,
        runtime_overrides=RuntimePlanningOverrides(
            planning_horizon_start_ms=3000,
            relocate_idle_robot_ids=["R001"],
        ),
        graph_arcs=ARCS,
        node_types=NODE_TYPES,
    )

    assert relocation.valid and relocation.applied
    assert relocation.relocations[0].robot_id == "R001"
    assert relocation.relocations[0].policy == "CHARGE"
    assert relocation.relocations[0].execution_only is True
    assert "R001" in execution_payload.fleet_data.vehicle_ids
    assert any(value.vehicle_id == "R001" for value in execution_result.routes)
    assert any(task.startswith("TERMINAL-R001-CHARGE") for task in execution_payload.task_data.task_ids)

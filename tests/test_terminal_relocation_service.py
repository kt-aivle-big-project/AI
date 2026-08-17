from __future__ import annotations

from app.domain.schemas import (
    CuOptPayload,
    FleetData,
    MapConstraints,
    OptimizationRequest,
    OptimizerResult,
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


def _arcs() -> list[dict]:
    return [
        {"edge_id": "E-S-C01", "source": "S", "target": "C01", "cost": 1.0, "travel_time_ms": 1_000},
        {"edge_id": "E-S-C02", "source": "S", "target": "C02", "cost": 5.0, "travel_time_ms": 5_000},
        {"edge_id": "E-T-C01", "source": "T", "target": "C01", "cost": 5.0, "travel_time_ms": 5_000},
        {"edge_id": "E-T-C02", "source": "T", "target": "C02", "cost": 1.0, "travel_time_ms": 1_000},
    ]


def _robot(robot_id: str, current_node: str, home_node: str) -> RobotRuntime:
    return RobotRuntime(
        robot_id=robot_id,
        robot_code=robot_id,
        status="low_battery",
        battery_pct=20,
        capacity_units=1,
        current_node=current_node,
        home_node=home_node,
    )


def _empty_payload() -> CuOptPayload:
    locations = {"S": 0, "T": 1, "C01": 2, "C02": 3}
    return CuOptPayload(
        snapshot_id="SNAP-1",
        location_index_map=locations,
        fleet_data=FleetData(vehicle_ids=[], vehicle_start_locations=[], capacities=[]),
        task_data=TaskData(
            task_ids=[],
            task_locations=[],
            pickup_and_delivery_pairs=[],
            demand=[],
            priorities=[],
            fixed_vehicle_ids=[],
        ),
        waypoint_graph_data=WaypointGraphData(
            edge_ids=[value["edge_id"] for value in _arcs()],
            from_indices=[locations[value["source"]] for value in _arcs()],
            to_indices=[locations[value["target"]] for value in _arcs()],
            costs=[value["cost"] for value in _arcs()],
            travel_times_ms=[value["travel_time_ms"] for value in _arcs()],
        ),
        applied_map_constraints=MapConstraints(),
    )


def test_low_battery_policy_prefers_dedicated_home_over_nearest_charger() -> None:
    policy, target = RobotTerminalPolicyService().policy_for_robot(
        robot=_robot("R001", "S", "C02"),
        graph_arcs=_arcs(),
        node_types={"S": "route", "C01": "charging_slot", "C02": "charging_slot"},
    )

    assert policy == "CHARGE"
    assert target == "C02"


def test_relocation_assigns_each_low_battery_robot_to_its_own_charger() -> None:
    robots = [_robot("R001", "S", "C02"), _robot("R002", "T", "C01")]
    payload, result, relocation = TerminalRelocationEnricher().enrich(
        payload=_empty_payload(),
        result=OptimizerResult(
            backend="cuopt",
            status="success",
            optimizer="cuopt",
        ),
        request=OptimizationRequest(
            snapshot_id="SNAP-1",
            tasks=[],
            vehicles=[],
            map_constraints=MapConstraints(),
        ),
        robot_context=RobotRuntimeContext(
            robots=robots,
            candidate_robot_ids=[],
            summary="two low-battery robots",
        ),
        runtime_overrides=RuntimePlanningOverrides(
            relocate_idle_robot_ids=["R001", "R002"]
        ),
        graph_arcs=_arcs(),
        node_types={
            "S": "route",
            "T": "route",
            "C01": "charging_slot",
            "C02": "charging_slot",
        },
    )

    assert relocation.valid is True
    assert {
        record.robot_id: record.to_node for record in relocation.relocations
    } == {"R001": "C02", "R002": "C01"}
    assert len({record.to_node for record in relocation.relocations}) == 2
    assert set(payload.task_data.task_ids) == {
        "TERMINAL-R001-CHARGE",
        "TERMINAL-R002-CHARGE",
    }
    assert {route.vehicle_id for route in result.routes} == {"R001", "R002"}


def test_robot_already_at_home_still_receives_charge_service() -> None:
    robot = _robot("R001", "C01", "C01")
    payload, _, relocation = TerminalRelocationEnricher().enrich(
        payload=_empty_payload(),
        result=OptimizerResult(backend="cuopt", status="success", optimizer="cuopt"),
        request=OptimizationRequest(
            snapshot_id="SNAP-1", tasks=[], vehicles=[], map_constraints=MapConstraints()
        ),
        robot_context=RobotRuntimeContext(
            robots=[robot], candidate_robot_ids=[], summary="already at charger"
        ),
        runtime_overrides=RuntimePlanningOverrides(relocate_idle_robot_ids=["R001"]),
        graph_arcs=_arcs(),
        node_types={"C01": "charging_slot", "C02": "charging_slot"},
    )

    assert relocation.valid is True
    assert [value.task_id for value in relocation.relocations] == [
        "TERMINAL-R001-CHARGE"
    ]
    assert payload.task_data.task_locations == [payload.location_index_map["C01"]]

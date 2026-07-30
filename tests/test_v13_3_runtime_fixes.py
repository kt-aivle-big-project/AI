"""Regression coverage for the v13.3 cuOpt and Windows-console fixes."""
from __future__ import annotations

from app.core.console import safe_console_print
from app.domain.schemas import (
    CuOptPayload,
    FleetData,
    MapConstraints,
    TaskData,
    WaypointGraphData,
)
from app.services.optimization_service import (
    CuOptPayloadValidator,
    OPTIONAL_TASK_PENALTY_BY_PRIORITY,
)


def _payload(*, drop_return: bool) -> CuOptPayload:
    """Create S -> P -> D with no directed D -> S return path."""

    return CuOptPayload(
        snapshot_id="SNAP-OPEN-ROUTE",
        location_index_map={"S": 0, "P": 1, "D": 2},
        fleet_data=FleetData(
            vehicle_ids=["R001"],
            vehicle_start_locations=[0],
            vehicle_end_locations=[0],
            capacities=[10],
            skip_first_trips=[False],
            drop_return_trips=[drop_return],
        ),
        task_data=TaskData(
            task_ids=["T1_PICK", "T1_DROP"],
            task_locations=[1, 2],
            pickup_and_delivery_pairs=[[0, 1]],
            demand=[3, -3],
            priorities=[0, 0],
            service_times_ms=[1750, 1600],
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


def test_open_route_does_not_require_delivery_to_start_return_path() -> None:
    result = CuOptPayloadValidator().validate(_payload(drop_return=True))
    assert result.valid, result.errors
    assert any("Open-route policy" in warning for warning in result.warnings)


def test_closed_route_rejects_missing_delivery_to_vehicle_end_path() -> None:
    result = CuOptPayloadValidator().validate(_payload(drop_return=False))
    assert not result.valid
    assert any("configured vehicle end" in error for error in result.errors)


def test_mandatory_pair_requires_at_least_one_reachable_vehicle_not_all_vehicles() -> None:
    payload = _payload(drop_return=True)
    payload = payload.model_copy(
        update={
            "fleet_data": FleetData(
                vehicle_ids=["R_BAD", "R_GOOD"],
                vehicle_start_locations=[2, 0],
                vehicle_end_locations=[2, 0],
                capacities=[10, 10],
                skip_first_trips=[False, False],
                drop_return_trips=[True, True],
            )
        }
    )
    result = CuOptPayloadValidator().validate(payload)
    assert result.valid, result.errors


class _Cp949Stream:
    encoding = "cp949"

    def __init__(self) -> None:
        self.values: list[str] = []

    def write(self, value: str) -> int:
        value.encode(self.encoding)
        self.values.append(value)
        return len(value)

    def flush(self) -> None:
        return None


def test_console_logging_never_crashes_on_em_dash_under_cp949() -> None:
    stream = _Cp949Stream()
    safe_console_print("frontend explanation — completed", stream=stream)  # type: ignore[arg-type]
    rendered = "".join(stream.values)
    assert "\\u2014" in rendered


def test_malformed_fleet_arrays_report_validation_error_instead_of_crashing() -> None:
    payload = _payload(drop_return=True)
    malformed = payload.model_copy(
        update={
            "fleet_data": payload.fleet_data.model_copy(
                update={"capacities": []}
            )
        }
    )
    result = CuOptPayloadValidator().validate(malformed)
    assert not result.valid
    assert any("arrays must have equal lengths" in error for error in result.errors)


def test_optional_task_penalty_preserves_lower_number_higher_priority_contract() -> None:
    """High priority (0) must be harder to drop than medium (1) and low (2)."""

    assert (
        OPTIONAL_TASK_PENALTY_BY_PRIORITY[0]
        > OPTIONAL_TASK_PENALTY_BY_PRIORITY[1]
        > OPTIONAL_TASK_PENALTY_BY_PRIORITY[2]
    )

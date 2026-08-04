"""Deterministic traffic scheduling with edge reservations and WAIT insertion."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.domain.schemas import (
    EdgeOccupancy,
    EdgeReservation,
    MapContext,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
)
from app.services.edge_calendar import EdgeCalendar


@dataclass(frozen=True)
class _Interval:
    """Internal edge-use calendar interval."""

    start: int
    end: int
    robot_id: str


class TrafficManagerService:
    """Build traffic-safe schedules from validated waypoint routes.

    This is the planning component of a traffic manager. A continuously running
    WCS would later commit reservations and release MOVE segments in real time.
    """

    SAFE_WAIT_NODE_TYPES = {
        "route",
        "route_charge_junction",
        "inbound",
        "outbound",
        "charging_slot",
        "rack_access",
        "inbound_handoff_access",
        "outbound_station_access",
        "empty_tote_buffer_access",
    }

    def schedule(
        self,
        *,
        expansion: WaypointRouteExpansionResult,
        map_context: MapContext,
        node_types: dict[str, str],
    ) -> TrafficScheduleResult:
        """Insert WAIT steps and create millisecond edge reservations."""

        if expansion.status != "expanded":
            return TrafficScheduleResult(valid=False, conflicts=[*expansion.errors])
        calendar = EdgeCalendar.from_map_context(map_context)
        routes: list[TimedRobotRoute] = []
        reservations: list[EdgeReservation] = []
        conflicts: list[str] = []
        warnings: list[str] = []
        for route in expansion.routes:
            current_time = 0
            steps: list[TimedRouteStep] = []
            for segment in route.segments:
                slot = calendar.earliest_slot(
                    edge_id=segment.edge_id,
                    earliest=current_time,
                    duration=segment.travel_time_ms,
                )
                if slot > current_time:
                    if node_types.get(segment.from_node) not in self.SAFE_WAIT_NODE_TYPES:
                        conflicts.append(
                            f"{route.vehicle_id} cannot wait safely at {segment.from_node} before {segment.edge_id}."
                        )
                        break
                    steps.append(
                        TimedRouteStep(
                            step_type="WAIT",
                            node_id=segment.from_node,
                            start_at_ms=current_time,
                            end_at_ms=slot,
                            reason=f"{segment.edge_id} is occupied or reserved.",
                        )
                    )
                    warnings.append(
                        f"{route.vehicle_id} waits {slot-current_time} ms at {segment.from_node} before {segment.edge_id}."
                    )
                end = slot + segment.travel_time_ms
                steps.append(
                    TimedRouteStep(
                        step_type="MOVE",
                        edge_id=segment.edge_id,
                        from_node=segment.from_node,
                        to_node=segment.to_node,
                        start_at_ms=slot,
                        end_at_ms=end,
                    )
                )
                reservation = EdgeReservation(
                    reservation_id=f"RES-{route.vehicle_id}-{segment.sequence:04d}",
                    edge_id=segment.edge_id,
                    robot_id=route.vehicle_id,
                    direction=f"{segment.from_node}_TO_{segment.to_node}",
                    start_at_ms=slot,
                    end_at_ms=end,
                )
                reservations.append(reservation)
                calendar.reserve(
                    edge_id=segment.edge_id, start=slot, end=end, robot_id=route.vehicle_id
                )
                current_time = end
            routes.append(TimedRobotRoute(robot_id=route.vehicle_id, steps=steps, finish_at_ms=current_time))
        return TrafficScheduleResult(
            valid=not conflicts,
            routes=routes,
            reservations=reservations,
            conflicts=conflicts,
            warnings=warnings,
        )


class TrafficScheduleValidator:
    """Validate generated reservations and waiting locations independently."""

    def validate(
        self,
        *,
        schedule: TrafficScheduleResult,
        map_context: MapContext,
        node_types: dict[str, str],
    ) -> TrafficScheduleResult:
        """Reject overlapping capacity-one reservations and unsafe waits."""

        conflicts = [*schedule.conflicts]
        warnings = [*schedule.warnings]
        by_edge: dict[str, list[_Interval]] = defaultdict(list)
        for occupancy in map_context.map_constraints.edge_occupancies:
            by_edge[occupancy.edge_id].append(
                _Interval(occupancy.occupied_from_ms, occupancy.occupied_until_ms, occupancy.robot_id)
            )
        for existing in map_context.map_constraints.edge_reservations:
            by_edge[existing.edge_id].append(
                _Interval(existing.start_at_ms, existing.end_at_ms, existing.robot_id)
            )
        for reservation in schedule.reservations:
            by_edge[reservation.edge_id].append(
                _Interval(reservation.start_at_ms, reservation.end_at_ms, reservation.robot_id)
            )
        for edge_id, intervals in by_edge.items():
            ordered = sorted(intervals, key=lambda value: (value.start, value.end))
            for first, second in zip(ordered, ordered[1:]):
                if first.end > second.start and first.robot_id != second.robot_id:
                    conflicts.append(
                        f"Edge {edge_id} overlaps for {first.robot_id} and {second.robot_id}: "
                        f"{first.start}-{first.end} vs {second.start}-{second.end}."
                    )
        for route in schedule.routes:
            previous_end = 0
            for step in route.steps:
                if step.start_at_ms < previous_end:
                    conflicts.append(f"{route.robot_id} has non-monotonic timed steps.")
                if step.step_type == "WAIT":
                    if step.node_id is None or node_types.get(step.node_id) not in TrafficManagerService.SAFE_WAIT_NODE_TYPES:
                        conflicts.append(f"{route.robot_id} waits at an unsafe node {step.node_id}.")
                previous_end = step.end_at_ms
        return schedule.model_copy(update={"valid": not conflicts, "conflicts": conflicts, "warnings": warnings})

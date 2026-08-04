"""Shared edge-use calendar for slot search across evaluation and scheduling.

This module is the single source of truth for "when can a robot enter this
edge for this duration". Both the traffic-aware option evaluator and the
traffic manager must use it so that an option's evaluated finish time and the
final schedule's finish time can never drift apart.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass

from app.core.config import get_settings
from app.domain.schemas import MapContext


@dataclass(frozen=True, order=True)
class EdgeInterval:
    """One committed edge-use interval on the calendar."""

    start: int
    end: int
    robot_id: str


class EdgeCalendar:
    """Sorted per-edge interval calendar with headway-aware slot search."""

    def __init__(self) -> None:
        """Create an empty calendar; headway is read once from settings."""

        self._intervals: dict[str, list[EdgeInterval]] = defaultdict(list)
        self.headway_ms = get_settings().traffic_safety_headway_ms

    @classmethod
    def from_map_context(
        cls,
        map_context: MapContext,
        *,
        edge_resource_map: dict[str, str] | None = None,
    ) -> "EdgeCalendar":
        """Build a calendar from current occupancies and existing reservations.

        ``edge_resource_map`` lets opposite directed arcs share one physical
        corridor calendar.  Callers that do not provide it keep the legacy
        edge-id calendar semantics.
        """

        resources = edge_resource_map or {}
        calendar = cls()
        for occupancy in map_context.map_constraints.edge_occupancies:
            calendar.reserve(
                edge_id=resources.get(occupancy.edge_id, occupancy.edge_id),
                start=occupancy.occupied_from_ms,
                end=occupancy.occupied_until_ms,
                robot_id=occupancy.robot_id,
            )
        for reservation in map_context.map_constraints.edge_reservations:
            calendar.reserve(
                edge_id=(
                    reservation.physical_resource_id
                    or resources.get(reservation.edge_id, reservation.edge_id)
                ),
                start=reservation.start_at_ms,
                end=reservation.end_at_ms,
                robot_id=reservation.robot_id,
            )
        return calendar

    def intervals(self, edge_id: str) -> list[EdgeInterval]:
        """Return the sorted committed intervals for one edge."""

        return list(self._intervals.get(edge_id, []))

    def reserve(self, *, edge_id: str, start: int, end: int, robot_id: str) -> None:
        """Commit one interval, keeping the per-edge list sorted."""

        if start < 0 or end <= start:
            raise ValueError(f"Invalid edge interval {edge_id}: {start}-{end}")
        bisect.insort(self._intervals[edge_id], EdgeInterval(start, end, robot_id))

    def clone(self) -> "EdgeCalendar":
        """Return a deep copy suitable for transactional route planning."""

        value = EdgeCalendar()
        value.headway_ms = self.headway_ms
        for edge_id, intervals in self._intervals.items():
            value._intervals[edge_id] = list(intervals)
        return value

    def replace_with(self, other: "EdgeCalendar") -> None:
        """Atomically replace this calendar with a successfully planned clone."""

        self._intervals = defaultdict(list, {key: list(values) for key, values in other._intervals.items()})

    def earliest_slot(
        self,
        *,
        edge_id: str,
        earliest: int,
        duration: int,
        ignore_robot_id: str | None = None,
    ) -> int:
        """Return the earliest start >= earliest whose [start, start+duration)
        does not overlap any committed interval, respecting safety headway.

        This function is monotone in `earliest`, which is what makes the
        time-dependent Dijkstra in the option evaluator admissible.
        """

        candidate = earliest
        for interval in self._intervals.get(edge_id, []):
            if ignore_robot_id is not None and interval.robot_id == ignore_robot_id:
                continue
            candidate_end = candidate + duration
            # A new traversal may fit before a future reservation only when
            # the configured safety headway also fits in the gap.
            if candidate_end + self.headway_ms <= interval.start:
                break
            if candidate < interval.end + self.headway_ms and candidate_end + self.headway_ms > interval.start:
                candidate = interval.end + self.headway_ms
        return candidate

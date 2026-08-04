"""Prioritized multi-goal MAPF planning for ordered solver task sequences.

The v9 MAPF layer is deliberately pragmatic.  Each robot is planned against
shared physical-corridor and node calendars using a safe-interval,
time-dependent shortest-path search.  A complete route is prepared on cloned
calendars and committed only after every MOVE, WAIT, and SERVICE interval is
feasible.  This is prioritized SIPP-style planning, not optimal CBS and not an
online MAPD task allocator.
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from math import inf

from app.core.config import get_settings
from app.domain.schemas import (
    CuOptPayload,
    EdgeReservation,
    ExpandedRobotRoute,
    HandlingUnitBatchPlan,
    MAPFValidationResult,
    MapContext,
    NodeReservation,
    OptimizerResult,
    RouteSegment,
    StationServiceReservation,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
)
from app.services.edge_calendar import EdgeCalendar
from app.services.graph_service import Arc, DirectedGraphService
from app.services.traffic_manager import TrafficManagerService


@dataclass(frozen=True)
class _NodeInterval:
    """One node-use interval used by the independent validator."""

    start: int
    end: int
    robot_id: str


@dataclass(frozen=True)
class _TimedArc:
    """One selected graph arc with its exact safe departure and arrival."""

    arc: Arc
    depart_at_ms: int
    arrive_at_ms: int


@dataclass(frozen=True)
class _TimedService:
    """One pickup/drop service action at an ordered task goal."""

    task_id: str
    node_id: str
    start_at_ms: int
    end_at_ms: int


@dataclass
class _PlannedRoute:
    """Transactional result for one robot before shared-calendar commit."""

    steps: list[TimedRouteStep]
    reservations: list[EdgeReservation]
    segments: list[RouteSegment]
    node_sequence: list[str]
    finish_at_ms: int
    edge_calendar: EdgeCalendar


class PrioritizedSIPPPlanner:
    """Build collision-aware timed paths for solver-assigned ordered goals."""

    SEARCH_HORIZON_MS = 24 * 60 * 60 * 1000

    def plan(
        self,
        *,
        payload: CuOptPayload,
        result: OptimizerResult,
        map_context: MapContext,
        node_types: dict[str, str],
        g2p_batches: list[HandlingUnitBatchPlan] | None = None,
        preserved_node_reservations: list[NodeReservation] | None = None,
        preserved_station_reservations: list[StationServiceReservation] | None = None,
    ) -> tuple[WaypointRouteExpansionResult, TrafficScheduleResult]:
        """Return static expansions and timed paths using shared calendars."""

        if result.status != "success":
            error = result.reason or result.status
            return (
                WaypointRouteExpansionResult(status="failed", errors=[error]),
                TrafficScheduleResult(valid=False, conflicts=[error]),
            )

        reverse_index = {index: node_id for node_id, index in payload.location_index_map.items()}
        graph = DirectedGraphService(
            [
                {
                    "edge_id": edge_id,
                    "source": reverse_index[source],
                    "target": reverse_index[target],
                    "cost": cost,
                    "travel_time_ms": travel,
                }
                for edge_id, source, target, cost, travel in zip(
                    payload.waypoint_graph_data.edge_ids,
                    payload.waypoint_graph_data.from_indices,
                    payload.waypoint_graph_data.to_indices,
                    payload.waypoint_graph_data.costs,
                    payload.waypoint_graph_data.travel_times_ms,
                    strict=True,
                )
            ]
        )
        starts = {
            robot_id: reverse_index[start]
            for robot_id, start in zip(
                payload.fleet_data.vehicle_ids,
                payload.fleet_data.vehicle_start_locations,
                strict=True,
            )
        }
        available_at_by_robot = {
            robot_id: int(available_at_ms)
            for robot_id, available_at_ms in zip(
                payload.fleet_data.vehicle_ids,
                payload.fleet_data.vehicle_available_at_ms
                or [0 for _ in payload.fleet_data.vehicle_ids],
                strict=True,
            )
        }
        task_location = {
            task_id: reverse_index[location]
            for task_id, location in zip(
                payload.task_data.task_ids,
                payload.task_data.task_locations,
                strict=True,
            )
        }
        service_times = list(payload.task_data.service_times_ms)
        if not service_times:
            settings = get_settings()
            service_times = [
                (
                    settings.pickup_service_time_ms
                    if task_id.endswith("_PICK")
                    else settings.drop_service_time_ms
                )
                for task_id in payload.task_data.task_ids
            ]
        task_service_time = dict(
            zip(payload.task_data.task_ids, service_times, strict=True)
        )
        task_priority = dict(zip(payload.task_data.task_ids, payload.task_data.priorities, strict=True))
        ordered_routes = sorted(
            result.routes,
            key=lambda route: (
                0
                if (
                    route.task_sequence
                    and starts.get(route.vehicle_id)
                    == task_location.get(route.task_sequence[0])
                )
                else 1,
                min((task_priority.get(task_id, 255) for task_id in route.task_sequence), default=255),
                route.vehicle_id,
            ),
        )

        edge_resource_map = {
            arc.edge_id: self._resource_id(arc.source, arc.target)
            for arcs in graph.by_source.values()
            for arc in arcs
        }
        edge_calendar = EdgeCalendar.from_map_context(
            map_context,
            edge_resource_map=edge_resource_map,
        )
        node_calendar = EdgeCalendar()
        for reservation in list(preserved_node_reservations or []):
            node_calendar.reserve(
                edge_id=f"NODE:{reservation.node_id}",
                start=reservation.start_at_ms,
                end=reservation.end_at_ms,
                robot_id=reservation.robot_id,
            )
        expanded_routes: list[ExpandedRobotRoute] = []
        timed_routes: list[TimedRobotRoute] = []
        reservations: list[EdgeReservation] = []
        conflicts: list[str] = []
        warnings: list[str] = []
        station_reservations: list[StationServiceReservation] = list(
            preserved_station_reservations or []
        )
        station_calendar = EdgeCalendar()
        for reservation in station_reservations:
            station_calendar.reserve(
                edge_id=self._station_resource_id(reservation.station_id),
                start=reservation.start_at_ms,
                end=reservation.end_at_ms,
                robot_id=reservation.mobile_robot_id,
            )
        settings = get_settings()
        batches = list(g2p_batches or [])
        station_task_by_id = {
            f"{batch.batch_id}_DROP": batch for batch in batches
        }
        task_station_constraints = {
            task_id: (
                self._station_resource_id(batch.station_id),
                min(
                    max(1, task_service_time.get(task_id, batch.station_receive_time_ms)),
                    max(1, batch.station_receive_time_ms),
                ),
            )
            for task_id, batch in station_task_by_id.items()
        }

        for optimizer_route in ordered_routes:
            robot_id = optimizer_route.vehicle_id
            if robot_id not in starts:
                conflicts.append(f"MAPF received unknown robot {robot_id}.")
                continue

            working_edges = edge_calendar.clone()
            planned, route_error = self._plan_robot_route(
                graph=graph,
                robot_id=robot_id,
                start_node=starts[robot_id],
                start_at_ms=available_at_by_robot.get(robot_id, 0),
                task_sequence=list(optimizer_route.task_sequence),
                task_location=task_location,
                task_service_time=task_service_time,
                edge_calendar=working_edges,
                node_calendar=node_calendar,
                station_calendar=station_calendar,
                task_station_constraints=task_station_constraints,
                node_types=node_types,
            )
            if planned is None:
                conflicts.append(route_error or f"No prioritized SIPP plan was found for {robot_id}.")
                continue

            edge_calendar.replace_with(planned.edge_calendar)
            for node_id, start, end in self._node_intervals(
                steps=planned.steps,
                headway_ms=max(1, settings.traffic_safety_headway_ms),
            ):
                node_calendar.reserve(
                    edge_id=f"NODE:{node_id}",
                    start=start,
                    end=end,
                    robot_id=robot_id,
                )
            for step in planned.steps:
                if step.step_type != "SERVICE" or step.task_id not in station_task_by_id:
                    continue
                batch = station_task_by_id[step.task_id]
                # The fixed station is a two-stage pipeline.  Its input handoff
                # port is exclusive only while the AMR transfers the box to
                # the blue robot.  Sorting/release to the green chute continues
                # independently, so another AMR may begin the next handoff.
                handoff_end_at_ms = min(
                    step.end_at_ms,
                    step.start_at_ms + max(1, batch.station_receive_time_ms),
                )
                station_reservations.append(
                    StationServiceReservation(
                        reservation_id=f"STATION-{batch.batch_id}",
                        station_id=batch.station_id,
                        station_robot_id=batch.station_robot_id,
                        handling_unit_id=batch.handling_unit_id,
                        mobile_robot_id=robot_id,
                        start_at_ms=step.start_at_ms,
                        end_at_ms=handoff_end_at_ms,
                        processed_quantity=batch.requested_quantity,
                        processing_ticks=batch.station_processing_ticks,
                    )
                )
                station_calendar.reserve(
                    edge_id=self._station_resource_id(batch.station_id),
                    start=step.start_at_ms,
                    end=handoff_end_at_ms,
                    robot_id=robot_id,
                )
                # Only the input handoff stage owns the fixed access resource.
                # The outgoing conveyor stage deliberately does not block the
                # next AMR handoff.
                # Reserve only the AMR port actually selected for this batch.
                # The fixed robot reservation above serializes its receive
                # action; the other three boundary nodes remain available as
                # approach/waiting positions for following AMRs.
                selected_access_node = (
                    batch.mobile_handoff_node or batch.station_access_node
                )
                node_calendar.reserve(
                    edge_id=f"NODE:{selected_access_node}",
                    start=step.start_at_ms,
                    end=handoff_end_at_ms,
                    robot_id=robot_id,
                )
            reservations.extend(planned.reservations)
            wait_ms = sum(
                step.end_at_ms - step.start_at_ms
                for step in planned.steps
                if step.step_type == "WAIT"
            )
            if wait_ms:
                warnings.append(f"{robot_id} accumulates {wait_ms} ms of MAPF wait.")
            expanded_routes.append(
                ExpandedRobotRoute(
                    vehicle_id=robot_id,
                    start_node=starts[robot_id],
                    task_sequence=list(optimizer_route.task_sequence),
                    node_sequence=planned.node_sequence,
                    segments=planned.segments,
                    total_cost=round(sum(value.cost for value in planned.segments), 6),
                    total_travel_time_ms=sum(value.travel_time_ms for value in planned.segments),
                )
            )
            timed_routes.append(
                TimedRobotRoute(
                    robot_id=robot_id,
                    steps=planned.steps,
                    finish_at_ms=planned.finish_at_ms,
                )
            )

        total_wait = sum(
            step.end_at_ms - step.start_at_ms
            for route in timed_routes
            for step in route.steps
            if step.step_type == "WAIT"
        )
        total_service = sum(
            step.end_at_ms - step.start_at_ms
            for route in timed_routes
            for step in route.steps
            if step.step_type == "SERVICE"
        )
        makespan = max((route.finish_at_ms for route in timed_routes), default=0)
        expansion = WaypointRouteExpansionResult(
            status="failed" if conflicts else "expanded",
            routes=expanded_routes,
            errors=list(conflicts),
        )
        schedule = TrafficScheduleResult(
            valid=not conflicts,
            planner="prioritized_sipp",
            routes=timed_routes,
            reservations=reservations,
            station_reservations=station_reservations,
            conflicts=conflicts,
            warnings=warnings,
            total_wait_ms=total_wait,
            total_service_ms=total_service,
            makespan_ms=makespan,
        )
        return expansion, schedule

    def _plan_robot_route(
        self,
        *,
        graph: DirectedGraphService,
        robot_id: str,
        start_node: str,
        task_sequence: list[str],
        task_location: dict[str, str],
        task_service_time: dict[str, int],
        edge_calendar: EdgeCalendar,
        node_calendar: EdgeCalendar,
        station_calendar: EdgeCalendar,
        task_station_constraints: dict[str, tuple[str, int]],
        node_types: dict[str, str],
        start_at_ms: int = 0,
    ) -> tuple[_PlannedRoute | None, str | None]:
        """Plan all ordered goals in one expanded-state SIPP search.

        Planning every leg greedily can arrive at a rack too early and become
        trapped there by a later node reservation.  The expanded search keeps
        ``goal_index`` in the state, so it may deliberately reach the same rack
        in a later safe interval when that makes the remaining task sequence
        feasible.
        """

        goals: list[tuple[str, str, int]] = []
        for task_id in task_sequence:
            if task_id not in task_location:
                return None, f"MAPF route references unknown task {task_id}."
            if task_id not in task_service_time:
                return None, f"MAPF route lacks handling time for task {task_id}."
            service_ms = int(task_service_time[task_id])
            goals.append((task_id, task_location[task_id], service_ms))

        finish_at, actions = self._plan_ordered_goals(
            graph=graph,
            start_node=start_node,
            goals=goals,
            edge_calendar=edge_calendar,
            node_calendar=node_calendar,
            station_calendar=station_calendar,
            task_station_constraints=task_station_constraints,
            node_types=node_types,
            robot_id=robot_id,
            start_at_ms=start_at_ms,
        )
        if finish_at == inf:
            return None, (
                f"No safe ordered-goal path for {robot_id}: "
                f"{start_node} -> {[node for _, node, _ in goals]}."
            )

        steps: list[TimedRouteStep] = []
        reservations: list[EdgeReservation] = []
        segments: list[RouteSegment] = []
        node_sequence = [start_node]
        current_node = start_node
        current_time = int(start_at_ms)
        for action in actions:
            if isinstance(action, _TimedArc):
                arc = action.arc
                if action.depart_at_ms > current_time:
                    if node_types.get(current_node) not in TrafficManagerService.SAFE_WAIT_NODE_TYPES:
                        return None, f"{robot_id} cannot wait safely at {current_node}."
                    steps.append(
                        TimedRouteStep(
                            step_type="WAIT",
                            node_id=current_node,
                            start_at_ms=current_time,
                            end_at_ms=action.depart_at_ms,
                            reason=f"Safe interval for {arc.edge_id} starts at {action.depart_at_ms} ms.",
                        )
                    )
                steps.append(
                    TimedRouteStep(
                        step_type="MOVE",
                        edge_id=arc.edge_id,
                        from_node=arc.source,
                        to_node=arc.target,
                        start_at_ms=action.depart_at_ms,
                        end_at_ms=action.arrive_at_ms,
                    )
                )
                resource_id = self._resource_id(arc.source, arc.target)
                reservations.append(
                    EdgeReservation(
                        reservation_id=f"MAPF-{robot_id}-{len(reservations):04d}",
                        edge_id=arc.edge_id,
                        robot_id=robot_id,
                        direction=f"{arc.source}_TO_{arc.target}",
                        start_at_ms=action.depart_at_ms,
                        end_at_ms=action.arrive_at_ms,
                        from_node=arc.source,
                        to_node=arc.target,
                        physical_resource_id=resource_id,
                    )
                )
                edge_calendar.reserve(
                    edge_id=resource_id,
                    start=action.depart_at_ms,
                    end=action.arrive_at_ms,
                    robot_id=robot_id,
                )
                segments.append(
                    RouteSegment(
                        sequence=len(segments),
                        edge_id=arc.edge_id,
                        from_node=arc.source,
                        to_node=arc.target,
                        cost=arc.cost,
                        travel_time_ms=arc.travel_time_ms,
                    )
                )
                node_sequence.append(arc.target)
                current_node = arc.target
                current_time = action.arrive_at_ms
            else:
                if action.start_at_ms > current_time:
                    if node_types.get(current_node) not in TrafficManagerService.SAFE_WAIT_NODE_TYPES:
                        return None, f"{robot_id} cannot wait safely for service at {current_node}."
                    steps.append(
                        TimedRouteStep(
                            step_type="WAIT",
                            node_id=current_node,
                            start_at_ms=current_time,
                            end_at_ms=action.start_at_ms,
                            reason=f"Service node {current_node} becomes available at {action.start_at_ms} ms.",
                        )
                    )
                steps.append(
                    TimedRouteStep(
                        step_type="SERVICE",
                        node_id=action.node_id,
                        start_at_ms=action.start_at_ms,
                        end_at_ms=action.end_at_ms,
                        task_id=action.task_id,
                        service_kind=self._service_kind(action.task_id),
                        reason=f"{action.task_id} service time.",
                    )
                )
                current_time = action.end_at_ms

        return (
            _PlannedRoute(
                steps=steps,
                reservations=reservations,
                segments=segments,
                node_sequence=node_sequence,
                finish_at_ms=int(finish_at),
                edge_calendar=edge_calendar,
            ),
            None,
        )

    @staticmethod
    def _service_kind(task_id: str) -> str:
        if task_id.endswith("_PICK"):
            return "PICKUP"
        if task_id.endswith("_RETURN"):
            return "RETURN"
        if task_id.endswith("_EMPTY_TOTE"):
            return "EMPTY_TOTE_BUFFER"
        if task_id.endswith("-PARK") or task_id.endswith("_PARK"):
            return "PARK"
        if task_id.endswith("-CHARGE") or task_id.endswith("_CHARGE"):
            return "CHARGE"
        if task_id.startswith("G2P-") and task_id.endswith("_DROP"):
            return "STATION"
        return "DROP"

    @staticmethod
    def _node_intervals(
        *,
        steps: list[TimedRouteStep],
        headway_ms: int,
    ) -> list[tuple[str, int, int]]:
        """Return node reservations for WAIT/SERVICE and MOVE arrivals."""

        values: list[tuple[str, int, int]] = []
        for step in steps:
            if step.step_type in {"WAIT", "SERVICE"} and step.node_id is not None:
                values.append((step.node_id, step.start_at_ms, step.end_at_ms))
            elif step.step_type == "MOVE" and step.to_node is not None:
                values.append((step.to_node, step.end_at_ms, step.end_at_ms + headway_ms))
        return values

    @staticmethod
    def _resource_id(source: str, target: str) -> str:
        """Map opposite directed arcs onto one physical corridor resource."""

        left, right = sorted((source, target))
        return f"CORRIDOR:{left}<->{right}"

    @staticmethod
    def _station_resource_id(station_id: str) -> str:
        """Return the shared calendar key for one fixed station robot."""

        return f"STATION:{station_id}"

    @classmethod
    def _plan_ordered_goals(
        cls,
        *,
        graph: DirectedGraphService,
        start_node: str,
        goals: list[tuple[str, str, int]],
        edge_calendar: EdgeCalendar,
        node_calendar: EdgeCalendar,
        station_calendar: EdgeCalendar,
        task_station_constraints: dict[str, tuple[str, int]],
        node_types: dict[str, str],
        robot_id: str,
        start_at_ms: int,
    ) -> tuple[float, list[_TimedArc | _TimedService]]:
        """Plan a fixed task order while choosing route and safe intervals."""

        start_key = cls._safe_interval_key(
            calendar=node_calendar,
            node_id=start_node,
            time_ms=start_at_ms,
            ignore_robot_id=robot_id,
        )
        if start_key is None:
            return inf, []
        # state = (node, safe_interval_index, next_goal_index)
        start_state = (start_node, start_key[0], 0)
        best: dict[tuple[str, int, int], int] = {start_state: start_at_ms}
        previous: dict[
            tuple[str, int, int],
            tuple[tuple[str, int, int], _TimedArc | _TimedService],
        ] = {}
        queue: list[tuple[int, str, int, int]] = [
            (start_at_ms, start_node, start_key[0], 0)
        ]
        goal_state: tuple[str, int, int] | None = None
        headway = max(1, get_settings().traffic_safety_headway_ms)

        while queue:
            arrival, node, interval_index, goal_index = heapq.heappop(queue)
            state = (node, interval_index, goal_index)
            if arrival != best.get(state):
                continue
            if goal_index == len(goals):
                goal_state = state
                break
            interval = cls._safe_intervals(
                node_calendar,
                node,
                ignore_robot_id=robot_id,
            )[interval_index]
            safe_end = interval[1]

            task_id, goal_node, service_ms = goals[goal_index]
            if node == goal_node:
                service_slot = cls._earliest_joint_service_slot(
                    node_calendar=node_calendar,
                    station_calendar=station_calendar,
                    node_id=node,
                    task_id=task_id,
                    earliest_ms=arrival,
                    service_ms=max(1, service_ms),
                    station_constraint=task_station_constraints.get(task_id),
                    robot_id=robot_id,
                )
                if (
                    service_slot + max(1, service_ms) <= safe_end
                    and (
                        service_slot == arrival
                        or node_types.get(node) in TrafficManagerService.SAFE_WAIT_NODE_TYPES
                    )
                ):
                    service_end = service_slot + max(1, service_ms)
                    next_key = cls._safe_interval_key(
                        calendar=node_calendar,
                        node_id=node,
                        time_ms=max(service_slot, service_end - 1),
                        ignore_robot_id=robot_id,
                    )
                    if next_key is not None:
                        next_state = (node, next_key[0], goal_index + 1)
                        if service_end < best.get(next_state, inf):
                            best[next_state] = service_end
                            previous[next_state] = (
                                state,
                                _TimedService(
                                    task_id=task_id,
                                    node_id=node,
                                    start_at_ms=service_slot,
                                    end_at_ms=service_end,
                                ),
                            )
                            heapq.heappush(
                                queue,
                                (service_end, node, next_key[0], goal_index + 1),
                            )

            for arc in graph.by_source.get(node, []):
                for depart, next_arrival, target_interval_index in cls._feasible_transitions(
                    arc=arc,
                    earliest_departure_ms=arrival,
                    source_safe_end_ms=safe_end,
                    edge_calendar=edge_calendar,
                    node_calendar=node_calendar,
                    target_hold_ms=headway,
                    robot_id=robot_id,
                ):
                    if depart > arrival and node_types.get(node) not in TrafficManagerService.SAFE_WAIT_NODE_TYPES:
                        continue
                    next_state = (arc.target, target_interval_index, goal_index)
                    if next_arrival < best.get(next_state, inf):
                        best[next_state] = next_arrival
                        previous[next_state] = (
                            state,
                            _TimedArc(
                                arc=arc,
                                depart_at_ms=depart,
                                arrive_at_ms=next_arrival,
                            ),
                        )
                        heapq.heappush(
                            queue,
                            (next_arrival, arc.target, target_interval_index, goal_index),
                        )

        if goal_state is None:
            return inf, []
        actions: list[_TimedArc | _TimedService] = []
        cursor = goal_state
        while cursor != start_state:
            if cursor not in previous:
                return inf, []
            parent, action = previous[cursor]
            actions.append(action)
            cursor = parent
        actions.reverse()
        return float(best[goal_state]), actions

    @classmethod
    def _earliest_joint_service_slot(
        cls,
        *,
        node_calendar: EdgeCalendar,
        station_calendar: EdgeCalendar,
        node_id: str,
        task_id: str,
        earliest_ms: int,
        service_ms: int,
        station_constraint: tuple[str, int] | None,
        robot_id: str,
    ) -> int:
        """Find the first service start where its node and fixed station are free.

        A station can expose several AMR handoff nodes while still having only
        one fixed robot.  Node calendars therefore cannot serialize station
        handoffs by themselves.  The monotone loop alternates both calendars
        until the complete node service and the exclusive receive window fit
        at the same start time.
        """

        candidate = max(0, int(earliest_ms))
        for _ in range(128):
            node_slot = node_calendar.earliest_slot(
                edge_id=f"NODE:{node_id}",
                earliest=candidate,
                duration=max(1, service_ms),
                ignore_robot_id=robot_id,
            )
            if station_constraint is None:
                return node_slot
            station_resource_id, receive_ms = station_constraint
            station_slot = station_calendar.earliest_slot(
                edge_id=station_resource_id,
                earliest=node_slot,
                duration=max(1, receive_ms),
            )
            if station_slot == node_slot:
                return node_slot
            candidate = station_slot
        raise RuntimeError(
            f"Unable to converge on a joint node/station slot for {task_id}."
        )

    @classmethod
    def _feasible_transitions(
        cls,
        *,
        arc: Arc,
        earliest_departure_ms: int,
        source_safe_end_ms: int,
        edge_calendar: EdgeCalendar,
        node_calendar: EdgeCalendar,
        target_hold_ms: int,
        robot_id: str,
    ) -> list[tuple[int, int, int]]:
        """Return earliest feasible traversal into every reachable target interval.

        Considering only the first target interval is incomplete: a robot may
        need to delay departure from the source so that it reaches a junction
        after another robot has cleared it.  SIPP represents those alternatives
        as separate ``(node, safe_interval)`` states.
        """

        resource_id = cls._resource_id(arc.source, arc.target)
        transitions: list[tuple[int, int, int]] = []
        for target_interval_index, (target_start, target_end) in enumerate(
            cls._safe_intervals(
                node_calendar,
                arc.target,
                ignore_robot_id=robot_id,
            )
        ):
            candidate = max(earliest_departure_ms, target_start - arc.travel_time_ms)
            for _ in range(128):
                depart = edge_calendar.earliest_slot(
                    edge_id=resource_id,
                    earliest=candidate,
                    duration=arc.travel_time_ms,
                    ignore_robot_id=robot_id,
                )
                if depart > source_safe_end_ms:
                    break
                arrival = depart + arc.travel_time_ms
                if arrival < target_start:
                    candidate = depart + (target_start - arrival)
                    continue
                if arrival + target_hold_ms <= target_end:
                    transitions.append((depart, arrival, target_interval_index))
                break
        return transitions

    @classmethod
    def _safe_interval_key(
        cls,
        *,
        calendar: EdgeCalendar,
        node_id: str,
        time_ms: int,
        ignore_robot_id: str | None = None,
    ) -> tuple[int, tuple[int, int]] | None:
        """Return the safe-interval index containing ``time_ms``."""

        for index, interval in enumerate(
            cls._safe_intervals(
                calendar,
                node_id,
                ignore_robot_id=ignore_robot_id,
            )
        ):
            if interval[0] <= time_ms < interval[1]:
                return index, interval
        return None

    @classmethod
    def _safe_intervals(
        cls,
        calendar: EdgeCalendar,
        node_id: str,
        ignore_robot_id: str | None = None,
    ) -> list[tuple[int, int]]:
        """Return complements of headway-expanded node reservations."""

        headway = max(1, calendar.headway_ms)
        blocked: list[tuple[int, int]] = []
        for interval in calendar.intervals(f"NODE:{node_id}"):
            if ignore_robot_id is not None and interval.robot_id == ignore_robot_id:
                continue
            start = max(0, interval.start - headway)
            end = min(cls.SEARCH_HORIZON_MS, interval.end + headway)
            if blocked and start <= blocked[-1][1]:
                blocked[-1] = (blocked[-1][0], max(blocked[-1][1], end))
            else:
                blocked.append((start, end))
        safe: list[tuple[int, int]] = []
        cursor = 0
        for start, end in blocked:
            if cursor < start:
                safe.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < cls.SEARCH_HORIZON_MS:
            safe.append((cursor, cls.SEARCH_HORIZON_MS))
        return safe or [(0, cls.SEARCH_HORIZON_MS)]


class MAPFPlanValidator:
    """Validate timed routes independently from the prioritized planner."""

    def validate(
        self,
        *,
        schedule: TrafficScheduleResult,
        map_context: MapContext,
        node_types: dict[str, str],
        max_edge_wait_ms: int | None = None,
        payload: CuOptPayload | None = None,
        preserved_node_reservations: list[NodeReservation] | None = None,
    ) -> MAPFValidationResult:
        """Check time order, handling durations, headway, and node occupancy."""

        errors = list(schedule.conflicts)
        warnings = list(schedule.warnings)
        headway = max(1, get_settings().traffic_safety_headway_ms)
        by_resource: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        resource_by_edge = {
            reservation.edge_id: (
                reservation.physical_resource_id or f"EDGE:{reservation.edge_id}"
            )
            for reservation in schedule.reservations
        }
        for existing in map_context.map_constraints.edge_reservations:
            if existing.physical_resource_id:
                resource_by_edge.setdefault(existing.edge_id, existing.physical_resource_id)
        for occupancy in map_context.map_constraints.edge_occupancies:
            resource = resource_by_edge.get(occupancy.edge_id, f"EDGE:{occupancy.edge_id}")
            by_resource[resource].append(
                (occupancy.occupied_from_ms, occupancy.occupied_until_ms, occupancy.robot_id)
            )
        for existing in map_context.map_constraints.edge_reservations:
            resource = existing.physical_resource_id or resource_by_edge.get(
                existing.edge_id,
                f"EDGE:{existing.edge_id}",
            )
            by_resource[resource].append((existing.start_at_ms, existing.end_at_ms, existing.robot_id))
        for reservation in schedule.reservations:
            resource = reservation.physical_resource_id or f"EDGE:{reservation.edge_id}"
            by_resource[resource].append(
                (reservation.start_at_ms, reservation.end_at_ms, reservation.robot_id)
            )
        for resource, intervals in by_resource.items():
            ordered = sorted(intervals)
            for first, second in zip(ordered, ordered[1:]):
                if first[2] == second[2]:
                    continue
                if first[1] + headway > second[0]:
                    errors.append(
                        f"Resource {resource} violates headway for {first[2]} and {second[2]}: "
                        f"{first[0]}-{first[1]} vs {second[0]}-{second[1]}."
                    )

        node_intervals: dict[str, list[_NodeInterval]] = defaultdict(list)
        for reservation in list(preserved_node_reservations or []):
            node_intervals[reservation.node_id].append(
                _NodeInterval(
                    reservation.start_at_ms,
                    reservation.end_at_ms,
                    reservation.robot_id,
                )
            )
        expected_service: dict[str, int] = {}
        mandatory_service_ids: set[str] = set()
        service_counts: dict[str, int] = defaultdict(int)
        if payload is not None:
            service_values = list(payload.task_data.service_times_ms)
            if not service_values:
                settings = get_settings()
                service_values = [
                    (
                        settings.pickup_service_time_ms
                        if task_id.endswith("_PICK")
                        else settings.drop_service_time_ms
                    )
                    for task_id in payload.task_data.task_ids
                ]
            expected_service = dict(
                zip(payload.task_data.task_ids, service_values, strict=True)
            )
            mandatory_service_ids = set(payload.task_data.task_ids).difference(
                payload.task_data.optional_task_ids
            )
        for route in schedule.routes:
            previous_end = 0
            for step in route.steps:
                if step.start_at_ms < previous_end:
                    errors.append(f"{route.robot_id} has non-monotonic MAPF steps.")
                if step.step_type == "WAIT":
                    if step.node_id is None or node_types.get(step.node_id) not in TrafficManagerService.SAFE_WAIT_NODE_TYPES:
                        errors.append(f"{route.robot_id} waits at unsafe node {step.node_id}.")
                    wait_ms = step.end_at_ms - step.start_at_ms
                    if max_edge_wait_ms is not None and wait_ms > max_edge_wait_ms:
                        errors.append(
                            f"{route.robot_id} wait {wait_ms} exceeds max_edge_wait_ms={max_edge_wait_ms}."
                        )
                if step.step_type == "SERVICE":
                    if step.task_id is None:
                        errors.append(
                            f"{route.robot_id} has a SERVICE step without task_id at {step.node_id}."
                        )
                    elif expected_service:
                        if step.task_id not in expected_service:
                            errors.append(
                                f"{route.robot_id} services unknown task {step.task_id}."
                            )
                        else:
                            actual_ms = step.end_at_ms - step.start_at_ms
                            expected_ms = expected_service[step.task_id]
                            if actual_ms != expected_ms:
                                errors.append(
                                    f"{route.robot_id} service duration for {step.task_id} is "
                                    f"{actual_ms} ms; expected {expected_ms} ms."
                                )
                            service_counts[step.task_id] += 1
                if step.step_type in {"WAIT", "SERVICE"} and step.node_id is not None:
                    node_intervals[step.node_id].append(
                        _NodeInterval(step.start_at_ms, step.end_at_ms, route.robot_id)
                    )
                elif step.step_type == "MOVE" and step.to_node is not None:
                    node_intervals[step.to_node].append(
                        _NodeInterval(step.end_at_ms, step.end_at_ms + headway, route.robot_id)
                    )
                previous_end = step.end_at_ms
        if expected_service:
            missing_service = sorted(
                task_id
                for task_id in mandatory_service_ids
                if service_counts.get(task_id, 0) == 0
            )
            duplicate_service = sorted(
                task_id
                for task_id, count in service_counts.items()
                if count != 1
            )
            if missing_service:
                errors.append(
                    f"Mandatory handling steps are missing for task ids: {missing_service}"
                )
            if duplicate_service:
                errors.append(
                    f"Handling steps must occur exactly once per assigned task row: {duplicate_service}"
                )
        for node_id, intervals in node_intervals.items():
            ordered = sorted(intervals, key=lambda value: (value.start, value.end, value.robot_id))
            for first, second in zip(ordered, ordered[1:]):
                if first.robot_id != second.robot_id and first.end + headway > second.start:
                    errors.append(
                        f"Node {node_id} violates headway for {first.robot_id} and {second.robot_id}: "
                        f"{first.start}-{first.end} vs {second.start}-{second.end}."
                    )
        by_station: dict[str, list[StationServiceReservation]] = defaultdict(list)
        for reservation in schedule.station_reservations:
            by_station[reservation.station_id].append(reservation)
        for station_id, values in by_station.items():
            ordered = sorted(values, key=lambda value: (value.start_at_ms, value.end_at_ms))
            for first, second in zip(ordered, ordered[1:]):
                if first.end_at_ms + headway > second.start_at_ms:
                    errors.append(
                        f"Station {station_id} capacity=1 overlap: "
                        f"{first.mobile_robot_id} {first.start_at_ms}-{first.end_at_ms} vs "
                        f"{second.mobile_robot_id} {second.start_at_ms}-{second.end_at_ms}."
                    )
        if not schedule.routes:
            warnings.append("MAPF produced no timed routes.")
        calculated_service = sum(
            step.end_at_ms - step.start_at_ms
            for route in schedule.routes
            for step in route.steps
            if step.step_type == "SERVICE"
        )
        if calculated_service != schedule.total_service_ms:
            errors.append(
                "TrafficScheduleResult.total_service_ms does not match SERVICE steps: "
                f"stored={schedule.total_service_ms}, calculated={calculated_service}."
            )
        return MAPFValidationResult(valid=not errors, errors=errors, warnings=warnings)

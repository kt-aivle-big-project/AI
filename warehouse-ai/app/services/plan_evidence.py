from __future__ import annotations

import math
from typing import Any

from app.models import (
    CollisionFreePlan,
    CuOptPlan,
    DistanceComparison,
    ReservationEvidence,
    RobotDistanceDifference,
    RobotRouteEvidence,
    RouteSegmentEvidence,
    RoutingEvidence,
    WaitEvidence,
)
from app.services.routing import active_edges
from app.services.wait_compression import compress_route_segments


def _snapshot_edge_map(
    problem: dict[str, Any],
) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for edge in active_edges(problem):
        start = int(edge["from_node"])
        target = int(edge["to_node"])
        result[(start, target)] = edge
        if str(edge.get("direction", "ONE_WAY")).upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            result[(target, start)] = edge
    return result


def _edge_identifier(edge: dict[str, Any], start: int, target: int) -> str:
    edge_id = edge.get("edge_id")
    return str(edge_id) if edge_id is not None else f"{start}->{target}"


def build_route_evidence(
    problem: dict[str, Any],
    optimizer_plan: CuOptPlan,
    collision_plan: CollisionFreePlan,
) -> tuple[RoutingEvidence, ReservationEvidence, DistanceComparison]:
    edge_map = _snapshot_edge_map(problem)
    route_sources = {
        str(key): str(value)
        for key, value in (collision_plan.metadata.get("route_sources") or {}).items()
    }
    preserved_prefix_end_steps = {
        str(key): int(value)
        for key, value in (
            collision_plan.metadata.get("preserved_prefix_end_steps") or {}
        ).items()
    }
    is_external = collision_plan.metadata.get("routing_backend") == "mapf"
    issues: list[str] = []
    robot_routes: list[RobotRouteEvidence] = []

    for route in collision_plan.routes:
        segments: list[RouteSegmentEvidence] = []
        prefix_end = preserved_prefix_end_steps.get(route.robot_id)
        default_source = route_sources.get(route.robot_id)
        for left, right in zip(route.waypoints, route.waypoints[1:]):
            is_wait = left.node_id == right.node_id
            source = (
                "PRESERVED_ACTIVE_PLAN"
                if prefix_end is not None and right.time_step <= prefix_end
                else (
                    default_source
                    if default_source in {
                        "PRESERVED_ACTIVE_PLAN",
                        "INTERNAL_ROUTE_SEARCH",
                    }
                    else (
                        "NEO4J_SNAPSHOT"
                        if is_external
                        else "INTERNAL_ROUTE_SEARCH"
                    )
                )
            )
            edge = None if is_wait else edge_map.get((left.node_id, right.node_id))
            if not is_wait and edge is None:
                issues.append(
                    f"SNAPSHOT_EDGE_NOT_FOUND:{route.robot_id}:"
                    f"{left.node_id}->{right.node_id}:{left.time_step}"
                )
            distance = (
                0.0
                if is_wait
                else (
                    float(edge.get("distance") or 0.0)
                    if edge is not None
                    else None
                )
            )
            segments.append(
                RouteSegmentEvidence(
                    from_node=left.node_id,
                    to_node=right.node_id,
                    depart_step=left.time_step,
                    arrive_step=right.time_step,
                    action="WAIT" if is_wait else "MOVE",
                    distance=distance,
                    travel_steps=max(0, right.time_step - left.time_step),
                    edge_identifier=(
                        None
                        if edge is None
                        else _edge_identifier(edge, left.node_id, right.node_id)
                    ),
                    source=source,
                )
            )
        compressed_segments = [
            RouteSegmentEvidence.model_validate(row)
            for row in compress_route_segments(
                segment.model_dump(mode="json") for segment in segments
            )
        ]
        segment_distance = sum(
            segment.distance
            for segment in compressed_segments
            if segment.distance is not None
        )
        distance_consistent = not any(
            segment.distance is None for segment in segments
        ) and math.isclose(segment_distance, route.distance, abs_tol=1e-6)
        if not distance_consistent:
            issues.append(f"ROUTE_DISTANCE_MISMATCH:{route.robot_id}")
        robot_routes.append(
            RobotRouteEvidence(
                robot_id=route.robot_id,
                task_ids=list(route.task_ids),
                segments=compressed_segments,
                segment_distance=round(segment_distance, 6),
                route_distance=round(route.distance, 6),
                distance_consistent=distance_consistent,
            )
        )

    routing_evidence = RoutingEvidence(
        engine=collision_plan.engine,
        route_segment_count=sum(len(route.segments) for route in robot_routes),
        complete=not issues,
        issues=issues,
        routes=robot_routes,
    )

    raw_wait_rows = list(collision_plan.metadata.get("wait_evidence", []))
    waits = [WaitEvidence.model_validate(row) for row in raw_wait_rows]
    reroute_value = (
        int(collision_plan.metadata.get("reroute_count") or 0)
        if "reroute_count" in collision_plan.metadata
        else None
    )
    reservation_evidence = ReservationEvidence(
        vertex_reservation_count=int(
            collision_plan.metadata.get("vertex_reservations") or 0
        ),
        edge_reservation_count=int(
            collision_plan.metadata.get("edge_reservations") or 0
        ),
        wait_count=len(waits),
        reroute_count=reroute_value,
        final_conflict_count=None,
        waits=waits,
        resolution_events=list(
            collision_plan.metadata.get("resolution_events") or []
        ),
        idle_action_task_count=int(
            collision_plan.metadata.get("idle_action_task_count") or 0
        ),
        idle_action_tasks=list(
            collision_plan.metadata.get("idle_action_tasks") or []
        ),
        idle_policy=dict(collision_plan.metadata.get("idle_policy") or {}),
    )

    estimated_by_robot: dict[str, float] = {}
    for task in optimizer_plan.scheduled_tasks:
        estimated_by_robot[task.robot_id] = (
            estimated_by_robot.get(task.robot_id, 0.0) + task.estimated_distance
        )
    final_by_robot = {route.robot_id: route.distance for route in collision_plan.routes}
    robot_differences: list[RobotDistanceDifference] = []
    for robot_id in sorted(set(estimated_by_robot) | set(final_by_robot)):
        estimated = estimated_by_robot.get(robot_id, 0.0)
        final = final_by_robot.get(robot_id, 0.0)
        route_evidence = next(
            (row for row in robot_routes if row.robot_id == robot_id), None
        )
        fully_preserved = bool(
            route_evidence
            and route_evidence.segments
            and all(
                segment.source == "PRESERVED_ACTIVE_PLAN"
                for segment in route_evidence.segments
            )
        )
        difference_value = final - estimated
        robot_wait_reasons = {
            str(row.get("reason") or "")
            for row in collision_plan.metadata.get("wait_evidence", [])
            if str(row.get("robot_id")) == robot_id
        }
        if fully_preserved:
            reason_code = "PRESERVED_ROUTE"
        elif "RESERVATION_CONFLICT_WAIT" in robot_wait_reasons:
            reason_code = (
                "CONFLICT_AVOIDANCE_DETOUR"
                if difference_value > 1e-9
                else "RESERVATION_WAIT"
            )
        elif not math.isclose(difference_value, 0.0, abs_tol=1e-9):
            # The local optimizer estimates distance on a distance-minimizing
            # graph, while the time-expanded router minimizes feasible arrival
            # time.  Different edge weights can therefore produce a longer but
            # faster final route even without a collision detour.
            reason_code = "TIME_OPTIMAL_ROUTE_DISTANCE_VARIANCE"
        else:
            reason_code = "ESTIMATED_DISTANCE_APPROXIMATION"
        robot_differences.append(
            RobotDistanceDifference(
                robot_id=robot_id,
                estimated_distance=round(estimated, 6),
                final_distance=round(final, 6),
                difference=round(difference_value, 6),
                reason_code=reason_code,
            )
        )
    optimizer_distance = sum(estimated_by_robot.values())
    routing_distance = collision_plan.total_distance
    difference = routing_distance - optimizer_distance
    difference_percent = (
        round((difference / optimizer_distance) * 100.0, 6)
        if optimizer_distance
        else (0.0 if math.isclose(difference, 0.0, abs_tol=1e-9) else None)
    )
    distance_comparison = DistanceComparison(
        optimizer_estimated_distance=round(optimizer_distance, 6),
        routing_final_distance=round(routing_distance, 6),
        difference=round(difference, 6),
        difference_percent=difference_percent,
        robot_differences=robot_differences,
    )
    return routing_evidence, reservation_evidence, distance_comparison

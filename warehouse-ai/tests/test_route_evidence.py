from __future__ import annotations

import math

from app.models import CuOptPlan, ScheduledTask
from app.planning.nodes import validate_plan_node
from app.services.plan_evidence import build_route_evidence
from app.services.routing import PrioritizedTimeExpandedPlanner
from tests.test_routing import square_problem


def _plan(*, start_time_step: int = 0) -> CuOptPlan:
    return CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="T1",
                robot_id="R1",
                source_node=1,
                target_node=3,
                start_time_step=start_time_step,
                end_time_step=start_time_step + 2,
                estimated_distance=2,
            )
        ],
        objective_value=2,
    )


def _evidence(*, start_time_step: int = 0):
    problem = square_problem()
    optimizer_plan = _plan(start_time_step=start_time_step)
    collision_plan = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(
        optimizer_plan
    )
    return (
        problem,
        optimizer_plan,
        collision_plan,
        *build_route_evidence(problem, optimizer_plan, collision_plan),
    )


def test_waypoint_pairs_match_route_segments_and_snapshot_edges() -> None:
    problem, _, collision_plan, routing, _, _ = _evidence()
    declared_pairs = set()
    for edge in problem["edges"]:
        pair = (edge["from_node"], edge["to_node"])
        declared_pairs.add(pair)
        if edge["direction"] == "BOTH":
            declared_pairs.add((pair[1], pair[0]))

    route_by_robot = {route.robot_id: route for route in collision_plan.routes}
    for route_evidence in routing.routes:
        waypoints = route_by_robot[route_evidence.robot_id].waypoints
        assert len(route_evidence.segments) == max(0, len(waypoints) - 1)
        for segment, left, right in zip(
            route_evidence.segments, waypoints, waypoints[1:]
        ):
            assert (segment.from_node, segment.depart_step) == (
                left.node_id,
                left.time_step,
            )
            assert (segment.to_node, segment.arrive_step) == (
                right.node_id,
                right.time_step,
            )
            if segment.action == "MOVE":
                assert (segment.from_node, segment.to_node) in declared_pairs
                assert segment.edge_identifier is not None
                dumped = segment.model_dump()
                assert "direction" not in dumped
                assert "speed_limit" not in dumped
                assert "capacity" not in dumped


def test_segment_distance_sum_matches_route_distance() -> None:
    _, _, _, routing, _, _ = _evidence()

    assert routing.complete
    for route in routing.routes:
        assert route.distance_consistent
        assert math.isclose(route.segment_distance, route.route_distance, abs_tol=1e-6)


def test_wait_and_reservation_evidence_come_from_planner_metadata() -> None:
    _, _, collision_plan, _, reservations, _ = _evidence(start_time_step=3)

    assert reservations.vertex_reservation_count == collision_plan.metadata[
        "vertex_reservations"
    ]
    assert reservations.edge_reservation_count == collision_plan.metadata[
        "edge_reservations"
    ]
    assert reservations.wait_count == len(reservations.waits) == 3
    assert all(wait.reason == "SCHEDULED_START_WAIT" for wait in reservations.waits)
    assert all(wait.blocked_by_robot_id is None for wait in reservations.waits)
    assert reservations.reroute_count == 0


def test_validation_writes_only_the_observed_final_conflict_count() -> None:
    problem, optimizer_plan, collision_plan, routing, reservations, comparison = (
        _evidence()
    )
    state = {
        "optimization_problem": problem,
        "cuopt_plan": optimizer_plan.model_dump(mode="json"),
        "collision_plan": collision_plan.model_dump(mode="json"),
        "routing_evidence": routing.model_dump(mode="json"),
        "reservation_evidence": reservations.model_dump(mode="json"),
        "distance_comparison": comparison.model_dump(mode="json"),
    }

    update = validate_plan_node(state)

    assert update["reservation_evidence"]["final_conflict_count"] == 0
    assert update["plan_validation"]["conflict_count"] == 0


def test_optimizer_routing_distance_difference_reconciles_by_robot() -> None:
    _, _, _, _, _, comparison = _evidence()

    assert math.isclose(
        sum(row.estimated_distance for row in comparison.robot_differences),
        comparison.optimizer_estimated_distance,
        abs_tol=1e-6,
    )
    assert math.isclose(
        sum(row.final_distance for row in comparison.robot_differences),
        comparison.routing_final_distance,
        abs_tol=1e-6,
    )
    assert math.isclose(
        sum(row.difference for row in comparison.robot_differences),
        comparison.difference,
        abs_tol=1e-6,
    )


def test_missing_snapshot_edge_is_reported_without_fabricated_attributes() -> None:
    problem, optimizer_plan, collision_plan, _, _, _ = _evidence()
    problem["edges"] = []

    routing, _, _ = build_route_evidence(problem, optimizer_plan, collision_plan)

    assert not routing.complete
    assert any(issue.startswith("SNAPSHOT_EDGE_NOT_FOUND") for issue in routing.issues)
    move_segments = [
        segment
        for route in routing.routes
        for segment in route.segments
        if segment.action == "MOVE"
    ]
    assert move_segments
    assert all(segment.distance is None for segment in move_segments)
    assert all(segment.edge_identifier is None for segment in move_segments)

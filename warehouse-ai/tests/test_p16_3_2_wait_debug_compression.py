from __future__ import annotations

import json
from copy import deepcopy

from app.models import CollisionFreePlan, CuOptPlan, TimedRoute, TimedWaypoint
from app.services.plan_evidence import build_route_evidence
from app.services.response_view import RESPONSE_SCHEMA_VERSION, shape_planning_response
from app.services.wait_compression import (
    compact_debug_payload_for_llm,
    compact_route_metadata_for_llm,
    compress_debug_payload_for_presentation,
)


def _long_wait_collision_plan(wait_steps: int = 100) -> CollisionFreePlan:
    waypoints = [TimedWaypoint(node_id=1, time_step=0, action="MOVE")]
    waypoints.extend(
        TimedWaypoint(node_id=1, time_step=step, action="WAIT")
        for step in range(1, wait_steps + 1)
    )
    waypoints.append(
        TimedWaypoint(node_id=2, time_step=wait_steps + 1, action="MOVE")
    )
    wait_evidence = [
        {
            "robot_id": "R1",
            "task_id": "T1",
            "node_id": 1,
            "time_step": step,
            "reason": "SCHEDULED_START_WAIT",
            "added_delay_steps": 1,
        }
        for step in range(1, wait_steps + 1)
    ]
    return CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=["T1"],
                waypoints=waypoints,
                distance=1.0,
            )
        ],
        time_step_seconds=5,
        total_distance=1.0,
        metadata={
            "routing_backend": "internal",
            "vertex_reservations": wait_steps + 2,
            "edge_reservations": 1,
            "wait_evidence": wait_evidence,
            "resolution_events": [
                {"resolution": "WAIT", **row} for row in wait_evidence
            ],
        },
    )


def test_route_evidence_compresses_consecutive_wait_ranges() -> None:
    collision = _long_wait_collision_plan(100)
    optimizer = CuOptPlan(
        scheduled_tasks=[],
        unassigned_task_ids=[],
        changed_robot_ids=[],
        objective_value=0,
        metadata={},
    )
    problem = {
        "nodes": [
            {"node_id": 1, "active": True},
            {"node_id": 2, "active": True},
        ],
        "edges": [
            {
                "edge_id": "E1",
                "from_node": 1,
                "to_node": 2,
                "distance": 1.0,
                "active": True,
                "direction": "ONE_WAY",
            }
        ],
    }

    routing, reservations, _ = build_route_evidence(problem, optimizer, collision)

    assert routing.route_segment_count == 2
    wait_segment = routing.routes[0].segments[0]
    assert wait_segment.action == "WAIT"
    assert wait_segment.depart_step == 0
    assert wait_segment.arrive_step == 100
    assert wait_segment.travel_steps == 100
    assert reservations.wait_count == 100
    assert len(reservations.waits) == 100
    assert len(reservations.resolution_events) == 100


def test_full_response_view_compresses_waits_without_mutating_internal_result() -> None:
    collision = _long_wait_collision_plan(100).model_dump(mode="json")
    timeline = [
        {"time_step": step, "robot_id": "R1", "event": "WAIT", "node_id": 1}
        for step in range(1, 101)
    ]
    response = {
        "status": "SIMULATION_SUCCESS",
        "report_detail_level": "DEBUG",
        "collision_plan": collision,
        "simulation": {
            "metrics": {"time_step_seconds": 5},
            "robot_routes": deepcopy(collision["routes"]),
            "timeline": timeline,
        },
        "data": {"timeline": deepcopy(timeline)},
    }
    raw_waypoint_count = len(response["collision_plan"]["routes"][0]["waypoints"])

    shaped = shape_planning_response(response, "FULL")

    assert RESPONSE_SCHEMA_VERSION == "p16.5.12.1"
    assert shaped["response_schema_version"] == "p16.5.12.1"
    compressed_route = shaped["collision_plan"]["routes"][0]
    assert compressed_route["waypoint_count_raw"] == raw_waypoint_count
    assert compressed_route["waypoint_count_compressed"] == 3
    wait_row = compressed_route["waypoints"][1]
    assert wait_row["action"] == "WAIT"
    assert wait_row["duration_steps"] == 100
    assert wait_row["duration_seconds"] == 500
    assert shaped["simulation"]["timeline_count_raw"] == 100
    assert shaped["simulation"]["timeline_count_compressed"] == 1
    assert shaped["data"]["timeline_count_compressed"] == 1

    # The internal/persisted response remains time-expanded for deterministic use.
    assert len(response["collision_plan"]["routes"][0]["waypoints"]) == raw_waypoint_count
    assert len(response["simulation"]["timeline"]) == 100


def test_verification_llm_metadata_omits_raw_wait_arrays() -> None:
    collision = _long_wait_collision_plan(200).model_dump(mode="json")
    compact = compact_route_metadata_for_llm(collision["metadata"])

    assert "wait_evidence" not in compact
    assert "resolution_events" not in compact
    assert compact["wait_summary"]["wait_step_count"] == 200
    assert compact["wait_summary"]["compressed_wait_range_count"] == 1
    assert len(compact["wait_summary"]["wait_ranges"]) == 1
    assert compact["payload_policy"] == "RAW_TIME_EXPANDED_WAIT_ROWS_OMITTED"
    assert len(json.dumps(compact, ensure_ascii=False)) < 10_000


def test_report_llm_debug_payload_is_bounded() -> None:
    segments = [
        {
            "from_node": 1,
            "to_node": 1,
            "depart_step": step,
            "arrive_step": step + 1,
            "action": "WAIT",
            "distance": 0.0,
            "travel_steps": 1,
            "edge_identifier": None,
            "source": "INTERNAL_ROUTE_SEARCH",
        }
        for step in range(500)
    ]
    waits = [
        {
            "robot_id": "R1",
            "task_id": "T1",
            "node_id": 1,
            "time_step": step,
            "reason": "SCHEDULED_START_WAIT",
            "added_delay_steps": 1,
        }
        for step in range(500)
    ]
    payload = {
        "routing_and_reservations": {
            "routes": {"routes": [{"robot_id": "R1", "segments": segments}]},
            "reservations": {
                "waits": waits,
                "resolution_events": [
                    {"resolution": "WAIT", **row} for row in waits
                ],
            },
        },
        "candidate_evaluation": {
            "task_evidence": [
                {"task_id": "T1", "candidates": [{"robot_id": f"R{i}"} for i in range(20)]}
            ]
        },
    }

    compact = compact_debug_payload_for_llm(payload)
    route = compact["routing_and_reservations"]["routes"]["routes"][0]
    reservations = compact["routing_and_reservations"]["reservations"]
    task = compact["candidate_evaluation"]["task_evidence"][0]

    assert len(route["segments"]) == 1
    assert route["segment_count_raw"] == 500
    assert route["segment_count_compressed"] == 1
    assert len(reservations["waits"]) == 1
    assert reservations["wait_range_count"] == 1
    assert len(task["candidates"]) == 5
    assert task["candidates_truncated_for_llm"] is True
    assert len(json.dumps(compact, ensure_ascii=False)) < 20_000


def test_debug_presentation_compresses_without_truncation() -> None:
    segments = [
        {
            "from_node": 1,
            "to_node": 1,
            "depart_step": step,
            "arrive_step": step + 1,
            "action": "WAIT",
            "distance": 0.0,
            "travel_steps": 1,
            "edge_identifier": None,
            "source": "INTERNAL_ROUTE_SEARCH",
        }
        for step in range(100)
    ] + [
        {
            "from_node": step,
            "to_node": step + 1,
            "depart_step": 100 + step,
            "arrive_step": 101 + step,
            "action": "MOVE",
            "distance": 1.0,
            "travel_steps": 1,
            "edge_identifier": f"E{step}",
            "source": "INTERNAL_ROUTE_SEARCH",
        }
        for step in range(60)
    ]
    payload = {
        "routing_and_reservations": {
            "routes": {"routes": [{"robot_id": "R1", "segments": segments}]},
            "reservations": {"waits": [], "resolution_events": []},
        },
        "candidate_evaluation": {"task_evidence": []},
    }

    compact = compress_debug_payload_for_presentation(payload)
    route = compact["routing_and_reservations"]["routes"]["routes"][0]

    assert route["segment_count_raw"] == 160
    assert route["segment_count_compressed"] == 61
    assert len(route["segments"]) == 61
    assert compact["presentation_compression"]["truncated"] is False
    assert "segments_truncated_for_llm" not in route

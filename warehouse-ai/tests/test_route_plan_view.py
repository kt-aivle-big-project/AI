from app.models import ResponseView
from app.services.response_view import shape_planning_response
from app.services.route_plan_view import build_route_plan_view


def route_output() -> dict:
    return {
        "warnings": [],
        "route_view_context": {
            "nodes": [
                {"node_id": 1, "node_code": "R1_5"},
                {"node_id": 2, "node_code": "R2_5"},
            ],
            "edges": [
                {
                    "edge_id": "V5_1",
                    "from_node": 1,
                    "to_node": 2,
                    "direction": "BOTH",
                }
            ],
        },
        "optimization_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "G2P-001_PICK",
                    "robot_id": "R002",
                    "action": "PICK",
                    "source_node": 2,
                    "target_node": 2,
                    "start_time_step": 3,
                    "end_time_step": 5,
                }
            ]
        },
        "collision_plan": {
            "engine": "PRIORITIZED_TIME_ASTAR",
            "time_step_seconds": 1,
            "metadata": {
                "task_completion_steps": {"G2P-001_PICK": 5},
                "wait_evidence": [
                    {
                        "robot_id": "R002",
                        "node_id": 2,
                        "time_step": 3,
                        "reason": "RESERVATION_CONFLICT_WAIT",
                    }
                ],
            },
            "routes": [
                {
                    "robot_id": "R002",
                    "task_ids": ["G2P-001_PICK"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 2, "action": "MOVE"},
                        {"node_id": 2, "time_step": 3, "action": "WAIT"},
                        {"node_id": 2, "time_step": 4, "action": "PICK"},
                        {"node_id": 2, "time_step": 5, "action": "PICK"},
                    ],
                }
            ],
        },
        "simulation": {
            "valid": True,
            "issues": [],
            "warnings": [],
        },
    }


def test_route_plan_view_matches_public_millisecond_contract() -> None:
    payload = build_route_plan_view(route_output()).model_dump(
        mode="json",
        exclude_none=True,
    )

    assert payload == {
        "valid": True,
        "planner": "prioritized_time_astar",
        "routes": [
            {
                "robot_id": "R002",
                "steps": [
                    {
                        "step_type": "MOVE",
                        "start_at_ms": 0,
                        "end_at_ms": 2000,
                        "edge_id": "V5_1",
                        "from_node": "R1_5",
                        "to_node": "R2_5",
                    },
                    {
                        "step_type": "WAIT",
                        "start_at_ms": 2000,
                        "end_at_ms": 3000,
                        "node_id": "R2_5",
                        "reason": "RESERVATION_CONFLICT_WAIT",
                    },
                    {
                        "step_type": "SERVICE",
                        "start_at_ms": 3000,
                        "end_at_ms": 5000,
                        "node_id": "R2_5",
                        "task_id": "G2P-001_PICK",
                        "service_kind": "PICKUP",
                    },
                ],
                "finish_at_ms": 5000,
            }
        ],
        "reservations": [],
        "station_reservations": [],
        "conflicts": [],
        "warnings": ["R002 accumulates 1000 ms of MAPF wait."],
        "total_wait_ms": 1000,
        "total_service_ms": 2000,
        "makespan_ms": 5000,
    }


def test_route_plan_response_view_returns_only_route_contract() -> None:
    payload = shape_planning_response(route_output(), ResponseView.ROUTE_PLAN)

    assert set(payload) == {
        "valid",
        "planner",
        "routes",
        "reservations",
        "station_reservations",
        "conflicts",
        "warnings",
        "total_wait_ms",
        "total_service_ms",
        "makespan_ms",
    }
    assert payload["routes"][0]["steps"][0]["edge_id"] == "V5_1"

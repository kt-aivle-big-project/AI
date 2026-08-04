from __future__ import annotations

from math import inf

from app.services.edge_calendar import EdgeCalendar
from app.services.graph_service import DirectedGraphService
from app.services.mapf_service import PrioritizedSIPPPlanner
from app.services.traffic_manager import TrafficManagerService


def _graph() -> DirectedGraphService:
    return DirectedGraphService(
        [
            {
                "edge_id": "E-S-ACCESS",
                "source": "S",
                "target": "K5_9_ACCESS_B",
                "cost": 1,
                "travel_time_ms": 100,
            },
            {
                "edge_id": "E-ACCESS-G",
                "source": "K5_9_ACCESS_B",
                "target": "G",
                "cost": 1,
                "travel_time_ms": 100,
            },
        ]
    )


def test_sipp_does_not_use_rack_access_as_transit() -> None:
    finish_at, actions = PrioritizedSIPPPlanner._plan_ordered_goals(
        graph=_graph(),
        start_node="S",
        goals=[("TASK-DROP", "G", 100)],
        edge_calendar=EdgeCalendar(),
        node_calendar=EdgeCalendar(),
        node_types={"S": "route", "K5_9_ACCESS_B": "rack_access", "G": "route"},
        robot_id="R1",
        start_at_ms=0,
    )

    assert finish_at == inf
    assert actions == []


def test_static_shortest_path_excludes_forbidden_access_transit() -> None:
    cost, path = _graph().shortest_path(
        "S",
        "G",
        forbidden_transit_nodes={"K5_9_ACCESS_B"},
    )

    assert cost == inf
    assert path == []


def test_static_shortest_path_allows_access_as_destination() -> None:
    cost, path = _graph().shortest_path(
        "S",
        "K5_9_ACCESS_B",
        forbidden_transit_nodes={"K5_9_ACCESS_B"},
    )

    assert cost == 1
    assert [arc.edge_id for arc in path] == ["E-S-ACCESS"]


def test_sipp_allows_rack_access_when_it_is_current_service_goal() -> None:
    finish_at, actions = PrioritizedSIPPPlanner._plan_ordered_goals(
        graph=_graph(),
        start_node="S",
        goals=[("TASK-PICK", "K5_9_ACCESS_B", 100)],
        edge_calendar=EdgeCalendar(),
        node_calendar=EdgeCalendar(),
        node_types={"S": "route", "K5_9_ACCESS_B": "rack_access", "G": "route"},
        robot_id="R1",
        start_at_ms=0,
    )

    assert finish_at == 200
    assert actions


def test_rack_access_is_not_a_general_wait_node() -> None:
    assert "rack_access" not in TrafficManagerService.SAFE_WAIT_NODE_TYPES

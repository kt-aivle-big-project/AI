"""Routing helpers for inventory racks with service-only access nodes.

A rack ID (``K1_7``) is master data and never belongs to the routing graph.
Robots visit one of the rack's access nodes (``K1_7_ACCESS_A/B``).  Both access
nodes are dead-end spurs, so route search cannot use the rack as a shortcut.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import inf
from typing import Iterable

from app.services.graph_service import DirectedGraphService


@dataclass(frozen=True)
class RackAccessChoice:
    """Best executable service side for one rack-level operation."""

    rack_id: str
    access_node_id: str
    robot_to_access_time_ms: int
    access_to_delivery_time_ms: int
    total_time_ms: int
    total_cost: float
    best_robot_id: str | None = None


def path_exists(graph: DirectedGraphService, source: str, target: str) -> bool:
    """Return whether source and target are identical or directionally connected."""

    if source == target:
        return True
    value, path = graph.shortest_path(source, target, metric="travel_time")
    return value != inf and bool(path)


def reachable_access_nodes(
    graph: DirectedGraphService,
    *,
    access_node_ids: Iterable[str],
    source_nodes: Iterable[str] = (),
    target_node: str | None = None,
) -> list[str]:
    """Return access nodes reachable from any source and optionally to a target."""

    sources = [str(value) for value in source_nodes if value]
    values: list[str] = []
    for access_node_id in dict.fromkeys(str(value) for value in access_node_ids if value):
        if sources and not any(path_exists(graph, source, access_node_id) for source in sources):
            continue
        if target_node and not path_exists(graph, access_node_id, target_node):
            continue
        values.append(access_node_id)
    return values


def choose_best_access_node(
    graph: DirectedGraphService,
    *,
    rack_id: str,
    access_node_ids: Iterable[str],
    robot_start_nodes: dict[str, str],
    delivery_node: str,
) -> RackAccessChoice | None:
    """Choose the lowest-cost service side without fixing the final robot.

    For each access node, the score is the cheapest eligible robot-to-access
    path plus access-to-delivery.  The solver still assigns the robot later; this
    pass only prevents an inventory rack from being used as a routing node and
    selects a physically valid service side.
    """

    choices: list[RackAccessChoice] = []
    for access_node_id in dict.fromkeys(str(value) for value in access_node_ids if value):
        delivery_time, delivery_path = graph.shortest_path(
            access_node_id,
            delivery_node,
            metric="travel_time",
        )
        if access_node_id != delivery_node and not delivery_path:
            continue
        delivery_cost = sum(float(arc.cost) for arc in delivery_path)

        robot_options: list[tuple[int, float, str]] = []
        for robot_id, start_node in robot_start_nodes.items():
            robot_time, robot_path = graph.shortest_path(
                start_node,
                access_node_id,
                metric="travel_time",
            )
            if start_node != access_node_id and not robot_path:
                continue
            robot_cost = sum(float(arc.cost) for arc in robot_path)
            robot_options.append((int(robot_time), robot_cost, robot_id))
        if not robot_options:
            continue
        robot_time, robot_cost, robot_id = min(
            robot_options,
            key=lambda value: (value[0], value[1], value[2]),
        )
        choices.append(
            RackAccessChoice(
                rack_id=rack_id,
                access_node_id=access_node_id,
                robot_to_access_time_ms=robot_time,
                access_to_delivery_time_ms=int(delivery_time),
                total_time_ms=robot_time + int(delivery_time),
                total_cost=robot_cost + delivery_cost,
                best_robot_id=robot_id,
            )
        )
    return min(
        choices,
        key=lambda value: (
            value.total_time_ms,
            value.total_cost,
            value.access_node_id,
        ),
        default=None,
    )

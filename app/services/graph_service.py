"""Directed shortest-path utilities for the supplied warehouse graph."""
from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from math import inf
from typing import TYPE_CHECKING, Iterable, Mapping

if TYPE_CHECKING:
    from app.domain.schemas import CuOptPayload


def payload_graph_arcs(
    payload: "CuOptPayload",
    *,
    reverse_index: Mapping[int, str] | None = None,
) -> list[dict]:
    """Rebuild graph arcs without losing service-only endpoint semantics."""

    graph = payload.waypoint_graph_data
    node_ids = reverse_index or {
        index: node_id for node_id, index in payload.location_index_map.items()
    }
    service_only = set(graph.service_only_node_indices)
    return [
        {
            "edge_id": edge_id,
            "source": node_ids[source],
            "target": node_ids[target],
            "cost": cost,
            "travel_time_ms": travel,
            "source_service_only": source in service_only,
            "target_service_only": target in service_only,
        }
        for edge_id, source, target, cost, travel in zip(
            graph.edge_ids,
            graph.from_indices,
            graph.to_indices,
            graph.costs,
            graph.travel_times_ms,
            strict=True,
        )
    ]


@dataclass(frozen=True)
class Arc:
    """Internal directed graph arc."""

    edge_id: str
    source: str
    target: str
    cost: float
    travel_time_ms: int


class DirectedGraphService:
    """Provide deterministic shortest paths over runtime-adjusted directed arcs."""

    def __init__(self, arcs: Iterable[dict]) -> None:
        """Index arcs by source and by edge identifier."""

        arc_values = [dict(value) for value in arcs]
        self.service_only_nodes = {
            str(node_id)
            for value in arc_values
            for node_id, marked in (
                (value["source"], value.get("source_service_only") is True),
                (value["target"], value.get("target_service_only") is True),
            )
            if marked
        }
        self.arcs = [
            Arc(
                edge_id=str(value["edge_id"]),
                source=str(value["source"]),
                target=str(value["target"]),
                cost=float(value["cost"]),
                travel_time_ms=int(value["travel_time_ms"]),
            )
            for value in arc_values
        ]
        self.by_source: dict[str, list[Arc]] = defaultdict(list)
        self.by_edge_id: dict[str, Arc] = {}
        for arc in self.arcs:
            self.by_source[arc.source].append(arc)
            self.by_edge_id[arc.edge_id] = arc

    def shortest_path(self, start: str, target: str, *, metric: str = "cost") -> tuple[float, list[Arc]]:
        """Return shortest path value and arcs using cost or travel time."""

        if start == target:
            return 0.0, []
        queue: list[tuple[float, str]] = [(0.0, start)]
        best: dict[str, float] = {start: 0.0}
        previous: dict[str, tuple[str, Arc]] = {}
        while queue:
            value, node = heapq.heappop(queue)
            if value != best.get(node):
                continue
            if node == target:
                break
            # A service-only facility can be the start or destination of an
            # operation, but it must never become a shortcut between aisles.
            if node in self.service_only_nodes and node != start:
                continue
            for arc in self.by_source.get(node, []):
                weight = arc.cost if metric == "cost" else float(arc.travel_time_ms)
                candidate = value + weight
                if candidate < best.get(arc.target, inf):
                    best[arc.target] = candidate
                    previous[arc.target] = (node, arc)
                    heapq.heappush(queue, (candidate, arc.target))
        if target not in best:
            return inf, []
        path: list[Arc] = []
        cursor = target
        while cursor != start:
            parent, arc = previous[cursor]
            path.append(arc)
            cursor = parent
        path.reverse()
        return best[target], path

    def reachable(self, start: str, target: str) -> bool:
        """Return whether a directed path exists."""

        value, _ = self.shortest_path(start, target)
        return value != inf

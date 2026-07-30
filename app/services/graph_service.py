"""Directed shortest-path utilities for the supplied warehouse graph."""
from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from math import inf
from typing import Iterable


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

        self.arcs = [
            Arc(
                edge_id=str(value["edge_id"]),
                source=str(value["source"]),
                target=str(value["target"]),
                cost=float(value["cost"]),
                travel_time_ms=int(value["travel_time_ms"]),
            )
            for value in arcs
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

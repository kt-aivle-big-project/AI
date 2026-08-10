"""Expand optimizer task order into waypoint routes and validate graph continuity."""
from __future__ import annotations

from math import isclose
from typing import Mapping

from app.domain.schemas import (
    CuOptPayload,
    ExpandedRobotRoute,
    OptimizerResult,
    RouteSegment,
    RouteValidationResult,
    WaypointRouteExpansionResult,
)
from app.services.graph_service import DirectedGraphService


SERVICE_ACCESS_NODE_TYPES = frozenset(
    {
        "rack_access",
        "inbound_handoff_access",
        "outbound_station_access",
    }
)


class WaypointRouteExpander:
    """Expand each robot task sequence over the adjusted directed graph."""

    def expand(self, *, payload: CuOptPayload, result: OptimizerResult) -> WaypointRouteExpansionResult:
        """Return node and edge sequences for every optimizer route."""

        reverse_index = {index: node_id for node_id, index in payload.location_index_map.items()}
        arcs = [
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
        graph = DirectedGraphService(arcs)
        vehicle_starts = {
            robot_id: reverse_index[start]
            for robot_id, start in zip(
                payload.fleet_data.vehicle_ids,
                payload.fleet_data.vehicle_start_locations,
                strict=True,
            )
        }
        task_locations = {
            task_id: reverse_index[location]
            for task_id, location in zip(
                payload.task_data.task_ids,
                payload.task_data.task_locations,
                strict=True,
            )
        }
        expanded: list[ExpandedRobotRoute] = []
        errors: list[str] = []
        for route in result.routes:
            current = vehicle_starts[route.vehicle_id]
            node_sequence = [current]
            segments: list[RouteSegment] = []
            total_cost = 0.0
            total_time = 0
            for task_id in route.task_sequence:
                target = task_locations[task_id]
                cost, path = graph.shortest_path(current, target)
                if not path and current != target:
                    errors.append(f"No directed path from {current} to {target} for {task_id}.")
                    break
                for arc in path:
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
                    total_cost += arc.cost
                    total_time += arc.travel_time_ms
                current = target
            if errors:
                continue
            expanded.append(
                ExpandedRobotRoute(
                    vehicle_id=route.vehicle_id,
                    start_node=vehicle_starts[route.vehicle_id],
                    task_sequence=route.task_sequence,
                    node_sequence=node_sequence,
                    segments=segments,
                    total_cost=round(total_cost, 6),
                    total_travel_time_ms=total_time,
                )
            )
        return WaypointRouteExpansionResult(
            status="failed" if errors else "expanded",
            routes=expanded,
            errors=errors,
        )


class StaticRouteValidator:
    """Validate edge existence, continuity, blocked resources, and route totals."""

    def validate(
        self,
        *,
        payload: CuOptPayload,
        expansion: WaypointRouteExpansionResult,
        node_types: Mapping[str, str] | None = None,
    ) -> RouteValidationResult:
        """Return an independent static route verdict."""

        errors: list[str] = []
        warnings: list[str] = []
        edge_lookup = {
            (edge_id, source, target): (cost, travel)
            for edge_id, source, target, cost, travel in zip(
                payload.waypoint_graph_data.edge_ids,
                payload.waypoint_graph_data.from_indices,
                payload.waypoint_graph_data.to_indices,
                payload.waypoint_graph_data.costs,
                payload.waypoint_graph_data.travel_times_ms,
                strict=True,
            )
        }
        index = payload.location_index_map
        reverse_index = {value: key for key, value in index.items()}
        task_locations = {
            task_id: reverse_index[location]
            for task_id, location in zip(
                payload.task_data.task_ids,
                payload.task_data.task_locations,
                strict=True,
            )
        }
        normalized_node_types = {
            node_id: str(node_type).casefold()
            for node_id, node_type in (node_types or {}).items()
        }
        has_authoritative_node_types = bool(normalized_node_types)
        # ``_ACCESS_`` is no longer exclusive to rack spurs: inbound handoffs
        # and fixed outbound stations also use that canonical naming pattern.
        # Use authoritative node types when available and retain the historical
        # name-based fallback only for older callers that do not provide them.
        rack_access_node_ids = {
            node_id
            for node_id in index
            if (
                normalized_node_types.get(node_id) == "rack_access"
                if has_authoritative_node_types
                else "_ACCESS_" in node_id
            )
        }
        service_access_node_ids = {
            node_id
            for node_id in index
            if (
                normalized_node_types.get(node_id) in SERVICE_ACCESS_NODE_TYPES
                if has_authoritative_node_types
                else "_ACCESS_" in node_id
            )
        }
        access_peers: dict[str, set[str]] = {
            node_id: set() for node_id in rack_access_node_ids
        }
        for source, target in zip(
            payload.waypoint_graph_data.from_indices,
            payload.waypoint_graph_data.to_indices,
            strict=True,
        ):
            source_id = reverse_index[source]
            target_id = reverse_index[target]
            if source_id in access_peers:
                access_peers[source_id].add(target_id)
            if target_id in access_peers:
                access_peers[target_id].add(source_id)
        for access_node_id, peers in access_peers.items():
            if len(peers) != 1:
                errors.append(
                    f"Rack access node {access_node_id} must be a one-neighbour dead-end spur."
                )
            if any(peer in rack_access_node_ids for peer in peers):
                errors.append(
                    f"Rack access node {access_node_id} must not connect to another rack access node."
                )
        blocked_edges = set(payload.applied_map_constraints.blocked_edge_ids)
        blocked_nodes = set(payload.applied_map_constraints.blocked_node_ids)
        if expansion.status != "expanded":
            return RouteValidationResult(valid=False, errors=[*expansion.errors])
        for route in expansion.routes:
            recomputed_cost = 0.0
            recomputed_time = 0
            previous = route.start_node
            task_endpoint_nodes = {
                task_locations[task_id]
                for task_id in route.task_sequence
                if task_id in task_locations
            }
            reported_transit_nodes: set[str] = set()
            for segment_index, segment in enumerate(route.segments):
                if segment.from_node != previous:
                    errors.append(f"{route.vehicle_id}: discontinuity before {segment.edge_id}.")
                key = (segment.edge_id, index[segment.from_node], index[segment.to_node])
                expected = edge_lookup.get(key)
                if expected is None:
                    errors.append(f"{route.vehicle_id}: edge {segment.edge_id} is absent from the adjusted graph.")
                else:
                    if not isclose(expected[0], segment.cost, rel_tol=1e-9, abs_tol=1e-9):
                        errors.append(f"{route.vehicle_id}: cost mismatch on {segment.edge_id}.")
                    if expected[1] != segment.travel_time_ms:
                        errors.append(f"{route.vehicle_id}: travel time mismatch on {segment.edge_id}.")
                for position, node_id in (
                    ("from", segment.from_node),
                    ("to", segment.to_node),
                ):
                    if node_id not in service_access_node_ids:
                        continue
                    is_route_start = (
                        segment_index == 0
                        and position == "from"
                        and node_id == route.start_node
                    )
                    is_task_endpoint = node_id in task_endpoint_nodes
                    if (
                        not is_route_start
                        and not is_task_endpoint
                        and node_id not in reported_transit_nodes
                    ):
                        errors.append(
                            f"{route.vehicle_id}: service access node {node_id} was used as transit."
                        )
                        reported_transit_nodes.add(node_id)
                if segment.edge_id in blocked_edges:
                    errors.append(f"{route.vehicle_id}: blocked edge {segment.edge_id} was used.")
                if segment.from_node in blocked_nodes or segment.to_node in blocked_nodes:
                    errors.append(f"{route.vehicle_id}: blocked node was used.")
                recomputed_cost += segment.cost
                recomputed_time += segment.travel_time_ms
                previous = segment.to_node
            if not isclose(recomputed_cost, route.total_cost, rel_tol=1e-9, abs_tol=1e-9):
                errors.append(f"{route.vehicle_id}: total cost mismatch.")
            if recomputed_time != route.total_travel_time_ms:
                errors.append(f"{route.vehicle_id}: total travel time mismatch.")
        if not expansion.routes:
            warnings.append("No expanded routes were produced.")
        return RouteValidationResult(valid=not errors, errors=errors, warnings=warnings)

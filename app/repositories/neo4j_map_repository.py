"""Neo4j adapter for the route-only warehouse graph projection.

Inventory racks (``K1_7``) remain PostgreSQL/JSON master-data entities.  Neo4j
contains only traversable nodes, including the dead-end service nodes
``K1_7_ACCESS_A`` and ``K1_7_ACCESS_B``.  There is deliberately no relationship
between the two access nodes, so a robot can service a rack from either aisle
side but can never drive through the rack.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from app.core.config import Settings, get_settings
from app.domain.schemas import normalize_warehouse_id


class Neo4jMapRepositoryError(RuntimeError):
    """Raised when the Neo4j route projection violates the map contract."""


@dataclass(frozen=True)
class Neo4jRouteGraphSnapshot:
    """Materialized traversable route graph fetched from Neo4j."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    version: str

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "rack_storage_nodes": 0,
            "rack_access_nodes": sum(
                str(value.get("type")) == "rack_access" for value in self.nodes
            ),
            "inbound_handoff_access_nodes": sum(
                str(value.get("type")) == "inbound_handoff_access" for value in self.nodes
            ),
            "outbound_station_access_nodes": sum(
                str(value.get("type")) == "outbound_station_access" for value in self.nodes
            ),
            "empty_tote_buffer_access_nodes": sum(
                str(value.get("type")) == "empty_tote_buffer_access" for value in self.nodes
            ),
            "routing_projection_excludes_racks": True,
        }


class Neo4jMapRepository:
    """Read and load the Neo4j route projection using the official driver."""

    def __init__(
        self,
        *,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        driver: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.uri = uri or cfg.neo4j_uri
        self.username = username or cfg.neo4j_username
        self.password = password if password is not None else cfg.neo4j_password
        self.database = database or cfg.neo4j_database
        self._driver = driver
        self._owns_driver = driver is None

    def _ensure_driver(self) -> Any:
        if self._driver is not None:
            return self._driver
        try:
            from neo4j import GraphDatabase
        except Exception as exc:  # pragma: no cover - optional dependency boundary
            raise Neo4jMapRepositoryError(
                "Neo4j backend requires the official neo4j Python driver. "
                "Install requirements.txt or set MAP_REPOSITORY_BACKEND=json."
            ) from exc
        if not self.password:
            raise Neo4jMapRepositoryError(
                "NEO4J_PASSWORD is required when MAP_REPOSITORY_BACKEND=neo4j."
            )
        self._driver = GraphDatabase.driver(
            self.uri,
            auth=(self.username, self.password),
        )
        return self._driver

    def close(self) -> None:
        if self._driver is not None and self._owns_driver:
            self._driver.close()
        self._driver = None


    def ping(self) -> dict[str, Any]:
        """Verify connectivity without referencing labels before initial seed.

        Querying ``:RouteNode``/``:TRAVERSES`` on a brand-new database produces
        noisy UnknownLabel warnings even though the connection is healthy. Route
        counts belong to the post-seed graph contract check, not to connectivity.
        """

        started = time.perf_counter()
        driver = self._ensure_driver()
        with driver.session(database=self.database) as session:
            row = session.run("RETURN 1 AS ok").single()
        return {
            "ok": bool(row and int(row["ok"]) == 1),
            "database": self.database,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    def graph_counts(self, warehouse_id: str | None = None) -> dict[str, int]:
        """Return route projection counts for one warehouse after seed."""

        warehouse_id = normalize_warehouse_id(
            warehouse_id or get_settings().default_warehouse_id
        )
        driver = self._ensure_driver()
        with driver.session(database=self.database) as session:
            row = session.run(
                "MATCH (n:RouteNode {warehouse_id: $warehouse_id}) "
                "WITH count(n) AS nodes "
                "OPTIONAL MATCH (:RouteNode {warehouse_id: $warehouse_id})"
                "-[r:TRAVERSES {warehouse_id: $warehouse_id}]->"
                "(:RouteNode {warehouse_id: $warehouse_id}) "
                "RETURN nodes, count(r) AS edges",
                warehouse_id=warehouse_id,
            ).single()
        return {
            "nodes": int(row["nodes"] if row else 0),
            "edges": int(row["edges"] if row else 0),
        }

    def roundtrip(
        self, probe_id: str, warehouse_id: str | None = None
    ) -> dict[str, Any]:
        """Create/read/delete one disposable warehouse-scoped node."""

        wid = normalize_warehouse_id(
            warehouse_id or get_settings().default_warehouse_id
        )
        driver = self._ensure_driver()
        scope_id = f"{wid}::{probe_id}"
        with driver.session(database=self.database) as session:
            record = session.run(
                """
                MERGE (n:LaroRoundtrip {scope_id: $scope_id})
                SET n.probe_id=$probe_id, n.warehouse_id=$warehouse_id
                RETURN properties(n) AS value
                """,
                scope_id=scope_id,
                probe_id=probe_id,
                warehouse_id=wid,
            ).single()
            session.run(
                "MATCH (n:LaroRoundtrip {scope_id: $scope_id}) DELETE n",
                scope_id=scope_id,
            ).consume()
        return {"probe_id": probe_id, "warehouse_id": wid, "payload": dict(record["value"])}

    @staticmethod
    def validate_snapshot(
        nodes: Iterable[dict[str, Any]],
        edges: Iterable[dict[str, Any]],
    ) -> None:
        """Reject rack transit nodes and malformed rack-access topology."""

        node_list = [dict(value) for value in nodes]
        edge_list = [dict(value) for value in edges]
        node_by_id = {str(value["id"]): value for value in node_list}
        if len(node_by_id) != len(node_list):
            raise Neo4jMapRepositoryError("Neo4j route graph contains duplicate node IDs.")
        edge_by_id = {str(value["id"]): value for value in edge_list}
        if len(edge_by_id) != len(edge_list):
            raise Neo4jMapRepositoryError("Neo4j route graph contains duplicate edge IDs.")
        for node_id, node in node_by_id.items():
            # A bare K#_# identifier is inventory master data and must never be
            # part of the traversable projection.
            if node_id.startswith("K") and "_ACCESS_" not in node_id:
                raise Neo4jMapRepositoryError(
                    f"Rack entity {node_id} must not exist in the Neo4j route projection."
                )
            if node.get("type") == "rack_storage":
                raise Neo4jMapRepositoryError(
                    f"Legacy rack_storage node {node_id} is not traversable."
                )
        incident: dict[str, list[dict[str, Any]]] = {value: [] for value in node_by_id}
        for edge in edge_list:
            source = str(edge["source"])
            target = str(edge["target"])
            if source not in node_by_id or target not in node_by_id:
                raise Neo4jMapRepositoryError(
                    f"Edge {edge['id']} references an unknown route node."
                )
            incident[source].append(edge)
            incident[target].append(edge)
        service_access_types = {
            "rack_access",
            "inbound_handoff_access",
            "outbound_station_access",
            "empty_tote_buffer_access",
        }
        for node_id, node in node_by_id.items():
            node_type = str(node.get("type") or "")
            if node_type not in service_access_types:
                continue
            if node.get("service_only") is not True or node.get("transit_allowed") is not False:
                raise Neo4jMapRepositoryError(
                    f"Service access node {node_id} must be service_only with transit_allowed=false."
                )
            if node_type == "rack_access" and not str(node.get("rack_id") or ""):
                raise Neo4jMapRepositoryError(f"Rack access node {node_id} is missing rack_id.")
            if node_type == "inbound_handoff_access" and not str(node.get("handoff_id") or ""):
                raise Neo4jMapRepositoryError(f"Inbound access node {node_id} is missing handoff_id.")
            if node_type == "outbound_station_access" and not str(node.get("station_id") or ""):
                raise Neo4jMapRepositoryError(f"Station access node {node_id} is missing station_id.")
            if node_type == "empty_tote_buffer_access" and not str(node.get("buffer_id") or ""):
                raise Neo4jMapRepositoryError(f"Empty-tote access node {node_id} is missing buffer_id.")
            peers = {
                str(edge["target"] if str(edge["source"]) == node_id else edge["source"])
                for edge in incident[node_id]
            }
            if len(peers) != 1:
                raise Neo4jMapRepositoryError(
                    f"Service access node {node_id} must be a one-neighbour dead-end spur."
                )
            peer = next(iter(peers))
            if str(node_by_id[peer].get("type") or "") in service_access_types:
                raise Neo4jMapRepositoryError(
                    f"Service access node {node_id} must not connect to another service access node."
                )

    @staticmethod
    def _version(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            {"nodes": nodes, "edges": edges},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def fetch_route_graph(
        self, warehouse_id: str | None = None
    ) -> Neo4jRouteGraphSnapshot:
        """Fetch one warehouse's route nodes and directed edges."""

        warehouse_id = normalize_warehouse_id(
            warehouse_id or get_settings().default_warehouse_id
        )
        driver = self._ensure_driver()
        node_query = """
        MATCH (n:RouteNode {warehouse_id: $warehouse_id})
        RETURN properties(n) AS value
        ORDER BY n.id
        """
        edge_query = """
        MATCH (a:RouteNode {warehouse_id: $warehouse_id})
              -[r:TRAVERSES {warehouse_id: $warehouse_id}]->
              (b:RouteNode {warehouse_id: $warehouse_id})
        RETURN r{.*, source: a.id, target: b.id} AS value
        ORDER BY r.id
        """
        with driver.session(database=self.database) as session:
            nodes = [
                dict(record["value"])
                for record in session.run(node_query, warehouse_id=warehouse_id)
            ]
            edges = [
                dict(record["value"])
                for record in session.run(edge_query, warehouse_id=warehouse_id)
            ]
        self.validate_snapshot(nodes, edges)
        return Neo4jRouteGraphSnapshot(
            nodes=nodes,
            edges=edges,
            version=self._version(nodes, edges),
        )

    def load_route_graph(
        self,
        *,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        warehouse_id: str | None = None,
        replace: bool = True,
    ) -> Neo4jRouteGraphSnapshot:
        """Load one validated route-only projection into Neo4j."""

        warehouse_id = normalize_warehouse_id(
            warehouse_id or get_settings().default_warehouse_id
        )
        self.validate_snapshot(nodes, edges)
        driver = self._ensure_driver()
        clean_nodes = [
            {
                **dict(value),
                "warehouse_id": warehouse_id,
                "scope_id": f"{warehouse_id}::{value['id']}",
            }
            for value in nodes
        ]
        clean_edges = [
            {
                **dict(value),
                "warehouse_id": warehouse_id,
                "scope_id": f"{warehouse_id}::{value['id']}",
            }
            for value in edges
        ]
        with driver.session(database=self.database) as session:
            session.run(
                "CREATE CONSTRAINT route_node_scope_id IF NOT EXISTS "
                "FOR (n:RouteNode) REQUIRE n.scope_id IS UNIQUE"
            ).consume()
            session.run(
                "CREATE INDEX route_node_type IF NOT EXISTS "
                "FOR (n:RouteNode) ON (n.type)"
            ).consume()
            session.run(
                "CREATE INDEX route_node_warehouse IF NOT EXISTS "
                "FOR (n:RouteNode) ON (n.warehouse_id)"
            ).consume()
            session.run(
                "CREATE INDEX rack_access_rack_id IF NOT EXISTS "
                "FOR (n:RackAccess) ON (n.rack_id)"
            ).consume()
            if replace:
                session.run(
                    "MATCH (n:RouteNode {warehouse_id: $warehouse_id}) DETACH DELETE n",
                    warehouse_id=warehouse_id,
                ).consume()
            service_access_types = {
                "rack_access",
                "inbound_handoff_access",
                "outbound_station_access",
                "empty_tote_buffer_access",
            }
            generic_nodes = [value for value in clean_nodes if value.get("type") not in service_access_types]
            access_nodes = [value for value in clean_nodes if value.get("type") in service_access_types]
            if generic_nodes:
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (n:RouteNode {scope_id: row.scope_id})
                    SET n += row
                    """,
                    rows=generic_nodes,
                ).consume()
            if access_nodes:
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (n:RouteNode:ServiceAccess {scope_id: row.scope_id})
                    SET n += row
                    FOREACH (_ IN CASE WHEN row.type = 'rack_access' THEN [1] ELSE [] END | SET n:RackAccess)
                    FOREACH (_ IN CASE WHEN row.type = 'inbound_handoff_access' THEN [1] ELSE [] END | SET n:InboundHandoffAccess)
                    FOREACH (_ IN CASE WHEN row.type = 'outbound_station_access' THEN [1] ELSE [] END | SET n:OutboundStationAccess)
                    FOREACH (_ IN CASE WHEN row.type = 'empty_tote_buffer_access' THEN [1] ELSE [] END | SET n:EmptyToteBufferAccess)
                    """,
                    rows=access_nodes,
                ).consume()
            if clean_edges:
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:RouteNode {scope_id: row.warehouse_id + '::' + row.source})
                    MATCH (b:RouteNode {scope_id: row.warehouse_id + '::' + row.target})
                    MERGE (a)-[r:TRAVERSES {scope_id: row.scope_id}]->(b)
                    SET r += row
                    """,
                    rows=clean_edges,
                ).consume()
        return Neo4jRouteGraphSnapshot(
            nodes=clean_nodes,
            edges=clean_edges,
            version=self._version(clean_nodes, clean_edges),
        )


class Neo4jWarehouseRepositoryMixin:
    """Mixin that replaces only the route graph of a JSON-backed repository."""

    _neo4j_graph_version: str

    def _replace_route_graph_from_neo4j(self) -> None:
        adapter = Neo4jMapRepository()
        try:
            try:
                snapshot = adapter.fetch_route_graph(self.warehouse_id)
            except TypeError:
                # Backward-compatible with test doubles and v13.18 adapters
                # that expose fetch_route_graph() without a warehouse argument.
                snapshot = adapter.fetch_route_graph()
        finally:
            adapter.close()
        self.nodes = {str(value["id"]): dict(value) for value in snapshot.nodes}
        self.edges = {str(value["id"]): dict(value) for value in snapshot.edges}
        self.graph = {
            "title": "Neo4j route-only warehouse projection",
            "routing_model": {
                "rack_entities_in_route_graph": False,
                "rack_access_node_type": "rack_access",
                "access_nodes_are_service_only": True,
                "rack_through_travel_allowed": False,
            },
            "summary": snapshot.summary,
            "nodes": snapshot.nodes,
            "edges": snapshot.edges,
        }
        self._neo4j_graph_version = snapshot.version
        # Reuse the base repository's inventory/order/robot/runtime reference
        # checks against the newly loaded route projection.
        self._validate_references()

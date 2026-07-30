"""Persistence adapter for the unmodified Spring BE compatibility endpoints.

The adapter follows an additive, single-source-of-truth contract:

* In shared-DB mode it first reads Spring's existing ``public.warehouse_node``
  and ``public.warehouse_edge`` tables.
* If those tables are not available yet, ``POST /optimize`` stores the received
  graph in normalized ``laro_contract.route_node`` / ``route_edge`` tables.
* Redis stores graph metadata by default, not a second full copy of the static
  graph.  Full Redis graph caching is an explicit opt-in.
* Neo4j is a disposable route projection, never the business-data writer.

The original Spring source code and its JPA entities remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import Settings, get_settings
from app.domain.be_compat import BeEdgeInput, BeGraphSnapshot, BeNodeInput
from app.infrastructure.manager import get_infrastructure_manager

LOGGER = logging.getLogger(__name__)


class BeCompatGraphNotFoundError(LookupError):
    """Raised when reoptimization has no usable graph for the warehouse."""


class BeCompatSpringGraphUnavailableError(LookupError):
    """Raised when spring_db is mandatory but the Spring graph is unavailable."""


class BeCompatRepository:
    """Read Spring graph tables and maintain a normalized compatibility fallback."""

    _memory_graphs: dict[int, BeGraphSnapshot] = {}
    _memory_sources: dict[int, str] = {}
    _memory_lock = Lock()

    def __init__(
        self,
        settings: Settings | None = None,
        manager: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.manager = manager or get_infrastructure_manager()
        self._schema_ready = False
        self._schema_lock = Lock()
        self.last_graph_source: str | None = None

    @property
    def live_enabled(self) -> bool:
        return self.settings.warehouse_repository_backend == "live"

    @staticmethod
    def graph_version(nodes: list[BeNodeInput], edges: list[BeEdgeInput]) -> str:
        payload = {
            "nodes": [
                value.model_dump(by_alias=True, mode="json")
                for value in sorted(nodes, key=lambda item: item.node_id)
            ],
            "edges": [
                value.model_dump(by_alias=True, mode="json")
                for value in sorted(edges, key=lambda item: item.edge_id)
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def ensure_schema(self) -> None:
        if (
            not self.live_enabled
            or not self.settings.be_compat_contract_schema_enabled
            or self._schema_ready
        ):
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            root = Path(__file__).resolve().parents[2] / "db" / "postgres"
            # Fresh v2 installations create only the additive ``laro_contract``
            # schema.  The v1 public ``be_compat_*`` tables are still readable
            # when they already exist, but are not created again because that
            # would reintroduce a duplicate graph/request store.
            self.manager.postgres.apply_schema(root / "003_be_shared_contract.sql")
            self._schema_ready = True

    @staticmethod
    def _snapshot_payload(snapshot: BeGraphSnapshot) -> dict[str, Any]:
        return snapshot.model_dump(by_alias=True, mode="json")

    def _redis_meta_key(self, warehouse_id: int) -> str:
        return self.manager.redis._key(
            "be_compat", "warehouse", str(warehouse_id), "graph", "meta"
        )

    def _redis_full_key(self, warehouse_id: int) -> str:
        return self.manager.redis._key(
            "be_compat", "warehouse", str(warehouse_id), "graph", "snapshot"
        )

    @staticmethod
    def _relation_exists(conn: Any, qualified_name: str) -> bool:
        row = conn.execute("SELECT to_regclass(%s) AS relation", (qualified_name,)).fetchone()
        return bool(row and row.get("relation"))

    def _spring_graph_tables_available(self, conn: Any) -> bool:
        return all(
            self._relation_exists(conn, name)
            for name in (
                "public.warehouse_layout",
                "public.warehouse_node",
                "public.warehouse_edge",
            )
        )

    def _load_spring_graph(self, warehouse_id: int) -> BeGraphSnapshot | None:
        if not self.live_enabled:
            return None
        self.ensure_schema()
        with self.manager.postgres._connection() as conn:
            if not self._spring_graph_tables_available(conn):
                return None
            node_rows = conn.execute(
                """
                SELECT node_id, x, y
                FROM public.warehouse_node
                WHERE warehouse_id = %s
                ORDER BY node_id
                """,
                (warehouse_id,),
            ).fetchall()
            if not node_rows:
                return None
            edge_rows = conn.execute(
                """
                SELECT e.edge_id,
                       e.from_node_id,
                       e.to_node_id,
                       COALESCE(e.distance, 0)::double precision AS distance,
                       e.direction_type::text AS direction_type
                FROM public.warehouse_edge e
                JOIN public.warehouse_node fn ON fn.node_id = e.from_node_id
                JOIN public.warehouse_node tn ON tn.node_id = e.to_node_id
                WHERE fn.warehouse_id = %s
                  AND tn.warehouse_id = %s
                ORDER BY e.edge_id
                """,
                (warehouse_id, warehouse_id),
            ).fetchall()
        nodes = [
            BeNodeInput(
                nodeId=int(row["node_id"]),
                x=float(row["x"]) if row.get("x") is not None else None,
                y=float(row["y"]) if row.get("y") is not None else None,
            )
            for row in node_rows
        ]
        edges = [
            BeEdgeInput(
                edgeId=int(row["edge_id"]),
                fromNodeId=int(row["from_node_id"]),
                toNodeId=int(row["to_node_id"]),
                distance=float(row["distance"]),
                directionType=str(row["direction_type"]),
            )
            for row in edge_rows
        ]
        return BeGraphSnapshot(
            warehouseId=warehouse_id,
            graphVersion=self.graph_version(nodes, edges),
            nodes=nodes,
            edges=edges,
        )

    def _load_contract_graph(self, warehouse_id: int) -> BeGraphSnapshot | None:
        if not self.live_enabled:
            return None
        self.ensure_schema()
        with self.manager.postgres._connection() as conn:
            node_rows = conn.execute(
                """
                SELECT node_id, x, y, graph_version
                FROM laro_contract.route_node
                WHERE warehouse_id = %s AND active = true
                ORDER BY node_id
                """,
                (warehouse_id,),
            ).fetchall()
            if not node_rows:
                return None
            edge_rows = conn.execute(
                """
                SELECT edge_id, from_node_id, to_node_id,
                       distance_m AS distance, direction_type, graph_version
                FROM laro_contract.route_edge
                WHERE warehouse_id = %s
                  AND active = true
                  AND mobile_robot_traversable = true
                ORDER BY edge_id
                """,
                (warehouse_id,),
            ).fetchall()
        nodes = [
            BeNodeInput(
                nodeId=int(row["node_id"]),
                x=float(row["x"]) if row.get("x") is not None else None,
                y=float(row["y"]) if row.get("y") is not None else None,
            )
            for row in node_rows
        ]
        edges = [
            BeEdgeInput(
                edgeId=int(row["edge_id"]),
                fromNodeId=int(row["from_node_id"]),
                toNodeId=int(row["to_node_id"]),
                distance=float(row["distance"]),
                directionType=str(row["direction_type"]),
            )
            for row in edge_rows
        ]
        version = str(node_rows[0].get("graph_version") or self.graph_version(nodes, edges))
        return BeGraphSnapshot(
            warehouseId=warehouse_id,
            graphVersion=version,
            nodes=nodes,
            edges=edges,
        )

    def _load_legacy_snapshot(self, warehouse_id: int) -> BeGraphSnapshot | None:
        if not self.live_enabled:
            return None
        self.ensure_schema()
        with self.manager.postgres._connection() as conn:
            if not self._relation_exists(conn, "public.be_compat_graph_snapshots"):
                return None
            row = conn.execute(
                """
                SELECT warehouse_id, graph_version, nodes_json, edges_json
                FROM public.be_compat_graph_snapshots
                WHERE warehouse_id = %s
                """,
                (warehouse_id,),
            ).fetchone()
        if not row:
            return None
        return BeGraphSnapshot.model_validate(
            {
                "warehouseId": int(row["warehouse_id"]),
                "graphVersion": str(row["graph_version"]),
                "nodes": row["nodes_json"],
                "edges": row["edges_json"],
            }
        )

    def _load_binding(self, warehouse_id: int) -> dict[str, Any] | None:
        if not self.live_enabled:
            return None
        self.ensure_schema()
        with self.manager.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT warehouse_id, graph_version, graph_source
                FROM laro_contract.warehouse_binding
                WHERE warehouse_id = %s AND active = true
                """,
                (warehouse_id,),
            ).fetchone()
        return dict(row) if row else None

    def _upsert_binding(
        self, snapshot: BeGraphSnapshot, *, source: str
    ) -> None:
        self.ensure_schema()
        normalized_source = (
            "spring_db" if source == "spring_db" else "contract"
        )
        with self.manager.postgres._connection() as conn:
            conn.execute(
                """
                INSERT INTO laro_contract.warehouse_binding(
                    warehouse_id, warehouse_code, graph_version,
                    graph_source, updated_at
                ) VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (warehouse_id) DO UPDATE SET
                    graph_version = EXCLUDED.graph_version,
                    graph_source = EXCLUDED.graph_source,
                    updated_at = now()
                """,
                (
                    snapshot.warehouse_id,
                    f"WH-{snapshot.warehouse_id:03d}",
                    snapshot.graph_version,
                    normalized_source,
                ),
            )
            conn.commit()

    def _save_contract_graph(self, snapshot: BeGraphSnapshot, *, source: str) -> None:
        self.ensure_schema()
        speed = self.settings.be_compat_robot_speed_distance_per_second
        with self.manager.postgres._connection() as conn:
            # Serialize writes per warehouse and commit graph rows + binding in one
            # transaction.  This prevents two concurrent /optimize calls from
            # leaving a binding that points at the other request's graph version.
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (snapshot.warehouse_id,),
            )
            conn.execute(
                "DELETE FROM laro_contract.route_edge WHERE warehouse_id = %s",
                (snapshot.warehouse_id,),
            )
            conn.execute(
                "DELETE FROM laro_contract.route_node WHERE warehouse_id = %s",
                (snapshot.warehouse_id,),
            )
            for node in snapshot.nodes:
                conn.execute(
                    """
                    INSERT INTO laro_contract.route_node(
                        warehouse_id, node_id, node_code, x, y,
                        semantic_type, service_only, transit_allowed,
                        active, graph_version, updated_at
                    ) VALUES (%s, %s, %s, %s, %s,
                              'ROUTE', false, true, true, %s, now())
                    """,
                    (
                        snapshot.warehouse_id,
                        node.node_id,
                        f"N{node.node_id}",
                        node.x,
                        node.y,
                        snapshot.graph_version,
                    ),
                )
            for edge in snapshot.edges:
                travel_ms = int(round((edge.distance / speed) * 1000))
                conn.execute(
                    """
                    INSERT INTO laro_contract.route_edge(
                        warehouse_id, edge_id, from_node_id, to_node_id,
                        direction_type, edge_code, edge_type, distance_m,
                        speed_limit_mps, nominal_travel_time_ms, base_cost,
                        physical_resource_code, service_only,
                        mobile_robot_traversable, active, version,
                        graph_version, updated_at
                    ) VALUES (%s, %s, %s, %s,
                              %s, %s, 'ROUTE', %s,
                              %s, %s, %s,
                              %s, false, true, true, 1,
                              %s, now())
                    """,
                    (
                        snapshot.warehouse_id,
                        edge.edge_id,
                        edge.from_node_id,
                        edge.to_node_id,
                        edge.direction_type,
                        f"E{edge.edge_id}",
                        edge.distance,
                        speed,
                        travel_ms,
                        edge.distance,
                        f"EDGE:{min(edge.from_node_id, edge.to_node_id)}<->{max(edge.from_node_id, edge.to_node_id)}",
                        snapshot.graph_version,
                    ),
                )
            conn.execute(
                """
                INSERT INTO laro_contract.warehouse_binding(
                    warehouse_id, warehouse_code, graph_version,
                    graph_source, updated_at
                ) VALUES (%s, %s, %s, 'contract', now())
                ON CONFLICT (warehouse_id) DO UPDATE SET
                    graph_version = EXCLUDED.graph_version,
                    graph_source = EXCLUDED.graph_source,
                    updated_at = now()
                """,
                (
                    snapshot.warehouse_id,
                    f"WH-{snapshot.warehouse_id:03d}",
                    snapshot.graph_version,
                ),
            )
            conn.commit()

    def _cache_graph(self, snapshot: BeGraphSnapshot, *, source: str) -> None:
        mode = self.settings.be_compat_graph_cache_mode
        if not self.live_enabled or mode == "off":
            return
        metadata = {
            "warehouseId": snapshot.warehouse_id,
            "graphVersion": snapshot.graph_version,
            "nodeCount": len(snapshot.nodes),
            "edgeCount": len(snapshot.edges),
            "source": source,
        }
        self.manager.redis.client.set(
            self._redis_meta_key(snapshot.warehouse_id),
            json.dumps(metadata, ensure_ascii=False),
            ex=self.settings.be_compat_graph_cache_ttl_seconds,
        )
        if mode == "full":
            self.manager.redis.client.set(
                self._redis_full_key(snapshot.warehouse_id),
                json.dumps(self._snapshot_payload(snapshot), ensure_ascii=False),
                ex=self.settings.be_compat_graph_cache_ttl_seconds,
            )

    def _load_full_redis_cache(self, warehouse_id: int) -> BeGraphSnapshot | None:
        if not self.live_enabled or self.settings.be_compat_graph_cache_mode != "full":
            return None
        raw = self.manager.redis.client.get(self._redis_full_key(warehouse_id))
        return BeGraphSnapshot.model_validate_json(raw) if raw else None

    def save_graph(
        self,
        *,
        warehouse_id: int,
        nodes: list[BeNodeInput],
        edges: list[BeEdgeInput],
    ) -> BeGraphSnapshot:
        request_snapshot = BeGraphSnapshot(
            warehouseId=warehouse_id,
            graphVersion=self.graph_version(nodes, edges),
            nodes=nodes,
            edges=edges,
        )
        chosen = request_snapshot
        source = "request_snapshot"
        contract_binding_saved = False

        if self.live_enabled:
            self.ensure_schema()
            mode = self.settings.be_compat_graph_source
            spring_snapshot = (
                self._load_spring_graph(warehouse_id)
                if mode in {"auto", "spring_db"}
                else None
            )
            if spring_snapshot is not None and (
                spring_snapshot.graph_version == request_snapshot.graph_version
                or mode == "spring_db"
            ):
                chosen = spring_snapshot
                source = "spring_db"
            elif mode == "spring_db":
                raise BeCompatSpringGraphUnavailableError(
                    "BE_COMPAT_GRAPH_SOURCE=spring_db but Spring warehouse_node/warehouse_edge "
                    f"data is unavailable for warehouseId={warehouse_id}."
                )
            else:
                # A request graph mismatch is kept in the additive contract instead
                # of overwriting Spring business tables.
                self._save_contract_graph(request_snapshot, source="request_snapshot")
                chosen = request_snapshot
                source = "contract"
                contract_binding_saved = True

            if not contract_binding_saved:
                self._upsert_binding(chosen, source=source)
            self._cache_graph(chosen, source=source)
            if self.settings.be_compat_neo4j_projection:
                try:
                    self._project_to_neo4j(chosen)
                except Exception as exc:  # projection does not invalidate the route
                    LOGGER.warning("BE compatibility Neo4j projection failed: %s", exc)

        with self._memory_lock:
            self._memory_graphs[warehouse_id] = chosen
            self._memory_sources[warehouse_id] = source
        self.last_graph_source = source
        return chosen

    def load_graph(self, warehouse_id: int) -> tuple[BeGraphSnapshot | None, str | None]:
        mode = self.settings.be_compat_graph_source
        if self.live_enabled:
            self.ensure_schema()
            binding = self._load_binding(warehouse_id)
            bound_source = str(binding.get("graph_source")) if binding else None
            bound_version = str(binding.get("graph_version")) if binding and binding.get("graph_version") else None

            # When /optimize detected a Spring/request mismatch, the binding is
            # set to contract so /reoptimize cannot silently switch graphs.
            if bound_source == "contract":
                contract = self._load_contract_graph(warehouse_id)
                if contract is not None and (
                    bound_version is None or contract.graph_version == bound_version
                ):
                    self.last_graph_source = "contract"
                    return contract, "contract"

            if mode in {"auto", "spring_db"} and bound_source != "contract":
                spring = self._load_spring_graph(warehouse_id)
                if spring is not None and (
                    bound_version is None
                    or bound_source != "spring_db"
                    or spring.graph_version == bound_version
                ):
                    self.last_graph_source = "spring_db"
                    return spring, "spring_db"
                if mode == "spring_db":
                    return None, None

            if mode in {"auto", "contract", "request_snapshot"}:
                contract = self._load_contract_graph(warehouse_id)
                if contract is not None:
                    self.last_graph_source = "contract"
                    return contract, "contract"
            legacy = self._load_legacy_snapshot(warehouse_id)
            if legacy is not None:
                self.last_graph_source = "legacy_postgres"
                return legacy, "legacy_postgres"
            redis_snapshot = self._load_full_redis_cache(warehouse_id)
            if redis_snapshot is not None:
                self.last_graph_source = "redis"
                return redis_snapshot, "redis"

        with self._memory_lock:
            cached = self._memory_graphs.get(warehouse_id)
            source = self._memory_sources.get(warehouse_id, "memory")
        if cached is not None:
            self.last_graph_source = source
            return cached, source
        return None, None

    def require_graph(self, warehouse_id: int) -> BeGraphSnapshot:
        snapshot, _ = self.load_graph(warehouse_id)
        if snapshot is None:
            raise BeCompatGraphNotFoundError(
                "No graph is available for warehouseId="
                f"{warehouse_id}. Start Spring against the shared PostgreSQL database, "
                "or call POST /optimize once so LARO can populate laro_contract.route_node/route_edge."
            )
        return snapshot

    def graph_status(self, warehouse_id: int) -> tuple[BeGraphSnapshot | None, str | None]:
        return self.load_graph(warehouse_id)

    def contract_status(self) -> dict[str, Any]:
        if not self.live_enabled:
            return {
                "ready": True,
                "spring_tables_available": False,
                "tables": [],
                "mode": "memory",
            }
        self.ensure_schema()
        names = [
            "laro_contract.warehouse_binding",
            "laro_contract.route_node",
            "laro_contract.route_edge",
            "laro_contract.rack",
            "laro_contract.rack_slot",
            "laro_contract.handling_unit",
            "laro_contract.outbound_order",
            "laro_contract.inbound_receipt",
            "laro_contract.facility",
            "laro_contract.request_log",
        ]
        with self.manager.postgres._connection() as conn:
            available = [name for name in names if self._relation_exists(conn, name)]
            spring = self._spring_graph_tables_available(conn)
        return {
            "ready": len(available) == len(names),
            "spring_tables_available": spring,
            "tables": available,
            "mode": "live",
        }

    def record_run(
        self,
        *,
        request_id: str,
        request_type: str,
        warehouse_id: int,
        simulation_run_id: int | None,
        status: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        runtime_source: str | None = None,
    ) -> None:
        if not self.live_enabled:
            return
        try:
            self.ensure_schema()
            with self.manager.postgres._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO laro_contract.request_log(
                        request_id, request_type, warehouse_id, simulation_run_id,
                        graph_source, runtime_source, status, request_json, response_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (request_id) DO UPDATE SET
                        graph_source = EXCLUDED.graph_source,
                        runtime_source = EXCLUDED.runtime_source,
                        status = EXCLUDED.status,
                        response_json = EXCLUDED.response_json
                    """,
                    (
                        request_id,
                        request_type,
                        warehouse_id,
                        simulation_run_id,
                        self.last_graph_source,
                        runtime_source,
                        status,
                        json.dumps(request_payload, ensure_ascii=False),
                        json.dumps(response_payload, ensure_ascii=False),
                    ),
                )
                conn.commit()
        except Exception as exc:  # audit failure must not invalidate a valid route
            LOGGER.warning("BE compatibility request audit failed: %s", exc)

    @staticmethod
    def _directed_edges(snapshot: BeGraphSnapshot) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for edge in snapshot.edges:
            common = {
                "numeric_edge_id": edge.edge_id,
                "distance_m": edge.distance,
                "direction_type": edge.direction_type,
                "physical_resource_code": (
                    f"EDGE:{min(edge.from_node_id, edge.to_node_id)}"
                    f"<->{max(edge.from_node_id, edge.to_node_id)}"
                ),
            }
            if edge.direction_type in {"BOTH", "A_TO_B"}:
                result.append(
                    {
                        **common,
                        "id": f"BE-{snapshot.warehouse_id}-{edge.edge_id}-F",
                        "from_node_id": edge.from_node_id,
                        "to_node_id": edge.to_node_id,
                    }
                )
            if edge.direction_type in {"BOTH", "B_TO_A"}:
                result.append(
                    {
                        **common,
                        "id": f"BE-{snapshot.warehouse_id}-{edge.edge_id}-R",
                        "from_node_id": edge.to_node_id,
                        "to_node_id": edge.from_node_id,
                    }
                )
        return result

    def _project_to_neo4j(self, snapshot: BeGraphSnapshot) -> None:
        driver = self.manager.neo4j._ensure_driver()
        warehouse_text = str(snapshot.warehouse_id)
        nodes = [
            {
                "numeric_node_id": value.node_id,
                "id": str(value.node_id),
                "scope_id": f"BE::{snapshot.warehouse_id}::{value.node_id}",
                "x": value.x,
                "y": value.y,
            }
            for value in snapshot.nodes
        ]
        edges = self._directed_edges(snapshot)
        with driver.session(database=self.manager.neo4j.database) as session:
            session.run(
                "MATCH (n:BECompatNode {warehouse_id: $warehouse_id}) DETACH DELETE n",
                warehouse_id=warehouse_text,
            ).consume()
            session.run(
                """
                UNWIND $nodes AS node
                CREATE (:RouteNode:BECompatNode {
                    warehouse_id: $warehouse_id,
                    id: node.id,
                    scope_id: node.scope_id,
                    numeric_node_id: node.numeric_node_id,
                    node_type: 'route',
                    x: node.x,
                    y: node.y,
                    service_only: false,
                    transit_allowed: true,
                    graph_version: $graph_version
                })
                """,
                warehouse_id=warehouse_text,
                graph_version=snapshot.graph_version,
                nodes=nodes,
            ).consume()
            session.run(
                """
                UNWIND $edges AS edge
                MATCH (a:BECompatNode {
                    warehouse_id: $warehouse_id,
                    numeric_node_id: edge.from_node_id
                })
                MATCH (b:BECompatNode {
                    warehouse_id: $warehouse_id,
                    numeric_node_id: edge.to_node_id
                })
                CREATE (a)-[:TRAVERSES {
                    warehouse_id: $warehouse_id,
                    id: edge.id,
                    numeric_edge_id: edge.numeric_edge_id,
                    edge_type: 'route',
                    distance_m: edge.distance_m,
                    cost: edge.distance_m,
                    physical_resource_code: edge.physical_resource_code,
                    direction_type: edge.direction_type,
                    graph_version: $graph_version
                }]->(b)
                """,
                warehouse_id=warehouse_text,
                graph_version=snapshot.graph_version,
                edges=edges,
            ).consume()

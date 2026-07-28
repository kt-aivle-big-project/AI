from typing import Any

from neo4j import GraphDatabase, RoutingControl


class Neo4jRepository:
    """고정 창고 지도와 연결 관계를 담당합니다."""

    def __init__(self, uri: str, user: str, password: str, database: str):
        if not uri or not password:
            raise RuntimeError("NEO4J_URI와 NEO4J_PASSWORD가 필요합니다.")
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def healthcheck(self) -> dict[str, Any]:
        self.driver.verify_connectivity()
        return {"ok": True, "database": self.database}

    def close(self) -> None:
        self.driver.close()

    def ensure_constraints(self) -> None:
        statements = [
            (
                "CREATE CONSTRAINT warehouse_id_unique IF NOT EXISTS "
                "FOR (w:Warehouse) REQUIRE w.warehouse_id IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT map_node_unique IF NOT EXISTS "
                "FOR (n:MapNode) REQUIRE (n.warehouse_id, n.node_id) IS UNIQUE"
            ),
            (
                "CREATE INDEX map_node_type IF NOT EXISTS "
                "FOR (n:MapNode) ON (n.warehouse_id, n.node_type)"
            ),
        ]
        for statement in statements:
            self.driver.execute_query(statement, database_=self.database)

    def upsert_map(
        self,
        warehouse_id: int,
        warehouse_name: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> dict[str, int]:
        self.ensure_constraints()
        self.driver.execute_query(
            """
            MERGE (w:Warehouse {warehouse_id: $warehouse_id})
            SET w.name = $warehouse_name
            WITH w
            UNWIND $nodes AS row
            MERGE (z:Zone {warehouse_id: $warehouse_id, zone_id: row.zone_id})
            MERGE (w)-[:HAS_ZONE]->(z)
            MERGE (n:MapNode {warehouse_id: $warehouse_id, node_id: row.node_id})
            SET n.node_type = row.node_type,
                n.x = row.x,
                n.y = row.y,
                n.active = coalesce(row.active, true),
                n.idle_allowed = coalesce(
                    row.idle_allowed,
                    n.idle_allowed,
                    false
                ),
                n.idle_capacity = coalesce(
                    row.idle_capacity,
                    row.parking_capacity,
                    n.idle_capacity,
                    1
                ),
                n.max_idle_seconds = coalesce(
                    row.max_idle_seconds,
                    n.max_idle_seconds
                ),
                n.linked_charger_node_id = coalesce(
                    row.linked_charger_node_id,
                    n.linked_charger_node_id
                ),
                n.parking_priority = coalesce(
                    row.parking_priority,
                    n.parking_priority,
                    100
                ),
                n.charging_cost = coalesce(
                    row.charging_cost,
                    row.charge_cost,
                    row.charger_cost,
                    row.price_per_percent,
                    row.cost,
                    n.charging_cost
                ),
                n.service_capacity = coalesce(
                    row.service_capacity, n.service_capacity
                ),
                n.service_duration_seconds = coalesce(
                    row.service_duration_seconds, n.service_duration_seconds
                ),
                n.charger_capacity = coalesce(
                    row.charger_capacity, n.charger_capacity
                ),
                n.charger_power_kw = coalesce(
                    row.charger_power_kw, n.charger_power_kw
                ),
                n.charging_rate_percent_per_minute = coalesce(
                    row.charging_rate_percent_per_minute,
                    n.charging_rate_percent_per_minute
                ),
                n.supported_robot_types = coalesce(
                    row.supported_robot_types, n.supported_robot_types
                ),
                n.waiting_capacity = coalesce(
                    row.waiting_capacity, n.waiting_capacity
                ),
                n.parking_capacity = coalesce(
                    row.parking_capacity, n.parking_capacity
                ),
                n.allowed_robot_types = coalesce(
                    row.allowed_robot_types, n.allowed_robot_types
                ),
                n.maximum_idle_duration = coalesce(
                    row.maximum_idle_duration,
                    row.max_idle_seconds,
                    n.maximum_idle_duration
                ),
                n.nearby_service_nodes = coalesce(
                    row.nearby_service_nodes, n.nearby_service_nodes
                )
            MERGE (z)-[:HAS_NODE]->(n)
            """,
            warehouse_id=warehouse_id,
            warehouse_name=warehouse_name,
            nodes=nodes,
            database_=self.database,
        )
        self.driver.execute_query(
            """
            UNWIND $edges AS row
            MATCH (a:MapNode {warehouse_id: $warehouse_id, node_id: row.from_node})
            MATCH (b:MapNode {warehouse_id: $warehouse_id, node_id: row.to_node})
            MERGE (a)-[r:CONNECTED_TO]->(b)
            SET r.edge_id = coalesce(row.edge_id, r.edge_id),
                r.distance = row.distance,
                r.travel_seconds = row.travel_seconds,
                r.direction = coalesce(row.direction, 'ONE_WAY'),
                r.active = coalesce(row.active, true),
                r.width = row.width
            """,
            warehouse_id=warehouse_id,
            edges=edges,
            database_=self.database,
        )
        return {"nodes": len(nodes), "edges": len(edges)}

    def upsert_idle_nodes(
        self,
        warehouse_id: int,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Add or update dedicated idle bays without replacing map metadata."""

        self.ensure_constraints()
        self.driver.execute_query(
            """
            MATCH (w:Warehouse {warehouse_id: $warehouse_id})
            WITH w
            UNWIND $nodes AS row
            MERGE (z:Zone {
                warehouse_id: $warehouse_id,
                zone_id: coalesce(row.zone_id, 'PARKING')
            })
            MERGE (w)-[:HAS_ZONE]->(z)
            MERGE (n:MapNode {
                warehouse_id: $warehouse_id,
                node_id: row.node_id
            })
            SET n.node_type = coalesce(row.node_type, 'PARKING'),
                n.x = row.x,
                n.y = row.y,
                n.active = coalesce(row.active, true),
                n.idle_allowed = coalesce(row.idle_allowed, true),
                n.idle_capacity = coalesce(
                    row.idle_capacity,
                    row.waiting_capacity,
                    row.parking_capacity,
                    1
                ),
                n.waiting_capacity = coalesce(
                    row.waiting_capacity, n.waiting_capacity
                ),
                n.parking_capacity = coalesce(
                    row.parking_capacity, n.parking_capacity
                ),
                n.max_idle_seconds = coalesce(
                    row.max_idle_seconds, n.max_idle_seconds
                ),
                n.maximum_idle_duration = coalesce(
                    row.maximum_idle_duration,
                    row.max_idle_seconds,
                    n.maximum_idle_duration
                ),
                n.allowed_robot_types = coalesce(
                    row.allowed_robot_types, n.allowed_robot_types
                ),
                n.nearby_service_nodes = coalesce(
                    row.nearby_service_nodes, n.nearby_service_nodes
                ),
                n.linked_charger_node_id = row.linked_charger_node_id,
                n.parking_priority = coalesce(row.parking_priority, 100)
            MERGE (z)-[:HAS_NODE]->(n)
            """,
            warehouse_id=warehouse_id,
            nodes=nodes,
            database_=self.database,
        )
        self.driver.execute_query(
            """
            UNWIND $edges AS row
            MATCH (a:MapNode {
                warehouse_id: $warehouse_id,
                node_id: row.from_node
            })
            MATCH (b:MapNode {
                warehouse_id: $warehouse_id,
                node_id: row.to_node
            })
            MERGE (a)-[r:CONNECTED_TO]->(b)
            SET r.edge_id = coalesce(row.edge_id, r.edge_id),
                r.distance = row.distance,
                r.travel_seconds = row.travel_seconds,
                r.direction = coalesce(row.direction, 'BOTH'),
                r.active = coalesce(row.active, true),
                r.width = row.width
            """,
            warehouse_id=warehouse_id,
            edges=edges,
            database_=self.database,
        )
        return {"nodes": len(nodes), "edges": len(edges)}

    def upsert_resource_capacities(
        self,
        warehouse_id: int,
        resources: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Update shared-resource properties without replacing map topology."""

        self.driver.execute_query(
            """
            UNWIND $resources AS row
            MATCH (n:MapNode {
                warehouse_id: $warehouse_id,
                node_id: row.node_id
            })
            SET n.service_capacity = coalesce(
                    row.service_capacity, n.service_capacity
                ),
                n.service_duration_seconds = coalesce(
                    row.service_duration_seconds, n.service_duration_seconds
                ),
                n.charger_capacity = coalesce(
                    row.charger_capacity, n.charger_capacity
                ),
                n.charger_power_kw = coalesce(
                    row.charger_power_kw, n.charger_power_kw
                ),
                n.charging_rate_percent_per_minute = coalesce(
                    row.charging_rate_percent_per_minute,
                    n.charging_rate_percent_per_minute
                ),
                n.supported_robot_types = coalesce(
                    row.supported_robot_types, n.supported_robot_types
                ),
                n.waiting_capacity = coalesce(
                    row.waiting_capacity, n.waiting_capacity
                ),
                n.parking_capacity = coalesce(
                    row.parking_capacity, n.parking_capacity
                ),
                n.idle_capacity = coalesce(
                    row.idle_capacity,
                    row.waiting_capacity,
                    row.parking_capacity,
                    n.idle_capacity
                ),
                n.allowed_robot_types = coalesce(
                    row.allowed_robot_types, n.allowed_robot_types
                ),
                n.maximum_idle_duration = coalesce(
                    row.maximum_idle_duration, n.maximum_idle_duration
                ),
                n.max_idle_seconds = coalesce(
                    row.maximum_idle_duration, n.max_idle_seconds
                ),
                n.nearby_service_nodes = coalesce(
                    row.nearby_service_nodes, n.nearby_service_nodes
                )
            RETURN count(n) AS updated_count
            """,
            warehouse_id=warehouse_id,
            resources=resources,
            database_=self.database,
        )
        return {"updated": len(resources)}

    def fetch_topology(self, warehouse_id: int) -> dict[str, list[dict[str, Any]]]:
        node_records, _, _ = self.driver.execute_query(
            """
            MATCH (:Warehouse {warehouse_id: $warehouse_id})
                  -[:HAS_ZONE]->(z:Zone)-[:HAS_NODE]->(n:MapNode)
            WHERE coalesce(n.active, true)
            RETURN n.node_id AS node_id, z.zone_id AS zone_id,
                   n.node_type AS node_type, n.x AS x, n.y AS y,
                   coalesce(n.active, true) AS active,
                   n.charging_cost AS charging_cost,
                   n.service_capacity AS service_capacity,
                   n.service_duration_seconds AS service_duration_seconds,
                   n.charger_capacity AS charger_capacity,
                   n.charger_power_kw AS charger_power_kw,
                   n.charging_rate_percent_per_minute
                       AS charging_rate_percent_per_minute,
                   n.supported_robot_types AS supported_robot_types,
                   n.waiting_capacity AS waiting_capacity,
                   n.parking_capacity AS parking_capacity,
                   n.allowed_robot_types AS allowed_robot_types,
                   n.maximum_idle_duration AS maximum_idle_duration,
                   n.nearby_service_nodes AS nearby_service_nodes,
                   coalesce(n.idle_allowed, false) AS idle_allowed,
                   coalesce(
                       n.idle_capacity,
                       n.waiting_capacity,
                       n.parking_capacity,
                       1
                   ) AS idle_capacity,
                   n.max_idle_seconds AS max_idle_seconds,
                   n.linked_charger_node_id AS linked_charger_node_id,
                   coalesce(n.parking_priority, 100) AS parking_priority
            ORDER BY n.node_id
            """,
            warehouse_id=warehouse_id,
            database_=self.database,
            routing_=RoutingControl.READ,
        )
        edge_records, _, _ = self.driver.execute_query(
            """
            MATCH (a:MapNode {warehouse_id: $warehouse_id})
                  -[r:CONNECTED_TO]->
                  (b:MapNode {warehouse_id: $warehouse_id})
            WHERE coalesce(a.active, true)
              AND coalesce(b.active, true)
              AND coalesce(r.active, true)
            RETURN r.edge_id AS edge_id,
                   a.node_id AS from_node, b.node_id AS to_node,
                   r.distance AS distance,
                   coalesce(r.travel_seconds, r.distance) AS travel_seconds,
                   coalesce(r.direction, 'ONE_WAY') AS direction,
                   r.width AS width
            """,
            warehouse_id=warehouse_id,
            database_=self.database,
            routing_=RoutingControl.READ,
        )
        return {
            "nodes": [dict(record) for record in node_records],
            "edges": [dict(record) for record in edge_records],
        }

    def validate_node_ids(self, warehouse_id: int, node_ids: list[int]) -> dict[str, Any]:
        unique_ids = sorted(set(int(value) for value in node_ids if value is not None))
        if not unique_ids:
            return {"valid": [], "missing": []}
        records, _, _ = self.driver.execute_query(
            """
            UNWIND $node_ids AS requested_id
            OPTIONAL MATCH (n:MapNode {
                warehouse_id: $warehouse_id,
                node_id: requested_id
            })
            RETURN requested_id AS node_id, n IS NOT NULL AS exists
            """,
            warehouse_id=warehouse_id,
            node_ids=unique_ids,
            database_=self.database,
            routing_=RoutingControl.READ,
        )
        valid = [int(record["node_id"]) for record in records if record["exists"]]
        missing = [int(record["node_id"]) for record in records if not record["exists"]]
        return {"valid": valid, "missing": missing}

    def set_charger_costs(
        self, warehouse_id: int, costs: dict[int, float]
    ) -> list[dict[str, Any]]:
        rows = [
            {"node_id": int(node_id), "charging_cost": float(cost)}
            for node_id, cost in sorted(costs.items())
        ]
        if not rows:
            return []
        records, _, _ = self.driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (n:MapNode {
                warehouse_id: $warehouse_id,
                node_id: row.node_id
            })
            WHERE coalesce(n.active, true)
              AND toUpper(coalesce(n.node_type, '')) = 'CHARGER'
            SET n.charging_cost = row.charging_cost
            RETURN n.node_id AS node_id, n.charging_cost AS charging_cost
            ORDER BY n.node_id
            """,
            warehouse_id=warehouse_id,
            rows=rows,
            database_=self.database,
        )
        return [dict(record) for record in records]

    def list_chargers(self, warehouse_id: int) -> list[dict[str, Any]]:
        records, _, _ = self.driver.execute_query(
            """
            MATCH (n:MapNode {warehouse_id: $warehouse_id})
            WHERE coalesce(n.active, true)
              AND toUpper(coalesce(n.node_type, '')) = 'CHARGER'
            RETURN n.node_id AS node_id,
                   coalesce(n.active, true) AS active,
                   n.charging_cost AS charging_cost
            ORDER BY n.node_id
            """,
            warehouse_id=warehouse_id,
            database_=self.database,
            routing_=RoutingControl.READ,
        )
        return [dict(record) for record in records]

"""Request-scoped repository over the existing Spring BE data model."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.core.config import get_settings
from app.infrastructure.be_centered_postgres import (
    BeCenteredDataError,
    BeCenteredPostgresAdapter,
)
from app.infrastructure.manager import get_infrastructure_manager
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository
from app.repositories.json_repository import DataContractError, JsonWarehouseRepository


def rack_access_map_from_neo4j(
    nodes: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Interpret rack service points from the Spring-written Neo4j projection."""

    access_by_rack: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        explicit_rack_id = node.get("rack_id")
        if explicit_rack_id:
            access_by_rack[str(explicit_rack_id)].add(node_id)
        rack_ids = node.get("rack_ids") or []
        if not isinstance(rack_ids, list):
            rack_ids = [rack_ids]
        for rack_id in rack_ids:
            if rack_id:
                access_by_rack[str(rack_id)].add(node_id)
    return {
        rack_id: sorted(access_node_ids)
        for rack_id, access_node_ids in access_by_rack.items()
    }


def resolve_runtime_route_node(
    runtime: Any,
    graph_node_ids: set[str],
    node_code_by_id: dict[int, str],
    rack_access_map: dict[str, list[str]],
) -> str | None:
    """Resolve a Spring physical-map position onto the Neo4j route graph."""

    current = getattr(runtime, "current_node_code", None)
    if not current and getattr(runtime, "current_node_id", None) is not None:
        current = node_code_by_id.get(int(runtime.current_node_id))
    if current and str(current) in graph_node_ids:
        return str(current)

    next_node = getattr(runtime, "next_node_code", None)
    if not next_node and getattr(runtime, "next_node_id", None) is not None:
        next_node = node_code_by_id.get(int(runtime.next_node_id))
    if next_node and str(next_node) in graph_node_ids:
        return str(next_node)

    if current:
        access_node_ids = rack_access_map.get(str(current), [])
        if access_node_ids:
            return access_node_ids[0]
    return str(current or next_node) if current or next_node else None


class BeSharedWarehouseRepository(JsonWarehouseRepository):
    """Use BE PostgreSQL/Redis as authority and Neo4j as route projection.

    Business operations are deliberately absent here.  They are attached by
    :class:`RequestOperationRepository` from the request's structured input.
    """

    def __init__(
        self,
        *,
        simulation_run_id: int,
        replanning_from_plan_id: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.infrastructure = get_infrastructure_manager()
        self.infrastructure.start()
        self.be_postgres = BeCenteredPostgresAdapter(
            self.settings, self.infrastructure
        )
        self.be_runtime = BeSpringRuntimeRepository(
            self.settings, self.infrastructure
        )
        context = self.be_postgres.resolve_simulation_run(simulation_run_id)
        self.simulation_run_id = int(simulation_run_id)
        self.replanning_from_plan_id = replanning_from_plan_id
        self.numeric_warehouse_id = int(context["warehouse_id"])
        self.warehouse_id = str(context["warehouse_code"])
        self.simulation_id = f"BE-RUN-{self.simulation_run_id}"
        self._runtime_snapshot = self.be_runtime.snapshot(self.simulation_run_id)

        graph_snapshot = self.infrastructure.neo4j.fetch_route_graph(self.warehouse_id)
        if not graph_snapshot.nodes:
            raise DataContractError(
                f"Neo4j has no RouteNode projection for {self.warehouse_id}. "
                "Run scripts/sync_be_graph_to_neo4j.py first."
            )
        self.graph = {
            "title": "Spring BE map projected to LARO RouteNode/TRAVERSES",
            "coordinate_unit": "display_unit",
            "physical_distance_unit": "meters",
            "routing_model": {
                "rack_entities_in_route_graph": False,
                "rack_through_travel_allowed": False,
                "goods_to_person_stations": True,
            },
            "summary": graph_snapshot.summary,
            "nodes": graph_snapshot.nodes,
            "edges": graph_snapshot.edges,
        }
        self._graph_version = graph_snapshot.version

        self._numeric_node_to_code = {
            int(value["node_id"]): str(value["node_code"])
            for value in self.be_postgres.route_nodes(self.numeric_warehouse_id)
        }
        self._numeric_edge_to_code = {
            int(value["edge_id"]): str(value["edge_code"])
            for value in self.be_postgres.route_edges(self.numeric_warehouse_id)
        }
        self._inventory_units = self.be_postgres.inventory_units(
            self.numeric_warehouse_id,
            replanning_from_plan_id=self.replanning_from_plan_id,
        )
        self.inventory = self._inventory_document()
        self.facility = self._facility_document()
        self.scenario = {
            "warehouse_id": self.warehouse_id,
            "orders": [],
            "inbound_receipts": [],
            "robots": self._runtime_robots(),
            "edge_runtime": self._runtime_edges(),
            "edge_reservations": [],
            "buffer_nodes": [],
        }
        self._live_versions = {
            **self.be_postgres.versions(self.numeric_warehouse_id),
            "graph_version": self._graph_version,
            "runtime_version": str(
                (self._runtime_snapshot.meta.runtime_version or 0)
                if self._runtime_snapshot.meta
                else 0
            ),
        }
        self._rebuild_indexes()

    def _rack_access_map(self) -> dict[str, list[str]]:
        return rack_access_map_from_neo4j(self.graph.get("nodes", []))

    def empty_tote_buffer_candidates(self) -> list[dict[str, Any]]:
        """Use explicit buffers, or the outbound-station release point in compact maps.

        The editor intentionally exposes no separate empty-tote facility.  In
        that compact contract, a depleted box leaves the mobile-robot workflow
        at the outbound station, so its station access node is also the release
        point.  This is planning metadata only; it does not create a hidden
        PostgreSQL or Neo4j node.
        """

        explicit = super().empty_tote_buffer_candidates()
        if explicit:
            return explicit
        access_node_ids = sorted({
            str(node_id)
            for station in self.outbound_stations.values()
            for node_id in station.get("access_node_ids", [])
            if node_id
        })
        if not access_node_ids:
            return []
        return [{
            "buffer_id": "OUTBOUND_STATION_TOTE_RELEASE",
            "access_node_ids": access_node_ids,
            "capacity": max(1, len(access_node_ids)),
            "status": "available",
            "virtual": True,
        }]

    def _validate_rack_access_contract(
        self,
        rack_id: str,
        access_node_ids: list[str],
    ) -> None:
        """Validate Spring's Neo4j rack-to-aisle access mapping."""

        if rack_id in self.nodes:
            raise DataContractError(f"rack entity {rack_id} must not be a routing node")
        if not access_node_ids:
            raise DataContractError(
                f"BE rack {rack_id} must connect to at least one aisle access node"
            )
        for access_node_id in access_node_ids:
            if access_node_id not in self.nodes:
                raise DataContractError(
                    f"BE rack {rack_id} references unknown aisle node {access_node_id}"
                )

    def _inventory_document(self) -> dict[str, Any]:
        access = self._rack_access_map()
        by_rack: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        rack_ids: set[str] = set(access)
        for row in self._inventory_units:
            rack_id = str(row["rack_code"])
            rack_ids.add(rack_id)
            level = int(row.get("rack_level") or 1)
            by_rack[rack_id][level] = {
                "warehouse_item_id": int(row["warehouse_item_id"]),
                "stock_id": f"WI-{row['warehouse_item_id']}",
                "inventory_unit_id": f"WI-{row['warehouse_item_id']}",
                # Compatibility alias only; there is no handling_units table.
                "handling_unit_id": f"WI-{row['warehouse_item_id']}",
                "item_id": str(row.get("product_code") or row["item_id"]),
                "item_name": str(row.get("product_name") or row.get("product_code") or row["item_id"]),
                "category": row.get("category"),
                "quantity": int(row["quantity"]),
                "capacity": int(row.get("capacity") or row["quantity"]),
                "unit": str(row.get("unit") or "EA"),
                "handling_unit_status": str(row.get("planning_status") or "STORED").casefold(),
                "version": int(row.get("version") or 1),
                "storage_location_id": int(row["storage_location_id"]),
            }
        records: list[dict[str, Any]] = []
        for rack_id in sorted(rack_ids):
            levels = []
            known_levels = set(by_rack.get(rack_id, {})) | {1, 2, 3}
            for level in sorted(known_levels):
                item = by_rack.get(rack_id, {}).get(level)
                levels.append(
                    {
                        "level": level,
                        "status": "PARTIAL" if item else "EMPTY",
                        "item": item,
                    }
                )
            records.append(
                {
                    "rack_id": rack_id,
                    "access_node_ids": access.get(rack_id, []),
                    "levels": levels,
                }
            )
        occupied = sum(
            1 for record in records for level in record["levels"] if level["item"]
        )
        return {
            "warehouse_id": self.warehouse_id,
            "version": self.be_postgres.versions(self.numeric_warehouse_id)["inventory_version"],
            "summary": {
                "rack_count": len(records),
                "occupied_level_count": occupied,
                "empty_level_count": sum(len(value["levels"]) for value in records) - occupied,
            },
            "racks": records,
        }

    def _facility_document(self) -> dict[str, Any]:
        rows = self.be_postgres.facilities(self.numeric_warehouse_id)
        ports: list[dict[str, Any]] = []
        handoffs: list[dict[str, Any]] = []
        chutes: list[dict[str, Any]] = []
        stations: list[dict[str, Any]] = []
        station_robots: list[dict[str, Any]] = []
        buffers: list[dict[str, Any]] = []
        for row in rows:
            code = str(row["facility_code"])
            ftype = str(row["facility_type"])
            access = [str(value) for value in (row.get("access_node_codes") or [])]
            if not access and row.get("node_code"):
                access = [str(row["node_code"])]
            metadata = dict(row.get("metadata") or {})
            capacity = int(row.get("capacity") or metadata.get("capacity") or 1)
            if ftype == "INBOUND_HANDOFF":
                handoffs.append({"handoff_id": code, "access_node_ids": access, "buffer_capacity": capacity})
            elif ftype == "INBOUND_PORT":
                ports.append({"port_id": code, "label": metadata.get("label", code), "handoff_id": metadata.get("handoff_id", code)})
            elif ftype == "OUTBOUND_CHUTE":
                chutes.append({"chute_id": code, "label": metadata.get("label", code)})
            elif ftype == "OUTBOUND_STATION":
                robot_id = str(metadata.get("station_robot_id") or f"SR-{code}")
                stations.append({
                    "station_id": code,
                    "station_robot_id": robot_id,
                    "access_node_ids": access,
                    "served_chute_ids": [str(value) for value in (row.get("served_destination_codes") or [])],
                    "tote_buffer_capacity": capacity,
                    "status": str(row.get("status") or "AVAILABLE").casefold(),
                })
                station_robots.append({
                    "station_robot_id": robot_id,
                    "station_id": code,
                    "status": str(row.get("status") or "AVAILABLE").casefold(),
                    "max_orders_per_wave": int(metadata.get("max_orders_per_wave", 32)),
                    "items_per_tick": int(metadata.get("items_per_tick", 1)),
                })
            elif ftype == "EMPTY_TOTE_BUFFER":
                buffers.append({"buffer_id": code, "access_node_ids": access, "capacity": capacity, "status": str(row.get("status") or "AVAILABLE").casefold()})

        # The active route graph is authoritative for physical access nodes.
        # Layout editing may collapse a former A/B pair into one access node or
        # create additional facilities while the facility metadata row still
        # contains the older node codes. Rebuild those references from the
        # graph so planning never mixes two different map revisions.
        graph_handoff_access: dict[str, list[str]] = defaultdict(list)
        graph_station_access: dict[str, list[str]] = defaultdict(list)
        graph_buffer_access: dict[str, list[str]] = defaultdict(list)
        for node in self.graph.get("nodes", []):
            node_type = str(node.get("type") or "")
            node_id = str(node["id"])
            if node_type == "outbound":
                if node_id not in {value["chute_id"] for value in chutes}:
                    chutes.append({"chute_id": node_id, "label": str(node.get("label") or node_id)})
            elif node_type == "inbound_handoff_access":
                handoff_id = str(node.get("handoff_id") or node_id)
                graph_handoff_access[handoff_id].append(node_id)
            elif node_type == "outbound_station_access":
                station_id = str(node.get("station_id") or "OUT_STATION_1")
                graph_station_access[station_id].append(node_id)
            elif node_type == "empty_tote_buffer_access":
                buffer_id = str(node.get("buffer_id") or "EMPTY_TOTE_BUFFER")
                graph_buffer_access[buffer_id].append(node_id)

        if graph_handoff_access:
            existing_handoffs = {str(value["handoff_id"]): value for value in handoffs}
            handoffs = []
            for handoff_id, node_ids in sorted(graph_handoff_access.items()):
                value = dict(existing_handoffs.get(handoff_id) or {
                    "handoff_id": handoff_id,
                    "buffer_capacity": 8,
                })
                value["access_node_ids"] = sorted(set(node_ids))
                handoffs.append(value)
            active_handoff_ids = set(graph_handoff_access)
            ports = [
                value for value in ports
                if str(value.get("handoff_id")) in active_handoff_ids
            ]
        else:
            handoffs = []
            ports = []

        graph_node_ids = {
            str(node["id"])
            for node in self.graph.get("nodes", [])
            if node.get("id")
        }
        explicit_station_contract_valid = bool(stations) and all(
            station.get("access_node_ids")
            and all(str(node_id) in graph_node_ids for node_id in station["access_node_ids"])
            for station in stations
        )

        if graph_station_access and not explicit_station_contract_valid:
            existing_stations = {str(value["station_id"]): value for value in stations}
            stations = []
            for station_id, node_ids in sorted(graph_station_access.items()):
                value = dict(existing_stations.get(station_id) or {
                    "station_id": station_id,
                    "station_robot_id": f"SR-{station_id}",
                    "served_chute_ids": [item["chute_id"] for item in chutes],
                    "tote_buffer_capacity": 8,
                    "status": "available",
                })
                value["access_node_ids"] = sorted(set(node_ids))
                stations.append(value)

            existing_station_robots = {
                str(value["station_id"]): value for value in station_robots
            }
            station_robots = []
            for station in stations:
                station_id = str(station["station_id"])
                robot_id = str(station["station_robot_id"])
                station_robots.append(dict(existing_station_robots.get(station_id) or {
                    "station_robot_id": robot_id,
                    "station_id": station_id,
                    "status": "available",
                    "max_orders_per_wave": 32,
                    "items_per_tick": 1,
                }))
        elif not explicit_station_contract_valid:
            stations = []
            station_robots = []

        if graph_buffer_access:
            existing_buffers = {str(value["buffer_id"]): value for value in buffers}
            buffers = []
            for buffer_id, node_ids in sorted(graph_buffer_access.items()):
                value = dict(existing_buffers.get(buffer_id) or {
                    "buffer_id": buffer_id,
                    "capacity": 32,
                    "status": "available",
                })
                value["access_node_ids"] = sorted(set(node_ids))
                buffers.append(value)
        else:
            # The compact editor map intentionally has no separate empty-tote
            # access node. JsonWarehouseRepository then exposes the outbound
            # station access nodes as a virtual release point.
            buffers = []
        # Stations discovered before map-derived chute fallbacks may have an empty
        # served list. Complete it only after every chute has been materialized.
        all_chute_ids = [str(value["chute_id"]) for value in chutes]
        for station in stations:
            if not station.get("served_chute_ids"):
                station["served_chute_ids"] = list(all_chute_ids)

        if handoffs and not ports:
            ports.append({"port_id": "INBOUND_PORT_DEFAULT", "label": "Default inbound port", "handoff_id": handoffs[0]["handoff_id"]})
        return {
            "warehouse_id": self.warehouse_id,
            "version": self.be_postgres.versions(self.numeric_warehouse_id)["facility_version"],
            "inbound_ports": ports,
            "inbound_handoffs": handoffs,
            "outbound_chutes": chutes,
            "outbound_stations": stations,
            "station_robots": station_robots,
            "empty_tote_buffers": buffers,
        }

    def _runtime_robots(self) -> list[dict[str, Any]]:
        snapshot = self._runtime_snapshot
        graph_node_ids = {
            str(node["id"])
            for node in self.graph.get("nodes", [])
            if node.get("id")
        }
        rack_access_map = self._rack_access_map()
        masters = {
            int(value["robot_id"]): value
            for value in self.be_postgres.robot_master(self.numeric_warehouse_id)
        }
        values: list[dict[str, Any]] = []
        for runtime in snapshot.robots:
            master = masters.get(runtime.robot_id, {})
            # robot_specs.robot_code is a model/spec code shared by multiple
            # physical robots (for example every row can be AGV-100).  It must
            # not be used as the planning identity or multiple robots collapse
            # into one solver vehicle.  The BE robot primary key is the stable
            # instance identity and also round-trips cleanly to Spring.
            robot_code = f"R{runtime.robot_id}"
            current_node = resolve_runtime_route_node(
                runtime,
                graph_node_ids,
                self._numeric_node_to_code,
                rack_access_map,
            )
            initial_node_id = master.get("initial_node_id")
            home_node = None
            if initial_node_id is not None:
                try:
                    candidate_home = self._numeric_node_to_code.get(int(initial_node_id))
                except (TypeError, ValueError):
                    candidate_home = None
                if candidate_home in graph_node_ids:
                    home_node = candidate_home
            status = str(runtime.status or "idle").casefold()
            status_alias = {
                "available": "idle",
                "idle": "idle",
                "moving": "moving",
                "working": "working",
                "charging": "charging",
                "error": "fault",
                "fault": "fault",
                "offline": "offline",
            }
            values.append(
                {
                    "robot_id": robot_code,
                    "robot_code": robot_code,
                    "current_node": current_node,
                    "home_node": home_node,
                    "current_edge": runtime.current_edge_code,
                    "from_node": runtime.from_node_code,
                    "to_node": runtime.to_node_code,
                    "edge_progress": None,
                    "status": status_alias.get(status, status),
                    "battery_pct": float(runtime.battery_level if runtime.battery_level is not None else master.get("initial_battery_pct", 0)),
                    # In the shared BOX model, capacity is physical boxes, not
                    # sellable units. Every AMR carries exactly one BOX.
                    "capacity_units": 1,
                    "current_load_units": (
                        1
                        if int(runtime.current_load_units or 0) > 0
                        or runtime.carrying_load is True
                        else 0
                    ),
                    "active_task_id": runtime.active_task_code or (str(runtime.current_task_id) if runtime.current_task_id is not None else None),
                    # During a replan barrier Spring publishes the exact clock
                    # at which each robot became stationary.  Keep it separate
                    # from the global Redis snapshot time: robots that stopped
                    # earlier must not be projected through later old-plan work.
                    "safe_handover_at_ms": runtime.wait_started_at_ms,
                    "sim_time_ms": int(
                        runtime.sim_time_ms
                        or runtime.simulation_time_millis
                        or (snapshot.meta.sim_time_ms if snapshot.meta else 0)
                        or 0
                    ),
                }
            )
        return values

    def _runtime_edges(self) -> list[dict[str, Any]]:
        values = []
        for edge in self.be_runtime.load_edge_runtime(self.simulation_run_id):
            code = edge.edge_code or self._numeric_edge_to_code.get(edge.edge_id)
            if not code:
                continue
            values.append(
                {
                    "edge_id": code,
                    "status": str(edge.status or "OPEN").casefold(),
                    "cost_multiplier": float(edge.cost_multiplier),
                    "travel_time_multiplier": float(edge.travel_time_multiplier),
                    "occupied_by_robot_id": (
                        f"R{edge.occupied_by_robot_id:03d}"
                        if edge.occupied_by_robot_id is not None
                        else None
                    ),
                }
            )
        return values

    @property
    def versions(self) -> dict[str, str]:
        """Return versions captured once for this planning request."""

        return dict(self._live_versions)

    @property
    def source_manifest(self) -> dict[str, str]:
        return {
            "route_nodes": "neo4j_projection_from_be_map",
            "route_edges": "neo4j_projection_from_be_map",
            "racks": "be_warehouse_node_plus_laro_ext",
            "inventory_units": "be_warehouse_items_live",
            "orders": "not_used",
            "handling_units": "not_used",
            "operations": "request_structured_input",
            "facilities": "laro_ext_facility_plus_be_map",
            "robots": "be_redis_live",
            "edge_runtime": "be_redis_live",
            "reservations": "be_redis_live",
        }

    def canonical_item_code(
        self, item_id: int | None, product_code: str | None
    ) -> str:
        if product_code:
            return str(product_code)
        if item_id is None:
            raise ValueError("item_id or product_code is required")
        for row in self._inventory_units:
            if int(row.get("item_id") or -1) == int(item_id):
                return str(row.get("product_code") or item_id)
        return str(item_id)

    def get_order(self, order_id: str) -> None:
        return None

    def find_orders(self, **_: Any) -> list[dict[str, Any]]:
        return []

    def get_inbound_receipt(self, inbound_id: str) -> None:
        return None

    def all_inbound_receipts(self) -> list[dict[str, Any]]:
        return []

    def handling_units(self, item_id: str | None = None) -> list[dict[str, Any]]:
        values = []
        for row in self.be_postgres.inventory_units(
            self.numeric_warehouse_id,
            item_id,
            replanning_from_plan_id=self.replanning_from_plan_id,
        ):
            rack_id = str(row["rack_code"])
            level = int(row.get("rack_level") or 1)
            status = str(row.get("planning_status") or "STORED").casefold()
            values.append(
                {
                    "warehouse_id": self.warehouse_id,
                    "warehouse_item_id": int(row["warehouse_item_id"]),
                    "inventory_unit_id": f"WI-{row['warehouse_item_id']}",
                    # Compatibility alias; no durable handling_units row exists.
                    "handling_unit_id": f"WI-{row['warehouse_item_id']}",
                    "stock_id": f"WI-{row['warehouse_item_id']}",
                    "item_id": str(row.get("product_code") or row["item_id"]),
                    "item_name": str(row.get("product_name") or row.get("product_code") or row["item_id"]),
                    "category": row.get("category"),
                    "quantity": int(row["quantity"]),
                    "capacity": int(row.get("capacity") or row["quantity"]),
                    "unit": str(row.get("unit") or "EA"),
                    "rack_id": rack_id,
                    "rack_level": level,
                    "home_rack_id": rack_id,
                    "home_rack_level": level,
                    "status": status,
                    "handling_unit_status": status,
                    "version": int(row.get("version") or 1),
                    "access_node_ids": self.rack_access_nodes(rack_id),
                    "storage_location_id": int(row["storage_location_id"]),
                }
            )
        return values

    def item_stocks(self, item_id: str) -> list[dict[str, Any]]:
        return self.handling_units(item_id)

    def empty_putaway_slots(self) -> list[dict[str, Any]]:
        values = []
        for row in self.be_postgres.empty_rack_slots(self.numeric_warehouse_id):
            rack_id = str(row["rack_code"])
            access = self.rack_access_nodes(rack_id)
            if not access:
                continue
            values.append(
                {
                    "rack_id": rack_id,
                    "rack_level": int(row["rack_level"]),
                    "access_node_ids": access,
                    "capacity": int(row.get("capacity") or 0),
                }
            )
        return values

    def all_robots(self) -> list[dict[str, Any]]:
        return self._runtime_robots()

    def runtime_edge_records(self) -> list[dict[str, Any]]:
        return self._runtime_edges()

    def existing_reservations(self) -> list[dict[str, Any]]:
        # The existing BE runtime contract does not yet expose a reservation
        # collection. Request-scoped preserved reservations remain supported.
        return []

    def node_code_for_numeric_id(self, node_id: int) -> str | None:
        return self._numeric_node_to_code.get(int(node_id))

    def node_code_for_storage_location(self, storage_location_id: int) -> str | None:
        return self.be_postgres.node_code_for_storage_location(storage_location_id)

    def facility_by_code(self, facility_code: str) -> dict[str, Any] | None:
        """Resolve one BE/LARO facility to its robot-accessible route node."""

        value = self.be_postgres.facility_by_code(
            self.numeric_warehouse_id, facility_code
        )
        if value is None:
            return None
        value = dict(value)
        metadata = dict(value.get("metadata") or {})
        access_codes = [
            str(item) for item in (value.get("access_node_codes") or []) if item
        ]
        if str(value.get("facility_type")) == "INBOUND_PORT":
            handoff_id = metadata.get("handoff_id")
            if handoff_id:
                handoff = self.be_postgres.facility_by_code(
                    self.numeric_warehouse_id, str(handoff_id)
                )
                if handoff:
                    access_codes = [
                        str(item)
                        for item in (handoff.get("access_node_codes") or [])
                        if item
                    ] or access_codes
        if access_codes:
            value["access_node_id"] = access_codes[0]
        elif value.get("node_code"):
            value["access_node_id"] = str(value["node_code"])
        return value

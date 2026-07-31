"""Concrete JSON repositories for the supplied warehouse map and scenario data.

These repositories are file-backed application data sources, not test doubles.
They provide deterministic, versioned snapshots that can later be replaced by
PostgreSQL, Redis, and Neo4j adapters without changing graph-node contracts.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.domain.schemas import normalize_warehouse_id
from app.repositories.context import (
    current_repository,
    current_simulation_id,
    current_warehouse_id,
)


class DataContractError(RuntimeError):
    """Raised when a supplied warehouse data file violates its contract."""


def _read_json(path: Path) -> dict[str, Any]:
    """Read one UTF-8 JSON object and raise a descriptive error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DataContractError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataContractError(f"{path} must contain a JSON object")
    return value


def _file_version(path: Path) -> str:
    """Return a short SHA-256 content version for a data file."""

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class JsonWarehouseRepository:
    """Load the user-supplied graph, rack inventory, and runtime scenario."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        warehouse_id: str | None = None,
        simulation_id: str | None = None,
        validate_document_warehouse: bool = True,
    ) -> None:
        """Load and index all local scenario files once per repository instance.

        JSON mode supports one directory per warehouse under
        ``WAREHOUSE_DATA_ROOT/<warehouse_id>``.  If that directory is absent,
        the configured ``DATA_DIR`` remains the backward-compatible default.
        """

        settings = get_settings()
        self.warehouse_id = normalize_warehouse_id(
            warehouse_id or current_warehouse_id(settings.default_warehouse_id)
        )
        self.simulation_id = str(
            simulation_id or current_simulation_id(settings.runtime_simulation_id)
        )
        warehouse_root = settings.warehouse_data_root / self.warehouse_id
        if data_dir is not None:
            root = Path(data_dir)
        elif self.warehouse_id == settings.default_warehouse_id:
            # DATA_DIR remains the authoritative default-warehouse source and
            # can be redirected by tests or deployments.
            root = settings.data_dir
        elif warehouse_root.exists():
            root = warehouse_root
        else:
            raise DataContractError(
                f"No JSON data directory is registered for warehouse {self.warehouse_id}. "
                f"Create {warehouse_root} or use the embedded/live repository backend."
            )
        self.data_root = root
        self.graph_path = root / "warehouse_graph.json"
        self.inventory_path = root / "rack_inventory.json"
        self.scenario_path = root / "scenario_state.json"
        self.facility_path = root / "facility_resources.json"
        self.inventory = _read_json(self.inventory_path)
        self.scenario = _read_json(self.scenario_path)
        self.facility = (
            _read_json(self.facility_path)
            if self.facility_path.exists()
            else {
                "inbound_ports": [],
                "inbound_handoffs": [],
                "outbound_chutes": [],
                "outbound_stations": [],
                "station_robots": [],
                "empty_tote_buffers": [],
            }
        )
        if validate_document_warehouse:
            for label, document in (
                ("warehouse_graph", _read_json(self.graph_path)),
                ("rack_inventory", self.inventory),
                ("scenario_state", self.scenario),
                ("facility_resources", self.facility),
            ):
                declared = document.get("warehouse_id")
                if (
                    declared is not None
                    and normalize_warehouse_id(declared) != self.warehouse_id
                ):
                    raise DataContractError(
                        f"{label} declares warehouse_id={declared!r}, but the repository "
                        f"was opened for {self.warehouse_id}."
                    )
        self._graph_version: str | None = None
        # The base repository is always file-backed.  ``get_repository`` selects
        # the hybrid Neo4j subclass only for the configured production data set.
        # Explicit fixture directories therefore stay hermetic and never contact
        # an external graph database during tests or scenario validation.
        self.graph = _read_json(self.graph_path)
        self._rebuild_indexes()


    def _rebuild_indexes(self) -> None:
        """Rebuild all defensive indexes after replacing one or more documents."""

        self.nodes = {str(node["id"]): dict(node) for node in self.graph.get("nodes", [])}
        self.edges = {str(edge["id"]): dict(edge) for edge in self.graph.get("edges", [])}
        self.racks = {
            str(rack["rack_id"]): dict(rack)
            for rack in self.inventory.get("racks", [])
        }
        self.rack_access_by_rack = {
            rack_id: [str(value) for value in rack.get("access_node_ids", [])]
            for rack_id, rack in self.racks.items()
        }
        self.rack_by_access_node = {
            access_node_id: rack_id
            for rack_id, access_node_ids in self.rack_access_by_rack.items()
            for access_node_id in access_node_ids
        }
        self.orders = {
            str(order["order_id"]): dict(order)
            for order in self.scenario.get("orders", [])
        }
        self.inbound_receipts = {
            str(value["inbound_id"]): dict(value)
            for value in self.scenario.get("inbound_receipts", [])
        }
        self.robots = {
            str(robot["robot_id"]): dict(robot)
            for robot in self.scenario.get("robots", [])
        }
        self.inbound_ports = {
            str(value["port_id"]): dict(value)
            for value in self.facility.get("inbound_ports", [])
        }
        self.inbound_handoffs = {
            str(value["handoff_id"]): dict(value)
            for value in self.facility.get("inbound_handoffs", [])
        }
        self.outbound_chutes = {
            str(value["chute_id"]): dict(value)
            for value in self.facility.get("outbound_chutes", [])
        }
        self.outbound_stations = {
            str(value["station_id"]): dict(value)
            for value in self.facility.get("outbound_stations", [])
        }
        self.station_robots = {
            str(value["station_robot_id"]): dict(value)
            for value in self.facility.get("station_robots", [])
        }
        self.empty_tote_buffers = {
            str(value["buffer_id"]): dict(value)
            for value in self.facility.get("empty_tote_buffers", [])
        }
        self.edge_runtime = {
            str(edge["edge_id"]): dict(edge)
            for edge in self.scenario.get("edge_runtime", [])
        }
        self._rack_levels = self._index_rack_levels()
        self._validate_references()

    def _index_rack_levels(self) -> list[dict[str, Any]]:
        """Flatten rack/level inventory into bounded stock records."""

        records: list[dict[str, Any]] = []
        for rack in self.inventory.get("racks", []):
            rack_id = str(rack["rack_id"])
            access_node_ids = [str(value) for value in rack.get("access_node_ids", [])]
            for level in rack.get("levels", []):
                item = level.get("item")
                if not item:
                    continue
                records.append(
                    {
                        "stock_id": f"STOCK-{rack_id}-L{level['level']}-{item['item_id']}",
                        # rack_id is a pure inventory/master-data identifier.
                        # It is intentionally absent from the route graph.
                        "rack_id": rack_id,
                        "access_node_ids": list(access_node_ids),
                        "rack_level": int(level["level"]),
                        **dict(item),
                    }
                )
        return records

    def _validate_references(self) -> None:
        """Validate graph, inventory, robot, order, and runtime references."""

        if len(self.nodes) != int(self.graph.get("summary", {}).get("node_count", -1)):
            raise DataContractError("warehouse_graph node_count does not match nodes")
        if len(self.edges) != int(self.graph.get("summary", {}).get("edge_count", -1)):
            raise DataContractError("warehouse_graph edge_count does not match edges")
        for edge in self.edges.values():
            if edge["source"] not in self.nodes or edge["target"] not in self.nodes:
                raise DataContractError(f"edge {edge['id']} references an unknown node")
        # Racks are inventory entities, while rack_access nodes are routing
        # entities.  Each rack must expose exactly two service-only dead-end
        # access nodes and must not itself occur in the route projection.
        for rack_id, access_node_ids in self.rack_access_by_rack.items():
            if rack_id in self.nodes:
                raise DataContractError(f"rack entity {rack_id} must not be a routing node")
            if len(access_node_ids) != 2:
                raise DataContractError(f"rack {rack_id} must declare exactly two access nodes")
            for access_node_id in access_node_ids:
                node = self.nodes.get(access_node_id)
                if node is None:
                    raise DataContractError(
                        f"rack {rack_id} references unknown access node {access_node_id}"
                    )
                if node.get("type") != "rack_access" or str(node.get("rack_id")) != rack_id:
                    raise DataContractError(
                        f"access node {access_node_id} is not a rack_access node for {rack_id}"
                    )
                if node.get("service_only") is not True or node.get("transit_allowed") is not False:
                    raise DataContractError(
                        f"access node {access_node_id} must be service_only with transit_allowed=false"
                    )
                incident_edges = [
                    edge for edge in self.edges.values()
                    if edge.get("source") == access_node_id or edge.get("target") == access_node_id
                ]
                peer_nodes = {
                    str(edge["target"] if edge.get("source") == access_node_id else edge["source"])
                    for edge in incident_edges
                }
                if len(peer_nodes) != 1:
                    raise DataContractError(
                        f"rack access node {access_node_id} must be a dead-end spur"
                    )
                if any(peer in self.rack_by_access_node for peer in peer_nodes):
                    raise DataContractError(
                        f"rack access node {access_node_id} must not connect to another access node"
                    )
        # Inbound handoff and outbound station access nodes are also
        # service-only dead-end spurs.  The business resource itself lives in
        # PostgreSQL/facility master data and is never a through-routing node.
        for collection_name, collection, node_type, id_field, allowed_counts in (
            ("inbound handoff", self.inbound_handoffs, "inbound_handoff_access", "handoff_id", {2}),
            ("outbound station", self.outbound_stations, "outbound_station_access", "station_id", {2}),
            ("empty tote buffer", self.empty_tote_buffers, "empty_tote_buffer_access", "buffer_id", {1, 2}),
        ):
            for resource_id, resource in collection.items():
                access_node_ids = [str(value) for value in resource.get("access_node_ids", [])]
                if len(access_node_ids) not in allowed_counts:
                    raise DataContractError(
                        f"{collection_name} {resource_id} must declare {sorted(allowed_counts)} access node count"
                    )
                for access_node_id in access_node_ids:
                    node = self.nodes.get(access_node_id)
                    if node is None:
                        raise DataContractError(
                            f"{collection_name} {resource_id} references unknown access node {access_node_id}"
                        )
                    if node.get("type") != node_type or str(node.get(id_field)) != resource_id:
                        raise DataContractError(
                            f"access node {access_node_id} is not a {node_type} for {resource_id}"
                        )
                    if node.get("service_only") is not True or node.get("transit_allowed") is not False:
                        raise DataContractError(
                            f"service access node {access_node_id} must be service_only with transit_allowed=false"
                        )
                    incident_edges = [
                        edge for edge in self.edges.values()
                        if edge.get("source") == access_node_id or edge.get("target") == access_node_id
                    ]
                    peer_nodes = {
                        str(edge["target"] if edge.get("source") == access_node_id else edge["source"])
                        for edge in incident_edges
                    }
                    if len(peer_nodes) != 1:
                        raise DataContractError(
                            f"service access node {access_node_id} must be a dead-end spur"
                        )
        for station_id, station in self.outbound_stations.items():
            robot_id = str(station.get("station_robot_id") or "")
            if robot_id not in self.station_robots:
                raise DataContractError(
                    f"outbound station {station_id} references unknown station robot {robot_id}"
                )
            unknown_chutes = set(station.get("served_chute_ids", [])) - set(self.outbound_chutes)
            if unknown_chutes:
                raise DataContractError(
                    f"outbound station {station_id} references unknown chutes {sorted(unknown_chutes)}"
                )
        for robot in self.robots.values():
            if robot["current_node"] not in self.nodes:
                raise DataContractError(f"robot {robot['robot_id']} references unknown current_node")
            current_edge = robot.get("current_edge")
            if current_edge and current_edge not in self.edges:
                raise DataContractError(f"robot {robot['robot_id']} references unknown current_edge")
        for order in self.orders.values():
            if order["delivery_node"] not in self.nodes:
                raise DataContractError(f"order {order['order_id']} references unknown delivery_node")
        for runtime in self.edge_runtime.values():
            if runtime["edge_id"] not in self.edges:
                raise DataContractError(f"runtime references unknown edge {runtime['edge_id']}")

    @property
    def versions(self) -> dict[str, str]:
        """Return content versions used by the context snapshot."""

        return {
            "graph_version": self._graph_version or _file_version(self.graph_path),
            "inventory_version": _file_version(self.inventory_path),
            "runtime_version": _file_version(self.scenario_path),
            "facility_version": (
                _file_version(self.facility_path)
                if self.facility_path.exists()
                else "legacy"
            ),
        }

    @property
    def source_manifest(self) -> dict[str, str]:
        """Describe the authoritative source used by each repository domain."""

        return {
            "route_nodes": "json_snapshot",
            "route_edges": "json_snapshot",
            "racks": "json_snapshot",
            "handling_units": "json_snapshot",
            "orders": "json_snapshot",
            "inbound_receipts": "json_snapshot",
            "facilities": "json_snapshot",
            "robots": "json_snapshot",
            "edge_runtime": "json_snapshot",
            "reservations": "json_snapshot",
        }

    def scenario_events(self) -> list[dict[str, Any]]:
        """Return normalized events declared by the scenario file."""

        return [dict(event) for event in self.scenario.get("events", [])]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Return one order by identifier."""

        order = self.orders.get(order_id)
        return dict(order) if order else None

    def get_inbound_receipt(self, inbound_id: str) -> dict[str, Any] | None:
        """Return one canonical inbound receipt or None."""

        value = self.inbound_receipts.get(inbound_id)
        return dict(value) if value else None

    def all_inbound_receipts(self) -> list[dict[str, Any]]:
        """Return all inbound receipts in canonical ID order."""

        return [dict(value) for _, value in sorted(self.inbound_receipts.items())]

    def empty_putaway_slots(self) -> list[dict[str, Any]]:
        """Return empty rack levels and their route access nodes."""

        values: list[dict[str, Any]] = []
        for rack in self.inventory.get("racks", []):
            rack_id = str(rack["rack_id"])
            access_node_ids = [str(value) for value in rack.get("access_node_ids", [])]
            for level in rack.get("levels", []):
                if level.get("item") is not None or str(level.get("status", "EMPTY")) != "EMPTY":
                    continue
                values.append({
                    "rack_id": rack_id,
                    "rack_level": int(level["level"]),
                    "access_node_ids": access_node_ids,
                    "capacity": int(level.get("capacity", 100)),
                })
        return values

    def all_robots(self) -> list[dict[str, Any]]:
        """Return current robot runtime records."""

        return [dict(robot) for robot in self.robots.values()]

    @staticmethod
    def normalize_search_text(value: str) -> str:
        """Return a compact case-insensitive token used by bounded resolvers."""

        return re.sub(r"[\s_\-]+", "", str(value).casefold())

    def find_orders(
        self,
        *,
        order_ids: list[str] | None = None,
        item_ids: list[str] | None = None,
        item_text: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search authoritative orders by safe indexed fields, never raw SQL."""

        requested_ids = set(order_ids or [])
        requested_items = set(item_ids or [])
        requested_statuses = {str(value).casefold() for value in (statuses or [])}
        text = self.normalize_search_text(item_text or "")
        item_names: dict[str, set[str]] = {}
        for record in self._rack_levels:
            item_names.setdefault(str(record["item_id"]), set()).add(str(record.get("item_name", "")))

        results: list[dict[str, Any]] = []
        for order in self.orders.values():
            order_id = str(order["order_id"])
            item_id = str(order.get("item_id", ""))
            if requested_ids and order_id not in requested_ids:
                continue
            if requested_items and item_id not in requested_items:
                continue
            if requested_statuses and str(order.get("status", "")).casefold() not in requested_statuses:
                continue
            if text:
                searchable = {
                    self.normalize_search_text(item_id),
                    self.normalize_search_text(order_id),
                    *{
                        self.normalize_search_text(name)
                        for name in item_names.get(item_id, set())
                    },
                }
                if not any(text in candidate or candidate in text for candidate in searchable if candidate):
                    continue
            results.append(dict(order))
        return sorted(results, key=lambda value: str(value["order_id"]))

    def find_robots(self, references: list[str]) -> list[dict[str, Any]]:
        """Resolve robot IDs or robot codes against the runtime store."""

        normalized = {self.normalize_search_text(value) for value in references if value}
        if not normalized:
            return self.all_robots()
        results: list[dict[str, Any]] = []
        for robot in self.robots.values():
            aliases = {
                self.normalize_search_text(str(robot.get("robot_id", ""))),
                self.normalize_search_text(str(robot.get("robot_code", ""))),
            }
            if aliases & normalized:
                results.append(dict(robot))
        return sorted(results, key=lambda value: str(value["robot_id"]))

    def search_map_entities(
        self,
        *,
        raw_text: str,
        expected_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve a bounded natural-language map reference to real nodes/edges.

        The resolver uses exact IDs, labels, simple warehouse aliases, and graph
        relationships.  It never creates a node or edge that is absent from the
        authoritative layout.
        """

        text = str(raw_text).strip()
        normalized = self.normalize_search_text(text)
        expected = {str(value).upper() for value in (expected_types or [])}
        results: dict[tuple[str, str], dict[str, Any]] = {}

        def type_allowed(entity_type: str) -> bool:
            """Return whether this map entity type is allowed by the semantic query."""
            if not expected:
                return True
            upper = entity_type.upper()
            if upper in expected:
                return True
            if upper == "OUTBOUND" and "NODE" in expected:
                return True
            if upper == "INBOUND" and "NODE" in expected:
                return True
            if upper in {"ROUTE", "RACK_ACCESS", "CHARGING_SLOT", "ROUTE_CHARGE_JUNCTION"} and "NODE" in expected:
                return True
            if upper == "RACK_ACCESS" and ("RACK_ACCESS" in expected or "NODE" in expected):
                return True
            return False

        def add_node(node_id: str, method: str, confidence: float) -> None:
            """Add one validated entity or relation to the accumulating result."""
            node = self.nodes.get(node_id)
            if node is None or not type_allowed(str(node.get("type", "NODE"))):
                return
            results[("NODE", node_id)] = {
                "entity_id": node_id,
                "entity_type": self._map_node_entity_type(str(node.get("type", ""))),
                "display_name": str(node.get("label") or node_id),
                "match_method": method,
                "confidence": confidence,
            }

        def add_edge(edge_id: str, method: str, confidence: float) -> None:
            """Add one validated entity or relation to the accumulating result."""
            edge = self.edges.get(edge_id)
            if edge is None or (expected and "EDGE" not in expected):
                return
            results[("EDGE", edge_id)] = {
                "entity_id": edge_id,
                "entity_type": "EDGE",
                "display_name": edge_id,
                "match_method": method,
                "confidence": confidence,
            }

        def add_rack(rack_id: str, method: str, confidence: float) -> None:
            """Add a non-routing rack entity and expose its service nodes."""
            rack = self.racks.get(rack_id)
            if rack is None or (expected and "RACK" not in expected):
                return
            results[("RACK", rack_id)] = {
                "entity_id": rack_id,
                "entity_type": "RACK",
                "display_name": rack_id,
                "match_method": method,
                "confidence": confidence,
            }

        if text in self.racks:
            add_rack(text, "EXACT_ID", 1.0)
        if text in self.nodes:
            add_node(text, "EXACT_ID", 1.0)
        if text in self.edges:
            add_edge(text, "EXACT_ID", 1.0)

        for rack_id in self.racks:
            aliases = {
                self.normalize_search_text(rack_id),
                self.normalize_search_text(f"선반 {rack_id}"),
                self.normalize_search_text(f"랙 {rack_id}"),
            }
            if normalized and normalized in aliases:
                add_rack(rack_id, "ALIAS", 0.98)

        for node_id, node in self.nodes.items():
            aliases = {
                self.normalize_search_text(node_id),
                self.normalize_search_text(str(node.get("label", ""))),
            }
            node_type = str(node.get("type", ""))
            if node_type == "outbound":
                aliases.update({
                    self.normalize_search_text(f"{node.get('label', '')} 출고"),
                    self.normalize_search_text(f"{node.get('label', '')} 출고지"),
                    self.normalize_search_text(f"{node.get('label', '')} 출고 통로"),
                })
            elif node_type == "inbound":
                aliases.update({
                    self.normalize_search_text(f"{node.get('label', '')} 입고"),
                    self.normalize_search_text(f"{node.get('label', '')} 입고지"),
                })
            elif node_type == "charging_slot":
                aliases.add(self.normalize_search_text(f"충전 {node.get('index', '')}"))
            if normalized and any(normalized == alias for alias in aliases if alias):
                add_node(node_id, "ALIAS", 0.98)

        # Human descriptions such as "D 출고 통로".
        outbound_match = re.search(r"([A-Ga-g])\s*(?:번)?\s*출고", text)
        if outbound_match:
            outbound_id = f"O_{outbound_match.group(1).upper()}"
            add_node(outbound_id, "ATTRIBUTE", 0.97)
            for edge_id, edge in self.edges.items():
                if edge.get("source") == outbound_id or edge.get("target") == outbound_id:
                    add_edge(edge_id, "GRAPH_RELATION", 0.9)

        # Row/corridor references such as H3 or 3번 통로.
        row_match = re.search(r"(?:H)?(\d+)\s*(?:번)?\s*(?:행|통로)?", text, re.IGNORECASE)
        if row_match and ("통로" in text or normalized.startswith("h")):
            prefix = f"H{int(row_match.group(1))}_"
            for edge_id in self.edges:
                if edge_id.startswith(prefix):
                    add_edge(edge_id, "ATTRIBUTE", 0.82)

        if not results and normalized:
            for edge_id in self.edges:
                candidate = self.normalize_search_text(edge_id)
                if normalized in candidate or candidate in normalized:
                    add_edge(edge_id, "SEMANTIC", 0.72)
            for node_id, node in self.nodes.items():
                candidate = self.normalize_search_text(str(node.get("label") or node_id))
                if normalized in candidate or candidate in normalized:
                    add_node(node_id, "SEMANTIC", 0.7)

        return sorted(results.values(), key=lambda value: (-float(value["confidence"]), str(value["entity_id"])))

    @staticmethod
    def _map_node_entity_type(node_type: str) -> str:
        """Map warehouse node types to retrieval entity types."""

        return {
            "outbound": "OUTBOUND",
            "inbound": "INBOUND",
            "rack_access": "NODE",
            "inbound_handoff_access": "INBOUND_HANDOFF",
            "outbound_station_access": "OUTBOUND_STATION",
            "charging_slot": "CHARGING_SLOT",
        }.get(node_type, "NODE")

    def rack(self, rack_id: str) -> dict[str, Any] | None:
        """Return one rack master-data record; racks are not route nodes."""

        value = self.racks.get(rack_id)
        return dict(value) if value else None

    def rack_access_nodes(self, rack_id: str) -> list[str]:
        """Return the service-only route nodes from which a rack can be handled."""

        return list(self.rack_access_by_rack.get(rack_id, []))

    def rack_for_access_node(self, access_node_id: str) -> str | None:
        """Return the rack served by one access node."""

        return self.rack_by_access_node.get(access_node_id)

    def handling_units(self, item_id: str | None = None) -> list[dict[str, Any]]:
        """Return movable handling units stored on occupied rack levels."""

        values = [
            {
                **dict(record),
                "handling_unit_id": str(
                    record.get("handling_unit_id")
                    or f"HU-{record['rack_id']}-L{record['rack_level']}-{record['item_id']}"
                ),
                "home_rack_id": str(record.get("home_rack_id") or record["rack_id"]),
                "home_rack_level": int(record.get("home_rack_level") or record["rack_level"]),
                "handling_unit_status": str(record.get("handling_unit_status") or "stored"),
            }
            for record in self._rack_levels
            if int(record.get("quantity", 0)) > 0
            and (item_id is None or str(record.get("item_id")) == item_id)
        ]
        return values

    def outbound_station_candidates(self, chute_ids: list[str]) -> list[dict[str, Any]]:
        """Return stations whose chute coverage includes every requested chute."""

        required = set(chute_ids)
        values = []
        for station in self.outbound_stations.values():
            if required.issubset(set(station.get("served_chute_ids", []))):
                values.append(dict(station))
        return sorted(values, key=lambda value: str(value["station_id"]))

    def outbound_station(self, station_id: str) -> dict[str, Any] | None:
        value = self.outbound_stations.get(station_id)
        return dict(value) if value else None

    def station_access_nodes(self, station_id: str) -> list[str]:
        station = self.outbound_stations.get(station_id, {})
        return [str(value) for value in station.get("access_node_ids", [])]

    def station_robot(self, station_id: str) -> dict[str, Any] | None:
        station = self.outbound_stations.get(station_id)
        if not station:
            return None
        value = self.station_robots.get(str(station.get("station_robot_id")))
        return dict(value) if value else None

    def station_runtime(self, simulation_id: str | None = None) -> list[dict[str, Any]]:
        """Return station queue/runtime state from the deterministic scenario."""

        explicit = {
            str(value.get("station_id")): dict(value)
            for value in self.scenario.get("station_runtime", [])
            if value.get("station_id")
        }
        values: list[dict[str, Any]] = []
        for station_id, station in sorted(self.outbound_stations.items()):
            runtime = explicit.get(station_id, {})
            values.append(
                {
                    "station_id": station_id,
                    "station_robot_id": station.get("station_robot_id"),
                    "status": runtime.get("status", station.get("status", "available")),
                    "queue_depth": int(runtime.get("queue_depth", 0)),
                    "available_at_ms": int(runtime.get("available_at_ms", 0)),
                    "active_handling_unit_id": runtime.get("active_handling_unit_id", ""),
                    "state_version": int(runtime.get("state_version", 1)),
                }
            )
        return values

    def empty_tote_buffer_candidates(self) -> list[dict[str, Any]]:
        """Return configured physical buffers for depleted handling units."""

        return [dict(value) for _, value in sorted(self.empty_tote_buffers.items())]

    def inbound_handoff_for_port(self, port_id: str) -> dict[str, Any] | None:
        port = self.inbound_ports.get(port_id)
        if not port:
            return None
        value = self.inbound_handoffs.get(str(port.get("handoff_id")))
        return dict(value) if value else None

    def inventory_overview(self) -> dict[str, int]:
        """Return aggregate rack inventory metrics without serializing all levels."""

        summary = self.inventory.get("summary", {})
        item_ids = {record["item_id"] for record in self._rack_levels}
        return {
            "rack_count": int(summary.get("rack_count", 0)),
            "occupied_level_count": int(summary.get("occupied_level_count", 0)),
            "empty_level_count": int(summary.get("empty_level_count", 0)),
            "distinct_item_count": len(item_ids),
            "total_quantity": sum(int(record["quantity"]) for record in self._rack_levels),
        }

    def item_stocks(self, item_id: str) -> list[dict[str, Any]]:
        """Return all positive-quantity rack levels for an item."""

        return [dict(record) for record in self._rack_levels if record["item_id"] == item_id and record["quantity"] > 0]

    def rack_id_for_access_node(self, access_node_id: str) -> str | None:
        """Compatibility alias for ``rack_for_access_node``."""

        return self.rack_for_access_node(access_node_id)

    def all_rack_access_mappings(self) -> dict[str, list[str]]:
        """Return a defensive copy of all rack-to-access mappings."""

        return {key: list(value) for key, value in self.rack_access_by_rack.items()}

    def node(self, node_id: str) -> dict[str, Any] | None:
        """Return one graph node."""

        value = self.nodes.get(node_id)
        return dict(value) if value else None

    def edge(self, edge_id: str) -> dict[str, Any] | None:
        """Return one graph edge."""

        value = self.edges.get(edge_id)
        return dict(value) if value else None

    def runtime_edge_records(self) -> list[dict[str, Any]]:
        """Return current edge congestion, occupancy, reservation, and blockage records."""

        return [dict(value) for value in self.edge_runtime.values()]

    def existing_reservations(self) -> list[dict[str, Any]]:
        """Return pre-existing edge reservations from the scenario runtime."""

        return [dict(value) for value in self.scenario.get("edge_reservations", [])]

    def buffer_nodes(self) -> list[str]:
        """Return configured recovery buffer nodes."""

        return [str(value) for value in self.scenario.get("buffer_nodes", [])]

    def base_edge_metrics(self, edge_id: str) -> tuple[float, int]:
        """Return physical distance cost and nominal travel time.

        Explicit ``distance_m`` / ``speed_limit_mps`` edge attributes are
        authoritative.  Legacy maps fall back to a documented coordinate scale
        so screen coordinates and physical timing are no longer silently mixed.
        """

        settings = get_settings()
        edge = self.edges[edge_id]
        source = self.nodes[edge["source"]]
        target = self.nodes[edge["target"]]
        coordinate_distance = math.hypot(
            float(source["x"]) - float(target["x"]),
            float(source["y"]) - float(target["y"]),
        )
        distance_m = float(
            edge.get("distance_m")
            or max(coordinate_distance * settings.map_meters_per_coordinate_unit, 0.001)
        )
        speed_mps = float(
            edge.get("speed_limit_mps") or settings.robot_nominal_speed_mps
        )
        explicit_time = edge.get("nominal_travel_time_ms")
        travel_time_ms = (
            int(explicit_time)
            if explicit_time is not None
            else max(
                settings.minimum_edge_travel_time_ms,
                round(distance_m / speed_mps * 1000),
            )
        )
        return round(distance_m, 6), int(travel_time_ms)

    def adjusted_arcs(
        self,
        *,
        blocked_edge_ids: set[str],
        blocked_node_ids: set[str],
        edge_penalties: dict[str, tuple[float, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return directed arcs with selected penalties and blocked resources applied.

        When ``edge_penalties`` is omitted the repository runtime overlay is used.
        Passing a mapping allows a validated formulation to add explicit soft-avoid
        penalties without double-applying the runtime multiplier.
        """

        arcs: list[dict[str, Any]] = []
        for edge_id, edge in self.edges.items():
            if edge_id in blocked_edge_ids:
                continue
            if edge["source"] in blocked_node_ids or edge["target"] in blocked_node_ids:
                continue
            distance_m, travel_time_ms = self.base_edge_metrics(edge_id)
            # Congestion changes cost/time, never the physical distance.
            cost = float(edge.get("cost", distance_m))
            if edge_penalties is not None:
                multiplier = edge_penalties.get(edge_id)
                if multiplier is not None:
                    cost *= float(multiplier[0])
                    travel_time_ms = round(travel_time_ms * float(multiplier[1]))
            else:
                runtime = self.edge_runtime.get(edge_id, {})
                if runtime.get("status") == "congested":
                    cost *= float(runtime.get("cost_multiplier", 1.0))
                    travel_time_ms = round(travel_time_ms * float(runtime.get("travel_time_multiplier", 1.0)))
            arcs.append(
                {
                    "edge_id": edge_id,
                    "source": str(edge["source"]),
                    "target": str(edge["target"]),
                    "cost": round(cost, 6),
                    "distance_m": round(float(distance_m), 6),
                    "speed_limit_mps": float(
                        edge.get("speed_limit_mps")
                        or get_settings().robot_nominal_speed_mps
                    ),
                    "travel_time_ms": int(travel_time_ms),
                }
            )
        return arcs


class Neo4jWarehouseRepository(JsonWarehouseRepository):
    """Hybrid repository: JSON business/runtime data plus Neo4j route graph."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        warehouse_id: str | None = None,
        simulation_id: str | None = None,
    ) -> None:
        super().__init__(
            data_dir, warehouse_id=warehouse_id, simulation_id=simulation_id
        )
        from app.repositories.neo4j_map_repository import Neo4jWarehouseRepositoryMixin

        # Invoke the mixin method without altering the long-lived public
        # repository contract used by graph nodes and services.
        Neo4jWarehouseRepositoryMixin._replace_route_graph_from_neo4j(self)

    @property
    def versions(self) -> dict[str, str]:
        values = super().versions
        values["graph_version"] = self._neo4j_graph_version
        return values


_data_dir_override: Path | None = None


def set_data_dir(data_dir: Path | None) -> None:
    """Point the process repository at a scenario fixture directory.

    Passing None restores the configured default. The repository cache is
    cleared so every subsequent get_repository() call reflects the change.
    Intended for validation runners and tests; production code should rely
    on the configured DATA_DIR.
    """

    global _data_dir_override
    _data_dir_override = data_dir
    get_repository.cache_clear()


@lru_cache(maxsize=128)
def _get_repository_cached(
    warehouse_id: str,
    simulation_id: str,
    data_override: str | None,
) -> JsonWarehouseRepository:
    return _create_repository(warehouse_id, simulation_id, data_override)


def _create_repository(
    warehouse_id: str,
    simulation_id: str,
    data_override: str | None,
) -> JsonWarehouseRepository:
    """Create one repository instance without process-level caching."""

    settings = get_settings()
    override = Path(data_override) if data_override else None
    if settings.warehouse_repository_backend in {"embedded", "live"} and override is None:
        from app.repositories.live_repository import LiveWarehouseRepository

        return LiveWarehouseRepository(
            warehouse_id=warehouse_id, simulation_id=simulation_id
        )
    if settings.map_repository_backend == "neo4j" and override is None:
        return Neo4jWarehouseRepository(
            warehouse_id=warehouse_id, simulation_id=simulation_id
        )
    return JsonWarehouseRepository(
        override, warehouse_id=warehouse_id, simulation_id=simulation_id
    )


def get_repository(
    warehouse_id: str | None = None,
    simulation_id: str | None = None,
) -> JsonWarehouseRepository:
    """Return a warehouse/session-scoped repository.

    Existing graph nodes may omit arguments; the request scope established by
    :class:`OrchestrationService` supplies them.
    """

    scoped = current_repository()
    if scoped is not None:
        return scoped
    settings = get_settings()
    resolved_warehouse = normalize_warehouse_id(
        warehouse_id or current_warehouse_id(settings.default_warehouse_id)
    )
    resolved_simulation = str(
        simulation_id or current_simulation_id(settings.runtime_simulation_id)
    )
    override = str(_data_dir_override) if _data_dir_override is not None else None
    return _get_repository_cached(resolved_warehouse, resolved_simulation, override)


def create_request_repository(
    warehouse_id: str,
    simulation_id: str,
) -> JsonWarehouseRepository:
    """Create a fresh repository snapshot for one orchestration request.

    JSON fixtures remain deterministic, while live mode performs exactly one
    PostgreSQL/Redis/Neo4j snapshot load that is then shared by every graph node
    in the request.
    """

    resolved_warehouse = normalize_warehouse_id(warehouse_id)
    resolved_simulation = str(simulation_id)
    override = str(_data_dir_override) if _data_dir_override is not None else None
    return _create_repository(resolved_warehouse, resolved_simulation, override)


def _clear_repository_cache() -> None:
    _get_repository_cached.cache_clear()


get_repository.cache_clear = _clear_repository_cache  # type: ignore[attr-defined]

"""Serverless local infrastructure backed by three SQLite files.

This module mirrors the PostgreSQL, Redis, and Neo4j adapter contracts so the
full repository path can be exercised without Docker or separately installed
services.  It is intentionally called *embedded*: it validates application and
persistence contracts, but it is not a performance substitute for the real
engines.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from app.core.config import Settings, get_settings
from app.repositories.neo4j_map_repository import Neo4jMapRepository, Neo4jRouteGraphSnapshot


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else value


def _version(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:16]


class EmbeddedPostgresWarehouseAdapter:
    """SQLite implementation of the durable multi-warehouse business contract."""

    P = "v13_19_"

    def __init__(self, settings: Settings | None = None, path: Path | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(path or self.settings.local_postgres_db_path)
        self._lock = RLock()

    def _warehouse(self, warehouse_id: str | None = None) -> str:
        from app.domain.schemas import normalize_warehouse_id

        return normalize_warehouse_id(
            warehouse_id or self.settings.default_warehouse_id
        )

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def close(self) -> None:
        return None

    @contextmanager
    def _connection(self):
        self.open()
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _create_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        p = self.P
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                f"""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS {p}warehouses(
                  warehouse_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {p}warehouse_meta(
                  warehouse_id TEXT NOT NULL REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE,
                  key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,key)
                );
                CREATE TABLE IF NOT EXISTS {p}racks(
                  warehouse_id TEXT NOT NULL REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE,
                  rack_id TEXT NOT NULL, access_node_ids TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,rack_id)
                );
                CREATE TABLE IF NOT EXISTS {p}rack_slots(
                  warehouse_id TEXT NOT NULL, rack_id TEXT NOT NULL, level INTEGER NOT NULL,
                  status TEXT NOT NULL, capacity INTEGER NOT NULL,
                  PRIMARY KEY(warehouse_id,rack_id,level),
                  FOREIGN KEY(warehouse_id,rack_id) REFERENCES {p}racks(warehouse_id,rack_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS {p}handling_units(
                  warehouse_id TEXT NOT NULL, handling_unit_id TEXT NOT NULL,
                  stock_id TEXT NOT NULL, item_id TEXT NOT NULL, item_name TEXT, category TEXT,
                  quantity INTEGER NOT NULL, capacity INTEGER NOT NULL, unit TEXT NOT NULL,
                  home_rack_id TEXT NOT NULL, home_rack_level INTEGER NOT NULL,
                  status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,handling_unit_id),
                  UNIQUE(warehouse_id,stock_id),
                  FOREIGN KEY(warehouse_id,home_rack_id) REFERENCES {p}racks(warehouse_id,rack_id)
                );
                CREATE TABLE IF NOT EXISTS {p}inbound_handoffs(
                  warehouse_id TEXT NOT NULL, handoff_id TEXT NOT NULL,
                  access_node_ids TEXT NOT NULL, buffer_capacity INTEGER NOT NULL,
                  PRIMARY KEY(warehouse_id,handoff_id),
                  FOREIGN KEY(warehouse_id) REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS {p}inbound_ports(
                  warehouse_id TEXT NOT NULL, port_id TEXT NOT NULL, label TEXT NOT NULL,
                  handoff_id TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,port_id),
                  FOREIGN KEY(warehouse_id,handoff_id) REFERENCES {p}inbound_handoffs(warehouse_id,handoff_id)
                );
                CREATE TABLE IF NOT EXISTS {p}outbound_chutes(
                  warehouse_id TEXT NOT NULL, chute_id TEXT NOT NULL, label TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,chute_id),
                  FOREIGN KEY(warehouse_id) REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS {p}outbound_stations(
                  warehouse_id TEXT NOT NULL, station_id TEXT NOT NULL,
                  station_robot_id TEXT NOT NULL, access_node_ids TEXT NOT NULL,
                  served_chute_ids TEXT NOT NULL, tote_buffer_capacity INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,station_id),
                  UNIQUE(warehouse_id,station_robot_id),
                  FOREIGN KEY(warehouse_id) REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS {p}station_robots(
                  warehouse_id TEXT NOT NULL, station_robot_id TEXT NOT NULL,
                  station_id TEXT NOT NULL, status TEXT NOT NULL,
                  max_orders_per_wave INTEGER NOT NULL, items_per_tick INTEGER NOT NULL,
                  PRIMARY KEY(warehouse_id,station_robot_id),
                  UNIQUE(warehouse_id,station_id),
                  FOREIGN KEY(warehouse_id,station_id) REFERENCES {p}outbound_stations(warehouse_id,station_id)
                );
                CREATE TABLE IF NOT EXISTS {p}empty_tote_buffers(
                  warehouse_id TEXT NOT NULL, buffer_id TEXT NOT NULL,
                  access_node_ids TEXT NOT NULL, capacity INTEGER NOT NULL, status TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,buffer_id),
                  FOREIGN KEY(warehouse_id) REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS {p}orders(
                  warehouse_id TEXT NOT NULL, order_id TEXT NOT NULL,
                  status TEXT NOT NULL, priority TEXT NOT NULL,
                  outbound_chute_id TEXT NOT NULL, preferred_station_ids TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,order_id),
                  FOREIGN KEY(warehouse_id,outbound_chute_id) REFERENCES {p}outbound_chutes(warehouse_id,chute_id)
                );
                CREATE TABLE IF NOT EXISTS {p}order_lines(
                  warehouse_id TEXT NOT NULL, order_id TEXT NOT NULL, line_no INTEGER NOT NULL,
                  item_id TEXT NOT NULL, required_qty INTEGER NOT NULL,
                  PRIMARY KEY(warehouse_id,order_id,line_no),
                  FOREIGN KEY(warehouse_id,order_id) REFERENCES {p}orders(warehouse_id,order_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS {p}inbound_receipts(
                  warehouse_id TEXT NOT NULL, inbound_id TEXT NOT NULL,
                  handling_unit_id TEXT NOT NULL, item_id TEXT NOT NULL, quantity INTEGER NOT NULL,
                  source_port_id TEXT NOT NULL, target_rack_id TEXT,
                  target_rack_level INTEGER, status TEXT NOT NULL,
                  priority TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,inbound_id),
                  UNIQUE(warehouse_id,handling_unit_id),
                  FOREIGN KEY(warehouse_id,source_port_id) REFERENCES {p}inbound_ports(warehouse_id,port_id),
                  FOREIGN KEY(warehouse_id,target_rack_id) REFERENCES {p}racks(warehouse_id,rack_id)
                );
                CREATE TABLE IF NOT EXISTS {p}outbound_batches(
                  warehouse_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                  simulation_id TEXT NOT NULL, item_id TEXT NOT NULL,
                  handling_unit_id TEXT NOT NULL, station_id TEXT NOT NULL,
                  mobile_robot_id TEXT, post_station_node TEXT NOT NULL,
                  post_station_action TEXT NOT NULL, requested_quantity INTEGER NOT NULL,
                  quantity_before INTEGER NOT NULL, quantity_after INTEGER NOT NULL,
                  return_required INTEGER NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,batch_id),
                  FOREIGN KEY(warehouse_id,handling_unit_id) REFERENCES {p}handling_units(warehouse_id,handling_unit_id),
                  FOREIGN KEY(warehouse_id,station_id) REFERENCES {p}outbound_stations(warehouse_id,station_id)
                );
                CREATE TABLE IF NOT EXISTS {p}outbound_batch_orders(
                  warehouse_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                  order_id TEXT NOT NULL, chute_id TEXT NOT NULL, quantity INTEGER NOT NULL,
                  PRIMARY KEY(warehouse_id,batch_id,order_id,chute_id),
                  FOREIGN KEY(warehouse_id,batch_id) REFERENCES {p}outbound_batches(warehouse_id,batch_id) ON DELETE CASCADE,
                  FOREIGN KEY(warehouse_id,order_id) REFERENCES {p}orders(warehouse_id,order_id)
                );
                CREATE TABLE IF NOT EXISTS {p}inventory_reservations(
                  warehouse_id TEXT NOT NULL, reservation_id TEXT NOT NULL,
                  batch_id TEXT NOT NULL, handling_unit_id TEXT NOT NULL,
                  reserved_quantity INTEGER NOT NULL, expected_handling_unit_version INTEGER NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,reservation_id),
                  FOREIGN KEY(warehouse_id,batch_id) REFERENCES {p}outbound_batches(warehouse_id,batch_id)
                );
                CREATE TABLE IF NOT EXISTS {p}infrastructure_roundtrip(
                  probe_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {p}infrastructure_roundtrip_scoped(
                  warehouse_id TEXT NOT NULL, probe_id TEXT NOT NULL,
                  payload TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(warehouse_id,probe_id),
                  FOREIGN KEY(warehouse_id) REFERENCES {p}warehouses(warehouse_id) ON DELETE CASCADE
                );
                """
            )

    def ping(self) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT sqlite_version() AS version").fetchone()
        return {
            "database": str(self.path),
            "engine": "sqlite-embedded-postgres",
            "version": row["version"],
        }

    def apply_schema(self, _schema_path: Path) -> None:
        self.open()

    def seed_from_documents(
        self,
        *,
        warehouse_id: str | None = None,
        inventory: dict[str, Any],
        scenario: dict[str, Any],
        facility: dict[str, Any],
        replace: bool = True,
    ) -> dict[str, int]:
        wid = self._warehouse(warehouse_id)
        now = datetime.now(timezone.utc).isoformat()
        p = self.P
        with self._lock, self._connection() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {p}warehouses(warehouse_id,label,created_at,updated_at) VALUES (?,?,COALESCE((SELECT created_at FROM {p}warehouses WHERE warehouse_id=?),?),?)",
                (wid, str(scenario.get("warehouse_label") or wid), wid, now, now),
            )
            if replace:
                # Child rows are warehouse scoped; delete in dependency order.
                for table in [
                    "inventory_reservations", "outbound_batch_orders", "outbound_batches",
                    "inbound_receipts", "order_lines", "orders", "handling_units",
                    "rack_slots", "station_robots", "outbound_stations", "outbound_chutes",
                    "empty_tote_buffers", "inbound_ports", "inbound_handoffs", "racks",
                    "warehouse_meta",
                ]:
                    conn.execute(f"DELETE FROM {p}{table} WHERE warehouse_id=?", (wid,))

            for rack in inventory.get("racks", []):
                rack_id = str(rack["rack_id"])
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}racks(warehouse_id,rack_id,access_node_ids) VALUES (?,?,?)",
                    (wid, rack_id, _json(rack.get("access_node_ids", []))),
                )
                for level in rack.get("levels", []):
                    item = level.get("item") or {}
                    conn.execute(
                        f"INSERT OR REPLACE INTO {p}rack_slots(warehouse_id,rack_id,level,status,capacity) VALUES (?,?,?,?,?)",
                        (wid, rack_id, int(level["level"]), str(level["status"]), int(item.get("capacity", 0))),
                    )
                    if not item:
                        continue
                    hu_id = str(item.get("handling_unit_id") or f"HU-{rack_id}-L{level['level']}-{item['item_id']}")
                    stock_id = f"STOCK-{rack_id}-L{level['level']}-{item['item_id']}"
                    conn.execute(
                        f"""
                        INSERT OR REPLACE INTO {p}handling_units(
                          warehouse_id,handling_unit_id,stock_id,item_id,item_name,category,
                          quantity,capacity,unit,home_rack_id,home_rack_level,status,version,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT version FROM {p}handling_units WHERE warehouse_id=? AND handling_unit_id=?),0),?)
                        """,
                        (
                            wid, hu_id, stock_id, str(item["item_id"]), item.get("item_name"),
                            item.get("category"), int(item["quantity"]),
                            int(item.get("capacity", item["quantity"])), str(item.get("unit", "EA")),
                            rack_id, int(level["level"]), str(item.get("handling_unit_status", "stored")),
                            wid, hu_id, now,
                        ),
                    )

            # Facility masters must exist before orders and inbound receipts.
            for value in facility.get("inbound_handoffs", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}inbound_handoffs(warehouse_id,handoff_id,access_node_ids,buffer_capacity) VALUES (?,?,?,?)",
                    (wid, str(value["handoff_id"]), _json(value["access_node_ids"]), int(value["buffer_capacity"])),
                )
            for value in facility.get("inbound_ports", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}inbound_ports(warehouse_id,port_id,label,handoff_id) VALUES (?,?,?,?)",
                    (wid, str(value["port_id"]), str(value["label"]), str(value["handoff_id"])),
                )
            for value in facility.get("outbound_chutes", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}outbound_chutes(warehouse_id,chute_id,label) VALUES (?,?,?)",
                    (wid, str(value["chute_id"]), str(value["label"])),
                )
            for value in facility.get("outbound_stations", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}outbound_stations(warehouse_id,station_id,station_robot_id,access_node_ids,served_chute_ids,tote_buffer_capacity,status) VALUES (?,?,?,?,?,?,?)",
                    (
                        wid, str(value["station_id"]), str(value["station_robot_id"]),
                        _json(value["access_node_ids"]), _json(value["served_chute_ids"]),
                        int(value["tote_buffer_capacity"]), str(value.get("status", "available")),
                    ),
                )
            for value in facility.get("station_robots", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}station_robots(warehouse_id,station_robot_id,station_id,status,max_orders_per_wave,items_per_tick) VALUES (?,?,?,?,?,?)",
                    (
                        wid, str(value["station_robot_id"]), str(value["station_id"]),
                        str(value.get("status", "idle")), int(value.get("max_orders_per_wave", 16)),
                        int(value.get("items_per_tick", 1)),
                    ),
                )
            for value in facility.get("empty_tote_buffers", []):
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}empty_tote_buffers(warehouse_id,buffer_id,access_node_ids,capacity,status) VALUES (?,?,?,?,?)",
                    (
                        wid, str(value["buffer_id"]), _json(value["access_node_ids"]),
                        int(value.get("capacity", 1)), str(value.get("status", "available")),
                    ),
                )

            for receipt in scenario.get("inbound_receipts", []):
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO {p}inbound_receipts(
                      warehouse_id,inbound_id,handling_unit_id,item_id,quantity,source_port_id,
                      target_rack_id,target_rack_level,status,priority,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        wid, str(receipt["inbound_id"]), str(receipt["handling_unit_id"]),
                        str(receipt["item_id"]), int(receipt["quantity"]),
                        str(receipt["source_port_id"]),
                        (
                            str(receipt["target_rack_id"])
                            if receipt.get("target_rack_id") is not None
                            else None
                        ),
                        (
                            int(receipt["target_rack_level"])
                            if receipt.get("target_rack_level") is not None
                            else None
                        ),
                        str(receipt.get("status", "pending")),
                        str(receipt.get("priority", "medium")), now, now,
                    ),
                )
            for order in scenario.get("orders", []):
                chute = str(order.get("logical_destination_id") or order.get("outbound_chute_id") or order.get("delivery_node"))
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}orders(warehouse_id,order_id,status,priority,outbound_chute_id,preferred_station_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        wid, str(order["order_id"]), str(order.get("status", "pending")),
                        str(order.get("priority", "medium")), chute,
                        _json(order.get("preferred_station_ids", [])), str(order.get("created_at", now)),
                    ),
                )
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}order_lines(warehouse_id,order_id,line_no,item_id,required_qty) VALUES (?,?,?,?,?)",
                    (wid, str(order["order_id"]), 1, str(order["item_id"]), int(order["required_qty"])),
                )

            versions = {
                "inventory_version": _version(inventory),
                "business_version": _version({"orders": scenario.get("orders", []), "inbound_receipts": scenario.get("inbound_receipts", [])}),
                "facility_version": _version(facility),
            }
            for key, value in versions.items():
                conn.execute(
                    f"INSERT OR REPLACE INTO {p}warehouse_meta(warehouse_id,key,value,updated_at) VALUES (?,?,?,?)",
                    (wid, key, value, now),
                )
            conn.commit()
        return self.count_summary(wid)

    @staticmethod
    def _order_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "warehouse_id": row["warehouse_id"],
            "order_id": row["order_id"],
            "item_id": row["item_id"],
            "required_qty": int(row["required_qty"]),
            "delivery_node": row["outbound_chute_id"],
            "outbound_chute_id": row["outbound_chute_id"],
            "logical_destination_id": row["outbound_chute_id"],
            "priority": row["priority"],
            "status": row["status"],
            "preferred_station_ids": _loads(row["preferred_station_ids"], []),
        }

    @staticmethod
    def _inbound_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "warehouse_id": row["warehouse_id"],
            "inbound_id": row["inbound_id"],
            "handling_unit_id": row["handling_unit_id"],
            "item_id": row["item_id"],
            "quantity": int(row["quantity"]),
            "source_port_id": row["source_port_id"],
            "target_rack_id": row["target_rack_id"],
            "target_rack_level": (
                int(row["target_rack_level"])
                if row["target_rack_level"] is not None
                else None
            ),
            "status": row["status"],
            "priority": row["priority"],
        }

    def load_inbound_receipts(self, warehouse_id: str | None = None) -> list[dict[str, Any]]:
        wid = self._warehouse(warehouse_id)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM {self.P}inbound_receipts WHERE warehouse_id=? ORDER BY inbound_id",
                (wid,),
            ).fetchall()
        return [self._inbound_record(row) for row in rows]

    def get_inbound_receipt(self, warehouse_id: str, inbound_id: str | None = None) -> dict[str, Any] | None:
        # Backward compatibility: get_inbound_receipt(inbound_id)
        if inbound_id is None:
            inbound_id = warehouse_id
            warehouse_id = self.settings.default_warehouse_id
        wid = self._warehouse(warehouse_id)
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT * FROM {self.P}inbound_receipts WHERE warehouse_id=? AND inbound_id=?",
                (wid, inbound_id),
            ).fetchone()
        return self._inbound_record(row) if row else None

    def load_orders(self, warehouse_id: str | None = None) -> list[dict[str, Any]]:
        wid = self._warehouse(warehouse_id)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT o.*,l.item_id,l.required_qty FROM {self.P}orders o JOIN {self.P}order_lines l ON l.warehouse_id=o.warehouse_id AND l.order_id=o.order_id AND l.line_no=1 WHERE o.warehouse_id=? ORDER BY o.order_id",
                (wid,),
            ).fetchall()
        return [self._order_record(row) for row in rows]

    def get_order(self, warehouse_id: str, order_id: str | None = None) -> dict[str, Any] | None:
        # Backward compatibility: get_order(order_id)
        if order_id is None:
            order_id = warehouse_id
            warehouse_id = self.settings.default_warehouse_id
        wid = self._warehouse(warehouse_id)
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT o.*,l.item_id,l.required_qty FROM {self.P}orders o JOIN {self.P}order_lines l ON l.warehouse_id=o.warehouse_id AND l.order_id=o.order_id AND l.line_no=1 WHERE o.warehouse_id=? AND o.order_id=?",
                (wid, order_id),
            ).fetchone()
        return self._order_record(row) if row else None

    def find_orders(
        self,
        warehouse_id: str | None = None,
        *,
        order_ids: list[str] | None = None,
        item_ids: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        values = self.load_orders(warehouse_id)
        if order_ids:
            allowed = set(order_ids)
            values = [value for value in values if value["order_id"] in allowed]
        if item_ids:
            allowed = set(item_ids)
            values = [value for value in values if value["item_id"] in allowed]
        if statuses:
            allowed = set(statuses)
            values = [value for value in values if value["status"] in allowed]
        return values

    def handling_units(self, warehouse_id: str | None = None, item_id: str | None = None) -> list[dict[str, Any]]:
        # Backward compatibility: handling_units(item_id)
        if item_id is None and warehouse_id and str(warehouse_id).startswith("ITEM_"):
            item_id = str(warehouse_id)
            warehouse_id = None
        wid = self._warehouse(warehouse_id)
        query = f"SELECT h.*,r.access_node_ids FROM {self.P}handling_units h JOIN {self.P}racks r ON r.warehouse_id=h.warehouse_id AND r.rack_id=h.home_rack_id WHERE h.warehouse_id=? AND h.quantity>0"
        params: list[Any] = [wid]
        if item_id:
            query += " AND h.item_id=?"
            params.append(item_id)
        query += " ORDER BY h.handling_unit_id"
        with self._connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            {
                "warehouse_id": row["warehouse_id"],
                "handling_unit_id": row["handling_unit_id"],
                "stock_id": row["stock_id"],
                "item_id": row["item_id"],
                "item_name": row["item_name"],
                "category": row["category"],
                "quantity": int(row["quantity"]),
                "capacity": int(row["capacity"]),
                "unit": row["unit"],
                "rack_id": row["home_rack_id"],
                "rack_level": int(row["home_rack_level"]),
                "home_rack_id": row["home_rack_id"],
                "home_rack_level": int(row["home_rack_level"]),
                "handling_unit_status": row["status"],
                "version": int(row["version"]),
                "access_node_ids": _loads(row["access_node_ids"], []),
            }
            for row in rows
        ]

    def item_stocks(self, warehouse_id: str, item_id: str | None = None) -> list[dict[str, Any]]:
        # Backward compatibility: item_stocks(item_id)
        if item_id is None:
            item_id = warehouse_id
            warehouse_id = self.settings.default_warehouse_id
        return self.handling_units(warehouse_id, item_id)


    def list_warehouses(self) -> list[dict[str, Any]]:
        """Return durable warehouse registry rows."""

        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT warehouse_id,label,created_at,updated_at FROM {self.P}warehouses ORDER BY warehouse_id"
            ).fetchall()
        return [dict(value) for value in rows]

    def count_summary(self, warehouse_id: str | None = None) -> dict[str, int]:
        wid = self._warehouse(warehouse_id)
        names = ["racks", "rack_slots", "handling_units", "orders", "inbound_receipts", "outbound_stations", "empty_tote_buffers"]
        with self._connection() as conn:
            return {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {self.P}{name} WHERE warehouse_id=?", (wid,)).fetchone()[0])
                for name in names
            }

    def load_inventory_document(self, warehouse_id: str | None = None) -> dict[str, Any]:
        wid = self._warehouse(warehouse_id)
        p = self.P
        with self._connection() as conn:
            racks = conn.execute(f"SELECT * FROM {p}racks WHERE warehouse_id=? ORDER BY rack_id", (wid,)).fetchall()
            slots = conn.execute(
                f"""
                SELECT s.*,h.handling_unit_id,h.item_id,h.item_name,h.category,h.quantity,
                       h.capacity AS hu_capacity,h.unit,h.status AS hu_status,h.version
                FROM {p}rack_slots s
                LEFT JOIN {p}handling_units h ON h.warehouse_id=s.warehouse_id
                  AND h.home_rack_id=s.rack_id AND h.home_rack_level=s.level
                  AND h.status NOT IN ('empty_buffered')
                WHERE s.warehouse_id=?
                ORDER BY s.rack_id,s.level
                """,
                (wid,),
            ).fetchall()
        by_rack: dict[str, list[dict[str, Any]]] = {}
        occupied = 0
        for row in slots:
            item = None
            if row["item_id"] is not None:
                occupied += 1
                item = {
                    "item_id": row["item_id"], "item_name": row["item_name"],
                    "category": row["category"], "quantity": int(row["quantity"]),
                    "capacity": int(row["hu_capacity"]), "unit": row["unit"],
                    "handling_unit_id": row["handling_unit_id"],
                    "handling_unit_status": row["hu_status"], "version": int(row["version"]),
                }
            by_rack.setdefault(str(row["rack_id"]), []).append(
                {
                    "level": int(row["level"]),
                    "status": row["status"],
                    "capacity": int(row["capacity"]),
                    "item": item,
                }
            )
        records = [
            {"rack_id": row["rack_id"], "access_node_ids": _loads(row["access_node_ids"], []), "levels": by_rack.get(str(row["rack_id"]), [])}
            for row in racks
        ]
        level_count = sum(len(value["levels"]) for value in records)
        return {
            "warehouse_id": wid,
            "title": "Embedded business inventory",
            "updated_at": "local",
            "summary": {
                "rack_count": len(records), "level_count": level_count,
                "occupied_level_count": occupied, "empty_level_count": level_count - occupied,
                "partial_level_count": occupied, "full_level_count": 0,
                "handling_unit_count": occupied,
            },
            "racks": records,
        }

    def load_facility_document(self, warehouse_id: str | None = None) -> dict[str, Any]:
        wid = self._warehouse(warehouse_id)
        p = self.P
        with self._connection() as conn:
            ports = [dict(row) for row in conn.execute(f"SELECT * FROM {p}inbound_ports WHERE warehouse_id=? ORDER BY port_id", (wid,))]
            handoffs = [dict(row) for row in conn.execute(f"SELECT * FROM {p}inbound_handoffs WHERE warehouse_id=? ORDER BY handoff_id", (wid,))]
            chutes = [dict(row) for row in conn.execute(f"SELECT * FROM {p}outbound_chutes WHERE warehouse_id=? ORDER BY chute_id", (wid,))]
            stations = [dict(row) for row in conn.execute(f"SELECT * FROM {p}outbound_stations WHERE warehouse_id=? ORDER BY station_id", (wid,))]
            robots = [dict(row) for row in conn.execute(f"SELECT * FROM {p}station_robots WHERE warehouse_id=? ORDER BY station_robot_id", (wid,))]
            buffers = [dict(row) for row in conn.execute(f"SELECT * FROM {p}empty_tote_buffers WHERE warehouse_id=? ORDER BY buffer_id", (wid,))]
        for values in (ports, handoffs, chutes, stations, robots, buffers):
            for value in values:
                value.pop("warehouse_id", None)
        for value in handoffs:
            value["access_node_ids"] = _loads(value["access_node_ids"], [])
        for value in stations:
            value["access_node_ids"] = _loads(value["access_node_ids"], [])
            value["served_chute_ids"] = _loads(value["served_chute_ids"], [])
        for value in buffers:
            value["access_node_ids"] = _loads(value["access_node_ids"], [])
        return {
            "warehouse_id": wid, "version": "13.20-embedded",
            "inbound_ports": ports, "inbound_handoffs": handoffs,
            "outbound_chutes": chutes, "outbound_stations": stations,
            "station_robots": robots, "empty_tote_buffers": buffers,
        }

    def versions(self, warehouse_id: str | None = None) -> dict[str, str]:
        wid = self._warehouse(warehouse_id)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT key,value FROM {self.P}warehouse_meta WHERE warehouse_id=?",
                (wid,),
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def create_batch_reservation(
        self,
        *,
        warehouse_id: str | None = None,
        batch: dict[str, Any],
        allocations: Iterable[dict[str, Any]],
        expected_version: int,
    ) -> str:
        wid = self._warehouse(warehouse_id or batch.get("warehouse_id"))
        reservation_id = f"RES-{batch['batch_id']}"
        now = datetime.now(timezone.utc).isoformat()
        p = self.P
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT quantity,status,version FROM {p}handling_units WHERE warehouse_id=? AND handling_unit_id=?",
                (wid, batch["handling_unit_id"]),
            ).fetchone()
            if row is None:
                raise RuntimeError("handling unit does not exist")
            if int(row["version"]) != expected_version:
                raise RuntimeError("handling unit version changed")
            if row["status"] != "stored" or int(row["quantity"]) < int(batch["requested_quantity"]):
                raise RuntimeError("handling unit is not reservable")
            conn.execute(
                f"""
                INSERT INTO {p}outbound_batches(
                  warehouse_id,batch_id,simulation_id,item_id,handling_unit_id,station_id,
                  mobile_robot_id,post_station_node,post_station_action,requested_quantity,
                  quantity_before,quantity_after,return_required,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    wid, batch["batch_id"], batch["simulation_id"], batch["item_id"],
                    batch["handling_unit_id"], batch["station_id"], batch.get("mobile_robot_id"),
                    batch["post_station_node"], batch["post_station_action"],
                    int(batch["requested_quantity"]), int(batch["quantity_before"]),
                    int(batch["quantity_after"]), 1 if batch["return_required"] else 0,
                    "reserved", now, now,
                ),
            )
            for value in allocations:
                conn.execute(
                    f"INSERT INTO {p}outbound_batch_orders(warehouse_id,batch_id,order_id,chute_id,quantity) VALUES (?,?,?,?,?)",
                    (wid, batch["batch_id"], value["order_id"], value["chute_id"], int(value["quantity"])),
                )
            conn.execute(
                f"INSERT INTO {p}inventory_reservations(warehouse_id,reservation_id,batch_id,handling_unit_id,reserved_quantity,expected_handling_unit_version,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'active',?,?)",
                (wid, reservation_id, batch["batch_id"], batch["handling_unit_id"], int(batch["requested_quantity"]), expected_version, now, now),
            )
            conn.execute(
                f"UPDATE {p}handling_units SET status='reserved',version=version+1,updated_at=? WHERE warehouse_id=? AND handling_unit_id=?",
                (now, wid, batch["handling_unit_id"]),
            )
            conn.commit()
        return reservation_id

    def commit_station_pick(self, *, warehouse_id: str | None = None, batch_id: str) -> dict[str, Any]:
        wid = self._warehouse(warehouse_id)
        now = datetime.now(timezone.utc).isoformat()
        p = self.P
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                f"SELECT * FROM {p}outbound_batches WHERE warehouse_id=? AND batch_id=?",
                (wid, batch_id),
            ).fetchone()
            if batch is None:
                raise RuntimeError("batch does not exist")
            hu_status = "returning" if bool(batch["return_required"]) else "empty_in_transit"
            batch_status = "returning" if bool(batch["return_required"]) else "empty_repositioning"
            conn.execute(
                f"UPDATE {p}handling_units SET quantity=?,status=?,version=version+1,updated_at=? WHERE warehouse_id=? AND handling_unit_id=?",
                (int(batch["quantity_after"]), hu_status, now, wid, batch["handling_unit_id"]),
            )
            conn.execute(
                f"UPDATE {p}inventory_reservations SET status='committed',updated_at=? WHERE warehouse_id=? AND batch_id=?",
                (now, wid, batch_id),
            )
            conn.execute(
                f"UPDATE {p}outbound_batches SET status=?,updated_at=? WHERE warehouse_id=? AND batch_id=?",
                (batch_status, now, wid, batch_id),
            )
            order_ids = [row[0] for row in conn.execute(f"SELECT order_id FROM {p}outbound_batch_orders WHERE warehouse_id=? AND batch_id=?", (wid, batch_id))]
            for order_id in order_ids:
                required = int(conn.execute(f"SELECT COALESCE(SUM(required_qty),0) FROM {p}order_lines WHERE warehouse_id=? AND order_id=?", (wid, order_id)).fetchone()[0] or 0)
                committed = int(conn.execute(
                    f"""
                    SELECT COALESCE(SUM(bo.quantity),0)
                    FROM {p}outbound_batch_orders bo
                    JOIN {p}outbound_batches b ON b.warehouse_id=bo.warehouse_id AND b.batch_id=bo.batch_id
                    WHERE bo.warehouse_id=? AND bo.order_id=?
                      AND b.status IN ('returning','empty_repositioning','completed')
                    """,
                    (wid, order_id),
                ).fetchone()[0] or 0)
                if required > 0 and committed >= required:
                    conn.execute(f"UPDATE {p}orders SET status='fulfilled' WHERE warehouse_id=? AND order_id=?", (wid, order_id))
            conn.commit()
        return {"warehouse_id": wid, "batch_id": batch_id, "handling_unit_status": hu_status, "batch_status": batch_status}

    def complete_post_station_move(self, *, warehouse_id: str | None = None, batch_id: str, robot_id: str) -> dict[str, Any]:
        wid = self._warehouse(warehouse_id)
        now = datetime.now(timezone.utc).isoformat()
        p = self.P
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(f"SELECT * FROM {p}outbound_batches WHERE warehouse_id=? AND batch_id=?", (wid, batch_id)).fetchone()
            if batch is None:
                raise RuntimeError("batch does not exist")
            if batch["mobile_robot_id"] and str(batch["mobile_robot_id"]) != robot_id:
                raise RuntimeError("post-station completion robot does not match the planned mobile robot")
            hu_status = "stored" if bool(batch["return_required"]) else "empty_buffered"
            conn.execute(
                f"UPDATE {p}handling_units SET status=?,version=version+1,updated_at=? WHERE warehouse_id=? AND handling_unit_id=?",
                (hu_status, now, wid, batch["handling_unit_id"]),
            )
            conn.execute(
                f"UPDATE {p}outbound_batches SET status='completed',updated_at=? WHERE warehouse_id=? AND batch_id=?",
                (now, wid, batch_id),
            )
            conn.commit()
        return {"warehouse_id": wid, "batch_id": batch_id, "handling_unit_status": hu_status, "batch_status": "completed"}

    def roundtrip(self, probe_id: str, *, warehouse_id: str | None = None) -> dict[str, Any]:
        wid = self._warehouse(warehouse_id)
        payload = {"probe": probe_id, "component": "embedded-postgres", "warehouse_id": wid}
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {self.P}infrastructure_roundtrip_scoped(warehouse_id,probe_id,payload,created_at) VALUES (?,?,?,?)",
                (wid, probe_id, _json(payload), now),
            )
            row = conn.execute(
                f"SELECT payload FROM {self.P}infrastructure_roundtrip_scoped WHERE warehouse_id=? AND probe_id=?",
                (wid, probe_id),
            ).fetchone()
            conn.execute(
                f"DELETE FROM {self.P}infrastructure_roundtrip_scoped WHERE warehouse_id=? AND probe_id=?",
                (wid, probe_id),
            )
            conn.commit()
        return {"probe_id": probe_id, "payload": _loads(row["payload"], {})}


class EmbeddedRedisRuntimeAdapter:
    """SQLite implementation of warehouse-scoped Redis runtime contracts."""

    def __init__(self, settings: Settings | None = None, path: Path | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(path or self.settings.local_redis_db_path)
        self._lock = RLock()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS v13_19_robots(warehouse_id TEXT,simulation_id TEXT,robot_id TEXT,payload TEXT,sequence INTEGER,PRIMARY KEY(warehouse_id,simulation_id,robot_id));
                CREATE TABLE IF NOT EXISTS v13_19_edges(warehouse_id TEXT,simulation_id TEXT,edge_id TEXT,payload TEXT,PRIMARY KEY(warehouse_id,simulation_id,edge_id));
                CREATE TABLE IF NOT EXISTS v13_19_stations(warehouse_id TEXT,simulation_id TEXT,station_id TEXT,payload TEXT,PRIMARY KEY(warehouse_id,simulation_id,station_id));
                CREATE TABLE IF NOT EXISTS v13_19_reservations(warehouse_id TEXT,simulation_id TEXT,reservation_id TEXT,payload TEXT,PRIMARY KEY(warehouse_id,simulation_id,reservation_id));
                CREATE TABLE IF NOT EXISTS v13_19_runtime_meta(warehouse_id TEXT,simulation_id TEXT,runtime_version INTEGER NOT NULL,PRIMARY KEY(warehouse_id,simulation_id));
                CREATE TABLE IF NOT EXISTS v13_19_streams(id INTEGER PRIMARY KEY AUTOINCREMENT,warehouse_id TEXT,simulation_id TEXT,stream_type TEXT,payload TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS roundtrip(probe_id TEXT PRIMARY KEY,payload TEXT);
                """
            )

    def close(self) -> None:
        return None

    @contextmanager
    def _connection(self):
        self.open()
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _scope(self, warehouse_id: str, simulation_id: str | None = None) -> tuple[str, str]:
        from app.domain.schemas import normalize_warehouse_id
        if simulation_id is None:
            simulation_id = warehouse_id
            warehouse_id = self.settings.default_warehouse_id
        return normalize_warehouse_id(warehouse_id), str(simulation_id)

    def ping(self) -> dict[str, Any]:
        with self._connection() as conn:
            version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        return {"database": str(self.path), "engine": "sqlite-embedded-redis", "version": version}

    def seed_from_documents(self, *, scenario: dict[str, Any], facility: dict[str, Any], warehouse_id: str | None = None, replace: bool = True) -> dict[str, int]:
        from app.domain.schemas import normalize_warehouse_id
        warehouse_id = normalize_warehouse_id(warehouse_id or scenario.get("warehouse_id") or self.settings.default_warehouse_id)
        simulation_id = str(scenario.get("simulation_id", "SIM001"))
        with self._lock, self._connection() as conn:
            if replace:
                for table in ("v13_19_robots", "v13_19_edges", "v13_19_stations", "v13_19_reservations", "v13_19_streams", "v13_19_runtime_meta"):
                    conn.execute(f"DELETE FROM {table} WHERE warehouse_id=? AND simulation_id=?", (warehouse_id, simulation_id))
            for robot in scenario.get("robots", []):
                payload = {**robot, "warehouse_id": warehouse_id, "state_version": int(robot.get("state_version", 1)), "sim_time_ms": int(robot.get("sim_time_ms", 0))}
                conn.execute("INSERT OR REPLACE INTO v13_19_robots(warehouse_id,simulation_id,robot_id,payload,sequence) VALUES (?,?,?,?,?)", (warehouse_id, simulation_id, str(robot["robot_id"]), _json(payload), int(robot.get("sequence", 0))))
            for edge in scenario.get("edge_runtime", []):
                conn.execute("INSERT OR REPLACE INTO v13_19_edges(warehouse_id,simulation_id,edge_id,payload) VALUES (?,?,?,?)", (warehouse_id, simulation_id, str(edge["edge_id"]), _json({**edge, "warehouse_id": warehouse_id})))
            for station in facility.get("outbound_stations", []):
                payload = {"warehouse_id": warehouse_id, "station_id": str(station["station_id"]), "station_robot_id": str(station["station_robot_id"]), "status": str(station.get("status", "available")), "active_handling_unit_id": "", "queue_depth": 0, "available_at_ms": 0, "state_version": 1}
                conn.execute("INSERT OR REPLACE INTO v13_19_stations(warehouse_id,simulation_id,station_id,payload) VALUES (?,?,?,?)", (warehouse_id, simulation_id, payload["station_id"], _json(payload)))
            for reservation in scenario.get("edge_reservations", []):
                reservation_id = str(reservation.get("reservation_id") or f"SEED-{reservation.get('edge_id')}-{reservation.get('robot_id')}")
                conn.execute("INSERT OR REPLACE INTO v13_19_reservations(warehouse_id,simulation_id,reservation_id,payload) VALUES (?,?,?,?)", (warehouse_id, simulation_id, reservation_id, _json({**reservation, "warehouse_id": warehouse_id, "reservation_id": reservation_id})))
            conn.execute("INSERT OR REPLACE INTO v13_19_runtime_meta(warehouse_id,simulation_id,runtime_version) VALUES (?,?,1)", (warehouse_id, simulation_id))
            conn.commit()
        return {"robots": len(scenario.get("robots", [])), "edges": len(scenario.get("edge_runtime", [])), "stations": len(facility.get("outbound_stations", [])), "reservations": len(scenario.get("edge_reservations", []))}

    def _rows(self, table: str, warehouse_id: str, simulation_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(f"SELECT payload FROM {table} WHERE warehouse_id=? AND simulation_id=? ORDER BY 1", (warehouse_id, simulation_id)).fetchall()
        return [_loads(row["payload"], {}) for row in rows]

    def all_robots(self, warehouse_id: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse_id, simulation_id = self._scope(warehouse_id, simulation_id)
        return self._rows("v13_19_robots", warehouse_id, simulation_id)

    def list_simulation_ids(self, warehouse_id: str) -> list[str]:
        warehouse_id, _ = self._scope(warehouse_id, "IGNORED")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT simulation_id FROM v13_19_robots WHERE warehouse_id=? ORDER BY simulation_id",
                (warehouse_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def edge_runtime(self, warehouse_id: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse_id, simulation_id = self._scope(warehouse_id, simulation_id)
        return self._rows("v13_19_edges", warehouse_id, simulation_id)

    def station_runtime(self, warehouse_id: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse_id, simulation_id = self._scope(warehouse_id, simulation_id)
        return self._rows("v13_19_stations", warehouse_id, simulation_id)

    def get_robot(self, warehouse_id: str, simulation_id: str, robot_id: str | None = None) -> dict[str, Any] | None:
        if robot_id is None:
            robot_id = simulation_id
            simulation_id = warehouse_id
            warehouse_id = self.settings.default_warehouse_id
        warehouse_id, simulation_id = self._scope(warehouse_id, simulation_id)
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM v13_19_robots WHERE warehouse_id=? AND simulation_id=? AND robot_id=?", (warehouse_id, simulation_id, robot_id)).fetchone()
        return _loads(row["payload"], {}) if row else None

    def existing_reservations(self, warehouse_id: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse_id, simulation_id = self._scope(warehouse_id, simulation_id)
        return self._rows("v13_19_reservations", warehouse_id, simulation_id)

    def _bump(self, conn: sqlite3.Connection, warehouse_id: str, simulation_id: str) -> int:
        row = conn.execute("SELECT runtime_version FROM v13_19_runtime_meta WHERE warehouse_id=? AND simulation_id=?", (warehouse_id, simulation_id)).fetchone()
        value = int(row[0]) + 1 if row else 1
        conn.execute("INSERT OR REPLACE INTO v13_19_runtime_meta(warehouse_id,simulation_id,runtime_version) VALUES (?,?,?)", (warehouse_id, simulation_id, value))
        return value

    def reserve_edges(self, *, warehouse_id: str | None = None, simulation_id: str, reservations: list[dict[str, Any]]) -> int:
        warehouse_id, simulation_id = self._scope(warehouse_id or self.settings.default_warehouse_id, simulation_id)
        with self._lock, self._connection() as conn:
            for reservation in reservations:
                conn.execute("INSERT OR REPLACE INTO v13_19_reservations(warehouse_id,simulation_id,reservation_id,payload) VALUES (?,?,?,?)", (warehouse_id, simulation_id, str(reservation["reservation_id"]), _json({**reservation, "warehouse_id": warehouse_id})))
            self._bump(conn, warehouse_id, simulation_id)
            conn.commit()
        return len(reservations)

    def update_robot_state(self, *, warehouse_id: str | None = None, simulation_id: str, robot_id: str, state: dict[str, Any], sequence: int) -> bool:
        warehouse_id, simulation_id = self._scope(warehouse_id or self.settings.default_warehouse_id, simulation_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT sequence,payload FROM v13_19_robots WHERE warehouse_id=? AND simulation_id=? AND robot_id=?", (warehouse_id, simulation_id, robot_id)).fetchone()
            if row and int(row["sequence"]) >= sequence:
                conn.rollback(); return False
            payload = {**(_loads(row["payload"], {}) if row else {}), **state, "warehouse_id": warehouse_id, "robot_id": robot_id, "sequence": sequence}
            conn.execute("INSERT OR REPLACE INTO v13_19_robots(warehouse_id,simulation_id,robot_id,payload,sequence) VALUES (?,?,?,?,?)", (warehouse_id, simulation_id, robot_id, _json(payload), sequence))
            conn.execute("INSERT INTO v13_19_streams(warehouse_id,simulation_id,stream_type,payload,created_at) VALUES (?,?,?,?,?)", (warehouse_id, simulation_id, "telemetry", _json(payload), now))
            self._bump(conn, warehouse_id, simulation_id); conn.commit()
        return True

    def publish_command(self, *, warehouse_id: str | None = None, simulation_id: str, command: dict[str, Any]) -> str:
        warehouse_id, simulation_id = self._scope(warehouse_id or self.settings.default_warehouse_id, simulation_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cur = conn.execute("INSERT INTO v13_19_streams(warehouse_id,simulation_id,stream_type,payload,created_at) VALUES (?,?,?,?,?)", (warehouse_id, simulation_id, "commands", _json({**command, "warehouse_id": warehouse_id}), now))
            conn.commit(); return str(cur.lastrowid)

    def runtime_version(self, warehouse_id: str, simulation_id: str | None = None) -> str:
        warehouse_id, simulation_id = self._scope(warehouse_id, simulation_id)
        with self._connection() as conn:
            row = conn.execute("SELECT runtime_version FROM v13_19_runtime_meta WHERE warehouse_id=? AND simulation_id=?", (warehouse_id, simulation_id)).fetchone()
        return str(row[0] if row else 0)

    def clone_simulation_runtime(
        self,
        *,
        warehouse_id: str,
        source_simulation_id: str,
        target_simulation_id: str,
        reset: bool = True,
        copy_robot_runtime: bool = True,
        copy_edge_runtime: bool = True,
        copy_station_runtime: bool = True,
        copy_reservations: bool = False,
    ) -> dict[str, Any]:
        warehouse_id, source = self._scope(warehouse_id, source_simulation_id)
        _, target = self._scope(warehouse_id, target_simulation_id)
        source_version = self.runtime_version(warehouse_id, source)
        if source == target:
            return {
                "status": "NOOP",
                "warehouse_id": warehouse_id,
                "source_simulation_id": source,
                "target_simulation_id": target,
                "robots": len(self.all_robots(warehouse_id, source)),
                "edges": len(self.edge_runtime(warehouse_id, source)),
                "stations": len(self.station_runtime(warehouse_id, source)),
                "reservations": len(self.existing_reservations(warehouse_id, source)),
                "source_runtime_version": source_version,
                "target_runtime_version": source_version,
            }

        tables = {
            "v13_19_robots": ("robot_id", copy_robot_runtime),
            "v13_19_edges": ("edge_id", copy_edge_runtime),
            "v13_19_stations": ("station_id", copy_station_runtime),
            "v13_19_reservations": ("reservation_id", copy_reservations),
        }
        counts: dict[str, int] = {}
        with self._lock, self._connection() as conn:
            if copy_robot_runtime:
                source_robot_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM v13_19_robots WHERE warehouse_id=? AND simulation_id=?",
                        (warehouse_id, source),
                    ).fetchone()[0]
                )
                if source_robot_count == 0:
                    raise RuntimeError(
                        f"Source simulation {source} has no robot runtime for warehouse {warehouse_id}."
                    )
            if reset:
                for table in (*tables, "v13_19_streams", "v13_19_runtime_meta"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE warehouse_id=? AND simulation_id=?",
                        (warehouse_id, target),
                    )
            for table, (id_field, enabled) in tables.items():
                if not enabled:
                    counts[table] = 0
                    continue
                rows = conn.execute(
                    f"SELECT {id_field},payload FROM {table} WHERE warehouse_id=? AND simulation_id=?",
                    (warehouse_id, source),
                ).fetchall()
                for row in rows:
                    payload = _loads(row["payload"], {})
                    payload.update(
                        {
                            "warehouse_id": warehouse_id,
                            "simulation_id": target,
                        }
                    )
                    if table == "v13_19_robots":
                        sequence = int(payload.get("sequence", 0))
                        conn.execute(
                            "INSERT OR REPLACE INTO v13_19_robots(warehouse_id,simulation_id,robot_id,payload,sequence) VALUES (?,?,?,?,?)",
                            (warehouse_id, target, row[id_field], _json(payload), sequence),
                        )
                    else:
                        conn.execute(
                            f"INSERT OR REPLACE INTO {table}(warehouse_id,simulation_id,{id_field},payload) VALUES (?,?,?,?)",
                            (warehouse_id, target, row[id_field], _json(payload)),
                        )
                counts[table] = len(rows)
            conn.execute(
                "INSERT OR REPLACE INTO v13_19_runtime_meta(warehouse_id,simulation_id,runtime_version) VALUES (?,?,1)",
                (warehouse_id, target),
            )
            conn.commit()
        return {
            "status": "BOOTSTRAPPED",
            "warehouse_id": warehouse_id,
            "source_simulation_id": source,
            "target_simulation_id": target,
            "robots": counts.get("v13_19_robots", 0),
            "edges": counts.get("v13_19_edges", 0),
            "stations": counts.get("v13_19_stations", 0),
            "reservations": counts.get("v13_19_reservations", 0),
            "source_runtime_version": source_version,
            "target_runtime_version": self.runtime_version(warehouse_id, target),
        }

    def roundtrip(self, probe_id: str, warehouse_id: str | None = None) -> dict[str, Any]:
        from app.domain.schemas import normalize_warehouse_id
        wid = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        scoped_probe = f"{wid}::{probe_id}"
        payload = {"probe": probe_id, "component": "embedded-redis", "warehouse_id": wid}
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO roundtrip(probe_id,payload) VALUES (?,?)", (scoped_probe, _json(payload)))
            row = conn.execute("SELECT payload FROM roundtrip WHERE probe_id=?", (scoped_probe,)).fetchone()
            conn.execute("DELETE FROM roundtrip WHERE probe_id=?", (scoped_probe,)); conn.commit()
        return _loads(row["payload"], {})


class EmbeddedNeo4jMapRepository:
    """SQLite materialization of warehouse-scoped Neo4j route projections."""

    def __init__(self, settings: Settings | None = None, path: Path | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(path or self.settings.local_neo4j_db_path)

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS v13_19_route_nodes(warehouse_id TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(warehouse_id,id));
                CREATE TABLE IF NOT EXISTS v13_19_route_edges(warehouse_id TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(warehouse_id,id));
                CREATE TABLE IF NOT EXISTS roundtrip(probe_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                """
            )

    def close(self) -> None:
        return None

    @contextmanager
    def _connection(self):
        self.open(); conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False); conn.row_factory = sqlite3.Row
        try: yield conn
        finally: conn.close()

    def ping(self) -> dict[str, Any]:
        with self._connection() as conn: version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        return {"database": str(self.path), "engine": "sqlite-embedded-neo4j", "version": version}

    def load_route_graph(self, *, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], warehouse_id: str | None = None, replace: bool = True) -> Neo4jRouteGraphSnapshot:
        from app.domain.schemas import normalize_warehouse_id
        warehouse_id = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        scoped_nodes = [{**value, "warehouse_id": warehouse_id} for value in nodes]
        scoped_edges = [{**value, "warehouse_id": warehouse_id} for value in edges]
        Neo4jMapRepository.validate_snapshot(scoped_nodes, scoped_edges)
        with self._connection() as conn:
            if replace:
                conn.execute("DELETE FROM v13_19_route_edges WHERE warehouse_id=?", (warehouse_id,)); conn.execute("DELETE FROM v13_19_route_nodes WHERE warehouse_id=?", (warehouse_id,))
            for value in scoped_nodes: conn.execute("INSERT OR REPLACE INTO v13_19_route_nodes(warehouse_id,id,payload) VALUES (?,?,?)", (warehouse_id, str(value["id"]), _json(value)))
            for value in scoped_edges: conn.execute("INSERT OR REPLACE INTO v13_19_route_edges(warehouse_id,id,payload) VALUES (?,?,?)", (warehouse_id, str(value["id"]), _json(value)))
            conn.commit()
        return Neo4jRouteGraphSnapshot(nodes=scoped_nodes, edges=scoped_edges, version=_version({"warehouse_id": warehouse_id, "nodes": scoped_nodes, "edges": scoped_edges}))

    def fetch_route_graph(self, warehouse_id: str | None = None) -> Neo4jRouteGraphSnapshot:
        from app.domain.schemas import normalize_warehouse_id
        warehouse_id = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        with self._connection() as conn:
            nodes = [_loads(row["payload"], {}) for row in conn.execute("SELECT payload FROM v13_19_route_nodes WHERE warehouse_id=? ORDER BY id", (warehouse_id,))]
            edges = [_loads(row["payload"], {}) for row in conn.execute("SELECT payload FROM v13_19_route_edges WHERE warehouse_id=? ORDER BY id", (warehouse_id,))]
        Neo4jMapRepository.validate_snapshot(nodes, edges)
        return Neo4jRouteGraphSnapshot(nodes=nodes, edges=edges, version=_version({"warehouse_id": warehouse_id, "nodes": nodes, "edges": edges}))

    def graph_counts(self, warehouse_id: str | None = None) -> dict[str, int]:
        snapshot = self.fetch_route_graph(warehouse_id)
        return {"nodes": len(snapshot.nodes), "edges": len(snapshot.edges)}

    def roundtrip(self, probe_id: str, warehouse_id: str | None = None) -> dict[str, Any]:
        from app.domain.schemas import normalize_warehouse_id
        wid = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        scoped_probe = f"{wid}::{probe_id}"
        payload = {"probe": probe_id, "component": "embedded-neo4j", "warehouse_id": wid}
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO roundtrip(probe_id,payload) VALUES (?,?)", (scoped_probe, _json(payload)))
            row = conn.execute("SELECT payload FROM roundtrip WHERE probe_id=?", (scoped_probe,)).fetchone(); conn.execute("DELETE FROM roundtrip WHERE probe_id=?", (scoped_probe,)); conn.commit()
        return {"probe_id": probe_id, "payload": _loads(row["payload"], {})}


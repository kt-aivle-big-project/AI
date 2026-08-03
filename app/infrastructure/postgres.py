"""Warehouse-scoped PostgreSQL source-of-truth adapter using psycopg 3."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from app.core.config import Settings, get_settings
from app.domain.schemas import normalize_warehouse_id


class PostgresInfrastructureError(RuntimeError):
    pass


class PostgresWarehouseAdapter:
    """Durable orders, inbound receipts, inventory and facility resources.

    Every business query is scoped by ``warehouse_id``.  One-argument read
    calls remain supported for legacy scripts and use ``DEFAULT_WAREHOUSE_ID``.
    """

    def __init__(self, settings: Settings | None = None, pool: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool = pool
        self._owns_pool = pool is None

    def _warehouse(self, value: str | None = None) -> str:
        return normalize_warehouse_id(value or self.settings.default_warehouse_id)

    def open(self) -> None:
        if self._pool is not None:
            return
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except Exception as exc:  # pragma: no cover
            raise PostgresInfrastructureError(
                "Live PostgreSQL requires psycopg[binary] and psycopg_pool."
            ) from exc
        pool = ConnectionPool(
            conninfo=self.settings.postgres_dsn,
            min_size=self.settings.postgres_pool_min_size,
            max_size=self.settings.postgres_pool_max_size,
            timeout=self.settings.postgres_pool_open_timeout_seconds,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": self.settings.postgres_connect_timeout_seconds,
            },
            open=False,
        )
        try:
            pool.open(
                wait=True,
                timeout=self.settings.postgres_pool_open_timeout_seconds,
            )
        except Exception:
            # Do not retain a half-open pool.  In particular, a failed CLI
            # probe must not reach interpreter shutdown with connection worker
            # threads still retrying in the background.
            try:
                pool.close(timeout=1.0)
            except Exception:
                pass
            raise
        self._pool = pool

    def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            self._pool.close()
        self._pool = None

    def _connection(self):
        self.open()
        return self._pool.connection()

    def ping(self) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT current_database() AS database, now() AS server_time").fetchone()
        return dict(row)

    def apply_schema(self, schema_path: Path) -> None:
        with self._connection() as conn:
            conn.execute(schema_path.read_text(encoding="utf-8"))
            conn.commit()

    def _delete_scope(self, conn: Any, warehouse_id: str) -> None:
        for table in (
            "inventory_reservations","outbound_batch_orders","outbound_batches",
            "inbound_receipts","order_lines","orders","handling_units","rack_slots","racks",
            "station_robots","outbound_stations","outbound_chutes","empty_tote_buffers",
            "inbound_ports","inbound_handoffs","warehouse_meta",
        ):
            conn.execute(f"DELETE FROM {table} WHERE warehouse_id=%s", (warehouse_id,))

    def seed_from_documents(
        self,
        *,
        inventory: dict[str, Any],
        scenario: dict[str, Any],
        facility: dict[str, Any],
        warehouse_id: str | None = None,
        replace: bool = True,
    ) -> dict[str, int]:
        """Seed one warehouse in foreign-key-safe order.

        Facility masters must exist before orders and inbound receipts reference
        their chute/port IDs.  Keeping the order explicit also makes Docker
        bootstrap failures deterministic instead of depending on deferred
        constraints or stale volumes.
        """

        wid = self._warehouse(warehouse_id)
        with self._connection() as conn:
            if replace:
                self._delete_scope(conn, wid)

            conn.execute(
                "INSERT INTO warehouses(warehouse_id,label,active) VALUES (%s,%s,true) "
                "ON CONFLICT (warehouse_id) DO UPDATE SET "
                "label=EXCLUDED.label,active=true,updated_at=now()",
                (wid, str(scenario.get("warehouse_label") or wid)),
            )

            # 1. Inventory masters and stored handling units.
            for rack in inventory.get("racks", []):
                rack_id = str(rack["rack_id"])
                conn.execute(
                    "INSERT INTO racks(warehouse_id,rack_id,access_node_ids) "
                    "VALUES (%s,%s,%s::jsonb) "
                    "ON CONFLICT (warehouse_id,rack_id) DO UPDATE SET "
                    "access_node_ids=EXCLUDED.access_node_ids",
                    (wid, rack_id, json.dumps(rack.get("access_node_ids", []))),
                )
                for level in rack.get("levels", []):
                    item = level.get("item") or {}
                    conn.execute(
                        "INSERT INTO rack_slots(warehouse_id,rack_id,level,status,capacity) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (warehouse_id,rack_id,level) DO UPDATE SET "
                        "status=EXCLUDED.status,capacity=EXCLUDED.capacity",
                        (
                            wid,
                            rack_id,
                            int(level["level"]),
                            str(level["status"]),
                            int(item.get("capacity", 0)),
                        ),
                    )
                    if not item:
                        continue
                    handling_unit_id = str(
                        item.get("handling_unit_id")
                        or f"HU-{rack_id}-L{level['level']}-{item['item_id']}"
                    )
                    stock_id = str(
                        item.get("stock_id")
                        or f"STOCK-{rack_id}-L{level['level']}-{item['item_id']}"
                    )
                    conn.execute(
                        """INSERT INTO handling_units(
                        warehouse_id,handling_unit_id,stock_id,item_id,item_name,category,
                        quantity,capacity,unit,home_rack_id,home_rack_level,status,version)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (warehouse_id,handling_unit_id) DO UPDATE SET
                        stock_id=EXCLUDED.stock_id,item_id=EXCLUDED.item_id,
                        item_name=EXCLUDED.item_name,category=EXCLUDED.category,
                        quantity=EXCLUDED.quantity,capacity=EXCLUDED.capacity,
                        unit=EXCLUDED.unit,home_rack_id=EXCLUDED.home_rack_id,
                        home_rack_level=EXCLUDED.home_rack_level,status=EXCLUDED.status,
                        version=EXCLUDED.version,updated_at=now()""",
                        (
                            wid,
                            handling_unit_id,
                            stock_id,
                            str(item["item_id"]),
                            item.get("item_name"),
                            item.get("category"),
                            int(item["quantity"]),
                            int(item.get("capacity", item["quantity"])),
                            str(item.get("unit", "EA")),
                            rack_id,
                            int(level["level"]),
                            str(item.get("handling_unit_status", "stored")),
                            int(item.get("version", 0)),
                        ),
                    )

            # 2. Facility masters. Orders and receipts reference these rows.
            for value in facility.get("inbound_handoffs", []):
                conn.execute(
                    "INSERT INTO inbound_handoffs(warehouse_id,handoff_id,access_node_ids,buffer_capacity) "
                    "VALUES (%s,%s,%s::jsonb,%s) "
                    "ON CONFLICT (warehouse_id,handoff_id) DO UPDATE SET "
                    "access_node_ids=EXCLUDED.access_node_ids,buffer_capacity=EXCLUDED.buffer_capacity",
                    (
                        wid,
                        str(value["handoff_id"]),
                        json.dumps(value["access_node_ids"]),
                        int(value["buffer_capacity"]),
                    ),
                )
            for value in facility.get("inbound_ports", []):
                conn.execute(
                    "INSERT INTO inbound_ports(warehouse_id,port_id,label,handoff_id) "
                    "VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (warehouse_id,port_id) DO UPDATE SET "
                    "label=EXCLUDED.label,handoff_id=EXCLUDED.handoff_id",
                    (
                        wid,
                        str(value["port_id"]),
                        str(value["label"]),
                        str(value["handoff_id"]),
                    ),
                )
            for value in facility.get("outbound_chutes", []):
                conn.execute(
                    "INSERT INTO outbound_chutes(warehouse_id,chute_id,label) VALUES (%s,%s,%s) "
                    "ON CONFLICT (warehouse_id,chute_id) DO UPDATE SET label=EXCLUDED.label",
                    (wid, str(value["chute_id"]), str(value["label"])),
                )
            for value in facility.get("outbound_stations", []):
                conn.execute(
                    """INSERT INTO outbound_stations(
                    warehouse_id,station_id,station_robot_id,access_node_ids,
                    served_chute_ids,tote_buffer_capacity,status)
                    VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                    ON CONFLICT (warehouse_id,station_id) DO UPDATE SET
                    station_robot_id=EXCLUDED.station_robot_id,
                    access_node_ids=EXCLUDED.access_node_ids,
                    served_chute_ids=EXCLUDED.served_chute_ids,
                    tote_buffer_capacity=EXCLUDED.tote_buffer_capacity,
                    status=EXCLUDED.status""",
                    (
                        wid,
                        str(value["station_id"]),
                        str(value["station_robot_id"]),
                        json.dumps(value["access_node_ids"]),
                        json.dumps(value["served_chute_ids"]),
                        int(value["tote_buffer_capacity"]),
                        str(value.get("status", "available")),
                    ),
                )
            for value in facility.get("station_robots", []):
                conn.execute(
                    """INSERT INTO station_robots(
                    warehouse_id,station_robot_id,station_id,status,max_orders_per_wave,items_per_tick)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (warehouse_id,station_robot_id) DO UPDATE SET
                    station_id=EXCLUDED.station_id,status=EXCLUDED.status,
                    max_orders_per_wave=EXCLUDED.max_orders_per_wave,
                    items_per_tick=EXCLUDED.items_per_tick""",
                    (
                        wid,
                        str(value["station_robot_id"]),
                        str(value["station_id"]),
                        str(value.get("status", "idle")),
                        int(value.get("max_orders_per_wave", 16)),
                        int(value.get("items_per_tick", 1)),
                    ),
                )
            for value in facility.get("empty_tote_buffers", []):
                conn.execute(
                    """INSERT INTO empty_tote_buffers(
                    warehouse_id,buffer_id,access_node_ids,capacity,status)
                    VALUES (%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT (warehouse_id,buffer_id) DO UPDATE SET
                    access_node_ids=EXCLUDED.access_node_ids,
                    capacity=EXCLUDED.capacity,status=EXCLUDED.status""",
                    (
                        wid,
                        str(value["buffer_id"]),
                        json.dumps(value["access_node_ids"]),
                        int(value.get("capacity", 1)),
                        str(value.get("status", "available")),
                    ),
                )

            # 3. Business documents that reference facility and inventory masters.
            for receipt in scenario.get("inbound_receipts", []):
                conn.execute(
                    """INSERT INTO inbound_receipts(
                    warehouse_id,inbound_id,handling_unit_id,item_id,quantity,
                    source_port_id,target_rack_id,target_rack_level,status,priority)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (warehouse_id,inbound_id) DO UPDATE SET
                    handling_unit_id=EXCLUDED.handling_unit_id,item_id=EXCLUDED.item_id,
                    quantity=EXCLUDED.quantity,source_port_id=EXCLUDED.source_port_id,
                    target_rack_id=EXCLUDED.target_rack_id,
                    target_rack_level=EXCLUDED.target_rack_level,
                    status=EXCLUDED.status,priority=EXCLUDED.priority,updated_at=now()""",
                    (
                        wid,
                        str(receipt["inbound_id"]),
                        str(receipt["handling_unit_id"]),
                        str(receipt["item_id"]),
                        int(receipt["quantity"]),
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
                        str(receipt.get("priority", "medium")),
                    ),
                )

            for order in scenario.get("orders", []):
                order_id = str(order["order_id"])
                chute_id = str(
                    order.get("logical_destination_id")
                    or order.get("outbound_chute_id")
                    or order.get("delivery_node")
                )
                conn.execute(
                    """INSERT INTO orders(
                    warehouse_id,order_id,status,priority,outbound_chute_id,preferred_station_ids)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (warehouse_id,order_id) DO UPDATE SET
                    status=EXCLUDED.status,priority=EXCLUDED.priority,
                    outbound_chute_id=EXCLUDED.outbound_chute_id,
                    preferred_station_ids=EXCLUDED.preferred_station_ids""",
                    (
                        wid,
                        order_id,
                        str(order.get("status", "pending")),
                        str(order.get("priority", "medium")),
                        chute_id,
                        json.dumps(order.get("preferred_station_ids", [])),
                    ),
                )
                lines = order.get("lines") or [
                    {
                        "item_id": order["item_id"],
                        "required_qty": order["required_qty"],
                    }
                ]
                for line_no, line in enumerate(lines, 1):
                    conn.execute(
                        "INSERT INTO order_lines(warehouse_id,order_id,line_no,item_id,required_qty) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (warehouse_id,order_id,line_no) DO UPDATE SET "
                        "item_id=EXCLUDED.item_id,required_qty=EXCLUDED.required_qty",
                        (
                            wid,
                            order_id,
                            line_no,
                            str(line["item_id"]),
                            int(line["required_qty"]),
                        ),
                    )

            versions = {
                "inventory_version": self._hash(inventory),
                "business_version": self._hash(
                    {
                        "orders": scenario.get("orders", []),
                        "inbound_receipts": scenario.get("inbound_receipts", []),
                    }
                ),
                "facility_version": self._hash(facility),
            }
            for key, value in versions.items():
                conn.execute(
                    "INSERT INTO warehouse_meta(warehouse_id,key,value) VALUES (%s,%s,%s) "
                    "ON CONFLICT (warehouse_id,key) DO UPDATE SET "
                    "value=EXCLUDED.value,updated_at=now()",
                    (wid, key, value),
                )
            conn.commit()
        return self.count_summary(wid)

    @staticmethod
    def _hash(value: Any) -> str:
        import hashlib
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:16]

    @staticmethod
    def _order_record(row: dict[str,Any]) -> dict[str,Any]:
        return {"warehouse_id":row["warehouse_id"],"order_id":row["order_id"],"item_id":row["item_id"],"required_qty":int(row["required_qty"]),"delivery_node":row["outbound_chute_id"],"outbound_chute_id":row["outbound_chute_id"],"logical_destination_id":row["outbound_chute_id"],"priority":row["priority"],"status":row["status"],"preferred_station_ids":list(row["preferred_station_ids"] or []),"created_at":str(row["created_at"])}

    @staticmethod
    def _inbound_record(row: dict[str,Any]) -> dict[str,Any]:
        return {
            **dict(row),
            "quantity": int(row["quantity"]),
            "target_rack_id": (
                str(row["target_rack_id"])
                if row.get("target_rack_id") is not None
                else None
            ),
            "target_rack_level": (
                int(row["target_rack_level"])
                if row.get("target_rack_level") is not None
                else None
            ),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def load_inbound_receipts(self, warehouse_id: str | None = None) -> list[dict[str,Any]]:
        wid=self._warehouse(warehouse_id)
        with self._connection() as conn: rows=conn.execute("SELECT * FROM inbound_receipts WHERE warehouse_id=%s ORDER BY inbound_id",(wid,)).fetchall()
        return [self._inbound_record(dict(r)) for r in rows]

    def get_inbound_receipt(self, warehouse_or_id:str, inbound_id:str|None=None) -> dict[str,Any]|None:
        wid=self._warehouse(warehouse_or_id if inbound_id is not None else None); target=inbound_id or warehouse_or_id
        with self._connection() as conn: row=conn.execute("SELECT * FROM inbound_receipts WHERE warehouse_id=%s AND inbound_id=%s",(wid,target)).fetchone()
        return self._inbound_record(dict(row)) if row else None

    def load_orders(self, warehouse_id:str|None=None) -> list[dict[str,Any]]:
        return self.find_orders(warehouse_id)

    def get_order(self, warehouse_or_id:str, order_id:str|None=None) -> dict[str,Any]|None:
        wid=self._warehouse(warehouse_or_id if order_id is not None else None); target=order_id or warehouse_or_id
        values=self.find_orders(wid,order_ids=[target]); return values[0] if values else None

    def find_orders(self, warehouse_id:str|None=None, *, order_ids:list[str]|None=None,item_ids:list[str]|None=None,statuses:list[str]|None=None) -> list[dict[str,Any]]:
        wid=self._warehouse(warehouse_id); clauses=["o.warehouse_id=%s"]; args:[Any]=[wid]
        if order_ids: clauses.append("o.order_id=ANY(%s)"); args.append(order_ids)
        if item_ids: clauses.append("l.item_id=ANY(%s)"); args.append(item_ids)
        if statuses: clauses.append("o.status=ANY(%s)"); args.append(statuses)
        sql="SELECT o.*,l.item_id,l.required_qty FROM orders o JOIN order_lines l ON l.warehouse_id=o.warehouse_id AND l.order_id=o.order_id WHERE "+" AND ".join(clauses)+" ORDER BY o.order_id,l.line_no"
        with self._connection() as conn: rows=conn.execute(sql,args).fetchall()
        return [self._order_record(dict(r)) for r in rows]

    def load_spring_tasks(self, simulation_run_id: int) -> list[dict[str, Any]]:
        """Read active task facts owned by the Spring BE for one simulation run.

        ``public.task`` is a Spring-managed table and is intentionally not part
        of the native LARO bootstrap schema.  It is queried only when a public
        Native Plan request supplies ``simulation_run_id``.
        """

        with self._connection() as conn:
            exists = conn.execute(
                "SELECT to_regclass('public.task') AS relation"
            ).fetchone()
            relation = exists.get("relation") if isinstance(exists, dict) else exists[0]
            if relation is None:
                raise PostgresInfrastructureError(
                    "Spring table public.task is unavailable for simulation_run_id planning."
                )
            rows = conn.execute(
                """
                SELECT
                    t.id AS task_id,
                    t.simulation_run_id,
                    t.warehouse_id,
                    t.robot_id,
                    t.task_type,
                    t.status,
                    t.item_id,
                    t.warehouse_item_id,
                    t.quantity,
                    t.start_node_id,
                    COALESCE(start_node.node_code, t.start_node_id::text) AS start_node,
                    t.end_node_id,
                    COALESCE(end_node.node_code, t.end_node_id::text) AS end_node,
                    t.release_at_seconds,
                    t.requested_at,
                    t.assigned_at,
                    t.started_at,
                    t.completed_at
                FROM public.task t
                LEFT JOIN public.warehouse_node start_node
                  ON start_node.warehouse_id = t.warehouse_id
                 AND start_node.node_id = t.start_node_id
                LEFT JOIN public.warehouse_node end_node
                  ON end_node.warehouse_id = t.warehouse_id
                 AND end_node.node_id = t.end_node_id
                WHERE t.simulation_run_id = %s
                ORDER BY t.id
                """,
                (int(simulation_run_id),),
            ).fetchall()
        return [
            {
                **dict(row),
                "task_id": str(row["task_id"]),
                "simulation_run_id": int(row["simulation_run_id"]),
                "warehouse_id": int(row["warehouse_id"]),
                "robot_id": (
                    str(row["robot_id"]) if row.get("robot_id") is not None else None
                ),
                "item_id": (
                    str(row["item_id"]) if row.get("item_id") is not None else None
                ),
                "quantity": int(row.get("quantity") or 1),
                "start_node": str(row["start_node"]),
                "end_node": str(row["end_node"]),
            }
            for row in rows
        ]

    def handling_units(
        self,
        warehouse_or_item: str | None = None,
        item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return canonical handling-unit records for live inventory reads.

        The JSON and embedded repositories expose rack aliases, access nodes,
        item labels, unit, and ``handling_unit_status``.  Live PostgreSQL must
        return the same contract; otherwise ``inventory_context`` fails before
        optimization even though the database rows themselves are valid.
        """

        if (
            item_id is None
            and warehouse_or_item
            and not str(warehouse_or_item).upper().startswith("WH-")
        ):
            wid = self._warehouse()
            target = str(warehouse_or_item)
        else:
            wid = self._warehouse(warehouse_or_item)
            target = item_id

        sql = (
            "SELECT h.*, r.access_node_ids "
            "FROM handling_units h "
            "JOIN racks r ON r.warehouse_id=h.warehouse_id "
            "AND r.rack_id=h.home_rack_id "
            "WHERE h.warehouse_id=%s AND h.quantity>0"
        )
        args: list[Any] = [wid]
        if target:
            sql += " AND h.item_id=%s"
            args.append(target)
        sql += " ORDER BY h.home_rack_id,h.home_rack_level,h.handling_unit_id"

        with self._connection() as conn:
            rows = conn.execute(sql, args).fetchall()

        values: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            status = str(row.get("status") or "stored")
            values.append(
                {
                    "warehouse_id": str(row["warehouse_id"]),
                    "handling_unit_id": str(row["handling_unit_id"]),
                    "stock_id": str(row["stock_id"]),
                    "item_id": str(row["item_id"]),
                    "item_name": str(row.get("item_name") or row["item_id"]),
                    "category": row.get("category"),
                    "quantity": int(row["quantity"]),
                    "capacity": int(row["capacity"]),
                    "unit": str(row.get("unit") or "EA"),
                    "rack_id": str(row["home_rack_id"]),
                    "rack_level": int(row["home_rack_level"]),
                    "home_rack_id": str(row["home_rack_id"]),
                    "home_rack_level": int(row["home_rack_level"]),
                    "status": status,
                    "handling_unit_status": status,
                    "version": int(row["version"]),
                    "access_node_ids": [
                        str(value) for value in (row.get("access_node_ids") or [])
                    ],
                }
            )
        return values

    def item_stocks(
        self,
        warehouse_or_item: str,
        item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return live stock candidates using the repository-wide shape."""

        wid = self._warehouse(warehouse_or_item if item_id is not None else None)
        target = item_id or warehouse_or_item
        return self.handling_units(wid, target)

    def list_warehouses(self) -> list[dict[str, Any]]:
        """Return the durable warehouse registry."""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT warehouse_id,label,active,created_at,updated_at "
                "FROM warehouses ORDER BY warehouse_id"
            ).fetchall()
        return [dict(value) for value in rows]

    def count_summary(self, warehouse_id:str|None=None) -> dict[str,int]:
        wid=self._warehouse(warehouse_id); names=("racks","handling_units","orders","inbound_receipts","outbound_stations","empty_tote_buffers")
        with self._connection() as conn: return {name:int(conn.execute(f"SELECT COUNT(*) AS n FROM {name} WHERE warehouse_id=%s",(wid,)).fetchone()["n"]) for name in names}

    def load_inventory_document(self, warehouse_id:str|None=None) -> dict[str,Any]:
        wid=self._warehouse(warehouse_id)
        with self._connection() as conn:
            racks=conn.execute("SELECT * FROM racks WHERE warehouse_id=%s ORDER BY rack_id",(wid,)).fetchall(); slots=conn.execute("SELECT * FROM rack_slots WHERE warehouse_id=%s ORDER BY rack_id,level",(wid,)).fetchall()
        units={}; used_quantity={}
        for value in self.handling_units(wid):
            key=(value["home_rack_id"],value["home_rack_level"])
            units.setdefault(key,value)
            used_quantity[key]=used_quantity.get(key,0)+int(value["quantity"])
        slots_by={(r["rack_id"],int(r["level"])):dict(r) for r in slots}; records=[]
        for rack in racks:
            levels=[]
            rack_levels=sorted(level for rack_id,level in slots_by if rack_id == rack["rack_id"])
            for level in rack_levels:
                slot=slots_by[(rack["rack_id"],level)]; unit=units.get((rack["rack_id"],level)); item=None
                if unit: item={"handling_unit_id":unit["handling_unit_id"],"item_id":unit["item_id"],"item_name":unit.get("item_name"),"category":unit.get("category"),"quantity":unit["quantity"],"capacity":unit["capacity"],"unit":unit["unit"],"handling_unit_status":unit["status"],"version":unit["version"]}
                levels.append({"level":level,"status":slot["status"],"capacity":int(slot["capacity"]),"used_quantity":used_quantity.get((rack["rack_id"],level),0),"item":item})
            records.append({"rack_id":rack["rack_id"],"access_node_ids":list(rack["access_node_ids"] or []),"levels":levels})
        return {"warehouse_id":wid,"version":self.versions(wid).get("inventory_version","live"),"racks":records}

    def load_facility_document(self, warehouse_id:str|None=None) -> dict[str,Any]:
        wid=self._warehouse(warehouse_id)
        with self._connection() as conn:
            ports=conn.execute("SELECT * FROM inbound_ports WHERE warehouse_id=%s ORDER BY port_id",(wid,)).fetchall(); handoffs=conn.execute("SELECT * FROM inbound_handoffs WHERE warehouse_id=%s ORDER BY handoff_id",(wid,)).fetchall(); chutes=conn.execute("SELECT * FROM outbound_chutes WHERE warehouse_id=%s ORDER BY chute_id",(wid,)).fetchall(); stations=conn.execute("SELECT * FROM outbound_stations WHERE warehouse_id=%s ORDER BY station_id",(wid,)).fetchall(); robots=conn.execute("SELECT * FROM station_robots WHERE warehouse_id=%s ORDER BY station_robot_id",(wid,)).fetchall(); buffers=conn.execute("SELECT * FROM empty_tote_buffers WHERE warehouse_id=%s ORDER BY buffer_id",(wid,)).fetchall()
        return {"warehouse_id":wid,"version":"live","inbound_ports":[dict(v) for v in ports],"inbound_handoffs":[{**dict(v),"access_node_ids":list(v["access_node_ids"])} for v in handoffs],"outbound_chutes":[dict(v) for v in chutes],"outbound_stations":[{**dict(v),"access_node_ids":list(v["access_node_ids"]),"served_chute_ids":list(v["served_chute_ids"])} for v in stations],"station_robots":[dict(v) for v in robots],"empty_tote_buffers":[{**dict(v),"access_node_ids":list(v["access_node_ids"])} for v in buffers]}

    def versions(self, warehouse_id:str|None=None) -> dict[str,str]:
        wid=self._warehouse(warehouse_id)
        with self._connection() as conn: rows=conn.execute("SELECT key,value FROM warehouse_meta WHERE warehouse_id=%s",(wid,)).fetchall()
        return {str(r["key"]):str(r["value"]) for r in rows}

    def create_batch_reservation(self, *, batch:dict[str,Any], allocations:Iterable[dict[str,Any]], expected_version:int, warehouse_id:str|None=None) -> str:
        wid=self._warehouse(warehouse_id or batch.get("warehouse_id")); rid=f"RES-{batch['batch_id']}"
        with self._connection() as conn:
            row=conn.execute("SELECT quantity,status,version FROM handling_units WHERE warehouse_id=%s AND handling_unit_id=%s FOR UPDATE",(wid,batch["handling_unit_id"])).fetchone()
            if row is None: raise PostgresInfrastructureError("handling unit does not exist")
            if int(row["version"])!=expected_version: raise PostgresInfrastructureError("handling unit version changed")
            if str(row["status"])!="stored" or int(row["quantity"])<int(batch["requested_quantity"]): raise PostgresInfrastructureError("handling unit is not reservable")
            conn.execute("INSERT INTO outbound_batches(warehouse_id,batch_id,simulation_id,item_id,handling_unit_id,station_id,mobile_robot_id,post_station_node,post_station_action,requested_quantity,quantity_before,quantity_after,return_required,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved')",(wid,batch["batch_id"],batch["simulation_id"],batch["item_id"],batch["handling_unit_id"],batch["station_id"],batch.get("mobile_robot_id"),batch["post_station_node"],batch["post_station_action"],batch["requested_quantity"],batch["quantity_before"],batch["quantity_after"],batch["return_required"]))
            for v in allocations: conn.execute("INSERT INTO outbound_batch_orders(warehouse_id,batch_id,order_id,chute_id,quantity) VALUES (%s,%s,%s,%s,%s)",(wid,batch["batch_id"],v["order_id"],v["chute_id"],v["quantity"]))
            conn.execute("INSERT INTO inventory_reservations(warehouse_id,reservation_id,batch_id,handling_unit_id,reserved_quantity,expected_handling_unit_version,status) VALUES (%s,%s,%s,%s,%s,%s,'active')",(wid,rid,batch["batch_id"],batch["handling_unit_id"],batch["requested_quantity"],expected_version))
            conn.execute("UPDATE handling_units SET status='reserved',version=version+1,updated_at=now() WHERE warehouse_id=%s AND handling_unit_id=%s",(wid,batch["handling_unit_id"])); conn.commit()
        return rid

    def commit_station_pick(self, *, batch_id:str, warehouse_id:str|None=None) -> dict[str,Any]:
        wid=self._warehouse(warehouse_id)
        with self._connection() as conn:
            batch=conn.execute("SELECT * FROM outbound_batches WHERE warehouse_id=%s AND batch_id=%s FOR UPDATE",(wid,batch_id)).fetchone()
            if batch is None: raise PostgresInfrastructureError("batch does not exist")
            hu_status="returning" if bool(batch["return_required"]) else "empty_in_transit"; b_status="returning" if bool(batch["return_required"]) else "empty_repositioning"
            conn.execute("UPDATE handling_units SET quantity=%s,status=%s,version=version+1,updated_at=now() WHERE warehouse_id=%s AND handling_unit_id=%s",(int(batch["quantity_after"]),hu_status,wid,batch["handling_unit_id"]))
            conn.execute("UPDATE inventory_reservations SET status='committed',updated_at=now() WHERE warehouse_id=%s AND batch_id=%s",(wid,batch_id)); conn.execute("UPDATE outbound_batches SET status=%s,updated_at=now() WHERE warehouse_id=%s AND batch_id=%s",(b_status,wid,batch_id))
            order_rows=conn.execute("SELECT DISTINCT order_id FROM outbound_batch_orders WHERE warehouse_id=%s AND batch_id=%s",(wid,batch_id)).fetchall()
            for r in order_rows:
                oid=str(r["order_id"]); required=int(conn.execute("SELECT COALESCE(SUM(required_qty),0) AS qty FROM order_lines WHERE warehouse_id=%s AND order_id=%s",(wid,oid)).fetchone()["qty"] or 0); committed=int(conn.execute("SELECT COALESCE(SUM(bo.quantity),0) AS qty FROM outbound_batch_orders bo JOIN outbound_batches b ON b.warehouse_id=bo.warehouse_id AND b.batch_id=bo.batch_id WHERE bo.warehouse_id=%s AND bo.order_id=%s AND b.status IN ('returning','empty_repositioning','completed')",(wid,oid)).fetchone()["qty"] or 0)
                if required and committed>=required: conn.execute("UPDATE orders SET status='fulfilled' WHERE warehouse_id=%s AND order_id=%s",(wid,oid))
            conn.commit()
        return {"warehouse_id":wid,"batch_id":batch_id,"handling_unit_status":hu_status,"batch_status":b_status}

    def complete_post_station_move(self, *, batch_id:str, robot_id:str, warehouse_id:str|None=None) -> dict[str,Any]:
        wid=self._warehouse(warehouse_id)
        with self._connection() as conn:
            batch=conn.execute("SELECT * FROM outbound_batches WHERE warehouse_id=%s AND batch_id=%s FOR UPDATE",(wid,batch_id)).fetchone()
            if batch is None: raise PostgresInfrastructureError("batch does not exist")
            if batch.get("mobile_robot_id") and str(batch["mobile_robot_id"])!=robot_id: raise PostgresInfrastructureError("post-station completion robot does not match the planned mobile robot")
            hu_status="stored" if bool(batch["return_required"]) else "empty_buffered"
            conn.execute("UPDATE handling_units SET status=%s,version=version+1,updated_at=now() WHERE warehouse_id=%s AND handling_unit_id=%s",(hu_status,wid,batch["handling_unit_id"])); conn.execute("UPDATE outbound_batches SET status='completed',updated_at=now() WHERE warehouse_id=%s AND batch_id=%s",(wid,batch_id)); conn.commit()
        return {"warehouse_id":wid,"batch_id":batch_id,"handling_unit_status":hu_status,"batch_status":"completed"}

    def roundtrip(self, probe_id:str, warehouse_id:str|None=None) -> dict[str,Any]:
        wid=self._warehouse(warehouse_id); payload={"probe":probe_id,"component":"postgres","warehouse_id":wid}
        with self._connection() as conn:
            conn.execute("INSERT INTO infrastructure_roundtrip(warehouse_id,probe_id,payload) VALUES (%s,%s,%s::jsonb) ON CONFLICT (warehouse_id,probe_id) DO UPDATE SET payload=EXCLUDED.payload,created_at=now()",(wid,probe_id,json.dumps(payload))); row=conn.execute("SELECT probe_id,payload FROM infrastructure_roundtrip WHERE warehouse_id=%s AND probe_id=%s",(wid,probe_id)).fetchone(); conn.execute("DELETE FROM infrastructure_roundtrip WHERE warehouse_id=%s AND probe_id=%s",(wid,probe_id)); conn.commit()
        return {"probe_id":row["probe_id"],"payload":dict(row["payload"])}

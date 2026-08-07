"""Read adapter over the existing Spring BE PostgreSQL schema.

No order or handling-unit master is created.  Business operations come from the
request, while inventory is derived from ``warehouse_items`` and planning-only
attributes come from ``laro_ext``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any

from app.core.config import Settings, get_settings
from app.infrastructure.manager import get_infrastructure_manager


class BeCenteredDataError(RuntimeError):
    pass


class BeCenteredPostgresAdapter:
    def __init__(self, settings: Settings | None = None, manager: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.manager = manager or get_infrastructure_manager()
        self._views_ready = False
        self._refresh_lock = RLock()

    @property
    def postgres(self):
        return self.manager.postgres

    def refresh_views(self, *, force: bool = False) -> bool:
        """Apply additive extensions and expose Spring tables as read views."""

        if self._views_ready and not force:
            return True
        with self._refresh_lock:
            if self._views_ready and not force:
                return True
            schema = (
                Path(__file__).resolve().parents[2]
                / "db"
                / "postgres"
                / "004_be_centered_extensions.sql"
            )
            self.postgres.apply_schema(schema)
            with self.postgres._connection() as conn:
                row = conn.execute(
                    "SELECT laro_ext.refresh_be_views() AS refreshed"
                ).fetchone()
                conn.commit()
            self._views_ready = bool(row and row["refreshed"])
            return self._views_ready

    def require_views(self) -> None:
        if not self.refresh_views():
            raise BeCenteredDataError(
                "Spring BE tables are not ready. Start BE once so Hibernate creates "
                "public.warehouse_layout, warehouse_node, warehouse_edge, "
                "warehouse_items, robot, and simulation_runs; then retry."
            )

    def resolve_simulation_run(self, simulation_run_id: int) -> dict[str, Any]:
        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT simulation_run_id, warehouse_id, warehouse_code,
                       run_status, run_version, simulation_speed,
                       charging_threshold, auto_replan, obstacle_enabled
                FROM laro_ext.be_simulation_run_v
                WHERE simulation_run_id = %s
                """,
                (int(simulation_run_id),),
            ).fetchone()
        if row is None:
            raise BeCenteredDataError(
                f"Simulation run {simulation_run_id} does not exist in public.simulation_runs."
            )
        return dict(row)

    def list_warehouses(self) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                """
                SELECT wp.warehouse_id, wp.warehouse_code, w.name AS label,
                       wp.active, wp.map_version, wp.inventory_version,
                       wp.facility_version
                FROM laro_ext.warehouse_profile wp
                JOIN public.warehouse_layout w ON w.id = wp.warehouse_id
                ORDER BY wp.warehouse_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def warehouse_name(self, warehouse_id: int) -> str:
        """Return the Spring warehouse label used by the front-end."""

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                "SELECT name FROM public.warehouse_layout WHERE id = %s",
                (int(warehouse_id),),
            ).fetchone()
        if row is None:
            raise BeCenteredDataError(f"Warehouse {warehouse_id} does not exist.")
        return str(row["name"])

    def product_catalog(
        self, product_codes: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return transport-relevant product facts, never route graph data."""

        self.require_views()
        clauses: list[str] = []
        args: list[Any] = []
        if product_codes:
            clauses.append("product_code = ANY(%s)")
            args.append(list(dict.fromkeys(str(value) for value in product_codes)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.postgres._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT product_id, product_code, product_name, category, unit,
                       units_per_box, temperature_zone, fragile
                FROM public.product
                {where}
                ORDER BY product_code
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def active_task_summary(self, simulation_run_id: int) -> dict[str, int]:
        """Summarize unfinished work that competes for robots and inventory."""

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)::integer AS unfinished_tasks,
                       COUNT(*) FILTER (WHERE status = 'PENDING')::integer AS pending_tasks,
                       COUNT(*) FILTER (WHERE status = 'ASSIGNED')::integer AS assigned_tasks,
                       COUNT(*) FILTER (WHERE status = 'IN_PROGRESS')::integer AS in_progress_tasks,
                       COUNT(*) FILTER (WHERE task_type = 'INBOUND')::integer AS inbound_tasks,
                       COUNT(*) FILTER (WHERE task_type = 'OUTBOUND')::integer AS outbound_tasks
                FROM public.task
                WHERE simulation_run_id = %s
                  AND status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
                """,
                (int(simulation_run_id),),
            ).fetchone()
        return {key: int(value or 0) for key, value in dict(row or {}).items()}

    def active_inventory_reservation_count(
        self, warehouse_id: int, simulation_run_id: int
    ) -> int:
        """Count boxes excluded by every active plan sharing the warehouse.

        A different simulation run can still own a live reservation for the
        same physical BOX, so the exclusion scope is the warehouse rather than
        only the requesting run. ``simulation_run_id`` stays in the signature
        to make the caller's context explicit.
        """

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT r.warehouse_item_id)::integer AS reserved_boxes
                FROM laro_ext.inventory_reservation r
                JOIN public.warehouse_items wi
                  ON wi.warehouse_item_id = r.warehouse_item_id
                WHERE wi.warehouse_id = %s
                  AND r.status = 'ACTIVE'
                """,
                (int(warehouse_id),),
            ).fetchone()
        return int(row["reserved_boxes"] or 0) if row else 0

    def route_nodes(self, warehouse_id: int) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM laro_ext.be_route_node_v
                WHERE warehouse_id = %s AND active = true
                ORDER BY node_id
                """,
                (warehouse_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def route_edges(self, warehouse_id: int) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM laro_ext.be_route_edge_v
                WHERE warehouse_id = %s
                  AND active = true
                  AND mobile_robot_traversable = true
                ORDER BY edge_id
                """,
                (warehouse_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def inventory_units(
        self,
        warehouse_id: int,
        item_code: str | None = None,
        *,
        include_active_reservations: bool = True,
        replanning_from_plan_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return BE inventory rows with active LARO reservations subtracted.

        During a rolling replan, inventory reserved by the plan being replaced
        remains owned by the same logical operations.  Excluding only that
        plan's reservations makes those rows visible to the replacement solve
        while reservations from every other plan remain hard exclusions.
        """

        self.require_views()
        clauses = ["v.warehouse_id = %s"]
        args: list[Any] = [warehouse_id]
        if item_code:
            clauses.append("(v.product_code = %s OR v.item_id::text = %s)")
            args.extend([str(item_code), str(item_code)])
        reservation_filter = "status = 'ACTIVE'"
        reservation_args: list[Any] = []
        if replanning_from_plan_id:
            reservation_filter += " AND plan_id <> %s"
            reservation_args.append(str(replanning_from_plan_id))
        reservation_join = (
            """
            LEFT JOIN (
                SELECT warehouse_item_id, SUM(reserved_quantity)::integer AS reserved_quantity
                FROM laro_ext.inventory_reservation
                WHERE {reservation_filter}
                GROUP BY warehouse_item_id
            ) r ON r.warehouse_item_id = v.warehouse_item_id
            """.format(reservation_filter=reservation_filter)
            if include_active_reservations
            else ""
        )
        reserved_expression = (
            "COALESCE(r.reserved_quantity, 0)"
            if include_active_reservations
            else "0"
        )
        sql = f"""
            SELECT v.*,
                   v.quantity AS physical_quantity,
                   {reserved_expression} AS reserved_quantity,
                   v.quantity::integer AS available_quantity
            FROM laro_ext.be_inventory_unit_v v
            {reservation_join}
            WHERE {' AND '.join(clauses)}
              AND v.quantity > 0
              AND {reserved_expression} = 0
            ORDER BY v.rack_code, v.rack_level, v.warehouse_item_id
        """
        with self.postgres._connection() as conn:
            rows = conn.execute(sql, [*reservation_args, *args]).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["quantity"] = int(value["available_quantity"])
        return values

    def empty_rack_slots(self, warehouse_id: int) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                """
                SELECT (sl.storage_location_id * 10 + levels.rack_level) AS rack_slot_id,
                       sl.warehouse_id,
                       sl.node_id AS rack_node_id,
                       levels.rack_level,
                       'EMPTY'::text AS status,
                       sl.storage_location_id,
                       1 AS capacity,
                       COALESCE(n.node_code, 'N' || n.node_id::text) AS rack_code
                FROM public.storage_location sl
                JOIN public.warehouse_node n ON n.node_id = sl.node_id
                CROSS JOIN generate_series(1, 3) AS levels(rack_level)
                LEFT JOIN public.warehouse_items wi
                       ON wi.storage_location_id = sl.storage_location_id
                      AND wi.rack_level = levels.rack_level
                      AND COALESCE(wi.quantity, 0) > 0
                WHERE sl.warehouse_id = %s
                  AND wi.warehouse_item_id IS NULL
                  AND upper(COALESCE(sl.status, 'AVAILABLE')) IN ('AVAILABLE', 'EMPTY')
                ORDER BY rack_code, levels.rack_level
                """,
                (warehouse_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rack_slot_counts(self, warehouse_id: int) -> dict[str, int]:
        """Return physical-rack and live three-level occupancy counts."""

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT sl.storage_location_id)::integer
                           AS storage_locations,
                       COUNT(*)::integer AS rack_slots,
                       COUNT(wi.warehouse_item_id)::integer AS occupied_rack_slots,
                       COUNT(*) FILTER (
                           WHERE wi.warehouse_item_id IS NULL
                             AND upper(COALESCE(sl.status, 'AVAILABLE'))
                                 IN ('AVAILABLE', 'EMPTY')
                       )::integer AS empty_rack_slots
                FROM public.storage_location sl
                CROSS JOIN generate_series(1, 3) AS levels(rack_level)
                LEFT JOIN public.warehouse_items wi
                       ON wi.storage_location_id = sl.storage_location_id
                      AND wi.rack_level = levels.rack_level
                      AND COALESCE(wi.quantity, 0) > 0
                WHERE sl.warehouse_id = %s
                """,
                (warehouse_id,),
            ).fetchone()
        return {
            "storage_locations": int(row["storage_locations"] or 0),
            "rack_slots": int(row["rack_slots"] or 0),
            "occupied_rack_slots": int(row["occupied_rack_slots"] or 0),
            "empty_rack_slots": int(row["empty_rack_slots"] or 0),
        }

    def storage_rack_codes(self, warehouse_id: int) -> list[str]:
        """Return every physical BE rack code, including completely full racks."""

        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT COALESCE(n.node_code, 'N' || n.node_id::text)
                                AS rack_code
                FROM public.storage_location sl
                JOIN public.warehouse_node n ON n.node_id = sl.node_id
                WHERE sl.warehouse_id = %s
                ORDER BY rack_code
                """,
                (warehouse_id,),
            ).fetchall()
        return [str(row["rack_code"]) for row in rows]

    def facilities(self, warehouse_id: int) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                """
                SELECT f.*, COALESCE(n.node_code, f.facility_code) AS node_code
                FROM laro_ext.facility f
                LEFT JOIN public.warehouse_node n ON n.node_id = f.node_id
                WHERE f.warehouse_id = %s AND f.active = true
                ORDER BY f.facility_type, f.facility_code
                """,
                (warehouse_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def charging_stations(self, warehouse_id: int) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            if not self._relation_exists(conn, "public.charging_station"):
                return []
            rows = conn.execute(
                """
                SELECT c.charging_station_id, c.warehouse_id, c.node_id,
                       COALESCE(n.node_code, 'N' || n.node_id::text) AS node_code,
                       c.name, c.status::text AS status, c.charging_power
                FROM public.charging_station c
                JOIN public.warehouse_node n ON n.node_id = c.node_id
                WHERE c.warehouse_id = %s
                ORDER BY c.charging_station_id
                """,
                (warehouse_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def robot_master(self, warehouse_id: int) -> list[dict[str, Any]]:
        self.require_views()
        with self.postgres._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM laro_ext.be_robot_master_v WHERE warehouse_id=%s ORDER BY robot_id",
                (warehouse_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def node_code_for_id(self, node_id: int) -> str | None:
        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                "SELECT node_code FROM laro_ext.be_route_node_v WHERE node_id=%s",
                (node_id,),
            ).fetchone()
        return str(row["node_code"]) if row else None

    def node_code_for_storage_location(self, storage_location_id: int) -> str | None:
        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(n.node_code, 'N' || n.node_id::text) AS node_code
                FROM public.storage_location sl
                JOIN public.warehouse_node n ON n.node_id = sl.node_id
                WHERE sl.storage_location_id=%s
                """,
                (storage_location_id,),
            ).fetchone()
        return str(row["node_code"]) if row else None

    def facility_by_code(self, warehouse_id: int, facility_code: str) -> dict[str, Any] | None:
        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT f.*, COALESCE(n.node_code, f.facility_code) AS node_code
                FROM laro_ext.facility f
                LEFT JOIN public.warehouse_node n ON n.node_id=f.node_id
                WHERE f.warehouse_id=%s AND f.facility_code=%s AND f.active=true
                """,
                (warehouse_id, facility_code),
            ).fetchone()
        return dict(row) if row else None

    def versions(self, warehouse_id: int) -> dict[str, str]:
        self.require_views()
        with self.postgres._connection() as conn:
            profile = conn.execute(
                "SELECT * FROM laro_ext.warehouse_profile WHERE warehouse_id=%s",
                (warehouse_id,),
            ).fetchone()
            inventory_rows = conn.execute(
                """
                SELECT warehouse_item_id, quantity, inbound_quantity, outbound_quantity
                FROM public.warehouse_items
                WHERE warehouse_id=%s
                ORDER BY warehouse_item_id
                """,
                (warehouse_id,),
            ).fetchall()
        profile = dict(profile or {})
        payload = json.dumps([dict(row) for row in inventory_rows], sort_keys=True, default=str)
        return {
            "map_version": str(profile.get("map_version", 1)),
            "inventory_version": hashlib.sha256(payload.encode()).hexdigest()[:16],
            "facility_version": str(profile.get("facility_version", 1)),
            "business_version": "request-structured-input",
        }


    def next_plan_version(self, simulation_run_id: int) -> int:
        """Return the next plan version for one Spring SimulationRun.

        Spring BE is the single plan-activation writer in this integration. The
        database unique constraint on ``(simulation_run_id, plan_version)`` is
        the final guard against accidental duplicate versions.
        """

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(plan_version), 0) + 1 AS next_version
                FROM laro_ext.simulation_plan
                WHERE simulation_run_id = %s
                """,
                (int(simulation_run_id),),
            ).fetchone()
        return int(row["next_version"] if row else 1)

    def load_plan(self, plan_id: str, simulation_run_id: int) -> dict[str, Any] | None:
        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT plan_json
                FROM laro_ext.simulation_plan
                WHERE plan_id = %s AND simulation_run_id = %s
                """,
                (plan_id, int(simulation_run_id)),
            ).fetchone()
        if not row:
            return None
        value = row["plan_json"]
        return value if isinstance(value, dict) else json.loads(value)

    def load_plan_request(
        self, plan_id: str, simulation_run_id: int
    ) -> dict[str, Any] | None:
        """Load the request-scoped operation overlay that produced a plan.

        BE-centered planning deliberately has no durable orders/handling-units
        master.  Rolling replans therefore retain the prior structured request
        so unfinished operation IDs can still be resolved to item, quantity,
        source inventory, and destination facts.
        """

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT request_json
                FROM laro_ext.simulation_plan
                WHERE plan_id = %s AND simulation_run_id = %s
                """,
                (plan_id, int(simulation_run_id)),
            ).fetchone()
        if not row:
            return None
        value = row["request_json"]
        return value if isinstance(value, dict) else json.loads(value)

    def load_request_response(
        self, request_id: str, simulation_run_id: int
    ) -> dict[str, Any] | None:
        """Return a prior response for an idempotent BE request retry."""

        self.require_views()
        with self.postgres._connection() as conn:
            row = conn.execute(
                """
                SELECT simulation_run_id, response_json
                FROM laro_ext.request_log
                WHERE request_id = %s
                """,
                (str(request_id),),
            ).fetchone()
        if row is None:
            return None
        if int(row["simulation_run_id"]) != int(simulation_run_id):
            raise BeCenteredDataError(
                f"request_id={request_id} is already bound to simulation_run_id="
                f"{row['simulation_run_id']}."
            )
        value = row.get("response_json")
        return dict(value) if isinstance(value, dict) and value else None

    def save_request_log(
        self,
        *,
        request_id: str,
        simulation_run_id: int,
        request_type: str,
        status: str,
        request_json: dict[str, Any],
        response_json: dict[str, Any] | None = None,
    ) -> None:
        """Persist one BE-to-LARO request without creating order/HU masters."""

        self.require_views()
        with self.postgres._connection() as conn:
            conn.execute(
                """
                INSERT INTO laro_ext.request_log(
                    request_id, simulation_run_id, request_type, status,
                    request_json, response_json
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                ON CONFLICT (request_id) DO UPDATE SET
                    status=EXCLUDED.status,
                    response_json=EXCLUDED.response_json
                """,
                (
                    request_id,
                    int(simulation_run_id),
                    str(request_type).upper(),
                    status,
                    json.dumps(request_json, ensure_ascii=False),
                    json.dumps(response_json or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def save_inventory_reservations(
        self,
        *,
        plan_id: str,
        simulation_run_id: int,
        batches: list[Any],
    ) -> int:
        """Reserve selected BE warehouse_items rows for one validated plan."""

        self.require_views()
        records: list[tuple[Any, ...]] = []
        for batch in batches:
            inventory_unit_id = str(
                getattr(batch, "handling_unit_id", "")
                or getattr(batch, "source_stock_id", "")
            )
            if not inventory_unit_id.startswith("WI-"):
                raise BeCenteredDataError(
                    "BE-centered inventory reservation requires a WI-{warehouse_item_id} "
                    f"inventory unit; received {inventory_unit_id!r}."
                )
            warehouse_item_id = int(inventory_unit_id.removeprefix("WI-"))
            batch_id = str(getattr(batch, "batch_id"))
            records.append(
                (
                    f"RES-{plan_id}-{batch_id}",
                    plan_id,
                    int(simulation_run_id),
                    warehouse_item_id,
                    batch_id,
                    int(getattr(batch, "requested_quantity")),
                    int(getattr(batch, "handling_unit_version", 0)),
                )
            )
        if not records:
            return 0
        with self.postgres._connection() as conn:
            for record in records:
                conn.execute(
                    """
                    INSERT INTO laro_ext.inventory_reservation(
                        reservation_id, plan_id, simulation_run_id,
                        warehouse_item_id, operation_id, reserved_quantity,
                        expected_item_version, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,'ACTIVE')
                    ON CONFLICT (reservation_id) DO NOTHING
                    """,
                    record,
                )
            conn.commit()
        return len(records)

    def save_plan(
        self,
        *,
        plan_id: str,
        simulation_run_id: int,
        warehouse_id: int,
        plan_version: int,
        status: str,
        request_json: dict[str, Any],
        plan_json: dict[str, Any],
        trace_json: dict[str, Any] | None,
        planning_mode: str | None,
        optimization_backend: str | None,
        map_version: str | None,
        runtime_version: str | None,
        makespan_ms: int | None,
        base_plan_id: str | None = None,
        supersedes_plan_id: str | None = None,
        plan_kind: str = "INITIAL",
    ) -> None:
        self.require_views()
        with self.postgres._connection() as conn:
            conn.execute(
                """
                INSERT INTO laro_ext.simulation_plan(
                    plan_id, simulation_run_id, warehouse_id, plan_version,
                    base_plan_id, supersedes_plan_id, plan_kind, status,
                    planning_mode, optimization_backend, map_version,
                    runtime_version, makespan_ms, request_json, plan_json, trace_json
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb
                )
                ON CONFLICT (plan_id) DO UPDATE SET
                    status=EXCLUDED.status,
                    plan_json=EXCLUDED.plan_json,
                    trace_json=EXCLUDED.trace_json
                """,
                (
                    plan_id,
                    simulation_run_id,
                    warehouse_id,
                    plan_version,
                    base_plan_id,
                    supersedes_plan_id,
                    plan_kind,
                    status,
                    planning_mode,
                    optimization_backend,
                    map_version,
                    runtime_version,
                    makespan_ms,
                    json.dumps(request_json, ensure_ascii=False),
                    json.dumps(plan_json, ensure_ascii=False),
                    json.dumps(trace_json or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    @staticmethod
    def _relation_exists(conn: Any, qualified_name: str) -> bool:
        row = conn.execute("SELECT to_regclass(%s) AS relation", (qualified_name,)).fetchone()
        return bool(row and row.get("relation"))

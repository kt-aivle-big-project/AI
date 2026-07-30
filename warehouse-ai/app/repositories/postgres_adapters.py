from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.repositories.postgres import PostgresRepository


PostgresSchemaProfile = Literal["legacy_ai", "backend_laro"]


class PlanningPostgresRepository(Protocol):
    """Common read contract consumed by the planning snapshot builder."""

    schema_profile: str

    def healthcheck(self) -> dict[str, Any]: ...

    def snapshot(
        self,
        warehouse_id: int,
        item_ids: list[str],
        simulation_id: str | None = None,
    ) -> dict[str, Any]: ...


class BackendLaroSchemaError(RuntimeError):
    """The selected backend profile is missing a required read-model field."""


class LegacyPostgresAdapter(PostgresRepository):
    """Compatibility name for the unchanged AI-owned PostgreSQL repository."""

    schema_profile = "legacy_ai"


class BackendLaroPostgresAdapter:
    """Read-only mapping from backend-owned tables to a planning snapshot."""

    schema_profile = "backend_laro"
    READ_ONLY_WARNING = "BACKEND_LARO_EXECUTION_PERSISTENCE_READ_ONLY"
    NODE_TYPE_MAP = {
        "ROUTE": "ROUTE",
        "ROUTE_CHARGE_JUNCTION": "INTERSECTION",
        "RACK_STORAGE": "STORAGE",
        "INBOUND": "INBOUND",
        "OUTBOUND": "OUTBOUND",
        "CHARGING_SLOT": "CHARGER",
    }

    def __init__(self, database_url: str):
        if not database_url:
            raise RuntimeError("DATABASE_URL이 설정되지 않았습니다.")
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            hide_parameters=True,
        )

    def healthcheck(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            value = connection.execute(text("SELECT 1")).scalar_one()
        return {"ok": value == 1, "schema_profile": self.schema_profile}

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _rows(result: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in result.mappings().all()]

    @staticmethod
    def _require_fields(
        row: Mapping[str, Any],
        required: set[str],
        *,
        source: str,
    ) -> None:
        missing = sorted(
            field
            for field in required
            if field not in row or row.get(field) is None
        )
        if missing:
            raise BackendLaroSchemaError(
                f"backend_laro {source} 필수 필드가 없습니다: {', '.join(missing)}"
            )

    def _read_required(
        self,
        statement: Any,
        params: dict[str, Any],
        *,
        source: str,
        required_columns: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        try:
            with self.engine.connect() as connection:
                return self._rows(connection.execute(statement, params))
        except SQLAlchemyError:
            columns = ", ".join(required_columns)
            raise BackendLaroSchemaError(
                f"backend_laro {source} 조회에 필요한 테이블/컬럼을 확인하세요: "
                f"{columns}"
            ) from None

    @classmethod
    def _map_inventory_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_fields(
            row,
            {
                "warehouse_item_id",
                "warehouse_id",
                "item_id",
                "node_id",
                "quantity",
            },
            source="warehouse_items/product",
        )
        quantity = int(row["quantity"])
        warehouse_item_id = str(row["warehouse_item_id"])
        return {
            "warehouse_item_id": warehouse_item_id,
            "warehouse_id": int(row["warehouse_id"]),
            "item_id": str(row["item_id"]),
            "lot_id": str(
                row.get("lot_id") or f"BACKEND-{warehouse_item_id}"
            ),
            "node_id": int(row["node_id"]),
            "quantity": quantity,
            "reserved_quantity": 0,
            "available_quantity": quantity,
            "expiry_date": row.get("expiry_date"),
            "expiration_at": None,
            "status": "AVAILABLE",
            "received_at": row.get("received_at"),
            "available_at": row.get("received_at"),
            "base_unit": "BOX",
            "version": 1,
        }

    @classmethod
    def _map_robot_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_fields(
            row,
            {"robot_id", "warehouse_id", "node_id", "battery", "status"},
            source="robot",
        )
        robot_id = str(row["robot_id"])
        return {
            "robot_id": robot_id,
            "robot_code": str(row.get("robot_code") or robot_id),
            "warehouse_id": int(row["warehouse_id"]),
            "node_id": int(row["node_id"]),
            "battery": float(row["battery"]),
            "status": str(row["status"]),
            "max_load": float(row.get("max_load") or 0),
            "current_load": float(row.get("current_load") or 0),
            "version": int(row.get("version") or 1),
        }

    @classmethod
    def _map_task_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_fields(
            row,
            {
                "work_id",
                "warehouse_id",
                "source_node",
                "target_node",
                "status",
                "operation_type",
            },
            source="task",
        )
        status_map = {
            "PENDING": "NEW",
            "ASSIGNED": "READY",
            "IN_PROGRESS": "EXECUTING",
        }
        backend_status = str(row["status"]).upper()
        mapped_status = status_map.get(backend_status)
        if mapped_status is None:
            raise BackendLaroSchemaError(
                "backend_laro task.status cannot be mapped: "
                f"{backend_status}"
            )

        work_id = str(row["work_id"])
        quantity = int(row.get("quantity") or 1)
        item_id = row.get("item_id")
        return {
            "work_id": work_id,
            "warehouse_id": int(row["warehouse_id"]),
            "task_code": str(row.get("task_code") or f"TASK-{work_id}"),
            "item_id": str(item_id) if item_id is not None else None,
            "quantity": quantity,
            "source_node": int(row["source_node"]),
            "target_node": int(row["target_node"]),
            "priority": int(row.get("priority") or 100),
            "status": mapped_status,
            "assigned_robot_id": (
                str(row["assigned_robot_id"])
                if row.get("assigned_robot_id") is not None
                else None
            ),
            "scheduled_start": row.get("scheduled_start"),
            "scheduled_end": row.get("scheduled_end"),
            "version": int(row.get("version") or 1),
            "operation_type": str(row["operation_type"]).upper(),
            "quantity_boxes": quantity,
            "required_at": row.get("required_at"),
            "allow_partial_fulfillment": False,
            "inventory_order_id": None,
        }

    @classmethod
    def _map_inventory_item_row(
        cls, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        cls._require_fields(
            row,
            {"item_id", "item_name"},
            source="product",
        )
        return {
            "item_id": str(row["item_id"]),
            "item_name": str(row["item_name"]),
            "base_unit": "BOX",
            "active": True,
        }

    @classmethod
    def _map_node_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_fields(
            row,
            {"node_id", "warehouse_id", "node_type"},
            source="warehouse_node",
        )
        backend_node_type = str(row["node_type"]).upper()
        node_type = cls.NODE_TYPE_MAP.get(backend_node_type)
        if node_type is None:
            raise BackendLaroSchemaError(
                "backend_laro warehouse_node.node_type을 변환할 수 없습니다: "
                f"{backend_node_type}"
            )
        charging_status = str(row.get("charging_status") or "").upper()
        is_charger = backend_node_type == "CHARGING_SLOT"
        active = not (
            is_charger
            and charging_status in {"UNAVAILABLE", "MAINTENANCE"}
        )
        return {
            "node_id": int(row["node_id"]),
            "warehouse_id": int(row["warehouse_id"]),
            "zone_id": str(row.get("zone_id") or "UNZONED"),
            "node_code": (
                str(row["node_code"])
                if row.get("node_code") is not None
                else None
            ),
            "backend_node_type": backend_node_type,
            "node_type": node_type,
            "x": (
                float(row["x"]) if row.get("x") is not None else None
            ),
            "y": (
                float(row["y"]) if row.get("y") is not None else None
            ),
            "active": active,
            "charger_capacity": 1 if is_charger else None,
            "charger_power_kw": (
                float(row["charging_power"])
                if row.get("charging_power") is not None
                else None
            ),
        }

    @classmethod
    def _map_edge_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        cls._require_fields(
            row,
            {
                "edge_id",
                "from_node",
                "to_node",
                "distance",
                "direction_type",
            },
            source="warehouse_edge",
        )
        from_node = int(row["from_node"])
        to_node = int(row["to_node"])
        direction_type = str(row["direction_type"]).upper()
        if direction_type == "BOTH":
            direction = "BOTH"
        elif direction_type == "A_TO_B":
            direction = "ONE_WAY"
        elif direction_type == "B_TO_A":
            from_node, to_node = to_node, from_node
            direction = "ONE_WAY"
        else:
            raise BackendLaroSchemaError(
                "backend_laro warehouse_edge.direction_type을 "
                f"변환할 수 없습니다: {direction_type}"
            )
        distance = float(row["distance"])
        return {
            "edge_id": str(row["edge_id"]),
            "from_node": from_node,
            "to_node": to_node,
            "distance": distance,
            "travel_seconds": distance,
            "direction": direction,
            "active": True,
            "width": None,
        }

    def fetch_inventory(
        self,
        warehouse_id: int,
        item_ids: list[str],
    ) -> list[dict[str, Any]]:
        item_filter = (
            "AND p.product_code::text = ANY(:item_ids)" if item_ids else ""
        )
        params: dict[str, Any] = {"warehouse_id": warehouse_id}
        if item_ids:
            params["item_ids"] = item_ids
        rows = self._read_required(
            text(
                f"""
                SELECT
                    wi.warehouse_item_id::text AS warehouse_item_id,
                    wi.warehouse_id,
                    p.product_code::text AS item_id,
                    CONCAT('BACKEND-', wi.warehouse_item_id) AS lot_id,
                    wi.node_id,
                    wi.quantity,
                    wi.received_at,
                    wi.expiry_date
                FROM warehouse_items wi
                JOIN product p
                  ON p.product_id = wi.item_id
                WHERE wi.warehouse_id = :warehouse_id
                  AND wi.quantity > 0
                  {item_filter}
                ORDER BY wi.received_at ASC NULLS LAST,
                         wi.warehouse_item_id
                """
            ),
            params,
            source="inventory",
            required_columns=(
                "warehouse_items.warehouse_item_id",
                "warehouse_items.warehouse_id",
                "warehouse_items.item_id",
                "warehouse_items.node_id",
                "warehouse_items.quantity",
                "warehouse_items.received_at",
                "warehouse_items.expiry_date",
                "product.product_id",
                "product.product_code",
            ),
        )
        return [self._map_inventory_row(row) for row in rows]

    def fetch_inventory_items(
        self, item_ids: list[str]
    ) -> list[dict[str, Any]]:
        item_filter = (
            "WHERE p.product_code::text = ANY(:item_ids)" if item_ids else ""
        )
        params: dict[str, Any] = {"item_ids": item_ids} if item_ids else {}
        rows = self._read_required(
            text(
                f"""
                SELECT
                    p.product_code::text AS item_id,
                    p.product_name::text AS item_name
                FROM product p
                {item_filter}
                ORDER BY p.product_code
                """
            ),
            params,
            source="product",
            required_columns=(
                "product.product_code",
                "product.product_name",
            ),
        )
        return [self._map_inventory_item_row(row) for row in rows]

    def fetch_inbound_orders(
        self, warehouse_id: int, item_ids: list[str]
    ) -> list[dict[str, Any]]:
        return []

    def fetch_outbound_orders(
        self, warehouse_id: int, item_ids: list[str]
    ) -> list[dict[str, Any]]:
        return []

    def fetch_storage_capacity(
        self, warehouse_id: int
    ) -> dict[str, Any] | None:
        rows = self._read_required(
            text(
                """
                SELECT
                    sl.storage_location_id::text AS storage_location_id,
                    sl.warehouse_id,
                    sl.node_id,
                    sl.max_quantity,
                    COALESCE(SUM(wi.quantity), 0) AS occupied_quantity,
                    GREATEST(
                        sl.max_quantity - COALESCE(SUM(wi.quantity), 0),
                        0
                    ) AS available_quantity,
                    sl.status
                FROM storage_location sl
                LEFT JOIN warehouse_items wi
                  ON wi.storage_location_id = sl.storage_location_id
                WHERE sl.warehouse_id = :warehouse_id
                GROUP BY
                    sl.storage_location_id,
                    sl.warehouse_id,
                    sl.node_id,
                    sl.max_quantity,
                    sl.status
                ORDER BY sl.storage_location_id
                """
            ),
            {"warehouse_id": warehouse_id},
            source="storage capacity",
            required_columns=(
                "storage_location.storage_location_id",
                "storage_location.warehouse_id",
                "storage_location.node_id",
                "storage_location.max_quantity",
                "storage_location.status",
                "warehouse_items.storage_location_id",
                "warehouse_items.quantity",
            ),
        )
        if not rows:
            return None

        locations: list[dict[str, Any]] = []
        for row in rows:
            self._require_fields(
                row,
                {
                    "storage_location_id",
                    "warehouse_id",
                    "node_id",
                    "max_quantity",
                    "occupied_quantity",
                    "available_quantity",
                    "status",
                },
                source="storage capacity",
            )
            locations.append(
                {
                    "storage_location_id": str(row["storage_location_id"]),
                    "warehouse_id": int(row["warehouse_id"]),
                    "node_id": int(row["node_id"]),
                    "max_quantity": float(row["max_quantity"]),
                    "occupied_quantity": float(row["occupied_quantity"]),
                    "available_quantity": float(row["available_quantity"]),
                    "status": str(row["status"]),
                }
            )
        return {
            "capacity_value": sum(
                location["max_quantity"] for location in locations
            ),
            "capacity_unit": "BOX",
            "capacity_type": "QUANTITY",
            "usable_capacity_value": sum(
                location["available_quantity"] for location in locations
            ),
            "locations": locations,
        }

    def fetch_robots(self, warehouse_id: int) -> list[dict[str, Any]]:
        rows = self._read_required(
            text(
                """
                SELECT
                    r.robot_id::text AS robot_id,
                    COALESCE(
                        rs.robot_code,
                        r.robot_id::text
                    ) AS robot_code,
                    r.warehouse_id,
                    r.node_id,
                    r.battery::double precision AS battery,
                    r.status,
                    0::double precision AS max_load,
                    0::double precision AS current_load,
                    1 AS version
                FROM robot r
                JOIN robot_specs rs
                  ON rs.id = r.robot_spec_id
                WHERE r.warehouse_id = :warehouse_id
                ORDER BY r.robot_id
                """
            ),
            {"warehouse_id": warehouse_id},
            source="robots",
            required_columns=(
                "robot.robot_id",
                "robot.warehouse_id",
                "robot.node_id",
                "robot.battery",
                "robot.status",
                "robot.robot_spec_id",
                "robot_specs.id",
                "robot_specs.robot_code",
            ),
        )
        return [self._map_robot_row(row) for row in rows]

    def fetch_map_nodes(
        self, warehouse_id: int
    ) -> list[dict[str, Any]]:
        rows = self._read_required(
            text(
                """
                SELECT
                    n.node_id,
                    n.warehouse_id,
                    n.zone_id,
                    n.node_code,
                    n.node_type,
                    n.x,
                    n.y,
                    cs.status AS charging_status,
                    cs.charging_power
                FROM warehouse_node n
                LEFT JOIN charging_station cs
                  ON cs.warehouse_id = n.warehouse_id
                 AND cs.node_id = n.node_id
                WHERE n.warehouse_id = :warehouse_id
                ORDER BY n.node_id
                """
            ),
            {"warehouse_id": warehouse_id},
            source="map nodes",
            required_columns=(
                "warehouse_node.node_id",
                "warehouse_node.warehouse_id",
                "warehouse_node.zone_id",
                "warehouse_node.node_code",
                "warehouse_node.node_type",
                "warehouse_node.x",
                "warehouse_node.y",
                "charging_station.warehouse_id",
                "charging_station.node_id",
                "charging_station.status",
                "charging_station.charging_power",
            ),
        )
        return [self._map_node_row(row) for row in rows]

    def fetch_map_edges(
        self, warehouse_id: int
    ) -> list[dict[str, Any]]:
        rows = self._read_required(
            text(
                """
                SELECT
                    e.edge_id,
                    e.from_node_id AS from_node,
                    e.to_node_id AS to_node,
                    e.distance,
                    e.direction_type
                FROM warehouse_edge e
                JOIN warehouse_node source
                  ON source.node_id = e.from_node_id
                JOIN warehouse_node target
                  ON target.node_id = e.to_node_id
                WHERE source.warehouse_id = :warehouse_id
                  AND target.warehouse_id = :warehouse_id
                ORDER BY e.edge_id
                """
            ),
            {"warehouse_id": warehouse_id},
            source="map edges",
            required_columns=(
                "warehouse_edge.edge_id",
                "warehouse_edge.from_node_id",
                "warehouse_edge.to_node_id",
                "warehouse_edge.distance",
                "warehouse_edge.direction_type",
                "warehouse_node.node_id",
                "warehouse_node.warehouse_id",
            ),
        )
        return [self._map_edge_row(row) for row in rows]

    def fetch_map(
        self, warehouse_id: int
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "nodes": self.fetch_map_nodes(warehouse_id),
            "edges": self.fetch_map_edges(warehouse_id),
        }

    def fetch_work_statuses(
        self,
        warehouse_id: int,
        simulation_run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_required(
            text(
                """
                SELECT
                    t.id::text AS work_id,
                    CASE t.status
                        WHEN 'PENDING' THEN 'NEW'
                        WHEN 'ASSIGNED' THEN 'READY'
                        WHEN 'IN_PROGRESS' THEN 'EXECUTING'
                    END AS status
                FROM task t
                WHERE t.warehouse_id = :warehouse_id
                  AND (
                      CAST(:simulation_run_id AS BIGINT) IS NULL
                      OR t.simulation_run_id =
                         CAST(:simulation_run_id AS BIGINT)
                  )
                  AND t.status IN (
                      'PENDING', 'ASSIGNED', 'IN_PROGRESS'
                  )
                ORDER BY t.requested_at, t.id
                """
            ),
            {
                "warehouse_id": warehouse_id,
                "simulation_run_id": simulation_run_id,
            },
            source="task statuses",
            required_columns=(
                "task.id",
                "task.warehouse_id",
                "task.status",
                "task.requested_at",
                "task.simulation_run_id",
            ),
        )
        return [dict(row) for row in rows]

    def fetch_open_works(
        self,
        warehouse_id: int,
        simulation_run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_required(
            text(
                """
                SELECT
                    t.id::text AS work_id,
                    t.warehouse_id,
                    ('TASK-' || t.id::text) AS task_code,
                    p.product_code::text AS item_id,
                    COALESCE(t.quantity, 1) AS quantity,
                    t.start_node_id AS source_node,
                    t.end_node_id AS target_node,
                    100 AS priority,
                    t.status,
                    t.robot_id::text AS assigned_robot_id,
                    t.requested_at AS scheduled_start,
                    NULL::timestamp AS scheduled_end,
                    1 AS version,
                    t.task_type AS operation_type,
                    t.requested_at AS required_at
                FROM task t
                LEFT JOIN warehouse_items wi
                  ON wi.warehouse_item_id = t.warehouse_item_id
                LEFT JOIN product p
                  ON p.product_id = COALESCE(t.item_id, wi.item_id)
                WHERE t.warehouse_id = :warehouse_id
                  AND (
                      CAST(:simulation_run_id AS BIGINT) IS NULL
                      OR t.simulation_run_id =
                         CAST(:simulation_run_id AS BIGINT)
                  )
                  AND t.status IN (
                      'PENDING', 'ASSIGNED', 'IN_PROGRESS'
                  )
                ORDER BY t.requested_at, t.id
                """
            ),
            {
                "warehouse_id": warehouse_id,
                "simulation_run_id": simulation_run_id,
            },
            source="tasks",
            required_columns=(
                "task.id",
                "task.warehouse_id",
                "task.item_id",
                "task.quantity",
                "task.start_node_id",
                "task.end_node_id",
                "task.status",
                "task.robot_id",
                "task.requested_at",
                "task.task_type",
                "task.warehouse_item_id",
                "task.simulation_run_id",
                "warehouse_items.warehouse_item_id",
                "warehouse_items.item_id",
                "product.product_id",
                "product.product_code",
            ),
        )
        return [self._map_task_row(row) for row in rows]

    def fetch_work_dependencies(
        self, warehouse_id: int
    ) -> list[dict[str, Any]]:
        return []

    def fetch_work_schedule_constraints(
        self, warehouse_id: int
    ) -> list[dict[str, Any]]:
        return []

    def snapshot(
        self,
        warehouse_id: int,
        item_ids: list[str],
        simulation_id: str | None = None,
    ) -> dict[str, Any]:
        simulation_run_id: int | None = None
        if simulation_id is not None:
            try:
                simulation_run_id = int(simulation_id)
            except (TypeError, ValueError):
                raise BackendLaroSchemaError(
                    "backend_laro simulation_id must be a numeric run id"
                ) from None

        works = self.fetch_open_works(
            warehouse_id,
            simulation_run_id,
        )
        requested_item_ids = {
            str(value) for value in item_ids if value
        }
        scoped_item_ids = (
            sorted(
                requested_item_ids
                | {
                    str(row.get("item_id"))
                    for row in works
                    if row.get("item_id")
                }
            )
            if requested_item_ids
            else []
        )
        snapshot = {
            "inventory": self.fetch_inventory(
                warehouse_id,
                scoped_item_ids,
            ),
            "inventory_items": self.fetch_inventory_items(
                scoped_item_ids
            ),
            "inbound_orders": [],
            "outbound_orders": [],
            "storage_capacity": self.fetch_storage_capacity(warehouse_id),
            "robots": self.fetch_robots(warehouse_id),
            "works": works,
            "work_statuses": self.fetch_work_statuses(
                warehouse_id,
                simulation_run_id,
            ),
            "work_dependencies": [],
            "work_schedule_constraints": [],
        }
        snapshot["warnings"] = [
            {
                "code": self.READ_ONLY_WARNING,
                "profile": self.schema_profile,
                "persistence": "READ_ONLY_NOT_CONFIGURED",
                "unmapped": [
                    "inbound_orders",
                    "outbound_orders",
                    "work_dependencies",
                    "work_schedule_constraints",
                    "ai_command_audit_writes",
                    "execution_dispatch_writes",
                ],
            }
        ]
        return snapshot

    # The backend profile is deliberately read-only. These lifecycle hooks
    # keep PLAN_ONLY Swagger requests functional without writing AI audit or
    # simulation rows into backend-owned task/event/simulation tables.
    def create_or_get_command_history(
        self, values: dict[str, Any]
    ) -> None:
        return None

    def finalize_command_audit(
        self,
        history_values: dict[str, Any],
        stage_rows: list[dict[str, Any]],
    ) -> None:
        return None

    def record_simulation(self, state: dict[str, Any]) -> None:
        return None

    def approve_execution_plan(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "BACKEND_LARO_EXECUTION_PERSISTENCE_NOT_CONFIGURED"
        )


def create_postgres_repository(
    database_url: str,
    profile: PostgresSchemaProfile = "legacy_ai",
) -> PlanningPostgresRepository:
    if profile == "legacy_ai":
        return LegacyPostgresAdapter(database_url)
    if profile == "backend_laro":
        return BackendLaroPostgresAdapter(database_url)
    raise ValueError(
        "POSTGRES_SCHEMA_PROFILE은 legacy_ai 또는 backend_laro여야 합니다."
    )

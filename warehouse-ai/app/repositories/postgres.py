import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.models import RobotEvent
from app.services.inventory_transition import calculate_inventory_transition
from app.state import PlanningState
from app.time_utils import as_utc_datetime


class CompletionValidationError(ValueError):
    """A deterministic, non-mutating rejection of a REAL completion event."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PostgresRepository:
    """PostgreSQL의 확정 재고·로봇·작업·실행 이력을 담당합니다."""

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
        return {"ok": value == 1}

    @staticmethod
    def _rows(result: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in result.mappings().all()]

    def fetch_inventory(
        self,
        warehouse_id: int,
        item_ids: list[str],
    ) -> list[dict[str, Any]]:
        item_filter = "AND item_id = ANY(:item_ids)" if item_ids else ""
        extended_statement = text(
            f"""
            SELECT
                warehouse_item_id, warehouse_id, item_id, lot_id, node_id,
                quantity, COALESCE(reserved_quantity, 0) AS reserved_quantity,
                quantity - COALESCE(reserved_quantity, 0) AS available_quantity,
                expiry_date, expiration_at, status, received_at, available_at,
                base_unit, version
            FROM warehouse_items
            WHERE warehouse_id = :warehouse_id
              AND status = 'AVAILABLE'
              AND (available_at IS NULL OR available_at <= CURRENT_TIMESTAMP)
              AND quantity - COALESCE(reserved_quantity, 0) > 0
              {item_filter}
            ORDER BY COALESCE(expiration_at, expiry_date::timestamptz) ASC NULLS LAST,
                     COALESCE(available_at, received_at) ASC NULLS LAST,
                     warehouse_item_id
            """
        )
        legacy_statement = text(
            f"""
            SELECT
                warehouse_item_id, warehouse_id, item_id, lot_id, node_id,
                quantity, COALESCE(reserved_quantity, 0) AS reserved_quantity,
                quantity - COALESCE(reserved_quantity, 0) AS available_quantity,
                expiry_date, version
            FROM warehouse_items
            WHERE warehouse_id = :warehouse_id
              AND quantity - COALESCE(reserved_quantity, 0) > 0
              {item_filter}
            ORDER BY expiry_date ASC NULLS LAST, warehouse_item_id
            """
        )
        params: dict[str, Any] = {"warehouse_id": warehouse_id}
        if item_ids:
            params["item_ids"] = item_ids
        with self.engine.connect() as connection:
            try:
                return self._rows(connection.execute(extended_statement, params))
            except SQLAlchemyError:
                connection.rollback()
                return self._rows(connection.execute(legacy_statement, params))

    def fetch_inventory_items(self, item_ids: list[str]) -> list[dict[str, Any]]:
        item_filter = "WHERE item_id = ANY(:item_ids)" if item_ids else ""
        params: dict[str, Any] = {"item_ids": item_ids} if item_ids else {}
        try:
            with self.engine.connect() as connection:
                return self._rows(
                    connection.execute(
                        text(
                            f"""
                            SELECT item_id, item_name, base_unit, active
                            FROM inventory_item
                            {item_filter}
                            ORDER BY item_id
                            """
                        ),
                        params,
                    )
                )
        except SQLAlchemyError:
            # Migration 010 is deliberately manual. Before it is applied the
            # existing planning features continue with legacy inventory rows.
            return []

    def fetch_inbound_orders(
        self, warehouse_id: int, item_ids: list[str]
    ) -> list[dict[str, Any]]:
        item_filter = "AND item_id = ANY(:item_ids)" if item_ids else ""
        params: dict[str, Any] = {"warehouse_id": warehouse_id}
        if item_ids:
            params["item_ids"] = item_ids
        try:
            with self.engine.connect() as connection:
                rows = self._rows(
                    connection.execute(
                        text(
                            f"""
                            SELECT inbound_id, warehouse_id, item_id,
                                   quantity_boxes, expected_arrival_at,
                                   expected_available_at, actual_arrival_at,
                                   actual_available_at, status, storage_node_id,
                                   lot_id, warehouse_item_id,
                                   (warehouse_item_id IS NOT NULL) AS lot_reflected
                            FROM inbound_order_line
                            WHERE warehouse_id = :warehouse_id
                              AND status IN (
                                  'SCHEDULED', 'ARRIVED', 'UNLOADING',
                                  'INSPECTING', 'AVAILABLE'
                              )
                              {item_filter}
                            ORDER BY COALESCE(actual_available_at, expected_available_at),
                                     inbound_id
                            """
                        ),
                        params,
                    )
                )
        except SQLAlchemyError:
            return []
        for row in rows:
            for field_name in (
                "expected_arrival_at",
                "expected_available_at",
                "actual_arrival_at",
                "actual_available_at",
            ):
                if row.get(field_name) is not None:
                    row[field_name] = as_utc_datetime(
                        row[field_name], field_name=field_name
                    )
        return rows

    def fetch_outbound_orders(
        self, warehouse_id: int, item_ids: list[str]
    ) -> list[dict[str, Any]]:
        item_filter = "AND item_id = ANY(:item_ids)" if item_ids else ""
        params: dict[str, Any] = {"warehouse_id": warehouse_id}
        if item_ids:
            params["item_ids"] = item_ids
        try:
            with self.engine.connect() as connection:
                rows = self._rows(
                    connection.execute(
                        text(
                            f"""
                            SELECT outbound_id, warehouse_id, item_id,
                                   requested_quantity_boxes, required_by,
                                   priority, allow_partial_fulfillment,
                                   status, work_id
                            FROM outbound_order_line
                            WHERE warehouse_id = :warehouse_id
                              AND status IN ('OPEN', 'APPROVED', 'PLANNED')
                              {item_filter}
                            ORDER BY required_by ASC NULLS LAST,
                                     priority DESC, outbound_id
                            """
                        ),
                        params,
                    )
                )
        except SQLAlchemyError:
            return []
        for row in rows:
            if row.get("required_by") is not None:
                row["required_by"] = as_utc_datetime(
                    row["required_by"], field_name="required_by"
                )
        return rows

    def fetch_storage_capacity(self, warehouse_id: int) -> dict[str, Any] | None:
        """Read an optional existing storage_location capacity contract."""

        try:
            with self.engine.connect() as connection:
                columns = {
                    str(row["column_name"])
                    for row in connection.execute(
                        text(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = current_schema()
                              AND table_name = 'storage_location'
                            """
                        )
                    ).mappings()
                }
                required = {
                    "warehouse_id",
                    "capacity_value",
                    "capacity_unit",
                    "capacity_type",
                    "usable_capacity_value",
                }
                if not required.issubset(columns):
                    return None
                row = connection.execute(
                    text(
                        """
                        SELECT SUM(capacity_value) AS capacity_value,
                               MIN(capacity_unit) AS capacity_unit,
                               MIN(capacity_type) AS capacity_type,
                               SUM(usable_capacity_value) AS usable_capacity_value
                        FROM storage_location
                        WHERE warehouse_id = :warehouse_id
                        """
                    ),
                    {"warehouse_id": warehouse_id},
                ).mappings().one()
                return dict(row) if row.get("capacity_value") is not None else None
        except SQLAlchemyError:
            return None

    def fetch_robots(self, warehouse_id: int) -> list[dict[str, Any]]:
        statement = text(
            """
            SELECT
                robot_id, robot_code, warehouse_id, node_id, battery, status,
                max_load, current_load, version
            FROM robot
            WHERE warehouse_id = :warehouse_id
            """
        )
        with self.engine.connect() as connection:
            return self._rows(connection.execute(statement, {"warehouse_id": warehouse_id}))

    def fetch_work_statuses(self, warehouse_id: int) -> list[dict[str, Any]]:
        statement = text(
            """
            SELECT work_id, status
            FROM works
            WHERE warehouse_id = :warehouse_id
            ORDER BY work_id
            """
        )
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(statement, {"warehouse_id": warehouse_id})
            )

    def fetch_open_works(self, warehouse_id: int) -> list[dict[str, Any]]:
        extended_statement = text(
            """
            SELECT
                work_id, warehouse_id, task_code, item_id, quantity,
                source_node, target_node, priority, status, assigned_robot_id,
                scheduled_start, scheduled_end, version, operation_type,
                quantity_boxes, required_at, allow_partial_fulfillment,
                inventory_order_id
            FROM works
            WHERE warehouse_id = :warehouse_id
              AND status IN (
                  'NEW', 'PLANNED', 'SCHEDULED', 'WAITING_FOR_PREDECESSOR',
                  'READY', 'DISPATCHED', 'EXECUTING', 'BLOCKED', 'DELAYED'
              )
            ORDER BY priority ASC, scheduled_start ASC NULLS LAST, work_id
            """
        )
        legacy_statement = text(
            """
            SELECT
                work_id, warehouse_id, task_code, item_id, quantity,
                source_node, target_node, priority, status, assigned_robot_id,
                scheduled_start, scheduled_end, version
            FROM works
            WHERE warehouse_id = :warehouse_id
              AND status IN (
                  'NEW', 'PLANNED', 'SCHEDULED', 'WAITING_FOR_PREDECESSOR',
                  'READY', 'DISPATCHED', 'EXECUTING', 'BLOCKED', 'DELAYED'
              )
            ORDER BY priority ASC, scheduled_start ASC NULLS LAST, work_id
            """
        )
        with self.engine.connect() as connection:
            try:
                rows = self._rows(
                    connection.execute(
                        extended_statement, {"warehouse_id": warehouse_id}
                    )
                )
            except SQLAlchemyError:
                connection.rollback()
                rows = self._rows(
                    connection.execute(
                        legacy_statement, {"warehouse_id": warehouse_id}
                    )
                )
        for row in rows:
            for field_name in ("scheduled_start", "scheduled_end"):
                value = row.get(field_name)
                if value is not None:
                    row[field_name] = as_utc_datetime(value, field_name=field_name)
        return rows

    def snapshot(self, warehouse_id: int, item_ids: list[str]) -> dict[str, Any]:
        # Open works are part of the planning scope even when a new command
        # names only one item. Load inventory/master/order rows for both the
        # command items and every currently open work item so inventory
        # precheck never treats a valid existing work as an unknown item merely
        # because the first query was narrowly scoped.
        works = self.fetch_open_works(warehouse_id)
        requested_item_ids = {str(value) for value in item_ids if value}
        # An empty item filter means "all registered items".  Previously the
        # empty filter was replaced with only the item ids of open works, so a
        # warehouse-wide inventory query silently returned A/F-like work items
        # and hid other registered products.  Keep the open-work union only for
        # genuinely item-scoped planning requests.
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
        return {
            "inventory": self.fetch_inventory(warehouse_id, scoped_item_ids),
            "inventory_items": self.fetch_inventory_items(scoped_item_ids),
            "inbound_orders": self.fetch_inbound_orders(
                warehouse_id, scoped_item_ids
            ),
            "outbound_orders": self.fetch_outbound_orders(
                warehouse_id, scoped_item_ids
            ),
            "storage_capacity": self.fetch_storage_capacity(warehouse_id),
            "robots": self.fetch_robots(warehouse_id),
            "works": works,
            "work_statuses": (
                self.fetch_work_statuses(warehouse_id)
                if hasattr(self, "fetch_work_statuses")
                else [
                    {"work_id": row.get("work_id"), "status": row.get("status")}
                    for row in works
                    if row.get("work_id")
                ]
            ),
            "work_dependencies": self.fetch_work_dependencies(warehouse_id),
            "work_schedule_constraints": self.fetch_work_schedule_constraints(
                warehouse_id
            ),
        }

    def fetch_work_dependencies(self, warehouse_id: int) -> list[dict[str, Any]]:
        statement = text(
            """
            SELECT d.predecessor_work_id, d.successor_work_id,
                   d.dependency_type, d.lag_seconds, d.source_command_id,
                   d.plan_version
            FROM work_dependencies d
            JOIN works successor ON successor.work_id = d.successor_work_id
            WHERE successor.warehouse_id = :warehouse_id
            ORDER BY d.predecessor_work_id, d.successor_work_id
            """
        )
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(statement, {"warehouse_id": warehouse_id})
            )

    def fetch_work_schedule_constraints(
        self, warehouse_id: int
    ) -> list[dict[str, Any]]:
        statement = text(
            """
            SELECT c.work_id, c.earliest_start, c.latest_finish,
                   c.time_constraint_type, c.fixed_robot_id,
                   c.same_robot_group, c.sequence_group, c.sequence_order,
                   c.source_command_id, c.plan_version
            FROM work_schedule_constraints c
            JOIN works w ON w.work_id = c.work_id
            WHERE w.warehouse_id = :warehouse_id
            ORDER BY c.work_id
            """
        )
        with self.engine.connect() as connection:
            rows = self._rows(
                connection.execute(statement, {"warehouse_id": warehouse_id})
            )
        for row in rows:
            for field_name in ("earliest_start", "latest_finish"):
                if row.get(field_name) is not None:
                    row[field_name] = as_utc_datetime(
                        row[field_name], field_name=field_name
                    )
        return rows

    def create_or_get_command_history(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        statement = text(
            """
            INSERT INTO command_history (
                command_id, warehouse_id, requested_execution_mode, source,
                original_text, actor_id, status, simulation_id,
                parent_command_id, received_at
            ) VALUES (
                :command_id, :warehouse_id, :requested_execution_mode, :source,
                :original_text, :actor_id, :status, :simulation_id,
                :parent_command_id, :received_at
            )
            ON CONFLICT (command_id) DO UPDATE
            SET updated_at = now()
            RETURNING *
            """
        )
        with self.engine.begin() as connection:
            return dict(connection.execute(statement, values).mappings().one())

    @staticmethod
    def _update_command_history(
        connection: Any,
        values: dict[str, Any],
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE command_history
                SET command_type = :command_type,
                    resolved_execution_mode = :resolved_execution_mode,
                    status = :status,
                    simulation_id = :simulation_id,
                    plan_version = :plan_version,
                    completed_at = :completed_at,
                    result_summary = CAST(:result_summary AS jsonb),
                    error_summary = CAST(:error_summary AS jsonb),
                    updated_at = now()
                WHERE command_id = :command_id
                """
            ),
            values,
        )

    def update_command_history(self, values: dict[str, Any]) -> None:
        params = dict(values)
        params["result_summary"] = json.dumps(
            params.get("result_summary"),
            ensure_ascii=False,
            default=str,
        )
        params["error_summary"] = (
            json.dumps(
                params.get("error_summary"),
                ensure_ascii=False,
                default=str,
            )
            if params.get("error_summary") is not None
            else None
        )
        with self.engine.begin() as connection:
            self._update_command_history(connection, params)

    @staticmethod
    def _insert_stage_logs(
        connection: Any,
        command_id: str,
        stages: list[dict[str, Any]],
    ) -> None:
        if not stages:
            return
        params = [
            {
                "command_id": command_id,
                "sequence": int(stage["sequence"]),
                "node_name": stage["node_name"],
                "attempt": int(stage.get("attempt") or 1),
                "status": stage["status"],
                "message": stage.get("message"),
                "details": json.dumps(
                    stage.get("details"),
                    ensure_ascii=False,
                    default=str,
                ),
                "created_at": stage.get("created_at") or datetime.now(UTC),
            }
            for stage in stages
        ]
        connection.execute(
            text(
                """
                INSERT INTO planning_stage_log (
                    command_id, sequence, node_name, attempt, status,
                    message, details, created_at
                ) VALUES (
                    :command_id, :sequence, :node_name, :attempt, :status,
                    :message, CAST(:details AS jsonb), :created_at
                )
                ON CONFLICT (command_id, sequence, attempt) DO NOTHING
                """
            ),
            params,
        )

    def persist_stage_logs(
        self,
        command_id: str,
        stages: list[dict[str, Any]],
    ) -> None:
        with self.engine.begin() as connection:
            self._insert_stage_logs(connection, command_id, stages)

    def finalize_command_audit(
        self,
        history: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> None:
        values = dict(history)
        values["result_summary"] = json.dumps(
            values.get("result_summary"),
            ensure_ascii=False,
            default=str,
        )
        values["error_summary"] = (
            json.dumps(
                values.get("error_summary"),
                ensure_ascii=False,
                default=str,
            )
            if values.get("error_summary") is not None
            else None
        )
        with self.engine.begin() as connection:
            self._update_command_history(connection, values)
            self._insert_stage_logs(connection, history["command_id"], stages)

    def list_command_history(
        self,
        *,
        warehouse_id: int | None = None,
        actor_id: str | None = None,
        status: str | None = None,
        requested_execution_mode: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        filters = {
            "warehouse_id": warehouse_id,
            "actor_id": actor_id,
            "status": status,
            "requested_execution_mode": requested_execution_mode,
        }
        for name, value in filters.items():
            if value is not None:
                conditions.append(f"{name} = :{name}")
                params[name] = value
        if date_from is not None:
            conditions.append("received_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            conditions.append("received_at <= :date_to")
            params["date_to"] = date_to
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        statement = text(
            f"""
            SELECT *
            FROM command_history
            {where}
            ORDER BY received_at DESC
            LIMIT :limit OFFSET :offset
            """
        )
        with self.engine.connect() as connection:
            return self._rows(connection.execute(statement, params))

    def get_command_history(self, command_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM command_history WHERE command_id = :command_id"),
                {"command_id": command_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def update_command_parent(
        self,
        command_id: str,
        parent_command_id: str | None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE command_history
                    SET parent_command_id = :parent_command_id, updated_at = now()
                    WHERE command_id = :command_id
                    """
                ),
                {
                    "command_id": command_id,
                    "parent_command_id": parent_command_id,
                },
            )

    def create_clarification_request(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        params = dict(values)
        for name in ("missing_fields", "ambiguous_fields", "options"):
            params[name] = json.dumps(
                params.get(name) or [], ensure_ascii=False, default=str
            )
        statement = text(
            """
            INSERT INTO clarification_request (
                clarification_id, conversation_id, command_id, warehouse_id,
                status, reason_code, question, missing_fields,
                ambiguous_fields, options, original_text, created_at, expires_at
            ) VALUES (
                :clarification_id, :conversation_id, :command_id, :warehouse_id,
                :status, :reason_code, :question, CAST(:missing_fields AS jsonb),
                CAST(:ambiguous_fields AS jsonb), CAST(:options AS jsonb),
                :original_text, :created_at, :expires_at
            )
            ON CONFLICT (clarification_id) DO NOTHING
            """
        )
        with self.engine.begin() as connection:
            connection.execute(statement, params)
            row = connection.execute(
                text(
                    "SELECT * FROM clarification_request "
                    "WHERE clarification_id = :clarification_id"
                ),
                {"clarification_id": params["clarification_id"]},
            ).mappings().one()
        return dict(row)

    def get_clarification_request(
        self,
        clarification_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM clarification_request "
                    "WHERE clarification_id = :clarification_id"
                ),
                {"clarification_id": clarification_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def resolve_clarification_request(
        self,
        clarification_id: str,
        *,
        response: dict[str, Any],
        resolved_command_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            current = connection.execute(
                text(
                    "SELECT * FROM clarification_request "
                    "WHERE clarification_id = :clarification_id FOR UPDATE"
                ),
                {"clarification_id": clarification_id},
            ).mappings().one_or_none()
            if current is None:
                return None
            if current["status"] == "RESOLVED":
                return dict(current)
            row = connection.execute(
                text(
                    """
                    UPDATE clarification_request
                    SET status = 'RESOLVED', response = CAST(:response AS jsonb),
                        resolved_command_id = :resolved_command_id,
                        resolved_at = now()
                    WHERE clarification_id = :clarification_id
                    RETURNING *
                    """
                ),
                {
                    "clarification_id": clarification_id,
                    "response": json.dumps(response, ensure_ascii=False, default=str),
                    "resolved_command_id": resolved_command_id,
                },
            ).mappings().one()
        return dict(row)

    def create_or_get_conversation(
        self,
        conversation_id: str,
        warehouse_id: int,
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO conversation_session (
                        conversation_id, warehouse_id, status
                    ) VALUES (:conversation_id, :warehouse_id, 'ACTIVE')
                    ON CONFLICT (conversation_id) DO NOTHING
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "warehouse_id": warehouse_id,
                },
            )
            row = connection.execute(
                text(
                    "SELECT * FROM conversation_session "
                    "WHERE conversation_id = :conversation_id FOR UPDATE"
                ),
                {"conversation_id": conversation_id},
            ).mappings().one()
            if int(row["warehouse_id"]) != int(warehouse_id):
                raise ValueError(
                    "conversation_id가 다른 warehouse_id에 속해 있습니다."
                )
        return dict(row)

    def get_conversation(
        self,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM conversation_session "
                    "WHERE conversation_id = :conversation_id"
                ),
                {"conversation_id": conversation_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def get_conversation_command_link(
        self,
        conversation_id: str,
        command_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM conversation_command_link
                    WHERE conversation_id = :conversation_id
                      AND command_id = :command_id
                    """
                ),
                {"conversation_id": conversation_id, "command_id": command_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def link_conversation_command(
        self,
        *,
        conversation_id: str,
        command_id: str,
        parent_command_id: str | None,
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM conversation_command_link "
                    "WHERE command_id = :command_id"
                ),
                {"command_id": command_id},
            ).mappings().one_or_none()
            if existing is not None:
                if existing["conversation_id"] != conversation_id:
                    raise ValueError("command_id가 다른 conversation에 연결되어 있습니다.")
                return dict(existing)
            connection.execute(
                text(
                    "SELECT conversation_id FROM conversation_session "
                    "WHERE conversation_id = :conversation_id FOR UPDATE"
                ),
                {"conversation_id": conversation_id},
            ).one()
            sequence = connection.execute(
                text(
                    """
                    SELECT COALESCE(MAX(sequence_number), 0) + 1
                    FROM conversation_command_link
                    WHERE conversation_id = :conversation_id
                    """
                ),
                {"conversation_id": conversation_id},
            ).scalar_one()
            row = connection.execute(
                text(
                    """
                    INSERT INTO conversation_command_link (
                        conversation_id, command_id, parent_command_id,
                        sequence_number
                    ) VALUES (
                        :conversation_id, :command_id, :parent_command_id,
                        :sequence_number
                    )
                    RETURNING *
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "command_id": command_id,
                    "parent_command_id": parent_command_id,
                    "sequence_number": int(sequence),
                },
            ).mappings().one()
        return dict(row)

    def update_conversation_session(
        self,
        conversation_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        params = {
            "conversation_id": conversation_id,
            "active_command_id": values.get("active_command_id"),
            "active_plan_version": values.get("active_plan_version"),
            "active_simulation_id": values.get("active_simulation_id"),
            "active_clarification_id": values.get("active_clarification_id"),
            "resolved_constraints": json.dumps(
                values.get("resolved_constraints") or {},
                ensure_ascii=False,
                default=str,
            ),
            "summary": json.dumps(
                values.get("summary") or {}, ensure_ascii=False, default=str
            ),
        }
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    UPDATE conversation_session
                    SET active_command_id = :active_command_id,
                        active_plan_version = :active_plan_version,
                        active_simulation_id = :active_simulation_id,
                        active_clarification_id = :active_clarification_id,
                        resolved_constraints = CAST(:resolved_constraints AS jsonb),
                        summary = CAST(:summary AS jsonb),
                        updated_at = now()
                    WHERE conversation_id = :conversation_id
                    RETURNING *
                    """
                ),
                params,
            ).mappings().one()
        return dict(row)

    def list_conversation_commands(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        """
                        SELECT ccl.sequence_number, ccl.parent_command_id,
                               ch.*
                        FROM conversation_command_link ccl
                        JOIN command_history ch ON ch.command_id = ccl.command_id
                        WHERE ccl.conversation_id = :conversation_id
                        ORDER BY ccl.sequence_number ASC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    {
                        "conversation_id": conversation_id,
                        "limit": limit,
                        "offset": offset,
                    },
                )
            )

    def get_latest_command_plan_evidence(
        self, command_id: str
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT command_id, plan_version, output_payload, created_at
                    FROM simulation_run
                    WHERE command_id = :command_id
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 1
                    """
                ),
                {"command_id": command_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def get_plan_evidence_by_version(
        self,
        *,
        warehouse_id: int,
        conversation_id: str,
        plan_version: str,
    ) -> dict[str, Any] | None:
        """Return a plan only when it belongs to this warehouse/conversation."""

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT sr.command_id, sr.plan_version, sr.output_payload,
                           sr.created_at
                    FROM simulation_run sr
                    JOIN conversation_command_link ccl
                      ON ccl.command_id = sr.command_id
                    WHERE sr.warehouse_id = :warehouse_id
                      AND ccl.conversation_id = :conversation_id
                      AND sr.plan_version = :plan_version
                    ORDER BY sr.created_at DESC, sr.run_id DESC
                    LIMIT 1
                    """
                ),
                {
                    "warehouse_id": warehouse_id,
                    "conversation_id": conversation_id,
                    "plan_version": plan_version,
                },
            ).mappings().one_or_none()
        return dict(row) if row else None

    def list_planning_stage_logs(self, command_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        """
                        SELECT stage_log_id, command_id, sequence, node_name,
                               attempt, status, message, details, created_at
                        FROM planning_stage_log
                        WHERE command_id = :command_id
                        ORDER BY sequence ASC, attempt ASC
                        """
                    ),
                    {"command_id": command_id},
                )
            )

    def list_simulation_sessions(
        self,
        *,
        warehouse_id: int | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if warehouse_id is not None:
            conditions.append("warehouse_id = :warehouse_id")
            params["warehouse_id"] = warehouse_id
        if status is not None:
            conditions.append("status = :status")
            params["status"] = status
        if date_from is not None:
            conditions.append("updated_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            conditions.append("updated_at <= :date_to")
            params["date_to"] = date_to
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        f"""
                        SELECT simulation_id, warehouse_id, status, generation,
                               checkpoint, created_by_command_id, last_command_id,
                               created_at, updated_at, reset_at, reset_by, reset_reason
                        FROM simulation_session
                        {where}
                        ORDER BY updated_at DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )

    def get_simulation_session(
        self,
        simulation_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM simulation_session
                    WHERE simulation_id = :simulation_id
                    """
                ),
                {"simulation_id": simulation_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def list_resettable_simulation_sessions(
        self,
        warehouse_id: int,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        """
                        SELECT * FROM simulation_session
                        WHERE warehouse_id = :warehouse_id
                          AND status <> 'RESET'
                        ORDER BY updated_at ASC, simulation_id ASC
                        """
                    ),
                    {"warehouse_id": warehouse_id},
                )
            )

    def mark_simulation_reset_pending(
        self,
        simulation_id: str,
        command_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    UPDATE simulation_session
                    SET status = 'RESET_PENDING',
                        last_command_id = :command_id,
                        updated_at = now()
                    WHERE simulation_id = :simulation_id
                      AND status <> 'RESET'
                    RETURNING *
                    """
                ),
                {"simulation_id": simulation_id, "command_id": command_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def complete_simulation_reset(
        self,
        *,
        simulation_id: str,
        status: str,
        command_id: str,
        actor_id: str | None,
        reason: str,
        reset_at: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE simulation_session
                    SET status = :status,
                        last_command_id = :command_id,
                        reset_at = :reset_at,
                        reset_by = :actor_id,
                        reset_reason = :reason,
                        updated_at = now()
                    WHERE simulation_id = :simulation_id
                    """
                ),
                {
                    "simulation_id": simulation_id,
                    "status": status,
                    "command_id": command_id,
                    "actor_id": actor_id,
                    "reason": reason,
                    "reset_at": reset_at,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"simulation_session을 찾을 수 없습니다: {simulation_id}"
                )

    def create_reset_audit(self, values: dict[str, Any]) -> None:
        params = dict(values)
        for name in ("before_summary", "after_summary", "failure_summary"):
            params[name] = (
                json.dumps(params.get(name), ensure_ascii=False, default=str)
                if params.get(name) is not None
                else None
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO simulation_reset_audit (
                        reset_id, command_id, warehouse_id, target_type,
                        target_simulation_id, actor_id, reason, status,
                        affected_simulation_count, before_summary, after_summary,
                        failure_summary, created_at, completed_at
                    ) VALUES (
                        :reset_id, :command_id, :warehouse_id, :target_type,
                        :target_simulation_id, :actor_id, :reason, :status,
                        :affected_simulation_count,
                        CAST(:before_summary AS jsonb), CAST(:after_summary AS jsonb),
                        CAST(:failure_summary AS jsonb), :created_at, :completed_at
                    )
                    ON CONFLICT (reset_id) DO NOTHING
                    """
                ),
                params,
            )

    def finalize_reset_audit(self, reset_id: str, values: dict[str, Any]) -> None:
        params = {"reset_id": reset_id, **values}
        for name in ("before_summary", "after_summary", "failure_summary"):
            params[name] = (
                json.dumps(params.get(name), ensure_ascii=False, default=str)
                if params.get(name) is not None
                else None
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE simulation_reset_audit
                    SET status = :status,
                        affected_simulation_count = :affected_simulation_count,
                        before_summary = CAST(:before_summary AS jsonb),
                        after_summary = CAST(:after_summary AS jsonb),
                        failure_summary = CAST(:failure_summary AS jsonb),
                        completed_at = :completed_at
                    WHERE reset_id = :reset_id
                    """
                ),
                params,
            )

    def list_simulation_runs(
        self,
        simulation_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        """
                        SELECT run_id, simulation_id, command_id, warehouse_id,
                               plan_version, status, checkpoint, created_at
                        FROM simulation_run
                        WHERE simulation_id = :simulation_id
                        ORDER BY created_at DESC, run_id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    {
                        "simulation_id": simulation_id,
                        "limit": limit,
                        "offset": offset,
                    },
                )
            )

    def get_latest_simulation_run(
        self,
        simulation_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest simulation run including its persisted output.

        The lightweight list endpoint intentionally omits JSON payloads, but
        purpose-specific views need the saved routes, tasks, timeline and
        metrics. Keeping this query separate avoids making every run-list call
        return a large payload.
        """

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT run_id, simulation_id, command_id, warehouse_id,
                           plan_version, status, checkpoint, output_payload,
                           created_at
                    FROM simulation_run
                    WHERE simulation_id = :simulation_id
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 1
                    """
                ),
                {"simulation_id": simulation_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def get_latest_simulation_runtime_plan(
        self,
        simulation_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest stored candidate plan for server-side recovery."""

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT run_id, simulation_id, command_id, warehouse_id,
                           plan_version, output_payload, created_at
                    FROM simulation_run
                    WHERE simulation_id = :simulation_id
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 1
                    """
                ),
                {"simulation_id": simulation_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def list_simulation_reset_audits(
        self,
        *,
        warehouse_id: int | None = None,
        simulation_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if warehouse_id is not None:
            conditions.append("warehouse_id = :warehouse_id")
            params["warehouse_id"] = warehouse_id
        if simulation_id is not None:
            conditions.append("target_simulation_id = :simulation_id")
            params["simulation_id"] = simulation_id
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        f"""
                        SELECT * FROM simulation_reset_audit
                        {where}
                        ORDER BY created_at DESC, reset_id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )

    def list_simulation_logs(self, simulation_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            commands = self._rows(
                connection.execute(
                    text(
                        """
                        SELECT DISTINCT ch.*
                        FROM command_history ch
                        LEFT JOIN simulation_reset_audit sra
                          ON sra.command_id = ch.command_id
                        WHERE ch.simulation_id = :simulation_id
                           OR sra.target_simulation_id = :simulation_id
                        ORDER BY ch.received_at ASC
                        """
                    ),
                    {"simulation_id": simulation_id},
                )
            )
            command_ids = [row["command_id"] for row in commands]
            stages = (
                self._rows(
                    connection.execute(
                        text(
                            """
                            SELECT * FROM planning_stage_log
                            WHERE command_id = ANY(:command_ids)
                            ORDER BY created_at ASC, sequence ASC, attempt ASC
                            """
                        ),
                        {"command_ids": command_ids},
                    )
                )
                if command_ids
                else []
            )
        return {
            "commands": commands,
            "stages": stages,
            "reset_audits": self.list_simulation_reset_audits(
                simulation_id=simulation_id,
                limit=200,
                offset=0,
            ),
        }

    def record_simulation(self, state: PlanningState) -> None:
        command = state["command"]
        simulation_id = state.get("simulation_id")
        base_state = state.get("simulation_base_state")
        current_state = state.get("simulation_current_state")
        checkpoint = state.get("simulation_checkpoint")
        output_payload = {
            "conversation_id": command.get("conversation_id"),
            "parent_command_id": command.get("parent_command_id"),
            "conversation_summary": state.get("conversation_summary", {}),
            "interpretation": state.get("interpretation"),
            "supervisor_decision": state.get("supervisor_decision"),
            "supervisor_source": state.get("supervisor_source"),
            "supervisor_prompt_version": state.get("supervisor_prompt_version"),
            "verification_decision": state.get("verification_decision"),
            "verification_evidence": state.get("verification_evidence", []),
            "verification_source": state.get("verification_source"),
            "verification_prompt_version": state.get("verification_prompt_version"),
            "verification_warnings": state.get("verification_warnings", []),
            "replan_attempt": state.get("replan_attempt", 0),
            "max_replan_attempts": state.get("max_replan_attempts", 0),
            "replan_history": state.get("replan_history", []),
            "last_verification_decision": state.get(
                "last_verification_decision"
            ),
            "repeated_failure_signatures": state.get(
                "repeated_failure_signatures", {}
            ),
            "replan_reason": state.get("replan_reason"),
            "original_plan_version": state.get("original_plan_version"),
            "current_plan_version": state.get("current_plan_version"),
            "base_plan_source": state.get("base_plan_source"),
            "base_plan_version": state.get("base_plan_version"),
            "base_plan_is_simulated": state.get("base_plan_is_simulated", False),
            "active_plan_version": state.get("active_plan_version"),
            "scope": state.get("scope"),
            "required_tasks": state.get("required_tasks"),
            "cuopt_plan": state.get("cuopt_plan"),
            "optimization_evidence": state.get("optimization_evidence", []),
            "objective_breakdown": state.get("objective_breakdown"),
            "collision_plan": state.get("collision_plan"),
            "inventory_operations": state.get("inventory_operations", []),
            "scheduled_task_constraints": state.get("interpretation", {}).get(
                "scheduled_task_constraints", []
            ),
            "execution_task_dependencies": state.get("cuopt_plan", {}).get(
                "metadata", {}
            ).get("execution_task_dependencies", []),
            "charger_node_ids": [
                int(row["node_id"])
                for row in state.get("optimization_problem", {}).get("nodes", [])
                if row.get("active", True)
                and str(row.get("node_type") or "").upper() == "CHARGER"
            ],
            "time_step_seconds": state.get("optimization_problem", {}).get(
                "time_step_seconds"
            ),
            "ready_task_ids": state.get("ready_task_ids", []),
            "waiting_task_ids": state.get("waiting_task_ids", []),
            "blocked_task_ids": state.get("blocked_task_ids", []),
            "reference_time": state.get("optimization_problem", {}).get(
                "reference_time"
            ),
            "routing_evidence": state.get("routing_evidence"),
            "reservation_evidence": state.get("reservation_evidence"),
            "distance_comparison": state.get("distance_comparison"),
            "report_evidence": state.get("report_evidence"),
            "report_source": state.get("report_source"),
            "report_prompt_version": state.get("report_prompt_version"),
            "plan_validation": state.get("plan_validation"),
            "simulation": state.get("simulation"),
            "impact": state.get("impact"),
            "errors": state.get("errors", []),
            "warnings": state.get("warnings", []),
            "trace": state.get("trace", []),
            "simulation_id": simulation_id,
        }
        params = {
            "run_id": str(uuid4()),
            "simulation_id": simulation_id,
            "command_id": command["command_id"],
            "warehouse_id": command["warehouse_id"],
            "plan_version": state.get("plan_version"),
            "status": state.get("final_status", "UNKNOWN"),
            "input_payload": json.dumps(command, ensure_ascii=False, default=str),
            "output_payload": json.dumps(
                output_payload,
                ensure_ascii=False,
                default=str,
            ),
            "current_state": (
                json.dumps(current_state, ensure_ascii=False, default=str)
                if current_state is not None
                else None
            ),
            "checkpoint": checkpoint,
            "created_at": datetime.now(UTC),
        }
        insert_statement = text(
            """
            INSERT INTO simulation_run (
                run_id, simulation_id, command_id, warehouse_id, plan_version,
                status, input_payload, output_payload, current_state,
                checkpoint, created_at
            ) VALUES (
                :run_id, :simulation_id, :command_id, :warehouse_id,
                :plan_version, :status, CAST(:input_payload AS jsonb),
                CAST(:output_payload AS jsonb), CAST(:current_state AS jsonb),
                :checkpoint, :created_at
            )
            """
        )
        with self.engine.begin() as connection:
            connection.execute(insert_statement, params)
            if simulation_id and base_state is not None and current_state is not None:
                connection.execute(
                    text(
                        """
                        INSERT INTO simulation_session (
                            simulation_id, warehouse_id, status, generation,
                            base_state, current_state, checkpoint,
                            created_by_command_id, last_command_id,
                            created_at, updated_at
                        ) VALUES (
                            :simulation_id, :warehouse_id, 'ACTIVE', 1,
                            CAST(:base_state AS jsonb), CAST(:current_state AS jsonb),
                            :checkpoint, :command_id, :command_id,
                            :created_at, :created_at
                        )
                        ON CONFLICT (simulation_id) DO UPDATE
                        SET current_state = EXCLUDED.current_state,
                            checkpoint = EXCLUDED.checkpoint,
                            last_command_id = EXCLUDED.last_command_id,
                            status = 'ACTIVE',
                            updated_at = EXCLUDED.updated_at
                        WHERE simulation_session.status NOT IN ('RESET', 'RESET_PENDING')
                        """
                    ),
                    {
                        **params,
                        "base_state": json.dumps(
                            base_state,
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                )

    def commit_completion(self, event: RobotEvent) -> dict[str, Any]:
        """실제 완료 이벤트를 한 트랜잭션으로 반영하며 event_id 재호출에 안전합니다."""
        if event.execution_context != "REAL":
            raise RuntimeError(
                "SIMULATION 이벤트는 실제 warehouse_items 또는 works를 수정할 수 없습니다."
            )
        with self.engine.begin() as connection:
            existing = connection.execute(
                text("SELECT event_id FROM work_event WHERE event_id = :event_id"),
                {"event_id": event.event_id},
            ).first()
            if existing:
                return {"committed": True, "idempotent_replay": True}

            work = connection.execute(
                text(
                    """
                    SELECT work_id, warehouse_id, status, assigned_robot_id,
                           operation_type, item_id, quantity_boxes,
                           inventory_order_id
                    FROM works
                    WHERE work_id = :work_id FOR UPDATE
                    """
                ),
                {"work_id": event.work_id},
            ).mappings().one_or_none()
            if work is None:
                raise CompletionValidationError("WORK_NOT_FOUND")
            if int(work["warehouse_id"]) != int(event.warehouse_id):
                raise CompletionValidationError("WORK_WAREHOUSE_MISMATCH")
            if str(work["status"] or "").upper() == "COMPLETED":
                raise CompletionValidationError("DUPLICATE_COMPLETION_CONFLICT")
            if str(work["status"] or "").upper() not in {
                "READY", "DISPATCHED", "EXECUTING"
            }:
                raise CompletionValidationError("WORK_NOT_COMPLETABLE")
            if (
                work.get("assigned_robot_id")
                and str(work["assigned_robot_id"]) != str(event.robot_id)
            ):
                raise CompletionValidationError("ASSIGNED_ROBOT_MISMATCH")
            if event.task_id and event.work_id and not str(event.task_id).startswith(
                f"{event.work_id}:"
            ):
                raise CompletionValidationError("TASK_WORK_MISMATCH")
            if not event.inventory_deltas and str(work.get("operation_type") or "").upper() == "OUTBOUND":
                raise CompletionValidationError("OUTBOUND_COMPLETION_INVENTORY_DELTAS_REQUIRED")
            planned_quantity = work.get("quantity_boxes")
            if (
                str(work.get("operation_type") or "").upper() == "OUTBOUND"
                and planned_quantity is not None
                and sum(abs(delta.quantity_delta) for delta in event.inventory_deltas)
                != int(planned_quantity)
            ):
                raise CompletionValidationError("OUTBOUND_COMPLETION_QUANTITY_MISMATCH")

            requested_plan_version = event.payload.get("plan_version")
            schedule_plan = connection.execute(
                text(
                    """
                    SELECT plan_version
                    FROM work_schedule_constraints
                    WHERE work_id = :work_id
                    """
                ),
                {"work_id": event.work_id},
            ).mappings().one_or_none()
            if (
                requested_plan_version
                and schedule_plan
                and schedule_plan.get("plan_version")
                and str(requested_plan_version) != str(schedule_plan["plan_version"])
            ):
                raise CompletionValidationError("PLAN_VERSION_MISMATCH")

            robot = connection.execute(
                text(
                    """
                    SELECT robot_id, warehouse_id
                    FROM robot
                    WHERE robot_id = :robot_id
                    FOR UPDATE
                    """
                ),
                {"robot_id": event.robot_id},
            ).mappings().one_or_none()
            if robot is None:
                raise CompletionValidationError("ROBOT_NOT_FOUND")
            if int(robot["warehouse_id"]) != int(event.warehouse_id):
                raise CompletionValidationError("ROBOT_WAREHOUSE_MISMATCH")

            movement_enabled = bool(
                connection.execute(
                    text("SELECT to_regclass('inventory_movement') IS NOT NULL")
                ).scalar()
            )
            current_quantities: dict[str, int] = {}
            inventory_metadata: dict[str, dict[str, Any]] = {}
            for warehouse_item_id in sorted(
                {delta.warehouse_item_id for delta in event.inventory_deltas}
            ):
                row = connection.execute(
                    text(
                        """
                        SELECT warehouse_item_id, warehouse_id, item_id, lot_id, quantity
                        FROM warehouse_items
                        WHERE warehouse_item_id = :warehouse_item_id
                        FOR UPDATE
                        """
                    ),
                    {"warehouse_item_id": warehouse_item_id},
                ).mappings().one_or_none()
                if row is None:
                    raise CompletionValidationError("WAREHOUSE_ITEM_NOT_FOUND")
                if int(row["warehouse_id"]) != int(event.warehouse_id):
                    raise CompletionValidationError("WAREHOUSE_ITEM_WAREHOUSE_MISMATCH")
                if work.get("item_id") and str(row["item_id"]) != str(work["item_id"]):
                    raise CompletionValidationError("WAREHOUSE_ITEM_ITEM_MISMATCH")
                current_quantities[str(warehouse_item_id)] = int(row["quantity"])
                inventory_metadata[str(warehouse_item_id)] = dict(row)

            next_quantities = calculate_inventory_transition(
                current_quantities,
                event.inventory_deltas,
            )
            for warehouse_item_id, new_quantity in next_quantities.items():
                if new_quantity < 0:
                    raise CompletionValidationError("INVENTORY_NEGATIVE_QUANTITY")
                connection.execute(
                    text(
                        """
                        UPDATE warehouse_items
                        SET quantity = :quantity, version = version + 1
                        WHERE warehouse_item_id = :warehouse_item_id
                        """
                    ),
                    {
                        "quantity": new_quantity,
                        "warehouse_item_id": warehouse_item_id,
                    },
                )
            if movement_enabled:
                for delta in event.inventory_deltas:
                    metadata = inventory_metadata[delta.warehouse_item_id]
                    connection.execute(
                        text(
                            """
                            INSERT INTO inventory_movement (
                                movement_id, warehouse_id, item_id, lot_id,
                                warehouse_item_id, work_id, order_id, plan_version, movement_type,
                                quantity_delta_boxes, occurred_at, idempotency_key
                            ) VALUES (
                                :movement_id, :warehouse_id, :item_id, :lot_id,
                                :warehouse_item_id, :work_id, :order_id, :plan_version, 'OUTBOUND_COMPLETED',
                                :quantity_delta_boxes, :occurred_at, :idempotency_key
                            )
                            """
                        ),
                        {
                            "movement_id": str(uuid4()),
                            "warehouse_id": event.warehouse_id,
                            "item_id": metadata["item_id"],
                            "lot_id": metadata.get("lot_id"),
                            "warehouse_item_id": delta.warehouse_item_id,
                            "work_id": event.work_id,
                            "order_id": work.get("inventory_order_id"),
                            "plan_version": requested_plan_version or (schedule_plan or {}).get("plan_version"),
                            "quantity_delta_boxes": delta.quantity_delta,
                            "occurred_at": event.occurred_at,
                            "idempotency_key": (
                                f"{event.event_id}:{delta.warehouse_item_id}"
                            ),
                        },
                    )

            if event.node_id is not None or event.battery is not None:
                connection.execute(
                    text(
                        """
                        UPDATE robot
                        SET node_id = COALESCE(:node_id, node_id),
                            battery = COALESCE(:battery, battery),
                            status = 'IDLE',
                            version = version + 1
                        WHERE robot_id = :robot_id
                        """
                    ),
                    {
                        "node_id": event.node_id,
                        "battery": event.battery,
                        "robot_id": event.robot_id,
                    },
                )

            connection.execute(
                text(
                    """
                    UPDATE works
                    SET status = 'COMPLETED',
                        actual_completed_at = :completed_at,
                        version = version + 1
                    WHERE work_id = :work_id
                    """
                ),
                {"completed_at": event.occurred_at, "work_id": event.work_id},
            )
            outbound_order_completion: dict[str, Any] | None = None
            if str(work.get("operation_type") or "").upper() == "OUTBOUND":
                outbound_id = work.get("inventory_order_id")
                if not outbound_id:
                    outbound = connection.execute(
                        text(
                            """
                            SELECT outbound_id
                            FROM outbound_order_line
                            WHERE warehouse_id = :warehouse_id
                              AND work_id = :work_id
                            FOR UPDATE
                            """
                        ),
                        {"warehouse_id": event.warehouse_id, "work_id": event.work_id},
                    ).mappings().one_or_none()
                    outbound_id = outbound.get("outbound_id") if outbound else None
                if outbound_id:
                    updated = connection.execute(
                        text(
                            """
                            UPDATE outbound_order_line
                            SET status = 'COMPLETED', updated_at = now()
                            WHERE outbound_id = :outbound_id
                              AND warehouse_id = :warehouse_id
                            """
                        ),
                        {"outbound_id": outbound_id, "warehouse_id": event.warehouse_id},
                    )
                    if updated.rowcount != 1:
                        raise CompletionValidationError("OUTBOUND_ORDER_NOT_FOUND")
                    outbound_order_completion = {
                        "completed": True,
                        "outbound_id": str(outbound_id),
                    }
            connection.execute(
                text(
                    """
                    INSERT INTO work_event (
                        event_id, work_id, robot_id, event_type, payload, occurred_at
                    ) VALUES (
                        :event_id, :work_id, :robot_id, :event_type,
                        CAST(:payload AS jsonb), :occurred_at
                    )
                    """
                ),
                {
                    "event_id": event.event_id,
                    "work_id": event.work_id,
                    "robot_id": event.robot_id,
                    "event_type": event.event_type,
                    "payload": event.model_dump_json(),
                    "occurred_at": event.occurred_at,
                },
            )
        return {
            "committed": True,
            "idempotent_replay": False,
            "previous_status": work["status"],
            "outbound_order_completion": outbound_order_completion,
        }

    def commit_inbound_available(self, event: RobotEvent) -> dict[str, Any]:
        """Commit one inspected inbound lot and its append-only movement."""
        if event.execution_context != "REAL":
            raise RuntimeError("SIMULATION 입고 이벤트는 실제 재고를 수정할 수 없습니다.")
        item_id = str(event.payload.get("item_id") or "").strip()
        quantity = int(event.payload.get("quantity_boxes") or 0)
        inbound_id = str(event.payload.get("inbound_id") or "").strip() or None
        warehouse_item_id = str(
            event.payload.get("warehouse_item_id")
            or (
                f"FUTURE:{inbound_id}"
                if inbound_id
                else f"IN-{event.event_id}"
            )
        )
        if not item_id or quantity <= 0:
            raise ValueError("INBOUND_AVAILABLE에는 item_id와 양의 quantity_boxes가 필요합니다.")
        idempotency_key = f"{event.event_id}:{warehouse_item_id}"
        with self.engine.begin() as connection:
            movement_exists = bool(
                connection.execute(
                    text("SELECT to_regclass('inventory_movement') IS NOT NULL")
                ).scalar()
            )
            if not movement_exists:
                raise RuntimeError("migration 010 inventory_movement가 필요합니다.")
            duplicate = connection.execute(
                text(
                    "SELECT movement_id FROM inventory_movement "
                    "WHERE idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if duplicate:
                return {"committed": True, "idempotent_replay": True}

            row = connection.execute(
                text(
                    """
                    SELECT warehouse_item_id, quantity
                    FROM warehouse_items
                    WHERE warehouse_item_id = :warehouse_item_id
                    FOR UPDATE
                    """
                ),
                {"warehouse_item_id": warehouse_item_id},
            ).mappings().first()
            if row:
                connection.execute(
                    text(
                        """
                        UPDATE warehouse_items
                        SET quantity = quantity + :quantity,
                            status = 'AVAILABLE',
                            available_at = :available_at,
                            version = version + 1
                        WHERE warehouse_item_id = :warehouse_item_id
                        """
                    ),
                    {
                        "quantity": quantity,
                        "available_at": event.occurred_at,
                        "warehouse_item_id": warehouse_item_id,
                    },
                )
            else:
                connection.execute(
                    text(
                        """
                        INSERT INTO warehouse_items (
                            warehouse_item_id, warehouse_id, item_id, lot_id,
                            node_id, quantity, reserved_quantity, version,
                            status, received_at, available_at, expiration_at,
                            base_unit
                        ) VALUES (
                            :warehouse_item_id, :warehouse_id, :item_id, :lot_id,
                            :node_id, :quantity, 0, 1, 'AVAILABLE',
                            :received_at, :available_at, :expiration_at, 'BOX'
                        )
                        """
                    ),
                    {
                        "warehouse_item_id": warehouse_item_id,
                        "warehouse_id": event.warehouse_id,
                        "item_id": item_id,
                        "lot_id": event.payload.get("lot_id"),
                        "node_id": event.node_id or event.payload.get("storage_node_id"),
                        "quantity": quantity,
                        "received_at": event.payload.get("actual_arrival_at")
                        or event.occurred_at,
                        "available_at": event.occurred_at,
                        "expiration_at": event.payload.get("expiration_at"),
                    },
                )
            connection.execute(
                text(
                    """
                    INSERT INTO inventory_movement (
                        movement_id, warehouse_id, item_id, lot_id,
                        warehouse_item_id, work_id, order_id, movement_type,
                        quantity_delta_boxes, occurred_at, idempotency_key
                    ) VALUES (
                        :movement_id, :warehouse_id, :item_id, :lot_id,
                        :warehouse_item_id, :work_id, :order_id,
                        'INBOUND_AVAILABLE', :quantity, :occurred_at,
                        :idempotency_key
                    )
                    """
                ),
                {
                    "movement_id": str(uuid4()),
                    "warehouse_id": event.warehouse_id,
                    "item_id": item_id,
                    "lot_id": event.payload.get("lot_id"),
                    "warehouse_item_id": warehouse_item_id,
                    "work_id": event.work_id,
                    "order_id": inbound_id,
                    "quantity": quantity,
                    "occurred_at": event.occurred_at,
                    "idempotency_key": idempotency_key,
                },
            )
            if inbound_id:
                connection.execute(
                    text(
                        """
                        UPDATE inbound_order_line
                        SET status = 'AVAILABLE',
                            actual_available_at = :available_at,
                            warehouse_item_id = :warehouse_item_id,
                            updated_at = :available_at
                        WHERE inbound_id = :inbound_id
                        """
                    ),
                    {
                        "available_at": event.occurred_at,
                        "warehouse_item_id": warehouse_item_id,
                        "inbound_id": inbound_id,
                    },
                )
            if event.work_id:
                connection.execute(
                    text(
                        """
                        UPDATE works
                        SET status = 'COMPLETED', actual_completed_at = :completed_at,
                            version = version + 1
                        WHERE work_id = :work_id
                        """
                    ),
                    {"completed_at": event.occurred_at, "work_id": event.work_id},
                )
        return {
            "committed": True,
            "idempotent_replay": False,
            "warehouse_item_id": warehouse_item_id,
            "quantity_boxes": quantity,
        }

    def persist_work_schedule(
        self,
        *,
        command_id: str,
        plan_version: str,
        dependencies: list[dict[str, Any]],
        constraints: list[dict[str, Any]],
        scheduled_tasks: list[dict[str, Any]],
    ) -> None:
        """Persist user constraints separately from calculated schedule output."""

        with self.engine.begin() as connection:
            for row in dependencies:
                connection.execute(
                    text(
                        """
                        INSERT INTO work_dependencies (
                            predecessor_work_id, successor_work_id,
                            dependency_type, lag_seconds, source_command_id,
                            plan_version
                        ) VALUES (
                            :predecessor_work_id, :successor_work_id,
                            :dependency_type, :lag_seconds, :command_id,
                            :plan_version
                        )
                        ON CONFLICT (predecessor_work_id, successor_work_id)
                        DO UPDATE SET
                            dependency_type = EXCLUDED.dependency_type,
                            lag_seconds = EXCLUDED.lag_seconds,
                            source_command_id = EXCLUDED.source_command_id,
                            plan_version = EXCLUDED.plan_version
                        """
                    ),
                    {**row, "command_id": command_id, "plan_version": plan_version},
                )
            for row in constraints:
                connection.execute(
                    text(
                        """
                        INSERT INTO work_schedule_constraints (
                            work_id, earliest_start, latest_finish,
                            time_constraint_type, fixed_robot_id,
                            same_robot_group, sequence_group, sequence_order,
                            source_command_id, plan_version
                        ) VALUES (
                            :work_id, :earliest_start, :latest_finish,
                            :time_constraint_type, :fixed_robot_id,
                            :same_robot_group, :sequence_group, :sequence_order,
                            :command_id, :plan_version
                        )
                        ON CONFLICT (work_id) DO UPDATE SET
                            earliest_start = EXCLUDED.earliest_start,
                            latest_finish = EXCLUDED.latest_finish,
                            time_constraint_type = EXCLUDED.time_constraint_type,
                            fixed_robot_id = EXCLUDED.fixed_robot_id,
                            same_robot_group = EXCLUDED.same_robot_group,
                            sequence_group = EXCLUDED.sequence_group,
                            sequence_order = EXCLUDED.sequence_order,
                            source_command_id = EXCLUDED.source_command_id,
                            plan_version = EXCLUDED.plan_version,
                            updated_at = now()
                        """
                    ),
                    {**row, "command_id": command_id, "plan_version": plan_version},
                )
            for row in scheduled_tasks:
                if not row.get("work_id"):
                    continue
                connection.execute(
                    text(
                        """
                        UPDATE works
                        SET assigned_robot_id = :robot_id,
                            scheduled_start = :scheduled_start,
                            scheduled_end = :scheduled_end,
                            status = :status,
                            version = version + 1
                        WHERE work_id = :work_id
                        """
                    ),
                    {
                        "work_id": row["work_id"],
                        "robot_id": row["robot_id"],
                        "scheduled_start": row.get("planned_start_at"),
                        "scheduled_end": row.get("planned_end_at"),
                        "status": row.get("schedule_status") or "SCHEDULED",
                    },
                )

    def record_task_started(self, event: RobotEvent) -> dict[str, Any]:
        if event.execution_context != "REAL" or not event.work_id:
            raise RuntimeError("REAL TASK_STARTED 이벤트와 work_id가 필요합니다.")
        with self.engine.begin() as connection:
            existing = connection.execute(
                text("SELECT event_id FROM work_event WHERE event_id = :event_id"),
                {"event_id": event.event_id},
            ).first()
            if existing:
                return {"committed": True, "idempotent_replay": True}
            connection.execute(
                text(
                    """
                    UPDATE works
                    SET status = 'EXECUTING',
                        actual_started_at = COALESCE(actual_started_at, :started_at),
                        version = version + 1
                    WHERE work_id = :work_id
                      AND status NOT IN ('COMPLETED', 'FAILED', 'BLOCKED')
                    """
                ),
                {"work_id": event.work_id, "started_at": event.occurred_at},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO work_event (
                        event_id, work_id, robot_id, event_type, payload, occurred_at
                    ) VALUES (
                        :event_id, :work_id, :robot_id, :event_type,
                        CAST(:payload AS jsonb), :occurred_at
                    )
                    """
                ),
                {
                    "event_id": event.event_id,
                    "work_id": event.work_id,
                    "robot_id": event.robot_id,
                    "event_type": event.event_type,
                    "payload": event.model_dump_json(),
                    "occurred_at": event.occurred_at,
                },
            )
        return {"committed": True, "idempotent_replay": False}
    def transition_successors(
        self,
        work_id: str,
        *,
        occurred_at: datetime,
        predecessor_failed: bool = False,
    ) -> dict[str, list[str]]:
        """Unlock or block direct successors in one deterministic transaction."""

        ready: list[str] = []
        waiting: list[str] = []
        blocked: list[str] = []
        with self.engine.begin() as connection:
            successors = self._rows(
                connection.execute(
                    text(
                        """
                        SELECT d.successor_work_id,
                               c.earliest_start,
                               (
                                   SELECT max(
                                       COALESCE(
                                           predecessor.actual_completed_at,
                                           predecessor.scheduled_end,
                                           :occurred_at
                                       )
                                       + dependency.lag_seconds * interval '1 second'
                                   )
                                   FROM work_dependencies dependency
                                   JOIN works predecessor
                                     ON predecessor.work_id = dependency.predecessor_work_id
                                   WHERE dependency.successor_work_id = d.successor_work_id
                                     AND predecessor.status = 'COMPLETED'
                               ) AS dependency_ready_at
                        FROM work_dependencies d
                        LEFT JOIN work_schedule_constraints c
                          ON c.work_id = d.successor_work_id
                        WHERE d.predecessor_work_id = :work_id
                        ORDER BY d.successor_work_id
                        """
                    ),
                    {"work_id": work_id, "occurred_at": occurred_at},
                )
            )
            for successor in successors:
                successor_id = str(successor["successor_work_id"])
                if predecessor_failed:
                    status = "BLOCKED"
                    blocked.append(successor_id)
                else:
                    incomplete = connection.execute(
                        text(
                            """
                            SELECT count(*)
                            FROM work_dependencies d
                            JOIN works predecessor
                              ON predecessor.work_id = d.predecessor_work_id
                            WHERE d.successor_work_id = :successor_work_id
                              AND predecessor.status <> 'COMPLETED'
                            """
                        ),
                        {"successor_work_id": successor_id},
                    ).scalar_one()
                    not_before = [
                        value
                        for value in (
                            successor.get("earliest_start"),
                            successor.get("dependency_ready_at"),
                        )
                        if value is not None
                    ]
                    if int(incomplete) == 0 and all(
                        value <= occurred_at for value in not_before
                    ):
                        status = "READY"
                        ready.append(successor_id)
                    else:
                        status = "WAITING_FOR_PREDECESSOR"
                        waiting.append(successor_id)
                connection.execute(
                    text(
                        """
                        UPDATE works
                        SET status = :status, version = version + 1
                        WHERE work_id = :work_id
                          AND status NOT IN ('COMPLETED', 'EXECUTING')
                        """
                    ),
                    {"status": status, "work_id": successor_id},
                )
        return {"ready": ready, "waiting": waiting, "blocked": blocked}

    def commit_failure(self, event: RobotEvent) -> dict[str, Any]:
        if event.execution_context != "REAL" or not event.work_id:
            raise RuntimeError("REAL TASK_FAILED 이벤트와 work_id가 필요합니다.")
        with self.engine.begin() as connection:
            existing = connection.execute(
                text("SELECT event_id FROM work_event WHERE event_id = :event_id"),
                {"event_id": event.event_id},
            ).first()
            if existing:
                return {"committed": True, "idempotent_replay": True}
            connection.execute(
                text(
                    """
                    UPDATE works
                    SET status = 'FAILED', version = version + 1
                    WHERE work_id = :work_id
                    """
                ),
                {"work_id": event.work_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO work_event (
                        event_id, work_id, robot_id, event_type, payload, occurred_at
                    ) VALUES (
                        :event_id, :work_id, :robot_id, :event_type,
                        CAST(:payload AS jsonb), :occurred_at
                    )
                    """
                ),
                {
                    "event_id": event.event_id,
                    "work_id": event.work_id,
                    "robot_id": event.robot_id,
                    "event_type": event.event_type,
                    "payload": event.model_dump_json(),
                    "occurred_at": event.occurred_at,
                },
            )
        return {"committed": True, "idempotent_replay": False}

    def update_simulation_checkpoint(
        self,
        event: RobotEvent,
        current_state: dict[str, Any],
        checkpoint: str,
    ) -> dict[str, Any]:
        if event.execution_context != "SIMULATION" or not event.simulation_id:
            raise RuntimeError("simulation checkpoint에는 SIMULATION 컨텍스트가 필요합니다.")
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE simulation_session
                    SET current_state = CAST(:current_state AS jsonb),
                        checkpoint = :checkpoint,
                        last_command_id = COALESCE(last_command_id, created_by_command_id),
                        updated_at = now()
                    WHERE simulation_id = :simulation_id
                    """
                ),
                {
                    "current_state": json.dumps(
                        current_state,
                        ensure_ascii=False,
                        default=str,
                    ),
                    "checkpoint": checkpoint,
                    "simulation_id": event.simulation_id,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"simulation_session을 찾을 수 없습니다: {event.simulation_id}"
                )
        return {"saved": True, "simulation_id": event.simulation_id}

    def create_or_get_scenario_comparison(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        params = dict(values)
        params["request_payload"] = json.dumps(
            params.get("request_payload") or {}, ensure_ascii=False, default=str
        )
        params["recommendation_summary"] = json.dumps(
            params.get("recommendation_summary") or {},
            ensure_ascii=False,
            default=str,
        )
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    INSERT INTO scenario_comparison (
                        comparison_id, request_key, conversation_id, warehouse_id,
                        command_id, status, request_payload,
                        recommendation_summary, created_at
                    ) VALUES (
                        :comparison_id, :request_key, :conversation_id, :warehouse_id,
                        :command_id, :status, CAST(:request_payload AS jsonb),
                        CAST(:recommendation_summary AS jsonb), :created_at
                    )
                    ON CONFLICT (request_key) DO UPDATE
                    SET request_key = EXCLUDED.request_key
                    RETURNING *
                    """
                ),
                params,
            ).mappings().one()
        return dict(row)

    def finalize_scenario_comparison(
        self,
        comparison_id: str,
        *,
        status: str,
        recommendation_summary: dict[str, Any],
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE scenario_comparison
                    SET status = :status,
                        recommendation_summary = CAST(:summary AS jsonb),
                        completed_at = now()
                    WHERE comparison_id = :comparison_id
                    """
                ),
                {
                    "comparison_id": comparison_id,
                    "status": status,
                    "summary": json.dumps(
                        recommendation_summary,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            )

    def upsert_scenario_comparison_run(
        self,
        values: dict[str, Any],
    ) -> None:
        params = dict(values)
        params["scenario_definition"] = json.dumps(
            params.get("scenario_definition") or {},
            ensure_ascii=False,
            default=str,
        )
        params["result_summary"] = json.dumps(
            params.get("result_summary") or {},
            ensure_ascii=False,
            default=str,
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO scenario_comparison_run (
                        comparison_id, scenario_id, simulation_id, command_id,
                        status, scenario_definition, result_summary,
                        created_at, completed_at
                    ) VALUES (
                        :comparison_id, :scenario_id, :simulation_id, :command_id,
                        :status, CAST(:scenario_definition AS jsonb),
                        CAST(:result_summary AS jsonb), :created_at, :completed_at
                    )
                    ON CONFLICT (comparison_id, scenario_id) DO UPDATE
                    SET simulation_id = EXCLUDED.simulation_id,
                        command_id = EXCLUDED.command_id,
                        status = EXCLUDED.status,
                        scenario_definition = EXCLUDED.scenario_definition,
                        result_summary = EXCLUDED.result_summary,
                        completed_at = EXCLUDED.completed_at
                    """
                ),
                params,
            )

    def get_scenario_comparison(
        self,
        comparison_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            comparison = connection.execute(
                text(
                    """
                    SELECT * FROM scenario_comparison
                    WHERE comparison_id = :comparison_id
                    """
                ),
                {"comparison_id": comparison_id},
            ).mappings().one_or_none()
            if comparison is None:
                return None
            runs = self._rows(
                connection.execute(
                    text(
                        """
                        SELECT comparison_id, scenario_id, simulation_id,
                               command_id, status, scenario_definition,
                               result_summary, created_at, completed_at
                        FROM scenario_comparison_run
                        WHERE comparison_id = :comparison_id
                        ORDER BY scenario_id
                        """
                    ),
                    {"comparison_id": comparison_id},
                )
            )
        row = dict(comparison)
        summary = row.get("recommendation_summary") or {}
        if summary:
            row.update(summary)
        row["scenario_runs"] = runs
        return row

    def get_scenario_comparison_run(
        self,
        comparison_id: str,
        scenario_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT comparison_id, scenario_id, simulation_id,
                           command_id, status, scenario_definition,
                           result_summary, created_at, completed_at
                    FROM scenario_comparison_run
                    WHERE comparison_id = :comparison_id
                      AND scenario_id = :scenario_id
                    """
                ),
                {"comparison_id": comparison_id, "scenario_id": scenario_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def list_scenario_comparisons(
        self,
        *,
        warehouse_id: int | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for name, value in (
            ("warehouse_id", warehouse_id),
            ("conversation_id", conversation_id),
            ("status", status),
        ):
            if value is not None:
                conditions.append(f"{name} = :{name}")
                params[name] = value
        if created_from is not None:
            conditions.append("created_at >= :created_from")
            params["created_from"] = created_from
        if created_to is not None:
            conditions.append("created_at <= :created_to")
            params["created_to"] = created_to
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        f"""
                        SELECT comparison_id, conversation_id, warehouse_id,
                               command_id, status, recommendation_summary,
                               created_at, completed_at
                        FROM scenario_comparison
                        {where}
                        ORDER BY created_at DESC, comparison_id
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )

    def create_execution_event_processing(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        params = dict(values)
        for field_name in ("event_payload", "impact_summary", "result_summary"):
            params[field_name] = json.dumps(
                params.get(field_name) or {},
                ensure_ascii=False,
                default=str,
            )
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO execution_event_processing (
                        event_id, warehouse_id, event_type, event_source,
                        status, event_payload, impact_summary, result_summary,
                        approval_required, created_at
                    ) VALUES (
                        :event_id, :warehouse_id, :event_type, :event_source,
                        :status, CAST(:event_payload AS jsonb),
                        CAST(:impact_summary AS jsonb),
                        CAST(:result_summary AS jsonb),
                        :approval_required, :created_at
                    )
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING *
                    """
                ),
                params,
            ).mappings().one_or_none()
            if inserted is not None:
                row = dict(inserted)
                row["duplicate"] = False
                return row
            existing = connection.execute(
                text(
                    """
                    SELECT * FROM execution_event_processing
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": params["event_id"]},
            ).mappings().one()
        row = dict(existing)
        row["duplicate"] = True
        return row

    def get_execution_event_processing(
        self,
        event_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM execution_event_processing
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def finalize_execution_event_processing(
        self,
        event_id: str,
        values: dict[str, Any],
    ) -> None:
        params = {
            "event_id": event_id,
            "status": values["status"],
            "impact_summary": json.dumps(
                values.get("impact_summary") or {},
                ensure_ascii=False,
                default=str,
            ),
            "failure_signature": values.get("failure_signature"),
            "generated_command_id": values.get("generated_command_id"),
            "generated_plan_version": values.get("generated_plan_version"),
            "replan_request_id": values.get("replan_request_id"),
            "approval_required": bool(values.get("approval_required")),
            "result_summary": json.dumps(
                values.get("result_summary") or {},
                ensure_ascii=False,
                default=str,
            ),
            "processed_at": values.get("processed_at") or datetime.now(UTC),
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE execution_event_processing
                    SET status = :status,
                        impact_summary = CAST(:impact_summary AS jsonb),
                        failure_signature = :failure_signature,
                        generated_command_id = :generated_command_id,
                        generated_plan_version = :generated_plan_version,
                        replan_request_id = :replan_request_id,
                        approval_required = :approval_required,
                        result_summary = CAST(:result_summary AS jsonb),
                        processed_at = :processed_at
                    WHERE event_id = :event_id
                    """
                ),
                params,
            )

    def update_execution_event_status(
        self,
        event_id: str,
        *,
        status: str,
        result_summary: dict[str, Any],
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE execution_event_processing
                    SET status = :status,
                        result_summary = CAST(:result_summary AS jsonb),
                        processed_at = now()
                    WHERE event_id = :event_id
                    """
                ),
                {
                    "event_id": event_id,
                    "status": status,
                    "result_summary": json.dumps(
                        result_summary,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            )

    def count_recent_event_failure_signature(
        self,
        warehouse_id: int,
        failure_signature: str,
        *,
        exclude_event_id: str,
        window_seconds: int,
    ) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM execution_event_processing
                        WHERE warehouse_id = :warehouse_id
                          AND failure_signature = :failure_signature
                          AND event_id <> :exclude_event_id
                          AND created_at >= now() - (:window_seconds * interval '1 second')
                        """
                    ),
                    {
                        "warehouse_id": warehouse_id,
                        "failure_signature": failure_signature,
                        "exclude_event_id": exclude_event_id,
                        "window_seconds": window_seconds,
                    },
                ).scalar_one()
            )

    def create_or_get_automatic_replan_request(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        params = dict(values)
        for field_name in (
            "affected_robot_ids",
            "affected_task_ids",
            "result_summary",
        ):
            params[field_name] = json.dumps(
                params.get(field_name) or ([] if field_name != "result_summary" else {}),
                ensure_ascii=False,
                default=str,
            )
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    INSERT INTO automatic_replan_request (
                        request_id, event_id, command_id, warehouse_id, scope,
                        status, execution_context, affected_robot_ids,
                        affected_task_ids, expected_active_plan_version,
                        approval_required, result_summary, created_at
                    ) VALUES (
                        :request_id, :event_id, :command_id, :warehouse_id, :scope,
                        :status, :execution_context,
                        CAST(:affected_robot_ids AS jsonb),
                        CAST(:affected_task_ids AS jsonb),
                        :expected_active_plan_version, :approval_required,
                        CAST(:result_summary AS jsonb), :created_at
                    )
                    ON CONFLICT (request_id) DO UPDATE
                    SET request_id = EXCLUDED.request_id
                    RETURNING *
                    """
                ),
                params,
            ).mappings().one()
        return dict(row)

    def update_automatic_replan_request(
        self,
        request_id: str,
        values: dict[str, Any],
    ) -> None:
        params = {
            "request_id": request_id,
            "status": values["status"],
            "generated_plan_version": values.get("generated_plan_version"),
            "simulation_id": values.get("simulation_id"),
            "verification_decision": values.get("verification_decision"),
            "result_summary": json.dumps(
                values.get("result_summary") or {},
                ensure_ascii=False,
                default=str,
            ),
            "approved_by": values.get("approved_by"),
            "approval_reason": values.get("approval_reason"),
            "approved_at": values.get("approved_at"),
            "rejected_by": values.get("rejected_by"),
            "rejection_reason": values.get("rejection_reason"),
            "rejected_at": values.get("rejected_at"),
            "completed_at": values.get("completed_at"),
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE automatic_replan_request
                    SET status = :status,
                        generated_plan_version = COALESCE(
                            :generated_plan_version, generated_plan_version
                        ),
                        simulation_id = COALESCE(:simulation_id, simulation_id),
                        verification_decision = COALESCE(
                            :verification_decision, verification_decision
                        ),
                        result_summary = CAST(:result_summary AS jsonb),
                        approved_by = COALESCE(:approved_by, approved_by),
                        approval_reason = COALESCE(
                            :approval_reason, approval_reason
                        ),
                        approved_at = COALESCE(:approved_at, approved_at),
                        rejected_by = COALESCE(:rejected_by, rejected_by),
                        rejection_reason = COALESCE(
                            :rejection_reason, rejection_reason
                        ),
                        rejected_at = COALESCE(:rejected_at, rejected_at),
                        completed_at = COALESCE(:completed_at, completed_at)
                    WHERE request_id = :request_id
                    """
                ),
                params,
            )

    def get_automatic_replan_request(
        self,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM automatic_replan_request
                    WHERE request_id = :request_id
                    """
                ),
                {"request_id": request_id},
            ).mappings().one_or_none()
        return dict(row) if row else None


    def approve_execution_plan(self, values: dict[str, Any]) -> dict[str, Any]:
        params = dict(values)
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    INSERT INTO execution_plan_approval (
                        plan_version, warehouse_id, command_id,
                        verification_decision, status, plan_fingerprint,
                        expected_active_plan_version, approved_by,
                        approval_reason, approved_at
                    ) VALUES (
                        :plan_version, :warehouse_id, :command_id,
                        :verification_decision, :status, :plan_fingerprint,
                        :expected_active_plan_version, :approved_by,
                        :approval_reason, :approved_at
                    )
                    ON CONFLICT (plan_version) DO UPDATE
                    SET plan_version = EXCLUDED.plan_version
                    RETURNING *
                    """
                ),
                params,
            ).mappings().one()
        return dict(row)

    def get_execution_plan_approval(
        self, plan_version: str
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM execution_plan_approval
                    WHERE plan_version = :plan_version
                    """
                ),
                {"plan_version": plan_version},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def get_plan_run_by_version(
        self,
        plan_version: str,
        *,
        warehouse_id: int | None = None,
    ) -> dict[str, Any] | None:
        condition = "AND warehouse_id = :warehouse_id" if warehouse_id is not None else ""
        params: dict[str, Any] = {"plan_version": plan_version}
        if warehouse_id is not None:
            params["warehouse_id"] = warehouse_id
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"""
                    SELECT run_id, simulation_id, command_id, warehouse_id,
                           plan_version, status, output_payload, created_at
                    FROM simulation_run
                    WHERE plan_version = :plan_version
                    {condition}
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 1
                    """
                ),
                params,
            ).mappings().one_or_none()
        return dict(row) if row else None

    def create_or_get_execution_dispatch(
        self,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        params = dict(values)
        for field_name in (
            "command_batches",
            "command_states",
            "gateway_result",
            "result_summary",
        ):
            params[field_name] = json.dumps(
                params.get(field_name) or ([] if field_name in {"command_batches", "command_states"} else {}),
                ensure_ascii=False,
                default=str,
            )
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO robot_execution_dispatch (
                        dispatch_id, idempotency_key, warehouse_id, command_id,
                        plan_version, approved_plan_fingerprint,
                        payload_fingerprint, previous_active_plan_version,
                        status, attempt_count, max_attempts, command_batches,
                        command_states, gateway_result, result_summary,
                        created_at, updated_at
                    ) VALUES (
                        :dispatch_id, :idempotency_key, :warehouse_id, :command_id,
                        :plan_version, :approved_plan_fingerprint,
                        :payload_fingerprint, :previous_active_plan_version,
                        :status, :attempt_count, :max_attempts,
                        CAST(:command_batches AS jsonb),
                        CAST(:command_states AS jsonb),
                        CAST(:gateway_result AS jsonb),
                        CAST(:result_summary AS jsonb),
                        :created_at, :updated_at
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING *
                    """
                ),
                params,
            ).mappings().one_or_none()
            if inserted is not None:
                return dict(inserted), True
            existing = connection.execute(
                text(
                    """
                    SELECT * FROM robot_execution_dispatch
                    WHERE idempotency_key = :idempotency_key
                    """
                ),
                {"idempotency_key": values["idempotency_key"]},
            ).mappings().one()
        return dict(existing), False

    def get_latest_execution_dispatch_by_plan_version(
        self, plan_version: str
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM robot_execution_dispatch
                    WHERE plan_version = :plan_version
                    ORDER BY created_at DESC, dispatch_id DESC
                    LIMIT 1
                    """
                ),
                {"plan_version": plan_version},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def get_execution_dispatch(
        self, dispatch_id: str
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM robot_execution_dispatch
                    WHERE dispatch_id = :dispatch_id
                    """
                ),
                {"dispatch_id": dispatch_id},
            ).mappings().one_or_none()
        return dict(row) if row else None

    def update_execution_dispatch(
        self,
        dispatch_id: str,
        values: dict[str, Any],
    ) -> None:
        current = self.get_execution_dispatch(dispatch_id)
        if current is None:
            raise RuntimeError("DISPATCH_NOT_FOUND")
        merged = {**current, **values, "dispatch_id": dispatch_id}
        for field_name in (
            "command_batches",
            "command_states",
            "gateway_result",
            "result_summary",
        ):
            merged[field_name] = json.dumps(
                merged.get(field_name) or ([] if field_name in {"command_batches", "command_states"} else {}),
                ensure_ascii=False,
                default=str,
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE robot_execution_dispatch
                    SET status = :status,
                        attempt_count = :attempt_count,
                        max_attempts = :max_attempts,
                        command_batches = CAST(:command_batches AS jsonb),
                        command_states = CAST(:command_states AS jsonb),
                        gateway_result = CAST(:gateway_result AS jsonb),
                        result_summary = CAST(:result_summary AS jsonb),
                        last_error = :last_error,
                        updated_at = :updated_at,
                        dispatched_at = :dispatched_at,
                        completed_at = :completed_at
                    WHERE dispatch_id = :dispatch_id
                    """
                ),
                merged,
            )

    def list_automatic_replan_requests(
        self,
        warehouse_id: int,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        condition = "AND status = :status" if status else ""
        params: dict[str, Any] = {
            "warehouse_id": warehouse_id,
            "limit": limit,
            "offset": offset,
        }
        if status:
            params["status"] = status
        with self.engine.connect() as connection:
            return self._rows(
                connection.execute(
                    text(
                        f"""
                        SELECT * FROM automatic_replan_request
                        WHERE warehouse_id = :warehouse_id
                        {condition}
                        ORDER BY created_at DESC, request_id
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )

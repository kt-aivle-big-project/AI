from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.repositories import (
    BackendLaroPostgresAdapter,
    BackendLaroSchemaError,
    LegacyPostgresAdapter,
    create_postgres_repository,
)


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self.rows


class FakeConnection:
    def __init__(self, rows_by_table: dict[str, list[dict[str, Any]]]):
        self.rows_by_table = rows_by_table
        self.statements: list[str] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self, statement: Any, params: dict[str, Any]
    ) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if "FROM storage_location sl" in sql:
            return FakeResult(self.rows_by_table.get("storage_location", []))
        if "FROM warehouse_items wi" in sql:
            return FakeResult(self.rows_by_table.get("warehouse_items", []))
        if "FROM product p" in sql:
            return FakeResult(self.rows_by_table.get("product", []))
        if "FROM robot r" in sql:
            return FakeResult(self.rows_by_table.get("robot", []))
        if "FROM task t" in sql:
            if "CASE t.status" in sql:
                return FakeResult(
                    self.rows_by_table.get("task_statuses", [])
                )
            return FakeResult(self.rows_by_table.get("task", []))
        if "FROM warehouse_node n" in sql:
            return FakeResult(self.rows_by_table.get("warehouse_node", []))
        if "FROM warehouse_edge e" in sql:
            return FakeResult(self.rows_by_table.get("warehouse_edge", []))
        raise AssertionError(f"Unexpected SQL in fake connection: {sql}")


class FakeEngine:
    def __init__(self, rows_by_table: dict[str, list[dict[str, Any]]]):
        self.connection = FakeConnection(rows_by_table)

    def connect(self) -> FakeConnection:
        return self.connection


def backend_adapter(
    rows_by_table: dict[str, list[dict[str, Any]]],
) -> BackendLaroPostgresAdapter:
    repository = BackendLaroPostgresAdapter.__new__(
        BackendLaroPostgresAdapter
    )
    repository.engine = FakeEngine(rows_by_table)  # type: ignore[assignment]
    return repository


def test_repository_factory_preserves_legacy_and_selects_backend() -> None:
    legacy = create_postgres_repository(
        "sqlite+pysqlite:///:memory:",
        "legacy_ai",
    )
    backend = create_postgres_repository(
        "sqlite+pysqlite:///:memory:",
        "backend_laro",
    )

    assert isinstance(legacy, LegacyPostgresAdapter)
    assert isinstance(backend, BackendLaroPostgresAdapter)


def test_settings_reject_unknown_postgres_schema_profile() -> None:
    assert (
        Settings(_env_file=None).postgres_schema_profile
        == "legacy_ai"
    )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            postgres_schema_profile="automatic",  # type: ignore[arg-type]
        )


def test_backend_robot_id_is_string_and_defaults_are_safe() -> None:
    repository = backend_adapter(
        {
            "robot": [
                {
                    "robot_id": 3,
                    "warehouse_id": 1,
                    "node_id": 152,
                    "battery": 78,
                    "status": "IDLE",
                    "robot_code": "AGV-100",
                }
            ]
        }
    )

    robots = repository.fetch_robots(1)

    assert robots == [
        {
            "robot_id": "3",
            "robot_code": "AGV-100",
            "warehouse_id": 1,
            "node_id": 152,
            "battery": 78.0,
            "status": "IDLE",
            "max_load": 0.0,
            "current_load": 0.0,
            "version": 1,
        }
    ]


def test_backend_inventory_joins_product_code_and_sets_defaults() -> None:
    repository = backend_adapter(
        {
            "warehouse_items": [
                {
                    "warehouse_item_id": 30,
                    "warehouse_id": 1,
                    "item_id": "C",
                    "node_id": 90,
                    "quantity": 12,
                    "expiry_date": "2026-12-31",
                }
            ]
        }
    )

    inventory = repository.fetch_inventory(1, ["C"])

    assert inventory[0]["warehouse_item_id"] == "30"
    assert inventory[0]["item_id"] == "C"
    assert inventory[0]["lot_id"] == "BACKEND-30"
    assert inventory[0]["reserved_quantity"] == 0
    assert inventory[0]["available_quantity"] == 12
    assert inventory[0]["received_at"] is None
    assert inventory[0]["expiry_date"] == "2026-12-31"
    assert inventory[0]["expiration_at"] is None
    sql = repository.engine.connection.statements[0]  # type: ignore[union-attr]
    assert "JOIN product p" in sql
    assert "p.product_id = wi.item_id" in sql


def test_backend_map_contract_normalizes_node_and_edge_types() -> None:
    repository = backend_adapter(
        {
            "warehouse_node": [
                {
                    "node_id": 88,
                    "warehouse_id": 1,
                    "zone_id": "STORAGE_ZONE",
                    "node_code": "K0_0",
                    "node_type": "RACK_STORAGE",
                    "x": 5.0,
                    "y": 1.1,
                    "charging_status": None,
                    "charging_power": None,
                },
                {
                    "node_id": 150,
                    "warehouse_id": 1,
                    "zone_id": "CHARGING_ZONE",
                    "node_code": "C01",
                    "node_type": "CHARGING_SLOT",
                    "x": 4.55,
                    "y": 5.64,
                    "charging_status": "AVAILABLE",
                    "charging_power": 50,
                },
            ],
            "warehouse_edge": [
                {
                    "edge_id": 1,
                    "from_node": 1,
                    "to_node": 2,
                    "distance": 0.9,
                    "direction_type": "BOTH",
                },
                {
                    "edge_id": 2,
                    "from_node": 3,
                    "to_node": 4,
                    "distance": 1.2,
                    "direction_type": "B_TO_A",
                },
            ],
        }
    )

    nodes = repository.fetch_map_nodes(1)
    edges = repository.fetch_map_edges(1)

    assert nodes[0]["node_type"] == "STORAGE"
    assert nodes[1]["node_type"] == "CHARGER"
    assert nodes[1]["charger_capacity"] == 1
    assert nodes[1]["charger_power_kw"] == 50.0
    assert edges[0]["direction"] == "BOTH"
    assert edges[1]["from_node"] == 4
    assert edges[1]["to_node"] == 3
    assert edges[1]["direction"] == "ONE_WAY"


def test_backend_storage_capacity_is_aggregated_for_ai_contract() -> None:
    repository = backend_adapter(
        {
            "storage_location": [
                {
                    "storage_location_id": 1,
                    "warehouse_id": 1,
                    "node_id": 88,
                    "max_quantity": 100,
                    "occupied_quantity": 40,
                    "available_quantity": 60,
                    "status": "AVAILABLE",
                },
                {
                    "storage_location_id": 2,
                    "warehouse_id": 1,
                    "node_id": 89,
                    "max_quantity": 80,
                    "occupied_quantity": 30,
                    "available_quantity": 50,
                    "status": "AVAILABLE",
                },
            ]
        }
    )

    capacity = repository.fetch_storage_capacity(1)

    assert capacity is not None
    assert capacity["capacity_value"] == 180
    assert capacity["usable_capacity_value"] == 110
    assert capacity["capacity_unit"] == "BOX"
    assert len(capacity["locations"]) == 2


def test_optional_ai_columns_are_not_required_but_required_fields_fail() -> None:
    mapped = BackendLaroPostgresAdapter._map_inventory_row(
        {
            "warehouse_item_id": 1,
            "warehouse_id": 1,
            "item_id": "A",
            "node_id": 88,
            "quantity": 5,
        }
    )
    assert mapped["expiry_date"] is None
    assert mapped["base_unit"] == "BOX"

    with pytest.raises(
        BackendLaroSchemaError,
        match="필수 필드.*item_id",
    ):
        BackendLaroPostgresAdapter._map_inventory_row(
            {
                "warehouse_item_id": 1,
                "warehouse_id": 1,
                "node_id": 88,
                "quantity": 5,
            }
        )


def test_backend_task_is_mapped_into_read_only_planning_work() -> None:
    repository = backend_adapter(
        {
            "task": [
                {
                    "work_id": "41",
                    "warehouse_id": 1,
                    "task_code": "TASK-41",
                    "item_id": "7",
                    "quantity": 3,
                    "source_node": 88,
                    "target_node": 143,
                    "priority": 100,
                    "status": "PENDING",
                    "assigned_robot_id": None,
                    "scheduled_start": None,
                    "scheduled_end": None,
                    "version": 1,
                    "operation_type": "OUTBOUND",
                    "required_at": None,
                }
            ]
        }
    )

    works = repository.fetch_open_works(1, 21)

    assert works[0]["work_id"] == "41"
    assert works[0]["status"] == "NEW"
    assert works[0]["operation_type"] == "OUTBOUND"
    assert works[0]["source_node"] == 88
    assert works[0]["target_node"] == 143
    assert works[0]["quantity_boxes"] == 3
    statement = repository.engine.connection.statements[0]  # type: ignore[union-attr]
    assert "CAST(:simulation_run_id AS BIGINT)" in statement


def test_backend_snapshot_marks_execution_persistence_read_only() -> None:
    repository = backend_adapter(
        {
            "warehouse_items": [],
            "product": [],
            "storage_location": [],
            "robot": [],
            "task": [],
            "task_statuses": [],
        }
    )

    snapshot = repository.snapshot(1, [])

    assert snapshot["inbound_orders"] == []
    assert snapshot["outbound_orders"] == []
    assert snapshot["works"] == []
    assert snapshot["work_dependencies"] == []
    assert snapshot["work_schedule_constraints"] == []
    assert snapshot["warnings"][0]["code"] == (
        "BACKEND_LARO_EXECUTION_PERSISTENCE_READ_ONLY"
    )
    assert snapshot["warnings"][0]["persistence"] == (
        "READ_ONLY_NOT_CONFIGURED"
    )


def test_backend_profile_does_not_expose_legacy_mutations() -> None:
    repository = backend_adapter({})

    assert repository.create_or_get_command_history({}) is None
    assert repository.record_simulation({}) is None
    assert not hasattr(repository, "commit_completion")
    with pytest.raises(
        RuntimeError,
        match="BACKEND_LARO_EXECUTION_PERSISTENCE_NOT_CONFIGURED",
    ):
        repository.approve_execution_plan()

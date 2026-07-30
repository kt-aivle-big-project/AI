from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from app.domain.schemas import (
    ContextSnapshot,
    RobotRuntime,
    RobotRuntimeContext,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
)
from app.infrastructure.postgres import PostgresWarehouseAdapter
from app.services.context_service import WarehouseContextService
from app.services.simulation_plan_service import SimulationPlanBuilder


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = False

    def execute(self, sql: str, _params=None):
        self.statements.append(" ".join(sql.split()))
        return self

    def commit(self) -> None:
        self.committed = True


class _SeedOrderAdapter(PostgresWarehouseAdapter):
    def __init__(self, connection: _RecordingConnection) -> None:
        super().__init__(pool=object())
        self.connection = connection

    @contextmanager
    def _connection(self):
        yield self.connection

    def count_summary(self, warehouse_id=None):
        return {"orders": 1, "inbound_receipts": 1}


def test_live_postgres_seed_creates_facility_before_business_documents() -> None:
    connection = _RecordingConnection()
    adapter = _SeedOrderAdapter(connection)
    adapter.seed_from_documents(
        warehouse_id="WH-001",
        inventory={
            "racks": [
                {
                    "rack_id": "K1_1",
                    "access_node_ids": ["K1_1_ACCESS_A", "K1_1_ACCESS_B"],
                    "levels": [
                        {"level": 1, "status": "EMPTY", "item": None},
                        {"level": 2, "status": "EMPTY", "item": None},
                        {"level": 3, "status": "EMPTY", "item": None},
                    ],
                }
            ]
        },
        facility={
            "inbound_handoffs": [
                {
                    "handoff_id": "IN_HANDOFF_1",
                    "access_node_ids": ["IN_HANDOFF_1_ACCESS_A"],
                    "buffer_capacity": 1,
                }
            ],
            "inbound_ports": [
                {"port_id": "I_a", "label": "A", "handoff_id": "IN_HANDOFF_1"}
            ],
            "outbound_chutes": [{"chute_id": "O_A", "label": "A"}],
            "outbound_stations": [
                {
                    "station_id": "OUT_STATION_1",
                    "station_robot_id": "SR-1",
                    "access_node_ids": ["OUT_STATION_1_ACCESS_A"],
                    "served_chute_ids": ["O_A"],
                    "tote_buffer_capacity": 1,
                }
            ],
            "station_robots": [
                {"station_robot_id": "SR-1", "station_id": "OUT_STATION_1"}
            ],
            "empty_tote_buffers": [
                {
                    "buffer_id": "EMPTY_TOTE_BUFFER_1",
                    "access_node_ids": ["EMPTY_TOTE_BUFFER_1_ACCESS"],
                    "capacity": 1,
                }
            ],
        },
        scenario={
            "orders": [
                {
                    "order_id": "ORD-001",
                    "item_id": "ITEM-1",
                    "required_qty": 1,
                    "delivery_node": "O_A",
                }
            ],
            "inbound_receipts": [
                {
                    "inbound_id": "IN-001",
                    "handling_unit_id": "HU-IN-001",
                    "item_id": "ITEM-1",
                    "quantity": 1,
                    "source_port_id": "I_a",
                    "target_rack_id": "K1_1",
                    "target_rack_level": 1,
                }
            ],
        },
    )
    statements = connection.statements

    def position(fragment: str) -> int:
        return next(index for index, value in enumerate(statements) if fragment in value)

    assert position("INSERT INTO inbound_handoffs") < position("INSERT INTO inbound_ports")
    assert position("INSERT INTO inbound_ports") < position("INSERT INTO inbound_receipts")
    assert position("INSERT INTO outbound_chutes") < position("INSERT INTO orders")
    assert position("INSERT INTO outbound_stations") < position("INSERT INTO station_robots")
    assert connection.committed


def test_simulation_plan_builder_materializes_implicit_time_gaps_as_waits() -> None:
    result = SimpleNamespace(
        status="plan_validated",
        warehouse_id="WH-001",
        simulation_id="SIM-GAP",
        traffic_schedule=TrafficScheduleResult(
            valid=True,
            routes=[
                TimedRobotRoute(
                    robot_id="R001",
                    finish_at_ms=3500,
                    steps=[
                        TimedRouteStep(
                            step_type="MOVE",
                            start_at_ms=500,
                            end_at_ms=2750,
                            edge_id="H0_0",
                            from_node="R0_0",
                            to_node="R0_1",
                        ),
                        TimedRouteStep(
                            step_type="SERVICE",
                            start_at_ms=3000,
                            end_at_ms=3500,
                            node_id="R0_1",
                            task_id="TASK-1_DROP",
                            service_kind="DROP",
                        ),
                    ],
                )
            ],
            makespan_ms=3500,
        ),
        robot_context=RobotRuntimeContext(
            robots=[
                RobotRuntime(
                    warehouse_id="WH-001",
                    robot_id="R001",
                    robot_code="R001",
                    status="idle",
                    battery_pct=90,
                    capacity_units=1,
                    current_node="R0_0",
                )
            ],
            candidate_robot_ids=["R001"],
            summary="gap probe",
        ),
        execution_optimizer_result=None,
        optimizer_result=None,
        goods_to_person_compilation=None,
        normalized_request=None,
        optimization_request=None,
        inventory_context=None,
        context_snapshot=ContextSnapshot(
            snapshot_id="SNAP-GAP",
            captured_at="2026-07-28T00:00:00Z",
            graph_version="MAP-GAP",
            inventory_version="INV-GAP",
            runtime_version="RUN-GAP",
        ),
    )
    plan = SimulationPlanBuilder().build(result)
    assert plan is not None
    steps = plan.robots[0].steps
    assert [value.step_type for value in steps] == ["WAIT", "MOVE", "WAIT", "SERVICE"]
    assert [(value.start_at_ms, value.end_at_ms) for value in steps] == [
        (0, 500),
        (500, 2750),
        (2750, 3000),
        (3000, 3500),
    ]
    move = steps[1]
    assert move.distance_m == 2.25
    assert move.nominal_travel_time_ms == 2250


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _InventoryConnection:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def execute(self, sql: str, _params=None):
        self.statements.append(" ".join(sql.split()))
        return _RowsResult(self.rows)


class _InventoryAdapter(PostgresWarehouseAdapter):
    def __init__(self, rows) -> None:
        super().__init__(pool=object())
        self.connection = _InventoryConnection(rows)

    @contextmanager
    def _connection(self):
        yield self.connection


def test_live_postgres_item_stocks_match_inventory_context_contract() -> None:
    adapter = _InventoryAdapter(
        [
            {
                "warehouse_id": "WH-001",
                "handling_unit_id": "HU-K1_7-L1-ITEM_BEARING",
                "stock_id": "STOCK-K1_7-L1-ITEM_BEARING",
                "item_id": "ITEM_BEARING",
                "item_name": "Bearing",
                "category": "PART",
                "quantity": 12,
                "capacity": 20,
                "unit": "EA",
                "home_rack_id": "K1_7",
                "home_rack_level": 1,
                "status": "stored",
                "version": 3,
                "access_node_ids": ["K1_7_ACCESS_A", "K1_7_ACCESS_B"],
            }
        ]
    )

    values = adapter.item_stocks("WH-001", "ITEM_BEARING")

    assert len(values) == 1
    value = values[0]
    assert value["item_name"] == "Bearing"
    assert value["unit"] == "EA"
    assert value["rack_id"] == "K1_7"
    assert value["rack_level"] == 1
    assert value["access_node_ids"] == ["K1_7_ACCESS_A", "K1_7_ACCESS_B"]
    assert value["handling_unit_status"] == "stored"
    assert "JOIN racks" in adapter.connection.statements[0]

    candidate = WarehouseContextService._candidate_stock(value)
    assert candidate.stock_id == "STOCK-K1_7-L1-ITEM_BEARING"
    assert candidate.access_node_ids == ["K1_7_ACCESS_A", "K1_7_ACCESS_B"]


def test_live_postgres_item_name_falls_back_to_item_id() -> None:
    adapter = _InventoryAdapter(
        [
            {
                "warehouse_id": "WH-001",
                "handling_unit_id": "HU-1",
                "stock_id": "STOCK-1",
                "item_id": "ITEM-1",
                "item_name": None,
                "category": None,
                "quantity": 1,
                "capacity": 1,
                "unit": None,
                "home_rack_id": "K1_1",
                "home_rack_level": 2,
                "status": "stored",
                "version": 0,
                "access_node_ids": ["K1_1_ACCESS_A"],
            }
        ]
    )

    value = adapter.item_stocks("WH-001", "ITEM-1")[0]
    assert value["item_name"] == "ITEM-1"
    assert value["unit"] == "EA"

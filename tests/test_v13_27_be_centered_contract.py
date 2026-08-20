from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.be_compat import BeCompatRuntimeRobot
from app.domain.be_centered import BeLowBatteryContext
from app.domain.schemas import (
    EdgeReservation,
    PlanHandoverPoint,
    ReplanExecutionSnapshot,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    StructuredMissionInput,
    StructuredOperationInput,
)
from app.repositories.request_operation_repository import RequestOperationRepository
from app.services.context_service import WarehouseContextService
from app.services.be_centered_plan_service import (
    _canonical_request_log_type,
    _low_battery_event,
    _merge_replan_operation_overlay,
    _with_quiesced_runtime_states,
    _with_low_battery_runtime_state,
)
from app.services.simulation_plan_service import RollingHorizonReplanService

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT.parent


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PLAN", "PLAN"),
        ("HITL_PLAN", "PLAN"),
        ("HITL", "PLAN"),
        ("REPLAN", "REPLAN"),
        ("HITL_REPLAN", "REPLAN"),
    ],
)
def test_human_review_request_log_keeps_plan_or_replan_contract(raw, expected):
    assert _canonical_request_log_type(raw) == expected


def test_unknown_request_log_type_is_rejected_before_database_insert():
    with pytest.raises(ValueError, match="Unsupported BE request log type"):
        _canonical_request_log_type("UNKNOWN")


class FakeBeRepository:
    warehouse_id = "WH-001"
    simulation_id = "BE-RUN-1"
    versions = {"map_version": "1", "inventory_version": "1"}
    source_manifest = {
        "route_nodes": "neo4j_projection_from_be_map",
        "inventory_units": "be_warehouse_items_live",
    }
    outbound_chutes = {"O_D": {"chute_id": "O_D"}}

    def canonical_item_code(self, item_id, product_code):
        return product_code or f"ITEM-{item_id}"

    def facility_by_code(self, code):
        values = {
            "I_a": {"facility_code": "I_a", "access_node_id": "IN_HANDOFF_1_ACCESS_A"},
            "O_D": {"facility_code": "O_D", "access_node_id": "O_D"},
        }
        return values.get(code)

    def node_code_for_numeric_id(self, node_id):
        return f"N{node_id}"

    def node_code_for_storage_location(self, storage_id):
        return f"K{storage_id}_ACCESS_A"

    def rack_id_for_access_node(self, node_code):
        return node_code.removesuffix("_ACCESS_A")

    def rack_access_nodes(self, rack_id):
        return [f"{rack_id}_ACCESS_A"]

    def empty_putaway_slots(self):
        return []


def test_low_battery_safe_stop_state_becomes_rule_runtime_override():
    context = BeLowBatteryContext(
        robot_id="R225",
        robot_numeric_id=225,
        battery_pct=20,
        charging_threshold_pct=20,
        current_node="A03",
        current_node_numeric_id=103,
        current_task_id=2792,
        carrying_load=False,
        stopped_at_sim_time_ms=12_500,
    )

    event = _low_battery_event(context)
    overrides = _with_low_battery_runtime_state(
        RuntimePlanningOverrides(), context
    )
    robot = overrides.robot_states[0]

    assert event.robot_id == "R225"
    assert event.node_id == "A03"
    assert event.payload["status"] == "LOW_BATTERY"
    assert event.payload["battery_pct"] == 20
    assert robot.robot_id == "R225"
    assert robot.current_node == "A03"
    assert robot.status == "low_battery"
    assert robot.battery_pct == 20
    assert robot.current_load_units == 0
    assert robot.sim_time_ms == 12_500
    assert overrides.planning_horizon_start_ms == 12_500
    assert overrides.relocate_idle_robot_ids == ["R225"]


def test_quiesced_empty_robots_become_authoritative_handover_states():
    class RuntimeRepository:
        @staticmethod
        def all_robots():
            return [
                {
                    "robot_id": "R333",
                    "current_node": "R2_8",
                    "current_edge": None,
                    "status": "idle",
                    "battery_pct": 81,
                    "capacity_units": 1,
                    "current_load_units": 0,
                    "active_task_id": "TASK-OLD",
                    "safe_handover_at_ms": 9_800,
                    "sim_time_ms": 11_200,
                },
                {
                    "robot_id": "R334",
                    "current_node": "R2_9",
                    "current_edge": None,
                    "status": "waiting",
                    "battery_pct": 77,
                    "capacity_units": 1,
                    "current_load_units": 0,
                    "safe_handover_at_ms": 0,
                    "sim_time_ms": 11_200,
                },
                {
                    "robot_id": "R335",
                    "current_node": "R3_1",
                    "current_edge": None,
                    "status": "working",
                    "battery_pct": 72,
                    "capacity_units": 1,
                    "current_load_units": 1,
                    "sim_time_ms": 11_200,
                },
            ]

    result = _with_quiesced_runtime_states(
        RuntimePlanningOverrides(),
        RuntimeRepository(),
        replan_at_sim_time_ms=11_200,
    )
    by_robot = {value.robot_id: value for value in result.robot_states}

    assert set(by_robot) == {"R333", "R334"}
    assert by_robot["R333"].current_node == "R2_8"
    assert by_robot["R333"].active_task_id is None
    assert by_robot["R333"].clear_active_work
    assert by_robot["R333"].safe_handover_reached
    assert by_robot["R333"].sim_time_ms == 9_800
    assert by_robot["R334"].current_node == "R2_9"
    assert by_robot["R334"].status == "idle"
    assert by_robot["R334"].sim_time_ms == 0


def test_quiesced_low_battery_robot_uses_actual_safe_stop_instead_of_plan_projection():
    class RuntimeRepository:
        @staticmethod
        def all_robots():
            return [
                {
                    "robot_id": "R366",
                    "current_node": "R3_10",
                    "current_edge": None,
                    "status": "low_battery",
                    "battery_pct": 20,
                    "capacity_units": 1,
                    "current_load_units": 0,
                    "active_task_id": None,
                    "safe_handover_at_ms": 18_675,
                    "sim_time_ms": 24_900,
                }
            ]

    trigger_state = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R366",
                current_node="R2_10",
                status="low_battery",
                battery_pct=20,
                current_load_units=1,
                active_task_id="TASK-3831",
                sim_time_ms=16_600,
            )
        ]
    )

    result = _with_quiesced_runtime_states(
        trigger_state,
        RuntimeRepository(),
        replan_at_sim_time_ms=24_900,
    )
    robot = result.robot_states[0]

    assert robot.robot_id == "R366"
    assert robot.current_node == "R3_10"
    assert robot.status == "low_battery"
    assert robot.battery_pct == 20
    assert robot.current_load_units == 0
    assert robot.active_task_id is None
    assert robot.clear_active_work
    assert robot.safe_handover_reached
    assert robot.sim_time_ms == 18_675


def test_spring_runtime_contract_reads_real_clock_and_carrying_load():
    runtime = BeCompatRuntimeRobot.model_validate(
        {
            "robotId": 336,
            "warehouseId": 68,
            "currentNodeId": 9657,
            "currentNodeCode": "R3_0",
            "batteryLevel": 20,
            "status": "WAITING",
            "currentTaskId": 3599,
            "carryingLoad": True,
            "simulationTimeMillis": 11_200,
            "waitStartedAtMillis": 10_750,
        }
    )

    assert runtime.carrying_load is True
    assert runtime.simulation_time_millis == 11_200
    assert runtime.wait_started_at_ms == 10_750


def test_safe_handover_reconciliation_prevents_projected_start_node_collapse():
    snapshot = ReplanExecutionSnapshot(
        source_plan_id="PLAN-OLD",
        replan_at_sim_time_ms=11_200,
        earliest_handover_at_ms=23_615,
        latest_handover_at_ms=23_615,
        handover_points=[
            PlanHandoverPoint(
                robot_id="R333",
                node_id="R3_10",
                handover_at_ms=23_615,
                reason="old plan projection",
            ),
            PlanHandoverPoint(
                robot_id="R334",
                node_id="R3_10",
                handover_at_ms=23_615,
                reason="old plan projection",
            ),
        ],
        robot_overrides=[
            RobotRuntimeOverride(
                robot_id="R333", current_node="R3_10", sim_time_ms=23_615
            ),
            RobotRuntimeOverride(
                robot_id="R334", current_node="R3_10", sim_time_ms=23_615
            ),
        ],
    )
    actual = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R333",
                current_node="R2_8",
                clear_active_work=True,
                safe_handover_reached=True,
                sim_time_ms=11_200,
            ),
            RobotRuntimeOverride(
                robot_id="R334",
                current_node="R2_9",
                clear_active_work=True,
                safe_handover_reached=True,
                sim_time_ms=11_200,
            ),
        ]
    )

    reconciled = RollingHorizonReplanService._reconcile_safe_handover_states(
        snapshot,
        actual,
    )
    points = {value.robot_id: value for value in reconciled.handover_points}
    overrides = {value.robot_id: value for value in reconciled.robot_overrides}

    assert points["R333"].node_id == "R2_8"
    assert points["R334"].node_id == "R2_9"
    assert points["R333"].handover_policy == "CURRENT_NODE"
    assert reconciled.latest_handover_at_ms == 11_200
    assert overrides["R333"].current_node == "R2_8"
    assert overrides["R334"].current_node == "R2_9"


def test_low_battery_robot_drops_stale_plan_reservations_after_safe_stop():
    snapshot = ReplanExecutionSnapshot(
        source_plan_id="PLAN-1",
        replan_at_sim_time_ms=10_000,
        earliest_handover_at_ms=10_000,
        latest_handover_at_ms=10_000,
        preserved_edge_reservations=[
            EdgeReservation(
                reservation_id="RES-OLD",
                edge_id="E-A03-A04",
                robot_id="R225",
                direction="A03>A04",
                start_at_ms=11_000,
                end_at_ms=12_000,
            )
        ],
    )
    explicit = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R225",
                current_node="A03",
                status="low_battery",
                battery_pct=20,
                sim_time_ms=10_000,
            )
        ],
        relocate_idle_robot_ids=["R225"],
    )

    merged = RollingHorizonReplanService._merge_runtime_overrides(
        snapshot, explicit
    )

    assert merged.preserved_edge_reservations == []
    assert merged.robot_states[0].current_node == "A03"


def test_structured_input_is_authoritative_and_creates_existing_event_contracts():
    value = StructuredMissionInput(
        request_id="REQ-001",
        operations=[
            StructuredOperationInput(
                operation_id="OUT-001",
                operation_type="OUTBOUND",
                product_code="ITEM_BEARING",
                quantity=5,
                destination_facility_code="O_D",
            ),
            StructuredOperationInput(
                operation_id="IN-001",
                operation_type="INBOUND",
                product_code="ITEM_SENSOR",
                quantity=3,
                source_facility_code="I_a",
                destination_storage_location_id=10,
            ),
        ],
    )
    events = value.to_events()
    assert [(event.type, event.order_id, event.inbound_id) for event in events] == [
        ("new_order", "OUT-001", None),
        ("inbound_item_arrived", None, "IN-001"),
    ]


def test_duplicate_operation_ids_are_rejected():
    with pytest.raises(ValidationError):
        StructuredMissionInput(
            operations=[
                StructuredOperationInput(
                    operation_id="OP-1",
                    operation_type="OUTBOUND",
                    product_code="ITEM-A",
                    destination_node_code="O_A",
                ),
                StructuredOperationInput(
                    operation_id="OP-1",
                    operation_type="INBOUND",
                    product_code="ITEM-B",
                    source_node_code="I_A",
                    destination_node_code="K1_ACCESS_A",
                ),
            ]
        )


def test_request_overlay_exposes_operations_without_order_or_hu_tables():
    value = StructuredMissionInput(
        request_id="REQ-OVERLAY",
        operations=[
            StructuredOperationInput(
                operation_id="OUT-X",
                operation_type="OUTBOUND",
                product_code="ITEM_BEARING",
                quantity=2,
                source_warehouse_item_id=77,
                destination_facility_code="O_D",
            ),
            StructuredOperationInput(
                operation_id="IN-X",
                operation_type="INBOUND",
                product_code="ITEM_SENSOR",
                quantity=3,
                source_facility_code="I_a",
                destination_storage_location_id=12,
            ),
        ],
    )
    repository = RequestOperationRepository(FakeBeRepository(), value)
    order = repository.get_order("OUT-X")
    inbound = repository.get_inbound_receipt("IN-X")
    assert order["source_warehouse_item_id"] == 77
    assert order["logical_destination_id"] == "O_D"
    assert inbound["source_node"] == "IN_HANDOFF_1_ACCESS_A"
    assert inbound["target_node"] == "K12_ACCESS_A"
    assert repository.source_manifest["orders"] == "not_used"
    assert repository.source_manifest["handling_units"] == "not_used"


def test_outbound_station_id_remains_logical_when_access_node_is_also_supplied():
    value = StructuredMissionInput(
        request_id="REQ-STATION-ACCESS",
        operations=[
            StructuredOperationInput(
                operation_id="ORD-1001",
                operation_type="OUTBOUND",
                product_code="ITEM_BEARING",
                quantity=1,
                source_warehouse_item_id=77,
                destination_node_code="OUT_STATION_1_ACCESS_A",
                destination_facility_code="OUT_STATION_1",
            )
        ],
    )

    order = RequestOperationRepository(FakeBeRepository(), value).get_order("ORD-1001")

    assert order["logical_destination_id"] == "OUT_STATION_1"
    assert order["delivery_node"] == "OUT_STATION_1"
    assert order["delivery_access_node"] == "OUT_STATION_1_ACCESS_A"


def test_inbound_destination_can_be_left_for_plan_putaway_assignment():
    value = StructuredMissionInput(
        request_id="REQ-AUTO-PUTAWAY",
        operations=[
            StructuredOperationInput(
                operation_id="IN-101",
                operation_type="INBOUND",
                product_code="ITEM_SENSOR",
                quantity=3,
                source_facility_code="I_a",
                attributes='{"transport_unit":"BOX","box_count":1}',
            )
        ],
    )

    repository = RequestOperationRepository(FakeBeRepository(), value)
    inbound = repository.get_inbound_receipt("IN-101")

    assert inbound["source_node"] == "IN_HANDOFF_1_ACCESS_A"
    assert inbound["target_node"] is None
    assert inbound["target_rack_id"] is None
    assert inbound["target_rack_level"] is None
    assert inbound["quantity"] == 3
    assert inbound["transport_unit_count"] == 1


def test_replan_context_restores_slot_reserved_by_same_be_task():
    value = StructuredMissionInput(
        request_id="REQ-COMMITTED-PUTAWAY",
        operations=[
            StructuredOperationInput(
                operation_id="IN-812",
                operation_type="INBOUND",
                task_id=812,
                product_code="ITEM_SENSOR",
                quantity=1,
                source_facility_code="I_a",
                destination_node_code="K12_ACCESS_A",
                target_rack_level=2,
            )
        ],
    )
    repository = RequestOperationRepository(FakeBeRepository(), value)

    inventory = WarehouseContextService(repository).build_inventory_context(
        inbound_ids=["IN-812"]
    )

    assert inventory.inbound_needs[0].task_id == 812
    assert inventory.inbound_needs[0].target_rack_id == "K12"
    assert [
        (
            slot.rack_id,
            slot.rack_level,
            slot.reservation_task_id,
            slot.access_node_ids,
        )
        for slot in inventory.candidate_putaway_slots
    ] == [("K12", 2, 812, ["K12_ACCESS_A"])]


def test_replan_overlay_retains_prior_operation_facts_and_current_values_win():
    prior = StructuredMissionInput(
        request_id="REQ-OLD",
        operations=[
            StructuredOperationInput(
                operation_id="OUT-OLD",
                operation_type="OUTBOUND",
                product_code="ITEM-001",
                quantity=10,
                source_warehouse_item_id=101,
                destination_facility_code="OUT_STATION_1",
            ),
            StructuredOperationInput(
                operation_id="OUT-SAME",
                operation_type="OUTBOUND",
                product_code="ITEM-002",
                quantity=5,
                source_warehouse_item_id=102,
                destination_facility_code="OUT_STATION_2",
            ),
        ],
    )
    current = StructuredMissionInput(
        request_id="REQ-NEW",
        operations=[
            StructuredOperationInput(
                operation_id="OUT-SAME",
                operation_type="OUTBOUND",
                product_code="ITEM-002",
                quantity=7,
                source_warehouse_item_id=102,
                destination_facility_code="OUT_STATION_2",
            ),
            StructuredOperationInput(
                operation_id="OUT-NEW",
                operation_type="OUTBOUND",
                product_code="ITEM-003",
                quantity=2,
                source_warehouse_item_id=103,
                destination_facility_code="OUT_STATION_3",
            ),
        ],
    )

    merged = _merge_replan_operation_overlay(
        current,
        {"structured_input": prior.model_dump(mode="json")},
    )

    by_id = {value.operation_id: value for value in merged.operations}
    assert merged.request_id == "REQ-NEW"
    assert set(by_id) == {"OUT-OLD", "OUT-SAME", "OUT-NEW"}
    assert by_id["OUT-OLD"].source_warehouse_item_id == 101
    assert by_id["OUT-SAME"].quantity == 7


def test_active_compose_does_not_mount_native_orders_or_handling_units_schema():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "004_be_centered_extensions.sql" in compose
    assert "003_be_shared_contract.sql" in compose
    assert "001_schema.sql:/docker-entrypoint-initdb.d" not in compose
    assert "WAREHOUSE_REPOSITORY_BACKEND: be_shared" in compose


def test_active_extension_schema_contains_only_missing_be_concepts():
    sql = (ROOT / "db/postgres/004_be_centered_extensions.sql").read_text(encoding="utf-8")
    normalized = " ".join(sql.casefold().split())
    assert "create table if not exists orders" not in normalized
    assert "create table if not exists handling_units" not in normalized
    for table in (
        "warehouse_profile",
        "node_profile",
        "edge_profile",
        "rack_slot",
        "warehouse_item_profile",
        "robot_profile",
        "facility",
        "inventory_reservation",
        "simulation_plan",
        "request_log",
    ):
        assert f"create table if not exists laro_ext.{table}" in normalized
    assert "from public.warehouse_items" in normalized
    assert "from public.simulation_runs" in normalized


def test_retired_spring_nodes_are_excluded_from_ai_route_views():
    sql = (ROOT / "db/postgres/004_be_centered_extensions.sql").read_text(
        encoding="utf-8"
    ).casefold()
    compat = (ROOT / "app/repositories/be_compat_repository.py").read_text(
        encoding="utf-8"
    ).casefold()

    assert "na.row_data->>'is_active'" in sql
    assert "to_jsonb(fn)->>'is_active'" in sql
    assert "to_jsonb(tn)->>'is_active'" in sql
    assert "to_jsonb(warehouse_node)->>'is_active'" in compat
    assert "to_jsonb(fn)->>'is_active'" in compat
    assert "to_jsonb(tn)->>'is_active'" in compat


def test_be_shared_inventory_uses_three_live_rack_levels():
    sql = (ROOT / "db/postgres/004_be_centered_extensions.sql").read_text(
        encoding="utf-8"
    ).casefold()
    adapter = (ROOT / "app/infrastructure/be_centered_postgres.py").read_text(
        encoding="utf-8"
    ).casefold()

    assert "wi.rack_level" in sql
    assert "cross join generate_series(1, 3)" in adapter
    assert "wi.rack_level = levels.rack_level" in adapter
    assert "1 as rack_level" not in adapter


def test_be_addition_is_isolated_under_laro_package():
    base = PACKAGE_ROOT / "BE/src/main/java/com/aivle/be/laro"
    expected = {
        "client/LaroPlanClient.java",
        "controller/LaroPlanController.java",
        "dto/LaroPlanRequest.java",
        "dto/LaroPlanResponse.java",
        "dto/LaroPreflightResponse.java",
        "dto/LaroLowBatteryContext.java",
        "service/LaroPlanExecutionService.java",
        "service/LaroInventoryReservationService.java",
        "service/LaroPlanService.java",
        "service/LaroReplanStateService.java",
        "service/LaroTaskId.java",
    }
    actual = {
        str(path.relative_to(base)).replace("\\", "/")
        for path in base.rglob("*.java")
    }
    assert expected <= actual
    controller = (base / "controller/LaroPlanController.java").read_text(encoding="utf-8")
    assert "/api/laro/simulation-runs" in controller
    client = (base / "client/LaroPlanClient.java").read_text(encoding="utf-8")
    assert "/api/v1/simulation-runs/{id}/missions/plan" in client


def test_source_warehouse_item_id_is_a_hard_inventory_constraint():
    from app.services.goods_to_person_service import GoodsToPersonPlanningService

    class InventoryRepository(FakeBeRepository):
        def handling_units(self, item_id):
            return [
                {
                    "warehouse_item_id": 10,
                    "inventory_unit_id": "WI-10",
                    "handling_unit_id": "WI-10",
                    "handling_unit_status": "stored",
                    "quantity": 10,
                },
                {
                    "warehouse_item_id": 20,
                    "inventory_unit_id": "WI-20",
                    "handling_unit_id": "WI-20",
                    "handling_unit_status": "stored",
                    "quantity": 4,
                },
            ]

    service = GoodsToPersonPlanningService(InventoryRepository())
    cycles = service._allocate_handling_units(
        item_id="ITEM_BEARING",
        orders=[
            {
                "order_id": "OUT-1",
                "required_qty": 3,
                "priority": "high",
                "logical_destination_id": "O_D",
                "source_warehouse_item_id": 20,
            }
        ],
        require_single=False,
    )
    assert len(cycles) == 1
    assert cycles[0][0]["warehouse_item_id"] == 20
    assert cycles[0][1][0].quantity == 3


def test_unpinned_inventory_keeps_smallest_sufficient_row_policy():
    from app.services.goods_to_person_service import GoodsToPersonPlanningService

    class InventoryRepository(FakeBeRepository):
        def handling_units(self, item_id):
            return [
                {
                    "warehouse_item_id": 10,
                    "inventory_unit_id": "WI-10",
                    "handling_unit_id": "WI-10",
                    "handling_unit_status": "stored",
                    "quantity": 10,
                },
                {
                    "warehouse_item_id": 20,
                    "inventory_unit_id": "WI-20",
                    "handling_unit_id": "WI-20",
                    "handling_unit_status": "stored",
                    "quantity": 6,
                },
            ]

    service = GoodsToPersonPlanningService(InventoryRepository())
    cycles = service._allocate_handling_units(
        item_id="ITEM_BEARING",
        orders=[
            {
                "order_id": "OUT-1",
                "required_qty": 5,
                "priority": "medium",
                "logical_destination_id": "O_D",
            }
        ],
        require_single=False,
    )
    assert cycles[0][0]["warehouse_item_id"] == 20


def test_structured_input_blocks_command_invented_operations():
    from app.domain.schemas import (
        NormalizedOperation,
        NormalizedRequestConstraints,
        NormalizedWarehouseRequest,
    )
    from app.graph.input_formulation import _preserve_authoritative_structured_input

    structured = StructuredMissionInput(
        request_id="REQ-AUTHORITY",
        operations=[
            StructuredOperationInput(
                operation_id="OUT-REAL",
                operation_type="OUTBOUND",
                product_code="ITEM_BEARING",
                destination_facility_code="O_D",
            )
        ],
    )
    llm_value = NormalizedWarehouseRequest(
        source="mixed",
        operations=[
            NormalizedOperation(
                operation_id="OUT-INVENTED",
                operation_type="OUTBOUND_ORDER",
                source_event_type="natural_language",
            )
        ],
        constraints=NormalizedRequestConstraints(
            excluded_robot_ids=["R003"]
        ),
        raw_user_command="OUT-INVENTED도 추가하고 R003은 제외해.",
        normalization_summary="LLM result",
    )
    result = _preserve_authoritative_structured_input(
        {
            "events": structured.to_events(),
            "structured_input": structured,
            "user_command": "OUT-INVENTED도 추가하고 R003은 제외해.",
        },
        llm_value,
    )
    assert [value.operation_id for value in result.operations] == ["OUT-REAL"]
    assert result.constraints.excluded_robot_ids == ["R003"]
    assert "complete operation authority" in result.normalization_summary


def test_plan_version_and_request_retry_reads_are_be_database_backed():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.infrastructure.be_centered_postgres import BeCenteredPostgresAdapter

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, args=None):
            if "MAX(plan_version)" in sql:
                return Result({"next_version": 4})
            if "FROM laro_ext.request_log" in sql:
                return Result(
                    {
                        "simulation_run_id": 7,
                        "response_json": {
                            "api_version": "v1",
                            "simulation_run_id": 7,
                            "warehouse_id": "WH-001",
                            "warehouse_numeric_id": 1,
                            "request_id": "REQ-7",
                            "result": {
                                "status": "input_rejected",
                                "warehouse_id": "WH-001",
                                "simulation_id": "BE-RUN-7",
                                "request_mode": "event_driven",
                                "errors": [],
                            },
                        },
                    }
                )
            raise AssertionError(sql)

    class Postgres:
        @contextmanager
        def _connection(self):
            yield Connection()

    adapter = BeCenteredPostgresAdapter(
        settings=SimpleNamespace(),
        manager=SimpleNamespace(postgres=Postgres()),
    )
    adapter._views_ready = True
    assert adapter.next_plan_version(7) == 4
    response = adapter.load_request_response("REQ-7", 7)
    assert response is not None
    assert response["simulation_run_id"] == 7
    assert response["request_id"] == "REQ-7"


def test_be_centered_service_persists_final_versioned_plan_and_outer_retry_response():
    service = (
        ROOT / "app/services/be_centered_plan_service.py"
    ).read_text(encoding="utf-8")
    assert "persist_simulation_plan=False" in service
    assert "next_plan_version(simulation_run_id)" in service
    assert '"plan_version": next_version' in service
    assert "SimulationPlanStore().save(plan, result)" in service
    assert "save_inventory_reservations" in service
    assert "response_json=response.model_dump" in service


def test_inventory_reservation_is_connected_to_plan_and_be_inventory():
    sql = (ROOT / "db/postgres/004_be_centered_extensions.sql").read_text(
        encoding="utf-8"
    ).casefold()
    adapter = (
        ROOT / "app/infrastructure/be_centered_postgres.py"
    ).read_text(encoding="utf-8")
    assert "plan_id text not null" in sql
    assert "foreign key (plan_id) references laro_ext.simulation_plan" in sql
    assert "foreign key (warehouse_item_id) references public.warehouse_items" in sql
    assert "reservation_filter = \"status = 'active'\"" in adapter.casefold()
    assert "plan_id <> %s" in adapter.casefold()
    assert "save_inventory_reservations" in adapter


def test_route_planning_fields_use_spring_columns_as_authority():
    sql = (ROOT / "db/postgres/004_be_centered_extensions.sql").read_text(
        encoding="utf-8"
    )
    prepare = (ROOT / "scripts/prepare_be_centered_data.py").read_text(
        encoding="utf-8"
    )

    assert sql.index("na.row_data->>'service_only'") < sql.index(
        "na.row_data->'route_attributes'->>'service_only'"
    )
    assert sql.index("ea.row_data->>'speed_limit_mps'") < sql.index(
        "ea.row_data->'route_attributes'->>'speed_limit_mps'"
    )
    assert "resource_type=%s,resource_code=%s,side=%s" in prepare
    assert "edge_type=%s,speed_limit_mps=%s" in prepare

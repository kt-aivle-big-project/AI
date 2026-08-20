from pydantic import TypeAdapter

from app.domain.be_runtime import BeRuntimeRobot
from app.domain.schemas import (
    EventInput,
    PlanHandoverPoint,
    ReplanExecutionSnapshot,
    ReplanReason,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    StructuredMissionInput,
    StructuredOperationInput,
)
from app.graph.input_formulation import _structured_normalized_request
from app.repositories.request_operation_repository import RequestOperationRepository
from app.services.be_centered_plan_service import (
    _trusted_replan_planning_mode,
    _with_quiesced_runtime_states,
)
from app.services.simulation_plan_service import RollingHorizonReplanService
from app.services.terminal_relocation_service import charge_service_duration_ms


def test_low_battery_is_a_supported_replan_reason() -> None:
    assert TypeAdapter(ReplanReason).validate_python("LOW_BATTERY") == "LOW_BATTERY"
    assert _trusted_replan_planning_mode("LOW_BATTERY") == "force_rule"
    assert _trusted_replan_planning_mode("NEW_ORDER") is None


def test_low_battery_signal_does_not_create_fake_business_work() -> None:
    normalized = _structured_normalized_request(
        {"events": [EventInput(type="low_battery")]}  # type: ignore[arg-type]
    )

    assert normalized.operations == []


def test_charge_service_reserves_time_to_reach_full_battery() -> None:
    assert charge_service_duration_ms(
        battery_pct=20,
        charge_rate_pct_per_minute=50,
        minimum_service_ms=500,
    ) == 96_000


def test_charge_service_keeps_minimum_duration_for_full_robot() -> None:
    assert charge_service_duration_ms(
        battery_pct=100,
        charge_rate_pct_per_minute=50,
        minimum_service_ms=500,
    ) == 500


class _RackAwareRequestRepository:
    warehouse_id = "WH-001"
    simulation_id = "BE-RUN-1"
    versions = {}
    source_manifest = {}
    outbound_chutes = {}

    @staticmethod
    def rack_id_for_access_node(node_code: str) -> str | None:
        return {"K2_2_ACCESS_A": "K2_2"}.get(node_code)

    @staticmethod
    def rack(rack_id: str) -> dict | None:
        return {"rack_id": rack_id} if rack_id == "K2_2" else None

    @staticmethod
    def rack_access_nodes(rack_id: str) -> list[str]:
        return ["K2_2_ACCESS_A"] if rack_id == "K2_2" else []


def test_low_battery_replan_keeps_committed_be_rack_destination() -> None:
    structured_input = StructuredMissionInput(
        request_id="REQ-LOW-BATTERY-REPLAN",
        operations=[
            StructuredOperationInput(
                operation_id="IN-COMMITTED",
                operation_type="INBOUND",
                task_id=41,
                product_code="ITEM_SENSOR",
                quantity=1,
                source_node_code="IN_HANDOFF_1_ACCESS_A",
                destination_node_code="K2_2",
                target_rack_level=3,
            )
        ],
    )

    inbound = RequestOperationRepository(
        _RackAwareRequestRepository(), structured_input
    ).get_inbound_receipt("IN-COMMITTED")

    assert inbound["task_id"] == 41
    assert inbound["target_node"] == "K2_2"
    assert inbound["target_rack_id"] == "K2_2"
    assert inbound["target_rack_level"] == 3


def test_low_battery_replan_still_maps_access_node_to_rack() -> None:
    structured_input = StructuredMissionInput(
        operations=[
            StructuredOperationInput(
                operation_id="IN-ACCESS",
                operation_type="INBOUND",
                product_code="ITEM_SENSOR",
                source_node_code="IN_HANDOFF_1_ACCESS_A",
                destination_node_code="K2_2_ACCESS_A",
                target_rack_level=1,
            )
        ]
    )

    inbound = RequestOperationRepository(
        _RackAwareRequestRepository(), structured_input
    ).get_inbound_receipt("IN-ACCESS")

    assert inbound["target_rack_id"] == "K2_2"
    assert inbound["target_rack_level"] == 1


def test_spring_runtime_contract_preserves_real_clock_and_load() -> None:
    runtime = BeRuntimeRobot.model_validate(
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
        }
    )

    assert runtime.carrying_load is True
    assert runtime.simulation_time_millis == 11_200


def test_quiesced_positions_replace_duplicate_future_projection() -> None:
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
                    "sim_time_ms": 11_200,
                },
                {
                    "robot_id": "R335",
                    "current_node": "R3_1",
                    "current_edge": None,
                    "status": "waiting",
                    "battery_pct": 73,
                    "capacity_units": 1,
                    "current_load_units": 1,
                    "sim_time_ms": 11_200,
                },
            ]

    actual = _with_quiesced_runtime_states(
        RuntimePlanningOverrides(),
        RuntimeRepository(),
        replan_at_sim_time_ms=11_200,
    )
    snapshot = ReplanExecutionSnapshot(
        source_plan_id="PLAN-OLD",
        replan_at_sim_time_ms=11_200,
        earliest_handover_at_ms=23_615,
        latest_handover_at_ms=23_615,
        handover_points=[
            PlanHandoverPoint(
                robot_id=robot_id,
                node_id="R3_10",
                handover_at_ms=23_615,
                reason="old plan projection",
            )
            for robot_id in ("R333", "R334")
        ],
        robot_overrides=[
            RobotRuntimeOverride(
                robot_id=robot_id,
                current_node="R3_10",
                sim_time_ms=23_615,
            )
            for robot_id in ("R333", "R334")
        ],
    )

    reconciled = RollingHorizonReplanService._reconcile_safe_handover_states(
        snapshot,
        actual,
    )
    points = {value.robot_id: value for value in reconciled.handover_points}

    assert {value.robot_id for value in actual.robot_states} == {"R333", "R334"}
    assert points["R333"].node_id == "R2_8"
    assert points["R334"].node_id == "R2_9"
    assert reconciled.latest_handover_at_ms == 11_200

from app.domain.schemas import (
    ReplanExecutionSnapshot,
    RobotRuntime,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    SimulationLogicalOperation,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
)
from app.services.be_centered_plan_service import _with_quiesced_runtime_states
from app.services.simulation_plan_service import RollingHorizonReplanService


def test_robot_runtime_accepts_spring_safe_handover_clock() -> None:
    runtime = RobotRuntime(
        robot_id="R001",
        robot_code="R001",
        status="WAITING",
        battery_pct=19,
        capacity_units=1,
        safe_handover_at_ms=0,
        sim_time_ms=12_000,
    )

    assert runtime.safe_handover_at_ms == 0
    assert runtime.sim_time_ms == 12_000


def test_robot_runtime_allows_initial_snapshot_without_handover_clock() -> None:
    runtime = RobotRuntime(
        robot_id="R002",
        robot_code="R002",
        status="IDLE",
        battery_pct=100,
        capacity_units=1,
        safe_handover_at_ms=None,
    )

    assert runtime.safe_handover_at_ms is None


def test_quiesced_low_battery_status_is_accepted_as_safe_stop() -> None:
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

    result = _with_quiesced_runtime_states(
        RuntimePlanningOverrides(
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
        ),
        RuntimeRepository(),
        replan_at_sim_time_ms=24_900,
    )

    robot = result.robot_states[0]
    assert robot.current_node == "R3_10"
    assert robot.status == "low_battery"
    assert robot.current_load_units == 0
    assert robot.active_task_id is None
    assert robot.safe_handover_reached is True
    assert robot.sim_time_ms == 18_675


def test_low_battery_replan_replaces_worker_and_keeps_four_task_robots() -> None:
    active = SimulationPlan(
        plan_id="PLAN-LOW-BATTERY-REPLACEMENT",
        plan_version=1,
        warehouse_id="WH-001",
        simulation_id="SIM-LOW-BATTERY",
        map_version="MAP-1",
        robots=[
            SimulationRobotPlan(
                robot_id=robot_id,
                initial_node=f"N{index}",
                finish_at_ms=1_000,
                steps=[
                    SimulationPlanStep(
                        step_id=f"{robot_id}-1",
                        sequence=1,
                        step_type="SERVICE",
                        start_at_ms=0,
                        end_at_ms=1_000,
                        node_id=f"N{index}",
                        task_id=f"TASK-{index}",
                        service_kind="PICKUP",
                    )
                ],
            )
            for index, robot_id in enumerate(
                ("R001", "R002", "R003", "R004"), start=1
            )
        ],
        logical_operations=[
            SimulationLogicalOperation(
                operation_id=f"ORD-{index}",
                operation_type="OUTBOUND_ORDER",
                task_ids=[f"TASK-{index}"],
            )
            for index in range(1, 9)
        ],
    )
    snapshot = ReplanExecutionSnapshot(
        source_plan_id=active.plan_id,
        replan_at_sim_time_ms=1_000,
        earliest_handover_at_ms=1_000,
        latest_handover_at_ms=1_000,
    )

    runtime_overrides = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id=robot_id,
                current_node=f"N{index}",
                status=("low_battery" if robot_id == "R001" else "idle"),
                battery_pct=battery,
            )
            for index, (robot_id, battery) in enumerate(
                (
                    ("R001", 20),
                    ("R002", 70),
                    ("R003", 75),
                    ("R004", 80),
                    ("R005", 95),
                    ("R006", 85),
                ),
                start=1,
            )
        ]
    )
    replacement = (
        RollingHorizonReplanService._low_battery_replacement_runtime_overrides(
            active,
            snapshot,
            runtime_overrides,
        )
    )

    assert replacement.allowed_task_robot_ids == [
        "R002",
        "R003",
        "R004",
        "R005",
    ]
    assert replacement.minimum_task_vehicle_count == 4
    assert "R001" not in replacement.allowed_task_robot_ids
    assert "R006" not in replacement.allowed_task_robot_ids

    two_new_operations = (
        RollingHorizonReplanService._low_battery_replacement_runtime_overrides(
            active,
            snapshot.model_copy(
                update={
                    "completed_task_bases": [
                        f"TASK-{index}" for index in range(1, 9)
                    ]
                }
            ),
            runtime_overrides,
            replannable_operation_count=2,
        )
    )
    assert two_new_operations.allowed_task_robot_ids == ["R003", "R004"]
    assert two_new_operations.minimum_task_vehicle_count == 2

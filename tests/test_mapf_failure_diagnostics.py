from __future__ import annotations

from types import SimpleNamespace

from app.domain.schemas import (
    EdgeReservation,
    MAPFValidationResult,
    PlanHandoverPoint,
    ReplanExecutionSnapshot,
    RobotRuntime,
    RobotRuntimeContext,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    TerminalRelocationRecord,
    TerminalRelocationResult,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
)
from app.graph import v9_planning
from app.services.context_service import apply_runtime_overrides
from app.services.simulation_plan_service import RollingHorizonReplanService


def _failed_low_battery_state() -> tuple[dict, TrafficScheduleResult]:
    schedule = TrafficScheduleResult(
        valid=True,
        routes=[
            TimedRobotRoute(
                robot_id="R248",
                steps=[
                    TimedRouteStep(
                        step_type="SERVICE",
                        start_at_ms=10_600,
                        end_at_ms=20_600,
                        node_id="C01",
                        task_id="TERMINAL-R248-CHARGE",
                        service_kind="CHARGE",
                    )
                ],
                finish_at_ms=20_600,
            )
        ],
        total_service_ms=10_000,
        makespan_ms=20_600,
    )
    state = {
        "simulation_id": "BE-RUN-138",
        "runtime_overrides": RuntimePlanningOverrides(
            robot_states=[
                RobotRuntimeOverride(
                    robot_id="R248",
                    current_node="R2_0",
                    status="low_battery",
                    battery_pct=20,
                    current_load_units=0,
                    active_task_id="2854",
                    sim_time_ms=10_600,
                )
            ],
            relocate_idle_robot_ids=["R248"],
        ),
        "terminal_relocation": TerminalRelocationResult(
            applied=True,
            relocations=[
                TerminalRelocationRecord(
                    robot_id="R248",
                    policy="CHARGE",
                    from_node="R2_0",
                    to_node="C01",
                    task_id="TERMINAL-R248-CHARGE",
                    reason="Low-battery robot returns after safe handover.",
                )
            ],
        ),
    }
    return state, schedule


def test_mapf_failure_diagnostics_identifies_low_battery_return() -> None:
    state, schedule = _failed_low_battery_state()
    validation = MAPFValidationResult(
        valid=False,
        errors=["Station CHARGE capacity=1 overlap: R248 100-200 vs R249 200-300."],
    )

    diagnostics = v9_planning._mapf_failure_diagnostics(state, schedule, validation)

    assert diagnostics["simulation_id"] == "BE-RUN-138"
    assert diagnostics["low_battery_robots"][0]["robot_id"] == "R248"
    assert diagnostics["low_battery_robots"][0]["current_node"] == "R2_0"
    assert diagnostics["terminal_relocations"][0]["to_node"] == "C01"
    assert diagnostics["mapf_routes"][0]["charge_task_ids"] == ["TERMINAL-R248-CHARGE"]
    assert "사용 시간이 다른 로봇과 겹쳤습니다" in diagnostics["operator_summary"]


def test_validator_logs_context_and_exposes_operator_message(monkeypatch) -> None:
    state, schedule = _failed_low_battery_state()
    validation = MAPFValidationResult(
        valid=False,
        errors=["R248 waits at unsafe node R2_0."],
    )
    state.update(
        {
            "traffic_schedule": schedule,
            "map_context": object(),
            "optimization_request": SimpleNamespace(max_edge_wait_ms=None),
            "execution_payload": object(),
            "graph_node_types": {},
        }
    )
    output: list[str] = []

    monkeypatch.setattr(
        v9_planning,
        "model_from_state",
        lambda current, key, _model: current[key],
    )
    monkeypatch.setattr(
        v9_planning,
        "MAPFPlanValidator",
        lambda: SimpleNamespace(validate=lambda **_kwargs: validation),
    )
    monkeypatch.setattr(v9_planning, "safe_console_print", output.append)

    update = v9_planning.mapf_plan_validator_node(state)

    assert update["workflow_status"] == "human_review"
    assert "배터리 부족 로봇 R248" in update["traffic_schedule"].conflicts[0]
    assert "안전 대기 노드가 아닌 위치" in update["traffic_schedule"].conflicts[0]
    assert update["traffic_schedule"].conflicts[1] == validation.errors[0]
    assert len(output) == 1
    assert '\"robot_id\": \"R248\"' in output[0]
    assert '\"to_node\": \"C01\"' in output[0]


def test_planner_failure_exposes_diagnostic_before_human_review(monkeypatch) -> None:
    state, _ = _failed_low_battery_state()
    expansion = WaypointRouteExpansionResult(
        status="failed",
        errors=["R248 waits at unsafe node R2_0."],
    )
    schedule = TrafficScheduleResult(
        valid=False,
        conflicts=["R248 waits at unsafe node R2_0."],
    )
    state.update(
        {
            "execution_payload": object(),
            "execution_optimizer_result": object(),
            "map_context": object(),
            "graph_node_types": {},
        }
    )
    output: list[str] = []

    monkeypatch.setattr(
        v9_planning,
        "model_from_state",
        lambda current, key, _model: current[key],
    )
    monkeypatch.setattr(
        v9_planning,
        "PrioritizedSIPPPlanner",
        lambda: SimpleNamespace(plan=lambda **_kwargs: (expansion, schedule)),
    )
    monkeypatch.setattr(v9_planning, "safe_console_print", output.append)

    update = v9_planning.prioritized_mapf_planner_node(state)

    assert update["waypoint_route_expansion"].status == "failed"
    assert update["traffic_schedule"].valid is False
    assert "배터리 부족 로봇 R248" in update["traffic_schedule"].conflicts[0]
    assert update["errors"][0].code == "mapf_route_expansion_failed"
    assert update["errors"][0].retryable is True
    assert len(output) == 1
    assert output[0].startswith("[prioritized_mapf_planner 진단]")
    assert '\"robot_id\": \"R248\"' in output[0]


def test_loaded_low_battery_robot_finishes_commitment_before_charging() -> None:
    snapshot = ReplanExecutionSnapshot(
        source_plan_id="PLAN-LOADED",
        replan_at_sim_time_ms=18_000,
        earliest_handover_at_ms=30_000,
        latest_handover_at_ms=30_000,
        handover_points=[
            PlanHandoverPoint(
                robot_id="R225",
                node_id="O_D",
                handover_at_ms=30_000,
                reason="Finish the carried outbound unit.",
                handover_policy="CURRENT_OPERATION_END",
                locked_task_ids=["TASK-3260_PICK", "TASK-3260_DROP"],
                carrying_load=True,
            )
        ],
        robot_overrides=[
            RobotRuntimeOverride(
                robot_id="R225",
                current_node="O_D",
                status="idle",
                current_load_units=0,
                sim_time_ms=30_000,
            )
        ],
        preserved_edge_reservations=[
            EdgeReservation(
                reservation_id="RES-COMMITTED",
                edge_id="E-R1_10-O_D",
                robot_id="R225",
                direction="R1_10_TO_O_D",
                start_at_ms=18_000,
                end_at_ms=29_000,
            )
        ],
        locked_task_bases=["TASK-3260"],
    )
    explicit = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R225",
                current_node="R1_10",
                status="low_battery",
                battery_pct=20,
                current_load_units=1,
                active_task_id="3260",
                sim_time_ms=18_000,
            )
        ],
        planning_horizon_start_ms=18_000,
        relocate_idle_robot_ids=["R225"],
    )

    merged = RollingHorizonReplanService._merge_runtime_overrides(
        snapshot, explicit
    )

    robot = merged.robot_states[0]
    assert robot.status == "low_battery"
    assert robot.battery_pct == 20
    assert robot.current_node == "O_D"
    assert robot.current_load_units == 0
    assert robot.active_task_id is None
    assert robot.sim_time_ms == 30_000
    assert [value.reservation_id for value in merged.preserved_edge_reservations] == [
        "RES-COMMITTED"
    ]


def test_low_battery_robot_starts_charge_route_from_projected_safe_handover() -> None:
    snapshot = ReplanExecutionSnapshot(
        source_plan_id="PLAN-OLD",
        replan_at_sim_time_ms=89_702,
        earliest_handover_at_ms=92_700,
        latest_handover_at_ms=118_437,
        handover_points=[
            PlanHandoverPoint(
                robot_id="R10025",
                node_id="R4_0",
                handover_at_ms=92_700,
                reason="Finish the current edge.",
                handover_policy="NEXT_NODE",
                locked_task_ids=[],
                carrying_load=False,
            )
        ],
        robot_overrides=[
            RobotRuntimeOverride(
                robot_id="R10025",
                current_node="R4_0",
                status="idle",
                current_load_units=0,
                clear_active_work=True,
                sim_time_ms=92_700,
            )
        ],
    )
    explicit = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R10025",
                current_node="R4_1",
                status="low_battery",
                battery_pct=20,
                current_load_units=1,
                active_task_id="837",
                sim_time_ms=89_702,
            )
        ],
        planning_horizon_start_ms=89_702,
        relocate_idle_robot_ids=["R10025"],
    )

    merged = RollingHorizonReplanService._merge_runtime_overrides(
        snapshot,
        explicit,
        activation_at_ms=118_437,
    )
    robot = merged.robot_states[0]

    assert robot.current_node == "R4_0"
    assert robot.status == "low_battery"
    assert robot.battery_pct == 20
    assert robot.current_load_units == 0
    assert robot.active_task_id is None
    assert robot.clear_active_work is True
    assert robot.sim_time_ms == 118_437


def test_normal_robot_uses_projected_handover_state_not_stale_active_work() -> None:
    snapshot = ReplanExecutionSnapshot(
        source_plan_id="PLAN-OLD",
        replan_at_sim_time_ms=2500,
        earliest_handover_at_ms=6000,
        latest_handover_at_ms=6000,
        robot_overrides=[
            RobotRuntimeOverride(
                robot_id="R001",
                current_node="DROP-END",
                status="idle",
                battery_pct=80,
                capacity_units=1,
                current_load_units=0,
                active_task_id=None,
                clear_active_work=True,
                sim_time_ms=6000,
            )
        ],
    )
    explicit = RuntimePlanningOverrides(
        robot_states=[
            RobotRuntimeOverride(
                robot_id="R001",
                current_node="EVENT-SAFE-NODE",
                status="idle",
                battery_pct=78,
                capacity_units=1,
                current_load_units=1,
                active_task_id="789",
                sim_time_ms=2500,
            )
        ]
    )

    merged = RollingHorizonReplanService._merge_runtime_overrides(
        snapshot,
        explicit,
        activation_at_ms=6000,
    )

    robot = merged.robot_states[0]
    assert robot.current_node == "DROP-END"
    assert robot.status == "idle"
    assert robot.battery_pct == 78
    assert robot.current_load_units == 0
    assert robot.active_task_id is None
    assert robot.clear_active_work is True
    assert robot.sim_time_ms == 6000

    context = RobotRuntimeContext(
        warehouse_id="WH-001",
        simulation_id="SIM-001",
        robots=[
            RobotRuntime(
                warehouse_id="WH-001",
                simulation_id="SIM-001",
                robot_id="R001",
                robot_code="R001",
                status="idle",
                battery_pct=78,
                capacity_units=1,
                current_node="EVENT-SAFE-NODE",
                active_task_id="789",
                active_mission_id="MISSION-OLD",
                load_state="LOADED",
                current_load_units=1,
                sim_time_ms=2500,
            )
        ],
        candidate_robot_ids=[],
        min_battery_pct=30,
        min_capacity_units=1,
        summary="Before handover projection.",
    )

    projected = apply_runtime_overrides(context, merged)

    projected_robot = projected.robots[0]
    assert projected_robot.current_node == "DROP-END"
    assert projected_robot.active_task_id is None
    assert projected_robot.active_mission_id is None
    assert projected_robot.current_load_units == 0
    assert projected_robot.load_state == "EMPTY"
    assert projected.candidate_robot_ids == ["R001"]

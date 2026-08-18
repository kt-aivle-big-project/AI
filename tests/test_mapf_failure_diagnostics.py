from __future__ import annotations

from itertools import permutations
from types import SimpleNamespace

from app.domain.schemas import (
    CuOptPayload,
    EdgeReservation,
    FleetData,
    MAPFValidationResult,
    MapConstraints,
    MapContext,
    OptimizerResult,
    OptimizerRoute,
    PlanHandoverPoint,
    ReplanExecutionSnapshot,
    RobotRuntime,
    RobotRuntimeContext,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    TerminalRelocationRecord,
    TerminalRelocationResult,
    TaskData,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
    WaypointRouteExpansionResult,
    WaypointGraphData,
)
from app.graph import v9_planning
from app.services.context_service import apply_runtime_overrides
from app.services.mapf_service import PrioritizedSIPPPlanner
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


def test_mapf_failure_diagnostics_reports_the_robot_that_actually_failed() -> None:
    state, schedule = _failed_low_battery_state()
    state["runtime_overrides"].robot_states.append(
        RobotRuntimeOverride(
            robot_id="R290",
            current_node="R3_3",
            status="idle",
            battery_pct=85,
            current_load_units=0,
            sim_time_ms=29_385,
        )
    )
    validation = MAPFValidationResult(
        valid=False,
        errors=[
            "No safe ordered-goal path for R290: "
            "R3_3 -> ['R3_2', 'R3_10', 'C02']."
        ],
    )

    diagnostics = v9_planning._mapf_failure_diagnostics(
        state, schedule, validation
    )
    message = v9_planning._mapf_operator_message(diagnostics)

    assert diagnostics["failed_robot_ids"] == ["R290"]
    assert diagnostics["low_battery_robots"][0]["robot_id"] == "R248"
    assert message.startswith("로봇 R290의 충돌 없는 실행 경로")
    assert "배터리 부족 로봇 R248" not in message


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


def _priority_conflict_problem() -> tuple[
    CuOptPayload,
    OptimizerResult,
    MapContext,
    dict[str, str],
]:
    nodes = ["A", "B", "C", "D", "E", "F", "G", "C04"]
    indices = {node: index for index, node in enumerate(nodes)}
    edges = [
        ("E-A-B", "A", "B"),
        ("E-B-C", "B", "C"),
        ("E-C-D", "C", "D"),
        ("E-E-C", "E", "C"),
        ("E-C-B", "C", "B"),
        ("E-B-F", "B", "F"),
        ("E-G-C04", "G", "C04"),
    ]
    payload = CuOptPayload(
        snapshot_id="SNAP-MAPF-PRIORITY",
        location_index_map=indices,
        fleet_data=FleetData(
            vehicle_ids=["R289", "R290", "R292"],
            vehicle_start_locations=[indices["A"], indices["E"], indices["G"]],
            vehicle_end_locations=[indices["A"], indices["E"], indices["C04"]],
            capacities=[1, 1, 1],
            vehicle_available_at_ms=[0, 0, 0],
            skip_first_trips=[False, False, False],
            drop_return_trips=[False, False, False],
        ),
        task_data=TaskData(
            task_ids=[
                "TASK-A_DROP",
                "TASK-B_DROP",
                "TERMINAL-R292-CHARGE",
            ],
            task_locations=[indices["D"], indices["F"], indices["C04"]],
            pickup_and_delivery_pairs=[],
            demand=[0, 0, 0],
            priorities=[10, 10, 10],
            service_times_ms=[1_000, 1_000, 96_000],
            fixed_vehicle_ids=["R289", "R290", "R292"],
        ),
        waypoint_graph_data=WaypointGraphData(
            edge_ids=[edge_id for edge_id, _, _ in edges],
            from_indices=[indices[source] for _, source, _ in edges],
            to_indices=[indices[target] for _, _, target in edges],
            costs=[1.0 for _ in edges],
            travel_times_ms=[1_000 for _ in edges],
        ),
        applied_map_constraints=MapConstraints(),
    )
    result = OptimizerResult(
        backend="cuopt",
        status="success",
        optimizer="cuopt",
        routes=[
            OptimizerRoute(vehicle_id="R289", task_sequence=["TASK-A_DROP"]),
            OptimizerRoute(vehicle_id="R290", task_sequence=["TASK-B_DROP"]),
            OptimizerRoute(
                vehicle_id="R292",
                task_sequence=["TERMINAL-R292-CHARGE"],
            ),
        ],
    )
    map_context = MapContext(
        graph_version="MAP-MAPF-PRIORITY",
        node_count=len(nodes),
        edge_count=len(edges),
        map_constraints=MapConstraints(),
        summary="Two routes need the same corridor in opposite directions.",
    )
    node_types = {
        "A": "route",
        "B": "route",
        "C": "rack",
        "D": "route",
        "E": "rack",
        "F": "route",
        "G": "route",
        "C04": "charging_slot",
    }
    return payload, result, map_context, node_types


def test_mapf_retries_with_blocked_robot_first_and_keeps_charge_route() -> None:
    payload, result, map_context, node_types = _priority_conflict_problem()
    planner = PrioritizedSIPPPlanner()

    first_expansion, first_schedule = planner.plan(
        payload=payload,
        result=result,
        map_context=map_context,
        node_types=node_types,
        _allow_priority_retry=False,
    )

    assert first_expansion.status == "failed"
    assert first_schedule.valid is False
    assert any("R290" in error for error in first_expansion.errors)

    recovered_expansion, recovered_schedule = planner.plan(
        payload=payload,
        result=result,
        map_context=map_context,
        node_types=node_types,
    )

    assert recovered_expansion.status == "expanded"
    assert recovered_schedule.valid is True
    assert {route.robot_id for route in recovered_schedule.routes} == {
        "R289",
        "R290",
        "R292",
    }
    charge_route = next(
        route for route in recovered_schedule.routes if route.robot_id == "R292"
    )
    assert charge_route.steps[-1].service_kind == "CHARGE"
    assert charge_route.steps[-1].node_id == "C04"
    assert charge_route.steps[-1].task_id == "TERMINAL-R292-CHARGE"
    assert any(
        warning.startswith(
            "MAPF recovered with alternate robot priority: R290>R289>R292 "
        )
        for warning in recovered_schedule.warnings
    )


def test_five_robot_retry_search_covers_every_priority_order() -> None:
    default_order = ("R295", "R296", "R297", "R298", "R299")

    retry_orders = PrioritizedSIPPPlanner._priority_retry_orders(
        default_order,
        ["R299"],
    )

    assert len(retry_orders) == 119
    assert default_order not in retry_orders
    assert set(retry_orders) == set(permutations(default_order)) - {
        default_order
    }
    assert retry_orders[0][0] == "R299"


def test_large_fleet_retry_search_stays_within_the_time_budget() -> None:
    default_order = tuple(f"R{index:03d}" for index in range(8))

    retry_orders = PrioritizedSIPPPlanner._priority_retry_orders(
        default_order,
        ["R007"],
    )

    assert len(retry_orders) == PrioritizedSIPPPlanner.MAX_PRIORITY_RETRY_ORDERS
    assert len(set(retry_orders)) == len(retry_orders)
    assert default_order not in retry_orders
    assert retry_orders[0][0] == "R007"


def test_planner_reaches_a_non_heuristic_five_robot_priority(monkeypatch) -> None:
    robot_ids = [f"R{index:03d}" for index in range(1, 6)]
    location_index = {
        f"S{index}": index - 1 for index in range(1, 6)
    }
    payload = CuOptPayload(
        snapshot_id="SNAP-FIVE-ROBOT-RETRY",
        location_index_map=location_index,
        fleet_data=FleetData(
            vehicle_ids=robot_ids,
            vehicle_start_locations=list(range(5)),
            vehicle_end_locations=list(range(5)),
            capacities=[1] * 5,
            vehicle_available_at_ms=[0] * 5,
            skip_first_trips=[False] * 5,
            drop_return_trips=[True] * 5,
        ),
        task_data=TaskData(
            task_ids=[],
            task_locations=[],
            pickup_and_delivery_pairs=[],
            demand=[],
            priorities=[],
            service_times_ms=[],
            fixed_vehicle_ids=[],
        ),
        waypoint_graph_data=WaypointGraphData(
            edge_ids=[],
            from_indices=[],
            to_indices=[],
            costs=[],
            travel_times_ms=[],
        ),
        applied_map_constraints=MapConstraints(),
    )
    result = OptimizerResult(
        backend="cuopt",
        status="success",
        optimizer="cuopt",
        routes=[
            OptimizerRoute(vehicle_id=robot_id, task_sequence=[])
            for robot_id in robot_ids
        ],
    )
    map_context = MapContext(
        graph_version="MAP-FIVE-ROBOT-RETRY",
        node_count=5,
        edge_count=0,
        map_constraints=MapConstraints(),
        summary="Synthetic priority-order recovery contract.",
    )
    planner = PrioritizedSIPPPlanner()
    target_order = ("R003", "R005", "R002", "R001", "R004")
    current_prefix: list[str] = []
    reset_before_next_call = False

    def fake_plan_robot_route(**kwargs):
        nonlocal current_prefix, reset_before_next_call
        if reset_before_next_call:
            current_prefix = []
            reset_before_next_call = False
        robot_id = kwargs["robot_id"]
        current_prefix.append(robot_id)
        if tuple(current_prefix) != target_order[: len(current_prefix)]:
            reset_before_next_call = True
            return None, f"Synthetic priority conflict for {robot_id}."
        return (
            SimpleNamespace(
                steps=[],
                reservations=[],
                segments=[],
                node_sequence=[kwargs["start_node"]],
                finish_at_ms=0,
                edge_calendar=kwargs["edge_calendar"],
            ),
            None,
        )

    monkeypatch.setattr(planner, "_plan_robot_route", fake_plan_robot_route)

    expansion, schedule = planner.plan(
        payload=payload,
        result=result,
        map_context=map_context,
        node_types={node_id: "route" for node_id in location_index},
    )

    assert expansion.status == "expanded"
    assert schedule.valid is True
    assert [route.robot_id for route in schedule.routes] == list(target_order)
    assert any(
        warning.startswith(
            "MAPF recovered with alternate robot priority: "
            + ">".join(target_order)
        )
        for warning in schedule.warnings
    )


def test_planner_diagnostics_keep_priority_retry_summary(monkeypatch) -> None:
    state, _ = _failed_low_battery_state()
    expansion = WaypointRouteExpansionResult(
        status="failed",
        errors=[
            "No safe ordered-goal path for R299: "
            "R3_3 -> ['N161', 'R5_3', 'N164', 'R5_4', 'C03']."
        ],
    )
    retry_warning = (
        "MAPF alternate-priority retries exhausted: attempts=119, robots=5, "
        "last_failures=R299>R295>R296>R297>R298=>R299"
    )
    schedule = TrafficScheduleResult(
        valid=False,
        conflicts=list(expansion.errors),
        warnings=[retry_warning],
    )
    state["runtime_overrides"].robot_states.append(
        RobotRuntimeOverride(
            robot_id="R299",
            current_node="R3_3",
            status="idle",
            battery_pct=85,
            current_load_units=0,
            sim_time_ms=23_096,
        )
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

    assert update["errors"][0].message.startswith(
        "로봇 R299의 충돌 없는 실행 경로"
    )
    assert retry_warning in update["traffic_schedule"].warnings
    assert len(output) == 1
    assert '"warnings": ["MAPF alternate-priority retries exhausted:' in output[0]

from datetime import UTC, datetime, timedelta

from app.models import (
    AtomicTask,
    CollisionFreePlan,
    CommandInterpretation,
    CuOptPlan,
    TimedRoute,
    TimedWaypoint,
    ScheduledTask,
    TaskDependency,
    TaskScheduleConstraint,
)
from app.planning.graph import after_select_tasks
from app.planning.nodes import (
    _reconcile_routing_schedule,
    planning_report_data,
    select_required_tasks_node,
    tasks_from_work,
)
from app.services.command_language import parse_deterministic_command
from app.services.conversation import apply_conversation_inheritance
from app.services.local_optimizer import LocalOptimizer
from app.services.schedule_dispatcher import ready_only_plan_payload
from app.services.scheduler_tick import SchedulerTickService
from app.services.scheduling import (
    parse_planning_reference_time,
    parse_schedule_language,
    relative_time_step,
    scope_dependency_graph,
    validate_dependency_graph,
)
from app.services.simulation import simulate_plan


REFERENCE = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)


def test_planning_reference_parser_supports_relative_and_absolute_clocks() -> None:
    base = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    cases = {
        "오늘 오전 7시를 기준으로 계획해줘": "2026-07-22T22:00:00+00:00",
        "내일 오전 7시 15분을 기준으로 계획해줘": "2026-07-23T22:15:00+00:00",
        "모레 오전 12시를 기준으로 계획해줘": "2026-07-24T15:00:00+00:00",
        "2026년 7월 24일 오전 7시 15분 시점 기준 계획해줘": "2026-07-23T22:15:00+00:00",
        "2026-07-24 07:15 기준 계획해줘": "2026-07-23T22:15:00+00:00",
    }
    for text, expected in cases.items():
        result, errors = parse_planning_reference_time(
            text, reference_time=base, warehouse_timezone="Asia/Seoul"
        )
        assert errors == []
        assert result is not None
        assert result.utc_at.isoformat() == expected
        assert result.timezone == "Asia/Seoul"


def test_work_task_preserves_business_deadline_instead_of_previous_schedule_end() -> None:
    business_deadline = REFERENCE - timedelta(minutes=30)
    task = tasks_from_work(
        {
            "work_id": "W-DEADLINE",
            "source_node": 1,
            "target_node": 2,
            "scheduled_end": REFERENCE + timedelta(hours=1),
        },
        frozen=False,
        business_deadline=business_deadline,
    )[0]

    assert task.deadline == business_deadline


def test_deterministic_command_keeps_user_planning_reference() -> None:
    base = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    result = parse_deterministic_command(
        "내일 오후 12시를 기준으로 미완료 출고 작업을 가상 시뮬레이션해줘",
        reference_time=base,
        warehouse_timezone="Asia/Seoul",
    )

    assert result.planning_reference is not None
    assert result.planning_reference.utc_at.isoformat() == "2026-07-24T03:00:00+00:00"
    assert result.planning_reference.source == "USER_COMMAND"
    assert result.daily_schedule_requested


def test_work_window_does_not_become_a_planning_reference() -> None:
    base = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    text = "내일 오전 9시부터 10시까지 W-001 작업을 처리해줘"

    planning_reference, errors = parse_planning_reference_time(
        text,
        reference_time=base,
        warehouse_timezone="Asia/Seoul",
    )
    interpretation = parse_deterministic_command(
        text,
        reference_time=base,
        warehouse_timezone="Asia/Seoul",
    )

    assert planning_reference is None
    assert errors == []
    assert interpretation.planning_reference is None
    assert interpretation.scheduled_task_constraints[0].work_id == "W-001"
    assert interpretation.scheduled_task_constraints[0].time_constraint_type == "HARD_WINDOW"


def test_work_start_constraint_does_not_become_a_planning_reference() -> None:
    base = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    text = "내일 오전 9시부터 W-001 작업을 처리해줘"

    interpretation = parse_deterministic_command(
        text,
        reference_time=base,
        warehouse_timezone="Asia/Seoul",
    )

    constraint = interpretation.scheduled_task_constraints[0]
    assert interpretation.planning_reference is None
    assert constraint.work_id == "W-001"
    assert constraint.earliest_start == datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    assert constraint.latest_finish is None


def schedule_problem(tasks: list[AtomicTask]) -> dict:
    return {
        "warehouse_id": 1,
        "captured_at": REFERENCE.isoformat(),
        "reference_time": REFERENCE.isoformat(),
        "plan_mode": "INITIAL_PLAN",
        "nodes": [{"node_id": value, "active": True} for value in (1, 2, 3)],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 1,
                "travel_seconds": 5,
                "direction": "BOTH",
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 1,
                "travel_seconds": 5,
                "direction": "BOTH",
            },
        ],
        "robots": [
            {"robot_id": "R1", "node_id": 1, "battery": 100, "status": "IDLE"},
            {"robot_id": "R2", "node_id": 3, "battery": 100, "status": "IDLE"},
        ],
        "tasks": [row.model_dump(mode="json") for row in tasks],
        "inventory": [],
        "temporary_closures": [],
        "active_plan": None,
        "fixed_task_ids": [],
        "changeable_task_ids": [],
        "affected_robot_ids": [],
        "weights": {},
    }


def optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
    )


def test_daily_command_parses_windows_and_dependency() -> None:
    result = parse_schedule_language(
        "오늘 오전 9시부터 10시까지 W-001 작업을 처리하고, "
        "완료하면 W-002 작업을 처리해줘. "
        "오후 1시부터 2시까지 W-003 작업을 실행해줘.",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )

    assert [row.work_id for row in result.constraints] == ["W-001", "W-003"]
    assert result.constraints[0].earliest_start == REFERENCE
    assert result.constraints[0].latest_finish == REFERENCE + timedelta(hours=1)
    assert [(row.predecessor_work_id, row.successor_work_id) for row in result.dependencies] == [
        ("W-001", "W-002")
    ]


def test_command_interpretation_exposes_daily_schedule() -> None:
    result = parse_deterministic_command(
        "W-001 완료하면 W-002 작업을 계획하고 시뮬레이션해줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )

    assert result.intent == "DAILY_PLAN"
    assert result.daily_schedule_requested
    assert result.target_task_ids == ["W-001", "W-002"]
    assert result.task_dependencies[0].successor_work_id == "W-002"


def test_dependency_cycle_is_detected_before_optimizer() -> None:
    dependencies = [
        TaskDependency(predecessor_work_id="W-001", successor_work_id="W-002"),
        TaskDependency(predecessor_work_id="W-002", successor_work_id="W-001"),
    ]
    order, errors = validate_dependency_graph(dependencies, ["W-001", "W-002"])

    assert order == []
    assert errors[0].startswith("CYCLIC_TASK_DEPENDENCY")


def test_hard_window_is_enforced_by_optimizer() -> None:
    task = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[3],
        earliest_start=REFERENCE,
        latest_finish=REFERENCE + timedelta(seconds=5),
        time_constraint_type="HARD_WINDOW",
    )
    service = optimizer()
    plan = service.optimize(schedule_problem([task]))

    assert plan.unassigned_task_ids == ["W-001:move"]
    reasons = {
        candidate.rejection_reason
        for row in service.last_optimization_evidence
        for candidate in row.candidates
    }
    assert plan.scheduled_tasks == []
    assert "HARD_WINDOW_VIOLATION" in reasons


def test_dependency_lag_delays_successor() -> None:
    dependency = TaskDependency(
        predecessor_work_id="W-001",
        successor_work_id="W-002",
        lag_seconds=10,
    )
    first = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[2],
    )
    second = AtomicTask(
        task_id="W-002:move",
        work_id="W-002",
        action="MOVE",
        source_candidates=[2],
        target_candidates=[3],
        predecessors=["W-001:move"],
        dependencies=[dependency],
    )
    plan = optimizer().optimize(schedule_problem([first, second]))
    by_id = {row.task_id: row for row in plan.scheduled_tasks}

    assert by_id["W-002:move"].start_time_step >= by_id["W-001:move"].end_time_step + 2


def test_independent_tasks_can_run_in_parallel() -> None:
    tasks = [
        AtomicTask(
            task_id="W-001:move",
            work_id="W-001",
            action="MOVE",
            source_candidates=[1],
            target_candidates=[2],
        ),
        AtomicTask(
            task_id="W-002:move",
            work_id="W-002",
            action="MOVE",
            source_candidates=[3],
            target_candidates=[2],
        ),
    ]
    plan = optimizer().optimize(schedule_problem(tasks))

    assert {row.start_time_step for row in plan.scheduled_tasks} == {0}
    assert len({row.robot_id for row in plan.scheduled_tasks}) == 2


def test_same_robot_group_forces_same_assignment() -> None:
    tasks = [
        AtomicTask(
            task_id=f"W-00{index}:move",
            work_id=f"W-00{index}",
            action="MOVE",
            source_candidates=[1 if index == 1 else 3],
            target_candidates=[2],
            same_robot_group="G1",
        )
        for index in (1, 2)
    ]
    plan = optimizer().optimize(schedule_problem(tasks))

    assert len({row.robot_id for row in plan.scheduled_tasks}) == 1


def test_simulation_rejects_hard_window_violation() -> None:
    task = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[2],
        latest_finish=REFERENCE + timedelta(seconds=5),
        time_constraint_type="HARD_WINDOW",
    )
    scheduled = ScheduledTask(
        task_id=task.task_id,
        work_id=task.work_id,
        robot_id="R1",
        source_node=1,
        target_node=2,
        start_time_step=0,
        end_time_step=2,
    )
    route = TimedRoute(
        robot_id="R1",
        task_ids=[task.task_id],
        waypoints=[
            TimedWaypoint(node_id=1, time_step=0, action="MOVE"),
            TimedWaypoint(node_id=2, time_step=2, action="MOVE"),
        ],
        distance=1,
    )
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        time_step_seconds=5,
        routes=[route],
        total_distance=1,
    )
    result = simulate_plan(
        collision,
        CuOptPlan(scheduled_tasks=[scheduled], objective_value=1),
        schedule_problem([task]),
    )

    assert not result.valid
    assert "HARD_WINDOW_VIOLATION" in {issue.code for issue in result.issues}


def test_routing_completion_reconciles_operational_schedule_times() -> None:
    task = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[3],
        deadline=REFERENCE - timedelta(seconds=10),
    )
    scheduled = ScheduledTask(
        task_id="W-001:move",
        work_id="W-001",
        robot_id="R1",
        source_node=1,
        target_node=3,
        start_time_step=0,
        end_time_step=8,
        planned_end_at=REFERENCE + timedelta(seconds=40),
    )
    optimizer_plan = CuOptPlan(scheduled_tasks=[scheduled], objective_value=1)
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        time_step_seconds=5,
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=[scheduled.task_id],
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0, action="MOVE"),
                    TimedWaypoint(node_id=3, time_step=24, action="MOVE"),
                ],
                distance=32,
            )
        ],
        total_distance=32,
        metadata={"task_completion_steps": {scheduled.task_id: 24}},
    )

    operational_plan, evidence = _reconcile_routing_schedule(
        optimizer_plan, collision, schedule_problem([task])
    )
    final_task = operational_plan.scheduled_tasks[0]
    simulation = simulate_plan(collision, operational_plan, schedule_problem([]))
    report = planning_report_data(
        {
            "optimization_problem": schedule_problem([]),
            "cuopt_plan": operational_plan.model_dump(mode="json"),
            "collision_plan": collision.model_dump(mode="json"),
            "simulation": {},
            "plan_validation": {},
            "validation": {},
            "warnings": [],
            "errors": [],
            "verification_decision": {},
        }
    )

    assert final_task.end_time_step == 24
    assert final_task.planned_end_at == REFERENCE + timedelta(seconds=120)
    assert operational_plan.metadata["optimizer_estimated_end_time_steps"] == {
        "W-001:move": 8
    }
    assert operational_plan.metadata["tardiness_time_steps"] == 26
    assert evidence["routing_end_time_steps"] == {"W-001:move": 24}
    assert report["daily_schedule"][0]["end_time_step"] == 24
    assert report["daily_schedule"][0]["planned_end_at"] == (
        REFERENCE + timedelta(seconds=120)
    ).isoformat()
    assert simulation.metrics["schedule_completion_at"] == (
        REFERENCE + timedelta(seconds=120)
    ).isoformat()
    assert simulation.metrics["active_work_duration_seconds"] == 120
    assert simulation.metrics["elapsed_until_completion_seconds"] == 120


def test_simulation_rejects_route_schedule_time_mismatch() -> None:
    scheduled = ScheduledTask(
        task_id="W-001:move",
        work_id="W-001",
        robot_id="R1",
        source_node=1,
        target_node=2,
        start_time_step=0,
        end_time_step=8,
        planned_end_at=REFERENCE + timedelta(seconds=40),
    )
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        time_step_seconds=5,
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=[scheduled.task_id],
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0, action="MOVE"),
                    TimedWaypoint(node_id=2, time_step=24, action="MOVE"),
                ],
                distance=32,
            )
        ],
        total_distance=32,
        metadata={"task_completion_steps": {scheduled.task_id: 24}},
    )

    result = simulate_plan(
        collision,
        CuOptPlan(scheduled_tasks=[scheduled], objective_value=1),
        schedule_problem([]),
    )

    assert not result.valid
    assert {
        "ROUTE_SCHEDULE_TIME_MISMATCH",
        "ROUTE_COMPLETION_TIME_MISMATCH",
    }.issubset({issue.code for issue in result.issues})


def test_timezone_equivalent_instants_have_same_step() -> None:
    utc_value = datetime(2026, 7, 22, 1, tzinfo=UTC)
    seoul_value = datetime.fromisoformat("2026-07-22T10:00:00+09:00")

    assert relative_time_step(utc_value, REFERENCE, 5, round_up=True) == relative_time_step(
        seoul_value, REFERENCE, 5, round_up=True
    )


def test_scheduler_tick_releases_future_task_without_polling_loop() -> None:
    plan = {
        "activated_at": REFERENCE.isoformat(),
        "collision_plan": {"time_step_seconds": 5},
        "cuopt_plan": {
            "scheduled_tasks": [
                ScheduledTask(
                    task_id="W-001:move",
                    work_id="W-001",
                    robot_id="R1",
                    source_node=1,
                    target_node=2,
                    start_time_step=12,
                    end_time_step=13,
                ).model_dump(mode="json")
            ]
        },
        "task_dependencies": [],
    }

    before = SchedulerTickService.evaluate(
        plan, now=REFERENCE + timedelta(seconds=55), completed_work_ids=[]
    )
    after = SchedulerTickService.evaluate(
        plan, now=REFERENCE + timedelta(seconds=60), completed_work_ids=[]
    )
    assert before["ready_task_ids"] == []
    assert after["ready_task_ids"] == ["W-001:move"]


def test_gateway_payload_contains_only_ready_tasks() -> None:
    plan = {
        "required_tasks": [{"task_id": "T1"}, {"task_id": "T2"}],
        "cuopt_plan": {
            "scheduled_tasks": [
                {"task_id": "T1", "robot_id": "R1", "end_time_step": 2},
                {"task_id": "T2", "robot_id": "R2", "end_time_step": 8},
            ]
        },
        "collision_plan": {
            "routes": [
                {"robot_id": "R1", "task_ids": ["T1"], "waypoints": []},
                {"robot_id": "R2", "task_ids": ["T2"], "waypoints": []},
            ]
        },
    }
    payload = ready_only_plan_payload(plan, ["T1"])

    assert [row["task_id"] for row in payload["required_tasks"]] == ["T1"]
    assert [row["robot_id"] for row in payload["collision_plan"]["routes"]] == ["R1"]


def test_schedule_constraints_are_inherited_in_same_conversation() -> None:
    previous = CommandInterpretation(
        command_kind="PLAN",
        intent="DAILY_PLAN",
        objective="initial",
        execution_mode="PLAN_ONLY",
        scheduled_task_constraints=[
            TaskScheduleConstraint(
                work_id="W-001",
                earliest_start=REFERENCE,
                latest_finish=REFERENCE + timedelta(hours=1),
                time_constraint_type="HARD_WINDOW",
            )
        ],
        task_dependencies=[
            TaskDependency(
                predecessor_work_id="W-001", successor_work_id="W-002"
            )
        ],
        daily_schedule_requested=True,
        summary="initial",
    )
    current = CommandInterpretation(
        command_kind="PLAN",
        intent="INSERT_TASK",
        objective="그 일정 그대로 두고 W-004만 지금 먼저 넣어줘",
        execution_mode="PLAN_ONLY",
        target_task_ids=["W-004"],
        summary="followup",
    )
    inherited = {
        "scheduled_task_constraints": [
            row.model_dump(mode="json")
            for row in previous.scheduled_task_constraints
        ],
        "task_dependencies": [
            row.model_dump(mode="json") for row in previous.task_dependencies
        ],
        "daily_schedule_requested": True,
    }

    resolved, applied, _ = apply_conversation_inheritance(
        current,
        inherited,
        active_plan_version="P1",
        active_simulation_id=None,
    )
    assert resolved.scheduled_task_constraints[0].work_id == "W-001"
    assert resolved.task_dependencies[0].successor_work_id == "W-002"
    assert "task_dependencies" in applied


def test_urgent_language_uses_insert_policy_without_preemption() -> None:
    result = parse_deterministic_command(
        "급하게 W-004를 지금 먼저 처리해줘. 진행 중인 작업은 중단하지 말아줘.",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert result.intent == "INSERT_TASK"
    assert result.insertion_policy == "URGENT"
    assert result.preemption_policy == "NON_PREEMPTIVE"
    assert result.priority == "EMERGENCY"


def test_explicit_preemption_requires_safe_stop_clarification() -> None:
    result = parse_deterministic_command(
        "W-001을 중단하고 W-004를 실행해줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert result.preemption_policy == "REQUIRE_SAFE_STOP_CONFIRMATION"
    assert "safe_stop_confirmation" in result.missing_information


def test_default_timezone_is_explicitly_reported() -> None:
    result = parse_schedule_language(
        "오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=REFERENCE,
        warehouse_timezone=None,
    )
    assert result.timezone_name == "Asia/Seoul"
    assert result.timezone_defaulted
    assert result.warnings == ["DEFAULT_WAREHOUSE_TIMEZONE_USED"]


def test_same_robot_language_creates_typed_group() -> None:
    result = parse_schedule_language(
        "W-001과 W-002를 같은 로봇으로 처리해줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert result.same_robot_groups[0].work_ids == ["W-001", "W-002"]
    assert {row.same_robot_group for row in result.constraints} == {
        "COMMAND_SAME_ROBOT_1"
    }


def test_parallel_language_does_not_create_false_dependency() -> None:
    result = parse_schedule_language(
        "W-001과 W-002를 가능한 경우 동시에 병렬 처리해줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert result.daily_schedule_requested
    assert result.dependencies == []


def test_deadline_is_soft_and_contributes_tardiness() -> None:
    task = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[3],
        deadline=REFERENCE + timedelta(seconds=5),
        latest_finish=REFERENCE + timedelta(seconds=5),
        time_constraint_type="DEADLINE",
    )
    service = optimizer()
    plan = service.optimize(schedule_problem([task]))
    assert plan.unassigned_task_ids == []
    assert plan.metadata["tardiness_time_steps"] == 1


def test_future_earliest_start_is_not_ready() -> None:
    task = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[2],
        earliest_start=REFERENCE + timedelta(minutes=5),
        time_constraint_type="ASAP",
    )
    plan = optimizer().optimize(schedule_problem([task]))
    assert plan.scheduled_tasks[0].start_time_step == 60
    assert plan.scheduled_tasks[0].schedule_status == "SCHEDULED"


def test_ready_task_waits_for_completed_predecessor() -> None:
    active_plan = {
        "activated_at": REFERENCE.isoformat(),
        "collision_plan": {"time_step_seconds": 5},
        "cuopt_plan": {
            "scheduled_tasks": [
                ScheduledTask(
                    task_id="W-002:move",
                    work_id="W-002",
                    robot_id="R1",
                    source_node=1,
                    target_node=2,
                    start_time_step=0,
                    end_time_step=1,
                ).model_dump(mode="json")
            ]
        },
        "task_dependencies": [
            TaskDependency(
                predecessor_work_id="W-001", successor_work_id="W-002"
            ).model_dump(mode="json")
        ],
    }
    waiting = SchedulerTickService.evaluate(
        active_plan, now=REFERENCE, completed_work_ids=[]
    )
    ready = SchedulerTickService.evaluate(
        active_plan, now=REFERENCE, completed_work_ids=["W-001"]
    )
    assert waiting["ready_task_ids"] == []
    assert ready["ready_task_ids"] == ["W-002:move"]


def test_ready_payload_truncates_future_waypoints() -> None:
    payload = ready_only_plan_payload(
        {
            "required_tasks": [{"task_id": "T1"}, {"task_id": "T2"}],
            "cuopt_plan": {
                "scheduled_tasks": [
                    {"task_id": "T1", "robot_id": "R1", "end_time_step": 2},
                    {"task_id": "T2", "robot_id": "R1", "end_time_step": 8},
                ]
            },
            "collision_plan": {
                "routes": [
                    {
                        "robot_id": "R1",
                        "task_ids": ["T1", "T2"],
                        "waypoints": [
                            {"node_id": 1, "time_step": 0},
                            {"node_id": 2, "time_step": 2},
                            {"node_id": 3, "time_step": 8},
                        ],
                    }
                ]
            },
        },
        ["T1"],
    )
    route = payload["collision_plan"]["routes"][0]
    assert route["task_ids"] == ["T1"]
    assert [row["time_step"] for row in route["waypoints"]] == [0, 2]


def test_insert_replan_preserves_frozen_and_can_change_future() -> None:
    problem = schedule_problem(
        [
            AtomicTask(
                task_id="T1",
                action="MOVE",
                source_candidates=[1],
                target_candidates=[2],
                frozen=True,
                assigned_robot_id="R1",
            ),
            AtomicTask(
                task_id="T2",
                action="MOVE",
                source_candidates=[2],
                target_candidates=[3],
            ),
            AtomicTask(
                task_id="T3",
                action="MOVE",
                source_candidates=[3],
                target_candidates=[2],
                priority=1,
            ),
        ]
    )
    problem["plan_mode"] = "INSERT_TASK"
    problem["changeable_task_ids"] = ["T2", "T3"]
    problem["fixed_task_ids"] = ["T1"]
    problem["active_plan"] = {
        "cuopt_plan": CuOptPlan(
            scheduled_tasks=[
                ScheduledTask(
                    task_id="T1",
                    robot_id="R1",
                    source_node=1,
                    target_node=2,
                    start_time_step=0,
                    end_time_step=1,
                ),
                ScheduledTask(
                    task_id="T2",
                    robot_id="R1",
                    source_node=2,
                    target_node=3,
                    start_time_step=10,
                    end_time_step=11,
                ),
            ],
            objective_value=1,
        ).model_dump(mode="json")
    }
    plan = optimizer().optimize(problem)
    by_id = {row.task_id: row for row in plan.scheduled_tasks}
    assert by_id["T1"].start_time_step == 0
    assert "T1" in plan.metadata["preserved_task_ids"]
    assert set(by_id) == {"T1", "T2", "T3"}


def test_invalid_dependency_graph_stops_before_optimizer() -> None:
    assert after_select_tasks(
        {"schedule_validation": {"valid": False}}
    ) == "report"
    assert after_select_tasks(
        {"schedule_validation": {"valid": True}}
    ) == "build_problem"


def test_each_predecessor_uses_its_own_lag() -> None:
    first = AtomicTask(
        task_id="W-001:move",
        work_id="W-001",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[2],
    )
    second = AtomicTask(
        task_id="W-002:move",
        work_id="W-002",
        action="MOVE",
        source_candidates=[1],
        target_candidates=[1],
    )
    successor = AtomicTask(
        task_id="W-003:move",
        work_id="W-003",
        action="MOVE",
        source_candidates=[2],
        target_candidates=[3],
        predecessors=["W-001:move", "W-002:move"],
        dependencies=[
            TaskDependency(
                predecessor_work_id="W-001",
                successor_work_id="W-003",
                lag_seconds=0,
            ),
            TaskDependency(
                predecessor_work_id="W-002",
                successor_work_id="W-003",
                lag_seconds=10,
            ),
        ],
    )
    plan = optimizer().optimize(schedule_problem([first, second, successor]))
    by_id = {row.task_id: row for row in plan.scheduled_tasks}
    expected = max(
        by_id["W-001:move"].end_time_step,
        by_id["W-002:move"].end_time_step + 2,
    )
    assert by_id["W-003:move"].start_time_step == expected


def test_three_work_arrow_sequence_creates_two_dependencies() -> None:
    result = parse_schedule_language(
        "W-001 → W-002 → W-003 순서로 처리해줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert [
        (row.predecessor_work_id, row.successor_work_id)
        for row in result.dependencies
    ] == [("W-001", "W-002"), ("W-002", "W-003")]


def test_explicit_until_time_is_deadline_not_hard_window() -> None:
    result = parse_schedule_language(
        "W-001 작업을 오전 10시까지 끝내줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert len(result.constraints) == 1
    assert result.constraints[0].time_constraint_type == "DEADLINE"
    assert result.constraints[0].earliest_start is None


def test_insert_task_selection_keeps_active_plan_work() -> None:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="INSERT_TASK",
        objective="W-004를 지금 먼저 처리해줘",
        target_task_ids=["W-004"],
        execution_mode="PLAN_ONLY",
        insertion_policy="URGENT",
        summary="urgent insertion",
    )
    active_schedule = [
        ScheduledTask(
            task_id=f"{work_id}:move",
            work_id=work_id,
            robot_id="R1",
            source_node=1,
            target_node=2,
            start_time_step=index,
            end_time_step=index + 1,
        ).model_dump(mode="json")
        for index, work_id in enumerate(("W-001", "W-002"))
    ]
    result = select_required_tasks_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "scope": {
                "plan_mode": "INSERT_TASK",
                "fixed_task_ids": ["W-001:move"],
                "changeable_task_ids": ["W-002:move", "W-004:move"],
                "affected_robot_ids": [],
                "affected_task_ids": ["W-002:move", "W-004:move"],
                "freeze_horizon_seconds": 15,
                "include_new_command": True,
                "optimization_goal": "urgent",
                "reason_summary": "test",
            },
            "snapshot": {
                "sql": {
                    "works": [
                        {
                            "work_id": work_id,
                            "source_node": 1,
                            "target_node": 2,
                            "priority": 5,
                            "status": "EXECUTING" if work_id == "W-001" else "NEW",
                            "assigned_robot_id": "R1" if work_id == "W-001" else None,
                        }
                        for work_id in ("W-001", "W-002", "W-004")
                    ],
                    "work_dependencies": [],
                    "work_schedule_constraints": [],
                },
                "redis": {
                    "active_plan": {"cuopt_plan": {"scheduled_tasks": active_schedule}},
                    "robots": [],
                },
            },
            "command": {"command_id": "C-1"},
        }
    )
    assert {row["work_id"] for row in result["required_tasks"]} == {
        "W-001",
        "W-002",
        "W-004",
    }


def test_tomorrow_window_uses_warehouse_local_date_then_converts_to_utc() -> None:
    reference = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    result = parse_schedule_language(
        "내일 오전 9시부터 10시까지 W-001 작업을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    )
    constraint = result.constraints[0]
    assert constraint.earliest_start == datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    assert constraint.latest_finish == datetime(2026, 7, 23, 1, 0, tzinfo=UTC)


def test_today_and_tomorrow_use_different_local_dates() -> None:
    reference = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    today = parse_schedule_language(
        "오늘 오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    ).constraints[0]
    tomorrow = parse_schedule_language(
        "내일 오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    ).constraints[0]
    assert tomorrow.earliest_start - today.earliest_start == timedelta(days=1)


def test_day_after_tomorrow_is_one_day_after_tomorrow() -> None:
    reference = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    tomorrow = parse_schedule_language(
        "내일 오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    ).constraints[0]
    day_after = parse_schedule_language(
        "모레 오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    ).constraints[0]
    assert day_after.earliest_start - tomorrow.earliest_start == timedelta(days=1)


def test_this_week_and_next_week_monday_are_resolved_from_reference() -> None:
    reference = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    this_week = parse_schedule_language(
        "이번 주 월요일 오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    ).constraints[0]
    next_week = parse_schedule_language(
        "다음 주 월요일 오전 9시부터 10시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    ).constraints[0]
    assert this_week.earliest_start == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    assert next_week.earliest_start == datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


def test_next_day_phrase_uses_reference_date_plus_one() -> None:
    reference = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    result = parse_schedule_language(
        "다음 날 오후 1시부터 2시까지 W-001을 처리해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    )
    assert result.constraints[0].earliest_start == datetime(
        2026, 7, 23, 4, 0, tzinfo=UTC
    )


def test_korean_subject_and_object_particles_preserve_dependencies() -> None:
    for command in (
        "W-001이 완료되면 W-002를 처리해줘",
        "W-001을 완료하면 W-002를 처리해줘",
        "W-001이 끝난 다음 W-002를 처리해줘",
        "먼저 W-001, 그다음 W-002를 처리해줘",
    ):
        result = parse_schedule_language(
            command,
            reference_time=REFERENCE,
            warehouse_timezone="Asia/Seoul",
        )
        assert [
            (row.predecessor_work_id, row.successor_work_id)
            for row in result.dependencies
        ] == [("W-001", "W-002")]


def test_two_ids_without_explicit_order_do_not_create_dependency() -> None:
    result = parse_schedule_language(
        "W-001 하고 W-002 작업을 처리해줘",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert result.dependencies == []


def test_dependency_scope_excludes_unrelated_open_work() -> None:
    dependencies = [
        TaskDependency(
            predecessor_work_id="W-001", successor_work_id="W-002"
        )
    ]
    scoped, work_ids, warnings, errors = scope_dependency_graph(
        dependencies,
        seed_work_ids=["W-001", "W-002"],
        known_work_ids=["W-001", "W-002", "W-003"],
    )
    order, graph_errors = validate_dependency_graph(scoped, work_ids)
    assert order == ["W-001", "W-002"]
    assert "W-003" not in order
    assert warnings == []
    assert errors == graph_errors == []


def test_out_of_scope_persisted_dependency_is_ignored_with_warning() -> None:
    dependencies = [
        TaskDependency(
            predecessor_work_id="W-001", successor_work_id="W-002"
        ),
        TaskDependency(
            predecessor_work_id="W-003", successor_work_id="W-004"
        ),
    ]
    scoped, work_ids, warnings, errors = scope_dependency_graph(
        dependencies,
        seed_work_ids=["W-001", "W-002"],
        known_work_ids=["W-001", "W-002", "W-003", "W-004"],
    )
    assert [(row.predecessor_work_id, row.successor_work_id) for row in scoped] == [
        ("W-001", "W-002")
    ]
    assert work_ids == ["W-001", "W-002"]
    assert warnings == ["OUT_OF_SCOPE_DEPENDENCIES_IGNORED:1"]
    assert errors == []


def test_actual_daily_command_keeps_dependency_and_excludes_w003() -> None:
    reference = datetime(2026, 7, 22, 3, 53, 42, tzinfo=UTC)
    interpretation = parse_deterministic_command(
        "내일 오전 9시부터 10시까지 W-001 작업을 처리하고, "
        "W-001이 완료되면 W-002 작업을 처리해줘. "
        "전체 계획을 가상 시뮬레이션해줘",
        reference_time=reference,
        warehouse_timezone="Asia/Seoul",
    )
    selection = select_required_tasks_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "scope": {
                "plan_mode": "INITIAL_PLAN",
                "fixed_task_ids": [],
                "changeable_task_ids": [],
                "affected_robot_ids": [],
                "affected_task_ids": [],
                "freeze_horizon_seconds": 15,
                "include_new_command": True,
                "optimization_goal": "daily schedule",
                "reason_summary": "integration regression",
            },
            "snapshot": {
                "sql": {
                    "works": [
                        {
                            "work_id": work_id,
                            "source_node": 1,
                            "target_node": 2,
                            "priority": 5,
                            "status": "NEW",
                            "assigned_robot_id": None,
                        }
                        for work_id in ("W-001", "W-002", "W-003")
                    ],
                    "work_dependencies": [],
                    "work_schedule_constraints": [],
                },
                "redis": {"active_plan": None, "robots": []},
            },
            "command": {"command_id": "C-relative-date"},
        }
    )
    assert selection["schedule_validation"] == {
        "valid": True,
        "errors": [],
        "dependency_order": ["W-001", "W-002"],
        "constraint_count": 1,
        "dependency_count": 1,
        "scope_work_ids": ["W-001", "W-002"],
        "warnings": [],
    }
    tasks = [AtomicTask.model_validate(row) for row in selection["required_tasks"]]
    assert {task.work_id for task in tasks} == {"W-001", "W-002"}
    by_work = {task.work_id: task for task in tasks}
    assert by_work["W-002"].predecessors == ["W-001:move"]
    problem = schedule_problem(tasks)
    problem["reference_time"] = reference.isoformat()
    problem["captured_at"] = reference.isoformat()
    plan = optimizer().optimize(problem)
    scheduled = {row.work_id: row for row in plan.scheduled_tasks}
    assert scheduled["W-001"].end_time_step <= scheduled["W-002"].start_time_step
    assert scheduled["W-002"].schedule_status == "WAITING_FOR_PREDECESSOR"
    assert plan.unassigned_task_ids == []


def test_initial_plan_does_not_treat_reset_scheduled_end_as_deadline() -> None:
    dependency = TaskDependency(
        predecessor_work_id="W-001",
        successor_work_id="W-002",
    )
    tomorrow_window = TaskScheduleConstraint(
        work_id="W-001",
        earliest_start=REFERENCE + timedelta(hours=20),
        latest_finish=REFERENCE + timedelta(hours=21),
        time_constraint_type="HARD_WINDOW",
    )
    first = tasks_from_work(
        {
            "work_id": "W-001",
            "source_node": 1,
            "target_node": 2,
            "priority": 1,
            "scheduled_end": REFERENCE + timedelta(hours=1),
        },
        False,
        constraint=tomorrow_window,
        dependencies=[dependency],
    )[0]
    second = tasks_from_work(
        {
            "work_id": "W-002",
            "source_node": 2,
            "target_node": 3,
            "priority": 1,
            "scheduled_end": REFERENCE + timedelta(hours=1),
        },
        False,
        dependencies=[dependency],
    )[0]

    plan = optimizer().optimize(schedule_problem([first, second]))
    scheduled = {row.work_id: row for row in plan.scheduled_tasks}

    assert second.deadline is None
    assert second.time_constraint_type == "ASAP"
    assert scheduled["W-001"].end_time_step <= scheduled["W-002"].start_time_step
    assert plan.metadata["tardiness_time_steps"] == 0


def test_routing_reconciliation_shifts_successor_start_after_actual_predecessor_completion() -> None:
    pick = AtomicTask(
        task_id="OP-1:1:pick",
        work_id="OP-1",
        action="PICK",
        source_candidates=[2],
        target_candidates=[2],
    )
    drop = AtomicTask(
        task_id="OP-1:1:drop",
        work_id="OP-1",
        action="DROP",
        source_candidates=[2],
        target_candidates=[3],
        predecessors=[pick.task_id],
    )
    optimizer_plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=pick.task_id,
                work_id="OP-1",
                action="PICK",
                robot_id="R1",
                source_node=2,
                target_node=2,
                start_time_step=0,
                end_time_step=1,
            ),
            ScheduledTask(
                task_id=drop.task_id,
                work_id="OP-1",
                action="DROP",
                robot_id="R1",
                source_node=2,
                target_node=3,
                start_time_step=1,
                end_time_step=2,
            ),
        ],
        objective_value=1,
    )
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        time_step_seconds=5,
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=[pick.task_id, drop.task_id],
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0, action="MOVE"),
                    TimedWaypoint(node_id=2, time_step=5, action="MOVE"),
                    TimedWaypoint(node_id=3, time_step=10, action="MOVE"),
                ],
                distance=2,
            )
        ],
        total_distance=2,
        metadata={
            "task_start_steps": {pick.task_id: 0, drop.task_id: 5},
            "task_completion_steps": {pick.task_id: 5, drop.task_id: 10},
        },
    )
    problem = schedule_problem([pick, drop])

    operational_plan, evidence = _reconcile_routing_schedule(
        optimizer_plan, collision, problem
    )
    scheduled = {row.task_id: row for row in operational_plan.scheduled_tasks}
    simulation = simulate_plan(collision, operational_plan, problem)

    assert scheduled[pick.task_id].end_time_step == 5
    assert scheduled[drop.task_id].start_time_step == 5
    assert scheduled[drop.task_id].end_time_step == 10
    assert evidence["routing_start_time_steps"] == {
        pick.task_id: 0,
        drop.task_id: 5,
    }
    assert "PRECEDENCE_VIOLATION" not in {
        issue.code for issue in simulation.issues
    }

from datetime import UTC, datetime, timedelta

from app.models import AtomicTask, CuOptPlan, ScheduledTask
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def square_problem() -> dict:
    return {
        "robots": [
            {"robot_id": "R1", "node_id": 1},
            {"robot_id": "R2", "node_id": 3},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            },
            {
                "from_node": 3,
                "to_node": 4,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            },
            {
                "from_node": 4,
                "to_node": 1,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            },
        ],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
    }


def test_two_robots_receive_conflict_free_routes() -> None:
    problem = square_problem()
    cuopt_plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="T1",
                robot_id="R1",
                source_node=1,
                target_node=3,
                start_time_step=0,
                end_time_step=2,
                priority=1,
            ),
            ScheduledTask(
                task_id="T2",
                robot_id="R2",
                source_node=3,
                target_node=1,
                start_time_step=0,
                end_time_step=2,
                priority=1,
            ),
        ],
        objective_value=4,
    )
    plan = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(cuopt_plan)
    result = simulate_plan(plan, cuopt_plan)

    assert result.success
    assert result.conflict_count == 0
    assert {route.robot_id for route in plan.routes} == {"R1", "R2"}


def test_far_future_absolute_start_uses_relative_search_horizon() -> None:
    problem = square_problem()
    scheduled = ScheduledTask(
        task_id="W-001:move",
        work_id="W-001",
        robot_id="R1",
        source_node=1,
        target_node=3,
        start_time_step=14237,
        end_time_step=14239,
    )

    plan = PrioritizedTimeExpandedPlanner(problem, 1, 720).solve(
        CuOptPlan(scheduled_tasks=[scheduled], objective_value=2)
    )

    route = plan.routes[0]
    assert [waypoint.time_step for waypoint in route.waypoints] == [14237, 14238, 14239]
    assert all(waypoint.time_step >= 14237 for waypoint in route.waypoints)
    assert plan.metadata["vertex_reservations"] == 3
    assert plan.metadata["edge_reservations"] == 2
    assert plan.metadata["wait_evidence"] == []


def test_search_deadline_is_segment_start_plus_configured_horizon() -> None:
    planner = PrioritizedTimeExpandedPlanner(square_problem(), 1, 2)

    path = planner.shortest_time_path(1, 3, 14237, set(), set())

    assert [waypoint.time_step for waypoint in path] == [14237, 14238, 14239]


def test_future_tasks_on_same_robot_continue_from_previous_target() -> None:
    problem = square_problem()
    tasks = [
        ScheduledTask(
            task_id="W-001:move",
            work_id="W-001",
            robot_id="R1",
            source_node=1,
            target_node=3,
            start_time_step=14237,
            end_time_step=14239,
            priority=1,
        ),
        ScheduledTask(
            task_id="W-002:move",
            work_id="W-002",
            robot_id="R1",
            source_node=3,
            target_node=4,
            start_time_step=14239,
            end_time_step=14240,
            priority=1,
        ),
    ]

    plan = PrioritizedTimeExpandedPlanner(problem, 1, 720).solve(
        CuOptPlan(scheduled_tasks=tasks, objective_value=3)
    )

    route = plan.routes[0]
    assert route.task_ids == ["W-001:move", "W-002:move"]
    assert [(row.node_id, row.time_step) for row in route.waypoints] == [
        (1, 14237),
        (2, 14238),
        (3, 14239),
        (4, 14240),
    ]
    assert tasks[0].end_time_step <= tasks[1].start_time_step


def test_unaffected_active_route_is_reused_during_replan() -> None:
    problem = square_problem()
    problem["affected_robot_ids"] = ["R2"]
    problem["freeze_horizon_seconds"] = 1
    problem["active_plan"] = {
        "activated_at": datetime.now(UTC).isoformat(),
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": ["OLD"],
                    "distance": 2,
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 1, "action": "MOVE"},
                        {"node_id": 3, "time_step": 2, "action": "MOVE"},
                    ],
                }
            ]
        },
    }
    cuopt_plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="NEW",
                robot_id="R2",
                source_node=3,
                target_node=1,
                start_time_step=0,
                end_time_step=2,
            )
        ],
        changed_robot_ids=["R2"],
        objective_value=2,
    )
    plan = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(cuopt_plan)
    result = simulate_plan(plan, cuopt_plan)

    assert result.success
    assert result.conflict_count == 0
    assert {route.robot_id for route in plan.routes} == {"R1", "R2"}


def test_stale_empty_active_route_is_replanned_for_executing_task() -> None:
    problem = square_problem()
    for robot in problem["robots"]:
        robot["battery"] = 100
    task_id = "W-001:move"
    problem["tasks"] = [
        AtomicTask(
            task_id=task_id,
            work_id="W-001",
            action="MOVE",
            source_candidates=[1],
            target_candidates=[3],
            frozen=True,
            assigned_robot_id="R1",
        ).model_dump(mode="json")
    ]
    problem["active_plan"] = {
        "plan_version": "STALE-PLAN",
        "activated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": [task_id],
                    "distance": 2,
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 1, "action": "MOVE"},
                        {"node_id": 3, "time_step": 2, "action": "MOVE"},
                    ],
                }
            ]
        },
    }
    cuopt_plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=task_id,
                work_id="W-001",
                robot_id="R1",
                source_node=1,
                target_node=3,
                start_time_step=0,
                end_time_step=2,
            )
        ],
        objective_value=2,
    )
    planner = PrioritizedTimeExpandedPlanner(problem, 1, 30)

    _, existing_routes = planner.seed_existing_reservations(
        cuopt_plan,
        set(),
        set(),
    )
    plan = planner.solve(cuopt_plan)
    result = simulate_plan(plan, cuopt_plan, problem)

    route = next(route for route in plan.routes if route.robot_id == "R1")
    assert "R1" not in existing_routes
    assert task_id in route.task_ids
    assert route.waypoints
    assert not any(
        issue.code in {"TASK_ROUTE_MISSING", "TASK_ENDPOINT_NOT_REACHED"}
        for issue in result.issues
    )
    assert result.success


def test_valid_future_route_with_all_tasks_is_reused_unchanged() -> None:
    problem = square_problem()
    task_id = "W-001:move"
    expected_waypoints = [
        {"node_id": 1, "time_step": 0, "action": "MOVE"},
        {"node_id": 2, "time_step": 1, "action": "MOVE"},
        {"node_id": 3, "time_step": 2, "action": "MOVE"},
    ]
    problem["active_plan"] = {
        "plan_version": "VALID-PLAN",
        "activated_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": [task_id],
                    "distance": 2,
                    "waypoints": expected_waypoints,
                }
            ]
        },
    }
    cuopt_plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=task_id,
                work_id="W-001",
                robot_id="R1",
                source_node=1,
                target_node=3,
                start_time_step=0,
                end_time_step=2,
            )
        ],
        objective_value=2,
    )

    plan = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(cuopt_plan)

    assert len(plan.routes) == 1
    assert plan.routes[0].task_ids == [task_id]
    assert [
        waypoint.model_dump(mode="json") for waypoint in plan.routes[0].waypoints
    ] == expected_waypoints


def test_long_wait_same_node_pick_keeps_strictly_increasing_route_time() -> None:
    """A WAIT boundary and a zero-distance PICK must not share one timestamp."""

    problem = square_problem()
    tasks = [
        ScheduledTask(
            task_id="T-MOVE",
            action="MOVE",
            robot_id="R1",
            source_node=1,
            target_node=3,
            start_time_step=0,
            end_time_step=2,
            priority=1,
        ),
        ScheduledTask(
            task_id="T-PICK",
            action="PICK",
            robot_id="R1",
            source_node=3,
            target_node=3,
            start_time_step=100,
            end_time_step=101,
            priority=2,
        ),
        ScheduledTask(
            task_id="T-DROP",
            action="DROP",
            robot_id="R1",
            source_node=3,
            target_node=4,
            start_time_step=101,
            end_time_step=102,
            priority=3,
        ),
    ]
    cuopt_plan = CuOptPlan(scheduled_tasks=tasks, objective_value=3)

    plan = PrioritizedTimeExpandedPlanner(problem, 1, 200).solve(cuopt_plan)
    route = plan.routes[0]
    time_steps = [waypoint.time_step for waypoint in route.waypoints]

    assert all(right > left for left, right in zip(time_steps, time_steps[1:]))
    assert plan.metadata["task_start_steps"]["T-PICK"] == 100
    assert plan.metadata["task_completion_steps"]["T-PICK"] == 101
    assert any(
        waypoint.time_step == 101 and waypoint.action == "PICK"
        for waypoint in route.waypoints
    )
    assert simulate_plan(plan, cuopt_plan).success

from __future__ import annotations

from app.models import CuOptPlan, ScheduledTask
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def _base_problem() -> dict:
    return {
        "robots": [
            {"robot_id": "R1", "node_id": 1, "status": "IDLE"},
            {"robot_id": "R2", "node_id": 3, "status": "IDLE"},
        ],
        "nodes": [
            {"node_id": 1, "node_type": "PARKING", "active": True},
            {"node_id": 2, "node_type": "ROUTE", "active": True},
            {"node_id": 3, "node_type": "PARKING", "active": True},
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
        ],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "excluded_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "reference_time": "2026-07-30T00:00:00Z",
    }


def test_changed_robot_ids_union_cuopt_and_event_impact() -> None:
    problem = _base_problem()
    problem["affected_robot_ids"] = ["R1"]
    problem["active_plan"] = {
        "candidate_plan": True,
        "reference_time": "2026-07-30T00:00:00Z",
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": ["OLD-R1"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 1, "action": "MOVE"},
                    ],
                },
                {
                    "robot_id": "R2",
                    "task_ids": ["OLD-R2"],
                    "waypoints": [
                        {"node_id": 3, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 1, "action": "MOVE"},
                    ],
                },
            ]
        },
    }
    cuopt = CuOptPlan(scheduled_tasks=[], changed_robot_ids=["R2"], objective_value=0)
    planner = PrioritizedTimeExpandedPlanner(problem, 1, 30)

    changed, _ = planner.seed_existing_reservations(cuopt, set(), set())

    assert changed == {"R1", "R2"}
    assert planner.stale_route_eviction_evidence["changed_robot_ids"] == ["R1", "R2"]


def test_failed_excluded_robot_route_is_not_preserved_or_reserved() -> None:
    problem = _base_problem()
    problem["affected_robot_ids"] = ["R-FAIL"]
    problem["excluded_robot_ids"] = ["R-FAIL"]
    problem["runtime_partial_replan"] = {
        "robot_state_overrides": {
            "R-FAIL": {"status": "FAILED", "node_id": 2}
        }
    }
    problem["active_plan"] = {
        "candidate_plan": True,
        "reference_time": "2026-07-30T00:00:00Z",
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R-FAIL",
                    "task_ids": ["OLD-PICK", "OLD-DROP"],
                    "waypoints": [
                        {"node_id": 2, "time_step": 0, "action": "MOVE"},
                        {"node_id": 3, "time_step": 1, "action": "MOVE"},
                    ],
                }
            ]
        },
    }
    cuopt = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="HANDOVER",
                work_id="W-C",
                action="MOVE",
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

    plan = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(cuopt)

    assert {route.robot_id for route in plan.routes} == {"R2"}
    assert plan.metadata["stale_route_eviction"]["evicted_robot_ids"] == ["R-FAIL"]
    assert "R-FAIL" not in plan.metadata["route_sources"]


def test_live_handover_shape_evicts_r2_03_and_keeps_replacement_and_d_routes() -> None:
    problem = {
        "robots": [
            {"robot_id": "R2-01", "node_id": 2147, "status": "IDLE", "battery": 100},
            {"robot_id": "R2-02", "node_id": 2146, "status": "IDLE", "battery": 95},
        ],
        "nodes": [
            {"node_id": 2146, "node_type": "OUTBOUND", "active": True},
            {"node_id": 2147, "node_type": "PARKING", "active": True},
            {"node_id": 2044, "node_type": "ROUTE", "active": True},
            {"node_id": 2088, "node_type": "STORAGE", "active": True},
        ],
        "edges": [
            {
                "from_node": 2146,
                "to_node": 2044,
                "distance": 1,
                "travel_seconds": 5,
                "direction": "BOTH",
            },
            {
                "from_node": 2147,
                "to_node": 2044,
                "distance": 1,
                "travel_seconds": 5,
                "direction": "BOTH",
            },
            {
                "from_node": 2044,
                "to_node": 2088,
                "distance": 1,
                "travel_seconds": 5,
                "direction": "BOTH",
            },
        ],
        "temporary_closures": [],
        "reference_time": "2026-07-30T00:01:00Z",
        "affected_robot_ids": ["R2-03"],
        "excluded_robot_ids": ["R2-03"],
        "freeze_horizon_seconds": 15,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
        "runtime_partial_replan": {
            "robot_state_overrides": {
                "R2-03": {
                    "status": "FAILED",
                    "node_id": 2088,
                    "safe_stop_confirmed": True,
                }
            }
        },
        "active_plan": {
            "candidate_plan": True,
            "reference_time": "2026-07-30T00:01:00Z",
            "collision_plan": {
                "routes": [
                    {
                        "robot_id": "R2-03",
                        "task_ids": ["C:1:pick", "C:1:drop"],
                        "waypoints": [
                            {"node_id": 2088, "time_step": 0, "action": "MOVE"},
                            {"node_id": 2044, "time_step": 1, "action": "MOVE"},
                            {"node_id": 2146, "time_step": 2, "action": "MOVE"},
                        ],
                    }
                ]
            },
        },
    }
    handover_pick = "C:handover:event-001:pick"
    handover_drop = "C:handover:event-001:drop"
    d_pick = "D:1:pick"
    d_drop = "D:1:drop"
    cuopt = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=handover_pick,
                work_id="C",
                action="PICK",
                robot_id="R2-02",
                source_node=2088,
                target_node=2088,
                start_time_step=0,
                end_time_step=3,
                priority=1,
            ),
            ScheduledTask(
                task_id=handover_drop,
                work_id="C",
                action="DROP",
                robot_id="R2-02",
                source_node=2088,
                target_node=2146,
                start_time_step=3,
                end_time_step=6,
                priority=2,
            ),
            ScheduledTask(
                task_id=d_pick,
                work_id="D",
                action="PICK",
                robot_id="R2-01",
                source_node=2088,
                target_node=2088,
                start_time_step=20,
                end_time_step=23,
                priority=3,
            ),
            ScheduledTask(
                task_id=d_drop,
                work_id="D",
                action="DROP",
                robot_id="R2-01",
                source_node=2088,
                target_node=2146,
                start_time_step=23,
                end_time_step=26,
                priority=4,
            ),
        ],
        changed_robot_ids=["R2-01", "R2-02"],
        objective_value=10,
        metadata={
            "execution_task_dependencies": [
                {
                    "predecessor_task_id": handover_pick,
                    "successor_task_id": handover_drop,
                    "dependency_type": "FINISH_TO_START",
                    "lag_seconds": 0,
                },
                {
                    "predecessor_task_id": d_pick,
                    "successor_task_id": d_drop,
                    "dependency_type": "FINISH_TO_START",
                    "lag_seconds": 0,
                },
            ]
        },
    )

    plan = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(cuopt)
    result = simulate_plan(plan, cuopt, problem)
    routes = {route.robot_id: route for route in plan.routes}

    assert set(routes) == {"R2-01", "R2-02"}
    assert routes["R2-02"].task_ids == [handover_pick, handover_drop]
    assert routes["R2-01"].task_ids == [d_pick, d_drop]
    assert "R2-03" not in plan.metadata["route_sources"]
    assert plan.metadata["stale_route_eviction"]["evicted_robot_ids"] == ["R2-03"]
    assert result.success
    assert not any("R2-03" in issue.message for issue in result.issues)

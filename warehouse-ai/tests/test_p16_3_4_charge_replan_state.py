from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import patch

from app.models import (
    CollisionFreePlan,
    CuOptPlan,
    ScheduledTask,
    TimedRoute,
    TimedWaypoint,
)
from app.planning import nodes
from app.services.command_language import parse_deterministic_command
from app.services.energy_reconciliation import reconcile_plan_energy
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner


FULL_POLICY_COMMAND = (
    "R2-03의 배터리가 현재 24.5%라고 가정해. 출고 작업 완료 후 "
    "안전 여유 0.5%를 포함해 최소 배터리 기준 이상으로 도달할 수 있는 "
    "active CHARGER 중 비용이 가장 낮은 충전소를 선택하고, 배터리를 "
    "작업 투입 기준인 80%까지 충전한 뒤 다음 작업에 투입해줘. "
    "안전하게 도달 가능한 충전소가 없으면 로컬 재계획해줘."
)


def test_full_policy_command_records_all_battery_hard_constraints() -> None:
    parsed = parse_deterministic_command(FULL_POLICY_COMMAND)
    assert "MINIMUM_REQUIRED_CHARGE" in parsed.hard_constraints
    assert "MINIMUM_BATTERY_AT_ALL_TIMES" in parsed.hard_constraints
    assert "CHARGE_TARGET_80_PERCENT" in parsed.hard_constraints
    assert "SAFE_CHARGER_REACHABILITY" in parsed.hard_constraints


def test_route_reconciliation_never_reduces_80_percent_post_charge_target() -> None:
    problem = {
        "nodes": [
            {"node_id": 1, "node_type": "INTERSECTION", "active": True},
            {"node_id": 2, "node_type": "CHARGER", "active": True},
            {"node_id": 3, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 9.82, "direction": "BOTH", "active": True},
            {"from_node": 2, "to_node": 3, "distance": 20.0, "direction": "BOTH", "active": True},
        ],
        "robots": [{"robot_id": "R1", "node_id": 1, "battery": 21.0}],
        "min_robot_battery": 20.0,
        "charge_target_battery": 80.0,
        "energy_per_distance": 0.05,
        "charge_rate_percent_per_minute": 5.0,
        "time_step_seconds": 5,
    }
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="C1",
                work_id="W1",
                action="CHARGE",
                robot_id="R1",
                source_node=1,
                target_node=2,
                start_time_step=0,
                end_time_step=145,
                estimated_distance=9.82,
                estimated_energy=0.491,
                # Optimizer estimate reached 80, but final routing arrival is
                # 0.18% lower and used to produce the real 79.82% failure.
                charged_percent=59.311,
                charge_target_battery=80.0,
                charge_duration_seconds=715,
                charger_candidates=[{"charger_node": 2, "selected": True}],
            ),
            ScheduledTask(
                task_id="D1",
                work_id="W1",
                action="DROP",
                robot_id="R1",
                source_node=2,
                target_node=3,
                start_time_step=145,
                end_time_step=146,
                estimated_distance=20.0,
                estimated_energy=1.0,
            ),
        ],
        objective_value=0.0,
        metadata={
            "charger_selections": [
                {
                    "task_id": "C1",
                    "robot_id": "R1",
                    "selected_charger_node": 2,
                    "candidates": [{"charger_node": 2, "selected": True}],
                }
            ]
        },
    )
    route = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        time_step_seconds=5,
        total_distance=29.82,
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=["C1", "D1"],
                distance=29.82,
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0, action="MOVE"),
                    TimedWaypoint(node_id=2, time_step=1, action="MOVE"),
                    TimedWaypoint(node_id=2, time_step=2, action="CHARGE"),
                    TimedWaypoint(node_id=2, time_step=145, action="CHARGE"),
                    TimedWaypoint(node_id=3, time_step=146, action="MOVE"),
                ],
            )
        ],
    )

    adjusted, evidence = reconcile_plan_energy(plan, route, problem)
    charge = next(row for row in adjusted.scheduled_tasks if row.action == "CHARGE")

    assert math.isclose(charge.charge_target_battery, 80.0, abs_tol=1e-9)
    assert math.isclose(charge.charged_percent, 59.491, abs_tol=1e-9)
    assert charge.charge_duration_seconds == 715
    assert evidence["unsafe_robot_ids"] == []
    assert evidence["robots"]["R1"]["charge_tasks"][0]["target_battery"] == 80.0


def test_local_replan_reschedules_fixed_robot_task_and_regenerates_charge() -> None:
    optimizer = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
        battery_safety_margin_percent=0.5,
    )
    problem = {
        "reference_time": "2026-07-24T07:00:00+00:00",
        "time_step_seconds": 5,
        "plan_mode": "LOCAL_REPLAN",
        "fixed_task_ids": [],
        "changeable_task_ids": ["T1"],
        "affected_robot_ids": ["R1"],
        "min_robot_battery": 20,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80,
        "charge_rate_percent_per_minute": 5,
        "robots": [
            {"robot_id": "R1", "node_id": 1, "battery": 24.5, "status": "IDLE"},
            {"robot_id": "R2", "node_id": 1, "battery": 100, "status": "IDLE"},
        ],
        "nodes": [
            {"node_id": 1, "node_type": "INTERSECTION", "active": True},
            {"node_id": 2, "node_type": "CHARGER", "active": True, "charging_cost": 1.0},
            {"node_id": 4, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 60.0, "travel_seconds": 60, "direction": "BOTH", "active": True},
            {"from_node": 2, "to_node": 4, "distance": 40.0, "travel_seconds": 40, "direction": "BOTH", "active": True},
        ],
        "tasks": [
            {
                "task_id": "T1",
                "work_id": "W1",
                "action": "PICK",
                "quantity": 0,
                "source_candidates": [2],
                "target_candidates": [4],
                "assigned_robot_id": "R1",
                "frozen": False,
            }
        ],
        "active_plan": {
            "candidate_plan": True,
            "reference_time": "2026-07-24T07:00:00+00:00",
            "cuopt_plan": {
                "scheduled_tasks": [
                    {
                        "task_id": "T1",
                        "work_id": "W1",
                        "action": "PICK",
                        "robot_id": "R1",
                        "source_node": 2,
                        "target_node": 4,
                        "start_time_step": 0,
                        "end_time_step": 20,
                        "estimated_distance": 100.0,
                        "estimated_energy": 5.0,
                    }
                ]
            },
        },
    }

    result = optimizer.optimize(problem)
    assert [row.robot_id for row in result.scheduled_tasks if row.action != "CHARGE"] == ["R1"]
    charge = next(row for row in result.scheduled_tasks if row.action == "CHARGE")
    assert charge.robot_id == "R1"
    assert charge.charge_target_battery == 80.0
    assert "T1" not in result.metadata["preserved_task_ids"]


def test_changed_candidate_route_does_not_duplicate_old_task_ids() -> None:
    problem = {
        "reference_time": "2026-07-24T07:00:00+00:00",
        "time_step_seconds": 5,
        "freeze_horizon_seconds": 0,
        "affected_robot_ids": ["R1"],
        "robots": [{"robot_id": "R1", "node_id": 1}],
        "nodes": [
            {"node_id": 1, "node_type": "INTERSECTION", "active": True},
            {"node_id": 2, "node_type": "OUTBOUND", "active": True},
        ],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 1.0, "travel_seconds": 5, "direction": "BOTH", "active": True}
        ],
        "active_plan": {
            "candidate_plan": True,
            "reference_time": "2026-07-24T07:00:00+00:00",
            "collision_plan": {
                "routes": [
                    {
                        "robot_id": "R1",
                        "task_ids": ["T1", "OLD:charge:9"],
                        "waypoints": [
                            {"node_id": 1, "time_step": 0, "action": "MOVE"},
                            {"node_id": 2, "time_step": 1, "action": "MOVE"},
                        ],
                    }
                ]
            },
        },
    }
    planner = PrioritizedTimeExpandedPlanner(problem, time_step_seconds=5, max_time_steps=100)
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="T1",
                work_id="W1",
                action="MOVE",
                robot_id="R1",
                source_node=1,
                target_node=2,
                start_time_step=0,
                end_time_step=1,
                estimated_distance=1.0,
                estimated_energy=0.05,
            )
        ],
        changed_robot_ids=["R1"],
        objective_value=0.0,
    )

    result = planner.solve(plan)
    assert result.routes[0].task_ids == ["T1"]


def test_prepare_replan_clears_all_route_derived_state() -> None:
    state = {
        "command": {"command_id": "C1", "warehouse_id": 1, "text": "plan"},
        "interpretation": {"execution_mode": "SIMULATE_ONLY"},
        "supervisor_decision": {"max_replan_attempts": 2},
        "snapshot": {
            "captured_at": "2026-07-24T00:00:00+00:00",
            "sql": {"robots": [{"robot_id": "R1"}], "works": [{"work_id": "W1", "status": "NEW"}]},
            "redis": {"executing_task_ids": [], "active_plan": None},
        },
        "scope": {
            "plan_mode": "INITIAL_PLAN",
            "affected_task_ids": [],
            "affected_robot_ids": [],
            "fixed_task_ids": [],
            "changeable_task_ids": [],
            "freeze_horizon_seconds": 10,
            "include_new_command": True,
            "optimization_goal": "safe",
            "reason_summary": "initial",
        },
        "required_tasks": [{"task_id": "T1", "work_id": "W1", "action": "MOVE", "source_candidates": [1], "target_candidates": [2], "frozen": False}],
        "cuopt_plan": {"scheduled_tasks": [{"task_id": "T1", "work_id": "W1", "robot_id": "R1"}]},
        "collision_plan": {"routes": []},
        "robot_command_batches": [{"robot_id": "R1", "commands": [{"action": "CHARGE"}]}],
        "routing_evidence": {"routes": ["stale"]},
        "reservation_evidence": {"waits": ["stale"]},
        "distance_comparison": {"difference": 1},
        "route_energy_reconciliation": {"unsafe_robot_ids": ["R1"]},
        "daily_schedule": [{"task_id": "OLD"}],
        "verification_decision": {
            "decision": "REPLAN_LOCAL",
            "requires_replan": True,
            "replan_scope": "LOCAL_REPLAN",
            "affected_robot_ids": ["R1"],
            "affected_task_ids": ["T1"],
            "blocking_findings": ["charge target"],
            "evidence_ids": ["verification:1"],
            "summary": "replan",
        },
        "verification_evidence": [{"evidence_id": "verification:1", "severity": "BLOCKING", "code": "CHARGE_TARGET_POLICY_NOT_MET", "source": "COMMAND_CONSTRAINT", "message": "charge target", "robot_ids": ["R1"], "task_ids": ["T1"]}],
        "replan_attempt": 0,
        "max_replan_attempts": 2,
        "replan_history": [],
        "repeated_failure_signatures": {},
        "plan_version": "P0",
        "original_plan_version": "P0",
        "current_plan_version": "P0",
    }

    with patch.object(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(freeze_horizon_seconds=10, time_step_seconds=5),
    ):
        update = nodes.prepare_replan_node(state)
    assert update["replan_ready"] is True
    assert update["robot_command_batches"] == []
    assert update["routing_evidence"] == {}
    assert update["reservation_evidence"] == {}
    assert update["distance_comparison"] == {}
    assert update["route_energy_reconciliation"] == {}
    assert update["daily_schedule"] == []

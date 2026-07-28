from __future__ import annotations

from copy import deepcopy

from app.planning.nodes import (
    _previous_successful_candidate,
    build_verification_evidence,
)
from app.services.charger_selection import (
    OPPORTUNITY_DISTANCE_FALLBACK_POLICY,
    rank_opportunity_charger_candidates,
)
from app.services.opportunity_charging import augment_plan_with_opportunity_charging
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan
from tests.test_p16_5_6_idle_holding_routing import (
    _daily_multi_robot_plan,
    _reconcile,
)
from tests.test_p16_5_8_opportunity_charging import _p16_5_8_problem


def test_mixed_cost_data_uses_uniform_distance_fallback() -> None:
    candidates = [
        {
            "charger_node": 2150,
            "safe_reachable": True,
            "rejection_reason": None,
            "linked_waiting_area_node_ids": [2160],
            "to_charger_distance": 8.0,
            "charger_to_next_source_distance": 8.0,
            "charged_percent": 5.0,
            "charge_duration_seconds": 60,
            "charger_cost": 1.2,
        },
        {
            "charger_node": 2152,
            "safe_reachable": True,
            "rejection_reason": None,
            "linked_waiting_area_node_ids": [2162],
            "to_charger_distance": 1.0,
            "charger_to_next_source_distance": 2.0,
            "charged_percent": 5.0,
            "charge_duration_seconds": 60,
            "charger_cost": None,
        },
    ]

    ranked, selected, policy, _ = rank_opportunity_charger_candidates(candidates)

    assert policy == OPPORTUNITY_DISTANCE_FALLBACK_POLICY
    assert selected is not None
    assert selected["charger_node"] == 2152
    assert selected["cost_mode"] == "UNIFORM_DISTANCE_FALLBACK_INCOMPLETE_COST_DATA"
    assert sum(bool(row.get("selected")) for row in ranked) == 1


def test_replan_regenerates_opportunity_tasks_and_routes_from_candidate_prefix() -> None:
    problem = _p16_5_8_problem()
    business_plan = _daily_multi_robot_plan()
    first_plan, _ = augment_plan_with_opportunity_charging(problem, business_plan)
    first_routes = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(first_plan)

    replan_problem = _p16_5_8_problem()
    replan_problem["active_plan"] = {
        "plan_version": "candidate-v1",
        "reference_time": "2026-07-24T22:15:00+00:00",
        "cuopt_plan": first_plan.model_dump(mode="json"),
        "collision_plan": first_routes.model_dump(mode="json"),
        "candidate_plan": True,
    }
    replan_problem["affected_robot_ids"] = ["R2-03"]
    reoptimized_business_plan = business_plan.model_copy(
        update={"changed_robot_ids": ["R2-03"]}
    )

    regenerated, evidence = augment_plan_with_opportunity_charging(
        replan_problem,
        reoptimized_business_plan,
    )
    second_routes = PrioritizedTimeExpandedPlanner(
        replan_problem, 5, 720
    ).solve(regenerated)

    charge_ids = [
        task.task_id for task in regenerated.scheduled_tasks if task.action == "CHARGE"
    ]
    assert evidence["inserted_charge_task_count"] == 3
    assert len(charge_ids) == len(set(charge_ids)) == 3
    assert len(second_routes.routes) == 3
    assert second_routes.total_distance > 0


def test_verification_replays_shared_opportunity_policy_without_false_cost_failure() -> None:
    problem = _p16_5_8_problem()
    # Reproduce the live Aura shape: some chargers have configured cost and the
    # selected nearby charger does not.  The shared policy must use one uniform
    # distance fallback rather than treating missing cost as zero.
    chargers = [
        row for row in problem["nodes"] if row.get("node_type") == "CHARGER"
    ]
    chargers[0]["charging_cost"] = 1.2
    chargers[1]["charging_cost"] = 1.5
    chargers[2].pop("charging_cost", None)

    augmented, _ = augment_plan_with_opportunity_charging(
        problem, _daily_multi_robot_plan()
    )
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(augmented)
    operational = _reconcile(augmented, collision)
    simulation = simulate_plan(collision, operational, problem)

    state = {
        "supervisor_decision": {"requires_clarification": False},
        "validation": {"valid": True, "errors": [], "warnings": []},
        "interpretation": {
            "command_kind": "PLAN",
            "inventory_operations": [],
            "excluded_robot_ids": [],
            "target_node_ids": [],
        },
        "optimization_problem": problem,
        "cuopt_plan": operational.model_dump(mode="json"),
        "collision_plan": collision.model_dump(mode="json"),
        "simulation": simulation.model_dump(mode="json"),
        "errors": [],
        "warnings": [],
    }

    evidence = build_verification_evidence(state)
    codes = {row["code"] for row in evidence}

    assert "CHARGER_COST_SELECTION_INVALID" not in codes
    assert "OPPORTUNITY_CHARGER_POLICY_SELECTION_INVALID" not in codes
    assert "OPPORTUNITY_CHARGER_POLICY_MISMATCH" not in codes
    assert "CHARGER_COST_DATA_INCOMPLETE_DISTANCE_FALLBACK" in codes


def test_previous_successful_candidate_is_preserved_before_replan() -> None:
    state = {
        "plan_version": "candidate-v1",
        "current_plan_version": "candidate-v1",
        "cuopt_plan": {"scheduled_tasks": [{"task_id": "T1"}]},
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": ["T1"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0},
                        {"node_id": 2, "time_step": 1},
                    ],
                }
            ]
        },
        "simulation": {"success": True, "valid": True},
        "plan_validation": {"success": True, "valid": True},
        "verification_decision": {
            "decision": "REPLAN_LOCAL",
            "blocking_findings": ["charger policy mismatch"],
        },
        "routing_evidence": {"complete": True},
        "reservation_evidence": {"final_conflict_count": 0},
        "distance_comparison": {"routing_final_distance": 1.0},
    }

    preserved = _previous_successful_candidate(deepcopy(state))

    assert preserved["plan_version"] == "candidate-v1"
    assert preserved["routing_succeeded"] is True
    assert preserved["simulation_succeeded"] is True
    assert preserved["collision_plan"]["routes"][0]["robot_id"] == "R1"

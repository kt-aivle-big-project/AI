from types import SimpleNamespace

from app.prompts.cuopt_formulator import CUOPT_FORMULATOR_SYSTEM, PROMPT_VERSION
from app.services.cuopt_formulation_service import _bounded_rule_vehicle_floor
from app.services.simulation_plan_service import RollingHorizonReplanService


def test_agent_reassesses_implicit_cost_default_from_live_context() -> None:
    assert PROMPT_VERSION == "13.35-contextual-objective-selection"
    assert "implicit MIN_TOTAL_COST value is a neutral request default" in CUOPT_FORMULATOR_SYSTEM
    assert "large independent wave" in CUOPT_FORMULATOR_SYSTEM
    assert "BALANCED, not implicit MIN_TOTAL_COST" in CUOPT_FORMULATOR_SYSTEM


def test_agent_coordinates_parallelism_with_soft_route_balance() -> None:
    assert "Keep objective_profile and minimum_vehicle_count coherent" in CUOPT_FORMULATOR_SYSTEM
    assert "Do not use minimum_vehicle_count as a substitute for workload balance" in CUOPT_FORMULATOR_SYSTEM
    assert "Never output raw" in CUOPT_FORMULATOR_SYSTEM
    assert "solver weights" in CUOPT_FORMULATOR_SYSTEM


def test_low_battery_rule_replan_preserves_only_replannable_task_capacity() -> None:
    active = SimpleNamespace(
        logical_operations=[
            SimpleNamespace(task_ids=["TASK-001"]),
            SimpleNamespace(task_ids=["TASK-002"]),
            SimpleNamespace(task_ids=["TASK-003"]),
        ],
        robots=[
            SimpleNamespace(
                robot_id=f"R00{index}",
                steps=[
                    SimpleNamespace(
                        step_type="SERVICE",
                        end_at_ms=4000,
                        task_id=f"TASK-00{index}_PICK",
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )
    snapshot = SimpleNamespace(
        completed_task_bases=["TASK-003"],
        locked_task_bases=[],
        replan_at_sim_time_ms=2500,
    )

    prior_task_vehicles = (
        RollingHorizonReplanService._remaining_task_vehicle_count(active, snapshot)
    )

    assert prior_task_vehicles == 2
    assert _bounded_rule_vehicle_floor(
        requested=prior_task_vehicles,
        eligible_vehicle_count=5,
        actionable_cycle_count=25,
    ) == 2
    assert _bounded_rule_vehicle_floor(
        requested=4,
        eligible_vehicle_count=3,
        actionable_cycle_count=2,
    ) == 2

from __future__ import annotations

import pytest

from app.domain.schemas import (
    RobotRuntime,
    RobotRuntimeContext,
    RuntimePlanningOverrides,
    SimulationPlan,
    SimulationPlanStep,
    SimulationRobotPlan,
)
from app.services.context_service import apply_runtime_overrides
from app.services.simulation_plan_service import ReplanTimelineValidator


def test_global_replan_horizon_clamps_robot_absent_from_old_plan() -> None:
    context = RobotRuntimeContext(
        robots=[
            RobotRuntime(
                robot_id="R002",
                robot_code="R002",
                status="idle",
                battery_pct=88,
                capacity_units=1,
                current_node="R1_5",
                sim_time_ms=0,
            ),
            RobotRuntime(
                robot_id="R003",
                robot_code="R003",
                status="idle",
                battery_pct=91,
                capacity_units=1,
                current_node="R2_7",
                sim_time_ms=3250,
            ),
        ],
        candidate_robot_ids=["R002", "R003"],
        min_battery_pct=30,
        min_capacity_units=1,
        summary="runtime before rolling horizon clamp",
    )

    result = apply_runtime_overrides(
        context,
        RuntimePlanningOverrides(planning_horizon_start_ms=3000),
    )

    robots = {value.robot_id: value for value in result.robots}
    assert robots["R002"].sim_time_ms == 3000
    assert robots["R003"].sim_time_ms == 3250
    assert result.candidate_robot_ids == ["R002", "R003"]


def test_replan_timeline_validator_rejects_step_before_horizon() -> None:
    plan = SimulationPlan(
        plan_id="PLAN-NEW",
        plan_version=2,
        base_plan_id="PLAN-OLD",
        warehouse_id="WH-001",
        simulation_id="SIM-RH",
        plan_kind="REPLAN",
        map_version="MAP-1",
        plan_start_sim_time_ms=3000,
        effective_from_sim_time_ms=3000,
        makespan_ms=4000,
        absolute_finish_at_ms=7000,
        robots=[
            SimulationRobotPlan(
                robot_id="R002",
                initial_node="R1_5",
                available_at_ms=3000,
                finish_at_ms=7000,
                steps=[
                    SimulationPlanStep(
                        step_id="R002-0001",
                        sequence=1,
                        step_type="MOVE",
                        start_at_ms=0,
                        end_at_ms=1000,
                        edge_id="E1",
                        from_node="R1_5",
                        to_node="R1_6",
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="REPLAN_STEP_BEFORE_HORIZON"):
        ReplanTimelineValidator.validate(plan, 3000)


def test_replan_timeline_validator_accepts_per_robot_absolute_availability() -> None:
    plan = SimulationPlan(
        plan_id="PLAN-NEW",
        plan_version=2,
        base_plan_id="PLAN-OLD",
        warehouse_id="WH-001",
        simulation_id="SIM-RH",
        plan_kind="REPLAN",
        map_version="MAP-1",
        plan_start_sim_time_ms=3000,
        effective_from_sim_time_ms=3000,
        makespan_ms=4500,
        absolute_finish_at_ms=7500,
        robots=[
            SimulationRobotPlan(
                robot_id="R002",
                initial_node="R1_5",
                available_at_ms=3000,
                finish_at_ms=7000,
                steps=[
                    SimulationPlanStep(
                        step_id="R002-0001",
                        sequence=1,
                        step_type="MOVE",
                        start_at_ms=3000,
                        end_at_ms=4000,
                        edge_id="E1",
                        from_node="R1_5",
                        to_node="R1_6",
                    )
                ],
            ),
            SimulationRobotPlan(
                robot_id="R003",
                initial_node="R2_7",
                available_at_ms=3250,
                finish_at_ms=7500,
                steps=[
                    SimulationPlanStep(
                        step_id="R003-0001",
                        sequence=1,
                        step_type="MOVE",
                        start_at_ms=3250,
                        end_at_ms=4250,
                        edge_id="E2",
                        from_node="R2_7",
                        to_node="R2_8",
                    )
                ],
            ),
        ],
    )

    ReplanTimelineValidator.validate(plan, 3000)

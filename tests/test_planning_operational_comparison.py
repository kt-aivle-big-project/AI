from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.planning_evaluation import (
    BranchQualityMetrics,
    PlanningComparisonRequest,
)
from app.services.planning_evaluation_service import (
    PlanningComparisonService,
    _comparison_task_identity,
    _distribution_summary,
    _physical_cycle_counts_by_robot,
)


def _metrics(
    *,
    route: str,
    repeat_index: int = 1,
    makespan_ms: int,
    distance_m: float,
    wait_ms: int,
    used_robots: int,
    cycle_range: int | None = None,
    cycle_standard_deviation: float | None = None,
    cycle_cv: float | None = None,
    cycle_gini: float | None = None,
    work_time_range_ms: int | None = None,
    work_time_standard_deviation_ms: float | None = None,
    work_time_cv: float | None = None,
    max_robot_finish_at_ms: float | None = None,
) -> BranchQualityMetrics:
    operation_ids = ["OP-001", "OP-002"]
    task_ids = ["OP-001:PICK", "OP-001:DROP", "OP-002:PICK", "OP-002:DROP"]
    return BranchQualityMetrics(
        route=route,
        repeat_index=repeat_index,
        applicability="APPLICABLE",
        snapshot_id="SNAPSHOT-001",
        objective_profile="BALANCED",
        operation_ids=operation_ids,
        mandatory_task_ids=task_ids,
        optimization_task_ids=task_ids,
        makespan_ms=makespan_ms,
        total_distance_m=distance_m,
        total_wait_ms=wait_ms,
        used_robot_count=used_robots,
        fleet_effort_robot_ms=makespan_ms * used_robots,
        throughput_operations_per_hour=2 * 3_600_000 / makespan_ms,
        physical_cycle_count_range=cycle_range,
        physical_cycle_count_standard_deviation=cycle_standard_deviation,
        physical_cycle_count_coefficient_of_variation=cycle_cv,
        physical_cycle_count_gini_coefficient=cycle_gini,
        scheduled_work_time_range_ms=work_time_range_ms,
        scheduled_work_time_standard_deviation_ms=(
            work_time_standard_deviation_ms
        ),
        scheduled_work_time_coefficient_of_variation=work_time_cv,
        max_robot_finish_at_ms=max_robot_finish_at_ms,
        hard_gate_passed=True,
    )


def test_balanced_is_the_default_evaluation_objective() -> None:
    assert PlanningComparisonRequest().required_objective_profile == "BALANCED"


def test_direct_inbound_task_identity_uses_canonical_operation_id() -> None:
    rule_task = SimpleNamespace(
        task_id="TASK-001",
        operation_type="INBOUND_ITEM",
        order_id="IN-3501",
    )
    agent_task = SimpleNamespace(
        task_id="IN-3501",
        operation_type="INBOUND_ITEM",
        order_id="IN-3501",
    )

    assert _comparison_task_identity(rule_task) == "INBOUND_ITEM:IN-3501"
    assert _comparison_task_identity(agent_task) == "INBOUND_ITEM:IN-3501"


def test_g2p_task_identity_keeps_physical_cycle_id() -> None:
    task = SimpleNamespace(
        task_id="G2P-SIM-001-EVAL-ITEM-001",
        operation_type="G2P_HANDLING_UNIT",
        order_id="ORD-3001",
    )

    assert _comparison_task_identity(task) == "G2P-SIM-001-EVAL-ITEM-001"


def test_physical_cycle_distribution_includes_unused_candidate_robots() -> None:
    payload = SimpleNamespace(
        fleet_data=SimpleNamespace(vehicle_ids=["R1", "R2", "R3"]),
        task_data=SimpleNamespace(
            task_ids=[
                "C1_PICK",
                "C1_DROP",
                "C2_PICK",
                "C2_DROP",
                "C3_PICK",
                "C3_DROP",
            ],
            pickup_and_delivery_pairs=[[0, 1], [2, 3], [4, 5]],
        ),
    )
    optimizer = SimpleNamespace(
        routes=[
            SimpleNamespace(
                vehicle_id="R1",
                task_sequence=["C1_PICK", "C1_DROP"],
            ),
            SimpleNamespace(
                vehicle_id="R2",
                task_sequence=["C2_PICK", "C2_DROP", "C3_PICK", "C3_DROP"],
            ),
        ]
    )

    counts = _physical_cycle_counts_by_robot(
        optimizer=optimizer,
        payload=payload,
    )
    distribution = _distribution_summary([float(value) for value in counts.values()])

    assert counts == {"R1": 1, "R2": 2, "R3": 0}
    assert distribution["range"] == 2.0
    assert distribution["coefficient_of_variation"] == pytest.approx(0.816497)
    assert distribution["gini_coefficient"] == pytest.approx(0.444444)


def test_distance_only_objective_is_rejected_for_operational_evaluation() -> None:
    with pytest.raises(ValueError, match="distance-only"):
        PlanningComparisonRequest(required_objective_profile="MIN_TOTAL_COST")


def test_more_robots_can_win_when_speed_gain_stays_inside_resource_guardrails() -> None:
    rule = _metrics(
        route="RULE_FORMULATION",
        makespan_ms=100_000,
        distance_m=100.0,
        wait_ms=10_000,
        used_robots=1,
    )
    agent_runs = [
        _metrics(
            route="AGENT_FORMULATION",
            repeat_index=index,
            makespan_ms=value,
            distance_m=110.0,
            wait_ms=9_000,
            used_robots=2,
        )
        for index, value in enumerate((58_000, 60_000, 62_000), start=1)
    ]

    result = PlanningComparisonService._operational_comparison(
        rule,
        agent_runs,
        PlanningComparisonRequest(agent_repeats=3, min_valid_agent_runs=3),
    )

    assert result.verdict == "AGENT_OPERATIONAL_WIN"
    assert result.strict_pass is True
    assert result.makespan_improvement_pct == 40.0
    assert result.used_robot_delta_agent_minus_rule == 1.0
    assert result.fleet_effort_improvement_pct == -20.0
    assert result.all_resource_guardrails_passed is True


def test_speed_gain_is_tradeoff_when_extra_fleet_and_distance_are_excessive() -> None:
    rule = _metrics(
        route="RULE_FORMULATION",
        makespan_ms=100_000,
        distance_m=100.0,
        wait_ms=10_000,
        used_robots=1,
    )
    agent_runs = [
        _metrics(
            route="AGENT_FORMULATION",
            repeat_index=index,
            makespan_ms=70_000,
            distance_m=160.0,
            wait_ms=15_000,
            used_robots=4,
        )
        for index in range(1, 4)
    ]

    result = PlanningComparisonService._operational_comparison(
        rule,
        agent_runs,
        PlanningComparisonRequest(agent_repeats=3, min_valid_agent_runs=3),
    )

    assert result.verdict == "TRADEOFF"
    assert result.strict_pass is False
    assert result.makespan_improvement_pct == 30.0
    assert result.distance_guardrail_passed is False
    assert result.fleet_effort_guardrail_passed is False
    assert result.wait_guardrail_passed is False


def test_like_for_like_operational_metrics_are_a_tie() -> None:
    rule = _metrics(
        route="RULE_FORMULATION",
        makespan_ms=100_000,
        distance_m=100.0,
        wait_ms=10_000,
        used_robots=1,
    )
    agent_runs = [
        _metrics(
            route="AGENT_FORMULATION",
            repeat_index=index,
            makespan_ms=100_000,
            distance_m=100.0,
            wait_ms=10_000,
            used_robots=1,
        )
        for index in range(1, 4)
    ]

    result = PlanningComparisonService._operational_comparison(
        rule,
        agent_runs,
        PlanningComparisonRequest(agent_repeats=3, min_valid_agent_runs=3),
    )

    assert result.verdict == "TIE"
    assert result.strict_pass is True


def test_workload_distribution_diagnostics_compare_rule_and_agent_medians() -> None:
    rule = _metrics(
        route="RULE_FORMULATION",
        makespan_ms=100_000,
        distance_m=100.0,
        wait_ms=10_000,
        used_robots=3,
        cycle_range=18,
        cycle_standard_deviation=8.0,
        cycle_cv=1.5,
        cycle_gini=0.75,
        work_time_range_ms=80_000,
        work_time_standard_deviation_ms=35_000,
        work_time_cv=1.4,
        max_robot_finish_at_ms=100_000,
    )
    agent_runs = [
        _metrics(
            route="AGENT_FORMULATION",
            repeat_index=index,
            makespan_ms=makespan,
            distance_m=110.0,
            wait_ms=9_000,
            used_robots=4,
            cycle_range=cycle_range,
            cycle_standard_deviation=cycle_standard_deviation,
            cycle_cv=cycle_cv,
            cycle_gini=cycle_gini,
            work_time_range_ms=work_range,
            work_time_standard_deviation_ms=work_standard_deviation,
            work_time_cv=work_cv,
            max_robot_finish_at_ms=makespan,
        )
        for index, (
            makespan,
            cycle_range,
            cycle_standard_deviation,
            cycle_cv,
            cycle_gini,
            work_range,
            work_standard_deviation,
            work_cv,
        )
        in enumerate(
            (
                (58_000, 1, 0.8, 0.10, 0.08, 9_000, 4_000, 0.18),
                (60_000, 2, 1.0, 0.20, 0.10, 10_000, 5_000, 0.20),
                (62_000, 3, 1.2, 0.30, 0.12, 11_000, 6_000, 0.22),
            ),
            start=1,
        )
    ]

    result = PlanningComparisonService._operational_comparison(
        rule,
        agent_runs,
        PlanningComparisonRequest(agent_repeats=3, min_valid_agent_runs=3),
    )

    assert result.agent_median_physical_cycle_count_range == 2.0
    assert result.physical_cycle_count_range_improvement_pct == pytest.approx(
        88.8888889
    )
    assert result.agent_median_physical_cycle_count_standard_deviation == 1.0
    assert (
        result.physical_cycle_count_standard_deviation_improvement_pct == 87.5
    )
    assert result.agent_median_physical_cycle_count_coefficient_of_variation == 0.2
    assert result.physical_cycle_count_cv_improvement_pct == pytest.approx(86.6666667)
    assert result.agent_median_physical_cycle_count_gini_coefficient == 0.1
    assert result.physical_cycle_count_gini_improvement_pct == pytest.approx(
        86.6666667
    )
    assert result.agent_median_scheduled_work_time_range_ms == 10_000.0
    assert result.scheduled_work_time_range_improvement_pct == 87.5
    assert result.agent_median_scheduled_work_time_standard_deviation_ms == 5_000.0
    assert result.scheduled_work_time_standard_deviation_improvement_pct == pytest.approx(
        85.7142857
    )
    assert result.agent_median_scheduled_work_time_coefficient_of_variation == 0.2
    assert result.scheduled_work_time_cv_improvement_pct == pytest.approx(85.7142857)
    assert result.agent_median_max_robot_finish_at_ms == 60_000.0
    assert result.max_robot_finish_at_improvement_pct == 40.0

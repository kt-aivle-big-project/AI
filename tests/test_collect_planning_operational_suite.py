from scripts.collect_planning_operational_suite import (
    _describe,
    _dynamic_statistics,
    _lower_is_better_change,
    _relationship,
)


def test_describe_reports_five_run_dispersion() -> None:
    result = _describe([100, 110, 90, 100, 100])

    assert result["count"] == 5
    assert result["median"] == 100
    assert result["minimum"] == 90
    assert result["maximum"] == 110
    assert result["standard_deviation"] is not None


def test_change_is_descriptive_and_not_thresholded() -> None:
    assert _lower_is_better_change(100, 75) == 25
    assert _lower_is_better_change(100, 125) == -25


def test_relationship_exposes_speed_resource_tradeoff() -> None:
    relationship = _relationship(
        {
            "makespan_ms": 100,
            "used_robot_count": 1,
            "fleet_effort_robot_ms": 100,
            "total_distance_m": 100,
            "total_wait_ms": 0,
        },
        {
            "makespan_ms": 60,
            "used_robot_count": 2,
            "fleet_effort_robot_ms": 120,
            "total_distance_m": 110,
            "total_wait_ms": 0,
        },
    )

    assert relationship == "AGENT_FASTER_WITH_MORE_RESOURCES"


def test_dynamic_statistics_separates_human_review_from_exception() -> None:
    summary, _ = _dynamic_statistics(
        "RP01",
        "REPLAN",
        {
            "agent_runs": [
                {
                    "passed": True,
                    "agent_execution_applicable": True,
                    "replan_workflow_status": "plan_validated",
                    "failed_checks": [],
                },
                {
                    "passed": False,
                    "agent_execution_applicable": True,
                    "replan_workflow_status": "human_review",
                    "failed_checks": ["replan_plan_returned"],
                },
            ],
            "agent_statistics": {
                "requested_runs": 2,
                "valid_runs": 1,
                "invalid_runs": 1,
                "applicable_runs": 2,
            },
        },
    )

    assert summary["completed_agent_runs"] == 2
    assert summary["alternate_human_review_runs"] == 1
    assert summary["exception_runs"] == 0

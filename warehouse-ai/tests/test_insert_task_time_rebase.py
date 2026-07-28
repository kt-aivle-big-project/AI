from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import NaturalLanguageCommand, ScheduledTask
from app.planning import nodes
from app.planning.nodes import same_absolute_schedule
from app.planning.graph import run_planning
from app.services.scheduling import (
    rebase_preserved_task,
    rebase_time_step,
)
from tests.test_insert_task_base_plan import (
    install_persisted_plan_fakes,
    schedule_by_id,
)


PARENT_REFERENCE = datetime(2026, 7, 22, 5, 5, 26, tzinfo=UTC)
CHILD_REFERENCE = datetime(2026, 7, 22, 5, 7, 35, tzinfo=UTC)
STEP_SECONDS = 5


class FixedDateTime(datetime):
    current = PARENT_REFERENCE

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


def run_parent_and_child(monkeypatch):
    services = install_persisted_plan_fakes(monkeypatch)
    settings = nodes.get_settings()
    settings.time_step_seconds = STEP_SECONDS
    settings.max_mapf_time_steps = 720
    settings.freeze_horizon_seconds = 15
    settings.warehouse_timezone = "Asia/Seoul"
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    monkeypatch.setattr(nodes, "datetime", FixedDateTime)

    FixedDateTime.current = PARENT_REFERENCE
    parent = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-TIME-PARENT",
            conversation_id="CONV-TIME-REBASE",
            warehouse_id=1,
            received_at=PARENT_REFERENCE,
            text=(
                "내일 오전 9시부터 10시까지 W-001 작업을 처리하고, "
                "W-001이 완료되면 W-002 작업을 처리해줘. "
                "전체 계획을 가상 시뮬레이션해줘."
            ),
        )
    )
    FixedDateTime.current = CHILD_REFERENCE
    child = run_planning(
        NaturalLanguageCommand(
            command_id="CMD-TIME-CHILD",
            conversation_id="CONV-TIME-REBASE",
            warehouse_id=1,
            received_at=CHILD_REFERENCE,
            text=(
                "그 일정은 그대로 유지하고 급하게 W-003 작업을 지금 먼저 넣어줘. "
                "기존 작업은 중단하지 말고 이후 일정을 다시 맞춰서 "
                "가상 시뮬레이션해줘."
            ),
        )
    )
    return services, parent, child


def test_rebase_preserved_task_keeps_absolute_window_and_duration() -> None:
    task = ScheduledTask(
        task_id="W-001:move",
        work_id="W-001",
        robot_id="R-01",
        source_node=1,
        target_node=2,
        start_time_step=100,
        end_time_step=104,
    )
    rebased = rebase_preserved_task(
        task,
        parent_reference_time=PARENT_REFERENCE,
        child_reference_time=CHILD_REFERENCE,
        time_step_seconds=STEP_SECONDS,
    )
    assert rebased.start_time_step == 75
    assert rebased.end_time_step == 79
    assert rebased.planned_start_at == PARENT_REFERENCE + timedelta(seconds=500)
    assert rebased.planned_end_at == PARENT_REFERENCE + timedelta(seconds=520)


def test_rebase_time_step_uses_child_reference_and_rounds_up() -> None:
    # 500 seconds after the parent is 371 seconds after the child: ceil(74.2)=75.
    assert rebase_time_step(
        100,
        parent_reference_time=PARENT_REFERENCE,
        child_reference_time=CHILD_REFERENCE,
        time_step_seconds=STEP_SECONDS,
    ) == 75


def test_shift_detection_uses_absolute_time_not_relative_step() -> None:
    before = {
        "robot_id": "R-01",
        "start_time_step": 100,
        "end_time_step": 104,
    }
    rebased = {
        "robot_id": "R-01",
        "start_time_step": 75,
        "end_time_step": 79,
        "planned_start_at": (PARENT_REFERENCE + timedelta(seconds=500)).isoformat(),
        "planned_end_at": (PARENT_REFERENCE + timedelta(seconds=520)).isoformat(),
    }
    assert same_absolute_schedule(
        before,
        rebased,
        before_reference_time=PARENT_REFERENCE,
        after_reference_time=CHILD_REFERENCE,
        time_step_seconds=STEP_SECONDS,
    )


def test_shift_detection_flags_real_wall_clock_change() -> None:
    before = {
        "robot_id": "R-01",
        "start_time_step": 100,
        "end_time_step": 104,
    }
    shifted = {
        "robot_id": "R-01",
        "start_time_step": 76,
        "end_time_step": 80,
        "planned_start_at": (PARENT_REFERENCE + timedelta(seconds=505)).isoformat(),
        "planned_end_at": (PARENT_REFERENCE + timedelta(seconds=525)).isoformat(),
    }
    assert not same_absolute_schedule(
        before,
        shifted,
        before_reference_time=PARENT_REFERENCE,
        after_reference_time=CHILD_REFERENCE,
        time_step_seconds=STEP_SECONDS,
    )


def test_insert_is_a_new_static_simulation_with_new_reference(monkeypatch) -> None:
    services, parent, child = run_parent_and_child(monkeypatch)
    assert parent["simulation_id"] != child["simulation_id"]
    assert parent["optimization_plan"]["metadata"]["reference_time"] == (
        PARENT_REFERENCE.isoformat()
    )
    assert child["optimization_plan"]["metadata"]["reference_time"] == (
        CHILD_REFERENCE.isoformat()
    )
    assert services.redis.activation_count == 0


def test_preserved_wall_clock_times_survive_child_rebase(monkeypatch) -> None:
    _, parent, child = run_parent_and_child(monkeypatch)
    parent_daily = {row["task_id"]: row for row in parent["daily_schedule"]}
    child_daily = {row["task_id"]: row for row in child["daily_schedule"]}
    for task_id in ("W-001:move", "W-002:move"):
        assert child_daily[task_id]["planned_start_at"] == parent_daily[task_id][
            "planned_start_at"
        ]
        assert child_daily[task_id]["planned_end_at"] == parent_daily[task_id][
            "planned_end_at"
        ]


def test_preserved_relative_steps_are_rebased_for_child(monkeypatch) -> None:
    _, parent, child = run_parent_and_child(monkeypatch)
    before = schedule_by_id(parent)
    after = schedule_by_id(child)
    for task_id in ("W-001:move", "W-002:move"):
        expected = rebase_time_step(
            before[task_id]["start_time_step"],
            parent_reference_time=PARENT_REFERENCE,
            child_reference_time=CHILD_REFERENCE,
            time_step_seconds=STEP_SECONDS,
        )
        assert after[task_id]["start_time_step"] == expected
        assert after[task_id]["start_time_step"] < before[task_id][
            "start_time_step"
        ]


def test_relative_rebase_does_not_mark_preserved_tasks_shifted(monkeypatch) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    insertion = child["insertion_result"]
    assert set(insertion["preserved_task_ids"]) == {
        "W-001:move",
        "W-002:move",
    }
    assert insertion["shifted_task_ids"] == []
    assert child["plan_changes"] == []


def test_assignment_daily_schedule_and_simulation_share_child_steps(
    monkeypatch,
) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    optimization = schedule_by_id(child)
    simulation = {
        row["task_id"]: row for row in child["simulation"]["task_assignments"]
    }
    data_assignments = {
        row["task_id"]: row for row in child["data"]["task_assignments"]
    }
    daily = {row["task_id"]: row for row in child["daily_schedule"]}
    for task_id, assignment in optimization.items():
        assert simulation[task_id]["start_time_step"] == assignment[
            "start_time_step"
        ]
        assert simulation[task_id]["end_time_step"] == assignment["end_time_step"]
        assert data_assignments[task_id]["start_time_step"] == assignment[
            "start_time_step"
        ]
        assert daily[task_id]["start_time_step"] == assignment["start_time_step"]
        assert daily[task_id]["end_time_step"] == assignment["end_time_step"]


def test_preserved_route_and_assignment_use_same_start_axis(monkeypatch) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    assignments = schedule_by_id(child)
    route = next(
        row
        for row in child["collision_plan"]["routes"]
        if "W-001:move" in row["task_ids"]
    )
    assert route["waypoints"][0]["time_step"] == assignments["W-001:move"][
        "start_time_step"
    ]
    assert route["waypoints"][-1]["time_step"] == assignments["W-002:move"][
        "end_time_step"
    ]


def test_optimizer_simulation_and_data_makespan_match(monkeypatch) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    optimizer_makespan = child["optimization_plan"]["metadata"][
        "makespan_time_steps"
    ]
    assert child["simulation"]["makespan"] == optimizer_makespan
    assert child["simulation"]["metrics"]["makespan_time_steps"] == (
        optimizer_makespan
    )
    assert child["data"]["makespan"] == optimizer_makespan
    assert child["data"]["elapsed_until_completion_seconds"] == (
        optimizer_makespan * STEP_SECONDS
    )
    assert child["data"]["schedule_completion_at"] == child["simulation"][
        "metrics"
    ]["schedule_completion_at"]


def test_urgent_task_remains_asap_and_dependencies_are_preserved(
    monkeypatch,
) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    assignments = schedule_by_id(child)
    assert assignments["W-003:move"]["start_time_step"] == 0


def test_rebased_plan_keeps_finish_to_start_dependency(monkeypatch) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    assignments = schedule_by_id(child)
    assert assignments["W-001:move"]["end_time_step"] <= assignments[
        "W-002:move"
    ]["start_time_step"]
    assert child["task_dependencies"] == [
        {
            "predecessor_work_id": "W-001",
            "successor_work_id": "W-002",
            "dependency_type": "FINISH_TO_START",
            "lag_seconds": 0,
        }
    ]


def test_rebased_plan_keeps_parent_hard_window(monkeypatch) -> None:
    _, _, child = run_parent_and_child(monkeypatch)
    w1_constraint = next(
        row
        for row in child["interpretation"]["scheduled_task_constraints"]
        if row["work_id"] == "W-001"
    )
    assert w1_constraint["time_constraint_type"] == "HARD_WINDOW"

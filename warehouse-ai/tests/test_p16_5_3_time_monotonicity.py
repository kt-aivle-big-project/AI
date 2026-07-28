from app.models import ScheduledTask
from app.services.scheduling import reconcile_task_time_window


def test_routing_reconciliation_never_shrinks_pick_to_zero_duration() -> None:
    task = ScheduledTask(
        task_id="PICK-1",
        action="PICK",
        robot_id="R1",
        source_node=1,
        target_node=1,
        start_time_step=10,
        end_time_step=11,
    )

    start, end = reconcile_task_time_window(
        task,
        route_start_step=10,
        route_end_step=10,
    )

    assert start == 10
    assert end == 11


def test_routing_reconciliation_preserves_shifted_operation_duration() -> None:
    task = ScheduledTask(
        task_id="DROP-1",
        action="DROP",
        robot_id="R1",
        source_node=1,
        target_node=2,
        start_time_step=10,
        end_time_step=13,
    )

    start, end = reconcile_task_time_window(
        task,
        route_start_step=20,
        route_end_step=21,
    )

    assert start == 20
    assert end == 23

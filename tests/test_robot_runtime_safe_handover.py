from app.domain.schemas import RobotRuntime


def test_robot_runtime_accepts_spring_safe_handover_clock() -> None:
    runtime = RobotRuntime(
        robot_id="R001",
        robot_code="R001",
        status="WAITING",
        battery_pct=19,
        capacity_units=1,
        safe_handover_at_ms=0,
        sim_time_ms=12_000,
    )

    assert runtime.safe_handover_at_ms == 0
    assert runtime.sim_time_ms == 12_000


def test_robot_runtime_allows_initial_snapshot_without_handover_clock() -> None:
    runtime = RobotRuntime(
        robot_id="R002",
        robot_code="R002",
        status="IDLE",
        battery_pct=100,
        capacity_units=1,
        safe_handover_at_ms=None,
    )

    assert runtime.safe_handover_at_ms is None

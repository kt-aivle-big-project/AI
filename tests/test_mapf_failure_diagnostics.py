from __future__ import annotations

from types import SimpleNamespace

from app.domain.schemas import (
    MAPFValidationResult,
    RobotRuntimeOverride,
    RuntimePlanningOverrides,
    TerminalRelocationRecord,
    TerminalRelocationResult,
    TimedRobotRoute,
    TimedRouteStep,
    TrafficScheduleResult,
)
from app.graph import v9_planning


def _failed_low_battery_state() -> tuple[dict, TrafficScheduleResult]:
    schedule = TrafficScheduleResult(
        valid=True,
        routes=[
            TimedRobotRoute(
                robot_id="R248",
                steps=[
                    TimedRouteStep(
                        step_type="SERVICE",
                        start_at_ms=10_600,
                        end_at_ms=20_600,
                        node_id="C01",
                        task_id="TERMINAL-R248-CHARGE",
                        service_kind="CHARGE",
                    )
                ],
                finish_at_ms=20_600,
            )
        ],
        total_service_ms=10_000,
        makespan_ms=20_600,
    )
    state = {
        "simulation_id": "BE-RUN-138",
        "runtime_overrides": RuntimePlanningOverrides(
            robot_states=[
                RobotRuntimeOverride(
                    robot_id="R248",
                    current_node="R2_0",
                    status="low_battery",
                    battery_pct=20,
                    current_load_units=0,
                    active_task_id="2854",
                    sim_time_ms=10_600,
                )
            ],
            relocate_idle_robot_ids=["R248"],
        ),
        "terminal_relocation": TerminalRelocationResult(
            applied=True,
            relocations=[
                TerminalRelocationRecord(
                    robot_id="R248",
                    policy="CHARGE",
                    from_node="R2_0",
                    to_node="C01",
                    task_id="TERMINAL-R248-CHARGE",
                    reason="Low-battery robot returns after safe handover.",
                )
            ],
        ),
    }
    return state, schedule


def test_mapf_failure_diagnostics_identifies_low_battery_return() -> None:
    state, schedule = _failed_low_battery_state()
    validation = MAPFValidationResult(
        valid=False,
        errors=["Station CHARGE capacity=1 overlap: R248 100-200 vs R249 200-300."],
    )

    diagnostics = v9_planning._mapf_failure_diagnostics(state, schedule, validation)

    assert diagnostics["simulation_id"] == "BE-RUN-138"
    assert diagnostics["low_battery_robots"][0]["robot_id"] == "R248"
    assert diagnostics["low_battery_robots"][0]["current_node"] == "R2_0"
    assert diagnostics["terminal_relocations"][0]["to_node"] == "C01"
    assert diagnostics["mapf_routes"][0]["charge_task_ids"] == ["TERMINAL-R248-CHARGE"]
    assert "사용 시간이 다른 로봇과 겹쳤습니다" in diagnostics["operator_summary"]


def test_validator_logs_context_and_exposes_operator_message(monkeypatch) -> None:
    state, schedule = _failed_low_battery_state()
    validation = MAPFValidationResult(
        valid=False,
        errors=["R248 waits at unsafe node R2_0."],
    )
    state.update(
        {
            "traffic_schedule": schedule,
            "map_context": object(),
            "optimization_request": SimpleNamespace(max_edge_wait_ms=None),
            "execution_payload": object(),
            "graph_node_types": {},
        }
    )
    output: list[str] = []

    monkeypatch.setattr(
        v9_planning,
        "model_from_state",
        lambda current, key, _model: current[key],
    )
    monkeypatch.setattr(
        v9_planning,
        "MAPFPlanValidator",
        lambda: SimpleNamespace(validate=lambda **_kwargs: validation),
    )
    monkeypatch.setattr(v9_planning, "safe_console_print", output.append)

    update = v9_planning.mapf_plan_validator_node(state)

    assert update["workflow_status"] == "human_review"
    assert "배터리 부족 로봇 R248" in update["traffic_schedule"].conflicts[0]
    assert "안전 대기 노드가 아닌 위치" in update["traffic_schedule"].conflicts[0]
    assert update["traffic_schedule"].conflicts[1] == validation.errors[0]
    assert len(output) == 1
    assert '\"robot_id\": \"R248\"' in output[0]
    assert '\"to_node\": \"C01\"' in output[0]

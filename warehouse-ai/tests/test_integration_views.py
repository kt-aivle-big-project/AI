from app.services.integration_views import (
    build_debug_view,
    build_execution_status_view,
    build_planning_ui_view,
    build_simulation_view,
)


def _history() -> dict:
    return {
        "command_id": "CMD-1",
        "conversation_id": "CONV-1",
        "warehouse_id": 2,
        "status": "SIMULATION_COMPLETED",
        "simulation_id": "SIM-1",
        "plan_version": "PLAN-1",
        "resolved_execution_mode": "SIMULATE_ONLY",
        "result_summary": {},
    }


def _output() -> dict:
    return {
        "simulation_id": "SIM-1",
        "verification_decision": {"decision": "PASS"},
        "cuopt_plan": {
            "scheduled_tasks": [
                {"task_id": "T-1", "robot_id": "R-1"},
            ]
        },
        "collision_plan": {
            "engine": "PRIORITIZED_TIME_ASTAR",
            "time_step_seconds": 5,
            "routes": [
                {
                    "robot_id": "R-1",
                    "waypoints": [
                        {"node_id": 1, "time_step": 0},
                        {"node_id": 2, "time_step": 1},
                    ],
                }
            ],
        },
        "simulation": {
            "valid": True,
            "metrics": {
                "time_step_seconds": 5,
                "total_distance": 10.0,
            },
            "timeline": [{"robot_id": "R-1", "time_step": 0}],
        },
        "warnings": [],
        "errors": [],
    }


def test_planning_ui_view_contains_only_consumer_summary_and_links() -> None:
    view = build_planning_ui_view(_history(), _output())

    assert view.schema_version == "planning-ui.v1"
    assert view.command_id == "CMD-1"
    assert view.verification["decision"] == "PASS"
    assert view.summary["task_count"] == 1
    assert view.summary["route_count"] == 1
    assert view.resources.simulation_view == "/v1/simulations/SIM-1/view"
    assert view.resources.debug_view == "/v1/commands/CMD-1/debug"


def test_simulation_view_keeps_routes_without_internal_verification_evidence() -> None:
    run = {
        "simulation_id": "SIM-1",
        "command_id": "CMD-1",
        "warehouse_id": 2,
        "plan_version": "PLAN-1",
        "status": "SIMULATION_COMPLETED",
        "output_payload": _output(),
    }

    view = build_simulation_view(run)

    assert view.valid
    assert view.planner == "prioritized_time_astar"
    assert len(view.routes) == 1
    assert view.routes[0].steps[0].step_type == "MOVE"
    assert view.routes[0].finish_at_ms == 5000
    assert view.makespan_ms == 5000


def test_execution_status_view_combines_plan_approval_and_latest_dispatch() -> None:
    run = {
        "simulation_id": "SIM-1",
        "command_id": "CMD-1",
        "warehouse_id": 2,
        "plan_version": "PLAN-1",
        "status": "SIMULATION_COMPLETED",
        "output_payload": _output(),
    }
    approval = {"status": "APPROVED"}
    dispatch = {
        "dispatch_id": "DISPATCH-1",
        "gateway_dispatch_id": "GW-1",
        "status": "ROLLED_BACK",
        "gateway_cancel_confirmed": True,
        "result_summary": {
            "inventory_reservation_release": {
                "status": "RELEASED",
                "released_count": 1,
            }
        },
    }

    view = build_execution_status_view(
        run,
        approval=approval,
        dispatch=dispatch,
    )

    assert view.schema_version == "execution-status.v1"
    assert view.approval["status"] == "APPROVED"
    assert view.dispatch["status"] == "ROLLED_BACK"
    assert view.inventory_reservations["released_count"] == 1


def test_debug_view_preserves_full_stored_output() -> None:
    output = _output()
    view = build_debug_view(_history(), output)

    assert view.schema_version == "planning-debug.v1"
    assert view.output == output
    assert view.resources.plan_evidence == "/v1/commands/CMD-1/plan-evidence"


def test_public_views_hide_provider_diagnostics_inside_verification() -> None:
    history = _history()
    history["result_summary"] = {
        "status": "SIMULATION_SUCCESS",
        "answer": "완료",
    }
    output = _output()
    provider_warning = (
        'cuOpt 호출 실패로 CPU optimizer를 사용했습니다: '
        'CUOPT_SUBMIT_HTTP_400:{"error":"pickup_and_delivery_pairs invalid"}'
    )
    output["verification_decision"] = {
        "decision": "PASS_WITH_WARNING",
        "warning_findings": [provider_warning],
        "user_visible_warnings": [provider_warning],
        "blocking_findings": [],
    }

    planning_view = build_planning_ui_view(history, output)
    execution_view = build_execution_status_view(
        {
            "simulation_id": "SIM-1",
            "command_id": "CMD-1",
            "warehouse_id": 2,
            "plan_version": "PLAN-1",
            "status": "SIMULATION_SUCCESS",
            "output_payload": output,
        },
        approval=None,
        dispatch=None,
    )

    public_message = (
        "기본 최적화 엔진을 사용할 수 없어 "
        "대체 최적화 엔진으로 계획했습니다."
    )
    assert planning_view.verification["warning_findings"] == [public_message]
    assert planning_view.verification["user_visible_warnings"] == [public_message]
    assert execution_view.verification["warning_findings"] == [public_message]
    assert execution_view.verification["user_visible_warnings"] == [public_message]

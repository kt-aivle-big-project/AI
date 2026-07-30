from copy import deepcopy

from app.models import ResponseView
from app.services.response_view import (
    RESPONSE_SCHEMA_VERSION,
    resolve_response_view,
    shape_planning_response,
)


def full_response(report_detail_level: str = "STANDARD") -> dict:
    return {
        "status": "SIMULATION_SUCCESS",
        "message": "처리 결과 보고서를 생성했습니다.",
        "answer": "시뮬레이션을 완료했습니다.",
        "intent": "HYPOTHETICAL_SCENARIO",
        "command_id": "CMD-1",
        "conversation_id": "CONV-1",
        "plan_version": "PLAN-1",
        "simulation_id": "SIM-1",
        "plan_mode": "LOCAL_REPLAN",
        "report_detail_level": report_detail_level,
        "report_source": "template",
        "verification_decision": {
            "decision": "PASS",
            "summary": "검증 통과",
            "requires_replan": False,
            "replan_scope": "NO_REPLAN",
            "blocking_findings": [],
            "warning_findings": [],
            "confidence": 0.99,
        },
        "data": {
            "execution_mode": "SIMULATE_ONLY",
            "valid": True,
            "task_assignments": [
                {
                    "task_id": "T-CHARGE",
                    "work_id": "W-1",
                    "action": "CHARGE",
                    "robot_id": "R-1",
                    "source_node": 10,
                    "target_node": 20,
                    "start_time_step": 0,
                    "end_time_step": 3,
                    "schedule_status": "READY",
                    "charger_candidates": [{"charger_node": 20}],
                },
                {
                    "task_id": "T-PICK",
                    "work_id": "W-1",
                    "action": "PICK",
                    "robot_id": "R-1",
                    "source_node": 30,
                    "target_node": 30,
                    "start_time_step": 3,
                    "end_time_step": 5,
                    "schedule_status": "SCHEDULED",
                },
            ],
            "charger_selections": [
                {
                    "task_id": "T-CHARGE",
                    "robot_id": "R-1",
                    "selected_charger_node": 20,
                    "charger_cost": 1.0,
                    "selection_policy": "MIN_CONFIGURED_CHARGER_COST",
                    "charged_percent": 2.0,
                    "projected_final_battery": 20.0,
                    "charge_duration_seconds": 10,
                    "candidates": [{"charger_node": 20}],
                }
            ],
            "battery_by_robot": {"R-1": {"final_battery": 20.0}},
            "makespan_seconds": 25,
            "schedule_completion_at": "2026-07-24T06:00:00Z",
        },
        "execution_task_dependencies": [
            {
                "predecessor_task_id": "T-CHARGE",
                "successor_task_id": "T-PICK",
                "dependency_type": "FINISH_TO_START",
                "lag_seconds": 0,
                "source": "AUTO_CHARGING",
            }
        ],
        "schedule_validation": {
            "valid": True,
            "dependency_count": 1,
            "execution_dependency_count": 1,
            "execution_dependency_order": ["T-CHARGE", "T-PICK"],
            "execution_dependency_violations": [],
            "validated_after_routing": True,
        },
        "collision_plan": {
            "metadata": {
                "wait_evidence": [{"resolution": "WAIT"}],
                "resolution_events": [{"resolution": "WAIT"}],
                "reroute_count": 1,
            }
        },
        "simulation": {
            "valid": True,
            "total_distance": 12.5,
            "makespan": 5,
            "tardiness": 0,
            "conflict_count": 0,
        },
        "inventory_feasibility": {
            "status": "PASS",
            "valid": True,
            "partial_success": False,
            "item_results": [
                {
                    "operation_id": "OP-1",
                    "operation_type": "OUTBOUND",
                    "item_id": "E",
                    "requested_quantity_boxes": 30,
                    "planned_quantity_boxes": 30,
                    "available_quantity_boxes": 120,
                    "shortage_quantity_boxes": 0,
                    "status": "PASS",
                    "lot_allocations": [{"lot_id": "SECRET-DETAIL"}],
                }
            ],
            "warnings": [],
        },
        "gateway_dispatched": False,
        "dispatched_robot_count": 0,
        "dispatched_command_count": 0,
        "interpretation": {"internal": "large"},
        "optimization_plan": {"scheduled_tasks": [1, 2, 3]},
        "trace": [{"node": "internal"}],
        "warnings": [],
        "errors": [],
    }


def test_auto_uses_compact_for_standard_and_removes_heavy_sections():
    source = full_response("STANDARD")
    original = deepcopy(source)

    result = shape_planning_response(source, ResponseView.AUTO)

    assert result["response_view"] == "COMPACT"
    assert result["response_schema_version"] == RESPONSE_SCHEMA_VERSION
    assert result["result"]["metrics"]["total_distance"] == 12.5
    assert result["result"]["charging"][0]["selected_charger_node"] == 20
    assert result["result"]["schedule_validation"]["dependency_count"] == 1
    assert result["result"]["collision_resolution"]["reroute_count"] == 1
    assert result["result"]["inventory"]["items"][0]["item_id"] == "E"
    assert "interpretation" not in result
    assert "optimization_plan" not in result
    assert "trace" not in result
    assert "charger_candidates" not in result["result"]["assignments"][0]
    assert "lot_allocations" not in result["result"]["inventory"]["items"][0]
    assert source == original


def test_auto_keeps_full_response_for_debug():
    source = full_response("DEBUG")
    result = shape_planning_response(source, ResponseView.AUTO)

    assert result["response_view"] == "FULL"
    assert result["interpretation"] == {"internal": "large"}
    assert result["trace"] == [{"node": "internal"}]
    assert "response_view" not in source


def test_explicit_compact_can_override_debug():
    result = shape_planning_response(full_response("DEBUG"), ResponseView.COMPACT)
    assert result["response_view"] == "COMPACT"
    assert "trace" not in result


def test_explicit_full_can_override_standard():
    result = shape_planning_response(full_response("STANDARD"), "FULL")
    assert result["response_view"] == "FULL"
    assert result["optimization_plan"]["scheduled_tasks"] == [1, 2, 3]


def test_response_view_resolution_contract():
    assert resolve_response_view("AUTO", report_detail_level="SUMMARY") == ResponseView.COMPACT
    assert resolve_response_view("AUTO", report_detail_level="DEBUG") == ResponseView.FULL
    assert resolve_response_view("COMPACT", report_detail_level="DEBUG") == ResponseView.COMPACT
    assert resolve_response_view("FULL", report_detail_level="SUMMARY") == ResponseView.FULL
    assert (
        resolve_response_view("ROUTE_PLAN", report_detail_level="DEBUG")
        == ResponseView.ROUTE_PLAN
    )


def test_route_plan_request_preserves_clarification_contract():
    source = full_response("STANDARD")
    source.update(
        status="CLARIFICATION_REQUIRED",
        clarification={
            "clarification_id": "CL-1",
            "conversation_id": "CONV-1",
            "command_id": "CMD-1",
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "MISSING_TARGET",
            "question": "어느 작업을 대상으로 할까요?",
            "missing_fields": ["target_task_id"],
            "ambiguous_fields": [],
            "options": [
                {
                    "value": "W-1",
                    "label": "작업 1",
                }
            ],
            "original_text": "그 작업을 재계획해줘",
        },
    )

    result = shape_planning_response(source, ResponseView.ROUTE_PLAN)

    assert result["response_view"] == "COMPACT"
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["clarification"]["clarification_id"] == "CL-1"
    assert result["clarification"]["question"] == "어느 작업을 대상으로 할까요?"
    assert "original_text" not in result["clarification"]



def test_compact_sanitizes_provider_diagnostics_but_full_preserves_them():
    provider_warning = (
        "cuOpt 호출 실패로 CPU optimizer를 사용했습니다: "
        'CUOPT_SUBMIT_HTTP_400:{"error":"pickup_and_delivery_pairs invalid"}'
    )
    source = full_response("STANDARD")
    source["answer"] = (
        "계획을 완료했습니다.\n\n"
        "주의:\n"
        f"- {provider_warning}\n\n"
        "경고 내용을 확인한 뒤 계획을 사용해 주세요."
    )
    source["verification_decision"]["decision"] = "PASS_WITH_WARNING"
    source["verification_decision"]["warning_findings"] = [provider_warning]
    source["verification_decision"]["user_visible_warnings"] = [provider_warning]
    source["warnings"] = [provider_warning]

    public_message = (
        "기본 최적화 엔진을 사용할 수 없어 "
        "대체 최적화 엔진으로 계획했습니다."
    )

    compact = shape_planning_response(source, None)
    assert compact["response_view"] == "COMPACT"
    assert "CUOPT_SUBMIT_HTTP" not in compact["answer"]
    assert "pickup_and_delivery_pairs" not in compact["answer"]
    assert public_message in compact["answer"]
    assert compact["warnings"] == [public_message]
    assert compact["verification"]["warning_findings"] == [public_message]
    assert compact["verification"]["user_visible_warnings"] == [public_message]

    full = shape_planning_response(source, ResponseView.FULL)
    assert full["response_view"] == "FULL"
    assert "CUOPT_SUBMIT_HTTP_400" in full["answer"]
    assert full["warnings"] == [provider_warning]
    assert full["verification_decision"]["warning_findings"] == [provider_warning]

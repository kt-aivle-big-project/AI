from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import (
    NaturalLanguageCommand,
    ReportDetailLevel,
    UserReportSummary,
)
from app.planning import nodes
from app.services.user_reporting import (
    build_debug_report_payload,
    build_user_report_summary,
    determine_report_detail_level,
    llm_report_is_supported,
    render_user_report,
    report_payload_for_level,
    report_state_fingerprint,
)
from tests.test_reporting import report_state


REFERENCE_TIME = datetime(2026, 7, 22, 5, 33, 34, tzinfo=UTC)


def insert_task_state() -> dict:
    state = report_state()
    state["command"].update(
        {
            "command_id": "CMD-INSERT-REPORT",
            "text": "기존 일정은 유지하고 W-003 작업을 긴급하게 추가해줘",
            "requested_execution_mode": "SIMULATE_ONLY",
        }
    )
    state["interpretation"].update(
        {
            "intent": "INSERT_TASK",
            "execution_mode": "SIMULATE_ONLY",
            "insertion_policy": "URGENT",
            "daily_schedule_requested": True,
            "task_dependencies": [
                {
                    "predecessor_work_id": "W-001",
                    "successor_work_id": "W-002",
                    "dependency_type": "FINISH_TO_START",
                    "lag_seconds": 0,
                }
            ],
        }
    )
    state["supervisor_decision"].update(
        {"plan_mode": "INSERT_TASK", "reasoning_summary": "긴급 작업 추가"}
    )
    state["scope"] = {"plan_mode": "INSERT_TASK", "fixed_task_ids": []}
    assignments = [
        {
            "task_id": "W-003:move",
            "work_id": "W-003",
            "robot_id": "R-02",
            "source_node": 3,
            "target_node": 9,
            "start_time_step": 0,
            "end_time_step": 6,
            "planned_start_at": "2026-07-22T05:33:34+00:00",
            "planned_end_at": "2026-07-22T05:34:04+00:00",
            "schedule_status": "READY",
            "priority": 1,
        },
        {
            "task_id": "W-001:move",
            "work_id": "W-001",
            "robot_id": "R-01",
            "source_node": 1,
            "target_node": 5,
            "start_time_step": 13277,
            "end_time_step": 13281,
            "planned_start_at": "2026-07-23T00:00:03+00:00",
            "planned_end_at": "2026-07-23T00:00:23+00:00",
            "schedule_status": "SCHEDULED",
            "priority": 2,
        },
        {
            "task_id": "W-002:move",
            "work_id": "W-002",
            "robot_id": "R-01",
            "source_node": 5,
            "target_node": 8,
            "start_time_step": 13281,
            "end_time_step": 13285,
            "planned_start_at": "2026-07-23T00:00:23+00:00",
            "planned_end_at": "2026-07-23T00:00:43+00:00",
            "schedule_status": "WAITING_FOR_PREDECESSOR",
            "priority": 3,
        },
    ]
    state["cuopt_plan"] = {
        "scheduled_tasks": deepcopy(assignments),
        "unassigned_task_ids": [],
        "objective_value": 23.56,
        "metadata": {"reference_time": REFERENCE_TIME.isoformat()},
    }
    state["optimization_problem"] = {
        "reference_time": REFERENCE_TIME.isoformat(),
        "time_step_seconds": 5,
        "optimization_profile": "DEFAULT",
        "optimization_weight_source": "DEFAULT",
        "weights": {},
    }
    state["simulation"] = {
        "success": True,
        "valid": True,
        "status": "SUCCESS",
        "total_distance": 23.56,
        "makespan": 13286,
        "tardiness": 0,
        "conflict_count": 0,
        "warnings": ["DEFAULT_WAREHOUSE_TIMEZONE_USED"],
        "errors": [],
        "issues": [],
        "task_assignments": deepcopy(assignments),
        "robot_routes": [],
    }
    state["collision_plan"] = {
        "time_step_seconds": 5,
        "routes": [],
        "total_distance": 23.56,
    }
    state["verification_decision"] = {
        "decision": "PASS_WITH_WARNING",
        "summary": "계산 검증 완료",
        "user_visible_warnings": ["DEFAULT_WAREHOUSE_TIMEZONE_USED"],
    }
    state["ready_task_ids"] = ["W-003:move"]
    state["waiting_task_ids"] = ["W-002:move"]
    state["blocked_task_ids"] = []
    state["base_plan_source"] = "PARENT_SIMULATION_PLAN"
    state["base_plan_version"] = "PLAN-PARENT"
    state["plan_version"] = "PLAN-CHILD"
    state["replan_base_plan"] = {
        "reference_time": REFERENCE_TIME.isoformat(),
        "cuopt_plan": {"scheduled_tasks": deepcopy(assignments[1:])},
    }
    state["replan_history"] = []
    state["warnings"] = ["DEFAULT_WAREHOUSE_TIMEZONE_USED"]
    state["errors"] = []
    state["final_status"] = "SIMULATION_SUCCESS"
    return state


def insert_report_data(state: dict) -> dict:
    data = nodes.planning_report_data(state)
    # The report layer consumes the actual calculated fields; no report-side
    # recomputation or unit assumption is introduced here.
    assert data["active_work_duration_seconds"] == 70
    return data


def summary_for_insert(
    level: ReportDetailLevel = ReportDetailLevel.STANDARD,
) -> tuple[dict, dict, UserReportSummary]:
    state = insert_task_state()
    data = insert_report_data(state)
    summary = build_user_report_summary(
        state,
        data,
        report_level=level,
    )
    return state, data, summary


def test_single_task_success_defaults_to_summary() -> None:
    state = report_state()
    state["replan_history"] = []
    state["verification_decision"] = {"decision": "PASS", "summary": "통과"}
    state["simulation"]["warnings"] = []
    state["simulation"]["tardiness"] = 0
    data = nodes.planning_report_data(state)

    assert determine_report_detail_level(state, data) == ReportDetailLevel.SUMMARY


def test_simulate_only_report_labels_deadline_delay_as_expected() -> None:
    state = report_state()
    state["interpretation"]["execution_mode"] = "SIMULATE_ONLY"
    state["simulation"]["tardiness"] = 15
    data = nodes.planning_report_data(state)
    summary = build_user_report_summary(
        state,
        data,
        report_level=ReportDetailLevel.SUMMARY,
    )

    assert "예상 납기 지연: 15초" in render_user_report(summary)


def test_summary_assignment_grammar_is_natural() -> None:
    state = report_state()
    state["replan_history"] = []
    state["verification_decision"] = {"decision": "PASS", "summary": "통과"}
    state["simulation"]["warnings"] = []
    data = nodes.planning_report_data(state)
    summary = build_user_report_summary(
        state,
        data,
        report_level=ReportDetailLevel.SUMMARY,
    )
    answer = render_user_report(summary)

    assert "배정되었습니다으며" not in answer
    assert "배정되었으며" in answer


@pytest.mark.parametrize(
    "modifier",
    [
        "후보 점수까지 보여줘",
        "전체 이동 경로를 보여줘",
        "예약 정보를 보여줘",
        "검증 근거를 보여줘",
        "trace까지 보여줘",
        "개발자용으로 보여줘",
        "상세하게 보여줘",
    ],
)
def test_debug_modifiers_only_select_report_detail(modifier: str) -> None:
    state = report_state()
    state["command"]["text"] = f"W-003을 시뮬레이션해줘. {modifier}"
    data = nodes.planning_report_data(state)
    assert determine_report_detail_level(state, data) == ReportDetailLevel.DEBUG


def test_simulation_id_detail_query_selects_debug_report() -> None:
    state = report_state()
    state["command"]["text"] = (
        "simulation_id sim-2026-01 결과를 상세 조회해줘"
    )
    assert determine_report_detail_level(state, {}) == ReportDetailLevel.DEBUG


def test_multi_task_and_insert_task_default_to_standard() -> None:
    state = insert_task_state()
    data = insert_report_data(state)

    assert determine_report_detail_level(state, data) == ReportDetailLevel.STANDARD


@pytest.mark.parametrize(
    "phrase",
    [
        "상세하게 보여줘",
        "근거까지 보여줘",
        "디버그 정보 보여줘",
        "로봇 후보 점수도 보여줘",
        "전체 경로와 예약을 보여줘",
        "개발자용으로 보여줘",
    ],
)
def test_explicit_debug_phrases_select_debug(phrase: str) -> None:
    state = report_state()
    state["command"]["text"] = phrase
    assert determine_report_detail_level(state, {}) == ReportDetailLevel.DEBUG


@pytest.mark.parametrize("level", ["SUMMARY", "DEBUG"])
def test_explicit_api_report_level_overrides_default(level: str) -> None:
    state = insert_task_state()
    state["command"]["report_detail_level"] = level
    assert determine_report_detail_level(state, insert_report_data(state)).value == level


def test_existing_request_body_remains_compatible() -> None:
    command = NaturalLanguageCommand(
        warehouse_id=1,
        text="W-003 작업을 시뮬레이션해줘",
    )
    assert command.report_detail_level is None
    explicit = NaturalLanguageCommand(
        warehouse_id=1,
        text="W-003 작업을 시뮬레이션해줘",
        report_detail_level="DEBUG",
    )
    assert explicit.report_detail_level == ReportDetailLevel.DEBUG


def test_user_report_models_forbid_extra_fields() -> None:
    _, _, summary = summary_for_insert()
    with pytest.raises(ValidationError):
        UserReportSummary.model_validate(
            {**summary.model_dump(mode="json"), "unexpected": True}
        )


def test_success_and_warning_outcomes_are_deterministic() -> None:
    state, data, warning_summary = summary_for_insert()
    assert warning_summary.outcome == "SUCCESS_WITH_WARNING"
    state["warnings"] = []
    state["simulation"]["warnings"] = []
    state["verification_decision"] = {"decision": "PASS", "summary": "통과"}
    data = nodes.planning_report_data(state)
    success_summary = build_user_report_summary(
        state, data, report_level=ReportDetailLevel.STANDARD
    )
    assert success_summary.outcome == "SUCCESS"


def test_verification_failure_report_starts_with_problem_and_action() -> None:
    state, data, _ = summary_for_insert()
    state["verification_decision"] = {"decision": "FAIL", "summary": "경로 실패"}
    state["simulation"].update(
        {
            "valid": False,
            "success": False,
            "status": "FAILED",
            "errors": ["R-01에서 W-001 출발지까지 이동 가능한 경로가 없습니다."],
            "issues": [
                {
                    "code": "ROUTE_FAILED",
                    "message": "R-01에서 W-001 출발지까지 이동 가능한 경로가 없습니다.",
                }
            ],
        }
    )
    data = nodes.planning_report_data(state)
    summary = build_user_report_summary(
        state, data, report_level=ReportDetailLevel.STANDARD
    )
    answer = render_user_report(summary)
    assert summary.outcome == "FAILED"
    assert answer.startswith("계획을 완료하지 못했습니다.")
    assert answer.index("문제:") < answer.index("확인할 사항:")
    assert "이동 가능한 통로" in answer


def test_clarification_required_report_uses_question() -> None:
    state = report_state()
    state["clarification"] = {
        "reason_code": "AMBIGUOUS_TARGET",
        "question": "어떤 작업을 대상으로 할까요?",
    }
    summary = build_user_report_summary(
        state,
        {},
        report_level=ReportDetailLevel.SUMMARY,
        primary_message="어떤 작업을 대상으로 할까요?",
    )
    answer = render_user_report(summary)
    assert summary.outcome == "CLARIFICATION_REQUIRED"
    assert "어떤 작업을 대상으로 할까요?" in answer


@pytest.mark.parametrize(
    "forbidden",
    [
        "command_id",
        "prompt_version",
        "evidence_id",
        "vertex_reservation_count",
        "edge_reservation_count",
        "time_step",
        "objective_value",
    ],
)
def test_standard_report_hides_internal_evidence(forbidden: str) -> None:
    _, _, summary = summary_for_insert()
    assert forbidden not in render_user_report(summary)


def test_debug_report_keeps_organized_internal_evidence() -> None:
    state, _, summary = summary_for_insert(ReportDetailLevel.DEBUG)
    state["verification_evidence"] = [
        {"evidence_id": "E-REPORT", "code": "PASS"}
    ]
    state["reservation_evidence"] = {
        "vertex_reservation_count": 7,
        "edge_reservation_count": 5,
        "wait_count": 0,
        "waits": [],
    }
    evidence = nodes.build_report_evidence(state)
    payload = build_debug_report_payload(state, summary, evidence)
    answer = render_user_report(summary, debug_payload=payload)
    assert "command_id" in answer
    assert "prompt_version" in answer
    assert "E-REPORT" in answer
    assert "vertex_reservation_count" in answer
    assert answer.count("## 4. Optimization assignments") == 1


def test_standard_time_fields_have_distinct_meanings() -> None:
    _, _, summary = summary_for_insert()
    answer = render_user_report(summary)
    assert "가상 계획 완료 예상:" in answer
    assert "예상 작업 소요시간: 1분 10초" in answer
    assert "현재부터 완료까지:" in answer
    assert "전체 작업이 70초 만에 끝" not in answer
    assert "makespan" not in answer


def test_schedule_completion_is_rounded_to_seconds() -> None:
    _, _, summary = summary_for_insert()
    summary.schedule_completion_at = datetime(
        2026, 7, 23, 9, 0, 2, 569_714, tzinfo=summary.assignment_summaries[1].start_at.tzinfo
    )
    answer = render_user_report(summary)
    assert "2026년 7월 23일 오전 9시 00분 03초" in answer
    assert ".569714" not in answer


def test_report_completion_uses_latest_assignment_end_time() -> None:
    state = insert_task_state()
    data = insert_report_data(state)
    calculated_completion = "2026-07-23T00:00:46+00:00"
    data["schedule_completion_at"] = calculated_completion

    summary = build_user_report_summary(
        state,
        data,
        report_level=ReportDetailLevel.STANDARD,
    )
    answer = render_user_report(summary)

    assert data["schedule_completion_at"] == calculated_completion
    assert summary.schedule_completion_at == max(
        row.end_at
        for row in summary.assignment_summaries
        if row.end_at is not None
    )
    assert "가상 계획 완료 예상: 2026년 7월 23일 오전 9시 00분 43초" in answer
    assert "오전 9시 00분 46초" not in answer


def test_report_completion_falls_back_when_assignments_have_no_end_time() -> None:
    state = report_state()
    state["cuopt_plan"]["scheduled_tasks"] = []
    state["simulation"]["task_assignments"] = []
    data = nodes.planning_report_data(state)
    data["schedule_completion_at"] = "2026-07-23T00:00:46+00:00"

    summary = build_user_report_summary(
        state,
        data,
        report_level=ReportDetailLevel.SUMMARY,
    )

    assert summary.assignment_summaries == []
    assert summary.schedule_completion_at.isoformat() == (
        "2026-07-23T09:00:46+09:00"
    )


def test_missing_distance_unit_defaults_to_warehouse_map_meters() -> None:
    _, _, summary = summary_for_insert()
    assert summary.distance_unit == "m"
    answer = render_user_report(summary)
    assert "총 이동거리: 23.56 m" in answer


def test_simulate_only_summary_uses_virtual_simulation_language() -> None:
    state = report_state()
    state["replan_history"] = []
    state["verification_decision"] = {"decision": "PASS", "summary": "통과"}
    state["simulation"]["warnings"] = []
    data = nodes.planning_report_data(state)
    summary = build_user_report_summary(
        state, data, report_level=ReportDetailLevel.SUMMARY
    )

    answer = render_user_report(summary)

    assert "가상 시뮬레이션" in answer
    assert "처리했습니다" not in answer
    assert "작업 완료" not in answer


def test_simulate_only_partial_success_uses_virtual_operation_language() -> None:
    state = report_state()
    state["inventory_feasibility"] = {
        "status": "PARTIAL_SUCCESS",
        "valid": True,
        "partial_success": True,
        "item_results": [],
        "shortage_work_ids": ["W-002"],
        "blocked_work_ids": [],
        "independent_work_ids": ["W-001"],
        "warnings": [],
    }
    state["emergency_review_items"] = [
        {
            "item_id": "A",
            "work_id": "W-002",
            "requested_quantity_boxes": 2,
            "available_quantity_boxes": 1,
            "shortage_quantity_boxes": 1,
            "recommended_actions": [],
        }
    ]
    data = nodes.planning_report_data(state)
    summary = build_user_report_summary(
        state, data, report_level=ReportDetailLevel.STANDARD
    )

    assert summary.title == (
        "일부 작업의 가상 시뮬레이션이 완료되었으며 재고 확인이 필요합니다."
    )
    assert summary.primary_message == (
        "재고가 충분한 독립 작업은 가상 시뮬레이션했고, 부족 작업은 긴급 검토가 필요합니다."
    )


def test_execute_and_plan_only_keep_their_existing_report_language() -> None:
    execute_state = report_state()
    execute_state["interpretation"]["execution_mode"] = "EXECUTE"
    execute_data = nodes.planning_report_data(execute_state)
    execute_summary = build_user_report_summary(
        execute_state, execute_data, report_level=ReportDetailLevel.SUMMARY
    )
    assert execute_summary.primary_message == "검증된 작업 계획을 실행 단계로 전달했습니다."

    plan_state = report_state()
    plan_state["interpretation"]["execution_mode"] = "PLAN_ONLY"
    plan_data = nodes.planning_report_data(plan_state)
    plan_summary = build_user_report_summary(
        plan_state, plan_data, report_level=ReportDetailLevel.SUMMARY
    )
    assert plan_summary.primary_message == "작업 계획을 생성했습니다."


def test_explicit_distance_unit_is_rendered() -> None:
    state, data, _ = summary_for_insert()
    state["snapshot"] = {"graph": {"metadata": {"distance_unit": "m"}}}
    summary = build_user_report_summary(
        state, data, report_level=ReportDetailLevel.STANDARD
    )
    assert "총 이동거리: 23.56 m" in render_user_report(summary)


def test_dependency_and_waiting_status_are_localized() -> None:
    _, _, summary = summary_for_insert()
    answer = render_user_report(summary)
    assert "W-001이 완료된 후 W-002가 시작됩니다." in answer
    assert "선행 작업 완료 대기" in answer
    assert "FINISH_TO_START" not in answer
    assert "WAITING_FOR_PREDECESSOR" not in answer


def test_insert_task_changes_and_violations_are_reported() -> None:
    _, _, summary = summary_for_insert()
    answer = render_user_report(summary)
    assert "W-003 작업을 새로 추가했습니다." in answer
    assert "W-001, W-002의 기존 일정과 로봇 배정을 유지했습니다." in answer
    assert "일정이 변경된 기존 작업은 없습니다." in answer
    assert "차단된 작업은 없습니다." in answer
    assert "시간창 위반: 없음" in answer
    assert "납기 위반: 없음" in answer
    assert summary.schedule_change_summary.previous_plan_version == "PLAN-PARENT"
    assert "PLAN-PARENT" not in answer


def test_warning_code_is_hidden_but_message_is_visible_in_standard() -> None:
    _, _, summary = summary_for_insert()
    answer = render_user_report(summary)
    assert "창고 시간대가 별도로 설정되지 않아 Asia/Seoul 기준을 사용했습니다." in answer
    assert "DEFAULT_WAREHOUSE_TIMEZONE_USED" not in answer


def test_debug_payload_is_only_attached_for_debug_level() -> None:
    state, _, standard = summary_for_insert()
    evidence = nodes.build_report_evidence(state)
    debug_payload = build_debug_report_payload(state, standard, evidence)
    assert "debug_evidence" not in report_payload_for_level(
        standard, debug_payload=debug_payload
    )
    debug = standard.model_copy(update={"report_level": ReportDetailLevel.DEBUG})
    assert "debug_evidence" in report_payload_for_level(
        debug, debug_payload=debug_payload
    )


def test_llm_report_validation_rejects_changed_core_numbers_and_units() -> None:
    _, _, summary = summary_for_insert()
    valid = (
        "W-003 R-02, W-001 R-01, W-002 R-01 작업을 반영했습니다. "
        "시스템 기준 이동거리 23.56, 납기 지연 없음, 경로 충돌 없음입니다."
    )
    assert llm_report_is_supported(valid, summary)
    assert not llm_report_is_supported(valid.replace("23.56", "99.9"), summary)
    assert llm_report_is_supported(valid.replace("23.56", "23.56m"), summary)


def test_report_generation_does_not_mutate_planning_state(monkeypatch) -> None:
    state = insert_task_state()
    before = report_state_fingerprint(state)
    full_before = deepcopy(state)
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            report_with_llm=False,
            openai_api_key="",
            time_step_seconds=5,
            warehouse_timezone="Asia/Seoul",
        ),
    )
    update = nodes.generate_final_report_node(state)
    assert report_state_fingerprint(state) == before
    assert state == full_before
    report_trace = next(
        row for row in update["trace"] if row["node"] == "generate_final_report"
    )
    assert report_trace["planning_state_unchanged"] is True


def test_response_keeps_structured_evidence_and_adds_user_summary(monkeypatch) -> None:
    state = insert_task_state()
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            report_with_llm=False,
            openai_api_key="",
            time_step_seconds=5,
            warehouse_timezone="Asia/Seoul",
        ),
    )
    response = nodes.generate_final_report_node(state)["response"]
    for field in (
        "interpretation",
        "supervisor_decision",
        "verification_decision",
        "data",
        "simulation",
        "optimization_plan",
        "daily_schedule",
        "task_dependencies",
        "insertion_result",
        "collision_plan",
        "snapshot_summary",
        "evidence_summary",
        "trace",
        "plan_version",
        "simulation_id",
    ):
        assert field in response
    assert response["report_detail_level"] == "STANDARD"
    assert response["user_report_summary"]["assignment_summaries"]
    assert response["report_prompt_version"] == "user_report_v2"



def test_inventory_required_time_is_rendered_in_warehouse_timezone() -> None:
    state = report_state()
    state["snapshot"] = {
        "sql": {"warehouse": {"timezone": "Asia/Seoul"}},
        "graph": {},
    }
    state["inventory_feasibility"] = {
        "status": "PASS",
        "valid": True,
        "partial_success": False,
        "item_results": [
            {
                "operation_id": "OP-C-1",
                "operation_type": "OUTBOUND",
                "item_id": "C",
                "requested_quantity_boxes": 1,
                "planned_quantity_boxes": 1,
                "available_quantity_boxes": 60,
                "shortage_quantity_boxes": 0,
                "required_at": "2026-07-27T12:51:55Z",
                "status": "PASS",
            }
        ],
        "shortage_work_ids": [],
        "blocked_work_ids": [],
        "independent_work_ids": ["OP-C-1"],
        "warnings": [],
    }
    data = nodes.planning_report_data(state)
    summary = build_user_report_summary(
        state,
        data,
        report_level=ReportDetailLevel.STANDARD,
    )

    answer = render_user_report(summary)

    assert "필요 시각: 2026년 7월 27일 오후 9시 51분 55초" in answer
    assert "필요 시각: 2026년 7월 27일 오후 12시 51분 55초" not in answer

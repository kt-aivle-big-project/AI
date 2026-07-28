from app.models import AssignmentSummary, ReportDetailLevel, UserReportSummary
from app.services.user_reporting import (
    build_user_report_summary,
    render_standard_report,
    render_summary_report,
    render_user_report,
)


def test_global_replan_inserted_row_is_numbered() -> None:
    summary = UserReportSummary(
        report_level=ReportDetailLevel.STANDARD,
        outcome="SUCCESS",
        title="완료",
        primary_message="완료",
        execution_mode_label="가상 시뮬레이션",
        plan_mode_label="전체 일정 재계획",
        assignment_summaries=[
            AssignmentSummary(
                work_id="W-NEW",
                robot_id="R-1",
                status_code="READY",
                status_label="시뮬레이션 가능",
                is_inserted=True,
            )
        ],
    )

    answer = render_standard_report(summary)

    assert "| 1 | W-NEW |" in answer
    assert "| 긴급 |" not in answer


def test_insert_task_row_keeps_urgent_label() -> None:
    summary = UserReportSummary(
        report_level=ReportDetailLevel.STANDARD,
        outcome="SUCCESS",
        title="완료",
        primary_message="완료",
        execution_mode_label="가상 시뮬레이션",
        plan_mode_label="긴급 작업 추가",
        assignment_summaries=[
            AssignmentSummary(
                work_id="W-URGENT",
                robot_id="R-1",
                status_code="READY",
                status_label="시뮬레이션 가능",
                is_inserted=True,
            )
        ],
    )

    assert "| 긴급 | W-URGENT |" in render_standard_report(summary)


def test_command_report_excludes_unrelated_open_work_inventory() -> None:
    command_operation_id = "CMD-E-15"
    state = {
        "interpretation": {
            "command_kind": "PLAN",
            "intent": "HYPOTHETICAL_SCENARIO",
            "execution_mode": "SIMULATE_ONLY",
            "daily_schedule_requested": False,
            "load_open_inventory_orders": False,
            "inventory_operations": [
                {
                    "operation_id": command_operation_id,
                    "work_id": None,
                    "operation_type": "OUTBOUND",
                    "item_id": "E",
                    "quantity_boxes": 15,
                    "source": "COMMAND",
                },
                {
                    "operation_id": "work:OPEN-F",
                    "work_id": "OPEN-F",
                    "operation_type": "OUTBOUND",
                    "item_id": "F",
                    "quantity_boxes": 50,
                    "source": "WORK",
                },
            ],
        },
        "verification_decision": {"decision": "PASS"},
        "scope": {"plan_mode": "GLOBAL_REPLAN"},
        "final_status": "SIMULATION_SUCCESS",
    }
    inventory = {
        "status": "PASS",
        "valid": True,
        "partial_success": False,
        "item_results": [
            {
                "operation_id": command_operation_id,
                "work_id": None,
                "operation_type": "OUTBOUND",
                "item_id": "E",
                "requested_quantity_boxes": 15,
                "planned_quantity_boxes": 15,
                "available_quantity_boxes": 120,
                "shortage_quantity_boxes": 0,
                "status": "PASS",
            },
            {
                "operation_id": "work:OPEN-F",
                "work_id": "OPEN-F",
                "operation_type": "OUTBOUND",
                "item_id": "F",
                "requested_quantity_boxes": 50,
                "planned_quantity_boxes": 50,
                "available_quantity_boxes": 50,
                "shortage_quantity_boxes": 0,
                "status": "PASS",
            },
        ],
        "shortage_work_ids": [],
        "blocked_work_ids": [],
        "independent_work_ids": [command_operation_id, "OPEN-F"],
        "warnings": [],
    }
    data = {
        "execution_mode": "SIMULATE_ONLY",
        "plan_mode": "GLOBAL_REPLAN",
        "valid": True,
        "inventory_feasibility": inventory,
        "emergency_review_items": [
            {
                "item_id": "F",
                "work_id": "OPEN-F",
                "requested_quantity_boxes": 50,
                "available_quantity_boxes": 30,
                "shortage_quantity_boxes": 20,
                "recommended_actions": ["추가 재고 확보"],
            }
        ],
        "warnings": [],
        "errors": [],
        "task_assignments": [],
    }

    summary = build_user_report_summary(
        state, data, report_level=ReportDetailLevel.STANDARD
    )
    answer = render_user_report(summary)

    assert [row.item_id for row in summary.inventory_feasibility.item_results] == ["E"]
    assert summary.emergency_review_items == []
    assert "- E: 요청 15 BOX" in answer
    assert "- F:" not in answer


def test_summary_simulation_duration_does_not_repeat_approximation_prefix() -> None:
    from datetime import UTC, datetime, timedelta

    started = datetime(2026, 7, 25, 1, 0, tzinfo=UTC)
    summary = UserReportSummary(
        report_level=ReportDetailLevel.SUMMARY,
        outcome="SUCCESS",
        title="완료",
        primary_message="작업을 가상 시뮬레이션했습니다.",
        execution_mode_label="가상 시뮬레이션",
        plan_mode_label="초기 계획",
        assignment_summaries=[
            AssignmentSummary(
                work_id="W-001",
                robot_id="R-1",
                status_code="READY",
                status_label="시뮬레이션 가능",
                start_at=started,
                end_at=started + timedelta(seconds=60),
            )
        ],
    )

    answer = render_summary_report(summary)

    assert "약 1분" in answer
    assert "약 약" not in answer

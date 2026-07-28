from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import (
    AssignmentSummary,
    DependencySummary,
    EmergencyReviewItem,
    InventoryFeasibilityResult,
    ReportDetailLevel,
    ScheduleChangeSummary,
    UserReportSummary,
    UserVisibleIssue,
    UserVisibleWarning,
)


EXECUTION_MODE_LABELS = {
    "PLAN_ONLY": "계획만 생성",
    "SIMULATE_ONLY": "가상 시뮬레이션",
    "EXECUTE": "실제 실행",
}
PLAN_MODE_LABELS = {
    "INITIAL_PLAN": "신규 계획",
    "INSERT_TASK": "작업 추가",
    "LOCAL_REPLAN": "일부 일정 재조정",
    "GLOBAL_REPLAN": "전체 일정 재계획",
    "NO_REPLAN": "기존 계획 유지",
}
STATUS_LABELS = {
    "WAITING_FOR_PREDECESSOR": "선행 작업 완료 대기",
    "SCHEDULED": "예약됨",
    "READY": "실행 준비",
    "BLOCKED": "실행 차단",
    "EXECUTING": "실행 중",
    "COMPLETED": "완료",
    "FAILED": "실패",
}
DEPENDENCY_LABELS = {
    "FINISH_TO_START": "앞 작업이 끝난 후 시작",
}
WARNING_MESSAGES = {
    "DEFAULT_WAREHOUSE_TIMEZONE_USED": (
        "창고 시간대가 별도로 설정되지 않아 Asia/Seoul 기준을 사용했습니다."
    ),
    "CAPACITY_DATA_NOT_CONFIGURED": (
        "저장 공간 용량 정보가 등록되지 않아 재고 수량 검증만 수행했습니다. "
        "저장 공간 초과 여부는 확인하지 못했습니다."
    ),
}
ISSUE_ACTIONS = {
    "HARD_WINDOW_VIOLATION": "작업 시간창과 선행 일정을 확인해 주세요.",
    "ROUTE_FAILED": "출발 위치와 목적지가 이동 가능한 통로로 연결되어 있는지 확인해 주세요.",
    "NO_PATH": "폐쇄된 노드와 통로, 지도 연결 상태를 확인해 주세요.",
    "CLARIFICATION_REQUIRED": "필요한 정보를 선택하거나 입력해 주세요.",
}

DEBUG_REQUEST_PATTERNS = (
    "상세하게 보여줘",
    "상세히 보여줘",
    "상세 조회",
    "근거까지 보여줘",
    "디버그 정보 보여줘",
    "로봇 후보 점수도 보여줘",
    "전체 경로와 예약을 보여줘",
    "개발자용으로 보여줘",
    "후보 점수",
    "전체 이동 경로",
    "예약 정보",
    "검증 근거",
    "trace",
    "개발자용",
    "상세하게",
)

PROTECTED_REPORT_STATE_FIELDS = (
    "interpretation",
    "supervisor_decision",
    "scope",
    "required_tasks",
    "optimization_problem",
    "cuopt_plan",
    "collision_plan",
    "plan_validation",
    "simulation",
    "verification_decision",
    "warnings",
    "schedule_impact",
    "schedule_validation",
    "plan_version",
    "current_plan_version",
    "task_dependencies",
    "inventory_operations",
    "inventory_feasibility",
    "inventory_timeline_validation",
    "inventory_projection",
    "inventory_reservations",
    "capacity_feasibility",
    "resource_reservation_plan",
    "emergency_review_items",
)


def canonical_work_id(value: Any) -> str:
    return str(value or "").split(":", 1)[0]


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def report_state_fingerprint(state: dict[str, Any]) -> str:
    protected = {
        key: state.get(key)
        for key in PROTECTED_REPORT_STATE_FIELDS
        if key in state
    }
    serialized = json.dumps(
        protected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def determine_report_detail_level(
    state: dict[str, Any],
    data: dict[str, Any],
) -> ReportDetailLevel:
    command = state.get("command", {})
    explicit = command.get("report_detail_level")
    if explicit:
        return ReportDetailLevel(
            explicit.value if isinstance(explicit, ReportDetailLevel) else str(explicit)
        )

    text = str(command.get("text") or "").lower()
    if any(pattern in text for pattern in DEBUG_REQUEST_PATTERNS):
        return ReportDetailLevel.DEBUG

    interpretation = state.get("interpretation", {})
    plan_mode = (
        data.get("plan_mode")
        or state.get("scope", {}).get("plan_mode")
        or state.get("supervisor_decision", {}).get("plan_mode")
    )
    assignments = data.get("daily_schedule") or data.get("task_assignments") or []
    if (
        len(assignments) > 1
        or bool(data.get("task_dependencies"))
        or bool(interpretation.get("daily_schedule_requested"))
        or bool(interpretation.get("comparison_requested"))
        or plan_mode in {"INSERT_TASK", "LOCAL_REPLAN", "GLOBAL_REPLAN"}
        or bool(state.get("replan_history"))
        or bool(state.get("inventory_operations"))
        or bool(state.get("emergency_review_items"))
    ):
        return ReportDetailLevel.STANDARD
    return ReportDetailLevel.SUMMARY


def _distance_unit(state: dict[str, Any], data: dict[str, Any]) -> str | None:
    candidates = [data.get("distance_unit")]
    snapshot = state.get("snapshot", {})
    graph = snapshot.get("graph", {}) if isinstance(snapshot, dict) else {}
    sql = snapshot.get("sql", {}) if isinstance(snapshot, dict) else {}
    candidates.extend(
        [
            graph.get("distance_unit") if isinstance(graph, dict) else None,
            (graph.get("metadata") or {}).get("distance_unit")
            if isinstance(graph, dict)
            and isinstance(graph.get("metadata"), dict)
            else None,
            (sql.get("warehouse") or {}).get("distance_unit")
            if isinstance(sql, dict)
            and isinstance(sql.get("warehouse"), dict)
            else None,
        ]
    )
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value).strip()
    # Routing distances are calculated on the warehouse map in meters. Keep
    # the report unit explicit even when older snapshots omit metadata.
    return "m"


def _warehouse_timezone(
    state: dict[str, Any], data: dict[str, Any]
) -> ZoneInfo | None:
    schedule_rows = data.get("daily_schedule") or []
    names = [
        row.get("timezone")
        for row in schedule_rows
        if isinstance(row, dict) and row.get("timezone")
    ]
    snapshot = state.get("snapshot", {})
    graph = snapshot.get("graph", {}) if isinstance(snapshot, dict) else {}
    sql = snapshot.get("sql", {}) if isinstance(snapshot, dict) else {}
    if isinstance(graph, dict):
        names.extend(
            value
            for value in (
                graph.get("timezone"),
                (graph.get("metadata") or {}).get("timezone")
                if isinstance(graph.get("metadata"), dict)
                else None,
            )
            if value
        )
    if isinstance(sql, dict) and isinstance(sql.get("warehouse"), dict):
        if sql["warehouse"].get("timezone"):
            names.append(sql["warehouse"]["timezone"])
    for name in names:
        try:
            return ZoneInfo(str(name))
        except ZoneInfoNotFoundError:
            continue
    return ZoneInfo("Asia/Seoul")


def _localized_datetime(value: Any, timezone: ZoneInfo | None) -> datetime | None:
    result = _as_datetime(value)
    if result is not None and timezone is not None:
        return result.astimezone(timezone)
    return result


def _localized_inventory_feasibility(
    result: InventoryFeasibilityResult | None,
    timezone: ZoneInfo | None,
) -> InventoryFeasibilityResult | None:
    if result is None:
        return None
    return result.model_copy(
        update={
            "item_results": [
                row.model_copy(
                    update={
                        "required_at": _localized_datetime(row.required_at, timezone),
                        "earliest_full_fulfillment_at": _localized_datetime(
                            row.earliest_full_fulfillment_at,
                            timezone,
                        ),
                    }
                )
                for row in result.item_results
            ]
        }
    )


def _localized_emergency_items(
    items: list[EmergencyReviewItem],
    timezone: ZoneInfo | None,
) -> list[EmergencyReviewItem]:
    return [
        row.model_copy(
            update={
                "required_at": _localized_datetime(row.required_at, timezone),
                "earliest_full_fulfillment_at": _localized_datetime(
                    row.earliest_full_fulfillment_at,
                    timezone,
                ),
            }
        )
        for row in items
    ]


def _status_for_assignment(state: dict[str, Any], row: dict[str, Any]) -> str:
    task_id = str(row.get("task_id") or row.get("work_id") or "")
    work_id = canonical_work_id(row.get("work_id") or task_id)
    provided = str(row.get("schedule_status") or "").upper()
    if provided:
        return provided
    status_sets = (
        ("BLOCKED", state.get("blocked_task_ids", [])),
        ("WAITING_FOR_PREDECESSOR", state.get("waiting_task_ids", [])),
        ("READY", state.get("ready_task_ids", [])),
    )
    for status, values in status_sets:
        normalized = {str(value) for value in values}
        if task_id in normalized or work_id in normalized:
            return status
    return "SCHEDULED"


def _dependency_summaries(data: dict[str, Any]) -> list[DependencySummary]:
    results: list[DependencySummary] = []
    for row in data.get("task_dependencies", []):
        relationship = str(
            row.get("dependency_type") or row.get("relationship") or "FINISH_TO_START"
        )
        predecessor = canonical_work_id(row.get("predecessor_work_id"))
        successor = canonical_work_id(row.get("successor_work_id"))
        if not predecessor or not successor:
            continue
        results.append(
            DependencySummary(
                predecessor_work_id=predecessor,
                successor_work_id=successor,
                relationship_label=DEPENDENCY_LABELS.get(
                    relationship,
                    "선행 작업 조건",
                ),
            )
        )
    return results


def _warning(code_or_message: Any) -> UserVisibleWarning | None:
    text = str(code_or_message or "").strip()
    if not text:
        return None
    if text in WARNING_MESSAGES:
        return UserVisibleWarning(code=text, message=WARNING_MESSAGES[text])
    return UserVisibleWarning(code="WARNING", message=text)


def _issue(code: str, message: str) -> UserVisibleIssue:
    normalized_code = code or "PROCESSING_ERROR"
    return UserVisibleIssue(
        code=normalized_code,
        message=message or "처리 중 확인이 필요한 문제가 발생했습니다.",
        action=ISSUE_ACTIONS.get(
            normalized_code,
            "관련 작업, 로봇 상태와 창고 지도를 확인한 뒤 다시 요청해 주세요.",
        ),
    )


def _collect_issues(state: dict[str, Any], data: dict[str, Any]) -> list[UserVisibleIssue]:
    results: list[UserVisibleIssue] = []
    simulation = state.get("simulation") or state.get("plan_validation") or {}
    for row in simulation.get("issues", []):
        if isinstance(row, dict):
            results.append(
                _issue(str(row.get("code") or "PROCESSING_ERROR"), str(row.get("message") or ""))
            )
        elif row:
            results.append(_issue("PROCESSING_ERROR", str(row)))
    for message in data.get("errors", []) or state.get("errors", []):
        text = str(message).strip()
        if not text:
            continue
        upper = text.upper()
        code = next(
            (
                candidate
                for candidate in ISSUE_ACTIONS
                if candidate in upper
            ),
            "PROCESSING_ERROR",
        )
        results.append(_issue(code, text))
    clarification = state.get("clarification")
    if clarification:
        results.append(
            UserVisibleIssue(
                code=str(clarification.get("reason_code") or "CLARIFICATION_REQUIRED"),
                message=str(clarification.get("question") or "추가 정보가 필요합니다."),
                action="필요한 정보를 선택하거나 입력해 주세요.",
            )
        )
    unique: dict[tuple[str, str], UserVisibleIssue] = {}
    for row in results:
        unique[(row.code, row.message)] = row
    return list(unique.values())


def _schedule_change_summary(
    state: dict[str, Any], data: dict[str, Any]
) -> ScheduleChangeSummary | None:
    plan_mode = data.get("plan_mode") or state.get("scope", {}).get("plan_mode")
    if plan_mode not in {"INSERT_TASK", "LOCAL_REPLAN", "GLOBAL_REPLAN"}:
        return None
    impact = data.get("insertion_result", {})
    return ScheduleChangeSummary(
        inserted_work_ids=_unique(
            [canonical_work_id(value) for value in impact.get("inserted_task_ids", [])]
        ),
        preserved_work_ids=_unique(
            [canonical_work_id(value) for value in impact.get("preserved_task_ids", [])]
        ),
        shifted_work_ids=_unique(
            [canonical_work_id(value) for value in impact.get("shifted_task_ids", [])]
        ),
        blocked_work_ids=_unique(
            [canonical_work_id(value) for value in impact.get("blocked_task_ids", [])]
        ),
        previous_plan_version=impact.get("previous_plan_version"),
        new_plan_version=impact.get("new_plan_version"),
        hard_window_violation=bool(impact.get("hard_window_violation")),
        deadline_violation=bool(impact.get("deadline_violation")),
    )



def _report_inventory_scope(
    state: dict[str, Any],
) -> tuple[set[str], set[str], set[str]] | None:
    """Return the explicit command inventory scope for user-facing reports.

    Planning may load unrelated open WORK/SQL_ORDER operations into the same
    snapshot. For a single natural-language inventory command, the standard
    report should describe the command operation instead of mixing in existing
    warehouse work. Daily-plan and explicit open-order requests remain unfiltered.
    """

    interpretation = state.get("interpretation", {})
    if interpretation.get("daily_schedule_requested") or interpretation.get(
        "load_open_inventory_orders"
    ):
        return None

    operations = interpretation.get("inventory_operations") or []
    command_operations = [
        row for row in operations if str(row.get("source") or "") == "COMMAND"
    ]
    if not command_operations:
        return None

    operation_ids = {
        str(row.get("operation_id"))
        for row in command_operations
        if row.get("operation_id")
    }
    work_ids = {
        str(row.get("work_id"))
        for row in command_operations
        if row.get("work_id")
    }
    item_ids = {
        str(row.get("item_id"))
        for row in command_operations
        if row.get("item_id")
    }
    return operation_ids, work_ids, item_ids


def _filter_inventory_for_report(
    state: dict[str, Any],
    result: InventoryFeasibilityResult | None,
) -> InventoryFeasibilityResult | None:
    scope = _report_inventory_scope(state)
    if result is None or scope is None:
        return result

    operation_ids, work_ids, _ = scope
    item_results = [
        row
        for row in result.item_results
        if row.operation_id in operation_ids
        or (row.work_id is not None and row.work_id in work_ids)
    ]
    # Preserve the original result when identifiers are unavailable or do not
    # match. This keeps legacy data and query-only reports backward compatible.
    if not item_results:
        return result

    shortage_rows = [row for row in item_results if row.shortage_quantity_boxes > 0]
    successful_rows = [row for row in item_results if row.planned_quantity_boxes > 0]
    if not shortage_rows:
        status = "PASS"
        partial_success = False
    elif successful_rows:
        status = "PARTIAL_SUCCESS"
        partial_success = True
    else:
        status = "FAILED"
        partial_success = False

    scoped_ids = operation_ids | work_ids
    return result.model_copy(
        update={
            "status": status,
            "partial_success": partial_success,
            "item_results": item_results,
            "shortage_work_ids": [
                value for value in result.shortage_work_ids if value in scoped_ids
            ],
            "blocked_work_ids": [
                value for value in result.blocked_work_ids if value in scoped_ids
            ],
            "independent_work_ids": [
                value for value in result.independent_work_ids if value in scoped_ids
            ],
        }
    )


def _filter_emergency_items_for_report(
    state: dict[str, Any],
    items: list[EmergencyReviewItem],
) -> list[EmergencyReviewItem]:
    scope = _report_inventory_scope(state)
    if scope is None:
        return items

    operation_ids, work_ids, item_ids = scope
    scoped_ids = operation_ids | work_ids
    filtered = [
        row
        for row in items
        if (row.work_id is not None and row.work_id in scoped_ids)
        or row.item_id in item_ids
    ]
    return filtered

def build_user_report_summary(
    state: dict[str, Any],
    data: dict[str, Any],
    *,
    report_level: ReportDetailLevel,
    primary_message: str | None = None,
) -> UserReportSummary:
    verification = state.get("verification_decision", {})
    verification_decision = verification.get("decision")
    warnings: list[UserVisibleWarning] = []
    raw_warnings = list(data.get("warnings", []))
    raw_warnings.extend(verification.get("user_visible_warnings", []))
    for value in raw_warnings:
        converted = _warning(value)
        if converted is not None:
            warnings.append(converted)
    warning_map = {(row.code, row.message): row for row in warnings}
    warnings = list(warning_map.values())
    issues = _collect_issues(state, data)
    inventory_raw = data.get("inventory_feasibility") or state.get(
        "inventory_feasibility"
    )
    inventory_feasibility = (
        InventoryFeasibilityResult.model_validate(inventory_raw)
        if inventory_raw
        else None
    )
    inventory_feasibility = _filter_inventory_for_report(
        state, inventory_feasibility
    )
    emergency_review_items = [
        EmergencyReviewItem.model_validate(row)
        for row in (
            data.get("emergency_review_items")
            or state.get("emergency_review_items", [])
        )
    ]
    emergency_review_items = _filter_emergency_items_for_report(
        state, emergency_review_items
    )

    if state.get("clarification"):
        outcome = "CLARIFICATION_REQUIRED"
    elif (
        inventory_feasibility is not None
        and inventory_feasibility.status == "FAILED"
    ):
        outcome = "FAILED"
    elif (
        verification_decision in {"FAIL", "CLARIFICATION_REQUIRED"}
        or data.get("valid") is False
        or bool(issues)
        or state.get("final_status") in {"ROUTE_FAILED", "VALIDATION_FAILED"}
    ):
        outcome = "FAILED"
    elif (
        inventory_feasibility is not None
        and inventory_feasibility.status == "PARTIAL_SUCCESS"
    ):
        outcome = "PARTIAL_SUCCESS_WITH_EMERGENCY"
    elif verification_decision == "PASS_WITH_WARNING" or warnings:
        outcome = "SUCCESS_WITH_WARNING"
    else:
        outcome = "SUCCESS"

    execution_mode = data.get("execution_mode") or state.get("interpretation", {}).get(
        "execution_mode"
    )
    is_simulate_only = execution_mode == "SIMULATE_ONLY"
    dependencies = _dependency_summaries(data)
    warehouse_timezone = _warehouse_timezone(state, data)
    inventory_feasibility = _localized_inventory_feasibility(
        inventory_feasibility,
        warehouse_timezone,
    )
    emergency_review_items = _localized_emergency_items(
        emergency_review_items,
        warehouse_timezone,
    )
    dependency_by_successor = {
        row.successor_work_id: row.relationship_label for row in dependencies
    }
    changes = _schedule_change_summary(state, data)
    inserted = set(changes.inserted_work_ids if changes else [])
    preserved = set(changes.preserved_work_ids if changes else [])
    shifted = set(changes.shifted_work_ids if changes else [])
    assignments: list[AssignmentSummary] = []
    schedule_rows = data.get("daily_schedule") or data.get("task_assignments") or []
    for row in schedule_rows:
        work_id = canonical_work_id(row.get("work_id") or row.get("task_id"))
        if not work_id:
            continue
        status = _status_for_assignment(state, row)
        assignments.append(
            AssignmentSummary(
                work_id=work_id,
                robot_id=(str(row.get("robot_id")) if row.get("robot_id") else None),
                start_at=_localized_datetime(
                    row.get("local_start_at") or row.get("planned_start_at"),
                    warehouse_timezone,
                ),
                end_at=_localized_datetime(
                    row.get("local_end_at") or row.get("planned_end_at"),
                    warehouse_timezone,
                ),
                status_code=status,
                status_label=(
                    "시뮬레이션 가능"
                    if is_simulate_only and status == "READY"
                    else STATUS_LABELS.get(status, status or "상태 확인 필요")
                ),
                dependency_label=dependency_by_successor.get(work_id),
                is_inserted=work_id in inserted,
                is_preserved=work_id in preserved,
                is_shifted=work_id in shifted,
            )
        )
    assignment_end_times = [
        row.end_at for row in assignments if row.end_at is not None
    ]
    report_schedule_completion_at = (
        max(assignment_end_times)
        if assignment_end_times
        else _localized_datetime(
            data.get("schedule_completion_at"), warehouse_timezone
        )
    )

    plan_mode = data.get("plan_mode") or state.get("scope", {}).get("plan_mode")
    interpretation = state.get("interpretation", {})
    urgent_insert = (
        plan_mode == "INSERT_TASK"
        and (
            str(interpretation.get("insertion_policy") or "").upper() == "URGENT"
            or str(interpretation.get("priority") or "").upper() == "EMERGENCY"
        )
    )
    if primary_message is None:
        if outcome == "CLARIFICATION_REQUIRED":
            primary_message = str(
                state.get("clarification", {}).get("question")
                or "계속 진행하려면 추가 정보가 필요합니다."
            )
        elif outcome == "FAILED":
            unknown_item_ids = list(
                data.get("inventory_unknown_item_ids")
                or state.get("inventory_unknown_item_ids", [])
            )
            no_known_inventory_data = bool(emergency_review_items) and all(
                item.available_quantity_boxes == 0
                and item.earliest_full_fulfillment_at is None
                for item in emergency_review_items
            )
            primary_message = (
                f"{', '.join(unknown_item_ids)} 품목은 시스템에 등록되지 않아 "
                "작업 계획을 생성하지 않았습니다."
                if unknown_item_ids
                else "현재 등록된 가용 재고 또는 입고 예정 정보가 없어 작업을 계획하지 못했습니다."
                if no_known_inventory_data
                else "재고 부족으로 요청된 작업을 계획하지 못했습니다."
                if emergency_review_items
                else "계획을 완료하지 못했습니다."
            )
        elif outcome == "PARTIAL_SUCCESS_WITH_EMERGENCY":
            primary_message = (
                "재고가 충분한 독립 작업은 가상 시뮬레이션했고, 부족 작업은 긴급 검토가 필요합니다."
                if is_simulate_only
                else "재고가 충분한 독립 작업은 처리했고, 부족 작업은 긴급 검토가 필요합니다."
            )
        elif plan_mode == "INSERT_TASK" and changes and changes.inserted_work_ids:
            label = "긴급 작업" if urgent_insert else "작업"
            primary_message = (
                f"{label} {', '.join(changes.inserted_work_ids)}을 기존 일정에 추가했습니다."
            )
        elif execution_mode == "SIMULATE_ONLY":
            primary_message = "작업을 가상 시뮬레이션했습니다."
        elif execution_mode == "PLAN_ONLY":
            primary_message = "작업 계획을 생성했습니다."
        elif execution_mode == "EXECUTE":
            primary_message = "검증된 작업 계획을 실행 단계로 전달했습니다."
        else:
            primary_message = "요청 처리가 완료되었습니다."

    if outcome == "FAILED":
        unknown_item_ids = list(
            data.get("inventory_unknown_item_ids")
            or state.get("inventory_unknown_item_ids", [])
        )
        title = (
            "미등록 품목으로 계획을 생성하지 않았습니다."
            if unknown_item_ids
            else "비상 재고 확인이 필요합니다."
            if emergency_review_items
            else "계획을 완료하지 못했습니다."
        )
    elif outcome == "PARTIAL_SUCCESS_WITH_EMERGENCY":
        title = (
            "일부 작업의 가상 시뮬레이션이 완료되었으며 재고 확인이 필요합니다."
            if is_simulate_only
            else "일부 작업 완료 및 비상 재고 확인이 필요합니다."
        )
    elif outcome == "CLARIFICATION_REQUIRED":
        title = "추가 정보가 필요합니다."
    elif plan_mode == "INSERT_TASK":
        title = (
            "긴급 작업을 일정에 반영했습니다."
            if urgent_insert
            else "새 작업을 일정에 반영했습니다."
        )
    else:
        title = primary_message

    command_kind = str(state.get("interpretation", {}).get("command_kind") or "")
    if outcome in {"FAILED", "CLARIFICATION_REQUIRED"}:
        unknown_item_ids = list(
            data.get("inventory_unknown_item_ids")
            or state.get("inventory_unknown_item_ids", [])
        )
        recommended_action = (
            "필요하면 품목을 등록하거나 등록된 품목 ID로 새 계획을 요청해 주세요."
            if unknown_item_ids
            else issues[0].action
            if issues
            else "입력 조건을 확인해 주세요."
        )
    elif command_kind == "QUERY" and warnings:
        recommended_action = "조회 결과의 주의사항을 확인해 주세요."
    elif command_kind == "QUERY":
        recommended_action = None
    elif emergency_review_items:
        recommended_action = (
            "재고 부족 작업은 긴급 검토 목록에서 보충 입고 또는 수량 조정을 결정해 주세요."
        )
    elif warnings:
        recommended_action = "경고 내용을 확인한 뒤 계획을 사용해 주세요."
    else:
        recommended_action = "현재 계산 결과를 기준으로 계획을 사용할 수 있습니다."

    return UserReportSummary(
        report_level=report_level,
        outcome=outcome,
        title=title,
        primary_message=primary_message,
        execution_mode_label=EXECUTION_MODE_LABELS.get(str(execution_mode)),
        plan_mode_label=(
            "긴급 작업 추가"
            if urgent_insert
            else PLAN_MODE_LABELS.get(str(plan_mode))
        ),
        assignment_summaries=assignments,
        dependency_summaries=dependencies,
        schedule_change_summary=changes,
        total_distance=(
            float(data["total_distance"]) if data.get("total_distance") is not None else None
        ),
        distance_unit=_distance_unit(state, data),
        schedule_completion_at=report_schedule_completion_at,
        active_work_duration_seconds=(
            int(data["active_work_duration_seconds"])
            if data.get("active_work_duration_seconds") is not None
            else None
        ),
        elapsed_until_completion_seconds=(
            int(data["elapsed_until_completion_seconds"])
            if data.get("elapsed_until_completion_seconds") is not None
            else None
        ),
        tardiness_seconds=(
            data.get("tardiness_seconds")
            if data.get("tardiness_seconds") is not None
            else data.get("tardiness")
        ),
        conflict_count=(
            int(data["conflict_count"])
            if data.get("conflict_count") is not None
            else None
        ),
        warnings=warnings,
        issues=issues,
        inventory_feasibility=inventory_feasibility,
        emergency_review_items=emergency_review_items,
        recommended_action=recommended_action,
    )


def _rounded_second(value: datetime) -> datetime:
    if value.microsecond >= 500_000:
        value += timedelta(seconds=1)
    return value.replace(microsecond=0)


def format_datetime_ko(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = _rounded_second(value)
    period = "오전" if value.hour < 12 else "오후"
    hour = value.hour % 12 or 12
    return (
        f"{value.year}년 {value.month}월 {value.day}일 "
        f"{period} {hour}시 {value.minute:02d}분 {value.second:02d}초"
    )


def format_duration_ko(seconds: int | float | None, *, approximate: bool = False) -> str | None:
    if seconds is None:
        return None
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    if secs or not parts:
        parts.append(f"{secs}초")
    prefix = "약 " if approximate and total > 0 else ""
    return prefix + " ".join(parts)


def _number(value: int | float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}".rstrip("0").rstrip(".")


def _distance_text(summary: UserReportSummary) -> str | None:
    if summary.total_distance is None:
        return None
    unit = f" {summary.distance_unit}" if summary.distance_unit else ""
    label = "총 이동거리" if summary.distance_unit else "시스템 기준 이동거리"
    return f"{label}: {_number(summary.total_distance)}{unit}"


def _tardiness_text(summary: UserReportSummary) -> str | None:
    if summary.tardiness_seconds is None:
        return None
    label = (
        "예상 납기 지연"
        if summary.execution_mode_label == "가상 시뮬레이션"
        else "납기 지연"
    )
    if float(summary.tardiness_seconds) <= 0:
        return f"{label}: 없음"
    return f"{label}: {format_duration_ko(summary.tardiness_seconds)}"


def _conflict_text(summary: UserReportSummary) -> str | None:
    if summary.conflict_count is None:
        return None
    if summary.conflict_count == 0:
        return "경로 충돌: 없음"
    return f"경로 충돌: {summary.conflict_count}건"


def _assignment_window(row: AssignmentSummary) -> str:
    start = format_datetime_ko(row.start_at)
    end = format_datetime_ko(row.end_at)
    if start and end:
        return f"{start} ~ {end}"
    return start or end or "시간 확인 필요"


def _render_failure(summary: UserReportSummary) -> str:
    lines = [summary.title, "", "문제:"]
    if summary.emergency_review_items:
        for item in summary.emergency_review_items:
            lines.append(
                f"- {item.item_id}: 요청 {item.requested_quantity_boxes} BOX, "
                f"가용 {item.available_quantity_boxes} BOX, "
                f"부족 {item.shortage_quantity_boxes} BOX"
            )
            if item.earliest_full_fulfillment_at:
                lines.append(
                    "  - 전체 출고 가능 예상: "
                    f"{format_datetime_ko(item.earliest_full_fulfillment_at)} 이후"
                )
            elif item.available_quantity_boxes == 0:
                lines.append("  - 현재 등록된 가용 재고·입고 예정 정보가 없습니다.")
    elif summary.issues:
        lines.extend(f"- {row.message}" for row in summary.issues)
    else:
        lines.append("- 확인된 조건으로 계획을 완료할 수 없습니다.")
    affected = _unique([row.work_id for row in summary.assignment_summaries])
    if affected:
        lines.extend(["", "영향:", f"- {', '.join(affected)} 작업을 완료할 수 없습니다."])
    lines.extend(["", "확인할 사항:"])
    actions = _unique([row.action or "" for row in summary.issues])
    actions.extend(
        action
        for item in summary.emergency_review_items
        for action in item.recommended_actions
        if action not in actions
    )
    lines.extend(f"- {action}" for action in (actions or [summary.recommended_action or "입력 조건을 확인해 주세요."]))
    if summary.assignment_summaries or summary.dependency_summaries:
        lines.extend(["", "이미 확인된 부분:"])
        if summary.assignment_summaries:
            lines.append("- 작업 대상과 일정 정보는 구조화했습니다.")
        if summary.dependency_summaries:
            lines.append("- 작업 선후관계는 해석했습니다.")
    return "\n".join(lines)


def _render_clarification(summary: UserReportSummary) -> str:
    lines = [summary.title, "", summary.primary_message]
    if summary.recommended_action and summary.recommended_action != summary.primary_message:
        lines.extend(["", summary.recommended_action])
    return "\n".join(lines)


def render_summary_report(summary: UserReportSummary) -> str:
    if summary.outcome == "FAILED":
        return _render_failure(summary)
    if summary.outcome == "CLARIFICATION_REQUIRED":
        return _render_clarification(summary)
    lines = [summary.primary_message]
    is_simulate_only = summary.execution_mode_label == "가상 시뮬레이션"
    if summary.assignment_summaries:
        row = summary.assignment_summaries[0]
        robot = f"{row.robot_id} 로봇" if row.robot_id else "배정 가능한 로봇"
        duration = (
            (row.end_at - row.start_at).total_seconds()
            if row.start_at is not None and row.end_at is not None
            else summary.active_work_duration_seconds
        )
        duration_text = format_duration_ko(duration, approximate=True)
        sentence = f"{row.work_id} 작업은 {robot}에 배정되었습니다."
        if is_simulate_only and duration_text:
            sentence = (
                f"{row.work_id} 작업은 {robot}에 배정되었으며, "
                f"가상 시뮬레이션 기준 {duration_text} 소요될 예정입니다."
            )
        elif duration_text:
            sentence = (
                f"{row.work_id} 작업은 {robot}에 배정되었으며, "
                f"{duration_text} 동안 수행될 예정입니다."
            )
        lines.extend(["", sentence])
    distance = _distance_text(summary)
    if distance:
        lines.append(f"- {distance}")
    if summary.schedule_completion_at:
        lines.append(
            "- {label}: {time}".format(
                label=("가상 계획 완료 예상" if is_simulate_only else "전체 계획 완료 예정"),
                time=format_datetime_ko(summary.schedule_completion_at),
            )
        )
    tardiness = _tardiness_text(summary)
    if tardiness:
        lines.append(f"- {tardiness}")
    conflict = _conflict_text(summary)
    if conflict:
        lines.append(f"- {conflict}")
    for item in summary.emergency_review_items:
        lines.append(
            f"- 긴급 검토: {item.item_id} {item.shortage_quantity_boxes} BOX 부족"
        )
    for warning in summary.warnings:
        lines.append(f"- 주의: {warning.message}")
    if summary.recommended_action:
        lines.extend(["", summary.recommended_action])
    return "\n".join(lines)


def render_standard_report(summary: UserReportSummary) -> str:
    if summary.outcome == "FAILED":
        return _render_failure(summary)
    if summary.outcome == "CLARIFICATION_REQUIRED":
        return _render_clarification(summary)
    lines = [summary.primary_message]
    is_simulate_only = summary.execution_mode_label == "가상 시뮬레이션"
    if summary.assignment_summaries:
        lines.extend(
            [
                "",
                "| 순서 | 작업 | 예정 시간 | 로봇 | 상태 |",
                "|---|---|---|---|---|",
            ]
        )
        normal_index = 0
        urgent_insert_mode = summary.plan_mode_label == "긴급 작업 추가"
        for row in summary.assignment_summaries:
            if row.is_inserted and urgent_insert_mode:
                order = "긴급"
            else:
                normal_index += 1
                order = str(normal_index)
            lines.append(
                "| {order} | {work} | {window} | {robot} | {status} |".format(
                    order=order,
                    work=row.work_id,
                    window=_assignment_window(row),
                    robot=row.robot_id or "미배정",
                    status=row.status_label,
                )
            )
    if summary.dependency_summaries:
        lines.extend(["", "작업 순서:"])
        for row in summary.dependency_summaries:
            lines.append(
                f"- {row.predecessor_work_id}이 완료된 후 "
                f"{row.successor_work_id}가 시작됩니다."
            )
    if summary.inventory_feasibility:
        lines.extend(["", "시간대별 재고 검증:"])
        for item in summary.inventory_feasibility.item_results:
            required = format_datetime_ko(item.required_at)
            timing = f" / 필요 시각: {required}" if required else ""
            lines.append(
                "- {item}: 요청 {requested} BOX / 계획 {planned} BOX / "
                "해당 시각 가용 {available} BOX / 부족 {shortage} BOX{timing}".format(
                    item=item.item_id,
                    requested=item.requested_quantity_boxes,
                    planned=item.planned_quantity_boxes,
                    available=item.available_quantity_boxes,
                    shortage=item.shortage_quantity_boxes,
                    timing=timing,
                )
            )
    if summary.emergency_review_items:
        lines.extend(["", "긴급 검토가 필요한 재고:"])
        for item in summary.emergency_review_items:
            lines.append(
                f"- {item.item_id}: {item.shortage_quantity_boxes} BOX 부족"
            )
            if item.earliest_full_fulfillment_at:
                lines.append(
                    "  - 전체 출고 가능 예상: "
                    f"{format_datetime_ko(item.earliest_full_fulfillment_at)}"
                )
            for action in item.recommended_actions:
                lines.append(f"  - 선택지: {action}")
    changes = summary.schedule_change_summary
    if changes:
        lines.extend(["", "일정 변경:"])
        if changes.inserted_work_ids:
            lines.append(f"- {', '.join(changes.inserted_work_ids)} 작업을 새로 추가했습니다.")
        if changes.preserved_work_ids:
            lines.append(
                f"- {', '.join(changes.preserved_work_ids)}의 기존 일정과 로봇 배정을 유지했습니다."
            )
        if changes.shifted_work_ids:
            lines.append(f"- {', '.join(changes.shifted_work_ids)}의 일정이 변경되었습니다.")
        else:
            lines.append("- 일정이 변경된 기존 작업은 없습니다.")
        if changes.blocked_work_ids:
            lines.append(f"- {', '.join(changes.blocked_work_ids)} 작업이 차단되었습니다.")
        else:
            lines.append("- 차단된 작업은 없습니다.")
    metric_lines: list[str] = []
    if summary.schedule_completion_at:
        metric_lines.append(
            "- {label}: {time}".format(
                label=("가상 계획 완료 예상" if is_simulate_only else "전체 계획 완료 예정"),
                time=format_datetime_ko(summary.schedule_completion_at),
            )
        )
    active = format_duration_ko(summary.active_work_duration_seconds)
    if active:
        metric_lines.append(
            f"- {'예상 작업 소요시간' if is_simulate_only else '작업 수행시간 합계'}: {active}"
        )
    elapsed = format_duration_ko(summary.elapsed_until_completion_seconds, approximate=True)
    if elapsed:
        metric_lines.append(f"- 현재부터 완료까지: {elapsed}")
    distance = _distance_text(summary)
    if distance:
        metric_lines.append(f"- {distance}")
    tardiness = _tardiness_text(summary)
    if tardiness:
        metric_lines.append(f"- {tardiness}")
    conflict = _conflict_text(summary)
    if conflict:
        metric_lines.append(f"- {conflict}")
    if changes:
        metric_lines.append(
            "- 시간창 위반: " + ("있음" if changes.hard_window_violation else "없음")
        )
        metric_lines.append(
            "- 납기 위반: " + ("있음" if changes.deadline_violation else "없음")
        )
    if metric_lines:
        lines.extend(["", "계획 결과:", *metric_lines])
    if summary.warnings:
        lines.extend(["", "주의:"])
        lines.extend(f"- {row.message}" for row in summary.warnings)
    if summary.recommended_action:
        lines.extend(["", summary.recommended_action])
    return "\n".join(lines)


def build_debug_report_payload(
    state: dict[str, Any],
    summary: UserReportSummary,
    report_evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "command_interpretation": {
            "command": report_evidence.get("user_command", {}),
            "interpretation": state.get("interpretation", {}),
        },
        "supervisor_decision": report_evidence.get("supervisor", {}),
        "base_plan_and_version": {
            "base_plan_source": state.get("base_plan_source"),
            "base_plan_version": state.get("base_plan_version"),
            "plan_version": state.get("plan_version"),
        },
        "optimization_assignments": report_evidence.get("assignments", []),
        "candidate_evaluation": report_evidence.get("optimization", {}),
        "routing_and_reservations": {
            "routes": report_evidence.get("routes", {}),
            "reservations": report_evidence.get("reservations", {}),
            "distance_comparison": report_evidence.get("distance_comparison", {}),
        },
        "simulation_metrics": report_evidence.get("simulation", {}),
        "verification_evidence": report_evidence.get("verification", {}),
        "inventory_feasibility": state.get("inventory_feasibility"),
        "inventory_timeline_validation": state.get(
            "inventory_timeline_validation"
        ),
        "inventory_reservations": state.get("inventory_reservations", []),
        "capacity_feasibility": state.get("capacity_feasibility"),
        "plan_changes": summary.schedule_change_summary.model_dump(mode="json")
        if summary.schedule_change_summary
        else None,
        "trace_summary": [
            {
                key: row.get(key)
                for key in ("node", "at", "success", "status", "attempt")
                if key in row
            }
            for row in state.get("trace", [])
        ],
    }


def render_debug_report(summary: UserReportSummary, payload: dict[str, Any]) -> str:
    sections = (
        ("Command interpretation", payload.get("command_interpretation")),
        ("Supervisor decision", payload.get("supervisor_decision")),
        ("Base plan and plan version", payload.get("base_plan_and_version")),
        ("Optimization assignments", payload.get("optimization_assignments")),
        ("Candidate evaluation", payload.get("candidate_evaluation")),
        ("Routing and reservations", payload.get("routing_and_reservations")),
        ("Simulation metrics", payload.get("simulation_metrics")),
        ("Verification evidence", payload.get("verification_evidence")),
        ("Inventory feasibility", payload.get("inventory_feasibility")),
        (
            "Inventory timeline validation",
            payload.get("inventory_timeline_validation"),
        ),
        ("Inventory reservations", payload.get("inventory_reservations")),
        ("Capacity feasibility", payload.get("capacity_feasibility")),
        ("Plan changes", payload.get("plan_changes")),
        ("Trace summary", payload.get("trace_summary")),
    )
    lines = [summary.primary_message]
    for index, (title, value) in enumerate(sections, start=1):
        lines.extend(
            [
                "",
                f"## {index}. {title}",
                "```json",
                json.dumps(value, ensure_ascii=False, indent=2, default=str),
                "```",
            ]
        )
    return "\n".join(lines)


def report_payload_for_level(
    summary: UserReportSummary,
    *,
    debug_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "report_detail_level": summary.report_level.value,
        "user_report_summary": summary.model_dump(mode="json"),
    }
    if summary.report_level == ReportDetailLevel.DEBUG:
        payload["debug_evidence"] = debug_payload or {}
    return payload


def render_user_report(
    summary: UserReportSummary,
    *,
    debug_payload: dict[str, Any] | None = None,
) -> str:
    if summary.report_level == ReportDetailLevel.DEBUG:
        return render_debug_report(summary, debug_payload or {})
    if summary.report_level == ReportDetailLevel.STANDARD:
        return render_standard_report(summary)
    return render_summary_report(summary)


def llm_report_is_supported(answer: str, summary: UserReportSummary) -> bool:
    text = answer.strip()
    if not text:
        return False
    if summary.report_level != ReportDetailLevel.DEBUG:
        forbidden = (
            "command_id",
            "prompt_version",
            "evidence_id",
            "objective_value",
            "incremental_objective",
            "vertex_reservation_count",
            "edge_reservation_count",
            "time_step",
            "tie_break_rule",
        )
        if any(token in text for token in forbidden):
            return False
    if summary.distance_unit is None and re.search(
        r"(?:이동거리|거리)[^\n]{0,20}\d(?:[\d.]*)\s*(?:m|km|미터|킬로미터)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    for row in summary.assignment_summaries:
        if row.work_id not in text:
            return False
        if row.robot_id and row.robot_id not in text:
            return False
    if summary.total_distance is not None and _number(summary.total_distance) not in text:
        return False
    if summary.tardiness_seconds is not None and "납기" not in text:
        return False
    if summary.conflict_count is not None and "충돌" not in text:
        return False
    return True

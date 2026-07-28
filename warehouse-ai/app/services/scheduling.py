"""Deterministic daily scheduling, dependency, and readiness helpers.

The language parser deliberately handles only explicit work IDs and explicit
times. Ambiguous prose is returned as missing information rather than being
turned into invented work or timestamps.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import (
    PlanningReference,
    SameRobotGroup,
    ScheduleParseResult,
    ScheduledTask,
    TaskDependency,
    TaskScheduleConstraint,
)
from app.time_utils import as_utc_datetime


DEFAULT_WAREHOUSE_TIMEZONE = "Asia/Seoul"
WORK_ID_PATTERN = r"W-?\d+"
RELATIVE_DATE_PATTERN = (
    r"(?:오늘|내일|모레|다음\s*날|"
    r"이번\s*주\s*[월화수목금토일]요일|"
    r"다음\s*주\s*[월화수목금토일]요일)"
)
ABSOLUTE_DATE_PATTERN = (
    r"(?:\d{4}년\s*\d{1,2}월\s*\d{1,2}일|"
    r"\d{1,2}월\s*\d{1,2}일|"
    r"\d{4}-\d{1,2}-\d{1,2})"
)
SCHEDULE_DATE_PATTERN = rf"(?:{RELATIVE_DATE_PATTERN}|{ABSOLUTE_DATE_PATTERN})"
WEEKDAY_INDEX = {
    "월": 0,
    "화": 1,
    "수": 2,
    "목": 3,
    "금": 4,
    "토": 5,
    "일": 6,
}


def canonical_work_id(value: str) -> str:
    match = re.fullmatch(r"W-?(\d+)", value.strip(), flags=re.IGNORECASE)
    if not match:
        return value.strip().upper()
    return f"W-{int(match.group(1)):03d}"


def resolve_warehouse_timezone(name: str | None) -> tuple[ZoneInfo, str, bool]:
    requested = (name or "").strip()
    effective = requested or DEFAULT_WAREHOUSE_TIMEZONE
    try:
        return ZoneInfo(effective), effective, not bool(requested)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_WAREHOUSE_TIMEZONE), DEFAULT_WAREHOUSE_TIMEZONE, True


def _local_time(
    reference_time: datetime,
    timezone: ZoneInfo,
    meridiem: str | None,
    hour: int,
    relative_date: str | None = None,
) -> datetime:
    local_reference = as_utc_datetime(
        reference_time, field_name="reference_time"
    ).astimezone(timezone)
    if meridiem == "오후" and hour < 12:
        hour += 12
    elif meridiem == "오전" and hour == 12:
        hour = 0
    target_date = _relative_local_date(local_reference, relative_date)
    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        tzinfo=timezone,
    )


def _inherited_range_end_meridiem(
    start_meridiem: str | None,
    explicit_end_meridiem: str | None,
    end_hour: int,
) -> str | None:
    """Resolve an omitted end meridiem for a natural Korean time range.

    ``오전 10시 30분부터 12시까지`` means noon, not the following
    midnight.  Other omitted endpoints continue to inherit the start
    meridiem, preserving established overnight handling such as
    ``오후 10시부터 12시까지``.
    """

    if explicit_end_meridiem:
        return explicit_end_meridiem
    if start_meridiem == "오전" and end_hour == 12:
        return "오후"
    return start_meridiem


def _relative_local_date(
    local_reference: datetime,
    relative_date: str | None,
) -> date:
    """Resolve relative or explicit dates from the warehouse-local clock."""

    normalized = re.sub(r"\s+", "", relative_date or "오늘")
    base = local_reference.date()
    if normalized == "오늘":
        return base
    if normalized in {"내일", "다음날"}:
        return base + timedelta(days=1)
    if normalized == "모레":
        return base + timedelta(days=2)
    weekday_match = re.fullmatch(
        r"(?P<week>이번주|다음주)(?P<weekday>[월화수목금토일])요일",
        normalized,
    )
    if weekday_match:
        monday = base - timedelta(days=base.weekday())
        week_offset = 7 if weekday_match.group("week") == "다음주" else 0
        return monday + timedelta(
            days=week_offset + WEEKDAY_INDEX[weekday_match.group("weekday")]
        )

    korean_date = re.fullmatch(
        r"(?:(?P<year>\d{4})년)?(?P<month>\d{1,2})월(?P<day>\d{1,2})일",
        normalized,
    )
    if korean_date:
        year = int(korean_date.group("year") or base.year)
        candidate = date(
            year,
            int(korean_date.group("month")),
            int(korean_date.group("day")),
        )
        if korean_date.group("year") is None and candidate < base:
            candidate = date(year + 1, candidate.month, candidate.day)
        return candidate

    iso_date = re.fullmatch(
        r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})",
        normalized,
    )
    if iso_date:
        return date(
            int(iso_date.group("year")),
            int(iso_date.group("month")),
            int(iso_date.group("day")),
        )
    return base


def parse_planning_reference_time(
    text: str,
    *,
    reference_time: datetime,
    warehouse_timezone: str | None,
) -> tuple[PlanningReference | None, list[str]]:
    """Parse an explicit relative or absolute planning clock.

    This intentionally requires a reference marker, so ordinary deadline or
    time-window expressions remain schedule constraints rather than silently
    changing the planning timeline.
    """

    timezone, timezone_name, _ = resolve_warehouse_timezone(warehouse_timezone)
    local_now = as_utc_datetime(reference_time, field_name="reference_time").astimezone(timezone)
    marker = r"(?:을|를)?\s*(?:기준(?:으로)?|시점\s*기준|현재)"
    patterns = (
        re.compile(
            rf"(?P<value>(?P<year>\d{{4}})년\s*(?P<month>\d{{1,2}})월\s*(?P<day>\d{{1,2}})일\s*(?P<meridiem>오전|오후|아침)?\s*(?P<hour>\d{{1,2}})시(?:\s*(?P<minute>\d{{1,2}})분)?)\s*{marker}"
        ),
        re.compile(
            rf"(?P<value>(?P<month>\d{{1,2}})월\s*(?P<day>\d{{1,2}})일\s*(?P<meridiem>오전|오후|아침)?\s*(?P<hour>\d{{1,2}})시(?:\s*(?P<minute>\d{{1,2}})분)?)\s*{marker}"
        ),
        re.compile(
            rf"(?P<value>(?P<date>오늘|내일|모레)\s*(?P<meridiem>오전|오후|아침)?\s*(?P<hour>\d{{1,2}})시(?:\s*(?P<minute>\d{{1,2}})분)?)\s*{marker}"
        ),
        re.compile(
            rf"(?P<value>(?P<date>\d{{4}}-\d{{1,2}}-\d{{1,2}})[ T](?P<hour>\d{{1,2}}):(?P<minute>\d{{2}}))\s*{marker}"
        ),
    )
    match = next((candidate.search(text) for candidate in patterns if candidate.search(text)), None)
    if match is None:
        if re.search(r"(?:기준(?:으로)?|시점\s*기준|현재)", text) and re.search(r"(?:오늘|내일|모레|오전|오후|\d{1,2}시|\d{4}-\d{1,2}-\d{1,2})", text):
            return None, ["planning_reference_time"]
        return None, []
    values = match.groupdict()
    try:
        hour = int(values["hour"])
        minute = int(values.get("minute") or 0)
        valid_hour = 1 <= hour <= 12 if values.get("meridiem") else 0 <= hour <= 23
        if not 0 <= minute <= 59 or not valid_hour:
            raise ValueError("invalid clock")
        meridiem = values.get("meridiem")
        if meridiem == "오후" and hour < 12:
            hour += 12
        elif meridiem in {"오전", "아침"} and hour == 12:
            hour = 0
        if values.get("date") in {"오늘", "내일", "모레"}:
            target_date = _relative_local_date(local_now, values["date"])
        elif values.get("date"):
            year, month, day = (int(part) for part in values["date"].split("-"))
            target_date = date(year, month, day)
        else:
            year = int(values.get("year") or local_now.year)
            target_date = date(year, int(values["month"]), int(values["day"]))
            if not values.get("year") and target_date < local_now.date():
                target_date = date(year + 1, target_date.month, target_date.day)
        local_at = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=timezone)
    except (TypeError, ValueError):
        return None, ["planning_reference_time"]
    return PlanningReference(
        original_text=match.group("value"),
        local_at=local_at,
        utc_at=local_at.astimezone(UTC),
        timezone=timezone_name,
        source="USER_COMMAND",
    ), []



def parse_explicit_time_windows(
    text: str,
    *,
    reference_time: datetime,
    warehouse_timezone: str | None,
) -> list[dict[str, object]]:
    """Return explicit warehouse-local time windows with source spans.

    Unlike ``_window_constraints`` this helper does not require a persisted
    work ID.  It is used by the command parser to bind a natural-language
    window (for example ``오전 9시부터 오전 11시``) to newly created
    inventory operation IDs.
    """

    timezone, _, _ = resolve_warehouse_timezone(warehouse_timezone)
    pattern = re.compile(
        rf"(?P<start_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<start_meridiem>오전|오후)?\s*(?P<start>\d{{1,2}})시"
        rf"(?:\s*(?P<start_minute>\d{{1,2}})분)?\s*"
        rf"(?:부터|에서|~|-)\s*(?P<end_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<end_meridiem>오전|오후)?\s*(?P<end>\d{{1,2}})시"
        rf"(?:\s*(?P<end_minute>\d{{1,2}})분)?(?:까지|\s*사이)?",
        re.IGNORECASE,
    )
    rows: list[dict[str, object]] = []
    inherited_date: str | None = None
    for match in pattern.finditer(text):
        explicit_start_date = match.group("start_date")
        if explicit_start_date:
            inherited_date = explicit_start_date
        start_date = explicit_start_date or inherited_date
        end_date = match.group("end_date") or start_date
        start_meridiem = match.group("start_meridiem")
        end_meridiem = _inherited_range_end_meridiem(
            start_meridiem,
            match.group("end_meridiem"),
            int(match.group("end")),
        )
        start_local = _local_time(
            reference_time,
            timezone,
            start_meridiem,
            int(match.group("start")),
            start_date,
        ).replace(minute=int(match.group("start_minute") or 0))
        end_local = _local_time(
            reference_time,
            timezone,
            end_meridiem,
            int(match.group("end")),
            end_date,
        ).replace(minute=int(match.group("end_minute") or 0))
        if end_local <= start_local:
            end_local += timedelta(days=1)
        rows.append(
            {
                "span_start": match.start(),
                "span_end": match.end(),
                "earliest_start": start_local.astimezone(UTC),
                "latest_finish": end_local.astimezone(UTC),
                "original_text": match.group(0),
            }
        )
    return rows

def _window_constraints(
    text: str,
    reference_time: datetime,
    timezone: ZoneInfo,
) -> list[TaskScheduleConstraint]:
    # Supports both "오전 9시부터 10시까지 W-001" and
    # "W-001을 오전 9시부터 10시까지" without guessing vague ranges.
    time_then_work = re.compile(
        rf"(?P<start_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<start_meridiem>오전|오후)?\s*(?P<start>\d{{1,2}})시\s*"
        rf"(?:부터|에서|~|-)\s*(?P<end_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<end_meridiem>오전|오후)?\s*"
        rf"(?P<end>\d{{1,2}})시(?:까지|\s*사이)?[^W]{{0,35}}"
        rf"(?P<work>{WORK_ID_PATTERN})",
        re.IGNORECASE,
    )
    work_then_time = re.compile(
        rf"(?P<work>{WORK_ID_PATTERN})[^\dW.。]{{0,35}}"
        rf"(?P<start_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<start_meridiem>오전|오후)?\s*(?P<start>\d{{1,2}})시\s*"
        rf"(?:부터|에서|~|-)\s*(?P<end_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<end_meridiem>오전|오후)?\s*"
        rf"(?P<end>\d{{1,2}})시(?:까지|\s*사이)?",
        re.IGNORECASE,
    )
    by_work: dict[str, TaskScheduleConstraint] = {}
    for pattern in (time_then_work, work_then_time):
        for match in pattern.finditer(text):
            work_id = canonical_work_id(match.group("work"))
            start_date = match.group("start_date")
            end_date = match.group("end_date") or start_date
            start_meridiem = match.group("start_meridiem")
            end_meridiem = _inherited_range_end_meridiem(
                start_meridiem,
                match.group("end_meridiem"),
                int(match.group("end")),
            )
            start_local = _local_time(
                reference_time,
                timezone,
                start_meridiem,
                int(match.group("start")),
                start_date,
            )
            end_local = _local_time(
                reference_time,
                timezone,
                end_meridiem,
                int(match.group("end")),
                end_date,
            )
            # A range such as 23:00-01:00 explicitly crosses midnight.
            if end_local <= start_local:
                end_local += timedelta(days=1)
            by_work[work_id] = TaskScheduleConstraint(
                work_id=work_id,
                earliest_start=start_local.astimezone(UTC),
                latest_finish=end_local.astimezone(UTC),
                time_constraint_type="HARD_WINDOW",
            )

    # "오전 9시부터 W-001" is an earliest-start constraint, not a
    # planning reference time. It intentionally has no inferred deadline.
    start_then_work = re.compile(
        rf"(?P<start_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<start_meridiem>오전|오후)?\s*(?P<start>\d{{1,2}})시\s*부터\s*"
        rf"(?P<work>{WORK_ID_PATTERN})",
        re.IGNORECASE,
    )
    work_then_start = re.compile(
        rf"(?P<work>{WORK_ID_PATTERN})[^\dW.。]{{0,35}}"
        rf"(?P<start_date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<start_meridiem>오전|오후)?\s*(?P<start>\d{{1,2}})시\s*부터",
        re.IGNORECASE,
    )
    for pattern in (start_then_work, work_then_start):
        for match in pattern.finditer(text):
            work_id = canonical_work_id(match.group("work"))
            if work_id in by_work:
                continue
            start_local = _local_time(
                reference_time,
                timezone,
                match.group("start_meridiem"),
                int(match.group("start")),
                match.group("start_date"),
            )
            by_work[work_id] = TaskScheduleConstraint(
                work_id=work_id,
                earliest_start=start_local.astimezone(UTC),
                time_constraint_type="HARD_WINDOW",
            )

    # A single explicit "W-001 ... 10시까지" is a deadline, unless it was
    # already captured as a hard window.
    deadline_pattern = re.compile(
        rf"(?P<work>{WORK_ID_PATTERN})[^W]{{0,50}}?"
        rf"(?P<date>{SCHEDULE_DATE_PATTERN})?\s*"
        rf"(?P<meridiem>오전|오후)?\s*(?P<hour>\d{{1,2}})시까지",
        re.IGNORECASE,
    )
    for match in deadline_pattern.finditer(text):
        if re.search(r"(?:부터|에서|~|-)\s*(?:오전|오후)?\s*\d{1,2}시까지", match.group(0)):
            continue
        work_id = canonical_work_id(match.group("work"))
        if work_id in by_work:
            continue
        deadline = _local_time(
            reference_time,
            timezone,
            match.group("meridiem"),
            int(match.group("hour")),
            match.group("date"),
        )
        by_work[work_id] = TaskScheduleConstraint(
            work_id=work_id,
            latest_finish=deadline.astimezone(UTC),
            time_constraint_type="DEADLINE",
        )
    return sorted(by_work.values(), key=lambda row: row.work_id)


def _dependency_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    explicit = re.compile(
        rf"(?P<left>{WORK_ID_PATTERN})(?:은|는|이|가|을|를)?"
        rf"(?:(?!{WORK_ID_PATTERN}).){{0,55}}?"
        rf"(?:완료(?:되면|하면|된\s*후|한\s*후)|완료\s*후|"
        rf"끝나면|끝난\s*다음|끝낸\s*다음|다음에|하고\s*나서)"
        rf"(?:(?!{WORK_ID_PATTERN}).){{0,25}}?"
        rf"(?P<right>{WORK_ID_PATTERN})",
        re.IGNORECASE | re.DOTALL,
    )
    pairs.extend(
        (
            canonical_work_id(match.group("left")),
            canonical_work_id(match.group("right")),
        )
        for match in explicit.finditer(text)
    )
    first_then = re.compile(
        rf"먼저\s*(?P<left>{WORK_ID_PATTERN})"
        rf"(?:(?!{WORK_ID_PATTERN}).){{0,30}}?"
        rf"그다음(?:에)?\s*(?P<right>{WORK_ID_PATTERN})",
        re.IGNORECASE | re.DOTALL,
    )
    pairs.extend(
        (
            canonical_work_id(match.group("left")),
            canonical_work_id(match.group("right")),
        )
        for match in first_then.finditer(text)
    )
    if "순서" in text or "차례" in text:
        ordered = [
            canonical_work_id(value)
            for value in re.findall(WORK_ID_PATTERN, text, flags=re.IGNORECASE)
        ]
        pairs.extend(zip(ordered, ordered[1:]))
    arrow_values = re.findall(
        rf"{WORK_ID_PATTERN}(?:\s*(?:->|→)\s*{WORK_ID_PATTERN})+",
        text,
        flags=re.IGNORECASE,
    )
    for expression in arrow_values:
        ordered = [
            canonical_work_id(value)
            for value in re.findall(WORK_ID_PATTERN, expression, flags=re.IGNORECASE)
        ]
        pairs.extend(zip(ordered, ordered[1:]))
    return list(dict.fromkeys((left, right) for left, right in pairs if left != right))


def scope_dependency_graph(
    dependencies: Iterable[TaskDependency],
    *,
    seed_work_ids: Iterable[str],
    known_work_ids: Iterable[str],
) -> tuple[list[TaskDependency], list[str], list[str], list[str]]:
    """Limit dependencies and topological nodes to the current planning scope."""

    rows = list(dependencies)
    known = {canonical_work_id(value) for value in known_work_ids}
    requested = {canonical_work_id(value) for value in seed_work_ids if value}
    scope = (requested & known) if requested else set(known)
    errors: list[str] = []
    changed = True
    while changed:
        changed = False
        for row in rows:
            left = canonical_work_id(row.predecessor_work_id)
            right = canonical_work_id(row.successor_work_id)
            if left not in scope and right not in scope:
                continue
            missing = sorted({left, right} - known)
            if missing:
                message = "DEPENDENCY_WORK_NOT_IN_SNAPSHOT:" + ",".join(missing)
                if message not in errors:
                    errors.append(message)
                continue
            before = len(scope)
            scope.update((left, right))
            changed = changed or len(scope) != before
    scoped = [
        row
        for row in rows
        if canonical_work_id(row.predecessor_work_id) in scope
        and canonical_work_id(row.successor_work_id) in scope
    ]
    ignored_count = len(rows) - len(scoped)
    warnings = (
        [f"OUT_OF_SCOPE_DEPENDENCIES_IGNORED:{ignored_count}"]
        if ignored_count
        else []
    )
    return scoped, sorted(scope), warnings, errors


def parse_schedule_language(
    text: str,
    *,
    reference_time: datetime,
    warehouse_timezone: str | None,
) -> ScheduleParseResult:
    timezone, timezone_name, defaulted = resolve_warehouse_timezone(
        warehouse_timezone
    )
    constraints = _window_constraints(text, reference_time, timezone)
    dependencies = [
        TaskDependency(predecessor_work_id=left, successor_work_id=right)
        for left, right in _dependency_pairs(text)
    ]
    work_ids = list(
        dict.fromkeys(
            canonical_work_id(value)
            for value in re.findall(WORK_ID_PATTERN, text, flags=re.IGNORECASE)
        )
    )
    same_robot_groups: list[SameRobotGroup] = []
    if len(work_ids) >= 2 and any(
        phrase in text
        for phrase in ("같은 로봇", "한 로봇", "동일 로봇")
    ):
        same_robot_groups.append(
            SameRobotGroup(group_id="COMMAND_SAME_ROBOT_1", work_ids=work_ids)
        )
        group_id = same_robot_groups[0].group_id
        by_work = {row.work_id: row for row in constraints}
        for work_id in work_ids:
            row = by_work.get(work_id) or TaskScheduleConstraint(work_id=work_id)
            row.same_robot_group = group_id
            by_work[work_id] = row
        constraints = sorted(by_work.values(), key=lambda row: row.work_id)

    urgent = any(
        phrase in text
        for phrase in (
            "지금 먼저",
            "급하게",
            "최우선",
            "일정을 미뤄",
            "가능한 한 빨리",
        )
    )
    asap = urgent or "지금" in text
    if asap:
        by_work = {row.work_id: row for row in constraints}
        for work_id in work_ids:
            row = by_work.get(work_id) or TaskScheduleConstraint(work_id=work_id)
            if row.earliest_start is None:
                row.earliest_start = as_utc_datetime(
                    reference_time, field_name="reference_time"
                )
                row.time_constraint_type = "ASAP"
            by_work[work_id] = row
        constraints = sorted(by_work.values(), key=lambda row: row.work_id)

    safe_stop_requested = any(
        phrase in text for phrase in ("중단하고", "멈추고")
    ) or (
        "취소하고" in text
        and "기존 일정" not in text
        and "기존 계획" not in text
    )
    vague_time = bool(re.search(r"(?:오전|오후|이후|이전)에(?:\s|$)", text))
    schedule_signal = bool(
        constraints
        or dependencies
        or same_robot_groups
        or re.search(r"(?:일정|스케줄|타임라인|병렬|동시)", text)
    )
    warnings = ["DEFAULT_WAREHOUSE_TIMEZONE_USED"] if defaulted else []
    missing = ["explicit_schedule_time"] if vague_time and schedule_signal else []
    return ScheduleParseResult(
        constraints=constraints,
        dependencies=dependencies,
        same_robot_groups=same_robot_groups,
        insertion_policy="URGENT" if urgent else "ASAP" if asap else "NORMAL",
        preemption_policy=(
            "REQUIRE_SAFE_STOP_CONFIRMATION"
            if safe_stop_requested
            else "NON_PREEMPTIVE"
        ),
        daily_schedule_requested=schedule_signal,
        timezone_name=timezone_name,
        timezone_defaulted=defaulted,
        warnings=warnings,
        missing_information=missing,
    )


def validate_dependency_graph(
    dependencies: Iterable[TaskDependency],
    work_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    nodes = {canonical_work_id(value) for value in work_ids}
    edges: dict[str, set[str]] = {node: set() for node in nodes}
    indegree: dict[str, int] = {node: 0 for node in nodes}
    for dependency in dependencies:
        left = canonical_work_id(dependency.predecessor_work_id)
        right = canonical_work_id(dependency.successor_work_id)
        nodes.update((left, right))
        edges.setdefault(left, set())
        edges.setdefault(right, set())
        indegree.setdefault(left, 0)
        indegree.setdefault(right, 0)
        if right not in edges[left]:
            edges[left].add(right)
            indegree[right] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for successor in sorted(edges[node]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort()
    if len(order) != len(indegree):
        cyclic = sorted(node for node, degree in indegree.items() if degree > 0)
        return [], ["CYCLIC_TASK_DEPENDENCY:" + ",".join(cyclic)]
    return order, []


def relative_time_step(
    value: datetime | str | None,
    reference_time: datetime | str,
    time_step_seconds: int,
    *,
    round_up: bool,
) -> int:
    if value is None:
        return 0
    seconds = (
        as_utc_datetime(value, field_name="schedule_time")
        - as_utc_datetime(reference_time, field_name="reference_time")
    ).total_seconds()
    quotient = seconds / max(1, int(time_step_seconds))
    return max(0, math.ceil(quotient) if round_up else math.floor(quotient))


def planned_at(
    reference_time: datetime | str,
    time_step: int,
    time_step_seconds: int,
) -> datetime:
    return as_utc_datetime(
        reference_time, field_name="reference_time"
    ) + timedelta(seconds=max(0, time_step) * max(1, time_step_seconds))


def reconcile_task_time_window(
    task: ScheduledTask,
    *,
    route_start_step: int | None,
    route_end_step: int | None,
) -> tuple[int, int]:
    """Merge optimizer and routing times without creating zero-duration work.

    Routing may report the same start/end step for a same-node PICK or DROP.
    The optimizer already includes the minimum processing duration, so the
    reconciled schedule must preserve that duration while still accepting any
    later arrival produced by collision-free routing.
    """

    final_start_step = (
        max(0, int(route_start_step))
        if route_start_step is not None
        else int(task.start_time_step)
    )
    original_duration_steps = max(
        0,
        int(task.end_time_step) - int(task.start_time_step),
    )
    if task.action in {"PICK", "DROP", "CHARGE"}:
        original_duration_steps = max(1, original_duration_steps)
    final_end_step = max(
        final_start_step + original_duration_steps,
        int(route_end_step) if route_end_step is not None else int(task.end_time_step),
    )
    return final_start_step, final_end_step


def rebase_time_step(
    time_step: int,
    *,
    parent_reference_time: datetime | str,
    child_reference_time: datetime | str,
    time_step_seconds: int,
) -> int:
    """Express one parent-plan step on the child plan's relative timeline."""

    absolute_time = planned_at(
        parent_reference_time,
        int(time_step),
        time_step_seconds,
    )
    return relative_time_step(
        absolute_time,
        child_reference_time,
        time_step_seconds,
        round_up=True,
    )


def rebase_preserved_task(
    task: ScheduledTask,
    *,
    parent_reference_time: datetime | str,
    child_reference_time: datetime | str,
    time_step_seconds: int,
) -> ScheduledTask:
    """Keep a candidate task's wall-clock window while rebasing its steps."""

    absolute_start = task.planned_start_at or planned_at(
        parent_reference_time,
        task.start_time_step,
        time_step_seconds,
    )
    absolute_end = task.planned_end_at or planned_at(
        parent_reference_time,
        task.end_time_step,
        time_step_seconds,
    )
    duration_steps = max(0, task.end_time_step - task.start_time_step)
    child_start_step = relative_time_step(
        absolute_start,
        child_reference_time,
        time_step_seconds,
        round_up=True,
    )
    return task.model_copy(
        update={
            "start_time_step": child_start_step,
            "end_time_step": child_start_step + duration_steps,
            "planned_start_at": as_utc_datetime(
                absolute_start, field_name="planned_start_at"
            ),
            "planned_end_at": as_utc_datetime(
                absolute_end, field_name="planned_end_at"
            ),
        }
    )


def ready_task_ids(
    scheduled_tasks: Iterable[ScheduledTask],
    dependencies: Iterable[TaskDependency],
    *,
    completed_work_ids: Iterable[str] = (),
    now_step: int = 0,
) -> tuple[list[str], list[str]]:
    completed = {canonical_work_id(value) for value in completed_work_ids}
    predecessors: dict[str, set[str]] = {}
    for dependency in dependencies:
        predecessors.setdefault(
            canonical_work_id(dependency.successor_work_id), set()
        ).add(canonical_work_id(dependency.predecessor_work_id))
    ready: list[str] = []
    waiting: list[str] = []
    for task in scheduled_tasks:
        work_id = canonical_work_id(task.work_id or task.task_id.split(":", 1)[0])
        is_ready = (
            task.start_time_step <= now_step
            and predecessors.get(work_id, set()).issubset(completed)
        )
        (ready if is_ready else waiting).append(task.task_id)
    return sorted(ready), sorted(waiting)

import math
from datetime import UTC, datetime
from typing import Any


def as_utc_datetime(value: datetime | str, *, field_name: str) -> datetime:
    """Parse a datetime once and normalize the represented instant to UTC.

    PostgreSQL ``timestamptz`` values are timezone-aware. Naive values can still
    arrive from older JSON payloads, so they are interpreted as UTC for backward
    compatibility instead of applying the host's local timezone implicitly.
    """

    parsed = value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(parsed, datetime):
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def planning_reference_time(problem: dict[str, Any]) -> datetime:
    value = problem.get("reference_time") or problem.get("captured_at")
    if value is None:
        raise ValueError("optimization problem에 reference_time이 필요합니다.")
    return as_utc_datetime(value, field_name="reference_time")


def deadline_time_step(
    deadline: datetime | str,
    reference_time: datetime | str,
    time_step_seconds: int,
) -> int:
    step_seconds = max(1, int(time_step_seconds))
    deadline_utc = as_utc_datetime(deadline, field_name="deadline")
    reference_utc = as_utc_datetime(reference_time, field_name="reference_time")
    relative_seconds = (deadline_utc - reference_utc).total_seconds()
    return math.floor(relative_seconds / step_seconds)


def task_tardiness_steps(
    *,
    deadline: datetime | str | None,
    reference_time: datetime | str,
    task_end_time_step: int,
    time_step_seconds: int,
) -> int:
    if deadline is None:
        return 0
    relative_deadline_step = deadline_time_step(
        deadline,
        reference_time,
        time_step_seconds,
    )
    # A negative deadline step means the deadline was already in the past.
    # Keeping it negative includes the pre-existing delay in total tardiness.
    return max(0, int(task_end_time_step) - relative_deadline_step)

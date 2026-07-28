"""Deterministic scheduler tick evaluation.

This service has no polling loop. A production scheduler may call ``evaluate``
at a controlled cadence, while tests pass an explicit clock.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.models import ScheduledTask, TaskDependency
from app.services.scheduling import ready_task_ids
from app.time_utils import as_utc_datetime


class SchedulerTickService:
    @staticmethod
    def evaluate(
        active_plan: dict[str, Any],
        *,
        now: datetime,
        completed_work_ids: list[str],
    ) -> dict[str, list[str] | int]:
        reference = as_utc_datetime(
            active_plan.get("activated_at") or now,
            field_name="active_plan_reference",
        )
        now_utc = as_utc_datetime(now, field_name="now")
        step_seconds = max(
            1,
            int(
                active_plan.get("collision_plan", {}).get("time_step_seconds")
                or active_plan.get("time_step_seconds")
                or 5
            ),
        )
        now_step = max(
            0,
            math.floor((now_utc - reference).total_seconds() / step_seconds),
        )
        tasks = [
            ScheduledTask.model_validate(row)
            for row in active_plan.get("cuopt_plan", {}).get(
                "scheduled_tasks", []
            )
        ]
        dependencies = [
            TaskDependency.model_validate(row)
            for row in active_plan.get("task_dependencies", [])
        ]
        ready, waiting = ready_task_ids(
            tasks,
            dependencies,
            completed_work_ids=completed_work_ids,
            now_step=now_step,
        )
        return {
            "now_step": now_step,
            "ready_task_ids": ready,
            "waiting_task_ids": waiting,
        }

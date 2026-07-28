from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Any, Iterable

from app.models import ScheduledTask


def dependency_aware_robot_task_ids(
    tasks: Iterable[ScheduledTask],
    dependencies: Iterable[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Return deterministic per-robot order respecting execution dependencies.

    Scheduled start time is the primary ordering signal. Priority is only a
    tie-breaker. Same-robot dependency edges override conflicting timestamps so
    a CHARGE/MOVE chain cannot be executed ahead of its predecessor merely
    because a synthetic task inherited a smaller priority value.
    """

    task_rows = list(tasks)
    by_id = {str(task.task_id): task for task in task_rows}
    grouped: dict[str, list[ScheduledTask]] = defaultdict(list)
    for task in task_rows:
        grouped[str(task.robot_id)].append(task)

    dependency_rows = list(dependencies or [])
    result: dict[str, list[str]] = {}
    errors: list[str] = []

    for robot_id, robot_tasks in sorted(grouped.items()):
        robot_ids = {str(task.task_id) for task in robot_tasks}
        adjacency: dict[str, set[str]] = {task_id: set() for task_id in robot_ids}
        indegree: dict[str, int] = {task_id: 0 for task_id in robot_ids}

        for raw in dependency_rows:
            predecessor_id = str(raw.get("predecessor_task_id") or "")
            successor_id = str(raw.get("successor_task_id") or "")
            if predecessor_id not in robot_ids or successor_id not in robot_ids:
                continue
            predecessor = by_id[predecessor_id]
            successor = by_id[successor_id]
            if str(predecessor.robot_id) != str(successor.robot_id):
                continue
            if successor_id in adjacency[predecessor_id]:
                continue
            adjacency[predecessor_id].add(successor_id)
            indegree[successor_id] += 1

        ready: list[tuple[int, int, int, str]] = []
        for task_id, degree in indegree.items():
            if degree:
                continue
            task = by_id[task_id]
            heapq.heappush(
                ready,
                (
                    int(task.start_time_step),
                    int(task.end_time_step),
                    int(task.priority),
                    task_id,
                ),
            )

        ordered: list[str] = []
        while ready:
            _start, _end, _priority, task_id = heapq.heappop(ready)
            ordered.append(task_id)
            for successor_id in sorted(adjacency[task_id]):
                indegree[successor_id] -= 1
                if indegree[successor_id] != 0:
                    continue
                successor = by_id[successor_id]
                heapq.heappush(
                    ready,
                    (
                        int(successor.start_time_step),
                        int(successor.end_time_step),
                        int(successor.priority),
                        successor_id,
                    ),
                )

        if len(ordered) != len(robot_ids):
            cycle_ids = sorted(robot_ids - set(ordered))
            errors.append(
                "ROBOT_TASK_DEPENDENCY_CYCLE: "
                f"robot={robot_id} tasks={cycle_ids}"
            )
            # Keep deterministic output for diagnostics. Callers must treat the
            # accompanying error as blocking and must not execute this order.
            ordered.extend(
                sorted(
                    robot_ids - set(ordered),
                    key=lambda task_id: (
                        int(by_id[task_id].start_time_step),
                        int(by_id[task_id].end_time_step),
                        int(by_id[task_id].priority),
                        task_id,
                    ),
                )
            )

        result[robot_id] = ordered

    return result, errors

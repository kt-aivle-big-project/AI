"""Lossless presentation-layer compression for long scheduled WAIT ranges.

The planner and simulator keep their time-expanded waypoints for deterministic
collision checking.  This module only compresses evidence, LLM payloads and
public API views so long idle windows do not create multi-megabyte responses.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


WAIT_ACTION = "WAIT"
CHARGE_ACTION = "CHARGE"
RANGE_ACTIONS = {WAIT_ACTION, CHARGE_ACTION}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _same_wait_segment(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("action") or "").upper() == WAIT_ACTION
        and str(right.get("action") or "").upper() == WAIT_ACTION
        and left.get("from_node") == right.get("from_node")
        and left.get("to_node") == right.get("to_node")
        and left.get("to_node") == right.get("from_node")
        and left.get("source") == right.get("source")
        and int(left.get("arrive_step") or 0) == int(right.get("depart_step") or 0)
    )


def compress_route_segments(segments: Iterable[Any]) -> list[dict[str, Any]]:
    """Merge adjacent same-node WAIT segments into one duration range."""

    result: list[dict[str, Any]] = []
    for raw in segments:
        row = deepcopy(_as_dict(raw))
        if not row:
            continue
        if result and _same_wait_segment(result[-1], row):
            previous = result[-1]
            previous["arrive_step"] = row.get("arrive_step")
            previous["travel_steps"] = max(
                0,
                int(previous.get("arrive_step") or 0)
                - int(previous.get("depart_step") or 0),
            )
            previous["distance"] = 0.0
            continue
        result.append(row)
    return result


def _wait_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("robot_id"),
        row.get("task_id"),
        row.get("node_id"),
        row.get("reason"),
        row.get("conflict_type"),
        row.get("blocked_resource"),
        row.get("blocked_by_robot_id"),
        row.get("blocked_by_task_id"),
    )


def compress_wait_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Merge consecutive wait evidence rows while preserving total delay."""

    result: list[dict[str, Any]] = []
    for raw in rows:
        row = deepcopy(_as_dict(raw))
        if not row:
            continue
        row["added_delay_steps"] = max(1, int(row.get("added_delay_steps") or 1))
        row_start = int(row.get("time_step") or 0)
        row_end = row_start + row["added_delay_steps"]
        if result:
            previous = result[-1]
            previous_start = int(previous.get("time_step") or 0)
            previous_end = previous_start + int(
                previous.get("added_delay_steps") or 1
            )
            if _wait_key(previous) == _wait_key(row) and previous_end == row_start:
                previous["added_delay_steps"] = row_end - previous_start
                previous["end_time_step"] = row_end
                continue
        row["end_time_step"] = row_end
        result.append(row)
    return result


def compress_resolution_events(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Compress WAIT resolution events using the same evidence semantics."""

    wait_rows: list[dict[str, Any]] = []
    passthrough: list[tuple[int, dict[str, Any]]] = []
    for index, raw in enumerate(rows):
        row = deepcopy(_as_dict(raw))
        if str(row.get("resolution") or "").upper() == WAIT_ACTION:
            row["_original_index"] = index
            wait_rows.append(row)
        elif row:
            passthrough.append((index, row))

    compressed_waits = compress_wait_rows(wait_rows)
    ordered: list[tuple[int, dict[str, Any]]] = list(passthrough)
    for row in compressed_waits:
        index = int(row.pop("_original_index", 0))
        ordered.append((index, row))
    return [row for _, row in sorted(ordered, key=lambda item: item[0])]


def compress_waypoints(waypoints: Iterable[Any], *, time_step_seconds: int = 1) -> list[dict[str, Any]]:
    """Collapse repeated same-node WAIT/CHARGE waypoints into public ranges.

    MOVE/PICK/DROP waypoints remain explicit. A compressed WAIT or CHARGE row
    has ``time_step`` (start), ``end_time_step`` and duration fields.
    """

    rows = [deepcopy(_as_dict(raw)) for raw in waypoints if _as_dict(raw)]
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        action = str(row.get("action") or "MOVE").upper()
        if action not in RANGE_ACTIONS:
            result.append(row)
            index += 1
            continue

        start = int(row.get("time_step") or 0)
        end = start
        node_id = row.get("node_id")
        cursor = index + 1
        while cursor < len(rows):
            candidate = rows[cursor]
            if (
                str(candidate.get("action") or "MOVE").upper() != action
                or candidate.get("node_id") != node_id
            ):
                break
            candidate_step = int(candidate.get("time_step") or 0)
            if candidate_step > end + 1:
                break
            end = max(end, candidate_step)
            cursor += 1

        duration_steps = max(1, end - start + 1)
        compressed = row
        compressed["time_step"] = start
        compressed["end_time_step"] = end
        compressed["duration_steps"] = duration_steps
        compressed["duration_seconds"] = duration_steps * max(1, time_step_seconds)
        result.append(compressed)
        index = cursor
    return result


def compress_timeline(rows: Iterable[Any], *, time_step_seconds: int = 1) -> list[dict[str, Any]]:
    """Compress consecutive WAIT/CHARGE events for one robot and node."""

    result: list[dict[str, Any]] = []
    for raw in rows:
        row = deepcopy(_as_dict(raw))
        if not row:
            continue
        action = str(row.get("event") or "").upper()
        is_range = action in RANGE_ACTIONS
        if is_range and result:
            previous = result[-1]
            previous_action = str(previous.get("event") or "").upper()
            previous_end = int(
                previous.get("end_time_step", previous.get("time_step", 0)) or 0
            )
            current_step = int(row.get("time_step") or 0)
            if (
                previous_action == action
                and previous.get("robot_id") == row.get("robot_id")
                and previous.get("node_id") == row.get("node_id")
                and current_step == previous_end + 1
            ):
                start = int(previous.get("time_step") or 0)
                previous["end_time_step"] = current_step
                previous["duration_steps"] = current_step - start + 1
                previous["duration_seconds"] = (
                    previous["duration_steps"] * max(1, time_step_seconds)
                )
                continue
        if is_range:
            row["end_time_step"] = int(row.get("time_step") or 0)
            row["duration_steps"] = 1
            row["duration_seconds"] = max(1, time_step_seconds)
        result.append(row)
    return result


def wait_summary(rows: Iterable[Any]) -> dict[str, Any]:
    raw_rows = [deepcopy(_as_dict(row)) for row in rows if _as_dict(row)]
    compressed = compress_wait_rows(raw_rows)
    total_steps = sum(max(1, int(row.get("added_delay_steps") or 1)) for row in raw_rows)
    return {
        "wait_step_count": total_steps,
        "compressed_wait_range_count": len(compressed),
        "wait_ranges": compressed,
    }


def compact_route_metadata_for_llm(metadata: Any, *, max_wait_ranges: int = 20) -> dict[str, Any]:
    """Return bounded routing metadata suitable for an LLM verification prompt."""

    source = deepcopy(_as_dict(metadata))
    waits = source.get("wait_evidence") or []
    events = source.get("resolution_events") or []
    wait_info = wait_summary(waits)
    wait_info["wait_ranges"] = wait_info["wait_ranges"][:max_wait_ranges]
    compressed_events = compress_resolution_events(events)
    allowed_keys = (
        "routing_backend",
        "vertex_reservations",
        "edge_reservations",
        "reroute_count",
        "conflict_wait_count",
        "route_sources",
        "preserved_prefix_end_steps",
        "task_completion_steps",
        "task_start_steps",
    )
    result = {key: source.get(key) for key in allowed_keys if key in source}
    result["wait_summary"] = wait_info
    result["resolution_event_count"] = len(events)
    result["resolution_event_ranges"] = compressed_events[:max_wait_ranges]
    result["payload_policy"] = "RAW_TIME_EXPANDED_WAIT_ROWS_OMITTED"
    return result


def compact_debug_payload_for_llm(
    payload: Any,
    *,
    max_route_segments_per_robot: int = 40,
    max_wait_ranges: int = 20,
    max_candidates_per_task: int = 5,
) -> dict[str, Any]:
    """Bound DEBUG evidence while preserving decision-critical facts."""

    result = deepcopy(_as_dict(payload))
    routing = _as_dict(result.get("routing_and_reservations"))
    routes = _as_dict(routing.get("routes"))
    for raw_route in routes.get("routes") or []:
        route = _as_dict(raw_route)
        raw_segments = route.get("segments") or []
        compressed = compress_route_segments(raw_segments)
        route["segments"] = compressed[:max_route_segments_per_robot]
        route["segment_count_raw"] = len(raw_segments)
        route["segment_count_compressed"] = len(compressed)
        route["segments_truncated_for_llm"] = len(compressed) > max_route_segments_per_robot
    reservations = _as_dict(routing.get("reservations"))
    raw_waits = reservations.get("waits") or []
    compressed_waits = compress_wait_rows(raw_waits)
    reservations["waits"] = compressed_waits[:max_wait_ranges]
    reservations["wait_range_count"] = len(compressed_waits)
    reservations["waits_truncated_for_llm"] = len(compressed_waits) > max_wait_ranges
    raw_events = reservations.get("resolution_events") or []
    compressed_events = compress_resolution_events(raw_events)
    reservations["resolution_events"] = compressed_events[:max_wait_ranges]
    reservations["resolution_event_range_count"] = len(compressed_events)
    reservations["resolution_events_truncated_for_llm"] = (
        len(compressed_events) > max_wait_ranges
    )

    candidate_evaluation = _as_dict(result.get("candidate_evaluation"))
    for raw_task in candidate_evaluation.get("task_evidence") or []:
        task = _as_dict(raw_task)
        candidates = task.get("candidates") or []
        task["candidates"] = candidates[:max_candidates_per_task]
        task["candidates_truncated_for_llm"] = len(candidates) > max_candidates_per_task

    result["llm_payload_policy"] = {
        "wait_rows": "COMPRESSED",
        "route_segments_per_robot_limit": max_route_segments_per_robot,
        "wait_range_limit": max_wait_ranges,
        "candidate_limit_per_task": max_candidates_per_task,
    }
    return result


def compress_debug_payload_for_presentation(payload: Any) -> dict[str, Any]:
    """Compress repeated waits for DEBUG output without truncating evidence."""

    result = compact_debug_payload_for_llm(
        payload,
        max_route_segments_per_robot=1_000_000,
        max_wait_ranges=1_000_000,
        max_candidates_per_task=1_000_000,
    )
    result.pop("llm_payload_policy", None)
    routing = _as_dict(result.get("routing_and_reservations"))
    routes = _as_dict(routing.get("routes"))
    for raw_route in routes.get("routes") or []:
        _as_dict(raw_route).pop("segments_truncated_for_llm", None)
    reservations = _as_dict(routing.get("reservations"))
    reservations.pop("waits_truncated_for_llm", None)
    reservations.pop("resolution_events_truncated_for_llm", None)
    candidate_evaluation = _as_dict(result.get("candidate_evaluation"))
    for raw_task in candidate_evaluation.get("task_evidence") or []:
        _as_dict(raw_task).pop("candidates_truncated_for_llm", None)
    result["presentation_compression"] = {
        "mode": "CONSECUTIVE_WAIT_AND_CHARGE_RANGES",
        "compressed_actions": ["WAIT", "CHARGE"],
        "truncated": False,
    }
    return result

"""P16.5.8 long-idle charger return and opportunity charging.

This module augments an optimizer plan *before* time-expanded routing.  It does
not replace cuOpt assignment/order decisions.  It only inserts feasible CHARGE
visits inside already-existing idle gaps and records the non-charging charger
area return policy for the routing layer.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Any

from app.models import CuOptPlan, ScheduledTask
from app.services.charger_selection import rank_opportunity_charger_candidates


UNAVAILABLE_STATUSES = {
    "FAILED",
    "ROBOT_FAILED",
    "OFFLINE",
    "MAINTENANCE",
    "DISABLED",
}


def _closed_resources(problem: dict[str, Any]) -> tuple[set[int], set[tuple[int, int]]]:
    closed_nodes: set[int] = set()
    closed_edges: set[tuple[int, int]] = set()
    for row in problem.get("temporary_closures", []):
        if row.get("node_id") is not None:
            closed_nodes.add(int(row["node_id"]))
        if row.get("from_node") is not None and row.get("to_node") is not None:
            edge = (int(row["from_node"]), int(row["to_node"]))
            closed_edges.add(edge)
            if bool(row.get("bidirectional")) or str(
                row.get("direction") or ""
            ).upper() in {"BOTH", "BIDIRECTIONAL"}:
                closed_edges.add((edge[1], edge[0]))
    return closed_nodes, closed_edges


def _graph(problem: dict[str, Any]) -> dict[int, list[tuple[int, float, float]]]:
    closed_nodes, closed_edges = _closed_resources(problem)
    valid_nodes = {
        int(row["node_id"])
        for row in problem.get("nodes", [])
        if row.get("active", True) and int(row["node_id"]) not in closed_nodes
    }
    graph: dict[int, list[tuple[int, float, float]]] = {
        node_id: [] for node_id in valid_nodes
    }
    for edge in problem.get("edges", []):
        start = int(edge["from_node"])
        target = int(edge["to_node"])
        if (
            not edge.get("active", True)
            or start not in valid_nodes
            or target not in valid_nodes
            or (start, target) in closed_edges
        ):
            continue
        distance = float(edge.get("distance") or 1.0)
        seconds = float(edge.get("travel_seconds") or distance)
        graph[start].append((target, distance, seconds))
        if str(edge.get("direction") or "ONE_WAY").upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        } and (target, start) not in closed_edges:
            graph[target].append((start, distance, seconds))
    for rows in graph.values():
        rows.sort(key=lambda value: (value[0], value[1], value[2]))
    return graph


def _shortest(
    graph: dict[int, list[tuple[int, float, float]]],
    start: int,
    target: int,
) -> tuple[float, float] | None:
    if start == target and start in graph:
        return 0.0, 0.0
    if start not in graph or target not in graph:
        return None
    queue: list[tuple[float, float, int]] = [(0.0, 0.0, start)]
    best: dict[int, tuple[float, float]] = {start: (0.0, 0.0)}
    while queue:
        distance, seconds, node = heapq.heappop(queue)
        if best.get(node) != (distance, seconds):
            continue
        if node == target:
            return distance, seconds
        for neighbor, edge_distance, edge_seconds in graph.get(node, []):
            candidate = (distance + edge_distance, seconds + edge_seconds)
            if candidate < best.get(neighbor, (math.inf, math.inf)):
                best[neighbor] = candidate
                heapq.heappush(queue, (*candidate, neighbor))
    return None


def _charger_cost(node: dict[str, Any]) -> float | None:
    properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    for name in (
        "charging_cost",
        "charge_cost",
        "charger_cost",
        "price_per_percent",
        "cost",
    ):
        value = node.get(name, properties.get(name))
        if value in (None, ""):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric >= 0:
            return numeric
    return None


def _robot_battery(row: dict[str, Any]) -> float:
    for key in ("battery", "battery_percent"):
        if row.get(key) is not None:
            return max(0.0, min(100.0, float(row[key])))
    return 100.0


def _linked_waiting_areas(problem: dict[str, Any]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = defaultdict(list)
    for row in problem.get("nodes", []):
        if not row.get("active", True):
            continue
        linked = row.get("linked_charger_node_id")
        if linked is None:
            continue
        node_type = str(row.get("node_type") or row.get("type") or "").upper()
        if node_type not in {
            "PARKING",
            "STAGING",
            "HOLDING",
            "CHARGER_WAITING_AREA",
            "ROBOT_PARKING",
        } and not bool(row.get("idle_allowed")):
            continue
        result[int(linked)].append(int(row["node_id"]))
    return {key: sorted(set(values)) for key, values in result.items()}


def _overlaps(start: int, end: int, reservations: list[tuple[int, int]]) -> bool:
    return any(start < reserved_end and reserved_start < end for reserved_start, reserved_end in reservations)


def augment_plan_with_opportunity_charging(
    problem: dict[str, Any],
    plan: CuOptPlan,
) -> tuple[CuOptPlan, dict[str, Any]]:
    """Insert opportunity CHARGE tasks that fit completely inside idle gaps.

    The function is deliberately bounded and deterministic:
    - original business task robot/order/window assignments are preserved;
    - no original task is delayed;
    - only active, safely reachable chargers are considered;
    - charger slot overlap is prevented before MAPF routing;
    - chargers linked to an explicit waiting area are preferred.
    """

    if problem.get("cuopt_charge_visits_preoptimized"):
        contract = dict(problem.get("charge_visit_optimization_contract") or {})
        evidence = dict(contract.get("opportunity_charging") or {})
        evidence.update(
            {
                "enabled": bool(contract.get("enabled", True)),
                "preoptimized": True,
                "optimizer_stage": "CUOPT_SECOND_PASS",
                "inserted_charge_task_count": len(
                    problem.get("explicit_charge_task_specs") or {}
                ),
                "explicit_charge_task_ids": sorted(
                    problem.get("explicit_charge_task_specs") or {}
                ),
            }
        )
        metadata = dict(plan.metadata)
        metadata["opportunity_charging"] = evidence
        metadata["charge_visit_optimization_contract"] = contract
        return plan.model_copy(update={"metadata": metadata}), evidence

    enabled = bool(problem.get("opportunity_charging_enabled", False))
    hard_constraints = {
        str(value).upper() for value in problem.get("hard_constraints", [])
    }
    enabled = enabled or "OPPORTUNITY_CHARGING" in hard_constraints
    if not enabled:
        metadata = dict(plan.metadata)
        metadata["opportunity_charging"] = {
            "enabled": False,
            "inserted_charge_task_count": 0,
            "decisions": [],
        }
        return plan.model_copy(update={"metadata": metadata}), metadata["opportunity_charging"]

    graph = _graph(problem)
    time_step_seconds = max(1, int(problem.get("time_step_seconds") or 5))
    energy_per_distance = max(0.0, float(problem.get("energy_per_distance") or 0.0))
    minimum_battery = max(0.0, float(problem.get("min_robot_battery") or 0.0))
    safety_margin = max(0.0, float(problem.get("battery_safety_margin_percent") or 0.5))
    safe_arrival = minimum_battery + safety_margin
    operation_target = max(0.0, float(problem.get("charge_target_battery") or 80.0))
    opportunity_target = max(
        operation_target,
        min(100.0, float(problem.get("opportunity_charge_target_battery") or 95.0)),
    )
    charge_rate = max(
        0.001,
        float(problem.get("charge_rate_percent_per_minute") or 5.0),
    )
    minimum_gap_steps = max(
        2,
        int(
            problem.get("opportunity_charge_min_gap_steps")
            or math.ceil(15 * 60 / time_step_seconds)
        ),
    )
    minimum_gain = max(
        0.0, float(problem.get("opportunity_charge_min_gain_percent") or 2.0)
    )
    activation_horizon_steps = max(
        1, int(problem.get("max_mapf_time_steps") or 720)
    )

    closed_nodes, _ = _closed_resources(problem)
    linked_waiting = _linked_waiting_areas(problem)
    chargers = [
        row
        for row in problem.get("nodes", [])
        if row.get("active", True)
        and int(row["node_id"]) not in closed_nodes
        and str(row.get("node_type") or "").upper() == "CHARGER"
    ]
    chargers.sort(key=lambda row: int(row["node_id"]))

    robots = {
        str(row["robot_id"]): row
        for row in problem.get("robots", [])
        if str(row.get("status") or "IDLE").upper() not in UNAVAILABLE_STATUSES
    }
    # Opportunity tasks are synthetic and must be regenerated from the current
    # business schedule on every planning/replanning pass.  Keeping stale
    # synthetic tasks from a rejected candidate can duplicate charger visits or
    # preserve reservations that no longer fit the revised schedule.
    base_tasks = [
        task
        for task in plan.scheduled_tasks
        if not (task.action == "CHARGE" and task.task_id.startswith("opportunity:"))
    ]
    grouped: dict[str, list[ScheduledTask]] = defaultdict(list)
    for task in base_tasks:
        grouped[str(task.robot_id)].append(task)
    for rows in grouped.values():
        rows.sort(key=lambda task: (task.start_time_step, task.priority, task.task_id))

    inserted: list[ScheduledTask] = []
    decisions: list[dict[str, Any]] = []
    charger_reservations: dict[int, list[tuple[int, int]]] = defaultdict(list)
    dependencies = [
        dict(row)
        for row in plan.metadata.get("execution_task_dependencies", [])
        if isinstance(row, dict)
        and not str(row.get("predecessor_task_id") or "").startswith("opportunity:")
        and not str(row.get("successor_task_id") or "").startswith("opportunity:")
    ]
    charger_selections = [
        dict(row)
        for row in plan.metadata.get("charger_selections", [])
        if isinstance(row, dict)
        and not str(row.get("task_id") or "").startswith("opportunity:")
    ]
    added_distance = 0.0
    added_energy = 0.0

    def add_dependency(predecessor: str | None, successor: str, reason: str) -> None:
        if not predecessor:
            return
        row = {
            "predecessor_task_id": predecessor,
            "successor_task_id": successor,
            "reason": reason,
        }
        if row not in dependencies:
            dependencies.append(row)

    for robot_id, tasks in sorted(grouped.items()):
        robot = robots.get(robot_id)
        if robot is None:
            continue
        current_node = int(robot["node_id"])
        current_time = 0
        battery = _robot_battery(robot)
        previous_task_id: str | None = None
        previous_was_charge = False

        for task in tasks:
            gap_steps = int(task.start_time_step) - int(current_time)
            decision_base = {
                "robot_id": robot_id,
                "next_task_id": task.task_id,
                "gap_start_step": int(current_time),
                "gap_end_step": int(task.start_time_step),
                "gap_steps": int(gap_steps),
                "battery_before_gap": round(float(battery), 6),
                "current_node": int(current_node),
                "next_source_node": int(task.source_node),
            }

            initial_pre_activation_gap = bool(
                problem.get("defer_initial_pre_activation")
                and previous_task_id is None
                and current_time == 0
            )
            can_evaluate = bool(
                task.action != "CHARGE"
                and not previous_was_charge
                and not initial_pre_activation_gap
                and gap_steps >= minimum_gap_steps
                and chargers
            )
            chosen: dict[str, Any] | None = None
            candidate_rows: list[dict[str, Any]] = []
            if can_evaluate:
                for charger in chargers:
                    charger_node = int(charger["node_id"])
                    to_charger = _shortest(graph, current_node, charger_node)
                    to_next = _shortest(graph, charger_node, int(task.source_node))
                    if to_charger is None or to_next is None:
                        candidate_rows.append(
                            {
                                "charger_node": charger_node,
                                "selected": False,
                                "safe_reachable": False,
                                "rejection_reason": "CHARGER_OR_NEXT_TASK_UNREACHABLE",
                            }
                        )
                        continue
                    battery_at_charger = battery - to_charger[0] * energy_per_distance
                    if battery_at_charger + 1e-9 < safe_arrival:
                        candidate_rows.append(
                            {
                                "charger_node": charger_node,
                                "selected": False,
                                "safe_reachable": False,
                                "battery_at_charger": round(battery_at_charger, 6),
                                "minimum_arrival_battery": round(safe_arrival, 6),
                                "rejection_reason": "BATTERY_BELOW_SAFE_ARRIVAL_THRESHOLD",
                            }
                        )
                        continue
                    gain = max(0.0, min(100.0 - battery_at_charger, opportunity_target - battery_at_charger))
                    if gain + 1e-9 < minimum_gain:
                        candidate_rows.append(
                            {
                                "charger_node": charger_node,
                                "selected": False,
                                "safe_reachable": True,
                                "battery_at_charger": round(battery_at_charger, 6),
                                "charged_percent": round(gain, 6),
                                "rejection_reason": "OPPORTUNITY_TARGET_ALREADY_MET",
                            }
                        )
                        continue
                    travel_steps = math.ceil(to_charger[1] / time_step_seconds)
                    charge_steps = math.ceil(
                        (gain / charge_rate) * 60 / time_step_seconds
                    )
                    charge_seconds = charge_steps * time_step_seconds
                    arrival_step = current_time + travel_steps
                    end_step = arrival_step + charge_steps
                    if end_step > int(task.start_time_step):
                        candidate_rows.append(
                            {
                                "charger_node": charger_node,
                                "selected": False,
                                "safe_reachable": True,
                                "charged_percent": round(gain, 6),
                                "required_end_step": int(end_step),
                                "gap_end_step": int(task.start_time_step),
                                "rejection_reason": "IDLE_GAP_TOO_SHORT_FOR_CHARGE",
                            }
                        )
                        continue
                    if _overlaps(
                        arrival_step,
                        end_step,
                        charger_reservations[charger_node],
                    ):
                        candidate_rows.append(
                            {
                                "charger_node": charger_node,
                                "selected": False,
                                "safe_reachable": True,
                                "charge_start_step": int(arrival_step),
                                "charge_end_step": int(end_step),
                                "rejection_reason": "CHARGER_SLOT_ALREADY_RESERVED",
                            }
                        )
                        continue
                    configured_cost = _charger_cost(charger)
                    row = {
                        "charger_node": charger_node,
                        "selected": False,
                        "safe_reachable": True,
                        "linked_waiting_area_node_ids": linked_waiting.get(charger_node, []),
                        "to_charger_distance": round(float(to_charger[0]), 6),
                        "to_charger_seconds": round(float(to_charger[1]), 6),
                        "charger_to_next_source_distance": round(float(to_next[0]), 6),
                        "battery_at_charger": round(float(battery_at_charger), 6),
                        "charged_percent": round(float(gain), 6),
                        "target_battery": round(float(battery_at_charger + gain), 6),
                        "charge_duration_seconds": int(charge_seconds),
                        "charge_start_step": int(arrival_step),
                        "charge_end_step": int(end_step),
                        "charger_cost": configured_cost,
                        "rejection_reason": None,
                    }
                    candidate_rows.append(row)

                candidate_rows, selected_candidate, selection_policy, selection_reason = (
                    rank_opportunity_charger_candidates(candidate_rows)
                )
                if selected_candidate is not None:
                    chosen = {
                        **selected_candidate,
                        "task_start_step": int(current_time),
                        "task_end_step": int(selected_candidate["charge_end_step"]),
                        "selection_policy": selection_policy,
                        "selection_reason": selection_reason,
                    }

            if chosen is not None:
                charger_node = int(chosen["charger_node"])
                charge_task_id = (
                    f"opportunity:{robot_id}:{task.task_id}:charge:{charger_node}"
                )
                charge_task = ScheduledTask(
                    task_id=charge_task_id,
                    work_id=task.work_id,
                    action="CHARGE",
                    robot_id=robot_id,
                    source_node=int(current_node),
                    target_node=charger_node,
                    start_time_step=int(chosen["task_start_step"]),
                    end_time_step=int(chosen["task_end_step"]),
                    priority=int(task.priority),
                    estimated_distance=float(chosen["to_charger_distance"]),
                    estimated_energy=float(chosen["to_charger_distance"]) * energy_per_distance,
                    charge_target_battery=float(chosen["target_battery"]),
                    charged_percent=float(chosen["charged_percent"]),
                    charge_duration_seconds=int(chosen["charge_duration_seconds"]),
                    charger_cost=chosen.get("charger_cost"),
                    charger_selection_policy=str(chosen["selection_policy"]),
                    charger_selection_reason=str(chosen["selection_reason"]),
                    charger_candidates=candidate_rows,
                    schedule_status="READY",
                )
                inserted.append(charge_task)
                add_dependency(previous_task_id, charge_task_id, "OPPORTUNITY_CHARGING")
                add_dependency(charge_task_id, task.task_id, "OPPORTUNITY_CHARGING")
                charger_reservations[charger_node].append(
                    (int(chosen["charge_start_step"]), int(chosen["charge_end_step"]))
                )
                charger_reservations[charger_node].sort()
                charger_selections.append(
                    {
                        "task_id": charge_task_id,
                        "robot_id": robot_id,
                        "selected_charger_node": charger_node,
                        "selection_policy": str(chosen["selection_policy"]),
                        "selection_reason": str(chosen["selection_reason"]),
                        "cost_mode": chosen.get("cost_mode"),
                        "total_selection_cost": chosen.get("total_selection_cost"),
                        "charger_cost": chosen.get("charger_cost"),
                        "battery_before_travel": round(float(battery), 6),
                        "battery_at_charger": float(chosen["battery_at_charger"]),
                        "charged_percent": float(chosen["charged_percent"]),
                        "target_battery": float(chosen["target_battery"]),
                        "projected_final_battery": float(chosen["target_battery"]),
                        "charge_duration_seconds": int(chosen["charge_duration_seconds"]),
                        "linked_waiting_area_node_ids": chosen.get(
                            "linked_waiting_area_node_ids", []
                        ),
                        "opportunity_charge": True,
                        "candidates": candidate_rows,
                    }
                )
                decisions.append(
                    {
                        **decision_base,
                        "selected_action": "RETURN_TO_CHARGER_AND_CHARGE",
                        "selected_charger_node": charger_node,
                        "linked_waiting_area_node_ids": chosen.get(
                            "linked_waiting_area_node_ids", []
                        ),
                        "charged_percent": float(chosen["charged_percent"]),
                        "target_battery": float(chosen["target_battery"]),
                        "charge_duration_seconds": int(chosen["charge_duration_seconds"]),
                        "charge_task_id": charge_task_id,
                        "candidate_count": len(candidate_rows),
                    }
                )
                added_distance += float(chosen["to_charger_distance"])
                added_energy += float(chosen["to_charger_distance"]) * energy_per_distance
                current_node = charger_node
                current_time = int(chosen["task_end_step"])
                battery = float(chosen["target_battery"])
                previous_task_id = charge_task_id
                previous_was_charge = True
            elif initial_pre_activation_gap:
                decisions.append(
                    {
                        **decision_base,
                        "selected_action": "DEFER_UNTIL_PLAN_ACTIVATION",
                        "reason": "INITIAL_GAP_OUTSIDE_MAPF_ACTIVATION_HORIZON",
                        "activation_horizon_steps": activation_horizon_steps,
                        "candidate_count": 0,
                        "candidates": [],
                    }
                )
            elif gap_steps >= minimum_gap_steps:
                decisions.append(
                    {
                        **decision_base,
                        "selected_action": "RETURN_TO_CHARGER_AREA_AND_WAIT",
                        "reason": (
                            "OPPORTUNITY_TARGET_ALREADY_MET_OR_NO_FEASIBLE_CHARGE_SLOT"
                        ),
                        "candidate_count": len(candidate_rows),
                        "candidates": candidate_rows,
                    }
                )

            # Apply the original/mandatory task to the projected robot state.
            travel_energy = max(0.0, float(task.estimated_energy or 0.0))
            battery = max(0.0, battery - travel_energy)
            if task.action == "CHARGE":
                if task.charge_target_battery is not None:
                    battery = max(battery, float(task.charge_target_battery))
                else:
                    battery = min(100.0, battery + float(task.charged_percent or 0.0))
            current_node = int(task.target_node)
            current_time = max(int(current_time), int(task.end_time_step))
            previous_task_id = task.task_id
            previous_was_charge = task.action == "CHARGE"

    scheduled_tasks = [*base_tasks, *inserted]
    scheduled_tasks.sort(
        key=lambda task: (
            task.start_time_step,
            task.priority,
            task.robot_id,
            0 if task.action == "CHARGE" else 1,
            task.task_id,
        )
    )

    metadata = dict(plan.metadata)
    metadata["execution_task_dependencies"] = dependencies
    metadata["charger_selections"] = charger_selections
    metadata["opportunity_charging"] = {
        "enabled": True,
        "policy": "LONG_IDLE_CHARGER_AREA_FIRST",
        "minimum_gap_steps": minimum_gap_steps,
        "minimum_gap_seconds": minimum_gap_steps * time_step_seconds,
        "target_battery_percent": round(opportunity_target, 6),
        "minimum_gain_percent": round(minimum_gain, 6),
        "inserted_charge_task_count": len(inserted),
        "inserted_charge_task_ids": [task.task_id for task in inserted],
        "charger_slot_reservations": {
            str(node_id): [
                {"start_step": start, "end_step": end}
                for start, end in reservations
            ]
            for node_id, reservations in sorted(charger_reservations.items())
        },
        "decisions": decisions,
    }
    metadata["idle_return_policy"] = {
        "policy": "CHARGER_AREA_FIRST",
        "charger_slot_idle_allowed": False,
        "post_charge_behavior": "LEAVE_SLOT_TO_LINKED_WAITING_AREA",
        "fallback": "WHITELISTED_PARKING_OR_STAGING",
    }
    weights = problem.get("weights") or {}
    objective_delta = (
        added_distance * float(weights.get("total_distance", 1.0))
        + added_energy * float(weights.get("energy", 1.0))
    )
    metadata["opportunity_charging_objective_delta"] = round(objective_delta, 6)
    metadata["total_distance"] = round(
        float(metadata.get("total_distance") or 0.0) + added_distance, 6
    )
    metadata["energy"] = round(
        float(metadata.get("energy") or 0.0) + added_energy, 6
    )

    augmented = plan.model_copy(
        update={
            "scheduled_tasks": scheduled_tasks,
            "changed_robot_ids": sorted(
                set(plan.changed_robot_ids)
                | {task.robot_id for task in inserted}
            ),
            "objective_value": round(float(plan.objective_value) + objective_delta, 6),
            "metadata": metadata,
        }
    )
    return augmented, metadata["opportunity_charging"]

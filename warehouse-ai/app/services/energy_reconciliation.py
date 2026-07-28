from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.models import CollisionFreePlan, CuOptPlan, ScheduledTask, TimedRoute


@dataclass(frozen=True)
class RouteDistanceProfile:
    total_distance: float
    cumulative_by_time_step: dict[int, float]
    charge_runs: list[dict[str, int]]


def _active_edge_distances(problem: dict[str, Any]) -> dict[tuple[int, int], float]:
    result: dict[tuple[int, int], float] = {}
    for raw in problem.get("edges", []):
        if not raw.get("active", True):
            continue
        start = int(raw["from_node"])
        target = int(raw["to_node"])
        distance = float(raw.get("distance") or 0.0)
        result[(start, target)] = distance
        if str(raw.get("direction", "ONE_WAY")).upper() in {
            "BOTH",
            "BIDIRECTIONAL",
        }:
            result[(target, start)] = distance
    return result


def route_distance_profile(
    route: TimedRoute,
    problem: dict[str, Any],
) -> RouteDistanceProfile:
    waypoints = sorted(route.waypoints, key=lambda row: row.time_step)
    distances = _active_edge_distances(problem)
    cumulative: dict[int, float] = {}
    running = 0.0
    if waypoints:
        cumulative[waypoints[0].time_step] = 0.0
    for previous, current in zip(waypoints, waypoints[1:]):
        if previous.node_id != current.node_id:
            running += float(
                distances.get(
                    (previous.node_id, current.node_id),
                    0.0,
                )
            )
        cumulative[current.time_step] = running

    # The routing engine's route.distance is authoritative. Scale the
    # cumulative profile when aliases or legacy edges omitted a distance.
    authoritative = float(route.distance or running)
    if running > 0 and not math.isclose(running, authoritative, abs_tol=1e-9):
        factor = authoritative / running
        cumulative = {step: value * factor for step, value in cumulative.items()}
        running = authoritative
    elif running == 0 and authoritative > 0 and waypoints:
        last_step = max(1, waypoints[-1].time_step - waypoints[0].time_step)
        cumulative = {
            row.time_step: authoritative
            * (row.time_step - waypoints[0].time_step)
            / last_step
            for row in waypoints
        }
        running = authoritative

    charge_runs: list[dict[str, int]] = []
    current_run: dict[str, int] | None = None
    for waypoint in waypoints:
        if waypoint.action == "CHARGE":
            if (
                current_run is None
                or current_run["node_id"] != waypoint.node_id
                or current_run["end_step"] + 1 != waypoint.time_step
            ):
                current_run = {
                    "node_id": waypoint.node_id,
                    "start_step": waypoint.time_step,
                    "end_step": waypoint.time_step,
                }
                charge_runs.append(current_run)
            else:
                current_run["end_step"] = waypoint.time_step
        else:
            current_run = None

    return RouteDistanceProfile(
        total_distance=round(authoritative, 12),
        cumulative_by_time_step=cumulative,
        charge_runs=charge_runs,
    )


def _distance_at(profile: RouteDistanceProfile, time_step: int) -> float:
    if not profile.cumulative_by_time_step:
        return 0.0
    eligible = [
        step for step in profile.cumulative_by_time_step if step <= int(time_step)
    ]
    if not eligible:
        return 0.0
    return float(profile.cumulative_by_time_step[max(eligible)])


def _charge_duration_seconds(
    charged_percent: float,
    *,
    charge_rate_percent_per_minute: float,
    time_step_seconds: int,
) -> int:
    if charged_percent <= 0:
        return 0
    if charge_rate_percent_per_minute <= 0:
        return time_step_seconds
    raw_seconds = charged_percent / charge_rate_percent_per_minute * 60.0
    return max(
        time_step_seconds,
        math.ceil(raw_seconds / time_step_seconds) * time_step_seconds,
    )


def _robots(problem: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("robot_id")): row
        for row in problem.get("robots", [])
        if row.get("robot_id") is not None
    }


def _charge_tasks_by_robot(plan: CuOptPlan) -> dict[str, list[ScheduledTask]]:
    result: dict[str, list[ScheduledTask]] = {}
    for task in plan.scheduled_tasks:
        if task.action == "CHARGE":
            result.setdefault(task.robot_id, []).append(task)
    for tasks in result.values():
        tasks.sort(key=lambda row: (row.start_time_step, row.task_id))
    return result


def _match_charge_runs(
    tasks: list[ScheduledTask],
    profile: RouteDistanceProfile,
) -> list[dict[str, int]]:
    available = list(profile.charge_runs)
    matched: list[dict[str, int]] = []
    cursor = 0
    for task in tasks:
        selected: dict[str, int] | None = None
        for index in range(cursor, len(available)):
            candidate = available[index]
            if candidate["node_id"] == task.target_node:
                selected = candidate
                cursor = index + 1
                break
        if selected is None:
            selected = {
                "node_id": task.target_node,
                "start_step": task.end_time_step,
                "end_step": task.end_time_step,
            }
        matched.append(selected)
    return matched


def calculate_route_battery_metrics(
    plan: CuOptPlan,
    collision_plan: CollisionFreePlan,
    problem: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    robots = _robots(problem)
    task_groups = _charge_tasks_by_robot(plan)
    energy_per_distance = float(problem.get("energy_per_distance") or 0.05)
    minimum_battery = float(problem.get("min_robot_battery") or 0.0)
    metrics: dict[str, dict[str, Any]] = {}

    for route in collision_plan.routes:
        robot = robots.get(route.robot_id)
        if robot is None:
            continue
        initial = float(robot.get("battery") or 0.0)
        profile = route_distance_profile(route, problem)
        charge_tasks = task_groups.get(route.robot_id, [])
        charge_runs = _match_charge_runs(charge_tasks, profile)
        battery = initial
        previous_distance = 0.0
        charger_selections: list[dict[str, Any]] = []
        charge_task_ids: list[str] = []
        charger_node_ids: list[int] = []
        charged_total = 0.0
        duration_total = 0
        battery_at_chargers: list[float] = []

        for task, run in zip(charge_tasks, charge_runs):
            distance_at_charger = _distance_at(profile, run["start_step"])
            leg_distance = max(0.0, distance_at_charger - previous_distance)
            battery -= leg_distance * energy_per_distance
            battery_at_chargers.append(battery)
            charge = float(task.charged_percent or 0.0)
            charged_total += charge
            battery = min(100.0, battery + charge)
            charge_task_ids.append(task.task_id)
            charger_node_ids.append(task.target_node)
            duration_total += int(task.charge_duration_seconds or 0)
            charger_selections.append(
                {
                    "task_id": task.task_id,
                    "charger_node": task.target_node,
                    "charger_cost": task.charger_cost,
                    "selection_policy": task.charger_selection_policy,
                    "selection_reason": task.charger_selection_reason,
                    "battery_at_charger": round(battery_at_chargers[-1], 6),
                    "charged_percent": round(charge, 6),
                    "target_battery": round(battery, 6),
                    "charge_duration_seconds": int(task.charge_duration_seconds or 0),
                    "candidates": task.charger_candidates,
                }
            )
            previous_distance = distance_at_charger

        remaining_distance = max(0.0, profile.total_distance - previous_distance)
        battery -= remaining_distance * energy_per_distance
        consumption = profile.total_distance * energy_per_distance
        metrics[route.robot_id] = {
            "initial_battery": round(initial, 6),
            "estimated_consumption": round(consumption, 6),
            "route_based_consumption": round(consumption, 6),
            "energy_source": "ROUTING_FINAL_DISTANCE",
            "route_distance": round(profile.total_distance, 6),
            "charged_percent": round(charged_total, 6),
            "projected_without_charge": round(initial - consumption, 6),
            "minimum_battery": round(minimum_battery, 6),
            "final_battery": round(battery, 6),
            "charge_task_ids": charge_task_ids,
            "charger_node_ids": charger_node_ids,
            "charge_duration_seconds": duration_total,
            "charger_selections": charger_selections,
            "battery_at_chargers": [round(value, 6) for value in battery_at_chargers],
        }
    return metrics


def reconcile_plan_energy(
    plan: CuOptPlan,
    collision_plan: CollisionFreePlan,
    problem: dict[str, Any],
) -> tuple[CuOptPlan, dict[str, Any]]:
    """Reconcile charging against the final routed distance.

    The optimizer selects a charger before time-expanded routing is known.  A
    detour can change the battery at charger arrival, so preserving only the
    optimizer's ``charged_percent`` may leave an 80% operation-ready target at
    79.82%.  P16.3.4 treats ``charge_target_battery`` as the *post-charge* state
    and rounds charging duration up without reducing that target.

    Existing CHARGE tasks are retained and increased when necessary.  A route
    with no CHARGE task is marked unsafe only when the final routed mission
    cannot satisfy the minimum battery reserve.
    """

    robots = _robots(problem)
    tasks_by_robot = _charge_tasks_by_robot(plan)
    energy_per_distance = float(problem.get("energy_per_distance") or 0.05)
    minimum_battery = float(problem.get("min_robot_battery") or 0.0)
    explicit_operation_target = problem.get("charge_target_battery")
    operation_target = (
        float(explicit_operation_target)
        if explicit_operation_target is not None
        else None
    )
    rate = float(problem.get("charge_rate_percent_per_minute") or 0.0)
    step_seconds = max(
        1,
        int(
            problem.get("time_step_seconds")
            or collision_plan.time_step_seconds
        ),
    )
    updates: dict[str, ScheduledTask] = {}
    robot_results: dict[str, dict[str, Any]] = {}
    requires_reroute = False
    unsafe_robot_ids: list[str] = []

    for route in collision_plan.routes:
        robot = robots.get(route.robot_id)
        if robot is None:
            continue
        initial = float(robot.get("battery") or 0.0)
        profile = route_distance_profile(route, problem)
        route_consumption = profile.total_distance * energy_per_distance
        charge_tasks = tasks_by_robot.get(route.robot_id, [])

        if not charge_tasks:
            final_without_charge = initial - route_consumption
            status = (
                "PASS"
                if final_without_charge >= minimum_battery - 1e-6
                else "CHARGE_TASK_REQUIRED"
            )
            if status != "PASS":
                unsafe_robot_ids.append(route.robot_id)
            robot_results[route.robot_id] = {
                "route_distance": round(profile.total_distance, 6),
                "route_consumption": round(route_consumption, 6),
                "optimizer_charge_percent": 0.0,
                "required_charge_percent": round(
                    max(0.0, minimum_battery - final_without_charge), 6
                ),
                "adjusted_charge_percent": 0.0,
                "projected_final_battery": round(final_without_charge, 6),
                "minimum_battery": round(minimum_battery, 6),
                "charge_tasks": [],
                "status": status,
            }
            continue

        runs = _match_charge_runs(charge_tasks, profile)
        battery = initial
        previous_distance = 0.0
        task_results: list[dict[str, Any]] = []
        existing_total_charge = sum(
            float(row.charged_percent or 0.0) for row in charge_tasks
        )
        required_total_charge = 0.0
        arrival_violation = False

        for index, (task, run) in enumerate(zip(charge_tasks, runs)):
            distance_at_charger = _distance_at(profile, run["start_step"])
            travel_distance = max(0.0, distance_at_charger - previous_distance)
            battery -= travel_distance * energy_per_distance
            battery_at_charger = battery
            if battery_at_charger < minimum_battery - 1e-6:
                arrival_violation = True

            # The charge must cover the path until the next charge (or route
            # completion) and must also satisfy the configured operation-ready
            # target.  Never lower an existing task target during reconciliation.
            if index + 1 < len(runs):
                next_distance = _distance_at(profile, runs[index + 1]["start_step"])
            else:
                next_distance = profile.total_distance
            distance_until_next_charge = max(0.0, next_distance - distance_at_charger)
            reserve_target = minimum_battery + (
                distance_until_next_charge * energy_per_distance
            )
            existing_task_target = float(task.charge_target_battery or 0.0)
            desired_target = max(existing_task_target, reserve_target)
            if operation_target is not None:
                desired_target = max(desired_target, operation_target)
            desired_target = min(100.0, desired_target)

            required_for_target = max(0.0, desired_target - battery_at_charger)
            required_total_charge += required_for_target
            existing_charge = float(task.charged_percent or 0.0)
            charged = max(existing_charge, required_for_target)
            charged = max(0.0, min(100.0 - battery_at_charger, charged))
            target = min(100.0, battery_at_charger + charged)
            duration = _charge_duration_seconds(
                charged,
                charge_rate_percent_per_minute=rate,
                time_step_seconds=step_seconds,
            )
            if int(task.charge_duration_seconds or 0) != duration:
                requires_reroute = True

            candidates = [dict(row) for row in task.charger_candidates]
            for candidate in candidates:
                if candidate.get("selected"):
                    # Preserve the optimizer-time fields used to build the
                    # immutable selection_key. Post-route energy values belong
                    # in explicit reconciliation fields; overwriting ranking
                    # inputs makes verification solve a different problem.
                    candidate["reconciled_battery_at_charger"] = round(
                        battery_at_charger, 6
                    )
                    candidate["reconciled_charged_percent"] = round(charged, 6)
                    candidate["reconciled_target_battery"] = round(target, 6)
                    candidate["reconciled_charge_duration_seconds"] = duration

            updated = task.model_copy(
                update={
                    "charged_percent": charged,
                    "charge_target_battery": target,
                    "charge_duration_seconds": duration,
                    "charger_candidates": candidates,
                }
            )
            updates[task.task_id] = updated
            task_results.append(
                {
                    "task_id": task.task_id,
                    "charger_node": task.target_node,
                    "battery_at_charger": round(battery_at_charger, 6),
                    "charged_percent": round(charged, 6),
                    "target_battery": round(target, 6),
                    "required_target_battery": round(desired_target, 6),
                    "charge_duration_seconds": duration,
                }
            )
            battery = target
            previous_distance = distance_at_charger

        battery -= max(0.0, profile.total_distance - previous_distance) * energy_per_distance
        status = "PASS"
        if arrival_violation:
            status = "BELOW_MINIMUM_AT_CHARGER"
        elif battery < minimum_battery - 1e-6:
            status = "BELOW_MINIMUM"
        if status != "PASS":
            unsafe_robot_ids.append(route.robot_id)

        adjusted_total = sum(
            float(row["charged_percent"]) for row in task_results
        )
        robot_results[route.robot_id] = {
            "route_distance": round(profile.total_distance, 6),
            "route_consumption": round(route_consumption, 6),
            "optimizer_charge_percent": round(existing_total_charge, 6),
            "required_charge_percent": round(required_total_charge, 6),
            "charge_adjustment_percent": round(
                max(0.0, adjusted_total - existing_total_charge), 6
            ),
            "adjusted_charge_percent": round(adjusted_total, 6),
            "projected_final_battery": round(battery, 6),
            "minimum_battery": round(minimum_battery, 6),
            "operation_charge_target": (
                round(operation_target, 6)
                if operation_target is not None
                else None
            ),
            "charge_tasks": task_results,
            "status": status,
        }

    scheduled = [updates.get(task.task_id, task) for task in plan.scheduled_tasks]
    metadata = dict(plan.metadata)
    selections = [dict(row) for row in metadata.get("charger_selections", [])]
    result_by_task = {
        row["task_id"]: row
        for robot in robot_results.values()
        for row in robot.get("charge_tasks", [])
    }
    for selection in selections:
        task_result = result_by_task.get(str(selection.get("task_id") or ""))
        if not task_result:
            continue
        selection.update(task_result)
        selection["projected_final_battery"] = robot_results.get(
            str(selection.get("robot_id")), {}
        ).get("projected_final_battery")
        candidates = [dict(row) for row in selection.get("candidates", [])]
        for candidate in candidates:
            if candidate.get("selected"):
                candidate["battery_at_charger"] = task_result[
                    "battery_at_charger"
                ]
                candidate["charged_percent"] = task_result["charged_percent"]
                candidate["target_battery"] = task_result["target_battery"]
                candidate["charge_duration_seconds"] = task_result[
                    "charge_duration_seconds"
                ]
        selection["candidates"] = candidates
    metadata["charger_selections"] = selections
    metadata["route_energy_reconciliation"] = {
        "energy_source": "ROUTING_FINAL_DISTANCE",
        "requires_reroute": requires_reroute,
        "unsafe_robot_ids": sorted(set(unsafe_robot_ids)),
        "robots": robot_results,
    }
    return (
        plan.model_copy(update={"scheduled_tasks": scheduled, "metadata": metadata}),
        metadata["route_energy_reconciliation"],
    )

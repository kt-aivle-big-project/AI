from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from app.models import CuOptPlan
from app.services.opportunity_charging import augment_plan_with_opportunity_charging
from app.time_utils import planning_reference_time


CHARGE_VISIT_OPTIMIZER_VERSION = "p16.5.13.8"


def _dependency_rows(plan: CuOptPlan) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in plan.metadata.get("execution_task_dependencies", [])
        if isinstance(row, dict)
    ]


def charge_visit_robot_binding_errors(
    plan: CuOptPlan,
    contract: dict[str, Any],
) -> list[str]:
    """Return robot-chain violations introduced by the second pass.

    A charge visit and the business gap that produced it are a single
    robot-specific chain.  Reassigning any member makes the precomputed idle
    window and relocation evidence invalid, so this is a hard invariant.
    """

    actual = {str(task.task_id): str(task.robot_id) for task in plan.scheduled_tasks}
    expected: dict[str, str] = {
        str(task_id): str(robot_id)
        for task_id, robot_id in (
            contract.get("business_task_robot_bindings") or {}
        ).items()
    }
    for task_id, spec in (contract.get("charge_task_specs") or {}).items():
        robot_id = str(spec.get("robot_id") or "")
        if robot_id:
            expected[str(task_id)] = robot_id
    charge_specs = contract.get("charge_task_specs") or {}
    for relocation_id in contract.get("explicit_relocation_task_ids") or []:
        relocation_text = str(relocation_id)
        charge_id = relocation_text.split(":move_to_next:", 1)[0]
        spec = charge_specs.get(charge_id) or {}
        robot_id = str(spec.get("robot_id") or "")
        if robot_id:
            expected[relocation_text] = robot_id

    errors = []
    for task_id, expected_robot in sorted(expected.items()):
        actual_robot = actual.get(task_id)
        if actual_robot is None:
            errors.append(f"SECOND_PASS_TASK_MISSING:{task_id}")
        elif actual_robot != expected_robot:
            errors.append(
                "SECOND_PASS_ROBOT_BINDING_VIOLATION:"
                f"{task_id}:expected={expected_robot}:actual={actual_robot}"
            )
    return errors


def prepare_charge_visit_optimization_problem(
    problem: dict[str, Any],
    baseline_plan: CuOptPlan,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert selected opportunity charging visits into optimizer tasks.

    P16.5.10 uses a bounded two-pass contract:
    1. the first optimizer pass assigns/orders business tasks;
    2. deterministic battery/idle-energy planning selects mandatory and opportunity
       charger visits;
    3. selected visits are converted into mandatory, robot-bound CHARGE AtomicTasks;
    4. the second optimizer pass receives business and CHARGE tasks together.

    The local warehouse scheduler remains responsible for charger/service/idle
    capacity and exact post-route battery reconciliation. It must not reselect a
    different charger after cuOpt has accepted the explicit visit task.
    """

    if problem.get("cuopt_charge_visits_preoptimized"):
        return deepcopy(problem), dict(problem.get("charge_visit_optimization_contract") or {})

    enabled = bool(problem.get("opportunity_charging_enabled", False)) or (
        "OPPORTUNITY_CHARGING"
        in {str(value).upper() for value in problem.get("hard_constraints", [])}
    )
    base_contract: dict[str, Any] = {
        "version": CHARGE_VISIT_OPTIMIZER_VERSION,
        "enabled": enabled,
        "mode": "TWO_PASS_EXPLICIT_CHARGE_VISITS",
        "first_pass_role": "BUSINESS_TASK_ASSIGNMENT_AND_ORDER",
        "second_pass_role": "ROBOT_BOUND_BUSINESS_AND_CHARGE_VISIT_ORDER",
        "cuopt_responsibilities": [
            "FIRST_PASS_ROBOT_ASSIGNMENT",
            "SECOND_PASS_ROBOT_BOUND_VISIT_ORDER",
            "VISIT_ORDER",
            "TIME_WINDOWS",
            "TRAVEL_COST",
            "CHARGE_VISIT_SERVICE_TIME",
        ],
        "local_scheduler_responsibilities": [
            "PICK_DROP_SAME_ROBOT",
            "SERVICE_NODE_CAPACITY",
            "CHARGER_SLOT_CAPACITY",
            "IDLE_SPACE_CAPACITY",
            "EXACT_CHARGE_AMOUNT",
            "POST_ROUTE_BATTERY_RECONCILIATION",
        ],
        "explicit_charge_task_count": 0,
        "explicit_charge_task_ids": [],
        "explicit_relocation_task_count": 0,
        "explicit_relocation_task_ids": [],
        "explicit_optimizer_task_ids": [],
        "charge_task_specs": {},
        "business_task_robot_bindings": {},
        "managed_cuopt_pairing_mode": "FIRST_PASS_ONLY",
    }
    if not enabled:
        return deepcopy(problem), base_contract

    augmented, opportunity = augment_plan_with_opportunity_charging(problem, baseline_plan)
    charge_tasks = [
        task for task in augmented.scheduled_tasks if task.action == "CHARGE"
    ]
    if not charge_tasks:
        base_contract["opportunity_charging"] = opportunity
        return deepcopy(problem), base_contract

    reference = planning_reference_time(problem)
    step_seconds = max(1, int(problem.get("time_step_seconds") or 5))
    dependencies = _dependency_rows(augmented)
    charge_task_ids = {task.task_id for task in charge_tasks}
    predecessor_by_charge: dict[str, list[str]] = {}
    successor_by_charge: dict[str, list[str]] = {}
    for row in dependencies:
        predecessor = str(row.get("predecessor_task_id") or "")
        successor = str(row.get("successor_task_id") or "")
        if successor in charge_task_ids:
            predecessor_by_charge.setdefault(successor, []).append(predecessor)
        if predecessor in charge_task_ids:
            successor_by_charge.setdefault(predecessor, []).append(successor)

    enriched = deepcopy(problem)
    task_rows = [dict(row) for row in enriched.get("tasks", [])]
    task_by_id = {
        str(row.get("task_id")): row for row in task_rows if row.get("task_id")
    }
    # The first pass owns robot assignment.  Once a charger visit has been
    # selected for a robot, the second pass must not move an earlier/later
    # business task onto that robot because the stored gap successor and idle
    # reservation would become stale.  Bind every business task to the
    # first-pass assignment while leaving its timing changeable.
    baseline_robot_by_task = {
        str(task.task_id): str(task.robot_id)
        for task in baseline_plan.scheduled_tasks
        if task.action in {"PICK", "DROP", "MOVE"}
    }
    for task_id, robot_id in baseline_robot_by_task.items():
        row = task_by_id.get(task_id)
        if row is None:
            continue
        row["assigned_robot_id"] = robot_id
        # Robot identity is fixed, but the second pass/local normalizer may
        # still adjust exact start/end times and visit order.
        row["frozen"] = False
    explicit_specs: dict[str, dict[str, Any]] = {}
    explicit_rows: list[dict[str, Any]] = []
    relocation_rows: list[dict[str, Any]] = []

    for charge in sorted(
        charge_tasks,
        key=lambda row: (row.start_time_step, row.robot_id, row.task_id),
    ):
        start_at = reference + timedelta(seconds=charge.start_time_step * step_seconds)
        end_at = reference + timedelta(seconds=charge.end_time_step * step_seconds)
        predecessors = sorted(
            set(value for value in predecessor_by_charge.get(charge.task_id, []) if value)
        )
        successor_ids = sorted(
            set(value for value in successor_by_charge.get(charge.task_id, []) if value)
        )
        # The first-pass charge end is an optimizer target, not a user
        # deadline.  Only a successor carrying an explicit HARD_WINDOW may
        # turn the charge completion into a hard latest-finish constraint.
        # Without this distinction, a few routing/resource-delay steps make
        # an otherwise valid CHARGE -> MOVE_TO_NEXT -> PICK -> DROP chain fail
        # with RESOURCE_DELAY_HARD_WINDOW_VIOLATION.
        hard_successor_starts = []
        for successor_id in successor_ids:
            successor_row = task_by_id.get(successor_id) or {}
            if (
                str(successor_row.get("time_constraint_type") or "").upper()
                != "HARD_WINDOW"
            ):
                continue
            raw_start = successor_row.get("earliest_start")
            if not raw_start:
                continue
            parsed = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            # A LOW_BATTERY replan can begin after the successor window has
            # already opened.  The historical earliest_start is then a lower
            # bound that has passed, not a deadline for the newly inserted
            # CHARGE visit.  Promoting it to latest_finish creates an
            # impossible window (for example CHARGE starts 00:05 but must end
            # by 00:00) and can make the managed/local second pass discard the
            # entire affected chain.  Only a genuinely future successor start
            # may constrain the charge completion.
            if parsed > end_at:
                hard_successor_starts.append(parsed)
        hard_latest_finish = (
            min(hard_successor_starts) if hard_successor_starts else None
        )
        row = {
            "task_id": charge.task_id,
            "work_id": charge.work_id,
            "action": "CHARGE",
            "item_id": None,
            "quantity": 0,
            "source_candidates": [int(charge.target_node)],
            "target_candidates": [int(charge.target_node)],
            "priority": int(charge.priority),
            "deadline": (
                hard_latest_finish.isoformat() if hard_latest_finish else None
            ),
            "predecessors": predecessors,
            "dependencies": [],
            "earliest_start": start_at.isoformat(),
            "latest_finish": (
                hard_latest_finish.isoformat() if hard_latest_finish else None
            ),
            "time_constraint_type": (
                "HARD_WINDOW" if hard_latest_finish else "ASAP"
            ),
            "same_robot_group": None,
            "frozen": False,
            "assigned_robot_id": str(charge.robot_id),
            "inventory_allocations": [],
        }
        explicit_rows.append(row)
        explicit_specs[charge.task_id] = {
            "task_id": charge.task_id,
            "robot_id": str(charge.robot_id),
            "charger_node_id": int(charge.target_node),
            "source_node_before_charge": int(charge.source_node),
            "planned_start_time_step": int(charge.start_time_step),
            "planned_end_time_step": int(charge.end_time_step),
            "charge_duration_seconds": int(charge.charge_duration_seconds or 0),
            "charged_percent": float(charge.charged_percent or 0.0),
            "target_battery": charge.charge_target_battery,
            "charger_cost": charge.charger_cost,
            "selection_policy": charge.charger_selection_policy,
            "selection_reason": charge.charger_selection_reason,
            "candidates": list(charge.charger_candidates),
            "predecessor_task_ids": predecessors,
            "successor_task_ids": successor_ids,
            # Auditable first-pass target only.  The local route/resource
            # scheduler may move this time unless a real user hard window was
            # inherited from a successor.
            "optimization_window_end_at": end_at.isoformat(),
            "hard_latest_finish_at": (
                hard_latest_finish.isoformat() if hard_latest_finish else None
            ),
        }

        for successor_id in explicit_specs[charge.task_id]["successor_task_ids"]:
            successor = task_by_id.get(successor_id)
            if successor is None:
                continue
            successor_predecessors = list(successor.get("predecessors") or [])
            if charge.task_id not in successor_predecessors:
                successor_predecessors.append(charge.task_id)

            next_sources = sorted(
                {int(value) for value in successor.get("source_candidates", [])}
            )
            if next_sources and int(charge.target_node) not in next_sources:
                relocation_id = f"{charge.task_id}:move_to_next:{successor_id}"
                successor_start = successor.get("earliest_start")
                relocation_latest_finish: str | None = None
                if successor_start:
                    parsed_successor_start = datetime.fromisoformat(
                        str(successor_start).replace("Z", "+00:00")
                    )
                    # Inventory lot availability may be older than the plan
                    # reference time.  It is a lower availability bound, not a
                    # valid upper deadline for the post-charge relocation.
                    # Only a genuinely future successor start may constrain
                    # MOVE_TO_NEXT as a hard latest-finish window.
                    if parsed_successor_start > end_at:
                        relocation_latest_finish = parsed_successor_start.isoformat()
                relocation_rows.append(
                    {
                        "task_id": relocation_id,
                        "work_id": charge.work_id,
                        "action": "MOVE",
                        "item_id": None,
                        "quantity": 0,
                        "source_candidates": [int(charge.target_node)],
                        "target_candidates": next_sources,
                        "priority": int(charge.priority),
                        "deadline": relocation_latest_finish,
                        "predecessors": [charge.task_id],
                        "dependencies": [],
                        "earliest_start": end_at.isoformat(),
                        "latest_finish": relocation_latest_finish,
                        "time_constraint_type": (
                            "HARD_WINDOW" if relocation_latest_finish else "ASAP"
                        ),
                        "same_robot_group": None,
                        "frozen": False,
                        "assigned_robot_id": str(charge.robot_id),
                        "inventory_allocations": [],
                    }
                )
                if relocation_id not in successor_predecessors:
                    successor_predecessors.append(relocation_id)
            successor["predecessors"] = successor_predecessors

    task_rows.extend(explicit_rows)
    task_rows.extend(relocation_rows)
    enriched["tasks"] = task_rows
    enriched["explicit_charge_task_specs"] = explicit_specs
    enriched["cuopt_charge_visits_preoptimized"] = True
    # NVIDIA managed cuOpt requires every task location index to participate
    # when pickup_and_delivery_pairs is supplied.  The second pass contains
    # standalone CHARGE/MOVE visits, so we bind all tasks by vehicle and omit
    # PDP pairs only for this pass.  PICK->DROP dependencies are revalidated by
    # the local warehouse normalizer.
    enriched["cuopt_disable_pickup_delivery_pairs"] = True
    enriched["opportunity_charging_enabled"] = False
    enriched["charge_visit_optimization_contract"] = {
        **base_contract,
        "explicit_charge_task_count": len(explicit_rows),
        "explicit_charge_task_ids": [row["task_id"] for row in explicit_rows],
        "explicit_relocation_task_count": len(relocation_rows),
        "explicit_relocation_task_ids": [
            row["task_id"] for row in relocation_rows
        ],
        "explicit_optimizer_task_ids": [
            row["task_id"] for row in [*explicit_rows, *relocation_rows]
        ],
        "charge_task_specs": explicit_specs,
        "business_task_robot_bindings": {
            task_id: robot_id
            for task_id, robot_id in sorted(baseline_robot_by_task.items())
            if task_id in task_by_id
        },
        "managed_cuopt_pairing_mode": "ROBOT_BOUND_TASKS_WITHOUT_PDP",
        "opportunity_charging": opportunity,
    }
    return enriched, dict(enriched["charge_visit_optimization_contract"])

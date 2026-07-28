"""Compact conversation state and deterministic inheritance policies."""

from __future__ import annotations

import json
from typing import Any

from app.models import (
    CommandInterpretation,
    FixedRobotAssignment,
    OptimizationWeights,
    SameRobotGroup,
    TaskDependency,
    TaskScheduleConstraint,
    InventoryOperationRequest,
    PlanningReference,
)
from app.services.command_language import normalize_text


INHERITABLE_FIELDS = (
    "target_task_ids",
    "target_robot_ids",
    "robot_limit",
    "excluded_robot_ids",
    "included_robot_ids",
    "excluded_node_ids",
    "excluded_edge_ids",
    "fixed_robot_assignments",
    "optimization_priority",
    "optimization_weights",
    "hypothetical_events",
    "scheduled_task_constraints",
    "task_dependencies",
    "same_robot_groups",
    "daily_schedule_requested",
    "inventory_operations",
    "load_open_inventory_orders",
    "planning_reference",
)

CONTEXT_INHERITANCE_PHRASES = (
    "같은 조건에서",
    "동일한 조건으로",
    "아까 조건 그대로",
    "이전 계획에서",
    "같은 작업으로",
    "방금 작업을",
    "이번에는 기준만 바꿔서",
    "그 일정 그대로",
    "일정 그대로 두고",
    "그 일정은 그대로 유지하고",
    "기존 일정은 그대로 유지하고",
)
TARGET_TASK_CLEAR_PHRASES = (
    "모든 작업으로 바꿔",
    "작업 제한을 없애",
    "전체 작업을 대상으로",
    "전체 작업으로 바꿔",
)
BASE_PLAN_DISCARD_PHRASES = (
    "기존 일정은 취소하고",
    "기존 계획은 취소하고",
    "모든 작업을 빼고",
    "이전 계획을 폐기",
    "기존 계획을 폐기",
    "으로 교체",
)


def requests_context_inheritance(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in CONTEXT_INHERITANCE_PHRASES)


def explicitly_clears_target_tasks(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in TARGET_TASK_CLEAR_PHRASES)


def explicitly_discards_base_plan(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in BASE_PLAN_DISCARD_PHRASES)


class ConversationAccessError(ValueError):
    """Raised when a command crosses conversation or warehouse boundaries."""


def constraints_from_interpretation(
    interpretation: CommandInterpretation,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if interpretation.target_task_ids:
        values["target_task_ids"] = interpretation.target_task_ids
    if interpretation.target_robot_ids:
        values["target_robot_ids"] = interpretation.target_robot_ids
    if interpretation.robot_limit is not None:
        values["robot_limit"] = interpretation.robot_limit
    if interpretation.excluded_robot_ids:
        values["excluded_robot_ids"] = interpretation.excluded_robot_ids
    if interpretation.included_robot_ids:
        values["included_robot_ids"] = interpretation.included_robot_ids
    if interpretation.excluded_node_ids:
        values["excluded_node_ids"] = interpretation.excluded_node_ids
    if interpretation.excluded_edge_ids:
        values["excluded_edge_ids"] = interpretation.excluded_edge_ids
    if interpretation.fixed_robot_assignments:
        values["fixed_robot_assignments"] = [
            row.model_dump(mode="json")
            for row in interpretation.fixed_robot_assignments
        ]
    if interpretation.optimization_priority:
        values["optimization_priority"] = interpretation.optimization_priority
        values["optimization_weights"] = interpretation.optimization_weights.model_dump()
    if interpretation.hypothetical_events:
        values["hypothetical_events"] = [
            row.model_dump(mode="json") for row in interpretation.hypothetical_events
        ]
    if interpretation.scheduled_task_constraints:
        values["scheduled_task_constraints"] = [
            row.model_dump(mode="json")
            for row in interpretation.scheduled_task_constraints
        ]
    if interpretation.task_dependencies:
        values["task_dependencies"] = [
            row.model_dump(mode="json") for row in interpretation.task_dependencies
        ]
    if interpretation.same_robot_groups:
        values["same_robot_groups"] = [
            row.model_dump(mode="json") for row in interpretation.same_robot_groups
        ]
    if interpretation.daily_schedule_requested:
        values["daily_schedule_requested"] = True
    if interpretation.inventory_operations:
        values["inventory_operations"] = [
            row.model_dump(mode="json") for row in interpretation.inventory_operations
        ]
    if interpretation.load_open_inventory_orders:
        values["load_open_inventory_orders"] = True
    if interpretation.planning_reference is not None:
        values["planning_reference"] = interpretation.planning_reference.model_dump(
            mode="json"
        )
    return values


def apply_conversation_inheritance(
    interpretation: CommandInterpretation,
    inherited: dict[str, Any],
    *,
    active_plan_version: str | None,
    active_simulation_id: str | None,
) -> tuple[CommandInterpretation, dict[str, Any], dict[str, Any]]:
    result = interpretation.model_copy(deep=True)
    applied: dict[str, Any] = {}
    overridden: dict[str, Any] = {}
    discard_base_plan = explicitly_discards_base_plan(result.objective)
    inherit_context = (
        requests_context_inheritance(result.objective)
        or result.intent == "INSERT_TASK"
    ) and not discard_base_plan
    clear_target_tasks = explicitly_clears_target_tasks(result.objective)

    if result.planning_reference is None and inherit_context and inherited.get(
        "planning_reference"
    ):
        result.planning_reference = PlanningReference.model_validate(
            inherited["planning_reference"]
        )
        applied["planning_reference"] = inherited["planning_reference"]
    elif result.planning_reference is not None:
        overridden["planning_reference"] = result.planning_reference.model_dump(
            mode="json"
        )

    inherited_task_ids = list(inherited.get("target_task_ids") or [])
    if result.target_task_ids:
        if result.intent == "INSERT_TASK" and inherit_context and inherited_task_ids:
            inserted_task_ids = list(result.target_task_ids)
            result.target_task_ids = list(
                dict.fromkeys([*inherited_task_ids, *inserted_task_ids])
            )
            applied["target_task_ids"] = inherited_task_ids
            overridden["target_task_ids"] = inserted_task_ids
        elif inherited_task_ids and result.target_task_ids != inherited_task_ids:
            overridden["target_task_ids"] = result.target_task_ids
    elif clear_target_tasks:
        if inherited_task_ids:
            overridden["target_task_ids"] = []
    elif inherit_context:
        if inherited_task_ids:
            result.target_task_ids = inherited_task_ids
            applied["target_task_ids"] = inherited_task_ids
        else:
            result.missing_information = list(
                dict.fromkeys([*result.missing_information, "target_task_scope"])
            )
            result.ambiguous_terms = list(
                dict.fromkeys(
                    [
                        *result.ambiguous_terms,
                        "inherited_target_task_ids_unavailable",
                    ]
                )
            )

    inherited_robot_ids = list(inherited.get("target_robot_ids") or [])
    if result.target_robot_ids:
        if inherited_robot_ids and result.target_robot_ids != inherited_robot_ids:
            overridden["target_robot_ids"] = result.target_robot_ids
    elif inherit_context and inherited_robot_ids:
        result.target_robot_ids = inherited_robot_ids
        applied["target_robot_ids"] = inherited_robot_ids

    if result.robot_limit is None and inherited.get("robot_limit") is not None:
        result.robot_limit = int(inherited["robot_limit"])
        applied["robot_limit"] = result.robot_limit
    elif result.robot_limit is not None and inherited.get("robot_limit") != result.robot_limit:
        overridden["robot_limit"] = result.robot_limit

    inherited_excluded = set(inherited.get("excluded_robot_ids") or [])
    inherited_included = set(inherited.get("included_robot_ids") or [])
    current_excluded = set(result.excluded_robot_ids)
    current_included = set(result.included_robot_ids)
    merged_included = (inherited_included | current_included) - current_excluded
    merged_excluded = (inherited_excluded | current_excluded) - current_included
    if inherited_excluded:
        applied["excluded_robot_ids"] = sorted(inherited_excluded)
    if inherited_included:
        applied["included_robot_ids"] = sorted(inherited_included)
    if current_excluded or current_included:
        overridden["excluded_robot_ids"] = sorted(merged_excluded)
    result.excluded_robot_ids = sorted(merged_excluded)
    result.included_robot_ids = sorted(merged_included)

    for field in ("excluded_node_ids", "excluded_edge_ids"):
        inherited_values = set(inherited.get(field) or [])
        current_values = set(getattr(result, field))
        merged = sorted(inherited_values | current_values)
        if inherited_values:
            applied[field] = sorted(inherited_values)
        if current_values:
            overridden[field] = merged
        setattr(result, field, merged)

    inherited_assignments = {
        str(row["task_id"]): str(row["robot_id"])
        for row in inherited.get("fixed_robot_assignments") or []
        if row.get("task_id") is not None and row.get("robot_id") is not None
    }
    current_assignments = {
        row.task_id: row.robot_id for row in result.fixed_robot_assignments
    }
    if current_assignments:
        assignments = (
            {**inherited_assignments, **current_assignments}
            if inherit_context
            else current_assignments
        )
        if inherited_assignments and assignments != inherited_assignments:
            overridden["fixed_robot_assignments"] = [
                {"task_id": task_id, "robot_id": robot_id}
                for task_id, robot_id in sorted(assignments.items())
            ]
        result.fixed_robot_assignments = [
            FixedRobotAssignment(task_id=task_id, robot_id=robot_id)
            for task_id, robot_id in sorted(assignments.items())
        ]
    elif inherit_context and inherited_assignments:
        result.fixed_robot_assignments = [
            FixedRobotAssignment(task_id=task_id, robot_id=robot_id)
            for task_id, robot_id in sorted(inherited_assignments.items())
        ]
        applied["fixed_robot_assignments"] = [
            row.model_dump(mode="json") for row in result.fixed_robot_assignments
        ]

    if not result.optimization_priority and inherited.get("optimization_priority"):
        result.optimization_priority = str(inherited["optimization_priority"])
        result.optimization_weights = OptimizationWeights.model_validate(
            inherited.get("optimization_weights") or {}
        )
        applied["optimization_priority"] = result.optimization_priority
        applied["optimization_weights"] = result.optimization_weights.model_dump()
    elif result.optimization_priority:
        overridden["optimization_priority"] = result.optimization_priority
        overridden["optimization_weights"] = result.optimization_weights.model_dump()

    # Hypothetical settings may continue only in non-EXECUTE requests. Actual
    # execution/event approval is never inherited.
    if (
        not result.hypothetical_events
        and result.execution_mode != "EXECUTE"
        and inherited.get("hypothetical_events")
    ):
        result.hypothetical_events = inherited["hypothetical_events"]
        applied["hypothetical_events"] = inherited["hypothetical_events"]

    if inherit_context:
        inherited_inventory = [
            InventoryOperationRequest.model_validate(row)
            for row in inherited.get("inventory_operations") or []
        ]
        if result.inventory_operations:
            overridden["inventory_operations"] = [
                row.model_dump(mode="json") for row in result.inventory_operations
            ]
        elif inherited_inventory:
            result.inventory_operations = inherited_inventory
            normalized_objective = normalize_text(result.objective)
            if any(
                phrase in normalized_objective
                for phrase in (
                    "가능한 수량만 먼저",
                    "재고가 있는 만큼만",
                    "부분 출고를 승인",
                    "현재 가능한 수량부터",
                    "나머지는 입고 후",
                )
            ):
                result.inventory_operations = [
                    row.model_copy(update={"allow_partial_fulfillment": True})
                    for row in result.inventory_operations
                ]
            applied["inventory_operations"] = [
                row.model_dump(mode="json") for row in result.inventory_operations
            ]
        if inherited.get("load_open_inventory_orders"):
            result.load_open_inventory_orders = True
            applied["load_open_inventory_orders"] = True

        inherited_constraints = {
            str(row["work_id"]): TaskScheduleConstraint.model_validate(row)
            for row in inherited.get("scheduled_task_constraints") or []
        }
        current_constraints = {
            row.work_id: row for row in result.scheduled_task_constraints
        }
        if inherited_constraints:
            merged_constraints = {**inherited_constraints, **current_constraints}
            result.scheduled_task_constraints = [
                merged_constraints[key] for key in sorted(merged_constraints)
            ]
            applied["scheduled_task_constraints"] = [
                row.model_dump(mode="json")
                for key, row in sorted(inherited_constraints.items())
                if key not in current_constraints
            ]
            if current_constraints:
                overridden["scheduled_task_constraints"] = [
                    row.model_dump(mode="json")
                    for row in current_constraints.values()
                ]

        inherited_dependencies = [
            TaskDependency.model_validate(row)
            for row in inherited.get("task_dependencies") or []
        ]
        dependency_keys = {
            (row.predecessor_work_id, row.successor_work_id): row
            for row in inherited_dependencies
        }
        current_dependency_keys = {
            (row.predecessor_work_id, row.successor_work_id): row
            for row in result.task_dependencies
        }
        if inherited_dependencies:
            dependency_keys.update(current_dependency_keys)
            result.task_dependencies = [
                dependency_keys[key] for key in sorted(dependency_keys)
            ]
            applied["task_dependencies"] = [
                row.model_dump(mode="json") for row in inherited_dependencies
            ]

        inherited_groups = {
            str(row["group_id"]): SameRobotGroup.model_validate(row)
            for row in inherited.get("same_robot_groups") or []
        }
        current_groups = {row.group_id: row for row in result.same_robot_groups}
        if inherited_groups:
            inherited_groups.update(current_groups)
            result.same_robot_groups = [
                inherited_groups[key] for key in sorted(inherited_groups)
            ]
            applied["same_robot_groups"] = [
                row.model_dump(mode="json")
                for row in result.same_robot_groups
            ]
        if inherited.get("daily_schedule_requested"):
            result.daily_schedule_requested = True
            applied["daily_schedule_requested"] = True

    if "target_reference" in result.missing_information and (
        active_plan_version or active_simulation_id
    ):
        result.missing_information = [
            value for value in result.missing_information if value != "target_reference"
        ]
        result.ambiguous_terms = [
            value
            for value in result.ambiguous_terms
            if value
            not in {
                "그 로봇",
                "아까 작업",
                "그 작업",
                "저 계획",
                "이 계획",
                "문제 있는 작업",
            }
        ]
        applied["target_reference"] = (
            active_simulation_id or active_plan_version
        )
    return CommandInterpretation.model_validate(result.model_dump()), applied, overridden


def compact_conversation_summary(state: dict[str, Any]) -> dict[str, Any]:
    interpretation = state.get("interpretation", {})
    verification = state.get("verification_decision", {})
    data = state.get("report_data", {}) or state.get("response", {}).get("data", {})
    summary = {
        "warehouse_id": state.get("command", {}).get("warehouse_id"),
        "previous_command": {
            "intent": interpretation.get("intent"),
            "execution_mode": interpretation.get("execution_mode"),
        },
        "active_constraints": state.get("resolved_constraints", {}),
        "active_plan_version": state.get("plan_version")
        or state.get("active_plan_version"),
        "active_simulation_id": state.get("simulation_id")
        or state.get("active_simulation_id"),
        "last_verification": verification.get("decision"),
        "last_result_metrics": {
            key: data.get(key)
            for key in ("total_distance", "makespan_seconds", "tardiness", "conflict_count")
            if data.get(key) is not None
        },
    }
    # Enforce a compact context. The current schema is normally below 2 KB;
    # this guard prevents accidental large state additions from reaching an LLM.
    if len(json.dumps(summary, ensure_ascii=False, default=str)) > 8192:
        summary["last_result_metrics"] = {}
    return summary

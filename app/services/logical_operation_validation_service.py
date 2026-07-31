"""Final exact-once operation coverage validation for executable plans."""
from __future__ import annotations

from collections import Counter

from app.domain.schemas import (
    CuOptDynamicInputDraft,
    LogicalOperationCoverageValidationResult,
    NormalizedWarehouseRequest,
    SimulationPlan,
)


class LogicalOperationCoverageValidator:
    """Reject a final plan that silently loses or duplicates business work."""

    _ACTIONABLE = {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}

    def validate(
        self,
        *,
        request: NormalizedWarehouseRequest,
        draft: CuOptDynamicInputDraft,
        plan: SimulationPlan,
    ) -> LogicalOperationCoverageValidationResult:
        requested_type_by_id = {
            value.operation_id: value.operation_type
            for value in request.operations
            if value.operation_type in self._ACTIONABLE
        }
        requested_ids = set(requested_type_by_id)
        deferred_ids = set(draft.deferred_order_ids)
        executable_ids = requested_ids - deferred_ids

        logical_ids = [value.operation_id for value in plan.logical_operations]
        logical_counts = Counter(logical_ids)
        planned_ids = set(logical_ids)
        missing = requested_ids - planned_ids
        unexpected = planned_ids - requested_ids
        duplicate = {value for value, count in logical_counts.items() if count > 1}

        operation_by_id = {
            value.operation_id: value for value in plan.logical_operations
        }
        operations_without_tasks: list[str] = []
        operations_without_robots: list[str] = []
        errors: list[str] = []
        warnings: list[str] = []

        if missing:
            errors.append("PLAN_OPERATION_COVERAGE_MISSING:" + ",".join(sorted(missing)))
        if unexpected:
            errors.append("PLAN_OPERATION_COVERAGE_UNKNOWN:" + ",".join(sorted(unexpected)))
        if duplicate:
            errors.append("PLAN_OPERATION_COVERAGE_DUPLICATE:" + ",".join(sorted(duplicate)))

        for operation_id in sorted(executable_ids & planned_ids):
            logical = operation_by_id[operation_id]
            expected_type = requested_type_by_id[operation_id]
            if logical.operation_type != expected_type:
                errors.append(
                    f"PLAN_OPERATION_TYPE_MISMATCH:{operation_id}:"
                    f"expected={expected_type};actual={logical.operation_type}"
                )
            if not logical.task_ids:
                operations_without_tasks.append(operation_id)
                errors.append(f"PLAN_OPERATION_HAS_NO_TASKS:{operation_id}")
            if not logical.assigned_robot_id:
                operations_without_robots.append(operation_id)
                errors.append(f"PLAN_OPERATION_HAS_NO_ROBOT:{operation_id}")

        for operation_id in sorted(deferred_ids & planned_ids):
            logical = operation_by_id[operation_id]
            if logical.task_ids or logical.assigned_robot_id:
                errors.append(f"DEFERRED_OPERATION_IS_EXECUTABLE:{operation_id}")

        service_task_ids = {
            str(step.task_id)
            for robot in plan.robots
            for step in robot.steps
            if step.step_type == "SERVICE" and step.task_id
        }
        task_ids_missing_from_plan: list[str] = []
        for operation_id in sorted(executable_ids & planned_ids):
            logical = operation_by_id[operation_id]
            for base_task_id in logical.task_ids:
                represented = any(
                    service_task_id == base_task_id
                    or service_task_id.startswith(base_task_id + "_")
                    or base_task_id.startswith(service_task_id + "_")
                    for service_task_id in service_task_ids
                )
                if not represented:
                    task_ids_missing_from_plan.append(base_task_id)
                    errors.append(
                        f"LOGICAL_TASK_MISSING_FROM_EXECUTION:{operation_id}:{base_task_id}"
                    )

        return LogicalOperationCoverageValidationResult(
            valid=not errors,
            requested_operation_ids=sorted(requested_ids),
            executable_operation_ids=sorted(executable_ids),
            deferred_operation_ids=sorted(deferred_ids),
            planned_operation_ids=sorted(planned_ids),
            missing_operation_ids=sorted(missing),
            duplicate_operation_ids=sorted(duplicate),
            unexpected_operation_ids=sorted(unexpected),
            operations_without_tasks=operations_without_tasks,
            operations_without_robots=operations_without_robots,
            task_ids_missing_from_plan=sorted(set(task_ids_missing_from_plan)),
            errors=list(dict.fromkeys(errors)),
            warnings=warnings,
        )

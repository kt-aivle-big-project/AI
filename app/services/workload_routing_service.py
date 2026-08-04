"""Deterministic workload bands used before Rule/Agent branch locking."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.domain.schemas import RoutingWorkloadContext


WorkloadRouteBand = Literal["UNSPECIFIED", "LOW", "GRAY", "HIGH"]


@dataclass(frozen=True)
class WorkloadRouteAssessment:
    band: WorkloadRouteBand
    effective_operation_count: int
    eligible_robot_count: int
    operations_per_robot: float | None
    reason: str


def assess_workload_route(
    context: RoutingWorkloadContext | None,
    settings: Settings,
) -> WorkloadRouteAssessment:
    """Classify obvious low/high load and leave the middle band to the router LLM."""

    if context is None:
        return WorkloadRouteAssessment(
            band="UNSPECIFIED",
            effective_operation_count=0,
            eligible_robot_count=0,
            operations_per_robot=None,
            reason="No authoritative workload snapshot was supplied; legacy routing applies.",
        )

    operation_count = context.effective_operation_count
    eligible = context.eligible_robot_count
    ratio = operation_count / eligible if eligible > 0 else None

    if operation_count > 0 and eligible == 0:
        band: WorkloadRouteBand = "HIGH"
        reason = "No immediately eligible robot is available for the pending workload."
    elif operation_count >= settings.workload_agent_min_operation_count:
        band = "HIGH"
        reason = (
            f"Effective operations {operation_count} reached the Agent threshold "
            f"{settings.workload_agent_min_operation_count}."
        )
    elif ratio is not None and ratio >= settings.workload_agent_min_operations_per_robot:
        band = "HIGH"
        reason = (
            f"Operations per eligible robot {ratio:.2f} reached the Agent threshold "
            f"{settings.workload_agent_min_operations_per_robot:.2f}."
        )
    elif (
        operation_count <= settings.workload_rule_max_operation_count
        and ratio is not None
        and ratio <= settings.workload_rule_max_operations_per_robot
    ):
        band = "LOW"
        reason = (
            f"Effective operations {operation_count} and load ratio {ratio:.2f} are within "
            "the deterministic Rule fast-path limits."
        )
    else:
        band = "GRAY"
        ratio_text = "unknown" if ratio is None else f"{ratio:.2f}"
        reason = (
            f"Effective operations {operation_count} and load ratio {ratio_text} fall in the "
            "gray band; the router LLM must consider the supplied workload and policies."
        )

    return WorkloadRouteAssessment(
        band=band,
        effective_operation_count=operation_count,
        eligible_robot_count=eligible,
        operations_per_robot=ratio,
        reason=reason,
    )

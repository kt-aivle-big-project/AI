"""Deterministic batch stock allocation before vehicle routing.

The allocator chooses one pickup rack per order and enforces stock quantities
across the full batch.  It never chooses a robot.  OR-Tools/cuOpt therefore
retain the full vehicle-assignment and multi-task sequencing decision.
"""
from __future__ import annotations

import re
from math import inf

from app.domain.schemas import (
    MissionSpec,
    PolicyCheck,
    PolicyValidationResult,
    PolicyViolation,
    StockAllocation,
    ValidatedTask,
)
from app.services.graph_service import DirectedGraphService
from app.services.rack_access_service import choose_best_access_node

_PRIORITY_RANK = {"high": 2, "medium": 1, "low": 0}


def _task_id(order_id: str, index: int) -> str:
    """Create a stable solver task id from an order identifier."""

    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", order_id).strip("-") or f"ORDER-{index:03d}"
    return f"TASK-{cleaned}"


class GlobalInventoryAllocator:
    """Allocate stock across a batch without fixing vehicle assignment."""

    def allocate(
        self,
        *,
        mission: MissionSpec,
        policy: PolicyValidationResult,
        graph_arcs: list[dict],
    ) -> PolicyValidationResult:
        """Return policy materialization with one stock/rack per outbound order.

        Candidate racks remain policy-derived.  The allocator tracks remaining
        quantities across all orders, chooses the least lower-bound travel-cost
        rack that at least one eligible robot can reach, and leaves every new
        task's ``fixed_robot_id`` empty.
        """

        if policy.status != "pass":
            return policy
        graph = DirectedGraphService(graph_arcs)
        remaining = {
            candidate.stock_id: candidate.available_qty
            for candidate in policy.fulfillment_candidates
        }
        candidates_by_order: dict[str, list] = {}
        for candidate in policy.fulfillment_candidates:
            candidates_by_order.setdefault(candidate.order_id, []).append(candidate)

        tasks = list(policy.validated_tasks)  # already-started recovery work
        allocations = list(policy.stock_allocations)
        checks = list(policy.checks)
        violations = list(policy.violations)

        requests = [request for request in mission.task_requests if request.request_type == "outbound_pick"]
        requests.sort(key=lambda value: (-_PRIORITY_RANK[value.priority], value.order_id or ""))
        for index, request in enumerate(requests, start=1):
            order_id = request.order_id or ""
            scored: list[tuple[float, object, object]] = []
            for candidate in candidates_by_order.get(order_id, []):
                if remaining.get(candidate.stock_id, 0) < request.requested_qty:
                    continue
                choice = choose_best_access_node(
                    graph,
                    rack_id=candidate.rack_id,
                    access_node_ids=candidate.access_node_ids,
                    robot_start_nodes={
                        robot.robot_id: robot.start_node
                        for robot in policy.candidate_robots
                        if robot.capacity_units >= request.requested_qty
                    },
                    delivery_node=candidate.delivery_node,
                )
                if choice is None:
                    continue
                scored.append((choice.total_cost, candidate, choice))
            if not scored:
                violations.append(
                    PolicyViolation(
                        code="batch_stock_allocation_failed",
                        message=f"No stock allocation remains for order {order_id}.",
                        repairable=order_id in set(mission.optional_order_ids),
                    )
                )
                checks.append(
                    PolicyCheck(
                        check_type="GLOBAL_STOCK_ALLOCATION",
                        status="fail",
                        target=order_id,
                        detail={"required_qty": request.requested_qty},
                    )
                )
                continue
            score, selected, access_choice = min(
                scored,
                key=lambda value: (
                    value[0],
                    value[1].rack_id,
                    value[1].rack_level,
                    value[1].stock_id,
                    value[2].access_node_id,
                ),
            )
            remaining[selected.stock_id] -= request.requested_qty
            tasks.append(
                ValidatedTask(
                    task_id=_task_id(order_id, index),
                    task_type="outbound_pick",
                    pickup_node=access_choice.access_node_id,
                    delivery_node=selected.delivery_node,
                    demand=request.requested_qty,
                    priority=request.priority,
                    item_id=request.item_id,
                    order_id=order_id,
                    stock_id=selected.stock_id,
                    rack_id=selected.rack_id,
                    rack_level=selected.rack_level,
                    fixed_robot_id=request.fixed_robot_id,
                )
            )
            allocations.append(
                StockAllocation(
                    stock_id=selected.stock_id,
                    item_id=selected.item_id,
                    rack_id=selected.rack_id,
                    service_node_id=access_choice.access_node_id,
                    rack_level=selected.rack_level,
                    quantity=request.requested_qty,
                    selection_cost=round(float(score), 6),
                )
            )
            checks.append(
                PolicyCheck(
                    check_type="GLOBAL_STOCK_ALLOCATION",
                    status="pass",
                    target=order_id,
                    detail={
                        "stock_id": selected.stock_id,
                        "rack_id": selected.rack_id,
                        "service_node_id": access_choice.access_node_id,
                        "rack_level": selected.rack_level,
                        "quantity": request.requested_qty,
                        "lower_bound_route_cost": round(float(score), 6),
                        "robot_fixed": request.fixed_robot_id is not None,
                    },
                )
            )

        if violations:
            status = "repairable" if all(value.repairable for value in violations) else "fail"
        else:
            status = "pass"
        return policy.model_copy(
            update={
                "status": status,
                "validated_tasks": tasks,
                "stock_allocations": allocations,
                "checks": checks,
                "violations": violations,
            }
        )

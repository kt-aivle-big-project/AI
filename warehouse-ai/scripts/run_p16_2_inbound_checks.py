"""Run deterministic P16.2 inbound execution checks without external services."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.models import (
    AtomicTask,
    CommandInterpretation,
    InventoryOperationRequest,
    TaskScheduleConstraint,
)
from app.planning.nodes import (
    _reconcile_routing_schedule,
    select_inbound_route_nodes,
    select_required_tasks_node,
)
from app.services.command_language import parse_deterministic_command
from app.services.local_optimizer import LocalOptimizer
from app.services.robot_adapter import RobotAdapter
from app.services.routing import PrioritizedTimeExpandedPlanner


REFERENCE = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 24, 4, 0, tzinfo=UTC)


def _nodes() -> list[dict[str, Any]]:
    return [
        {"node_id": 1, "node_type": "INTERSECTION", "active": True},
        {"node_id": 10, "node_type": "INBOUND", "active": True},
        {"node_id": 11, "node_type": "INBOUND", "active": False},
        {"node_id": 12, "node_type": "INBOUND", "active": True},
        {"node_id": 20, "node_type": "STORAGE", "active": True},
    ]


def _edges() -> list[dict[str, Any]]:
    return [
        {"from_node": 1, "to_node": 10, "distance": 1, "travel_seconds": 5, "direction": "BOTH", "active": True},
        {"from_node": 10, "to_node": 20, "distance": 2, "travel_seconds": 10, "direction": "BOTH", "active": True},
        {"from_node": 1, "to_node": 12, "distance": 5, "travel_seconds": 25, "direction": "BOTH", "active": True},
        {"from_node": 12, "to_node": 20, "distance": 1, "travel_seconds": 5, "direction": "BOTH", "active": True},
        {"from_node": 1, "to_node": 11, "distance": 0.1, "travel_seconds": 1, "direction": "BOTH", "active": True},
        {"from_node": 11, "to_node": 20, "distance": 0.1, "travel_seconds": 1, "direction": "BOTH", "active": True},
    ]


def _robots() -> list[dict[str, Any]]:
    return [
        {
            "robot_id": "R2-01",
            "node_id": 1,
            "battery": 90,
            "max_load": 50,
            "status": "ACTIVE",
        }
    ]


def _snapshot() -> dict[str, Any]:
    return {
        "sql": {
            "works": [],
            "work_dependencies": [],
            "work_schedule_constraints": [],
            "robots": _robots(),
        },
        "graph": {"nodes": _nodes(), "edges": _edges()},
        "redis": {"active_plan": None, "robots": []},
    }


def _interpretation() -> CommandInterpretation:
    return CommandInterpretation(
        command_kind="PLAN",
        intent="INBOUND",
        objective="C상품 50 BOX 입고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[
            InventoryOperationRequest(
                operation_id="OP-C",
                operation_type="INBOUND",
                item_id="C",
                quantity_boxes=50,
                expected_arrival_at=WINDOW_START,
                storage_node_id=20,
            )
        ],
        target_node_ids=[20],
        target_node_type="STORAGE",
        scheduled_task_constraints=[
            TaskScheduleConstraint(
                work_id="OP-C",
                earliest_start=WINDOW_START,
                latest_finish=WINDOW_END,
                time_constraint_type="HARD_WINDOW",
            )
        ],
        planning_reference={
            "original_text": "2026년 7월 24일 오전 7시 15분",
            "local_at": "2026-07-24T07:15:00+09:00",
            "utc_at": REFERENCE,
            "timezone": "Asia/Seoul",
            "source": "USER_COMMAND",
        },
        daily_schedule_requested=True,
        summary="P16.2 check",
    )


def _select_tasks() -> dict[str, Any]:
    interpretation = _interpretation()
    return select_required_tasks_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "scope": {
                "plan_mode": "INITIAL_PLAN",
                "fixed_task_ids": [],
                "changeable_task_ids": [],
                "affected_robot_ids": [],
                "affected_task_ids": [],
                "freeze_horizon_seconds": 15,
                "include_new_command": True,
                "optimization_goal": "inbound check",
                "reason_summary": "inbound check",
            },
            "snapshot": _snapshot(),
            "inventory_feasibility": {
                "status": "PASS",
                "valid": True,
                "partial_success": False,
                "item_results": [
                    {
                        "operation_id": "OP-C",
                        "operation_type": "INBOUND",
                        "item_id": "C",
                        "requested_quantity_boxes": 50,
                        "planned_quantity_boxes": 50,
                        "available_quantity_boxes": 0,
                        "shortage_quantity_boxes": 0,
                        "status": "PASS",
                        "lot_allocations": [],
                    }
                ],
                "shortage_work_ids": [],
                "blocked_work_ids": [],
                "independent_work_ids": ["OP-C"],
            },
            "inventory_blocked_work_ids": [],
            "command": {"command_id": "P16-2-CHECK"},
        }
    )


def _parse_check() -> dict[str, Any]:
    result = parse_deterministic_command(
        "2026년 7월 24일 오전 7시 15분을 기준으로 오전 11시부터 오후 1시 사이에 "
        "C상품 50 BOX를 입고 구역에서 수령하여 저장 노드 2088에 보관하는 계획을 "
        "시뮬레이션해줘. 배정 로봇과 MOVE, WAIT, PICKUP, DROPOFF 명령을 보여줘.",
        reference_time=datetime(2026, 7, 24, 6, 0, tzinfo=UTC),
        warehouse_timezone=None,
    )
    operation = result.inventory_operations[0] if result.inventory_operations else None
    constraint = (
        result.scheduled_task_constraints[0]
        if result.scheduled_task_constraints
        else None
    )
    passed = bool(
        result.intent == "INBOUND"
        and result.target_node_type == "STORAGE"
        and result.target_node_ids == [2088]
        and operation is not None
        and operation.storage_node_id == 2088
        and operation.expected_arrival_at == WINDOW_START
        and constraint is not None
        and constraint.earliest_start == WINDOW_START
        and constraint.latest_finish == WINDOW_END
    )
    return {
        "passed": passed,
        "intent": result.intent,
        "target_node_ids": result.target_node_ids,
        "storage_node_id": operation.storage_node_id if operation else None,
        "earliest_start": constraint.earliest_start.isoformat() if constraint else None,
        "latest_finish": constraint.latest_finish.isoformat() if constraint else None,
    }


def _source_selection_check() -> dict[str, Any]:
    source, target, evidence = select_inbound_route_nodes(
        _snapshot(),
        source_candidates=[10, 11, 12],
        target_candidates=[20],
    )
    return {
        "passed": source == 10 and target == 20 and evidence["source_candidate_count"] == 2,
        **evidence,
    }


def _task_and_command_check() -> dict[str, Any]:
    update = _select_tasks()
    tasks = [AtomicTask.model_validate(row) for row in update["required_tasks"]]
    problem = {
        "warehouse_id": 2,
        "reference_time": REFERENCE,
        "time_step_seconds": 5,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
        "robots": _robots(),
        "nodes": _nodes(),
        "edges": _edges(),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "weights": {},
        "hard_constraints": [],
    }
    optimized = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
    ).optimize(problem)
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(optimized)
    operational, _ = _reconcile_routing_schedule(optimized, collision, problem)
    batches, validation = RobotAdapter(time_step_seconds=5).adapt(
        "PLAN-P16-2",
        {
            "warehouse_id": 2,
            "cuopt_plan": operational.model_dump(mode="json"),
            "required_tasks": [task.model_dump(mode="json") for task in tasks],
            "inventory_operations": _interpretation().model_dump(mode="json")[
                "inventory_operations"
            ],
            "collision_plan": collision.model_dump(mode="json"),
            "charger_node_ids": [],
        },
    )
    commands = batches[0].commands if batches else []
    actions = [command.action for command in commands]
    pickup = next((row for row in commands if row.action == "PICKUP"), None)
    dropoff = next((row for row in commands if row.action == "DROPOFF"), None)
    passed = bool(
        len(tasks) == 2
        and {task.action for task in tasks} == {"PICK", "DROP"}
        and validation["valid"]
        and actions.count("PICKUP") == 1
        and actions.count("DROPOFF") == 1
        and pickup is not None
        and pickup.node_id == 10
        and dropoff is not None
        and dropoff.node_id == 20
        and dropoff.payload.get("destination_node_id") == 20
    )
    return {
        "passed": passed,
        "task_count": len(tasks),
        "robot_count": len(batches),
        "command_count": sum(batch.command_count for batch in batches),
        "actions": actions,
        "adapter_valid": validation["valid"],
        "pickup_node_id": pickup.node_id if pickup else None,
        "dropoff_node_id": dropoff.node_id if dropoff else None,
    }


def run_all_checks() -> dict[str, Any]:
    checks = {
        "inbound_command_parsing": _parse_check(),
        "active_inbound_source_selection": _source_selection_check(),
        "inbound_task_and_command_generation": _task_and_command_check(),
    }
    return {
        "all_passed": all(row["passed"] for row in checks.values()),
        "checks": checks,
    }


def main() -> None:
    result = run_all_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

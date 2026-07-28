from datetime import UTC, datetime

from app.planning.nodes import (
    _reconcile_routing_schedule,
    inventory_precheck_node,
    select_required_tasks_node,
)
from app.services.command_language import parse_deterministic_command
from app.services.local_optimizer import LocalOptimizer
from app.services.robot_adapter import RobotAdapter
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.user_reporting import build_user_report_summary


REFERENCE = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
OUTBOUND_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
OUTBOUND_END = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)
INBOUND_START = OUTBOUND_END
INBOUND_END = datetime(2026, 7, 24, 4, 0, tzinfo=UTC)

COMMAND = (
    "2026년 7월 24일 오전 7시 15분을 기준으로 오전 9시부터 오전 11시 사이에 "
    "A상품 30 BOX와 B상품 20 BOX를 출고 노드 30으로 출고하고, "
    "A상품 재고가 부족하면 A 작업만 제외하고 B 작업은 계속 진행해줘. "
    "오전 작업이 끝난 뒤 필요한 경우에만 최소 운용 배터리 20%를 유지하도록 충전하고, "
    "오전 11시부터 오후 1시 사이에 C상품 50 BOX를 활성 입고 구역에서 수령하여 "
    "저장 노드 20에 보관하는 하루 계획을 시뮬레이션해줘. "
    "작업 시간, 배정 로봇과 MOVE, WAIT, CHARGE, PICKUP, DROPOFF 명령을 보여줘."
)


def _interpretation():
    return parse_deterministic_command(
        COMMAND,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )


def _snapshot() -> dict:
    return {
        "captured_at": REFERENCE,
        "sql": {
            "works": [],
            "work_dependencies": [],
            "work_schedule_constraints": [],
            "inventory_items": [
                {"item_id": "A"},
                {"item_id": "B"},
                {"item_id": "C"},
            ],
            "inventory": [
                {
                    "warehouse_item_id": "LOT-A",
                    "item_id": "A",
                    "available_quantity": 10,
                    "node_id": 20,
                    "available_at": REFERENCE,
                    "status": "AVAILABLE",
                },
                {
                    "warehouse_item_id": "LOT-B",
                    "item_id": "B",
                    "available_quantity": 20,
                    "node_id": 20,
                    "available_at": REFERENCE,
                    "status": "AVAILABLE",
                },
            ],
            "inbound_orders": [],
            "outbound_orders": [],
            "storage_capacity": None,
            "robots": [
                {
                    "robot_id": "R1",
                    "node_id": 1,
                    "battery": 90,
                    "status": "IDLE",
                    "max_load": 50,
                }
            ],
        },
        "graph": {
            "nodes": [
                {"node_id": 1, "node_type": "CHARGER", "active": True},
                {"node_id": 10, "node_type": "INBOUND", "active": True},
                {"node_id": 20, "node_type": "STORAGE", "active": True},
                {"node_id": 30, "node_type": "OUTBOUND", "active": True},
                {
                    "node_id": 40,
                    "node_type": "PARKING",
                    "idle_allowed": True,
                    "active": True,
                },
            ],
            "edges": [
                {
                    "from_node": 1,
                    "to_node": 20,
                    "distance": 2,
                    "travel_seconds": 10,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 20,
                    "to_node": 30,
                    "distance": 3,
                    "travel_seconds": 15,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 30,
                    "to_node": 10,
                    "distance": 4,
                    "travel_seconds": 20,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 10,
                    "to_node": 20,
                    "distance": 2,
                    "travel_seconds": 10,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 1,
                    "to_node": 40,
                    "distance": 1,
                    "travel_seconds": 5,
                    "direction": "BOTH",
                    "active": True,
                },
            ],
        },
        "redis": {
            "active_plan": None,
            "robots": [],
            "inventory_reservations": [],
        },
    }


def _precheck() -> tuple[object, dict, dict]:
    interpretation = _interpretation()
    snapshot = _snapshot()
    update = inventory_precheck_node(
        {
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": snapshot,
        }
    )
    return interpretation, snapshot, update


def _selected_tasks() -> tuple[object, dict, dict, dict]:
    interpretation, snapshot, precheck = _precheck()
    selected = select_required_tasks_node(
        {
            "interpretation": precheck["interpretation"],
            "inventory_operations": precheck["inventory_operations"],
            "inventory_feasibility": precheck["inventory_feasibility"],
            "inventory_blocked_work_ids": precheck["inventory_blocked_work_ids"],
            "scope": {
                "plan_mode": "INITIAL_PLAN",
                "fixed_task_ids": [],
                "changeable_task_ids": [],
                "affected_robot_ids": [],
                "affected_task_ids": [],
                "freeze_horizon_seconds": 15,
                "include_new_command": True,
                "optimization_goal": "P16.3 final integration",
                "reason_summary": "P16.3 final integration",
            },
            "snapshot": snapshot,
            "command": {"command_id": "P16-3-FINAL"},
        }
    )
    return interpretation, snapshot, precheck, selected


def test_combined_command_is_real_daily_plan_not_hypothetical_shortage() -> None:
    result = _interpretation()
    by_item = {row.item_id: row for row in result.inventory_operations}
    windows = {
        row.work_id: (row.earliest_start, row.latest_finish)
        for row in result.scheduled_task_constraints
    }

    assert result.intent == "DAILY_PLAN"
    assert result.execution_mode == "SIMULATE_ONLY"
    assert result.hypothetical_events == []
    assert "MINIMUM_REQUIRED_CHARGE" in result.hard_constraints
    assert set(by_item) == {"A", "B", "C"}
    assert result.target_node_type == "OUTBOUND"
    assert result.target_node_ids == [30]
    assert by_item["C"].storage_node_id == 20
    assert windows[by_item["A"].operation_id] == (OUTBOUND_START, OUTBOUND_END)
    assert windows[by_item["B"].operation_id] == (OUTBOUND_START, OUTBOUND_END)
    assert windows[by_item["C"].operation_id] == (INBOUND_START, INBOUND_END)


def test_inventory_shortage_blocks_only_a_and_preserves_b_and_c() -> None:
    interpretation, _, update = _precheck()
    operation_ids = {row.item_id: row.operation_id for row in interpretation.inventory_operations}
    feasibility = update["inventory_feasibility"]
    by_item = {row["item_id"]: row for row in feasibility["item_results"]}

    assert feasibility["status"] == "PARTIAL_SUCCESS"
    assert feasibility["valid"] is True
    assert feasibility["partial_success"] is True
    assert by_item["A"]["planned_quantity_boxes"] == 0
    assert by_item["A"]["shortage_quantity_boxes"] == 20
    assert by_item["B"]["planned_quantity_boxes"] == 20
    assert by_item["C"]["planned_quantity_boxes"] == 50
    assert update["inventory_blocked_work_ids"] == [operation_ids["A"]]
    assert set(feasibility["independent_work_ids"]) == {
        operation_ids["B"],
        operation_ids["C"],
    }


def test_mixed_task_generation_keeps_operation_specific_destinations() -> None:
    interpretation, _, _, selected = _selected_tasks()
    operation_ids = {row.item_id: row.operation_id for row in interpretation.inventory_operations}
    tasks = selected["required_tasks"]

    assert len(tasks) == 4
    assert not any(row["work_id"] == operation_ids["A"] for row in tasks)
    by_item_action = {(row["item_id"], row["action"]): row for row in tasks}
    assert by_item_action[("B", "DROP")]["target_candidates"] == [30]
    assert by_item_action[("C", "PICK")]["source_candidates"] == [10]
    assert by_item_action[("C", "DROP")]["target_candidates"] == [20]
    assert by_item_action[("B", "PICK")]["earliest_start"] == OUTBOUND_START.isoformat().replace("+00:00", "Z")
    assert by_item_action[("C", "PICK")]["earliest_start"] == INBOUND_START.isoformat().replace("+00:00", "Z")


def test_combined_optimizer_route_and_robot_commands_include_only_b_and_c() -> None:
    interpretation, snapshot, precheck, selected = _selected_tasks()
    problem = {
        "warehouse_id": 2,
        "reference_time": REFERENCE,
        "time_step_seconds": 5,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80,
        "charge_rate_percent_per_minute": 5,
        "robots": snapshot["sql"]["robots"],
        "nodes": snapshot["graph"]["nodes"],
        "edges": snapshot["graph"]["edges"],
        "tasks": selected["required_tasks"],
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "weights": {},
        "hard_constraints": interpretation.hard_constraints,
    }
    optimized = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
    ).optimize(problem)
    routed = PrioritizedTimeExpandedPlanner(problem, 5, 5000).solve(optimized)
    operational, _ = _reconcile_routing_schedule(optimized, routed, problem)
    batches, validation = RobotAdapter(time_step_seconds=5).adapt(
        "P16-3-FINAL",
        {
            "warehouse_id": 2,
            "cuopt_plan": operational.model_dump(mode="json"),
            "required_tasks": selected["required_tasks"],
            "inventory_operations": precheck["inventory_operations"],
            "collision_plan": routed.model_dump(mode="json"),
            "charger_node_ids": [1],
        },
    )

    assert optimized.unassigned_task_ids == []
    assert [row.action for row in optimized.scheduled_tasks] == [
        "PICK", "DROP", "PICK", "DROP"
    ]
    assert optimized.scheduled_tasks[0].start_time_step >= 1260
    assert optimized.scheduled_tasks[2].start_time_step >= 2700
    assert not any(row.action == "CHARGE" for row in optimized.scheduled_tasks)
    assert validation["valid"] is True

    commands = [command for batch in batches for command in batch.commands]
    payload_commands = [
        command for command in commands if command.action in {"PICKUP", "DROPOFF"}
    ]
    assert [row.payload.get("item_id") for row in payload_commands] == [
        "B", "B", "C", "C"
    ]
    assert next(
        row for row in payload_commands
        if row.action == "DROPOFF" and row.payload.get("item_id") == "B"
    ).payload["destination_node_id"] == 30
    assert next(
        row for row in payload_commands
        if row.action == "DROPOFF" and row.payload.get("item_id") == "C"
    ).payload["destination_node_id"] == 20
    assert not any(row.payload.get("item_id") == "A" for row in commands)


def test_final_report_marks_combined_result_as_partial_success() -> None:
    interpretation, _, precheck, _ = _selected_tasks()
    state = {
        "interpretation": interpretation.model_dump(mode="json"),
        "inventory_feasibility": precheck["inventory_feasibility"],
        "verification_decision": {"decision": "PASS"},
        "final_status": "SIMULATION_SUCCESS",
    }
    data = {
        "valid": True,
        "execution_mode": "SIMULATE_ONLY",
        "inventory_feasibility": precheck["inventory_feasibility"],
        "daily_schedule": [],
        "warnings": [],
    }
    summary = build_user_report_summary(
        state,
        data,
        report_level="DEBUG",
    )

    assert summary.outcome == "PARTIAL_SUCCESS_WITH_EMERGENCY"
    assert "일부 작업" in summary.title

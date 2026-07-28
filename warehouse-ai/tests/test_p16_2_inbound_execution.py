from datetime import UTC, datetime

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


def _nodes() -> list[dict]:
    return [
        {"node_id": 1, "node_type": "INTERSECTION", "active": True},
        {"node_id": 10, "node_type": "INBOUND", "active": True},
        {"node_id": 11, "node_type": "INBOUND", "active": False},
        {"node_id": 12, "node_type": "INBOUND", "active": True},
        {"node_id": 20, "node_type": "STORAGE", "active": True},
    ]


def _edges() -> list[dict]:
    return [
        {
            "from_node": 1,
            "to_node": 10,
            "distance": 1,
            "travel_seconds": 5,
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
            "to_node": 12,
            "distance": 5,
            "travel_seconds": 25,
            "direction": "BOTH",
            "active": True,
        },
        {
            "from_node": 12,
            "to_node": 20,
            "distance": 1,
            "travel_seconds": 5,
            "direction": "BOTH",
            "active": True,
        },
        {
            "from_node": 1,
            "to_node": 11,
            "distance": 0.1,
            "travel_seconds": 1,
            "direction": "BOTH",
            "active": True,
        },
        {
            "from_node": 11,
            "to_node": 20,
            "distance": 0.1,
            "travel_seconds": 1,
            "direction": "BOTH",
            "active": True,
        },
    ]


def _snapshot() -> dict:
    return {
        "sql": {
            "works": [],
            "work_dependencies": [],
            "work_schedule_constraints": [],
            "robots": [
                {
                    "robot_id": "R2-01",
                    "node_id": 1,
                    "battery": 90,
                    "max_load": 50,
                    "status": "ACTIVE",
                }
            ],
        },
        "graph": {"nodes": _nodes(), "edges": _edges()},
        "redis": {"active_plan": None, "robots": []},
    }


def _interpretation() -> CommandInterpretation:
    operation = InventoryOperationRequest(
        operation_id="OP-C",
        operation_type="INBOUND",
        item_id="C",
        quantity_boxes=50,
        expected_arrival_at=WINDOW_START,
        storage_node_id=20,
    )
    return CommandInterpretation(
        command_kind="PLAN",
        intent="INBOUND",
        objective="C상품 50 BOX 입고",
        execution_mode="SIMULATE_ONLY",
        inventory_operations=[operation],
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
        summary="P16.2 test",
    )


def _select_update() -> dict:
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
                "optimization_goal": "test",
                "reason_summary": "test",
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
            "command": {"command_id": "C-P16-2"},
        }
    )


def test_inbound_command_extracts_storage_destination_and_window() -> None:
    result = parse_deterministic_command(
        "2026년 7월 24일 오전 7시 15분을 기준으로 오전 11시부터 오후 1시 사이에 "
        "C상품 50 BOX를 입고 구역에서 수령하여 저장 노드 2088에 보관하는 계획을 "
        "시뮬레이션해줘. 배정 로봇과 MOVE, WAIT, PICKUP, DROPOFF 명령을 보여줘.",
        reference_time=datetime(2026, 7, 24, 6, 0, tzinfo=UTC),
        warehouse_timezone=None,
    )

    assert result.intent == "INBOUND"
    assert result.target_node_type == "STORAGE"
    assert result.target_node_ids == [2088]
    assert len(result.inventory_operations) == 1
    operation = result.inventory_operations[0]
    assert operation.storage_node_id == 2088
    assert operation.expected_arrival_at == WINDOW_START
    assert len(result.scheduled_task_constraints) == 1
    constraint = result.scheduled_task_constraints[0]
    assert constraint.earliest_start == WINDOW_START
    assert constraint.latest_finish == WINDOW_END




def test_explicit_inbound_node_is_source_not_destination() -> None:
    result = parse_deterministic_command(
        "2026년 7월 24일 오전 11시부터 오후 1시 사이에 C상품 50 BOX를 "
        "입고 노드 2136에서 수령하여 저장 노드 2088에 보관해줘.",
        reference_time=datetime(2026, 7, 24, 0, 0, tzinfo=UTC),
        warehouse_timezone="Asia/Seoul",
    )

    assert result.source_node_ids == [2136]
    assert result.target_node_ids == [2088]
    assert result.target_node_type == "STORAGE"
    assert result.inventory_operations[0].storage_node_id == 2088


def test_inbound_source_selection_ignores_inactive_node() -> None:
    source, target, evidence = select_inbound_route_nodes(
        _snapshot(),
        source_candidates=[10, 11, 12],
        target_candidates=[20],
    )

    assert source == 10
    assert target == 20
    assert evidence["selection_policy"] == (
        "MIN_ACTIVE_ROBOT_APPROACH_PLUS_STORAGE_DISTANCE"
    )
    assert evidence["source_candidate_count"] == 2


def test_inbound_operation_creates_one_pick_drop_pair() -> None:
    update = _select_update()
    tasks = [AtomicTask.model_validate(row) for row in update["required_tasks"]]

    assert len(tasks) == 2
    by_action = {task.action: task for task in tasks}
    assert set(by_action) == {"PICK", "DROP"}
    assert by_action["PICK"].source_candidates == [10]
    assert by_action["PICK"].target_candidates == [10]
    assert by_action["DROP"].source_candidates == [10]
    assert by_action["DROP"].target_candidates == [20]
    assert by_action["DROP"].predecessors == [by_action["PICK"].task_id]
    assert by_action["PICK"].same_robot_group == by_action["DROP"].same_robot_group
    assert all(task.quantity == 50 for task in tasks)
    assert all(task.earliest_start == WINDOW_START for task in tasks)
    assert all(task.latest_finish == WINDOW_END for task in tasks)
    selection = update["schedule_validation"]["inbound_route_selections"][0]
    assert selection["source_node_id"] == 10
    assert selection["target_node_id"] == 20


def test_inbound_plan_emits_pickup_move_dropoff_commands() -> None:
    update = _select_update()
    tasks = [AtomicTask.model_validate(row) for row in update["required_tasks"]]
    problem = {
        "warehouse_id": 2,
        "reference_time": REFERENCE,
        "time_step_seconds": 5,
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
        "robots": _snapshot()["sql"]["robots"],
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
    optimizer = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
    )
    optimized = optimizer.optimize(problem)
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

    assert validation["valid"] is True
    assert len(batches) == 1
    actions = [command.action for command in batches[0].commands]
    assert actions.count("PICKUP") == 1
    assert actions.count("DROPOFF") == 1
    assert actions.index("PICKUP") < actions.index("DROPOFF")
    pickup = next(command for command in batches[0].commands if command.action == "PICKUP")
    dropoff = next(command for command in batches[0].commands if command.action == "DROPOFF")
    assert pickup.node_id == 10
    assert pickup.payload["item_id"] == "C"
    assert pickup.payload["quantity_boxes"] == 50
    assert dropoff.node_id == 20
    assert dropoff.payload["destination_node_id"] == 20

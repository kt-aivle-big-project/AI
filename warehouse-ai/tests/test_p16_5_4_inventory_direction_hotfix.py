from datetime import UTC, datetime

from app.models import (
    AtomicTask,
    CollisionFreePlan,
    CuOptPlan,
    ScheduledTask,
    TimedRoute,
    TimedWaypoint,
)
from app.services.command_language import parse_inventory_operations
from app.services.scheduling import parse_explicit_time_windows
from app.services.simulation import simulate_plan


REFERENCE = datetime(2026, 7, 23, 22, 15, tzinfo=UTC)
COMPLEX_COMMAND = (
    "2026년7월24일 오전7시15분을 기준으로 창고2의 전체 작업 계획을 시뮬레이션해줘. "
    "오전9시부터10시30분까지 A상품30 BOX와 B상품20 BOX를 출고 노드2146으로 이동해줘. "
    "오전10시30분부터12시까지 C상품40 BOX와 D상품20 BOX를 저장 노드2088로 입고해줘. "
    "오후1시부터3시까지 E상품30 BOX와 F상품50 BOX를 출고 노드2146으로 이동해줘. "
    "F상품은 현재 재고30 BOX와 오전7시10분 이후 사용 가능한 예정 입고20 BOX를 함께 사용해줘. "
    "오후3시부터5시까지 C상품20 BOX를 저장 노드2088로 추가 입고해줘."
)


def test_complex_daily_plan_binds_each_clause_to_its_own_direction() -> None:
    operations, missing, ambiguous, _ = parse_inventory_operations(
        COMPLEX_COMMAND,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )

    assert missing == []
    assert ambiguous == []
    assert [
        (row.item_id, row.quantity_boxes, row.operation_type)
        for row in operations
    ] == [
        ("A", 30, "OUTBOUND"),
        ("B", 20, "OUTBOUND"),
        ("C", 40, "INBOUND"),
        ("D", 20, "INBOUND"),
        ("E", 30, "OUTBOUND"),
        ("F", 50, "OUTBOUND"),
        ("C", 20, "INBOUND"),
    ]


def test_morning_range_ending_at_12_means_noon_not_next_midnight() -> None:
    windows = parse_explicit_time_windows(
        "오전10시30분부터12시까지 C상품40 BOX를 입고해줘.",
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )

    assert len(windows) == 1
    assert windows[0]["earliest_start"] == datetime(
        2026, 7, 24, 1, 30, tzinfo=UTC
    )
    assert windows[0]["latest_finish"] == datetime(
        2026, 7, 24, 3, 0, tzinfo=UTC
    )


def _one_task_plan(task_id: str, work_id: str) -> tuple[CollisionFreePlan, CuOptPlan]:
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=[task_id],
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0, action="PICK"),
                    TimedWaypoint(node_id=1, time_step=1, action="PICK"),
                ],
                distance=0,
            )
        ],
        time_step_seconds=5,
        total_distance=0,
        metadata={"task_completion_steps": {task_id: 1}},
    )
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=task_id,
                work_id=work_id,
                action="PICK",
                robot_id="R1",
                source_node=1,
                target_node=1,
                start_time_step=0,
                end_time_step=1,
            )
        ],
        objective_value=0,
    )
    return collision, plan


def test_inbound_pick_does_not_consume_existing_storage_inventory() -> None:
    task_id = "IN-1:1:pick"
    collision, plan = _one_task_plan(task_id, "IN-1")
    problem = {
        "tasks": [
            AtomicTask(
                task_id=task_id,
                work_id="IN-1",
                action="PICK",
                item_id="D",
                quantity=20,
                source_candidates=[1],
                target_candidates=[1],
            ).model_dump(mode="json")
        ],
        "inventory": [{"item_id": "D", "available_quantity": 0}],
        "inventory_operations": [
            {
                "operation_id": "IN-1",
                "operation_type": "INBOUND",
                "item_id": "D",
                "quantity_boxes": 20,
            }
        ],
        "robots": [{"robot_id": "R1", "node_id": 1, "battery": 100}],
        "min_robot_battery": 0,
        "energy_per_distance": 0,
    }

    result = simulate_plan(collision, plan, problem)

    assert result.success
    assert all(issue.code != "INSUFFICIENT_INVENTORY" for issue in result.issues)


def test_future_inbound_allocation_satisfies_outbound_pick() -> None:
    task_id = "OUT-1:1:pick"
    collision, plan = _one_task_plan(task_id, "OUT-1")
    problem = {
        "tasks": [
            AtomicTask(
                task_id=task_id,
                work_id="OUT-1",
                action="PICK",
                item_id="F",
                quantity=50,
                source_candidates=[1],
                target_candidates=[1],
                inventory_allocations=[
                    {
                        "item_id": "F",
                        "quantity_boxes": 30,
                        "source_type": "CURRENT_LOT",
                    },
                    {
                        "item_id": "F",
                        "quantity_boxes": 20,
                        "source_type": "FUTURE_INBOUND",
                    },
                ],
            ).model_dump(mode="json")
        ],
        "inventory": [{"item_id": "F", "available_quantity": 30}],
        "inventory_operations": [
            {
                "operation_id": "OUT-1",
                "operation_type": "OUTBOUND",
                "item_id": "F",
                "quantity_boxes": 50,
            }
        ],
        "robots": [{"robot_id": "R1", "node_id": 1, "battery": 100}],
        "min_robot_battery": 0,
        "energy_per_distance": 0,
    }

    result = simulate_plan(collision, plan, problem)

    assert result.success
    assert all(issue.code != "INSUFFICIENT_INVENTORY" for issue in result.issues)

from datetime import UTC, datetime

from app.models import AtomicTask
from app.services.local_optimizer import LocalOptimizer
from app.services.task_splitting import (
    capacity_trip_pairs,
    outbound_trip_capacity,
    split_allocation_by_capacity,
)


def _problem(tasks: list[AtomicTask]) -> dict:
    return {
        "warehouse_id": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "plan_mode": "INITIAL_PLAN",
        "nodes": [
            {"node_id": 1, "active": True},
            {"node_id": 2, "active": True},
            {"node_id": 3, "active": True},
        ],
        "edges": [
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            },
            {
                "from_node": 2,
                "to_node": 3,
                "distance": 1,
                "travel_seconds": 1,
                "direction": "BOTH",
            },
        ],
        "robots": [
            {
                "robot_id": "R1",
                "node_id": 1,
                "battery": 100,
                "status": "IDLE",
                "max_load": 100,
                "current_load": 0,
            }
        ],
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "inventory": [],
        "temporary_closures": [],
        "active_plan": None,
        "fixed_task_ids": [],
        "changeable_task_ids": [],
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "weights": {},
        "min_robot_battery": 20,
        "energy_per_distance": 0.01,
    }


def _optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20,
        energy_per_distance=0.01,
    )


def test_partial_allocation_is_split_by_robot_capacity() -> None:
    robots = [{"robot_id": "R1", "max_load": 100}]
    capacity = outbound_trip_capacity(robots)
    chunks = split_allocation_by_capacity(
        {"node_id": 2, "quantity": 120, "quantity_boxes": 120},
        capacity,
    )

    assert capacity == 100
    assert [row["quantity_boxes"] for row in chunks] == [100, 20]


def test_unassigned_pick_blocks_its_drop() -> None:
    tasks = [
        AtomicTask(
            task_id="W:1:pick",
            work_id="W",
            action="PICK",
            quantity=120,
            source_candidates=[2],
            target_candidates=[2],
            same_robot_group="W:1",
        ),
        AtomicTask(
            task_id="W:1:drop",
            work_id="W",
            action="DROP",
            quantity=120,
            source_candidates=[2],
            target_candidates=[3],
            predecessors=["W:1:pick"],
            same_robot_group="W:1",
        ),
    ]

    plan = _optimizer().optimize(_problem(tasks))

    assert plan.scheduled_tasks == []
    assert set(plan.unassigned_task_ids) == {"W:1:pick", "W:1:drop"}


def test_split_pick_drop_trips_are_all_assignable() -> None:
    tasks: list[AtomicTask] = []
    previous_drop: str | None = None
    for index, quantity in enumerate((100, 20), start=1):
        prefix = f"W:{index}"
        pick_id = f"{prefix}:pick"
        drop_id = f"{prefix}:drop"
        tasks.extend(
            [
                AtomicTask(
                    task_id=pick_id,
                    work_id="W",
                    action="PICK",
                    quantity=quantity,
                    source_candidates=[2],
                    target_candidates=[2],
                    predecessors=[previous_drop] if previous_drop else [],
                    same_robot_group=prefix,
                ),
                AtomicTask(
                    task_id=drop_id,
                    work_id="W",
                    action="DROP",
                    quantity=quantity,
                    source_candidates=[2],
                    target_candidates=[3],
                    predecessors=[pick_id],
                    same_robot_group=prefix,
                ),
            ]
        )
        previous_drop = drop_id

    plan = _optimizer().optimize(_problem(tasks))

    assert plan.unassigned_task_ids == []
    assert {task.task_id for task in plan.scheduled_tasks} == {
        task.task_id for task in tasks
    }


def test_capacity_trip_pairs_only_link_drop_to_its_own_pick() -> None:
    pairs = capacity_trip_pairs(
        {"node_id": 2, "quantity": 120, "quantity_boxes": 120},
        50,
        prefix_base="W",
    )

    assert [row["allocation"]["quantity_boxes"] for row in pairs] == [50, 50, 20]
    assert [row["pick_predecessors"] for row in pairs] == [[], [], []]
    assert [row["drop_predecessors"] for row in pairs] == [
        ["W:1:pick"],
        ["W:2:pick"],
        ["W:3:pick"],
    ]

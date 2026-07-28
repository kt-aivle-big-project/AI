from copy import deepcopy

from app.services.robot_adapter import RobotAdapter


def outbound_plan() -> dict:
    return {
        "warehouse_id": 1,
        "charger_node_ids": [9],
        "required_tasks": [
            {
                "task_id": "T-OUT",
                "work_id": "W-OUT",
                "action": "MOVE",
                "item_id": "ITEM-A",
                "quantity": 50,
                "inventory_allocations": [
                    {
                        "warehouse_item_id": "LOT-A",
                        "item_id": "ITEM-A",
                        "lot_id": "LOT-A",
                        "quantity_boxes": 30,
                        "storage_node_id": 2,
                        "available_at": "2026-07-24T00:00:00+00:00",
                        "source_type": "CURRENT_LOT",
                    },
                    {
                        "warehouse_item_id": "VIRTUAL-LOT-A",
                        "item_id": "ITEM-A",
                        "lot_id": "FUTURE-LOT-A",
                        "quantity_boxes": 20,
                        "storage_node_id": 2,
                        "source_type": "FUTURE_INBOUND",
                        "inbound_source_id": "INBOUND-1",
                    },
                ],
            }
        ],
        "inventory_operations": [
            {
                "work_id": "W-OUT",
                "order_id": "ORDER-1",
                "operation_type": "OUTBOUND",
                "item_id": "ITEM-A",
            }
        ],
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "T-OUT",
                    "work_id": "W-OUT",
                    "action": "MOVE",
                    "robot_id": "R-01",
                    "source_node": 2,
                    "target_node": 3,
                    "start_time_step": 0,
                    "end_time_step": 4,
                }
            ]
        },
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R-01",
                    "task_ids": ["T-OUT"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 1, "action": "MOVE"},
                        {"node_id": 2, "time_step": 2, "action": "WAIT"},
                        {"node_id": 3, "time_step": 3, "action": "MOVE"},
                    ],
                }
            ]
        },
    }


def test_outbound_route_becomes_ordered_robot_commands() -> None:
    batches, validation = RobotAdapter(time_step_seconds=5).adapt("PLAN-1", outbound_plan())

    assert validation["valid"] is True
    assert len(batches) == 1
    commands = batches[0].commands
    assert [command.action for command in commands] == [
        "START", "MOVE", "PICKUP", "WAIT", "MOVE", "DROPOFF", "STOP"
    ]
    pickup = next(command for command in commands if command.action == "PICKUP")
    dropoff = next(command for command in commands if command.action == "DROPOFF")
    assert pickup.node_id == 2
    assert dropoff.node_id == 3
    assert pickup.payload["item_id"] == "ITEM-A"
    assert pickup.payload["quantity_boxes"] == 50
    assert pickup.payload["lot_allocations"][0]["lot_id"] == "LOT-A"
    assert pickup.payload["lot_allocations"][0]["source_type"] == "CURRENT_LOT"
    assert pickup.payload["lot_allocations"][0]["storage_node_id"] == 2
    assert pickup.payload["lot_allocations"][1]["lot_id"] == "FUTURE-LOT-A"
    assert pickup.payload["lot_allocations"][1]["source_type"] == "FUTURE_INBOUND"
    assert pickup.payload["lot_allocations"][1]["inbound_source_id"] == "INBOUND-1"
    assert pickup.payload["order_id"] == "ORDER-1"
    assert dropoff.payload["destination_node_id"] == 3
    assert dropoff.payload["order_id"] == "ORDER-1"
    wait = next(command for command in commands if command.action == "WAIT")
    assert wait.payload == {"duration_steps": 1, "duration_seconds": 5}
    assert [command.sequence for command in commands] == list(range(1, len(commands) + 1))


def test_command_identifiers_are_deterministic() -> None:
    adapter = RobotAdapter(time_step_seconds=5)

    left, _ = adapter.adapt("PLAN-1", outbound_plan())
    right, _ = adapter.adapt("PLAN-1", outbound_plan())

    assert [command.command_id for command in left[0].commands] == [
        command.command_id for command in right[0].commands
    ]


def test_charge_waypoints_are_merged_and_keep_charge_metadata() -> None:
    plan = {
        "warehouse_id": 1,
        "charger_node_ids": [9],
        "required_tasks": [{"task_id": "T-CHARGE", "action": "CHARGE"}],
        "cuopt_plan": {
            "scheduled_tasks": [{
                "task_id": "T-CHARGE", "action": "CHARGE", "robot_id": "R-01",
                "source_node": 1, "target_node": 9, "start_time_step": 0,
                "end_time_step": 3, "charged_percent": 25.0,
                "charge_target_battery": 80.0,
            }]
        },
        "collision_plan": {"routes": [{
            "robot_id": "R-01", "task_ids": ["T-CHARGE"], "waypoints": [
                {"node_id": 1, "time_step": 0, "action": "MOVE"},
                {"node_id": 9, "time_step": 1, "action": "MOVE"},
                {"node_id": 9, "time_step": 2, "action": "CHARGE"},
                {"node_id": 9, "time_step": 3, "action": "CHARGE"},
            ],
        }]},
    }

    batches, validation = RobotAdapter(time_step_seconds=5).adapt("PLAN-CHARGE", plan)

    assert validation["valid"] is True
    charges = [command for command in batches[0].commands if command.action == "CHARGE"]
    assert len(charges) == 1
    assert charges[0].node_id == 9
    assert charges[0].payload == {
        "charger_node_id": 9,
        "duration_steps": 2,
        "duration_seconds": 10,
        "charged_percent": 25.0,
        "target_battery": 80.0,
    }


def test_multiple_robots_get_separate_batches_and_invalid_assignment_is_blocked() -> None:
    plan = outbound_plan()
    second = deepcopy(plan["cuopt_plan"]["scheduled_tasks"][0])
    second.update({"task_id": "T-SECOND", "work_id": "W-SECOND", "robot_id": "R-02"})
    plan["cuopt_plan"]["scheduled_tasks"].append(second)
    plan["required_tasks"].append({"task_id": "T-SECOND", "action": "MOVE"})
    plan["collision_plan"]["routes"].append({
        "robot_id": "R-02", "task_ids": ["T-SECOND"],
        "waypoints": [{"node_id": 2, "time_step": 0}, {"node_id": 3, "time_step": 1}],
    })

    batches, validation = RobotAdapter().adapt("PLAN-2", plan)
    assert validation["valid"] is True
    assert [batch.robot_id for batch in batches] == ["R-01", "R-02"]

    plan["collision_plan"]["routes"][1]["robot_id"] = "R-WRONG"
    _, validation = RobotAdapter().adapt("PLAN-2", plan)
    assert validation["valid"] is False
    assert any(error.startswith("ROBOT_ASSIGNMENT_MISMATCH") for error in validation["errors"])


def test_invalid_pickup_dropoff_order_is_rejected() -> None:
    batches, _ = RobotAdapter().adapt("PLAN-1", outbound_plan())
    commands = [command.model_copy() for command in batches[0].commands if command.action != "PICKUP"]
    for sequence, command in enumerate(commands, start=1):
        command.sequence = sequence
    batches[0].commands = commands
    batches[0].command_count = len(commands)

    validation = RobotAdapter().validate(batches, {9})
    assert validation["valid"] is False
    assert "PICKUP_DROPOFF_ORDER_INVALID:T-OUT" in validation["errors"]


def test_atomic_pick_drop_pair_emits_one_handling_command_each() -> None:
    plan = {
        "warehouse_id": 1,
        "charger_node_ids": [],
        "required_tasks": [
            {
                "task_id": "OP-1:1:pick",
                "work_id": "OP-1",
                "action": "PICK",
                "item_id": "ITEM-A",
                "quantity": 15,
                "inventory_allocations": [
                    {
                        "warehouse_item_id": "LOT-A",
                        "item_id": "ITEM-A",
                        "lot_id": "LOT-A",
                        "quantity_boxes": 15,
                        "storage_node_id": 2,
                    }
                ],
            },
            {
                "task_id": "OP-1:1:drop",
                "work_id": "OP-1",
                "action": "DROP",
                "item_id": "ITEM-A",
                "quantity": 15,
                "inventory_allocations": [
                    {
                        "warehouse_item_id": "LOT-A",
                        "item_id": "ITEM-A",
                        "lot_id": "LOT-A",
                        "quantity_boxes": 15,
                        "storage_node_id": 2,
                    }
                ],
            },
        ],
        "inventory_operations": [],
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "OP-1:1:pick",
                    "work_id": "OP-1",
                    "action": "PICK",
                    "robot_id": "R-01",
                    "source_node": 2,
                    "target_node": 2,
                    "start_time_step": 0,
                    "end_time_step": 5,
                },
                {
                    "task_id": "OP-1:1:drop",
                    "work_id": "OP-1",
                    "action": "DROP",
                    "robot_id": "R-01",
                    "source_node": 2,
                    "target_node": 3,
                    "start_time_step": 5,
                    "end_time_step": 10,
                },
            ]
        },
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R-01",
                    "task_ids": ["OP-1:1:pick", "OP-1:1:drop"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0, "action": "MOVE"},
                        {"node_id": 2, "time_step": 5, "action": "MOVE"},
                        {"node_id": 3, "time_step": 10, "action": "MOVE"},
                    ],
                }
            ]
        },
    }

    batches, validation = RobotAdapter(time_step_seconds=5).adapt("PLAN-PAIR", plan)

    assert validation["valid"] is True
    handling = [
        command
        for command in batches[0].commands
        if command.action in {"PICKUP", "DROPOFF"}
    ]
    assert [(row.action, row.task_id, row.node_id, row.time_step) for row in handling] == [
        ("PICKUP", "OP-1:1:pick", 2, 5),
        ("DROPOFF", "OP-1:1:drop", 3, 10),
    ]

from app.services.robot_adapter import RobotAdapter


def _boundary_plan(*, duration_seconds: int, charge_end_step: int) -> dict:
    charge_id = "opportunity:R:work:pick:charge:2150"
    move_id = f"{charge_id}:move_to_next:work:pick"
    waypoints = [
        {"node_id": 2150, "time_step": 0, "action": "MOVE"},
    ]
    waypoints.extend(
        {
            "node_id": 2150,
            "time_step": step,
            "action": "CHARGE",
        }
        for step in range(1, charge_end_step + 1)
    )
    # The final CHARGE waypoint occurs at the exact start time of the
    # following explicit MOVE assignment.  Even without a duplicate waypoint,
    # both assignments match that boundary time and node.
    waypoints.append(
        {"node_id": 2139, "time_step": charge_end_step + 2, "action": "MOVE"}
    )
    return {
        "warehouse_id": 2,
        "charger_node_ids": [2150],
        "required_tasks": [],
        "inventory_operations": [],
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": charge_id,
                    "work_id": "work",
                    "action": "CHARGE",
                    "robot_id": "R",
                    "source_node": 2150,
                    "target_node": 2150,
                    "start_time_step": 0,
                    "end_time_step": charge_end_step,
                    "charge_duration_seconds": duration_seconds,
                    "charged_percent": 5,
                    "charge_target_battery": 95,
                },
                {
                    "task_id": move_id,
                    "work_id": "work",
                    "action": "MOVE",
                    "robot_id": "R",
                    "source_node": 2150,
                    "target_node": 2139,
                    "start_time_step": charge_end_step,
                    "end_time_step": charge_end_step + 2,
                },
            ]
        },
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R",
                    "task_ids": [charge_id, move_id],
                    "waypoints": waypoints,
                }
            ]
        },
    }


def _charge_commands(plan: dict):
    batches, validation = RobotAdapter(time_step_seconds=5).adapt("P", plan)
    commands = [
        command
        for batch in batches
        for command in batch.commands
        if command.action == "CHARGE"
    ]
    return commands, validation


def test_charge_boundary_waypoint_stays_with_charge_task_for_60_seconds() -> None:
    plan = _boundary_plan(duration_seconds=60, charge_end_step=12)
    commands, validation = _charge_commands(plan)

    charge_id = plan["cuopt_plan"]["scheduled_tasks"][0]["task_id"]
    command = next(command for command in commands if command.task_id == charge_id)
    assert command.payload["duration_steps"] == 12
    assert command.payload["duration_seconds"] == 60
    assert validation["valid"] is True


def test_charge_boundary_waypoint_stays_with_charge_task_for_35_seconds() -> None:
    plan = _boundary_plan(duration_seconds=35, charge_end_step=7)
    commands, validation = _charge_commands(plan)

    charge_id = plan["cuopt_plan"]["scheduled_tasks"][0]["task_id"]
    command = next(command for command in commands if command.task_id == charge_id)
    assert command.payload["duration_steps"] == 7
    assert command.payload["duration_seconds"] == 35
    assert validation["valid"] is True


def test_no_charge_command_is_emitted_for_following_move_task_at_boundary() -> None:
    plan = _boundary_plan(duration_seconds=60, charge_end_step=12)
    commands, _ = _charge_commands(plan)

    move_id = plan["cuopt_plan"]["scheduled_tasks"][1]["task_id"]
    assert all(command.task_id != move_id for command in commands)

"""Deterministic adapter from a collision-free plan to robot command batches."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.models import RobotCommand, RobotCommandBatch


def _command_id(
    plan_version: str,
    robot_id: str,
    task_id: str | None,
    sequence: int,
    action: str,
    node_id: int,
    time_step: int,
) -> str:
    value = ":".join(
        [plan_version, robot_id, task_id or "", str(sequence), action, str(node_id), str(time_step)]
    )
    return str(uuid5(NAMESPACE_URL, value))


def _transfer_key(task_id: str | None) -> str | None:
    """Return the shared transfer key for paired PICK/DROP atomic tasks."""

    if not task_id:
        return None
    value = str(task_id)
    for suffix in (":pick", ":drop"):
        if value.lower().endswith(suffix):
            return value[: -len(suffix)]
    return value


class RobotAdapter:
    def __init__(self, *, time_step_seconds: int = 5):
        self.time_step_seconds = max(1, time_step_seconds)

    def _append(
        self,
        commands: list[dict[str, Any]],
        *,
        task_id: str | None,
        work_id: str | None,
        action: str,
        node_id: int,
        time_step: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload = payload or {}
        # Merge contiguous waiting/charging ranges.  MOVE and handling actions
        # remain explicit because their sequence is operationally meaningful.
        if (
            commands
            and action in {"WAIT", "CHARGE"}
            and commands[-1]["action"] == action
            and commands[-1]["node_id"] == node_id
            and commands[-1].get("task_id") == task_id
        ):
            commands[-1]["payload"]["duration_steps"] = (
                int(commands[-1]["payload"].get("duration_steps", 1)) + 1
            )
            commands[-1]["payload"]["duration_seconds"] = (
                int(commands[-1]["payload"]["duration_steps"])
                * self.time_step_seconds
            )
            return
        commands.append(
            {
                "task_id": task_id,
                "work_id": work_id,
                "action": action,
                "node_id": node_id,
                "time_step": time_step,
                "payload": payload,
            }
        )

    @staticmethod
    def _task_payload(
        task: dict[str, Any],
        *,
        dropoff: bool,
        destination_node_id: int | None = None,
        inventory_operation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allocations = deepcopy(task.get("inventory_allocations") or [])
        quantity = task.get("quantity_boxes", task.get("quantity", 0))
        order_id = (
            task.get("order_id")
            or task.get("inventory_order_id")
            or (inventory_operation or {}).get("order_id")
        )
        if dropoff:
            return {
                "item_id": task.get("item_id"),
                "quantity_boxes": quantity,
                "destination_node_id": destination_node_id
                if destination_node_id is not None
                else task.get("target_node"),
                "order_id": order_id,
            }
        return {
            "item_id": task.get("item_id"),
            "quantity_boxes": quantity,
            "lot_allocations": allocations,
            "order_id": order_id,
        }

    def adapt(self, plan_version: str, plan: dict[str, Any]) -> tuple[list[RobotCommandBatch], dict[str, Any]]:
        warehouse_id = int(plan["warehouse_id"])
        scheduled = {
            str(row["task_id"]): row
            for row in plan.get("cuopt_plan", {}).get("scheduled_tasks", [])
        }
        required = {
            str(row["task_id"]): row
            for row in plan.get("required_tasks", [])
        }
        operations_by_work: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for operation in plan.get("inventory_operations", []):
            operation_key = operation.get("work_id") or operation.get("operation_id")
            if operation_key is not None:
                operations_by_work[str(operation_key)].append(operation)
        charger_nodes = {int(value) for value in plan.get("charger_node_ids", [])}
        by_robot: dict[str, list[dict[str, Any]]] = defaultdict(list)
        route_errors: list[str] = []
        for route in plan.get("collision_plan", {}).get("routes", []):
            robot_id = str(route["robot_id"])
            waypoints = sorted(route.get("waypoints") or [], key=lambda row: int(row["time_step"]))
            if not waypoints:
                continue
            route_tasks = [str(value) for value in route.get("task_ids", [])]
            assignments = []
            for value in route_tasks:
                assignment = scheduled.get(value)
                if assignment is None:
                    continue
                if str(assignment.get("robot_id")) != robot_id:
                    route_errors.append(
                        f"ROBOT_ASSIGNMENT_MISMATCH:{value}:{robot_id}"
                    )
                    continue
                assignments.append(assignment)
            commands = by_robot[robot_id]
            handled_pickups: set[str] = set()
            handled_dropoffs: set[str] = set()
            first = waypoints[0]
            self._append(commands, task_id=None, work_id=None, action="START", node_id=int(first["node_id"]), time_step=int(first["time_step"]))
            previous_node = int(first["node_id"])
            for index, waypoint in enumerate(waypoints):
                node_id = int(waypoint["node_id"])
                time_step = int(waypoint["time_step"])
                action = str(waypoint.get("action") or "MOVE").upper()
                matching = [
                    assignment for assignment in assignments
                    if int(assignment.get("start_time_step") or 0) <= time_step
                    and int(assignment.get("end_time_step") or 0) >= time_step
                    and (
                        (
                            str(assignment.get("action") or "").upper() == "CHARGE"
                            and int(assignment.get("target_node") or -1) == node_id
                        )
                        or (
                            str(assignment.get("action") or "").upper() != "CHARGE"
                            and node_id in {
                                int(assignment.get("source_node") or -1),
                                int(assignment.get("target_node") or -1),
                            }
                        )
                    )
                ]
                # A CHARGE dwell can end at the exact same node/time step
                # where the following explicit MOVE begins.  At that boundary
                # both assignments match the waypoint.  Selecting the last
                # generic match attributes the final CHARGE step to MOVE and
                # shortens the emitted robot command by one time step.  The
                # physical waypoint action is authoritative for CHARGE dwell
                # ownership, so prefer the matching CHARGE assignment.
                if action == "CHARGE":
                    charge_matching = [
                        assignment
                        for assignment in matching
                        if str(assignment.get("action") or "").upper()
                        == "CHARGE"
                        and int(assignment.get("target_node") or -1) == node_id
                    ]
                    current = charge_matching[-1] if charge_matching else (
                        matching[-1] if matching else None
                    )
                else:
                    current = matching[-1] if matching else None
                task_id = str(current["task_id"]) if current else None
                work_id = current.get("work_id") if current else None
                if index and node_id != previous_node:
                    self._append(commands, task_id=task_id, work_id=work_id, action="MOVE", node_id=node_id, time_step=time_step)
                elif action == "WAIT":
                    self._append(commands, task_id=task_id, work_id=work_id, action="WAIT", node_id=node_id, time_step=time_step, payload={"duration_steps": 1, "duration_seconds": self.time_step_seconds})
                elif action == "CHARGE":
                    charge_task = scheduled.get(task_id or "", {})
                    charge_payload = {
                        "charger_node_id": node_id,
                        "duration_steps": 1,
                        "duration_seconds": self.time_step_seconds,
                        "charged_percent": charge_task.get("charged_percent", 0),
                        "target_battery": charge_task.get("charge_target_battery"),
                    }
                    optional_charge_fields = {
                        "charger_cost": charge_task.get("charger_cost"),
                        "selection_policy": charge_task.get(
                            "charger_selection_policy"
                        ),
                        "selection_reason": charge_task.get(
                            "charger_selection_reason"
                        ),
                        "candidates": charge_task.get("charger_candidates"),
                    }
                    charge_payload.update(
                        {
                            key: value
                            for key, value in optional_charge_fields.items()
                            if value not in (None, [], "")
                        }
                    )
                    self._append(
                        commands,
                        task_id=task_id,
                        work_id=work_id,
                        action="CHARGE",
                        node_id=node_id,
                        time_step=time_step,
                        payload=charge_payload,
                    )
                for assignment in assignments:
                    task_key = str(assignment["task_id"])
                    task = required.get(task_key, {})
                    if not task.get("item_id"):
                        continue
                    assignment_action = str(
                        assignment.get("action") or task.get("action") or "MOVE"
                    ).upper()
                    operation = next(
                        (
                            row
                            for row in operations_by_work.get(
                                str(assignment.get("work_id")), []
                            )
                            if (
                                not task.get("item_id")
                                or row.get("item_id") == task.get("item_id")
                            )
                        ),
                        None,
                    )
                    target_node = int(assignment.get("target_node") or -1)
                    source_node = int(assignment.get("source_node") or -1)
                    end_step = int(assignment.get("end_time_step") or 0)

                    # Atomic PICK tasks finish at the inventory location and
                    # produce exactly one pickup command.  Their source and
                    # target are often the same node, so the legacy generic
                    # source/target logic would otherwise emit both pickup
                    # and dropoff at the same instant.
                    if (
                        assignment_action == "PICK"
                        and task_key not in handled_pickups
                        and node_id == target_node
                        and time_step >= end_step
                    ):
                        self._append(
                            commands,
                            task_id=task_key,
                            work_id=assignment.get("work_id"),
                            action="PICKUP",
                            node_id=node_id,
                            time_step=time_step,
                            payload=self._task_payload(
                                task,
                                dropoff=False,
                                inventory_operation=operation,
                            ),
                        )
                        handled_pickups.add(task_key)
                        continue

                    # Atomic DROP tasks only unload at their routed target.
                    # The physical pickup was already represented by the
                    # paired PICK task.
                    if (
                        assignment_action == "DROP"
                        and task_key not in handled_dropoffs
                        and node_id == target_node
                        and time_step >= end_step
                    ):
                        self._append(
                            commands,
                            task_id=task_key,
                            work_id=assignment.get("work_id"),
                            action="DROPOFF",
                            node_id=node_id,
                            time_step=time_step,
                            payload=self._task_payload(
                                task,
                                dropoff=True,
                                destination_node_id=target_node,
                                inventory_operation=operation,
                            ),
                        )
                        handled_dropoffs.add(task_key)
                        continue

                    # Backward-compatible support for legacy single MOVE
                    # tasks that model pickup and dropoff in one assignment.
                    if assignment_action not in {"PICK", "DROP", "CHARGE"}:
                        if (
                            task_key not in handled_pickups
                            and node_id == source_node
                        ):
                            self._append(
                                commands,
                                task_id=task_key,
                                work_id=assignment.get("work_id"),
                                action="PICKUP",
                                node_id=node_id,
                                time_step=time_step,
                                payload=self._task_payload(
                                    task,
                                    dropoff=False,
                                    inventory_operation=operation,
                                ),
                            )
                            handled_pickups.add(task_key)
                        if (
                            task_key in handled_pickups
                            and task_key not in handled_dropoffs
                            and node_id == target_node
                        ):
                            self._append(
                                commands,
                                task_id=task_key,
                                work_id=assignment.get("work_id"),
                                action="DROPOFF",
                                node_id=node_id,
                                time_step=time_step,
                                payload=self._task_payload(
                                    task,
                                    dropoff=True,
                                    destination_node_id=target_node,
                                    inventory_operation=operation,
                                ),
                            )
                            handled_dropoffs.add(task_key)
                previous_node = node_id
            last = waypoints[-1]
            self._append(commands, task_id=None, work_id=None, action="STOP", node_id=int(last["node_id"]), time_step=int(last["time_step"]))

        batches: list[RobotCommandBatch] = []
        errors: list[str] = []
        for robot_id, raw_commands in sorted(by_robot.items()):
            commands = [
                RobotCommand(
                    command_id=_command_id(plan_version, robot_id, row["task_id"], sequence, row["action"], row["node_id"], row["time_step"]),
                    sequence=sequence,
                    plan_version=plan_version,
                    warehouse_id=warehouse_id,
                    robot_id=robot_id,
                    task_id=row["task_id"], work_id=row["work_id"], action=row["action"], node_id=row["node_id"], time_step=row["time_step"], time_step_seconds=self.time_step_seconds, payload=row["payload"],
                )
                for sequence, row in enumerate(raw_commands, start=1)
            ]
            batches.append(RobotCommandBatch(plan_version=plan_version, warehouse_id=warehouse_id, robot_id=robot_id, commands=commands, command_count=len(commands)))
        pickup_required_transfer_keys = {
            key
            for task_id, assignment in scheduled.items()
            if str(assignment.get("action") or "").upper() in {"PICK", "MOVE"}
            and (key := _transfer_key(task_id)) is not None
        }
        validation = self.validate(
            batches,
            charger_nodes,
            pickup_required_transfer_keys=pickup_required_transfer_keys,
        )
        emitted_charge_commands = {
            (batch.robot_id, str(command.task_id)): command
            for batch in batches
            for command in batch.commands
            if command.action == "CHARGE" and command.task_id
        }
        for task_id, assignment in scheduled.items():
            if str(assignment.get("action") or "").upper() != "CHARGE":
                continue
            robot_id = str(assignment.get("robot_id") or "")
            emitted = emitted_charge_commands.get((robot_id, task_id))
            if emitted is None:
                validation["errors"].append(
                    f"CHARGE_COMMAND_MISSING:{robot_id}:{task_id}"
                )
                continue
            planned_duration = assignment.get("charge_duration_seconds")
            if planned_duration is not None and int(
                emitted.payload.get("duration_seconds") or 0
            ) != int(planned_duration):
                validation["errors"].append(
                    "CHARGE_DURATION_MISMATCH:"
                    f"{robot_id}:{task_id}:{planned_duration}:"
                    f"{emitted.payload.get('duration_seconds')}"
                )
        if route_errors:
            validation["errors"].extend(route_errors)
        if validation["errors"]:
            validation["valid"] = False
        return batches, validation

    def validate(
        self,
        batches: list[RobotCommandBatch],
        charger_nodes: set[int],
        *,
        pickup_required_transfer_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        # Direct validate() calls retain the strict legacy behavior.  adapt()
        # supplies the transfers that actually have a PICK/MOVE task in the
        # plan, allowing a standalone DROP continuation for an already-loaded
        # robot without weakening paired PICK/DROP validation.
        enforce_all_dropoffs = pickup_required_transfer_keys is None
        required_pickups = pickup_required_transfer_keys or set()
        for batch in batches:
            previous_time = -1
            pickup_index: dict[str, int] = {}
            for index, command in enumerate(batch.commands, start=1):
                if command.sequence != index:
                    errors.append(f"SEQUENCE_INVALID:{batch.robot_id}")
                if command.time_step < previous_time:
                    errors.append(f"TIME_STEP_DECREASED:{batch.robot_id}")
                previous_time = command.time_step
                transfer_key = _transfer_key(command.task_id)
                if command.action == "PICKUP" and transfer_key:
                    pickup_index[transfer_key] = index
                if (
                    command.action == "DROPOFF"
                    and transfer_key
                    and (enforce_all_dropoffs or transfer_key in required_pickups)
                    and (
                        transfer_key not in pickup_index
                        or pickup_index[transfer_key] >= index
                    )
                ):
                    errors.append(
                        f"PICKUP_DROPOFF_ORDER_INVALID:{command.task_id}"
                    )
                if command.action == "CHARGE" and command.node_id not in charger_nodes:
                    errors.append(f"CHARGE_NODE_INVALID:{command.robot_id}:{command.node_id}")
        return {"valid": not errors, "errors": errors, "batch_count": len(batches), "command_count": sum(batch.command_count for batch in batches)}

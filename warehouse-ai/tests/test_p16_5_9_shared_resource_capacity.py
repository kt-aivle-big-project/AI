from __future__ import annotations

from app.models import CollisionFreePlan, CuOptPlan, ScheduledTask
from app.services.response_view import compact_planning_response
from app.services.shared_resources import (
    finalize_idle_resource_reservations,
    schedule_shared_resources,
)


def _task(
    task_id: str,
    robot_id: str,
    *,
    action: str = "PICK",
    node_id: int = 2088,
    start: int = 0,
    end: int = 2,
    priority: int = 1,
    charge_seconds: int | None = None,
) -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        work_id=task_id.split(":", 1)[0],
        action=action,
        robot_id=robot_id,
        source_node=node_id,
        target_node=node_id,
        start_time_step=start,
        end_time_step=end,
        priority=priority,
        charge_duration_seconds=charge_seconds,
        schedule_status="READY",
    )


def _problem(nodes: list[dict], *, tasks: list[dict] | None = None) -> dict:
    return {
        "reference_time": "2026-07-25T00:00:00+00:00",
        "time_step_seconds": 5,
        "nodes": nodes,
        "tasks": tasks or [],
        "fixed_task_ids": [],
        "weights": {"makespan": 1.0},
    }


def test_service_capacity_one_serializes_simultaneous_tasks() -> None:
    plan = CuOptPlan(
        scheduled_tasks=[
            _task("A:pick", "R1", priority=1),
            _task("B:pick", "R2", priority=2),
        ],
        objective_value=0.0,
    )
    updated, result = schedule_shared_resources(
        _problem(
            [
                {
                    "node_id": 2088,
                    "node_type": "STORAGE",
                    "service_capacity": 1,
                    "service_duration_seconds": 5,
                }
            ]
        ),
        plan,
    )

    assert result["valid"] is True
    assert result["reservation_count"] == 2
    reservations = {row["task_id"]: row for row in result["reservations"]}
    assert reservations["A:pick"]["start_time_step"] == 1
    assert reservations["A:pick"]["end_time_step"] == 2
    assert reservations["B:pick"]["start_time_step"] == 2
    assert reservations["B:pick"]["end_time_step"] == 3
    scheduled = {row.task_id: row for row in updated.scheduled_tasks}
    assert scheduled["B:pick"].start_time_step == 1
    assert scheduled["B:pick"].end_time_step == 3
    assert result["adjustment_count"] >= 1


def test_service_capacity_two_keeps_parallel_service() -> None:
    plan = CuOptPlan(
        scheduled_tasks=[
            _task("A:pick", "R1"),
            _task("B:pick", "R2"),
        ],
        objective_value=0.0,
    )
    updated, result = schedule_shared_resources(
        _problem(
            [
                {
                    "node_id": 2088,
                    "node_type": "STORAGE",
                    "service_capacity": 2,
                    "service_duration_seconds": 5,
                }
            ]
        ),
        plan,
    )

    assert result["valid"] is True
    assert result["adjustment_count"] == 0
    assert {row["slot_index"] for row in result["reservations"]} == {1, 2}
    assert [row.start_time_step for row in updated.scheduled_tasks] == [0, 0]


def test_charger_capacity_shift_cascades_to_next_robot_task() -> None:
    plan = CuOptPlan(
        scheduled_tasks=[
            _task(
                "C1:charge",
                "R1",
                action="CHARGE",
                node_id=2150,
                start=0,
                end=6,
                priority=1,
                charge_seconds=15,
            ),
            _task(
                "C2:charge",
                "R2",
                action="CHARGE",
                node_id=2150,
                start=0,
                end=6,
                priority=2,
                charge_seconds=15,
            ),
            _task(
                "NEXT:pick",
                "R2",
                node_id=2088,
                start=10,
                end=12,
                priority=3,
            ),
        ],
        objective_value=0.0,
        metadata={
            "execution_task_dependencies": [
                {
                    "predecessor_task_id": "C2:charge",
                    "successor_task_id": "NEXT:pick",
                    "reason": "OPPORTUNITY_CHARGING",
                }
            ]
        },
    )
    updated, result = schedule_shared_resources(
        _problem(
            [
                {
                    "node_id": 2150,
                    "node_type": "CHARGER",
                    "charger_capacity": 1,
                },
                {
                    "node_id": 2088,
                    "node_type": "STORAGE",
                    "service_capacity": 1,
                    "service_duration_seconds": 5,
                },
            ]
        ),
        plan,
    )

    assert result["valid"] is True
    scheduled = {row.task_id: row for row in updated.scheduled_tasks}
    assert scheduled["C1:charge"].start_time_step == 0
    assert scheduled["C2:charge"].start_time_step == 3
    assert scheduled["C2:charge"].end_time_step == 9
    assert scheduled["NEXT:pick"].start_time_step == 13
    charge_rows = [
        row for row in result["reservations"] if row["resource_type"] == "CHARGER_SLOT"
    ]
    assert [(row["start_time_step"], row["end_time_step"]) for row in charge_rows] == [
        (3, 6),
        (6, 9),
    ]


def test_configured_service_duration_extends_short_task() -> None:
    plan = CuOptPlan(
        scheduled_tasks=[
            _task("A:drop", "R1", action="DROP", start=0, end=1),
            _task("B:pick", "R1", start=1, end=3, priority=2),
        ],
        objective_value=0.0,
    )
    updated, result = schedule_shared_resources(
        _problem(
            [
                {
                    "node_id": 2088,
                    "node_type": "STORAGE",
                    "service_capacity": 1,
                    "service_duration_seconds": 10,
                }
            ]
        ),
        plan,
    )

    assert result["valid"] is True
    scheduled = {row.task_id: row for row in updated.scheduled_tasks}
    assert scheduled["A:drop"].end_time_step == 2
    assert scheduled["B:pick"].start_time_step >= 2


def test_idle_waiting_capacity_is_validated_after_routing() -> None:
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[],
        time_step_seconds=5,
        total_distance=0,
        metadata={
            "idle_action_tasks": [
                {
                    "idle_task_id": "idle:R1:wait",
                    "robot_id": "R1",
                    "action": "WAIT_AT_IDLE_NODE",
                    "source_node": 2160,
                    "target_node": 2160,
                    "start_time_step": 0,
                    "end_time_step": 10,
                },
                {
                    "idle_task_id": "idle:R2:wait",
                    "robot_id": "R2",
                    "action": "WAIT_AT_IDLE_NODE",
                    "source_node": 2160,
                    "target_node": 2160,
                    "start_time_step": 5,
                    "end_time_step": 12,
                },
            ]
        },
    )
    invalid = finalize_idle_resource_reservations(
        _problem(
            [
                {
                    "node_id": 2160,
                    "node_type": "CHARGER_WAITING_AREA",
                    "waiting_capacity": 1,
                }
            ]
        ),
        collision,
        {"valid": True, "reservations": [], "warnings": [], "errors": []},
    )
    valid = finalize_idle_resource_reservations(
        _problem(
            [
                {
                    "node_id": 2160,
                    "node_type": "CHARGER_WAITING_AREA",
                    "waiting_capacity": 2,
                }
            ]
        ),
        collision,
        {"valid": True, "reservations": [], "warnings": [], "errors": []},
    )

    assert invalid["valid"] is False
    assert "IDLE_SPACE_CAPACITY_EXCEEDED: node=2160" in invalid["errors"]
    assert valid["valid"] is True
    assert {row["slot_index"] for row in valid["idle_reservations"]} == {1, 2}


def test_compact_response_exposes_resource_reservations() -> None:
    response = {
        "status": "SIMULATION_SUCCESS",
        "data": {
            "valid": True,
            "task_assignments": [],
            "resource_reservation_plan": {
                "status": "PASS",
                "valid": True,
                "reservation_count": 1,
                "adjustment_count": 1,
                "reservations": [
                    {
                        "reservation_id": "RES-1",
                        "resource_type": "SERVICE_NODE",
                        "node_id": 2088,
                        "capacity": 1,
                        "slot_index": 1,
                        "task_id": "A:drop",
                        "robot_id": "R1",
                        "start_time_step": 10,
                        "end_time_step": 11,
                    }
                ],
            },
        },
        "verification_decision": {
            "decision": "PASS",
            "requires_replan": False,
            "replan_scope": "NO_REPLAN",
        },
    }

    compact = compact_planning_response(response)

    assert compact["response_schema_version"] == "p16.5.12.1"
    resources = compact["result"]["resources"]
    assert resources["valid"] is True
    assert resources["reservation_count"] == 1
    assert resources["reservations"][0]["node_id"] == 2088


def test_daily_plan_resource_schedule_routes_without_shared_node_overlap() -> None:
    from app.services.opportunity_charging import augment_plan_with_opportunity_charging
    from app.services.routing import PrioritizedTimeExpandedPlanner
    from tests.test_p16_5_6_idle_holding_routing import _daily_multi_robot_plan
    from tests.test_p16_5_8_opportunity_charging import _p16_5_8_problem

    problem = _p16_5_8_problem()
    for node in problem["nodes"]:
        node_type = str(node.get("node_type") or "").upper()
        if node_type in {"STORAGE", "INBOUND", "OUTBOUND"}:
            node["service_capacity"] = 1
            node["service_duration_seconds"] = 5
        elif node_type == "CHARGER":
            node["charger_capacity"] = 1
        elif node_type == "CHARGER_WAITING_AREA":
            node["waiting_capacity"] = 1

    charging_plan, _ = augment_plan_with_opportunity_charging(
        problem,
        _daily_multi_robot_plan(),
    )
    scheduled, resources = schedule_shared_resources(problem, charging_plan)
    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(scheduled)
    finalized = finalize_idle_resource_reservations(
        problem,
        collision,
        resources,
    )

    assert resources["valid"] is True
    assert any(
        row["node_id"] == 2139
        and row["reason"] == "SHARED_RESOURCE_CAPACITY"
        for row in resources["adjustments"]
    )
    assert len(collision.routes) == 3
    assert collision.total_distance > 0
    assert finalized["valid"] is True
    assert finalized["reservation_count"] >= resources["reservation_count"]


def test_collision_node_returns_auditable_resource_plan() -> None:
    from app.planning.nodes import collision_avoidance_node
    from tests.test_p16_5_6_idle_holding_routing import _daily_multi_robot_plan
    from tests.test_p16_5_8_opportunity_charging import _p16_5_8_problem

    problem = _p16_5_8_problem()
    for node in problem["nodes"]:
        node_type = str(node.get("node_type") or "").upper()
        if node_type in {"STORAGE", "INBOUND", "OUTBOUND"}:
            node["service_capacity"] = 1
            node["service_duration_seconds"] = 5
        elif node_type == "CHARGER":
            node["charger_capacity"] = 1
        elif node_type == "CHARGER_WAITING_AREA":
            node["waiting_capacity"] = 1

    result = collision_avoidance_node(
        {
            "command": {"warehouse_id": 2},
            "cuopt_plan": _daily_multi_robot_plan().model_dump(mode="json"),
            "optimization_problem": problem,
            "schedule_validation": {
                "valid": True,
                "errors": [],
                "dependency_count": 0,
                "dependency_order": [],
            },
            "required_tasks": [],
            "inventory_operations": [],
            "current_plan_version": "TEST-P16-5-9",
            "interpretation": {"task_dependencies": []},
        }
    )

    assert result["final_status"] == "ROUTES_READY"
    assert result["errors"] == []
    resources = result["resource_reservation_plan"]
    assert resources["valid"] is True
    assert resources["reservation_count"] > 0
    assert resources["adjustment_count"] >= 1
    assert result["schedule_validation"]["resource_capacity_valid"] is True
    assert result["collision_plan"]["total_distance"] > 0

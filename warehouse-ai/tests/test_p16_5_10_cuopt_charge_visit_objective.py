from __future__ import annotations

from datetime import timedelta

from app.models import CollisionFreePlan, CuOptPlan, ScheduledTask, TimedRoute, TimedWaypoint
from app.services.charge_visit_optimization import (
    CHARGE_VISIT_OPTIMIZER_VERSION,
    prepare_charge_visit_optimization_problem,
)
from app.services.cuopt_rest import build_cuopt_routing_payload
from app.services.local_optimizer import LocalOptimizer
from app.services.operational_objective import calculate_operational_objective
from app.services.response_view import compact_planning_response
from app.time_utils import planning_reference_time
from tests.test_p16_5_6_idle_holding_routing import (
    _daily_multi_robot_plan,
    _warehouse_two_problem,
)
from tests.test_p16_5_8_opportunity_charging import _p16_5_8_problem


def _business_problem_from_baseline() -> tuple[dict, CuOptPlan]:
    problem = _p16_5_8_problem()
    baseline = _daily_multi_robot_plan()
    reference = planning_reference_time(problem)
    tasks = []
    for scheduled in baseline.scheduled_tasks:
        if scheduled.action == "CHARGE":
            continue
        tasks.append(
            {
                "task_id": scheduled.task_id,
                "work_id": scheduled.work_id,
                "action": scheduled.action,
                "quantity": 0,
                "source_candidates": [scheduled.source_node],
                "target_candidates": [scheduled.target_node],
                "priority": scheduled.priority,
                "predecessors": [],
                "earliest_start": (
                    reference
                    + timedelta(seconds=scheduled.start_time_step * 5)
                ).isoformat(),
                "latest_finish": (
                    reference
                    + timedelta(seconds=scheduled.end_time_step * 5)
                ).isoformat(),
                "time_constraint_type": "HARD_WINDOW",
                "assigned_robot_id": scheduled.robot_id,
            }
        )
    problem["tasks"] = tasks
    return problem, baseline


def test_opportunity_visits_become_explicit_optimizer_tasks() -> None:
    problem, baseline = _business_problem_from_baseline()

    enriched, contract = prepare_charge_visit_optimization_problem(problem, baseline)

    assert contract["version"] == CHARGE_VISIT_OPTIMIZER_VERSION
    assert contract["explicit_charge_task_count"] == 3
    assert enriched["cuopt_charge_visits_preoptimized"] is True
    assert enriched["opportunity_charging_enabled"] is False
    explicit_ids = set(contract["explicit_charge_task_ids"])
    task_rows = {row["task_id"]: row for row in enriched["tasks"]}
    assert explicit_ids.issubset(task_rows)
    assert all(task_rows[task_id]["action"] == "CHARGE" for task_id in explicit_ids)
    assert all(task_rows[task_id]["assigned_robot_id"] for task_id in explicit_ids)
    assert all(
        task_rows[task_id]["source_candidates"]
        == task_rows[task_id]["target_candidates"]
        for task_id in explicit_ids
    )


def test_cuopt_payload_contains_charge_service_times_and_composite_cost() -> None:
    problem, baseline = _business_problem_from_baseline()
    enriched, contract = prepare_charge_visit_optimization_problem(problem, baseline)

    payload, context = build_cuopt_routing_payload(
        enriched,
        solver_time_limit_seconds=10,
    )

    task_ids = context["task_ids"]
    services = payload["task_data"]["service_times"]
    for task_id in contract["explicit_charge_task_ids"]:
        index = task_ids.index(task_id)
        expected = contract["charge_task_specs"][task_id]["charge_duration_seconds"]
        assert services[index] == expected
    objective = context["cuopt_objective_contract"]
    assert objective["matrix_mode"] == (
        "DISTANCE_ENERGY_CONGESTION_CHARGER_VISIT_COMPOSITE"
    )
    assert objective["charge_service_times_included"] is True
    assert set(context["explicit_charge_task_ids"]) == set(
        contract["explicit_charge_task_ids"]
    )


def test_second_pass_binds_business_tasks_and_omits_mixed_pdp_payload() -> None:
    problem, baseline = _business_problem_from_baseline()
    baseline_robot = {
        task.task_id: task.robot_id
        for task in baseline.scheduled_tasks
        if task.action in {"PICK", "DROP", "MOVE"}
    }
    # Reproduce the live daily-plan input: business rows are not explicitly
    # robot-bound before the first pass and PICK/DROP predecessors are present.
    pickup_by_work = {}
    for row in problem["tasks"]:
        row.pop("assigned_robot_id", None)
        if row["action"] == "PICK":
            pickup_by_work[row["work_id"]] = row["task_id"]
    for row in problem["tasks"]:
        if row["action"] == "DROP":
            row["predecessors"] = [pickup_by_work[row["work_id"]]]
    problem["allow_local_robot_rebalance"] = True

    enriched, contract = prepare_charge_visit_optimization_problem(problem, baseline)
    rows = {row["task_id"]: row for row in enriched["tasks"]}

    assert contract["managed_cuopt_pairing_mode"] == (
        "ROBOT_BOUND_TASKS_WITHOUT_PDP"
    )
    assert enriched["cuopt_disable_pickup_delivery_pairs"] is True
    for task_id, robot_id in baseline_robot.items():
        assert rows[task_id]["assigned_robot_id"] == robot_id

    payload, context = build_cuopt_routing_payload(
        enriched,
        solver_time_limit_seconds=10,
    )
    assert "pickup_and_delivery_pairs" not in payload["task_data"]
    assert context["cuopt_objective_contract"][
        "pickup_delivery_pairs_enabled"
    ] is False
    assert context["cuopt_objective_contract"][
        "pickup_delivery_pairs_omitted_reason"
    ] == "STANDALONE_CHARGE_MOVE_TASKS_REQUIRE_ROBOT_BOUND_SECOND_PASS"
    assert len(payload["task_data"]["order_vehicle_match"]) == len(
        enriched["tasks"]
    )

    optimizer = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
        battery_safety_margin_percent=0.5,
    )
    plan = optimizer.optimize(enriched)
    actual = {
        task.task_id: task.robot_id
        for task in plan.scheduled_tasks
        if task.task_id in baseline_robot
    }
    assert actual == baseline_robot


def test_local_normalizer_executes_explicit_charge_without_reselection() -> None:
    problem, baseline = _business_problem_from_baseline()
    enriched, contract = prepare_charge_visit_optimization_problem(problem, baseline)
    optimizer = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
        battery_safety_margin_percent=0.5,
    )

    plan = optimizer.optimize(enriched)

    assert plan.unassigned_task_ids == []
    explicit_ids = set(contract["explicit_charge_task_ids"])
    charges = {task.task_id: task for task in plan.scheduled_tasks if task.action == "CHARGE"}
    assert explicit_ids == set(charges)
    for task_id, task in charges.items():
        spec = contract["charge_task_specs"][task_id]
        assert task.robot_id == spec["robot_id"]
        assert task.target_node == spec["charger_node_id"]
        assert task.charge_duration_seconds == spec["charge_duration_seconds"]
        assert task.charged_percent == spec["charged_percent"]
    assert plan.metadata["explicit_charge_task_ids"] == sorted(explicit_ids)
    assert all(
        row.get("source") == "CUOPT_EXPLICIT_CHARGE_VISIT"
        for row in plan.metadata["charger_selections"]
    )



def test_optimizer_node_runs_bounded_two_pass_without_unassigned(monkeypatch) -> None:
    from app.config import Settings
    from app.planning import nodes
    from app.services.command_language import parse_deterministic_command

    problem, _ = _business_problem_from_baseline()
    interpretation = parse_deterministic_command(
        "오늘 전체 작업을 계획하고 일이 없으면 충전소로 복귀해서 필요한 만큼 충전해줘.",
        warehouse_timezone="Asia/Seoul",
    )
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: Settings(optimizer_backend="local", cuopt_auto_enable=False),
    )
    result = nodes.optimizer_node(
        {
            "optimization_problem": problem,
            "interpretation": interpretation.model_dump(mode="json"),
            "snapshot": {"sql": {"works": []}},
            "required_tasks": problem["tasks"],
        }
    )

    assert result["final_status"] == "OPTIMIZATION_READY"
    assert result["cuopt_plan"]["unassigned_task_ids"] == []
    contract = result["cuopt_plan"]["metadata"][
        "charge_visit_optimization_contract"
    ]
    assert contract["explicit_charge_task_count"] > 0
    assert contract["explicit_relocation_task_count"] > 0
    assert result["optimizer_execution"]["charge_visit_two_pass"]["enabled"] is True


def test_two_pass_plan_routes_with_resources_and_operational_objective(monkeypatch) -> None:
    from datetime import datetime

    from app.config import Settings
    from app.planning import nodes
    from app.services.command_language import parse_deterministic_command

    problem, _ = _business_problem_from_baseline()
    # This fixture models already-optimized exact end times. Give the shared
    # resource scheduler realistic operational slack so the test focuses on
    # the two-pass routing contract rather than an intentionally infeasible
    # zero-slack capacity collision.
    for row in problem["tasks"]:
        if row.get("latest_finish"):
            parsed = datetime.fromisoformat(str(row["latest_finish"]))
            row["latest_finish"] = (parsed + timedelta(hours=1)).isoformat()
    interpretation = parse_deterministic_command(
        "오늘 전체 작업을 계획하고 일이 없으면 충전소로 복귀해서 필요한 만큼 충전해줘.",
        warehouse_timezone="Asia/Seoul",
    )
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: Settings(
            optimizer_backend="local",
            cuopt_auto_enable=False,
            report_with_llm=False,
        ),
    )
    state = {
        "optimization_problem": problem,
        "interpretation": interpretation.model_dump(mode="json"),
        "snapshot": {"sql": {"works": []}},
        "required_tasks": problem["tasks"],
        "command": {"warehouse_id": 2},
        "schedule_validation": {"valid": True},
        "errors": [],
        "warnings": [],
    }
    state.update(nodes.optimizer_node(state))

    result = nodes.collision_avoidance_node(state)

    assert result["final_status"] == "ROUTES_READY"
    assert result["errors"] == []
    assert result["resource_reservation_plan"]["valid"] is True
    assert result["operational_objective"]["status"] == "PASS"
    assert result["operational_objective"]["metrics"]["charger_visit_count"] > 0

def test_operational_objective_includes_new_cost_components() -> None:
    problem = _warehouse_two_problem()
    problem["weights"] = {
        "total_distance": 1.0,
        "makespan": 1.0,
        "tardiness": 5.0,
        "energy": 1.0,
        "robot_activation": 0.5,
        "plan_change": 2.0,
        "charging_time": 0.2,
        "charger_wait": 0.5,
        "charger_visit": 1.0,
        "congestion": 1.0,
        "shared_resource_occupancy": 0.05,
        "unnecessary_charger_roundtrip": 1.0,
    }
    problem["congestion_node_ids"] = [2013]
    charge = ScheduledTask(
        task_id="charge:1",
        action="CHARGE",
        robot_id="R1",
        source_node=2150,
        target_node=2150,
        start_time_step=0,
        end_time_step=4,
        charge_duration_seconds=15,
        charged_percent=2.0,
    )
    plan = CuOptPlan(
        scheduled_tasks=[charge],
        objective_value=0.0,
        metadata={
            "energy": 1.5,
            "tardiness_time_steps": 0,
            "plan_changes": 0,
            "opportunity_charging": {"added_distance": 3.0},
        },
    )
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=["charge:1"],
                waypoints=[
                    TimedWaypoint(node_id=2013, time_step=0),
                    TimedWaypoint(node_id=2150, time_step=1),
                ],
                distance=5.0,
            )
        ],
        time_step_seconds=5,
        total_distance=5.0,
    )
    resources = {
        "reservations": [
            {
                "resource_type": "IDLE_SPACE",
                "node_type": "CHARGER_WAITING_AREA",
                "start_time_step": 4,
                "end_time_step": 10,
            },
            {
                "resource_type": "CHARGER_SLOT",
                "node_type": "CHARGER",
                "start_time_step": 1,
                "end_time_step": 4,
            },
        ]
    }

    result = calculate_operational_objective(problem, plan, collision, resources)

    assert result["status"] == "PASS"
    assert result["hard_constraint_policy"] == (
        "INFEASIBLE_CANDIDATES_REMOVED_NOT_PENALIZED"
    )
    assert result["metrics"]["charger_visit_count"] == 1
    assert result["metrics"]["charger_wait_time_steps"] == 6
    assert result["metrics"]["congestion_node_visit_count"] == 1
    assert result["components"]["charging_time_component"] > 0
    assert result["components"]["shared_resource_occupancy_component"] > 0
    assert result["total"] == round(sum(result["components"].values()), 6)



def test_preoptimized_plan_is_not_augmented_twice() -> None:
    from app.services.opportunity_charging import augment_plan_with_opportunity_charging

    problem, baseline = _business_problem_from_baseline()
    enriched, contract = prepare_charge_visit_optimization_problem(problem, baseline)
    optimizer = LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
        battery_safety_margin_percent=0.5,
    )
    optimized = optimizer.optimize(enriched)

    augmented, evidence = augment_plan_with_opportunity_charging(enriched, optimized)

    assert evidence["preoptimized"] is True
    assert evidence["inserted_charge_task_count"] == contract["explicit_charge_task_count"]
    assert [task.task_id for task in augmented.scheduled_tasks] == [
        task.task_id for task in optimized.scheduled_tasks
    ]

def test_compact_response_exposes_p16_5_10_objective_and_roles() -> None:
    response = {
        "status": "SIMULATION_SUCCESS",
        "data": {
            "valid": True,
            "task_assignments": [],
            "operational_objective": {
                "version": "p16.5.12.1",
                "status": "PASS",
                "total": 123.0,
                "metrics": {"charger_visit_count": 2},
                "components": {"charger_visit_component": 2.0},
                "weights": {"charger_visit": 1.0},
                "role_contract": {"cuopt": ["EXPLICIT_CHARGE_VISITS"]},
            },
        },
        "optimization_plan": {
            "metadata": {
                "charge_visit_optimization_contract": {
                    "version": "p16.5.12.1",
                    "explicit_charge_task_count": 2,
                }
            }
        },
        "verification_decision": {
            "decision": "PASS",
            "requires_replan": False,
            "replan_scope": "NO_REPLAN",
        },
    }

    compact = compact_planning_response(response)

    assert compact["response_schema_version"] == "p16.5.12.1"
    assert compact["result"]["objective"]["total"] == 123.0
    assert compact["result"]["optimizer_roles"]["explicit_charge_task_count"] == 2

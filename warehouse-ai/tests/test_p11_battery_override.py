from types import SimpleNamespace

from app.models import AtomicTask, ScopeDecision
from app.planning import nodes
from app.services.command_language import parse_deterministic_command
from app.services.local_optimizer import LocalOptimizer
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.robot_adapter import RobotAdapter
from app.services.simulation import simulate_plan
from app.services.user_reporting import build_user_report_summary


P11_COMMAND = (
    "R2-03의 배터리가 현재 21%라고 가정하고 E상품 30 BOX를 "
    "R2-03에 고정 배정해. 최소 배터리를 유지하지 못하면 active "
    "CHARGER 노드 중 비용이 가장 낮은 충전소에서 필요한 만큼 "
    "충전한 뒤 출고 노드 2146으로 이동해. 실제 Redis 배터리는 "
    "변경하지 말고 시뮬레이션만 해."
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        time_step_seconds=1,
        min_robot_battery=20.0,
        energy_per_distance=0.1,
        charge_target_battery=80.0,
        charge_rate_percent_per_minute=60.0,
    )


def _state() -> tuple[dict, dict]:
    interpretation = parse_deterministic_command(P11_COMMAND)
    operation_id = interpretation.inventory_operations[0].operation_id
    snapshot = {
        "captured_at": "2026-07-24T01:00:00+00:00",
        "sql": {
            "robots": [
                {
                    "robot_id": "R2-03",
                    "node_id": 1,
                    "battery": 90,
                    "status": "IDLE",
                    "max_load": 100,
                }
            ],
            "inventory": [],
        },
        "redis": {
            "robots": [
                {
                    "robot_id": "R2-03",
                    "node_id": 1,
                    "battery": 90,
                    "last_event": "IDLE",
                }
            ],
            "temporary_closures": [],
            "active_plan": None,
        },
        "graph": {
            "nodes": [
                {"node_id": 1, "node_type": "AISLE", "active": True},
                {
                    "node_id": 2,
                    "node_type": "CHARGER",
                    "active": True,
                    "charging_cost": 1,
                },
                {"node_id": 3, "node_type": "AISLE", "active": True},
                {"node_id": 2146, "node_type": "OUTBOUND", "active": True},
            ],
            "edges": [
                {
                    "from_node": 1,
                    "to_node": 2,
                    "distance": 1,
                    "travel_seconds": 1,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 2,
                    "to_node": 3,
                    "distance": 4,
                    "travel_seconds": 4,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 1,
                    "to_node": 3,
                    "distance": 10,
                    "travel_seconds": 10,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 3,
                    "to_node": 2146,
                    "distance": 6,
                    "travel_seconds": 6,
                    "direction": "BOTH",
                    "active": True,
                },
            ],
        },
    }
    state = {
        "command": {"warehouse_id": 1, "text": P11_COMMAND},
        "interpretation": interpretation.model_dump(mode="json"),
        "scope": ScopeDecision(
            plan_mode="INSERT_TASK",
            optimization_goal="P11 battery scenario",
            reason_summary="test",
        ).model_dump(mode="json"),
        "snapshot": snapshot,
        "required_tasks": [
            AtomicTask(
                task_id=f"{operation_id}:drop",
                work_id=operation_id,
                action="DROP",
                item_id="E",
                quantity=30,
                source_candidates=[3],
                target_candidates=[2146],
            ).model_dump(mode="json")
        ],
    }
    return state, snapshot


def test_p11_override_charge_and_verification(monkeypatch) -> None:
    state, snapshot = _state()
    monkeypatch.setattr(nodes, "get_settings", _settings)

    update = nodes.build_optimization_problem_node(state)
    problem = update["optimization_problem"]

    assert snapshot["redis"]["robots"][0]["battery"] == 90
    assert problem["robots"][0]["battery"] == 21
    assert problem["robot_state_overrides"] == [
        {
            "robot_id": "R2-03",
            "battery_percent": 21.0,
            "source": "COMMAND_HYPOTHETICAL_OVERRIDE",
        }
    ]

    optimizer = LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20,
        energy_per_distance=0.1,
        charge_target_battery=80,
        charge_rate_percent_per_minute=60,
    )
    plan = optimizer.optimize(problem)
    charge = next(task for task in plan.scheduled_tasks if task.action == "CHARGE")
    assert charge.target_node == 2
    assert charge.charge_target_battery == 80
    assert charge.charged_percent > 0
    assert charge.charger_selection_policy == "MIN_SAFE_CONFIGURED_CHARGER_COST"

    routed = PrioritizedTimeExpandedPlanner(problem, 1, 5000).solve(plan)
    simulation = simulate_plan(routed, plan, problem).model_dump(mode="json")
    battery = simulation["metrics"]["battery_by_robot"]["R2-03"]
    assert battery["initial_battery"] == 21
    assert battery["final_battery"] >= 20
    assert battery["charge_task_ids"] == [charge.task_id]
    assert battery["charge_duration_seconds"] == charge.charge_duration_seconds

    command_plan = {
        "warehouse_id": 1,
        "charger_node_ids": [2],
        "required_tasks": state["required_tasks"],
        "inventory_operations": state["interpretation"]["inventory_operations"],
        "cuopt_plan": plan.model_dump(mode="json"),
        "collision_plan": routed.model_dump(mode="json"),
    }
    batches, command_validation = RobotAdapter(time_step_seconds=1).adapt(
        "P11-PLAN", command_plan
    )
    assert command_validation["valid"] is True
    charge_commands = [
        command
        for batch in batches
        for command in batch.commands
        if command.action == "CHARGE"
    ]
    assert len(charge_commands) == 1
    assert charge_commands[0].node_id == 2
    assert charge_commands[0].payload["charged_percent"] == charge.charged_percent
    assert charge_commands[0].payload["duration_seconds"] == charge.charge_duration_seconds
    assert charge_commands[0].payload["selection_policy"] == (
        "MIN_SAFE_CONFIGURED_CHARGER_COST"
    )
    assert snapshot["redis"]["robots"][0]["battery"] == 90

    verification_state = {
        **state,
        **update,
        "cuopt_plan": plan.model_dump(mode="json"),
        "collision_plan": routed.model_dump(mode="json"),
        "simulation": simulation,
        "supervisor_decision": {
            "requires_clarification": False,
            "allow_replan": True,
        },
        "validation": {"errors": [], "warnings": []},
        "errors": [],
        "warnings": [],
    }
    evidence = nodes.build_verification_evidence(verification_state)
    assert [row for row in evidence if row["severity"] == "BLOCKING"] == []


def test_p11_verification_blocks_missing_charge_false_pass() -> None:
    interpretation = parse_deterministic_command(P11_COMMAND).model_dump(mode="json")
    state = {
        "interpretation": interpretation,
        "supervisor_decision": {
            "requires_clarification": False,
            "allow_replan": True,
        },
        "validation": {"errors": [], "warnings": []},
        "errors": [],
        "warnings": [],
        "optimization_problem": {
            "robots": [{"robot_id": "R2-03", "battery": 21}],
            "nodes": [{"node_id": 2, "node_type": "CHARGER", "active": True}],
            "min_robot_battery": 20,
            "time_step_seconds": 1,
            "charge_rate_percent_per_minute": 60,
        },
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "E:drop",
                    "action": "DROP",
                    "robot_id": "R2-03",
                    "target_node": 2146,
                }
            ],
            "unassigned_task_ids": [],
        },
        "simulation": {
            "valid": True,
            "issues": [],
            "errors": [],
            "warnings": [],
            "metrics": {
                "battery_by_robot": {
                    "R2-03": {
                        "initial_battery": 21,
                        "estimated_consumption": 1.482,
                        "charged_percent": 0,
                        "final_battery": 19.518,
                        "charge_task_ids": [],
                        "charger_node_ids": [],
                    }
                }
            },
        },
    }

    evidence = nodes.build_verification_evidence(state)
    blocking_codes = {
        row["code"] for row in evidence if row["severity"] == "BLOCKING"
    }
    assert "MISSING_REQUIRED_CHARGE" in blocking_codes
    assert "BATTERY_BELOW_MINIMUM" in blocking_codes
    decision = nodes.deterministic_verification_decision(state, evidence)
    assert decision.decision == "REPLAN_LOCAL"
    assert decision.requires_replan is True
    assert decision.replan_scope == "LOCAL_REPLAN"


def test_p11_command_scope_does_not_mix_existing_f_work() -> None:
    interpretation = parse_deterministic_command(
        "E상품 30 BOX를 출고해 시뮬레이션만 해"
    )
    operations = nodes._inventory_operations_from_snapshot(
        interpretation,
        {
            "works": [
                {
                    "work_id": "DEMO-W-OUT-2-F",
                    "operation_type": "OUTBOUND",
                    "item_id": "F",
                    "quantity_boxes": 50,
                    "priority": 50,
                }
            ]
        },
    )

    assert [(row.item_id, row.source) for row in operations] == [("E", "COMMAND")]


def test_normal_insert_report_is_not_labelled_urgent() -> None:
    state = {
        "interpretation": {
            "command_kind": "PLAN",
            "execution_mode": "SIMULATE_ONLY",
            "insertion_policy": "NORMAL",
            "priority": "NORMAL",
            "inventory_operations": [],
        },
        "scope": {"plan_mode": "INSERT_TASK"},
        "verification_decision": {
            "decision": "PASS",
            "user_visible_warnings": [],
        },
    }
    data = {
        "valid": True,
        "execution_mode": "SIMULATE_ONLY",
        "plan_mode": "INSERT_TASK",
        "warnings": [],
        "errors": [],
        "task_assignments": [],
        "insertion_result": {
            "inserted_task_ids": ["W1"],
            "preserved_task_ids": [],
            "shifted_task_ids": [],
            "blocked_task_ids": [],
        },
        "conflict_count": 0,
    }

    summary = build_user_report_summary(state, data, report_level="STANDARD")
    assert summary.plan_mode_label == "작업 추가"
    assert "긴급" not in summary.title
    assert "긴급" not in summary.primary_message

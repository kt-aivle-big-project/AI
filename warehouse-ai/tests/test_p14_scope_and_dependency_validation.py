from types import SimpleNamespace

from app.models import (
    CommandInterpretation,
    CuOptPlan,
    InventoryOperationRequest,
    NaturalLanguageCommand,
    ScheduledTask,
    SupervisorDecision,
)
from app.planning import nodes


def _settings(max_replan_count: int = 3):
    return SimpleNamespace(max_replan_count=max_replan_count)


def _hypothetical_interpretation(*, robot_ids: list[str]) -> CommandInterpretation:
    return CommandInterpretation(
        command_kind="PLAN",
        intent="HYPOTHETICAL_SCENARIO",
        objective="배터리 가정 시뮬레이션",
        execution_mode="SIMULATE_ONLY",
        target_robot_ids=robot_ids,
        extracted_robot_ids=robot_ids,
        verified_robot_ids=robot_ids,
        inventory_operations=[
            InventoryOperationRequest(
                operation_id="OP-1",
                operation_type="OUTBOUND",
                item_id="E",
                quantity_boxes=30,
            )
        ],
        summary="가상 운영 시나리오",
    )


def _raw_supervisor(plan_mode: str, *, allow_replan: bool = False) -> SupervisorDecision:
    return SupervisorDecision(
        intent="HYPOTHETICAL_SCENARIO",
        command_kind="PLAN",
        execution_mode="SIMULATE_ONLY",
        required_tools=["SNAPSHOT"],
        plan_mode=plan_mode,
        requires_clarification=False,
        risk_level="MEDIUM",
        allow_replan=allow_replan,
        max_replan_attempts=0,
        reasoning_summary="LLM 임시 판단",
    )


def test_single_robot_hypothetical_scope_is_deterministically_local(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings())
    interpretation = _hypothetical_interpretation(robot_ids=["R2-03"])
    command = NaturalLanguageCommand(
        command_id="P14-C1",
        warehouse_id=2,
        text="R2-03 배터리를 가정해 시뮬레이션해",
        requested_execution_mode="SIMULATE_ONLY",
    )

    normalized = nodes.normalize_supervisor_decision(
        _raw_supervisor("GLOBAL_REPLAN", allow_replan=False),
        command,
        interpretation,
    )

    assert normalized.plan_mode == "LOCAL_REPLAN"
    assert normalized.allow_replan is True
    assert normalized.max_replan_attempts == 2
    assert normalized.required_tools == [
        "SNAPSHOT",
        "OPTIMIZER",
        "ROUTING",
        "SIMULATION",
        "VERIFICATION",
    ]


def test_multi_robot_hypothetical_scope_is_deterministically_global(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "get_settings", lambda: _settings())
    interpretation = _hypothetical_interpretation(robot_ids=["R2-01", "R2-03"])
    command = NaturalLanguageCommand(
        command_id="P14-C2",
        warehouse_id=2,
        text="두 로봇을 가정해 전체 계획을 비교해",
        requested_execution_mode="SIMULATE_ONLY",
    )

    normalized = nodes.normalize_supervisor_decision(
        _raw_supervisor("LOCAL_REPLAN", allow_replan=True),
        command,
        interpretation,
    )

    assert normalized.plan_mode == "GLOBAL_REPLAN"


def _plan(*, pick_start: int = 6) -> CuOptPlan:
    charge_id = "W1:1:pick:charge:2151"
    pick_id = "W1:1:pick"
    drop_id = "W1:1:drop"
    return CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id=charge_id,
                work_id="W1",
                action="CHARGE",
                robot_id="R2-03",
                source_node=2152,
                target_node=2151,
                start_time_step=0,
                end_time_step=6,
            ),
            ScheduledTask(
                task_id=pick_id,
                work_id="W1",
                action="PICK",
                robot_id="R2-03",
                source_node=2088,
                target_node=2088,
                start_time_step=pick_start,
                end_time_step=17,
            ),
            ScheduledTask(
                task_id=drop_id,
                work_id="W1",
                action="DROP",
                robot_id="R2-03",
                source_node=2088,
                target_node=2146,
                start_time_step=17,
                end_time_step=30,
            ),
        ],
        objective_value=1.0,
        metadata={
            "execution_task_dependencies": [
                {
                    "predecessor_task_id": charge_id,
                    "successor_task_id": pick_id,
                    "dependency_type": "FINISH_TO_START",
                    "lag_seconds": 0,
                    "source": "AUTO_CHARGING",
                },
                {
                    "predecessor_task_id": pick_id,
                    "successor_task_id": drop_id,
                    "dependency_type": "FINISH_TO_START",
                    "lag_seconds": 0,
                    "source": "PLANNER_PREDECESSOR",
                },
            ]
        },
    )


def test_execution_dependencies_are_validated_after_routing() -> None:
    result = nodes.validate_execution_task_dependencies(
        _plan(),
        time_step_seconds=5,
    )

    assert result["valid"] is True
    assert result["dependency_count"] == 2
    assert result["dependency_order"] == [
        "W1:1:pick:charge:2151",
        "W1:1:pick",
        "W1:1:drop",
    ]
    assert result["violations"] == []


def test_execution_dependency_order_violation_is_blocking_evidence() -> None:
    plan = _plan(pick_start=5)
    result = nodes.validate_execution_task_dependencies(
        plan,
        time_step_seconds=5,
    )
    assert result["valid"] is False
    assert result["violations"][0]["code"] == "EXECUTION_DEPENDENCY_ORDER_VIOLATION"

    state = {
        "schedule_validation": {
            "valid": False,
            "execution_dependency_violations": result["violations"],
        },
        "simulation": {
            "success": True,
            "valid": True,
            "status": "SUCCESS",
            "issues": [],
            "errors": [],
            "warnings": [],
            "metrics": {},
        },
        "cuopt_plan": plan.model_dump(mode="json"),
        "interpretation": {},
        "optimization_problem": {},
        "errors": [],
        "warnings": [],
    }
    evidence = nodes.build_verification_evidence(state)
    violation = next(
        row
        for row in evidence
        if row["code"] == "EXECUTION_DEPENDENCY_ORDER_VIOLATION"
    )
    assert violation["severity"] == "BLOCKING"
    assert set(violation["task_ids"]) == {
        "W1:1:pick:charge:2151",
        "W1:1:pick",
    }

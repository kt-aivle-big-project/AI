from __future__ import annotations

from app.models import CuOptPlan, ScheduledTask
from app.services.mapf_replan import (
    MAPF_REPLAN_VERSION,
    build_mapf_replan_policy,
    classify_mapf_failure,
    order_robot_ids,
    start_delay_steps,
)
from app.services.routing import PrioritizedTimeExpandedPlanner
from tests.test_p16_5_8_opportunity_charging import _p16_5_8_problem


def _plan() -> dict:
    return {
        "scheduled_tasks": [
            {
                "task_id": "work-a:1:pick",
                "work_id": "work-a",
                "action": "PICK",
                "robot_id": "R2-02",
                "source_node": 2088,
                "target_node": 2088,
                "start_time_step": 100,
                "end_time_step": 101,
            },
            {
                "task_id": "work-b:1:pick",
                "work_id": "work-b",
                "action": "PICK",
                "robot_id": "R2-03",
                "source_node": 2139,
                "target_node": 2139,
                "start_time_step": 100,
                "end_time_step": 101,
            },
        ]
    }


def _problem() -> dict:
    return {
        "robots": [
            {"robot_id": "R2-01"},
            {"robot_id": "R2-02"},
            {"robot_id": "R2-03"},
        ]
    }


def test_local_reservation_failure_extracts_task_and_robot() -> None:
    result = classify_mapf_failure(
        "R2-02 작업 work-a:1:pick 처리 시간 예약 충돌",
        error_code="ROUTE_FAILED",
        routing_backend="internal",
        problem=_problem(),
        cuopt_plan=_plan(),
    )

    assert result["version"] == MAPF_REPLAN_VERSION
    assert result["code"] == "MAPF_LOCAL_CONFLICT"
    assert result["retryable"] is True
    assert result["recommended_scope"] == "LOCAL_REPLAN"
    assert result["affected_robot_ids"] == ["R2-02"]
    assert result["affected_task_ids"] == ["work-a:1:pick"]
    assert len(result["failure_signature"]) == 64


def test_topology_failure_widens_to_global_replan() -> None:
    result = classify_mapf_failure(
        "NO_PATH: node 2088에서 node 2146까지 이동 가능한 통로가 없습니다.",
        error_code="ROUTE_FAILED",
        routing_backend="internal",
        problem=_problem(),
        cuopt_plan=_plan(),
    )

    assert result["code"] == "MAPF_TOPOLOGY_FAILURE"
    assert result["retryable"] is True
    assert result["recommended_scope"] == "GLOBAL_REPLAN"
    assert result["affected_node_ids"] == [2088, 2146]


def test_backend_configuration_failure_is_not_retried() -> None:
    result = classify_mapf_failure(
        "ROUTING_BACKEND=mapf에는 MAPF_URL이 필요합니다.",
        error_code="ROUTE_FAILED",
        routing_backend="mapf",
        problem=_problem(),
        cuopt_plan=_plan(),
    )

    assert result["code"] == "MAPF_CONFIGURATION_FAILURE"
    assert result["retryable"] is False
    assert result["recommended_scope"] == "NO_REPLAN"


def test_local_policy_moves_affected_robot_to_front() -> None:
    policy = build_mapf_replan_policy(
        attempt=1,
        scope="LOCAL_REPLAN",
        affected_robot_ids=["R2-03"],
    )
    ordered = order_robot_ids(["R2-01", "R2-02", "R2-03"], policy)

    assert policy["strategy"] == "AFFECTED_ROBOTS_FIRST"
    assert ordered == ["R2-03", "R2-01", "R2-02"]
    assert start_delay_steps("R2-03", ordered, policy) == 0


def test_global_policy_rotates_and_staggers_robot_activation() -> None:
    policy = build_mapf_replan_policy(
        attempt=2,
        scope="GLOBAL_REPLAN",
        affected_robot_ids=["R2-02"],
        escalated_from_local=True,
    )
    ordered = order_robot_ids(["R2-01", "R2-02", "R2-03"], policy)

    assert policy["strategy"] == "ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER"
    assert ordered == ["R2-03", "R2-01", "R2-02"]
    assert start_delay_steps("R2-03", ordered, policy) == 0
    assert start_delay_steps("R2-01", ordered, policy) == 2
    assert start_delay_steps("R2-02", ordered, policy) == 4


def test_internal_router_exposes_applied_replan_policy() -> None:
    problem = _p16_5_8_problem()
    problem["idle_whitelist_strict"] = False
    problem["mapf_replan_policy"] = build_mapf_replan_policy(
        attempt=1,
        scope="LOCAL_REPLAN",
        affected_robot_ids=["R2-03"],
    )
    tasks = [
        ScheduledTask(
            task_id="r1",
            work_id="w1",
            action="MOVE",
            robot_id="R2-01",
            source_node=2146,
            target_node=2146,
            start_time_step=0,
            end_time_step=1,
            priority=1,
        ),
        ScheduledTask(
            task_id="r3",
            work_id="w3",
            action="MOVE",
            robot_id="R2-03",
            source_node=2152,
            target_node=2152,
            start_time_step=0,
            end_time_step=1,
            priority=1,
        ),
    ]
    plan = CuOptPlan(scheduled_tasks=tasks, objective_value=0, metadata={})

    collision = PrioritizedTimeExpandedPlanner(problem, 5, 720).solve(plan)

    assert collision.metadata["mapf_replan_policy"]["version"] == "p16.5.12.1"
    assert collision.metadata["robot_processing_order"][0] == "R2-03"


def _route_failure_state() -> dict:
    from app.planning.nodes import (
        build_verification_evidence,
        deterministic_verification_decision,
    )

    route_failure = classify_mapf_failure(
        "R2-02 작업 work-a:1:pick 처리 시간 예약 충돌",
        error_code="ROUTE_FAILED",
        routing_backend="internal",
        problem=_problem(),
        cuopt_plan=_plan(),
    )
    state = {
        "route_failure": route_failure,
        "simulation": {
            "success": False,
            "valid": False,
            "status": "FAILED",
            "issues": [
                {
                    "code": "PLAN_COMPONENT_MISSING",
                    "message": "최적화 계획 또는 충돌 방지 경로가 없습니다.",
                    "robot_ids": [],
                    "task_ids": [],
                    "node_ids": [],
                    "time_steps": [],
                }
            ],
            "errors": ["ROUTE_FAILED"],
            "warnings": [],
        },
        "plan_validation": {},
        "errors": [],
        "warnings": [],
        "supervisor_decision": {
            "allow_replan": True,
            "max_replan_attempts": 2,
        },
        "required_tasks": [
            {
                "task_id": "work-a:1:pick",
                "work_id": "work-a",
                "action": "PICK",
                "source_candidates": [2088],
                "target_candidates": [2088],
                "frozen": False,
            },
            {
                "task_id": "work-b:1:pick",
                "work_id": "work-b",
                "action": "PICK",
                "source_candidates": [2139],
                "target_candidates": [2139],
                "frozen": False,
            },
        ],
        "cuopt_plan": _plan(),
        "snapshot": {
            "captured_at": "2026-07-26T00:00:00Z",
            "sql": {
                "robots": [
                    {"robot_id": "R2-01"},
                    {"robot_id": "R2-02"},
                ],
                "works": [],
            },
            "redis": {
                "executing_task_ids": [],
                "active_plan": {},
            },
        },
        "scope": {
            "plan_mode": "INITIAL_PLAN",
            "affected_task_ids": [],
            "affected_robot_ids": [],
            "fixed_task_ids": [],
            "changeable_task_ids": [],
            "freeze_horizon_seconds": 15,
            "include_new_command": True,
            "optimization_goal": "distance",
            "reason_summary": "initial",
        },
        "optimization_problem": {
            "reference_time": "2026-07-26T00:00:00Z",
            "time_step_seconds": 5,
        },
        "replan_attempt": 0,
        "max_replan_attempts": 2,
        "replan_history": [],
        "repeated_failure_signatures": {},
        "verification_decision": {},
        "verification_evidence": [],
    }
    evidence = build_verification_evidence(state)
    decision = deterministic_verification_decision(state, evidence)
    state["verification_evidence"] = evidence
    state["verification_decision"] = decision.model_dump(mode="json")
    return state


def test_route_failure_evidence_replaces_missing_plan_noise() -> None:
    state = _route_failure_state()
    codes = {row["code"] for row in state["verification_evidence"]}

    assert "MAPF_LOCAL_CONFLICT" in codes
    assert "PLAN_COMPONENT_MISSING" not in codes
    assert "DETERMINISTIC_RESULT_MISSING" not in codes
    assert "PIPELINE_ERROR" not in codes
    assert state["verification_decision"]["decision"] == "REPLAN_LOCAL"
    assert state["verification_decision"]["affected_robot_ids"] == ["R2-02"]


def test_prepare_replan_builds_local_mapf_policy() -> None:
    from app.planning.nodes import prepare_replan_node

    update = prepare_replan_node(_route_failure_state())

    assert update["replan_ready"] is True
    assert update["scope"]["plan_mode"] == "LOCAL_REPLAN"
    assert update["scope"]["changeable_task_ids"] == ["work-a:1:pick"]
    assert update["scope"]["fixed_task_ids"] == ["work-b:1:pick"]
    assert update["mapf_replan_policy"]["strategy"] == "AFFECTED_ROBOTS_FIRST"
    assert update["mapf_replan_policy"]["affected_robot_ids"] == ["R2-02"]
    assert update["route_failure"] == {}


def test_repeated_local_mapf_failure_escalates_once_to_global() -> None:
    from app.planning.nodes import (
        prepare_replan_node,
        verification_failure_signature,
    )

    state = _route_failure_state()
    signature = verification_failure_signature(state)
    state["replan_attempt"] = 1
    state["repeated_failure_signatures"] = {signature: 1}

    update = prepare_replan_node(state)

    assert update["replan_ready"] is True
    assert update["scope"]["plan_mode"] == "GLOBAL_REPLAN"
    assert update["scope"]["changeable_task_ids"] == [
        "work-a:1:pick",
        "work-b:1:pick",
    ]
    assert update["mapf_replan_policy"]["strategy"] == (
        "ROTATE_ALL_ROBOTS_WITH_BOUNDED_STAGGER"
    )
    assert update["mapf_replan_policy"]["escalated_from_local"] is True
    assert "전역 재계획" in update["replan_reason"]


def test_collision_node_emits_structured_retryable_failure(monkeypatch) -> None:
    from app.planning import nodes

    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="work-a:1:pick",
                work_id="work-a",
                action="PICK",
                robot_id="R2-02",
                source_node=2088,
                target_node=2088,
                start_time_step=100,
                end_time_step=101,
            )
        ],
        objective_value=0,
        metadata={},
    )
    state = {
        "cuopt_plan": plan.model_dump(mode="json"),
        "optimization_problem": {
            "robots": [{"robot_id": "R2-02", "node_id": 2088}],
            "nodes": [],
            "edges": [],
            "tasks": [],
            "time_step_seconds": 5,
        },
        "command": {"warehouse_id": 2},
    }
    monkeypatch.setattr(
        nodes,
        "augment_plan_with_opportunity_charging",
        lambda problem, optimizer_plan: (optimizer_plan, {}),
    )
    monkeypatch.setattr(
        nodes,
        "schedule_shared_resources",
        lambda problem, optimizer_plan: (
            optimizer_plan,
            {
                "valid": True,
                "reservations": [],
                "warnings": [],
                "errors": [],
                "adjustments": [],
            },
        ),
    )
    monkeypatch.setattr(
        nodes,
        "build_collision_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("R2-02 작업 work-a:1:pick 처리 시간 예약 충돌")
        ),
    )

    update = nodes.collision_avoidance_node(state)

    assert update["final_status"] == "ROUTE_FAILED"
    assert update["route_failure"]["code"] == "MAPF_LOCAL_CONFLICT"
    assert update["route_failure"]["affected_robot_ids"] == ["R2-02"]
    assert update["errors"] == []


def test_compact_response_exposes_mapf_replan_history() -> None:
    from app.services.response_view import compact_planning_response

    response = {
        "status": "SIMULATION_SUCCESS",
        "verification_decision": {
            "decision": "PASS",
            "requires_replan": False,
            "replan_scope": "NO_REPLAN",
        },
        "mapf_replan_policy": build_mapf_replan_policy(
            attempt=1,
            scope="LOCAL_REPLAN",
            affected_robot_ids=["R2-02"],
        ),
        "replan_attempt": 1,
        "replan_history": [
            {
                "attempt": 1,
                "scope": "LOCAL_REPLAN",
                "status": "COMPLETED",
                "verification_before": "REPLAN_LOCAL",
                "verification_after": "PASS",
                "affected_robot_ids": ["R2-02"],
                "affected_task_ids": ["work-a:1:pick"],
                "failure_signature": "a" * 64,
            }
        ],
        "data": {"valid": True},
    }

    compact = compact_planning_response(response)
    mapf = compact["result"]["mapf_replan"]

    assert compact["response_schema_version"] == "p16.5.12.1"
    assert mapf["enabled"] is True
    assert mapf["strategy"] == "AFFECTED_ROBOTS_FIRST"
    assert mapf["history"][0]["status"] == "COMPLETED"


def test_compact_response_keeps_mapf_version_when_replan_is_inactive() -> None:
    from app.services.response_view import compact_planning_response

    compact = compact_planning_response(
        {
            "status": "SIMULATION_SUCCESS",
            "verification_decision": {
                "decision": "PASS",
                "requires_replan": False,
                "replan_scope": "NO_REPLAN",
            },
            "data": {"valid": True},
        }
    )

    assert compact["response_schema_version"] == "p16.5.12.1"
    assert compact["result"]["mapf_replan"] == {
        "version": "p16.5.12.1",
        "enabled": False,
        "attempt": 0,
        "scope": None,
        "strategy": None,
        "affected_robot_ids": [],
        "escalated_from_local": False,
        "last_failure_code": None,
        "last_failure_category": None,
        "retryable": None,
        "history": [],
    }

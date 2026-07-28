from types import SimpleNamespace

from app.models import (
    CollisionFreePlan,
    CommandInterpretation,
    CuOptPlan,
    ScopeDecision,
    ScheduledTask,
    TimedRoute,
    TimedWaypoint,
)
from app.planning import graph as graph_module
from app.planning import nodes


class FakeStructuredModel:
    def __init__(self, value: ScopeDecision):
        self.value = value

    def with_structured_output(self, *_args, **_kwargs):
        return self

    def invoke(self, *_args, **_kwargs):
        return self.value.model_copy(deep=True)


def interpretation(command_kind: str = "PLAN", mode: str = "PLAN_ONLY") -> dict:
    return CommandInterpretation(
        command_kind=command_kind,
        intent="DAILY_PLAN" if command_kind != "QUERY" else "INVENTORY_QUERY",
        objective="test",
        execution_mode=mode,
        summary="test",
    ).model_dump(mode="json")


def scope_decision(plan_mode: str = "LOCAL_REPLAN") -> ScopeDecision:
    return ScopeDecision(
        plan_mode=plan_mode,
        optimization_goal="minimum cost",
        reason_summary="test",
    )


def scope_state(command_kind: str = "PLAN", *, active: bool = False, failed: bool = False) -> dict:
    return {
        "command": {"command_id": "C1", "warehouse_id": 1, "text": "test"},
        "interpretation": interpretation(command_kind),
        "snapshot": {
            "captured_at": "2026-07-16T00:00:00+00:00",
            "sql": {"inventory": [], "robots": [], "works": []},
            "graph": {"nodes": [], "edges": []},
            "redis": {
                "active_plan_version": "P1" if active else None,
                "active_plan": {} if active else None,
                "executing_task_ids": ["T1"] if active else [],
                "planned_task_ids": [],
                "temporary_closures": [],
                "robots": [
                    {"robot_id": "R1", "last_event": "ROBOT_FAILED"}
                ] if failed else [],
                "tasks": [
                    {"task_id": "T1", "robot_id": "R1"}
                ] if failed else [],
            },
            "validation": {"valid": True},
        },
        "replan_count": 0,
    }


def test_query_forces_no_replan_and_graph_skips_optimizer(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: FakeStructuredModel(scope_decision("GLOBAL_REPLAN")))
    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace(freeze_horizon_seconds=15))

    update = nodes.decide_scope_node(scope_state("QUERY"))

    assert update["scope"]["plan_mode"] == "NO_REPLAN"
    assert graph_module.after_route_by_command(
        {
            "validation": {"valid": True},
            "interpretation": {"command_kind": "QUERY"},
            "scope": update["scope"],
        }
    ) == "report"


def test_no_active_plan_forces_initial_plan(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: FakeStructuredModel(scope_decision("LOCAL_REPLAN")))
    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace(freeze_horizon_seconds=15))

    update = nodes.decide_scope_node(scope_state())

    assert update["scope"]["plan_mode"] == "INITIAL_PLAN"


def test_failed_robot_forces_local_replan(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: FakeStructuredModel(scope_decision("INSERT_TASK")))
    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace(freeze_horizon_seconds=15))

    update = nodes.decide_scope_node(scope_state(active=True, failed=True))

    assert update["scope"]["plan_mode"] == "LOCAL_REPLAN"
    assert update["scope"]["affected_robot_ids"] == ["R1"]
    assert "T1" in update["scope"]["changeable_task_ids"]


def simulation_state(mode: str) -> dict:
    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="T1",
                robot_id="R1",
                source_node=1,
                target_node=2,
                start_time_step=0,
                end_time_step=1,
            )
        ],
        objective_value=1,
    )
    routes = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[
            TimedRoute(
                robot_id="R1",
                task_ids=["T1"],
                waypoints=[
                    TimedWaypoint(node_id=1, time_step=0),
                    TimedWaypoint(node_id=2, time_step=1),
                ],
                distance=1,
            )
        ],
        time_step_seconds=1,
        total_distance=1,
    )
    return {
        "interpretation": interpretation("PLAN", mode),
        "cuopt_plan": plan.model_dump(mode="json"),
        "collision_plan": routes.model_dump(mode="json"),
        "optimization_problem": {
            "captured_at": "2026-07-16T00:00:00+00:00",
            "nodes": [{"node_id": 1}, {"node_id": 2}],
            "edges": [{"from_node": 1, "to_node": 2, "distance": 1}],
            "robots": [{"robot_id": "R1", "node_id": 1, "battery": 100}],
            "tasks": [],
            "inventory": [],
            "temporary_closures": [],
            "min_robot_battery": 20,
            "energy_per_distance": 0.05,
        },
    }


def test_simulate_only_uses_simulation_session_only(monkeypatch) -> None:
    simulation_redis = object()
    called = {"count": 0}

    def fake_replay(state, result, redis_repository):
        assert redis_repository is simulation_redis
        called["count"] += 1
        return {
            "simulation_id": "SIM-1",
            "current_state": {
                "inventory": [],
                "robots": [],
                "works": [],
                "checkpoint": "1-0",
            },
            "checkpoint": "1-0",
            "event_count": 3,
        }

    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: SimpleNamespace(redis=simulation_redis),
    )
    monkeypatch.setattr(nodes, "replay_simulation_session", fake_replay)

    update = nodes.simulation_node(simulation_state("SIMULATE_ONLY"))

    assert update["simulation"]["valid"] is True
    assert update["simulation"]["conflict_count"] == 0
    assert update["simulation_id"] == "SIM-1"
    assert called["count"] == 1


def test_execute_without_gateway_is_blocked_before_redis(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(robot_gateway_url="", request_timeout_seconds=1),
    )
    monkeypatch.setattr(
        nodes,
        "get_services",
        lambda: (_ for _ in ()).throw(AssertionError("Redis must not be called")),
    )

    update = nodes.dispatch_plan_node({})

    assert update["final_status"] == "EXECUTION_BLOCKED"
    assert "ROBOT_GATEWAY_URL" in update["errors"][0]


def test_plan_only_stops_after_validation_and_simulate_mode_continues() -> None:
    assert graph_module.after_routes(
        {"interpretation": {"execution_mode": "PLAN_ONLY"}}
    ) == "validate_plan"
    assert graph_module.after_routes(
        {"interpretation": {"execution_mode": "SIMULATE_ONLY"}}
    ) == "simulate"

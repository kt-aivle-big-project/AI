from copy import deepcopy

import pytest

from app.models import EventReplanDecisionRequest, RobotEvent
from app.services.event_impact import analyze_event_impact
from app.services.event_replan import EventReplanConflictError, EventReplanService


def active_plan(two_robots_on_node_two: bool = False) -> dict:
    second_waypoints = (
        [
            {"node_id": 4, "time_step": 0},
            {"node_id": 2, "time_step": 1},
            {"node_id": 5, "time_step": 2},
        ]
        if two_robots_on_node_two
        else [
            {"node_id": 4, "time_step": 0},
            {"node_id": 5, "time_step": 1},
        ]
    )
    return {
        "cuopt_plan": {
            "scheduled_tasks": [
                {"task_id": "W-001:move", "robot_id": "R-01"},
                {"task_id": "W-002:move", "robot_id": "R-02"},
            ]
        },
        "collision_plan": {
            "routes": [
                {
                    "robot_id": "R-01",
                    "task_ids": ["W-001:move"],
                    "waypoints": [
                        {"node_id": 1, "time_step": 0},
                        {"node_id": 2, "time_step": 1},
                        {"node_id": 3, "time_step": 2},
                    ],
                },
                {
                    "robot_id": "R-02",
                    "task_ids": ["W-002:move"],
                    "waypoints": second_waypoints,
                },
            ]
        },
    }


class FakePostgres:
    def __init__(self):
        self.events = {}
        self.requests = {}
        self.stages = []

    def snapshot(self, _warehouse_id, _item_ids):
        return {
            "inventory": [{"item_id": "ITEM-1", "available_quantity": 10}],
            "robots": [
                {"robot_id": "R-01", "status": "IDLE", "node_id": 1},
                {"robot_id": "R-02", "status": "IDLE", "node_id": 4},
            ],
            "works": [
                {"work_id": "W-001", "status": "EXECUTING"},
                {"work_id": "W-002", "status": "PLANNED"},
            ],
        }

    def get_execution_event_processing(self, event_id):
        return deepcopy(self.events.get(event_id))

    def create_execution_event_processing(self, values):
        if values["event_id"] in self.events:
            return {**deepcopy(self.events[values["event_id"]]), "duplicate": True}
        self.events[values["event_id"]] = deepcopy(values)
        return {**deepcopy(values), "duplicate": False}

    def finalize_execution_event_processing(self, event_id, values):
        self.events[event_id].update(deepcopy(values))

    def count_recent_event_failure_signature(
        self, warehouse_id, failure_signature, *, exclude_event_id, window_seconds
    ):
        return sum(
            row.get("warehouse_id") == warehouse_id
            and row.get("failure_signature") == failure_signature
            and event_id != exclude_event_id
            for event_id, row in self.events.items()
        )

    def create_or_get_automatic_replan_request(self, values):
        self.requests.setdefault(values["request_id"], deepcopy(values))
        return deepcopy(self.requests[values["request_id"]])

    def update_automatic_replan_request(self, request_id, values):
        self.requests[request_id].update(deepcopy(values))

    def get_automatic_replan_request(self, request_id):
        return deepcopy(self.requests.get(request_id))

    def persist_stage_logs(self, command_id, stages):
        self.stages.extend(deepcopy(stages))


class FakeRedis:
    def __init__(self, *, two_robots_on_node_two=False):
        self.active_version = "P-1"
        self.plan = active_plan(two_robots_on_node_two)
        self.real_updates = 0
        self.simulation_updates = 0
        self.simulations = {
            "SIM-A": {
                "simulation_id": "SIM-A",
                "inventory": [],
                "robots": [
                    {"robot_id": "R-01", "status": "EXECUTING", "node_id": 1},
                    {"robot_id": "R-02", "status": "IDLE", "node_id": 4},
                ],
                "works": [
                    {"work_id": "W-001", "status": "EXECUTING"},
                    {"work_id": "W-002", "status": "PLANNED"},
                ],
                "active_plan_version": "P-1",
                "active_plan": {**active_plan(two_robots_on_node_two), "plan_version": "P-1"},
            }
        }

    def live_snapshot(self, _warehouse_id):
        return {
            "robots": [],
            "tasks": [],
            "active_plan_version": self.active_version,
            "active_plan": deepcopy(self.plan),
            "temporary_closures": [],
        }

    def simulation_snapshot(self, simulation_id):
        return deepcopy(self.simulations[simulation_id])


class FakeNeo4j:
    def fetch_topology(self, _warehouse_id):
        return {
            "nodes": [{"node_id": value} for value in range(1, 10)],
            "edges": [
                {"edge_id": "E-12", "from_node": 1, "to_node": 2, "direction": "BOTH"},
                {"edge_id": "E-23", "from_node": 2, "to_node": 3, "direction": "BOTH"},
                {"edge_id": "E-42", "from_node": 4, "to_node": 2, "direction": "BOTH"},
                {"edge_id": "E-45", "from_node": 4, "to_node": 5, "direction": "BOTH"},
            ],
        }


class Services:
    def __init__(self, *, two_robots_on_node_two=False):
        self.postgres = FakePostgres()
        self.redis = FakeRedis(two_robots_on_node_two=two_robots_on_node_two)
        self.neo4j = FakeNeo4j()


def event(event_type, *, event_id="EVENT-1", context="REAL", **values):
    simulation_id = "SIM-A" if context == "SIMULATION" else None
    payload = values.pop("payload", {})
    return RobotEvent(
        event_id=event_id,
        warehouse_id=1,
        robot_id=values.pop("robot_id", "R-01"),
        task_id=values.pop("task_id", "W-001:move"),
        event_type=event_type,
        execution_context=context,
        simulation_id=simulation_id,
        payload=payload,
        **values,
    )


def test_robot_failed_and_delayed_impact_are_deterministic() -> None:
    services = Services()
    failed = analyze_event_impact(event("ROBOT_FAILED"), services)
    assert failed.recommended_scope == "LOCAL_REPLAN"
    assert failed.risk_level == "HIGH"
    assert failed.approval_required is True
    minor = analyze_event_impact(
        event("ROBOT_DELAYED", payload={"delay_seconds": 5}), services
    )
    major = analyze_event_impact(
        event("ROBOT_DELAYED", event_id="EVENT-2", payload={"delay_seconds": 30}),
        services,
    )
    assert minor.recommended_scope == "NO_REPLAN"
    assert major.recommended_scope == "LOCAL_REPLAN"


def test_path_blocked_unused_is_no_replan_and_used_is_local() -> None:
    services = Services()
    unused = analyze_event_impact(event("PATH_BLOCKED", node_id=9), services)
    used = analyze_event_impact(
        event("PATH_BLOCKED", event_id="EVENT-2", node_id=3), services
    )
    assert unused.recommended_scope == "NO_REPLAN"
    assert used.recommended_scope == "LOCAL_REPLAN"
    assert used.affected_robot_ids == ["R-01"]


def test_path_blocked_multiple_routes_is_global() -> None:
    impact = analyze_event_impact(
        event("PATH_BLOCKED", node_id=2),
        Services(two_robots_on_node_two=True),
    )
    assert impact.recommended_scope == "GLOBAL_REPLAN"
    assert impact.affected_robot_ids == ["R-01", "R-02"]


def test_path_deviated_uses_reachability_and_unknown_node_is_global() -> None:
    services = Services()
    local = analyze_event_impact(event("PATH_DEVIATED", node_id=4), services)
    global_result = analyze_event_impact(
        event("PATH_DEVIATED", event_id="EVENT-2", node_id=99), services
    )
    assert local.recommended_scope == "LOCAL_REPLAN"
    assert global_result.recommended_scope == "GLOBAL_REPLAN"


def planning_response(command):
    if command.requested_execution_mode == "EXECUTE":
        return {
            "status": "DISPATCHED",
            "command_id": command.command_id,
            "plan_version": "P-EXEC",
            "verification_decision": {"decision": "PASS"},
        }
    return {
        "status": "SIMULATION_SUCCESS",
        "command_id": command.command_id,
        "simulation_id": f"SIM-{command.command_id[-4:]}",
        "plan_version": "P-SIM",
        "verification_decision": {"decision": "PASS"},
    }


def handler_for(services):
    def handler(robot_event, *, auto_replan, analyze_impact):
        assert auto_replan is False
        assert analyze_impact is True
        if robot_event.execution_context == "REAL":
            services.redis.real_updates += 1
        else:
            services.redis.simulation_updates += 1
        impact = analyze_event_impact(robot_event, services)
        return {
            "redis_updated": True,
            "impact_analysis": impact.model_dump(mode="json"),
            "final_status": "REPLAN_REQUIRED",
        }

    return handler


def test_real_event_auto_replans_only_through_simulation_and_requires_approval() -> None:
    services = Services()
    commands = []

    def planner(command):
        commands.append(command)
        return planning_response(command)

    result = EventReplanService(
        services, planner=planner, event_handler=handler_for(services)
    ).handle(event("ROBOT_FAILED"))
    assert result["status"] == "APPROVAL_REQUIRED"
    assert result["auto_replan_requested"] is True
    assert commands[0].requested_execution_mode == "SIMULATE_ONLY"
    assert services.redis.real_updates == 1
    assert all(command.requested_execution_mode != "EXECUTE" for command in commands)


def test_duplicate_event_is_idempotent_and_does_not_change_state_twice() -> None:
    services = Services()
    service = EventReplanService(
        services, planner=planning_response, event_handler=handler_for(services)
    )
    first = service.handle(event("ROBOT_FAILED"))
    second = service.handle(event("ROBOT_FAILED"))
    assert first["event_id"] == second["event_id"]
    assert second["duplicate"] is True
    assert services.redis.real_updates == 1


def test_repeated_failure_signature_stops_second_replan() -> None:
    services = Services()
    calls = []

    def planner(command):
        calls.append(command.command_id)
        return planning_response(command)

    service = EventReplanService(
        services, planner=planner, event_handler=handler_for(services)
    )
    service.handle(event("ROBOT_FAILED", event_id="EVENT-1"))
    result = service.handle(event("ROBOT_FAILED", event_id="EVENT-2"))
    assert result["status"] == "FAILED"
    assert result["failure_reason"] == "REPEATED_FAILURE_DETECTED"
    assert len(calls) == 1


def test_simulation_event_does_not_touch_real_state_or_require_approval() -> None:
    services = Services()
    result = EventReplanService(
        services, planner=planning_response, event_handler=handler_for(services)
    ).handle(event("ROBOT_FAILED", context="SIMULATION"))
    assert result["status"] == "REPLAN_VERIFIED"
    assert result["approval_required"] is False
    assert services.redis.real_updates == 0
    assert services.redis.simulation_updates == 1


def test_approve_checks_stale_plan_before_execute_and_reject_is_supported() -> None:
    services = Services()
    commands = []

    def planner(command):
        commands.append(command)
        return planning_response(command)

    service = EventReplanService(
        services, planner=planner, event_handler=handler_for(services)
    )
    result = service.handle(event("ROBOT_FAILED"))
    request_id = result["replan_request_id"]
    approved = service.approve(
        request_id,
        EventReplanDecisionRequest(reason="운영자 승인", actor_id="tester"),
    )
    assert approved["status"] == "EXECUTED"
    assert commands[-1].requested_execution_mode == "EXECUTE"

    result2 = service.handle(event("PATH_BLOCKED", event_id="EVENT-2", node_id=3))
    rejected = service.reject(
        result2["replan_request_id"],
        EventReplanDecisionRequest(reason="수동 확인 필요"),
    )
    assert rejected["status"] == "REJECTED"


def test_stale_active_plan_blocks_approval_before_gateway_command() -> None:
    services = Services()
    commands = []

    def planner(command):
        commands.append(command)
        return planning_response(command)

    service = EventReplanService(
        services, planner=planner, event_handler=handler_for(services)
    )
    result = service.handle(event("ROBOT_FAILED"))
    services.redis.active_version = "P-CHANGED"
    with pytest.raises(EventReplanConflictError):
        service.approve(
            result["replan_request_id"],
            EventReplanDecisionRequest(reason="승인"),
        )
    assert len(commands) == 1
    assert services.postgres.requests[result["replan_request_id"]]["status"] == "STALE_PLAN"


def test_no_replan_event_does_not_create_planning_command() -> None:
    services = Services()
    calls = []
    result = EventReplanService(
        services,
        planner=lambda command: calls.append(command),
        event_handler=handler_for(services),
    ).handle(event("PATH_BLOCKED", node_id=9))
    assert result["status"] == "REPLAN_NOT_REQUIRED"
    assert result["auto_replan_requested"] is False
    assert calls == []


def successful_completion_handler(
    calls: list[str],
    movements: list[int] | None = None,
):
    def handler(robot_event, *, auto_replan, analyze_impact):
        calls.append(robot_event.event_id)
        if movements is not None:
            movements.append(-30)
        assert auto_replan is False
        assert analyze_impact is True
        return {
            "redis_updated": True,
            "sql_committed": True,
            "commit_result": {
                "committed": True,
                "idempotent_replay": False,
                "previous_status": "READY",
                "final_status": "COMPLETED",
            },
            "final_status": "COMPLETED",
            "errors": [],
        }

    return handler


def test_completed_event_uses_successful_execution_status_without_replan() -> None:
    services = Services()
    calls: list[str] = []

    result = EventReplanService(
        services,
        planner=lambda _command: pytest.fail("normal completion must not replan"),
        event_handler=successful_completion_handler(calls),
    ).handle(event("TASK_COMPLETED", work_id="W-001"))

    assert result["status"] == "COMPLETED"
    assert result["final_status"] == "COMPLETED"
    assert result["auto_replan_requested"] is False
    assert result["redis_updated"] is True
    assert result["sql_committed"] is True
    assert result["errors"] == []
    assert calls == ["EVENT-1"]


@pytest.mark.parametrize(
    "execution_result",
    [
        {
            "redis_updated": True,
            "sql_committed": False,
            "final_status": "COMMIT_FAILED",
            "errors": ["commit failed"],
        },
        {
            "redis_updated": False,
            "sql_committed": False,
            "final_status": "LIVE_UPDATE_FAILED",
            "errors": ["redis failed"],
        },
        {
            "redis_updated": True,
            "sql_committed": True,
            "valid": False,
            "final_status": "VALIDATION_FAILED",
            "errors": ["validation failed"],
        },
    ],
)
def test_completed_event_reports_failed_only_for_required_update_failure(
    execution_result,
) -> None:
    services = Services()

    result = EventReplanService(
        services,
        event_handler=lambda *_args, **_kwargs: execution_result,
    ).handle(event("TASK_COMPLETED", work_id="W-001"))

    assert result["status"] == "FAILED"
    assert result["errors"]


def test_completed_event_duplicate_replays_stored_result_without_second_commit() -> None:
    services = Services()
    calls: list[str] = []
    movements: list[int] = []
    service = EventReplanService(
        services,
        event_handler=successful_completion_handler(calls, movements),
    )

    first = service.handle(event("TASK_COMPLETED", work_id="W-001"))
    second = service.handle(event("TASK_COMPLETED", work_id="W-001"))

    assert first["status"] == "COMPLETED"
    assert second["status"] == "COMPLETED"
    assert second["final_status"] == "COMPLETED"
    assert second["duplicate"] is True
    assert second["commit_result"]["committed"] is True
    assert second["commit_result"]["idempotent_replay"] is True
    assert calls == ["EVENT-1"]
    assert movements == [-30]


def test_duplicate_completion_normalizes_legacy_contradictory_response() -> None:
    services = Services()
    services.postgres.events["EVENT-LEGACY"] = {
        "event_id": "EVENT-LEGACY",
        "event_type": "TASK_COMPLETED",
        "status": "FAILED",
        "result_summary": {
            "status": "FAILED",
            "final_status": "COMPLETED",
            "redis_updated": True,
            "sql_committed": True,
            "commit_result": {
                "committed": True,
                "idempotent_replay": False,
            },
            "errors": [],
        },
    }

    result = EventReplanService(
        services,
        event_handler=lambda *_args, **_kwargs: pytest.fail(
            "duplicate event must not invoke the handler"
        ),
    ).handle(event("TASK_COMPLETED", event_id="EVENT-LEGACY", work_id="W-001"))

    assert result["duplicate"] is True
    assert result["status"] == "COMPLETED"
    assert result["final_status"] == "COMPLETED"
    assert result["commit_result"] == {
        "committed": True,
        "idempotent_replay": True,
    }


def test_repeated_local_path_failure_escalates_once_then_stops() -> None:
    services = Services()
    commands = []

    def planner(command):
        commands.append(command)
        return planning_response(command)

    service = EventReplanService(
        services, planner=planner, event_handler=handler_for(services)
    )
    first = service.handle(
        event(
            "PATH_BLOCKED",
            event_id="PATH-EVENT-1",
            context="SIMULATION",
            node_id=3,
        )
    )
    second = service.handle(
        event(
            "PATH_BLOCKED",
            event_id="PATH-EVENT-2",
            context="SIMULATION",
            node_id=3,
        )
    )
    third = service.handle(
        event(
            "PATH_BLOCKED",
            event_id="PATH-EVENT-3",
            context="SIMULATION",
            node_id=3,
        )
    )

    assert first["scope"] == "LOCAL_REPLAN"
    assert first["escalated_from_local"] is False
    assert second["status"] == "REPLAN_VERIFIED"
    assert second["final_status"] == "REPLAN_VERIFIED"
    assert second["scope"] == "GLOBAL_REPLAN"
    assert second["original_scope"] == "LOCAL_REPLAN"
    assert second["escalated_from_local"] is True
    assert second["repeat_count"] == 1
    assert second["impact_analysis"]["recommended_scope"] == "GLOBAL_REPLAN"
    assert "전체 재계획" in commands[1].text
    assert len(commands) == 2

    assert third["status"] == "FAILED"
    assert third["final_status"] == "FAILED"
    assert third["failure_reason"] == "REPEATED_FAILURE_DETECTED"
    assert third["repeat_count"] == 2
    assert len(commands) == 2


def test_simulation_payload_plan_is_ignored_and_server_plan_wins() -> None:
    services = Services()
    injected_plan = active_plan()
    injected_plan["plan_version"] = "P-INJECTED-LOCAL"
    robot_event = RobotEvent(
        event_id="PLAN-VERSION-EVENT",
        warehouse_id=1,
        robot_id="R-01",
        task_id="W-001:move",
        event_type="PATH_BLOCKED",
        node_id=3,
        execution_context="SIMULATION",
        simulation_id="SIM-A",
        payload={"active_plan": injected_plan, "current_time_step": 999},
    )

    impact = analyze_event_impact(robot_event, services)

    assert impact.recommended_scope == "LOCAL_REPLAN"
    assert impact.active_plan_version == "P-1"


def test_no_replan_response_final_status_matches_status() -> None:
    services = Services()
    result = EventReplanService(
        services, planner=planning_response, event_handler=handler_for(services)
    ).handle(
        event(
            "PATH_BLOCKED",
            event_id="NO-REPLAN-EVENT",
            context="SIMULATION",
            node_id=9,
        )
    )

    assert result["status"] == "REPLAN_NOT_REQUIRED"
    assert result["final_status"] == "REPLAN_NOT_REQUIRED"

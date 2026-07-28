from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from app.models import CommandInterpretation, RobotEvent, ScopeDecision
from app.planning import nodes
from app.services.event_impact import analyze_event_impact
from app.services.event_replan import EventReplanService, _scenario_for_event
from app.services.robot_failure_recovery import derive_robot_failure_recovery
from app.services.simulation_session import _inventory_deltas
from app.models import AtomicTask


SOURCE_PLAN = {
    "plan_version": "P-FAILURE",
    "reference_time": "2026-07-29T00:00:00Z",
    "required_tasks": [
        {
            "task_id": "W-C:1:pick",
            "work_id": "W-C",
            "action": "PICK",
            "item_id": "C",
            "quantity": 10,
            "source_candidates": [2088],
            "target_candidates": [2088],
            "priority": 1,
            "earliest_start": "2026-07-29T00:00:00Z",
            "latest_finish": "2026-07-29T01:00:00Z",
            "time_constraint_type": "HARD_WINDOW",
            "same_robot_group": "W-C:1",
            "inventory_allocations": [
                {
                    "warehouse_item_id": "INV-C",
                    "item_id": "C",
                    "lot_id": "LOT-C",
                    "node_id": 2088,
                    "storage_node_id": 2088,
                    "quantity": 10,
                    "quantity_boxes": 10,
                    "source_type": "CURRENT_LOT",
                }
            ],
        },
        {
            "task_id": "W-C:1:drop",
            "work_id": "W-C",
            "action": "DROP",
            "item_id": "C",
            "quantity": 10,
            "source_candidates": [2088],
            "target_candidates": [2146],
            "priority": 2,
            "earliest_start": "2026-07-29T00:00:00Z",
            "latest_finish": "2026-07-29T01:00:00Z",
            "time_constraint_type": "HARD_WINDOW",
            "same_robot_group": "W-C:1",
            "predecessors": ["W-C:1:pick"],
            "inventory_allocations": [],
        },
        {
            "task_id": "W-D:1:pick",
            "work_id": "W-D",
            "action": "PICK",
            "item_id": "D",
            "quantity": 5,
            "source_candidates": [2088],
            "target_candidates": [2088],
            "priority": 3,
        },
        {
            "task_id": "W-D:1:drop",
            "work_id": "W-D",
            "action": "DROP",
            "item_id": "D",
            "quantity": 5,
            "source_candidates": [2088],
            "target_candidates": [2146],
            "priority": 4,
            "predecessors": ["W-D:1:pick"],
        },
    ],
    "cuopt_plan": {
        "scheduled_tasks": [
            {
                "task_id": "W-C:1:pick",
                "work_id": "W-C",
                "action": "PICK",
                "robot_id": "R-FAIL",
                "source_node": 2088,
                "target_node": 2088,
                "start_time_step": 10,
                "end_time_step": 20,
            },
            {
                "task_id": "W-C:1:drop",
                "work_id": "W-C",
                "action": "DROP",
                "robot_id": "R-FAIL",
                "source_node": 2088,
                "target_node": 2146,
                "start_time_step": 20,
                "end_time_step": 40,
            },
            {
                "task_id": "W-D:1:pick",
                "work_id": "W-D",
                "action": "PICK",
                "robot_id": "R-KEEP",
                "source_node": 2088,
                "target_node": 2088,
                "start_time_step": 50,
                "end_time_step": 60,
            },
            {
                "task_id": "W-D:1:drop",
                "work_id": "W-D",
                "action": "DROP",
                "robot_id": "R-KEEP",
                "source_node": 2088,
                "target_node": 2146,
                "start_time_step": 60,
                "end_time_step": 70,
            },
        ],
        "objective_value": 0,
        "metadata": {},
    },
    "collision_plan": {
        "routes": [
            {
                "robot_id": "R-FAIL",
                "task_ids": ["W-C:1:pick", "W-C:1:drop"],
                "waypoints": [
                    {"node_id": 2088, "time_step": 20},
                    {"node_id": 2013, "time_step": 21},
                    {"node_id": 2014, "time_step": 22},
                    {"node_id": 2146, "time_step": 40},
                ],
            },
            {
                "robot_id": "R-KEEP",
                "task_ids": ["W-D:1:pick", "W-D:1:drop"],
                "waypoints": [
                    {"node_id": 2146, "time_step": 50},
                    {"node_id": 2088, "time_step": 60},
                    {"node_id": 2146, "time_step": 70},
                ],
            },
        ]
    },
}


SQL = {
    "inventory": [],
    "robots": [
        {
            "robot_id": "R-FAIL",
            "node_id": 2014,
            "battery": 60,
            "status": "EXECUTING",
            "max_load": 20,
            "current_load": 10,
        },
        {
            "robot_id": "R-REPLACE",
            "node_id": 2152,
            "battery": 95,
            "status": "IDLE",
            "max_load": 20,
            "current_load": 0,
        },
        {
            "robot_id": "R-KEEP",
            "node_id": 2146,
            "battery": 90,
            "status": "IDLE",
            "max_load": 20,
            "current_load": 0,
        },
    ],
    "works": [],
    "work_dependencies": [],
    "work_schedule_constraints": [],
}


class Services:
    def __init__(self):
        self.postgres = SimpleNamespace(snapshot=lambda *_args: deepcopy(SQL))
        self.redis = SimpleNamespace(
            simulation_snapshot=lambda _simulation_id: {
                "simulation_id": "SIM-FAIL",
                "inventory": [],
                "robots": deepcopy(SQL["robots"]),
                "works": [],
                "active_plan_version": "P-FAILURE",
                "active_plan": deepcopy(SOURCE_PLAN),
                "temporary_closures": [],
            }
        )
        self.neo4j = SimpleNamespace(
            fetch_topology=lambda _warehouse_id: {
                "nodes": [{"node_id": value} for value in (2013, 2014, 2088, 2146, 2152)],
                "edges": [
                    {"from_node": 2014, "to_node": 2013, "direction": "BOTH"},
                    {"from_node": 2013, "to_node": 2088, "direction": "BOTH"},
                    {"from_node": 2013, "to_node": 2146, "direction": "BOTH"},
                    {"from_node": 2152, "to_node": 2014, "direction": "BOTH"},
                ],
            }
        )


def failure_event(*, step=22, safe=True, secured=True, carrying=None) -> RobotEvent:
    payload = {
        "safe_stop_confirmed": safe,
        "load_secured": secured,
        "_server_runtime": {
            "source": "SIMULATION_REDIS_SESSION",
            "current_time_step": step,
            "active_plan_version": "P-FAILURE",
            "active_plan": deepcopy(SOURCE_PLAN),
            "robot_state": {
                "robot_id": "R-FAIL",
                "node_id": 2014,
                "current_load": 10 if step >= 20 else 0,
            },
        },
    }
    if carrying is not None:
        payload["carrying_load"] = carrying
    return RobotEvent(
        event_id=f"FAIL-{step}-{safe}-{secured}-{carrying}",
        warehouse_id=1,
        robot_id="R-FAIL",
        work_id="W-C",
        task_id="W-C:1:drop" if step >= 20 else "W-C:1:pick",
        event_type="ROBOT_FAILED",
        node_id=2014,
        occurred_at="2026-07-29T00:01:50Z",
        execution_context="SIMULATION",
        simulation_id="SIM-FAIL",
        payload=payload,
    )


def test_failure_before_pick_reassigns_original_chain_without_handover() -> None:
    event = failure_event(step=15, carrying=False)
    recovery = derive_robot_failure_recovery(
        event,
        active_plan=deepcopy(SOURCE_PLAN),
        sql=deepcopy(SQL),
        live={"robots": deepcopy(SQL["robots"])},
    )
    assert recovery["status"] == "READY"
    assert recovery["strategy"] == "REASSIGN_UNPICKED_CHAIN"
    assert recovery["load_state"] == "EMPTY"
    assert recovery["recovery_tasks"] == []
    assert "R-REPLACE" in recovery["replacement_candidate_ids"]


def test_secured_carried_load_generates_handover_chain_at_failure_node() -> None:
    event = failure_event(step=22, safe=True, secured=True)
    recovery = derive_robot_failure_recovery(
        event,
        active_plan=deepcopy(SOURCE_PLAN),
        sql=deepcopy(SQL),
        live={"robots": deepcopy(SQL["robots"])},
    )
    assert recovery["status"] == "READY"
    assert recovery["strategy"] == "HANDOVER_SECURED_LOAD"
    assert recovery["load_state"] == "CARRYING"
    assert recovery["replace_task_ids"] == ["W-C:1:pick", "W-C:1:drop"]
    pick, drop = recovery["recovery_tasks"]
    assert pick["action"] == "PICK"
    assert pick["source_candidates"] == [2014]
    assert pick["inventory_allocations"][0]["source_type"] == "ROBOT_HANDOVER"
    assert pick["inventory_transition_policy"] == "NO_STOCK_DELTA"
    assert _inventory_deltas(AtomicTask.model_validate(pick)) == []
    assert drop["action"] == "DROP"
    assert drop["target_candidates"] == [2146]
    assert drop["predecessors"] == [pick["task_id"]]


def test_unsecured_carried_load_blocks_automatic_replan() -> None:
    impact = analyze_event_impact(
        failure_event(step=22, safe=False, secured=False), Services()
    )
    assert impact.recommended_scope == "NO_REPLAN"
    assert impact.robot_failure_recovery["status"] == "BLOCKED"
    assert impact.robot_failure_recovery["strategy"] == "MANUAL_LOAD_RECOVERY_REQUIRED"
    assert impact.robot_failure_recovery["requires_manual_recovery"] is True


def test_impact_releases_failed_chain_and_adds_synthetic_recovery_tasks() -> None:
    impact = analyze_event_impact(failure_event(step=22), Services())
    recovery_ids = impact.robot_failure_recovery["recovery_task_ids"]
    assert impact.recommended_scope == "LOCAL_REPLAN"
    assert set(recovery_ids).issubset(set(impact.changeable_task_ids))
    assert not set(impact.robot_failure_recovery["replace_task_ids"]) & set(impact.frozen_task_ids)
    scenario = _scenario_for_event(failure_event(step=22), impact)
    assert scenario.excluded_robot_ids == ["R-FAIL"]
    assert scenario.recovery_tasks == impact.robot_failure_recovery["recovery_tasks"]
    assert scenario.recovery_replace_task_ids == ["W-C:1:pick", "W-C:1:drop"]


def _selection_state(impact) -> dict:
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="LOCAL_REPLAN",
        objective="robot failure recovery",
        execution_mode="SIMULATE_ONLY",
        target_task_ids=list(impact.changeable_task_ids),
        extracted_task_ids=[row["task_id"] for row in SOURCE_PLAN["required_tasks"]],
        extracted_robot_ids=["R-FAIL"],
        excluded_robot_ids=["R-FAIL"],
        summary="robot failure recovery",
    )
    scope = ScopeDecision(
        plan_mode="LOCAL_REPLAN",
        affected_task_ids=list(impact.affected_task_ids),
        affected_robot_ids=["R-FAIL"],
        fixed_task_ids=list(impact.frozen_task_ids),
        changeable_task_ids=list(impact.changeable_task_ids),
        freeze_horizon_seconds=15,
        optimization_goal="recover carried load",
        reason_summary="failed robot",
    )
    scenario = _scenario_for_event(failure_event(step=22), impact)
    return {
        "command": {
            "command_id": "C-FAILURE",
            "warehouse_id": 1,
            "text": "recover",
            "scenario_definition": scenario.model_dump(mode="json"),
        },
        "interpretation": interpretation.model_dump(mode="json"),
        "scope": scope.model_dump(mode="json"),
        "snapshot": {
            "sql": deepcopy(SQL),
            "redis": {"robots": deepcopy(SQL["robots"]), "executing_task_ids": []},
            "graph": {"nodes": [], "edges": []},
        },
        "replan_base_plan": deepcopy(SOURCE_PLAN),
        "inventory_operations": [],
        "inventory_feasibility": {"item_results": []},
        "inventory_timeline_validation": {},
        "inventory_blocked_work_ids": [],
    }


def test_required_task_overlay_removes_original_pick_and_inserts_handover_pair() -> None:
    impact = analyze_event_impact(failure_event(step=22), Services())
    update = nodes.select_required_tasks_node(_selection_state(impact))
    by_id = {row["task_id"]: row for row in update["required_tasks"]}
    recovery_ids = impact.robot_failure_recovery["recovery_task_ids"]
    assert "W-C:1:pick" not in by_id
    assert "W-C:1:drop" not in by_id
    assert set(recovery_ids).issubset(by_id)
    assert by_id[recovery_ids[0]]["source_candidates"] == [2014]
    assert by_id[recovery_ids[1]]["target_candidates"] == [2146]
    assert by_id["W-D:1:pick"]["frozen"] is True
    assert update["schedule_validation"]["robot_failure_recovery"]["strategy"] == "HANDOVER_SECURED_LOAD"


def test_manual_recovery_returns_explicit_terminal_status_without_planner() -> None:
    event = failure_event(step=22, safe=False, secured=False)
    impact = analyze_event_impact(event, Services())
    calls = []

    class Postgres:
        def __init__(self):
            self.events = {}
        def get_execution_event_processing(self, event_id):
            return self.events.get(event_id)
        def create_execution_event_processing(self, values):
            self.events[values["event_id"]] = deepcopy(values)
            return {**deepcopy(values), "duplicate": False}
        def finalize_execution_event_processing(self, event_id, values):
            self.events[event_id].update(deepcopy(values))

    services = Services()
    services.postgres = Postgres()
    result = EventReplanService(
        services,
        planner=lambda _command: calls.append(_command),
        event_handler=lambda *_args, **_kwargs: {
            "redis_updated": True,
            "impact_analysis": impact.model_dump(mode="json"),
            "final_status": "REPLAN_REQUIRED",
        },
    ).handle(event)
    assert result["status"] == "MANUAL_RECOVERY_REQUIRED"
    assert result["recovery_required"] is True
    assert result["auto_replan_requested"] is False
    assert calls == []


def test_verified_handover_requires_same_nonfailed_replacement_robot() -> None:
    event = failure_event(step=22)
    impact = analyze_event_impact(event, Services())
    recovery_ids = impact.robot_failure_recovery["recovery_task_ids"]

    class Postgres:
        def __init__(self):
            self.events = {}
            self.requests = {}
        def get_execution_event_processing(self, event_id):
            return self.events.get(event_id)
        def create_execution_event_processing(self, values):
            self.events[values["event_id"]] = deepcopy(values)
            return {**deepcopy(values), "duplicate": False}
        def finalize_execution_event_processing(self, event_id, values):
            self.events[event_id].update(deepcopy(values))
        def count_recent_event_failure_signature(self, *_args, **_kwargs):
            return 0
        def create_or_get_automatic_replan_request(self, values):
            self.requests[values["request_id"]] = deepcopy(values)
            return deepcopy(values)
        def update_automatic_replan_request(self, request_id, values):
            self.requests[request_id].update(deepcopy(values))
        def persist_stage_logs(self, *_args, **_kwargs):
            return None

    services = Services()
    services.postgres = Postgres()
    planner = lambda command: {
        "status": "SIMULATION_SUCCESS",
        "command_id": command.command_id,
        "simulation_id": "SIM-RECOVERED",
        "plan_version": "P-RECOVERED",
        "verification_decision": {"decision": "PASS"},
        "collision_plan": {
            "metadata": {
                "stale_route_eviction": {
                    "version": "p16.5.14.1",
                    "policy": "EVICT_EXCLUDED_OR_FAILED_ACTIVE_ROUTES",
                    "changed_robot_ids": ["R-FAIL", "R-REPLACE"],
                    "evicted_robot_ids": ["R-FAIL"],
                    "preserved_robot_ids": [],
                }
            }
        },
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": recovery_ids[0],
                    "action": "PICK",
                    "robot_id": "R-REPLACE",
                    "source_node": 2014,
                    "target_node": 2014,
                    "start_time_step": 23,
                    "end_time_step": 24,
                },
                {
                    "task_id": recovery_ids[1],
                    "action": "DROP",
                    "robot_id": "R-REPLACE",
                    "source_node": 2014,
                    "target_node": 2146,
                    "start_time_step": 24,
                    "end_time_step": 35,
                },
            ]
        },
    }
    result = EventReplanService(
        services,
        planner=planner,
        event_handler=lambda *_args, **_kwargs: {
            "redis_updated": True,
            "impact_analysis": impact.model_dump(mode="json"),
            "final_status": "REPLAN_REQUIRED",
        },
    ).handle(event)
    assert result["status"] == "REPLAN_VERIFIED"
    evidence = result["robot_failure_recovery_result"]
    assert evidence["status"] == "PASS"
    assert evidence["replacement_robot_ids"] == ["R-REPLACE"]
    assert evidence["same_replacement_robot"] is True
    assert evidence["handover_order_valid"] is True
    assert result["stale_route_eviction"]["evicted_robot_ids"] == ["R-FAIL"]

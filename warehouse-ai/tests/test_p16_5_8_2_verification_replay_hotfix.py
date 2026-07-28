from __future__ import annotations

from copy import deepcopy

from app.models import (
    CollisionFreePlan,
    CuOptPlan,
    ScheduledTask,
    SimulationResult,
    TimedRoute,
    TimedWaypoint,
)
from app.services.charger_selection import (
    OPPORTUNITY_DISTANCE_FALLBACK_POLICY,
    expected_opportunity_candidate,
    rank_opportunity_charger_candidates,
)
from app.services.energy_reconciliation import reconcile_plan_energy
from app.services.inventory_transition import calculate_inventory_transition
from app.services.simulation_session import replay_simulation_session


def _tied_candidates() -> list[dict]:
    return [
        {
            "charger_node": 2150,
            "safe_reachable": True,
            "rejection_reason": None,
            "linked_waiting_area_node_ids": [2160],
            "to_charger_distance": 20.98,
            "charger_to_next_source_distance": 8.10,
            "charged_percent": 2.4,
            "charge_duration_seconds": 30,
            "charger_cost": 1.2,
        },
        {
            "charger_node": 2151,
            "safe_reachable": True,
            "rejection_reason": None,
            "linked_waiting_area_node_ids": [2161],
            "to_charger_distance": 19.46,
            "charger_to_next_source_distance": 9.62,
            "charged_percent": 2.4,
            "charge_duration_seconds": 30,
            "charger_cost": 1.0,
        },
        {
            "charger_node": 2155,
            "safe_reachable": True,
            "rejection_reason": None,
            "linked_waiting_area_node_ids": [],
            "to_charger_distance": 14.35,
            "charger_to_next_source_distance": 14.73,
            "charged_percent": 2.4,
            "charge_duration_seconds": 30,
            "charger_cost": None,
        },
    ]


def test_verifier_uses_recorded_selection_key_after_route_adjustment() -> None:
    ranked, selected, policy, _ = rank_opportunity_charger_candidates(
        _tied_candidates()
    )
    assert policy == OPPORTUNITY_DISTANCE_FALLBACK_POLICY
    assert selected is not None
    assert selected["charger_node"] == 2150

    # Reproduce the live failure: routing reconciliation changed only the
    # selected charger's operational duration. The planning selection_key must
    # remain authoritative for verification.
    for row in ranked:
        if row.get("selected"):
            row["reconciled_charge_duration_seconds"] = 35
            row["reconciled_charged_percent"] = 2.559

    expected, replayed_policy, _ = expected_opportunity_candidate(ranked)

    assert expected is not None
    assert expected["charger_node"] == 2150
    assert replayed_policy == OPPORTUNITY_DISTANCE_FALLBACK_POLICY


def test_energy_reconciliation_does_not_overwrite_selection_inputs() -> None:
    ranked, selected, policy, reason = rank_opportunity_charger_candidates(
        _tied_candidates()
    )
    assert selected is not None
    original_selected = next(row for row in ranked if row.get("selected"))
    original_duration = original_selected["charge_duration_seconds"]
    original_key = deepcopy(original_selected["selection_key"])

    plan = CuOptPlan(
        scheduled_tasks=[
            ScheduledTask(
                task_id="opportunity:R2-02:T1:charge:2150",
                work_id="T1",
                action="CHARGE",
                robot_id="R2-02",
                source_node=2146,
                target_node=2150,
                start_time_step=0,
                end_time_step=6,
                priority=1,
                estimated_distance=20.98,
                charge_target_battery=95.0,
                charged_percent=2.4,
                charge_duration_seconds=30,
                charger_cost=1.2,
                charger_selection_policy=policy,
                charger_selection_reason=reason,
                charger_candidates=ranked,
                schedule_status="READY",
            )
        ],
        objective_value=0.0,
    )
    collision = CollisionFreePlan(
        engine="PRIORITIZED_TIME_ASTAR",
        routes=[
            TimedRoute(
                robot_id="R2-02",
                task_ids=["opportunity:R2-02:T1:charge:2150"],
                waypoints=[
                    TimedWaypoint(node_id=2146, time_step=0, action="MOVE"),
                    TimedWaypoint(node_id=2150, time_step=6, action="CHARGE"),
                    TimedWaypoint(node_id=2150, time_step=12, action="CHARGE"),
                ],
                distance=22.78,
            )
        ],
        time_step_seconds=5,
        total_distance=22.78,
    )
    problem = {
        "robots": [{"robot_id": "R2-02", "battery": 93.58}],
        "nodes": [{"node_id": 2146}, {"node_id": 2150}],
        "edges": [
            {
                "from_node": 2146,
                "to_node": 2150,
                "distance": 22.78,
                "direction": "BOTH",
                "active": True,
            }
        ],
        "energy_per_distance": 0.05,
        "min_robot_battery": 20.0,
        "charge_target_battery": 95.0,
        "charge_rate_percent_per_minute": 5.0,
        "time_step_seconds": 5,
    }

    reconciled, _ = reconcile_plan_energy(plan, collision, problem)
    updated = reconciled.scheduled_tasks[0]
    selected_after = next(
        row for row in updated.charger_candidates if row.get("selected")
    )

    assert selected_after["selection_key"] == original_key
    assert selected_after["charge_duration_seconds"] == original_duration
    assert "reconciled_charge_duration_seconds" in selected_after
    assert updated.charge_duration_seconds >= original_duration


class _ReplayRedis:
    def __init__(self) -> None:
        self.sessions = {
            "SIM-REPLAN": {
                "simulation_id": "SIM-REPLAN",
                "inventory": [
                    {
                        "warehouse_item_id": "DEMO-INV-2-B",
                        "quantity": 0,
                        "reserved_quantity": 0,
                    }
                ],
                "robots": [],
                "works": [],
                "checkpoint": "stale",
            }
        }
        self.remove_calls: list[tuple[int, str]] = []
        self.counter = 0

    def remove_simulation_state(self, warehouse_id: int, simulation_id: str) -> dict:
        self.remove_calls.append((warehouse_id, simulation_id))
        self.sessions.pop(simulation_id, None)
        return {"deleted": True}

    def initialize_simulation_session(self, simulation_id: str, snapshot: dict) -> dict:
        if simulation_id not in self.sessions:
            self.sessions[simulation_id] = {
                "simulation_id": simulation_id,
                "inventory": deepcopy(snapshot["sql"]["inventory"]),
                "robots": [],
                "works": [],
                "checkpoint": "0",
            }
        return deepcopy(self.sessions[simulation_id])

    def update_simulation_from_event(self, event) -> dict:
        session = self.sessions[event.simulation_id]
        if event.event_type == "TASK_COMPLETED" and event.inventory_deltas:
            by_id = {
                str(row["warehouse_item_id"]): dict(row)
                for row in session["inventory"]
            }
            quantities = calculate_inventory_transition(
                {key: int(row.get("quantity") or 0) for key, row in by_id.items()},
                event.inventory_deltas,
            )
            for key, quantity in quantities.items():
                by_id[key]["quantity"] = quantity
            session["inventory"] = list(by_id.values())
        self.counter += 1
        session["checkpoint"] = str(self.counter)
        return deepcopy(session)

    def simulation_snapshot(self, simulation_id: str) -> dict:
        return deepcopy(self.sessions[simulation_id])


def test_replan_replay_resets_stale_virtual_inventory() -> None:
    repository = _ReplayRedis()
    state = {
        "simulation_id": "SIM-REPLAN",
        "replan_attempt": 1,
        "command": {"warehouse_id": 2},
        "snapshot": {
            "warehouse_id": 2,
            "captured_at": "2026-07-25T00:00:00+00:00",
            "sql": {
                "inventory": [
                    {
                        "warehouse_item_id": "DEMO-INV-2-B",
                        "quantity": 20,
                        "reserved_quantity": 0,
                    }
                ]
            },
        },
        "optimization_problem": {
            "warehouse_id": 2,
            "reference_time": "2026-07-25T00:00:00+00:00",
        },
        "collision_plan": {"time_step_seconds": 5, "routes": []},
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "B:pick",
                    "work_id": "B",
                    "robot_id": "R2-03",
                    "source_node": 2088,
                    "target_node": 2088,
                    "start_time_step": 0,
                    "end_time_step": 1,
                }
            ]
        },
        "required_tasks": [
            {
                "task_id": "B:pick",
                "work_id": "B",
                "action": "PICK",
                "item_id": "B",
                "quantity": 20,
                "source_candidates": [2088],
                "target_candidates": [2088],
                "inventory_allocations": [
                    {
                        "warehouse_item_id": "DEMO-INV-2-B",
                        "quantity": 20,
                    }
                ],
            }
        ],
        "inventory_operations": [],
    }
    result = SimulationResult(success=True, valid=True, status="SUCCESS")

    replayed = replay_simulation_session(state, result, repository)

    assert repository.remove_calls == [(2, "SIM-REPLAN")]
    assert replayed["session_reset_for_replan"] is True
    item = replayed["current_state"]["inventory"][0]
    assert item["quantity"] == 0

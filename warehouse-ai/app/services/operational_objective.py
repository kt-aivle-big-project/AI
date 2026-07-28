from __future__ import annotations

from typing import Any

from app.models import CollisionFreePlan, CuOptPlan


OPERATIONAL_OBJECTIVE_VERSION = "p16.5.12.1"


def calculate_operational_objective(
    problem: dict[str, Any],
    plan: CuOptPlan,
    collision_plan: CollisionFreePlan,
    resource_plan: dict[str, Any],
) -> dict[str, Any]:
    """Create the post-routing objective ledger used for approval evidence."""

    weights = {
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
        **(problem.get("weights") or {}),
    }
    step_seconds = max(1, int(problem.get("time_step_seconds") or 5))
    tasks = list(plan.scheduled_tasks)
    charge_tasks = [task for task in tasks if task.action == "CHARGE"]
    makespan = max((task.end_time_step for task in tasks), default=0)
    tardiness = int(plan.metadata.get("tardiness_time_steps") or 0)
    travel_energy = float(plan.metadata.get("energy") or 0.0)
    active_robots = len({task.robot_id for task in tasks})
    plan_changes = int(plan.metadata.get("plan_changes") or 0)
    charging_time_steps = sum(
        max(0, int((task.charge_duration_seconds or 0) + step_seconds - 1) // step_seconds)
        for task in charge_tasks
    )
    charging_energy_percent = round(
        sum(float(task.charged_percent or 0.0) for task in charge_tasks), 6
    )
    reservations = list(resource_plan.get("reservations", []) or [])
    charger_wait_steps = sum(
        max(0, int(row.get("end_time_step") or 0) - int(row.get("start_time_step") or 0))
        for row in reservations
        if str(row.get("resource_type") or "").upper() == "IDLE_SPACE"
        and str(row.get("node_type") or "").upper() == "CHARGER_WAITING_AREA"
    )
    resource_occupancy_steps = sum(
        max(0, int(row.get("end_time_step") or 0) - int(row.get("start_time_step") or 0))
        for row in reservations
    )
    congestion_nodes = {int(value) for value in problem.get("congestion_node_ids", [])}
    congestion_visits = sum(
        1
        for route in collision_plan.routes
        for waypoint in route.waypoints
        if int(waypoint.node_id) in congestion_nodes
    )
    opportunity = plan.metadata.get("opportunity_charging", {}) or {}
    unnecessary_roundtrip_distance = float(opportunity.get("added_distance") or 0.0)
    total_distance = float(collision_plan.total_distance or 0.0)

    components = {
        "distance_component": total_distance * float(weights["total_distance"]),
        "makespan_component": makespan * float(weights["makespan"]),
        "tardiness_component": tardiness * float(weights["tardiness"]),
        "energy_component": travel_energy * float(weights["energy"]),
        "robot_activation_component": active_robots * float(weights["robot_activation"]),
        "plan_change_component": plan_changes * float(weights["plan_change"]),
        "charging_time_component": charging_time_steps * float(weights["charging_time"]),
        "charger_wait_component": charger_wait_steps * float(weights["charger_wait"]),
        "charger_visit_component": len(charge_tasks) * float(weights["charger_visit"]),
        "congestion_component": congestion_visits * float(weights["congestion"]),
        "shared_resource_occupancy_component": resource_occupancy_steps
        * float(weights["shared_resource_occupancy"]),
        "unnecessary_charger_roundtrip_component": unnecessary_roundtrip_distance
        * float(weights["unnecessary_charger_roundtrip"]),
    }
    rounded_components = {key: round(value, 6) for key, value in components.items()}
    return {
        "version": OPERATIONAL_OBJECTIVE_VERSION,
        "status": "PASS",
        "objective_scope": "POST_ROUTING_OPERATIONAL_PLAN",
        "hard_constraint_policy": "INFEASIBLE_CANDIDATES_REMOVED_NOT_PENALIZED",
        "total": round(sum(components.values()), 6),
        "metrics": {
            "total_distance": round(total_distance, 6),
            "makespan_time_steps": makespan,
            "tardiness_time_steps": tardiness,
            "travel_energy_percent": round(travel_energy, 6),
            "charging_energy_percent": charging_energy_percent,
            "charging_time_steps": charging_time_steps,
            "charger_wait_time_steps": charger_wait_steps,
            "charger_visit_count": len(charge_tasks),
            "active_robot_count": active_robots,
            "plan_change_count": plan_changes,
            "congestion_node_visit_count": congestion_visits,
            "shared_resource_occupancy_time_steps": resource_occupancy_steps,
            "unnecessary_charger_roundtrip_distance": round(
                unnecessary_roundtrip_distance, 6
            ),
        },
        "components": rounded_components,
        "weights": {key: float(value) for key, value in weights.items()},
        "role_contract": {
            "cuopt": [
                "ROBOT_ASSIGNMENT",
                "VISIT_ORDER",
                "TIME_WINDOWS",
                "TRAVEL_ENERGY_CONGESTION_COMPOSITE_COST",
                "EXPLICIT_CHARGE_VISITS",
            ],
            "local_scheduler": [
                "PICK_DROP_SAME_ROBOT",
                "SHARED_RESOURCE_CAPACITY",
                "EXACT_CHARGE_AMOUNT",
                "IDLE_WHITELIST",
            ],
            "prioritized_time_astar": [
                "VERTEX_COLLISION_AVOIDANCE",
                "EDGE_SWAP_AVOIDANCE",
                "TIMED_NODE_EDGE_RESERVATIONS",
            ],
        },
    }

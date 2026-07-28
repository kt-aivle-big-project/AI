from app.models import CuOptPlan, ScheduledTask
from app.services.plan_evidence import build_route_evidence
from app.services.routing import PrioritizedTimeExpandedPlanner
from app.services.simulation import simulate_plan


def _problem(nodes, robots, edges):
    return {
        "nodes": nodes,
        "robots": robots,
        "edges": edges,
        "temporary_closures": [],
        "active_plan": None,
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
    }


def _edge(start, target, *, distance=1.0):
    return {
        "from_node": start,
        "to_node": target,
        "distance": distance,
        "travel_seconds": 1,
        "direction": "BOTH",
        "active": True,
    }


def _plan(problem, tasks):
    cuopt = CuOptPlan(scheduled_tasks=tasks, objective_value=0)
    collision = PrioritizedTimeExpandedPlanner(problem, 1, 30).solve(cuopt)
    simulation = simulate_plan(collision, cuopt, problem)
    return cuopt, collision, simulation


def test_vertex_conflict_is_resolved_with_attributed_wait() -> None:
    problem = _problem(
        [{"node_id": value, "node_type": "INTERSECTION"} for value in range(1, 6)],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 4}],
        [_edge(1, 2), _edge(2, 3), _edge(4, 2), _edge(2, 5)],
    )
    tasks = [
        ScheduledTask(
            task_id="NORMAL-A",
            robot_id="R1",
            source_node=1,
            target_node=3,
            start_time_step=0,
            end_time_step=10,
            priority=10,
        ),
        ScheduledTask(
            task_id="NORMAL-B",
            robot_id="R2",
            source_node=4,
            target_node=5,
            start_time_step=0,
            end_time_step=10,
            priority=20,
        ),
    ]

    _, collision, simulation = _plan(problem, tasks)

    wait = next(
        row
        for row in collision.metadata["wait_evidence"]
        if row.get("reason") == "RESERVATION_CONFLICT_WAIT"
    )
    assert wait["robot_id"] == "R2"
    assert wait["conflict_type"] == "VERTEX_OCCUPANCY"
    assert wait["blocked_resource"] == "NODE:2@1"
    assert wait["blocked_by_robot_id"] == "R1"
    assert wait["blocked_by_task_id"] == "NORMAL-A"
    assert simulation.conflict_count == 0


def test_edge_swap_conflict_is_resolved_by_attributed_reroute() -> None:
    problem = _problem(
        [{"node_id": value, "node_type": "INTERSECTION"} for value in range(1, 5)],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 2}],
        [_edge(1, 2), _edge(2, 3), _edge(3, 4), _edge(4, 1)],
    )
    tasks = [
        ScheduledTask(
            task_id="EDGE-A",
            robot_id="R1",
            source_node=1,
            target_node=2,
            start_time_step=0,
            end_time_step=10,
            priority=1,
        ),
        ScheduledTask(
            task_id="EDGE-B",
            robot_id="R2",
            source_node=2,
            target_node=1,
            start_time_step=0,
            end_time_step=10,
            priority=2,
        ),
    ]

    cuopt, collision, simulation = _plan(problem, tasks)

    reroute = next(
        row
        for row in collision.metadata["resolution_events"]
        if row.get("resolution") == "REROUTE"
    )
    assert collision.metadata["reroute_count"] == 1
    assert reroute["robot_id"] == "R2"
    assert reroute["conflict_type"] == "EDGE_SWAP"
    assert reroute["blocked_resource"] == "EDGE:2<->1@0"
    assert reroute["blocked_by_robot_id"] == "R1"
    assert reroute["blocked_by_task_id"] == "EDGE-A"
    assert simulation.conflict_count == 0

    _, reservations, _ = build_route_evidence(problem, cuopt, collision)
    assert reservations.reroute_count == 1
    assert any(row["resolution"] == "REROUTE" for row in reservations.resolution_events)


def test_shared_charger_is_serialized_and_reports_occupancy_owner() -> None:
    problem = _problem(
        [
            {"node_id": 1, "node_type": "INTERSECTION"},
            {"node_id": 2, "node_type": "CHARGER"},
            {"node_id": 3, "node_type": "INTERSECTION"},
        ],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 3}],
        [_edge(1, 2), _edge(3, 2)],
    )
    tasks = [
        ScheduledTask(
            task_id="CHARGE-A",
            action="CHARGE",
            robot_id="R1",
            source_node=2,
            target_node=2,
            start_time_step=0,
            end_time_step=10,
            priority=1,
            charge_duration_seconds=2,
        ),
        ScheduledTask(
            task_id="CHARGE-B",
            action="CHARGE",
            robot_id="R2",
            source_node=2,
            target_node=2,
            start_time_step=0,
            end_time_step=10,
            priority=2,
            charge_duration_seconds=2,
        ),
    ]

    _, collision, simulation = _plan(problem, tasks)

    routes = {route.robot_id: route for route in collision.routes}
    r1_charge_steps = {
        row.time_step for row in routes["R1"].waypoints if row.action == "CHARGE"
    }
    r2_charge_steps = {
        row.time_step for row in routes["R2"].waypoints if row.action == "CHARGE"
    }
    assert r1_charge_steps
    assert r2_charge_steps
    assert r1_charge_steps.isdisjoint(r2_charge_steps)
    occupancy_waits = [
        row
        for row in collision.metadata["wait_evidence"]
        if row.get("conflict_type") == "CHARGER_OCCUPANCY"
    ]
    assert occupancy_waits
    assert all(row["robot_id"] == "R2" for row in occupancy_waits)
    assert all(row["blocked_by_robot_id"] == "R1" for row in occupancy_waits)
    assert all(row["blocked_by_task_id"] == "CHARGE-A" for row in occupancy_waits)
    assert simulation.conflict_count == 0


def test_emergency_priority_reserves_shared_node_before_normal_task() -> None:
    problem = _problem(
        [{"node_id": value, "node_type": "INTERSECTION"} for value in range(1, 6)],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 4}],
        [_edge(1, 2), _edge(2, 3), _edge(4, 2), _edge(2, 5)],
    )
    # Intentionally list NORMAL first. Routing must still prioritize the lower
    # numeric emergency priority.
    tasks = [
        ScheduledTask(
            task_id="NORMAL",
            robot_id="R1",
            source_node=1,
            target_node=3,
            start_time_step=0,
            end_time_step=10,
            priority=50,
        ),
        ScheduledTask(
            task_id="EMERGENCY",
            robot_id="R2",
            source_node=4,
            target_node=5,
            start_time_step=0,
            end_time_step=10,
            priority=1,
        ),
    ]

    _, collision, simulation = _plan(problem, tasks)

    route_order = [route.robot_id for route in collision.routes]
    assert route_order[0] == "R2"
    wait = next(
        row
        for row in collision.metadata["wait_evidence"]
        if row.get("reason") == "RESERVATION_CONFLICT_WAIT"
    )
    assert wait["robot_id"] == "R1"
    assert wait["blocked_by_robot_id"] == "R2"
    assert wait["blocked_by_task_id"] == "EMERGENCY"
    assert simulation.conflict_count == 0


def test_long_same_direction_edge_is_treated_as_capacity_one() -> None:
    problem = _problem(
        [
            {"node_id": 0, "node_type": "INTERSECTION"},
            {"node_id": 1, "node_type": "INTERSECTION"},
            {"node_id": 2, "node_type": "INTERSECTION"},
        ],
        [{"robot_id": "R1", "node_id": 1}, {"robot_id": "R2", "node_id": 0}],
        [
            _edge(0, 1),
            {
                "from_node": 1,
                "to_node": 2,
                "distance": 3.0,
                "travel_seconds": 3,
                "direction": "BOTH",
                "active": True,
            },
        ],
    )
    tasks = [
        ScheduledTask(
            task_id="LONG-A",
            robot_id="R1",
            source_node=1,
            target_node=2,
            start_time_step=0,
            end_time_step=20,
            priority=1,
        ),
        ScheduledTask(
            task_id="LONG-B",
            robot_id="R2",
            source_node=1,
            target_node=2,
            start_time_step=0,
            end_time_step=20,
            priority=2,
        ),
    ]

    _, collision, simulation = _plan(problem, tasks)

    edge_resolutions = [
        row
        for row in collision.metadata["resolution_events"]
        if row.get("conflict_type") == "EDGE_CAPACITY"
    ]
    assert edge_resolutions
    assert all(row["robot_id"] == "R2" for row in edge_resolutions)
    assert all(row["blocked_by_robot_id"] == "R1" for row in edge_resolutions)
    assert simulation.conflict_count == 0

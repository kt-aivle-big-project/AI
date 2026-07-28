from __future__ import annotations

from app.services.command_language import parse_deterministic_command
from app.services.local_optimizer import LocalOptimizer
from app.services.response_view import shape_planning_response


P16_3_3_COMMAND = (
    "R2-03의 배터리가 현재 24.5%라고 가정해. 출고 작업을 완료한 뒤 "
    "C 입고 작업까지 수행하면 최소 배터리 20% 아래로 내려갈 것으로 "
    "예상되는 경우, 출고 작업 종료 후 active CHARGER 노드 중 비용이 "
    "가장 낮은 충전소에서 필요한 만큼만 충전하고 다음 작업에 투입해줘."
)


def _optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=5,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
        battery_safety_margin_percent=0.5,
    )


def _graph_problem() -> tuple[dict, dict[int, list[tuple[int, float, float]]]]:
    # Battery at the charger-selection point mirrors the real Swagger failure.
    # 2151 is cheapest but arrives at 19.994%; 2159 arrives at 20.509%.
    nodes = [
        {"node_id": 2146, "node_type": "OUTBOUND", "active": True},
        {"node_id": 2139, "node_type": "INBOUND", "active": True},
        {"node_id": 2088, "node_type": "STORAGE", "active": True},
        {"node_id": 2151, "node_type": "CHARGER", "active": True, "charging_cost": 1.0},
        {"node_id": 2152, "node_type": "CHARGER", "active": True, "charging_cost": 1.5},
        {"node_id": 2159, "node_type": "CHARGER", "active": True},
    ]
    edges = [
        {"from_node": 2146, "to_node": 2151, "distance": 19.46, "travel_seconds": 20, "direction": "BOTH", "active": True},
        {"from_node": 2146, "to_node": 2152, "distance": 18.22, "travel_seconds": 19, "direction": "BOTH", "active": True},
        {"from_node": 2146, "to_node": 2159, "distance": 9.16, "travel_seconds": 10, "direction": "BOTH", "active": True},
        {"from_node": 2151, "to_node": 2139, "distance": 9.62, "travel_seconds": 10, "direction": "BOTH", "active": True},
        {"from_node": 2152, "to_node": 2139, "distance": 8.38, "travel_seconds": 9, "direction": "BOTH", "active": True},
        {"from_node": 2159, "to_node": 2139, "distance": 2.0, "travel_seconds": 2, "direction": "BOTH", "active": True},
        {"from_node": 2139, "to_node": 2088, "distance": 7.63, "travel_seconds": 8, "direction": "BOTH", "active": True},
    ]
    problem = {
        "nodes": nodes,
        "edges": edges,
        "min_robot_battery": 20,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80,
        "charge_rate_percent_per_minute": 5,
    }
    return problem, _optimizer()._graph(problem)


def test_command_parser_marks_path_wide_battery_constraint() -> None:
    parsed = parse_deterministic_command(P16_3_3_COMMAND)
    assert "MINIMUM_REQUIRED_CHARGE" in parsed.hard_constraints
    assert "MINIMUM_BATTERY_AT_ALL_TIMES" in parsed.hard_constraints


def test_real_failure_shape_rejects_cheap_unsafe_charger_and_selects_safe_fallback() -> None:
    optimizer = _optimizer()
    problem, graph = _graph_problem()
    option = optimizer._charge_option(
        graph,
        problem,
        robot_node=2146,
        battery=20.967,
        source=2139,
        target=2088,
        operation=(7.63, 8.0),
    )

    assert option is not None
    assert option["charger_node"] == 2159
    assert option["selection_policy"] == "SAFE_DISTANCE_FALLBACK_NO_COST_DATA"
    assert option["target_battery"] == 80
    assert option["battery_at_charger"] >= 20.5

    by_node = {row["charger_node"]: row for row in option["candidates"]}
    assert by_node[2151]["safe_reachable"] is False
    assert by_node[2151]["battery_at_charger"] == 19.994
    assert by_node[2152]["safe_reachable"] is False
    assert by_node[2159]["safe_reachable"] is True
    assert by_node[2159]["selected"] is True


def test_full_view_compresses_repeated_charge_timeline() -> None:
    raw_timeline = [
        {"time_step": step, "robot_id": "R2-03", "event": "CHARGE", "node_id": 2159}
        for step in range(100, 244)
    ]
    response = {
        "status": "SIMULATION_SUCCESS",
        "report_detail_level": "DEBUG",
        "simulation": {
            "metrics": {"time_step_seconds": 5},
            "timeline": raw_timeline,
        },
        "data": {"timeline": list(raw_timeline)},
    }

    shaped = shape_planning_response(response, "FULL")
    assert shaped["response_schema_version"] == "p16.5.12.1"
    assert shaped["simulation"]["timeline_count_raw"] == 144
    assert shaped["simulation"]["timeline_count_compressed"] == 1
    charge = shaped["simulation"]["timeline"][0]
    assert charge["event"] == "CHARGE"
    assert charge["duration_steps"] == 144
    assert charge["duration_seconds"] == 720

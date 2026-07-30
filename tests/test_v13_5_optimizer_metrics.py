"""v13.5 separates global solver cost from per-vehicle timing metrics."""
from __future__ import annotations

from app.services.optimization_service import CuOptNativeResponseParser
from scripts.run_v13_mixed_batch_scenario import build_problem
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"


def _payload():
    return build_problem(FIXTURE)[1]


def _raw_response() -> dict:
    return {
        "response": {
            "solver_response": {
                "status": 0,
                "solution_cost": 28.09999918937683,
                "objective_values": {"cost": 28.09999918937683},
                "vehicle_data": {
                    "R002": {
                        "task_id": ["Depot", "ORD-004_PICK", "ORD-004_DROP"],
                        "arrival_stamp": [0.0, 4087.0, 21623.0],
                        "route": [6, 7, 8, 93, 19, 101, 30, 31, 110, 42, 43, 145, 146, 147, 148],
                        "type": ["Depot", "w", "w", "Pickup", "w", "w", "w", "w", "w", "w", "w", "w", "w", "w", "Delivery"],
                    },
                    "R004": {
                        "task_id": [
                            "Depot",
                            "ORD-003_PICK",
                            "ORD-001_PICK",
                            "ORD-002_PICK",
                            "ORD-001_DROP",
                            "ORD-002_DROP",
                            "ORD-003_DROP",
                        ],
                        "arrival_stamp": [0.0, 2512.0, 12661.0, 16086.0, 28323.0, 30823.0, 33298.0],
                        "route": [],
                        "type": [],
                    },
                    "R005": {
                        "task_id": ["Depot", "IN-001_PICK", "IN-001_DROP"],
                        "arrival_stamp": [0.0, 1925.0, 9988.0],
                        "route": [],
                        "type": [],
                    },
                    "R006": {
                        "task_id": ["Depot", "IN-002_PICK", "IN-003_PICK", "IN-002_DROP", "IN-003_DROP"],
                        "arrival_stamp": [0.0, 0.0, 1500.0, 13287.0, 21537.0],
                        "route": [],
                        "type": [],
                    },
                },
                "dropped_tasks": {"task_id": []},
            }
        }
    }


def test_cuopt_global_cost_is_not_copied_to_each_route() -> None:
    result = CuOptNativeResponseParser().parse(_raw_response(), _payload())

    assert result.status == "success"
    assert result.global_objective_cost == 28.09999918937683
    assert result.estimated_makespan_ms == 34898.0
    assert result.objective_values[0].name == "cost"

    by_vehicle = {route.vehicle_id: route for route in result.routes}
    assert by_vehicle["R002"].route_cost is None
    assert by_vehicle["R002"].last_task_arrival_ms == 21623.0
    assert by_vehicle["R002"].completion_ms == 23423.0
    assert by_vehicle["R004"].last_task_arrival_ms == 33298.0
    assert by_vehicle["R004"].completion_ms == 34898.0
    assert by_vehicle["R005"].completion_ms == 11588.0
    assert by_vehicle["R006"].completion_ms == 23337.0

    dumped = result.model_dump(mode="json")
    assert all("objective_cost" not in route for route in dumped["routes"])


def test_final_depot_stamp_is_not_mistaken_for_task_completion() -> None:
    raw = _raw_response()
    vehicle = raw["response"]["solver_response"]["vehicle_data"]["R002"]
    vehicle["task_id"].append("Depot")
    vehicle["arrival_stamp"].append(30000.0)

    result = CuOptNativeResponseParser().parse(raw, _payload())
    route = next(value for value in result.routes if value.vehicle_id == "R002")
    assert route.last_task_arrival_ms == 21623.0
    assert route.completion_ms == 23423.0

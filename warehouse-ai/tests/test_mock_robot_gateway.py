from datetime import datetime
import os

import pytest
from fastapi.testclient import TestClient

from app.models import RobotCommandBatch

from mock_robot_gateway import (
    _real_execution_events,
    _real_execution_events_from_batches,
    app,
)


client = TestClient(app)


def setup_function() -> None:
    os.environ["MOCK_GATEWAY_AUTO_EXECUTE"] = "false"
    client.delete("/received-plans")


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mock-robot-gateway",
    }


def test_dispatch_records_plan_and_route_counts() -> None:
    payload = {
        "plan_version": "PLAN-001",
        "plan": {
            "command_id": "COMMAND-001",
            "collision_plan": {
                "routes": [
                    {"robot_id": "R1", "waypoints": [1, 2]},
                    {"robot_id": "R1", "waypoints": [2, 3]},
                    {"robot_id": "R2", "waypoints": [4, 5]},
                ]
            },
        },
    }

    response = client.post("/dispatch", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "status": "DISPATCH_ACCEPTED",
        "plan_version": "PLAN-001",
        "received_robot_count": 2,
        "message": "Mock Robot Gateway가 계획을 정상 수신했습니다.",
    }

    listing = client.get("/received-plans")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    assert body["plans"][0]["plan_version"] == "PLAN-001"
    assert body["plans"][0]["command_id"] == "COMMAND-001"
    assert body["plans"][0]["received_robot_count"] == 2
    assert body["plans"][0]["robot_route_counts"] == {"R1": 2, "R2": 1}
    assert body["plans"][0]["plan"] == payload["plan"]
    datetime.fromisoformat(body["plans"][0]["received_at"])


def test_dispatch_records_standard_robot_command_batches() -> None:
    payload = {
        "plan_version": "PLAN-002",
        "batches": [
            {
                "plan_version": "PLAN-002",
                "warehouse_id": 1,
                "robot_id": "R1",
                "command_count": 1,
                "commands": [
                    {
                        "command_id": "COMMAND-1",
                        "sequence": 1,
                        "plan_version": "PLAN-002",
                        "warehouse_id": 1,
                        "robot_id": "R1",
                        "task_id": None,
                        "work_id": None,
                        "action": "START",
                        "node_id": 10,
                        "time_step": 0,
                        "time_step_seconds": 5,
                        "payload": {},
                    }
                ],
            }
        ],
    }

    response = client.post("/dispatch", json=payload)

    assert response.status_code == 200
    assert response.json()["received_robot_count"] == 1
    assert response.json()["received_command_count"] == 1
    stored = client.get("/received-plans").json()["plans"][0]
    assert stored["plan"] is None
    assert stored["robot_command_batches"][0]["robot_id"] == "R1"


def test_delete_received_plans_clears_memory() -> None:
    client.post(
        "/dispatch",
        json={"plan_version": "PLAN-001", "plan": {}},
    )

    response = client.delete("/received-plans")

    assert response.status_code == 200
    assert response.json() == {"status": "cleared", "deleted_count": 1}
    assert client.get("/received-plans").json() == {"count": 0, "plans": []}


@pytest.mark.parametrize(
    "payload",
    [
        {"plan": {}},
        {"plan_version": "PLAN-001"},
        {"plan_version": "", "plan": {}},
        {"plan_version": "PLAN-001", "plan": []},
    ],
)
def test_dispatch_rejects_invalid_requests(payload: dict) -> None:
    response = client.post("/dispatch", json=payload)

    assert response.status_code == 422
    assert client.get("/received-plans").json()["count"] == 0


def test_mock_auto_execution_events_are_always_real() -> None:
    events = _real_execution_events(
        {
            "warehouse_id": 1,
            "required_tasks": [{"task_id": "T1", "action": "MOVE"}],
            "cuopt_plan": {
                "scheduled_tasks": [
                    {
                        "task_id": "T1",
                        "work_id": "W1",
                        "robot_id": "R1",
                        "source_node": 1,
                        "target_node": 2,
                        "start_time_step": 0,
                        "end_time_step": 1,
                    }
                ]
            },
            "collision_plan": {
                "routes": [
                    {
                        "robot_id": "R1",
                        "waypoints": [
                            {"node_id": 1, "time_step": 0},
                            {"node_id": 2, "time_step": 1},
                        ],
                    }
                ]
            },
        }
    )

    assert {event["event_type"] for event in events} == {
        "TASK_STARTED",
        "POSITION_UPDATED",
        "TASK_COMPLETED",
    }
    assert all(event["execution_context"] == "REAL" for event in events)
    assert all(event["simulation_id"] is None for event in events)



def _standard_outbound_batch() -> dict:
    return {
        "plan_version": "PLAN-BATCH-1",
        "warehouse_id": 1,
        "robot_id": "R1",
        "command_count": 7,
        "commands": [
            {
                "command_id": "C1",
                "sequence": 1,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": None,
                "work_id": None,
                "action": "START",
                "node_id": 10,
                "time_step": 0,
                "time_step_seconds": 5,
                "payload": {},
            },
            {
                "command_id": "C2",
                "sequence": 2,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": "T1",
                "work_id": "W1",
                "action": "MOVE",
                "node_id": 20,
                "time_step": 1,
                "time_step_seconds": 5,
                "payload": {},
            },
            {
                "command_id": "C3",
                "sequence": 3,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": "T1",
                "work_id": "W1",
                "action": "PICKUP",
                "node_id": 20,
                "time_step": 1,
                "time_step_seconds": 5,
                "payload": {
                    "lot_allocations": [
                        {"warehouse_item_id": "LOT-A", "quantity_boxes": 30},
                        {"warehouse_item_id": "LOT-B", "quantity": 20},
                    ]
                },
            },
            {
                "command_id": "C4",
                "sequence": 4,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": None,
                "work_id": None,
                "action": "MOVE",
                "node_id": 30,
                "time_step": 2,
                "time_step_seconds": 5,
                "payload": {},
            },
            {
                "command_id": "C5",
                "sequence": 5,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": "T1",
                "work_id": "W1",
                "action": "MOVE",
                "node_id": 40,
                "time_step": 3,
                "time_step_seconds": 5,
                "payload": {},
            },
            {
                "command_id": "C6",
                "sequence": 6,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": "T1",
                "work_id": "W1",
                "action": "DROPOFF",
                "node_id": 40,
                "time_step": 3,
                "time_step_seconds": 5,
                "payload": {},
            },
            {
                "command_id": "C7",
                "sequence": 7,
                "plan_version": "PLAN-BATCH-1",
                "warehouse_id": 1,
                "robot_id": "R1",
                "task_id": None,
                "work_id": None,
                "action": "STOP",
                "node_id": 40,
                "time_step": 3,
                "time_step_seconds": 5,
                "payload": {},
            },
        ],
    }


def test_standard_batches_generate_real_completion_with_inventory_deltas() -> None:
    batch = RobotCommandBatch.model_validate(_standard_outbound_batch())

    events = _real_execution_events_from_batches([batch])

    assert {event["event_type"] for event in events} == {
        "POSITION_UPDATED",
        "TASK_STARTED",
        "TASK_COMPLETED",
    }
    completion = next(
        event for event in events if event["event_type"] == "TASK_COMPLETED"
    )
    assert completion["work_id"] == "W1"
    assert completion["task_id"] == "T1"
    assert completion["payload"]["plan_version"] == "PLAN-BATCH-1"
    assert completion["inventory_deltas"] == [
        {"warehouse_item_id": "LOT-A", "quantity_delta": -30},
        {"warehouse_item_id": "LOT-B", "quantity_delta": -20},
    ]
    assert all(event["execution_context"] == "REAL" for event in events)
    assert all(event["simulation_id"] is None for event in events)


def test_auto_execute_replays_events_for_standard_batches(monkeypatch) -> None:
    delivered: dict[str, object] = {}

    def capture(record: dict, events: list[dict]) -> None:
        delivered["record"] = record
        delivered["events"] = events

    monkeypatch.setattr("mock_robot_gateway._send_real_execution_events", capture)
    os.environ["MOCK_GATEWAY_AUTO_EXECUTE"] = "true"

    response = client.post(
        "/dispatch",
        json={"plan_version": "PLAN-BATCH-1", "batches": [_standard_outbound_batch()]},
    )

    assert response.status_code == 200
    events = delivered["events"]
    assert isinstance(events, list)
    assert any(event["event_type"] == "TASK_COMPLETED" for event in events)
    stored = client.get("/received-plans").json()["plans"][0]
    assert stored["auto_execute"] is True
    assert stored["generated_event_count"] == len(events)


def test_gateway_dispatch_idempotency_prevents_duplicate_recording() -> None:
    payload = {
        "dispatch_id": "D-15",
        "idempotency_key": "D-15",
        "payload_fingerprint": "FP-15",
        "plan_version": "PLAN-15",
        "batches": [
            {
                "plan_version": "PLAN-15",
                "warehouse_id": 1,
                "robot_id": "R1",
                "command_count": 1,
                "commands": [
                    {
                        "command_id": "C-15",
                        "sequence": 1,
                        "plan_version": "PLAN-15",
                        "warehouse_id": 1,
                        "robot_id": "R1",
                        "task_id": None,
                        "work_id": None,
                        "action": "START",
                        "node_id": 10,
                        "time_step": 0,
                        "time_step_seconds": 5,
                        "payload": {},
                    }
                ],
            }
        ],
    }
    first = client.post("/dispatch", json=payload)
    second = client.post("/dispatch", json=payload)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert client.get("/received-plans").json()["count"] == 1


def test_gateway_rejects_same_idempotency_key_with_different_payload() -> None:
    base = {
        "dispatch_id": "D-15",
        "idempotency_key": "D-15",
        "payload_fingerprint": "FP-15",
        "plan_version": "PLAN-15",
        "plan": {},
    }
    assert client.post("/dispatch", json=base).status_code == 200
    conflicting = {**base, "payload_fingerprint": "FP-OTHER"}
    response = client.post("/dispatch", json=conflicting)
    assert response.status_code == 409
    assert client.get("/received-plans").json()["count"] == 1


def test_gateway_cancel_is_idempotent() -> None:
    payload = {
        "dispatch_id": "D-15",
        "idempotency_key": "D-15",
        "payload_fingerprint": "FP-15",
        "plan_version": "PLAN-15",
        "plan": {},
    }
    client.post("/dispatch", json=payload)
    first = client.post(
        "/dispatches/D-15/cancel",
        json={"plan_version": "PLAN-15", "reason": "operator cancel"},
    )
    second = client.post(
        "/dispatches/D-15/cancel",
        json={"plan_version": "PLAN-15", "reason": "operator cancel"},
    )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

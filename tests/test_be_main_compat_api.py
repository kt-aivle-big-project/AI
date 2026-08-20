from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.config import Settings
from app.domain.be_compat import (
    BeGraphSnapshot,
    BeOptimizationRequest,
    BeReoptimizationRequest,
)
from app.services.be_compat_service import BeCompatOptimizationService, BeCompatRoutingError


@dataclass
class FakeRepository:
    snapshot: BeGraphSnapshot | None = None

    def save_graph(self, *, warehouse_id, nodes, edges):
        self.snapshot = BeGraphSnapshot(
            warehouseId=warehouse_id,
            graphVersion="test-version",
            nodes=nodes,
            edges=edges,
        )
        return self.snapshot

    def require_graph(self, warehouse_id):
        assert self.snapshot is not None
        assert self.snapshot.warehouse_id == warehouse_id
        return self.snapshot

    def record_run(self, **kwargs):
        return None


def _settings() -> Settings:
    return Settings(
        WAREHOUSE_REPOSITORY_BACKEND="json",
        BE_COMPAT_ROBOT_SPEED_DISTANCE_PER_SECOND=2.0,
        BE_COMPAT_MIN_BATTERY_PCT=30,
        BE_COMPAT_RUNTIME_SOURCE="request_only",
    )


def _initial_payload() -> dict:
    return {
        "warehouseId": 1,
        "robots": [
            {
                "robotId": 10,
                "currentNodeId": 1,
                "targetNodeId": 4,
                "batteryLevel": 90.0,
            }
        ],
        "nodes": [
            {"nodeId": 1, "x": 0.0, "y": 0.0},
            {"nodeId": 2, "x": 1.0, "y": 0.0},
            {"nodeId": 3, "x": 2.0, "y": 0.0},
            {"nodeId": 4, "x": 3.0, "y": 0.0},
        ],
        "edges": [
            {
                "edgeId": 101,
                "fromNodeId": 1,
                "toNodeId": 2,
                "distance": 1.0,
                "directionType": "BOTH",
            },
            {
                "edgeId": 102,
                "fromNodeId": 2,
                "toNodeId": 3,
                "distance": 1.0,
                "directionType": "A_TO_B",
            },
            {
                "edgeId": 103,
                "fromNodeId": 3,
                "toNodeId": 4,
                "distance": 2.0,
                "directionType": "BOTH",
            },
        ],
    }


def test_optimize_matches_unmodified_spring_camel_case_contract() -> None:
    repository = FakeRepository()
    service = BeCompatOptimizationService(repository=repository, settings=_settings())
    request = BeOptimizationRequest.model_validate(_initial_payload())

    response = service.optimize(request)
    payload = response.model_dump(by_alias=True, mode="json")

    assert payload["status"] == "success"
    assert payload["requestId"].startswith("OPT-W1-")
    assert payload["routes"] == [
        {
            "robotId": 10,
            "nodePath": [1, 2, 3, 4],
            "totalDistance": 4.0,
            "estimatedTime": 2.0,
        }
    ]


def test_direction_type_is_respected() -> None:
    payload = _initial_payload()
    payload["robots"][0]["currentNodeId"] = 3
    payload["robots"][0]["targetNodeId"] = 2
    repository = FakeRepository()
    service = BeCompatOptimizationService(repository=repository, settings=_settings())

    with pytest.raises(BeCompatRoutingError):
        service.optimize(BeOptimizationRequest.model_validate(payload))


def test_reoptimize_assigns_tasks_and_excludes_low_battery_robot() -> None:
    repository = FakeRepository()
    service = BeCompatOptimizationService(repository=repository, settings=_settings())
    service.optimize(BeOptimizationRequest.model_validate(_initial_payload()))

    request = BeReoptimizationRequest.model_validate(
        {
            "simulationRunId": 77,
            "warehouseId": 1,
            "reason": "NEW_TASK_ADDED",
            "triggerRobotId": None,
            "blockedEdgeIds": [],
            "description": "test",
            "robots": [
                {
                    "robotId": 10,
                    "currentNodeId": 1,
                    "batteryLevel": 10.0,
                    "status": "IDLE",
                },
                {
                    "robotId": 11,
                    "currentNodeId": 1,
                    "batteryLevel": 90.0,
                    "status": "IDLE",
                },
            ],
            "remainingTasks": [
                {
                    "taskId": 501,
                    "assignedRobotId": None,
                    "startNodeId": 2,
                    "endNodeId": 4,
                    "taskType": "OUTBOUND",
                    "status": "PENDING",
                }
            ],
        }
    )

    response = service.reoptimize(request)
    payload = response.model_dump(by_alias=True, mode="json")

    assert payload["status"] == "success"
    assert payload["assignments"] == [{"taskId": 501, "robotId": 11}]
    assert payload["routes"][0]["robotId"] == 11
    assert payload["routes"][0]["nodePath"] == [1, 2, 3, 4]


def test_reoptimize_preserves_in_progress_assignment_when_robot_is_eligible() -> None:
    repository = FakeRepository()
    service = BeCompatOptimizationService(repository=repository, settings=_settings())
    service.optimize(BeOptimizationRequest.model_validate(_initial_payload()))

    request = BeReoptimizationRequest.model_validate(
        {
            "simulationRunId": 78,
            "warehouseId": 1,
            "reason": "MANUAL_REQUEST",
            "robots": [
                {
                    "robotId": 10,
                    "currentNodeId": 1,
                    "batteryLevel": 90.0,
                    "status": "MOVING",
                },
                {
                    "robotId": 11,
                    "currentNodeId": 2,
                    "batteryLevel": 90.0,
                    "status": "IDLE",
                },
            ],
            "remainingTasks": [
                {
                    "taskId": 502,
                    "assignedRobotId": 10,
                    "startNodeId": 2,
                    "endNodeId": 4,
                    "taskType": "OUTBOUND",
                    "status": "IN_PROGRESS",
                }
            ],
        }
    )

    response = service.reoptimize(request)
    assert response.assignments[0].robot_id == 10


def test_reoptimize_returns_no_eligible_robot_without_fake_assignment() -> None:
    repository = FakeRepository()
    service = BeCompatOptimizationService(repository=repository, settings=_settings())
    service.optimize(BeOptimizationRequest.model_validate(_initial_payload()))

    request = BeReoptimizationRequest.model_validate(
        {
            "simulationRunId": 79,
            "warehouseId": 1,
            "reason": "ROBOT_FAILURE",
            "triggerRobotId": 10,
            "robots": [
                {
                    "robotId": 10,
                    "currentNodeId": 1,
                    "batteryLevel": 90.0,
                    "status": "ERROR",
                }
            ],
            "remainingTasks": [
                {
                    "taskId": 503,
                    "assignedRobotId": 10,
                    "startNodeId": 2,
                    "endNodeId": 4,
                    "taskType": "OUTBOUND",
                    "status": "IN_PROGRESS",
                }
            ],
        }
    )

    response = service.reoptimize(request)
    assert response.status == "no_eligible_robot"
    assert response.assignments == []
    assert response.routes == []

from fastapi.testclient import TestClient

import app.api as api_module
from app.config import Settings


client = TestClient(api_module.app)


def _nodes() -> list[dict]:
    return [
        {"nodeId": 1, "x": 0.0, "y": 0.0},
        {"nodeId": 2, "x": 1.0, "y": 0.0},
        {"nodeId": 3, "x": 2.0, "y": 0.0},
    ]


def _edges() -> list[dict]:
    return [
        {
            "edgeId": 10,
            "fromNodeId": 1,
            "toNodeId": 2,
            "distance": 1.0,
            "directionType": "BOTH",
        },
        {
            "edgeId": 11,
            "fromNodeId": 2,
            "toNodeId": 3,
            "distance": 1.0,
            "directionType": "BOTH",
        },
    ]


def _backend_map() -> dict[str, list[dict]]:
    return {
        "nodes": [
            {
                "node_id": node["nodeId"],
                "warehouse_id": 1,
                "node_type": "ROUTE",
                "x": node["x"],
                "y": node["y"],
                "active": True,
            }
            for node in _nodes()
        ],
        "edges": [
            {
                "edge_id": str(edge["edgeId"]),
                "from_node": edge["fromNodeId"],
                "to_node": edge["toNodeId"],
                "distance": edge["distance"],
                "travel_seconds": edge["distance"],
                "direction": "BOTH",
                "active": True,
            }
            for edge in _edges()
        ],
    }


def test_optimize_endpoint_matches_spring_contract() -> None:
    response = client.post(
        "/optimize",
        json={
            "warehouseId": 1,
            "robots": [
                {
                    "robotId": 1,
                    "currentNodeId": 1,
                    "targetNodeId": 3,
                    "batteryLevel": 100.0,
                }
            ],
            "nodes": _nodes(),
            "edges": _edges(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"requestId", "status", "routes"}
    assert payload["status"] == "COMPLETED"
    assert payload["routes"] == [
        {
            "robotId": 1,
            "nodePath": [1, 2, 3],
            "totalDistance": 2.0,
            "estimatedTime": 10.0,
        }
    ]


def test_optimize_honors_backend_b_to_a_direction() -> None:
    response = client.post(
        "/optimize",
        json={
            "warehouseId": 1,
            "robots": [
                {
                    "robotId": 7,
                    "currentNodeId": 2,
                    "targetNodeId": 1,
                    "batteryLevel": 100.0,
                }
            ],
            "nodes": _nodes()[:2],
            "edges": [
                {
                    "edgeId": 20,
                    "fromNodeId": 1,
                    "toNodeId": 2,
                    "distance": 1.0,
                    "directionType": "B_TO_A",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["routes"][0]["nodePath"] == [2, 1]


def test_reoptimize_loads_backend_map_and_returns_assignments(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api_module,
        "load_backend_map",
        lambda warehouse_id, settings: _backend_map(),
    )
    response = client.post(
        "/reoptimize",
        json={
            "simulationRunId": 91,
            "warehouseId": 1,
            "reason": "NEW_TASK_ADDED",
            "triggerRobotId": None,
            "blockedEdgeIds": [],
            "description": "integration test",
            "robots": [
                {
                    "robotId": 1,
                    "currentNodeId": 1,
                    "batteryLevel": 100.0,
                    "status": "AVAILABLE",
                },
                {
                    "robotId": 2,
                    "currentNodeId": 3,
                    "batteryLevel": 100.0,
                    "status": "FAILED",
                },
            ],
            "remainingTasks": [
                {
                    "taskId": 501,
                    "assignedRobotId": None,
                    "startNodeId": 1,
                    "endNodeId": 3,
                    "taskType": "MOVE",
                    "status": "PENDING",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "COMPLETED"
    assert payload["assignments"] == [{"taskId": 501, "robotId": 1}]
    assert payload["routes"][0]["nodePath"] == [1, 2, 3]


def test_reoptimize_marks_unreachable_task_partial(monkeypatch) -> None:
    monkeypatch.setattr(
        api_module,
        "load_backend_map",
        lambda warehouse_id, settings: _backend_map(),
    )
    response = client.post(
        "/reoptimize",
        json={
            "simulationRunId": 92,
            "warehouseId": 1,
            "reason": "OBSTACLE_DETECTED",
            "blockedEdgeIds": [11],
            "robots": [
                {
                    "robotId": 1,
                    "currentNodeId": 1,
                    "batteryLevel": 100.0,
                    "status": "AVAILABLE",
                }
            ],
            "remainingTasks": [
                {
                    "taskId": 502,
                    "assignedRobotId": None,
                    "startNodeId": 1,
                    "endNodeId": 3,
                    "taskType": "MOVE",
                    "status": "PENDING",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PARTIAL"
    assert response.json()["assignments"] == []
    assert response.json()["routes"] == []


def test_component_style_backend_environment_builds_connection_urls() -> None:
    settings = Settings(
        _env_file=None,
        postgres_db="warehouse",
        postgres_user="warehouse_app",
        postgres_password="example password",
        neo4j_password="example password",
        redis_password="example password",
    )

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert "example+password" in settings.database_url
    assert settings.postgres_schema_profile == "backend_laro"
    assert settings.neo4j_uri == ""
    assert "NEO4J_URI" in settings.missing_for_connections()
    assert settings.redis_url.startswith("redis://:")


def test_explicit_aura_uri_has_priority_and_enables_tls() -> None:
    uri = "neo4j+s://your-database-id.databases.neo4j.io"
    settings = Settings(
        _env_file=None,
        neo4j_uri=uri,
        neo4j_user="neo4j",
        neo4j_database="neo4j",
    )

    assert settings.neo4j_uri == uri
    assert settings.neo4j_uses_tls is True
    assert settings.neo4j_database == "neo4j"

    self_signed = Settings(
        _env_file=None,
        neo4j_uri="neo4j+ssc://your-database-id.databases.neo4j.io",
    )
    assert self_signed.neo4j_uses_tls is True

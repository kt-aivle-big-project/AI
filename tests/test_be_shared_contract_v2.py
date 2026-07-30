from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.domain.be_compat import (
    BeGraphSnapshot,
    BeOptimizationRequest,
    BeReoptimizationRequest,
)
from app.repositories.be_compat_repository import BeCompatRepository
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository
from app.services.be_compat_service import BeCompatOptimizationService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, **kwargs):
        self.values[key] = value
        return True

    def sadd(self, key: str, *values: str):
        current = self.sets.setdefault(key, set())
        before = len(current)
        current.update(str(value) for value in values)
        return len(current) - before

    def smembers(self, key: str):
        return set(self.sets.get(key, set()))

    def delete(self, *keys: str):
        count = 0
        for key in keys:
            count += int(key in self.values or key in self.sets)
            self.values.pop(key, None)
            self.sets.pop(key, None)
        return count

    def mget(self, keys):
        return [self.get(key) for key in keys]

    def scan_iter(self, match: str):
        for key in sorted(set(self.values) | set(self.sets)):
            if fnmatch.fnmatch(key, match):
                yield key


@dataclass
class FakeGraphRepository:
    snapshot: BeGraphSnapshot

    def require_graph(self, warehouse_id: int) -> BeGraphSnapshot:
        assert warehouse_id == self.snapshot.warehouse_id
        return self.snapshot

    def save_graph(self, *, warehouse_id, nodes, edges):
        return self.snapshot

    def record_run(self, **kwargs):
        return None


def _settings(**overrides) -> Settings:
    values = {
        "WAREHOUSE_REPOSITORY_BACKEND": "json",
        "BE_COMPAT_RUNTIME_SOURCE": "request_only",
        "BE_COMPAT_GRAPH_SOURCE": "auto",
        "BE_COMPAT_GRAPH_CACHE_MODE": "metadata",
        "BE_COMPAT_MIN_BATTERY_PCT": 30,
    }
    values.update(overrides)
    return Settings(**values)


def _snapshot() -> BeGraphSnapshot:
    request = BeOptimizationRequest.model_validate(
        {
            "warehouseId": 1,
            "robots": [{"robotId": 1, "currentNodeId": 1, "targetNodeId": 4}],
            "nodes": [
                {"nodeId": 1, "x": 0, "y": 0},
                {"nodeId": 2, "x": 1, "y": 0},
                {"nodeId": 3, "x": 0, "y": 1},
                {"nodeId": 4, "x": 2, "y": 0},
            ],
            "edges": [
                {"edgeId": 101, "fromNodeId": 1, "toNodeId": 2, "distance": 1, "directionType": "BOTH"},
                {"edgeId": 102, "fromNodeId": 1, "toNodeId": 3, "distance": 1, "directionType": "BOTH"},
                {"edgeId": 103, "fromNodeId": 3, "toNodeId": 2, "distance": 1, "directionType": "BOTH"},
                {"edgeId": 104, "fromNodeId": 2, "toNodeId": 4, "distance": 1, "directionType": "BOTH"},
            ],
        }
    )
    return BeGraphSnapshot(
        warehouseId=1,
        graphVersion=BeCompatRepository.graph_version(request.nodes, request.edges),
        nodes=request.nodes,
        edges=request.edges,
    )


def test_contract_sql_is_additive_and_contains_required_structures() -> None:
    path = Path(__file__).resolve().parents[1] / "db" / "postgres" / "003_be_shared_contract.sql"
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "create schema if not exists laro_contract" in lowered
    for name in (
        "warehouse_binding",
        "route_node",
        "route_edge",
        "rack",
        "rack_slot",
        "handling_unit",
        "outbound_order",
        "inbound_receipt",
        "facility",
        "request_log",
    ):
        assert f"laro_contract.{name}" in lowered
    assert "alter table public." not in lowered
    assert "drop table public." not in lowered
    assert "delete from public." not in lowered
    assert "refresh_spring_views" in lowered

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    # Fresh v2 stacks must not recreate the legacy duplicate public graph store.
    assert "002_be_compat_schema.sql" not in compose
    assert "003_be_shared_contract.sql" in compose


def test_fresh_v2_schema_bootstrap_does_not_create_legacy_public_snapshot_tables() -> None:
    class Postgres:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def apply_schema(self, path) -> None:
            self.paths.append(Path(path).name)

    class Manager:
        def __init__(self) -> None:
            self.postgres = Postgres()

    manager = Manager()
    repository = BeCompatRepository(
        settings=_settings(WAREHOUSE_REPOSITORY_BACKEND="live"),
        manager=manager,
    )
    repository.ensure_schema()
    repository.ensure_schema()
    assert manager.postgres.paths == ["003_be_shared_contract.sql"]


def test_spring_redis_runtime_without_extension_is_explicit_compatibility_mode() -> None:
    redis = FakeRedis()
    repo = BeSpringRuntimeRepository(settings=_settings(), client=redis)
    redis.sadd(repo.robot_ids_key(77), "10")
    redis.set(
        repo.robot_state_key(77, 10),
        json.dumps(
            {
                "robotId": 10,
                "warehouseId": 1,
                "currentNodeId": 1,
                "currentNodeCode": "R1_1",
                "nextNodeId": None,
                "nextNodeCode": None,
                "arrivalInSeconds": None,
                "batteryLevel": 88,
                "status": "IDLE",
                "currentTaskId": None,
                "updatedAt": None,
            }
        ),
    )

    snapshot = repo.snapshot(77)
    assert snapshot.mode == "COMPATIBILITY"
    assert snapshot.robots[0].robot_id == 10
    assert snapshot.robots[0].compatibility_mode is True
    assert snapshot.meta is not None and snapshot.meta.compatibility_mode is True


def test_spring_redis_runtime_extension_enables_full_mode_and_edge_block() -> None:
    redis = FakeRedis()
    repo = BeSpringRuntimeRepository(settings=_settings(), client=redis)
    repo.bootstrap(
        78,
        warehouse_id=1,
        robots=[
            {
                "robotId": 11,
                "currentNodeId": 1,
                "batteryLevel": 90,
                "status": "IDLE",
            }
        ],
        sim_time_ms=3000,
    )
    redis.set(
        repo.robot_extension_key(78, 11),
        json.dumps(
            {
                "schemaVersion": 1,
                "simTimeMs": 3000,
                "stateVersion": 2,
                "activePlanId": "PLAN-1",
                "activePlanVersion": 1,
                "currentStepId": "R011-0001",
                "currentStepType": "WAIT",
                "capacityUnits": 8,
                "currentLoadUnits": 0,
            }
        ),
    )
    redis.sadd(repo.edge_ids_key(78), "101")
    redis.set(
        repo.edge_state_key(78, 101),
        json.dumps({"edgeId": 101, "status": "BLOCKED"}),
    )

    snapshot = repo.snapshot(78)
    assert snapshot.mode == "FULL"
    assert snapshot.meta is not None and snapshot.meta.sim_time_ms == 3000
    assert snapshot.robots[0].compatibility_mode is False
    assert snapshot.blocked_edge_ids == [101]


def test_reoptimize_can_load_empty_robot_list_from_spring_redis_and_avoid_blocked_edge() -> None:
    redis = FakeRedis()
    runtime = BeSpringRuntimeRepository(
        settings=_settings(BE_COMPAT_RUNTIME_SOURCE="redis_only"),
        client=redis,
    )
    runtime.bootstrap(
        90,
        warehouse_id=1,
        robots=[
            {
                "robotId": 11,
                "currentNodeId": 1,
                "batteryLevel": 90,
                "status": "IDLE",
            }
        ],
    )
    redis.sadd(runtime.edge_ids_key(90), "101")
    redis.set(
        runtime.edge_state_key(90, 101),
        json.dumps({"edgeId": 101, "status": "BLOCKED"}),
    )

    settings = _settings(BE_COMPAT_RUNTIME_SOURCE="redis_only")
    service = BeCompatOptimizationService(
        repository=FakeGraphRepository(_snapshot()),
        settings=settings,
        runtime_repository=runtime,
    )
    request = BeReoptimizationRequest.model_validate(
        {
            "simulationRunId": 90,
            "warehouseId": 1,
            "reason": "NEW_TASK_ADDED",
            "robots": [],
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
    assert response.status == "success"
    assert response.assignments[0].robot_id == 11
    assert response.routes[0].node_path == [1, 3, 2, 4]
    assert service.last_runtime_source == "spring_redis_compatibility"


def test_request_robot_list_remains_authoritative_over_spring_redis() -> None:
    redis = FakeRedis()
    runtime = BeSpringRuntimeRepository(
        settings=_settings(BE_COMPAT_RUNTIME_SOURCE="request_then_redis"),
        client=redis,
    )
    runtime.bootstrap(
        91,
        warehouse_id=1,
        robots=[
            {
                "robotId": 99,
                "currentNodeId": 1,
                "batteryLevel": 99,
                "status": "IDLE",
            }
        ],
    )
    settings = _settings(BE_COMPAT_RUNTIME_SOURCE="request_then_redis")
    service = BeCompatOptimizationService(
        repository=FakeGraphRepository(_snapshot()),
        settings=settings,
        runtime_repository=runtime,
    )
    request = BeReoptimizationRequest.model_validate(
        {
            "simulationRunId": 91,
            "warehouseId": 1,
            "reason": "NEW_TASK_ADDED",
            "robots": [
                {
                    "robotId": 12,
                    "currentNodeId": 1,
                    "batteryLevel": 90,
                    "status": "IDLE",
                }
            ],
            "remainingTasks": [
                {
                    "taskId": 502,
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
    assert response.assignments[0].robot_id == 12
    assert all(value.robot_id != 99 for value in response.assignments)


def test_graph_source_prefers_matching_spring_db_and_uses_contract_on_mismatch(monkeypatch) -> None:
    settings = _settings(
        WAREHOUSE_REPOSITORY_BACKEND="live",
        BE_COMPAT_GRAPH_SOURCE="auto",
    )
    repo = BeCompatRepository(settings=settings, manager=object())
    request_snapshot = _snapshot()
    spring_snapshot = _snapshot()
    saved: list[str] = []
    monkeypatch.setattr(repo, "ensure_schema", lambda: None)
    monkeypatch.setattr(repo, "_load_spring_graph", lambda warehouse_id: spring_snapshot)
    monkeypatch.setattr(repo, "_save_contract_graph", lambda snapshot, source: saved.append(source))
    monkeypatch.setattr(repo, "_upsert_binding", lambda snapshot, source: None)
    monkeypatch.setattr(repo, "_cache_graph", lambda snapshot, source: None)
    monkeypatch.setattr(repo, "_project_to_neo4j", lambda snapshot: None)

    value = repo.save_graph(
        warehouse_id=1,
        nodes=request_snapshot.nodes,
        edges=request_snapshot.edges,
    )
    assert value.graph_version == spring_snapshot.graph_version
    assert repo.last_graph_source == "spring_db"
    assert saved == []

    changed_edges = list(request_snapshot.edges)
    changed_edges[0] = changed_edges[0].model_copy(update={"distance": 9.0})
    value = repo.save_graph(
        warehouse_id=1,
        nodes=request_snapshot.nodes,
        edges=changed_edges,
    )
    assert repo.last_graph_source == "contract"
    assert saved == ["request_snapshot"]
    assert value.graph_version != spring_snapshot.graph_version


def test_route_and_assignment_are_deterministic_across_repeated_runs() -> None:
    snapshot = _snapshot()
    settings = _settings()
    request = BeReoptimizationRequest.model_validate(
        {
            "simulationRunId": 99,
            "warehouseId": 1,
            "reason": "MANUAL_REQUEST",
            "robots": [
                {"robotId": 10, "currentNodeId": 1, "batteryLevel": 90, "status": "IDLE"},
                {"robotId": 11, "currentNodeId": 1, "batteryLevel": 90, "status": "IDLE"},
            ],
            "remainingTasks": [
                {
                    "taskId": 1,
                    "assignedRobotId": None,
                    "startNodeId": 2,
                    "endNodeId": 4,
                    "taskType": "OUTBOUND",
                    "status": "PENDING",
                },
                {
                    "taskId": 2,
                    "assignedRobotId": None,
                    "startNodeId": 3,
                    "endNodeId": 4,
                    "taskType": "INBOUND",
                    "status": "PENDING",
                },
            ],
        }
    )
    observed: set[str] = set()
    for _ in range(20):
        service = BeCompatOptimizationService(
            repository=FakeGraphRepository(snapshot),
            settings=settings,
        )
        response = service.reoptimize(request)
        canonical = response.model_dump(by_alias=True, mode="json")
        canonical.pop("requestId")
        observed.add(json.dumps(canonical, sort_keys=True))
    assert len(observed) == 1

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.models import AtomicTask
from app.services import optimizer as optimizer_module
from app.services.cuopt_rest import (
    CuOptRestError,
    CuOptRestOptimizer,
    build_cuopt_routing_payload,
)
from app.services.local_optimizer import LocalOptimizer


def base_problem() -> dict:
    return {
        "warehouse_id": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "reference_time": datetime.now(UTC).isoformat(),
        "plan_mode": "INITIAL_PLAN",
        "nodes": [{"node_id": node_id, "active": True} for node_id in (1, 2, 3, 4)],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 3, "to_node": 4, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 4, "to_node": 1, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
        ],
        "robots": [
            {"robot_id": "R1", "node_id": 1, "battery": 90, "status": "IDLE", "max_load": 100, "current_load": 0},
            {"robot_id": "R2", "node_id": 2, "battery": 80, "status": "IDLE", "max_load": 100, "current_load": 0},
            {"robot_id": "R3", "node_id": 4, "battery": 70, "status": "IDLE", "max_load": 100, "current_load": 0},
        ],
        "tasks": [
            AtomicTask(task_id="T1", action="MOVE", source_candidates=[1], target_candidates=[3], priority=1).model_dump(mode="json"),
            AtomicTask(task_id="T2", action="MOVE", source_candidates=[2], target_candidates=[4], priority=2).model_dump(mode="json"),
            AtomicTask(task_id="T3", action="MOVE", source_candidates=[4], target_candidates=[2], priority=3).model_dump(mode="json"),
        ],
        "inventory": [],
        "temporary_closures": [],
        "active_plan": None,
        "fixed_task_ids": [],
        "changeable_task_ids": [],
        "affected_robot_ids": [],
        "freeze_horizon_seconds": 0,
        "weights": {},
        "min_robot_battery": 20,
        "energy_per_distance": 0.05,
    }


def settings(**overrides):
    values = {
        "optimizer_backend": "auto",
        "cuopt_url": "",
        "cuopt_client_sak": "",
        "cuopt_api_key": "",
        "cuopt_rest_url": "https://optimize.api.nvidia.com/v1/nvidia/cuopt",
        "cuopt_status_url": "https://optimize.api.nvidia.com/v1/status/{request_id}",
        "cuopt_auto_enable": True,
        "cuopt_fallback_to_local": True,
        "cuopt_poll_timeout_seconds": 1.0,
        "cuopt_poll_interval_seconds": 0.01,
        "cuopt_solver_time_limit_seconds": 1,
        "request_timeout_seconds": 1.0,
        "time_step_seconds": 1,
        "min_robot_battery": 20.0,
        "battery_safety_margin_percent": 0.5,
        "energy_per_distance": 0.05,
        "charge_target_battery": 80.0,
        "charge_rate_percent_per_minute": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def local_optimizer() -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20,
        energy_per_distance=0.05,
        charge_target_battery=80,
        charge_rate_percent_per_minute=5,
        battery_safety_margin_percent=0.5,
    )


def solution(request_id: str = "req-1") -> dict:
    return {
        "reqId": request_id,
        "response": {
            "solver_response": {
                "status": 0,
                "solution_cost": 7.5,
                "vehicle_data": {
                    "R2": {"task_id": ["Depot", 1, 0, "Depot"]},
                    "R3": {"task_id": ["Depot", 2, "Depot"]},
                },
                "dropped_tasks": {"task_id": [], "task_index": []},
            }
        },
    }


def test_rest_payload_uses_live_api_compatible_minimal_task_data() -> None:
    problem = base_problem()
    problem["tasks"][0]["assigned_robot_id"] = "R2"
    problem["tasks"][0]["frozen"] = True

    payload, context = build_cuopt_routing_payload(problem, solver_time_limit_seconds=3)

    assert payload["task_data"]["task_locations"] == [2, 3, 1]
    assert "task_ids" not in payload["task_data"]
    assert "priorities" not in payload["task_data"]
    assert "mandatory_task_ids" not in payload["task_data"]
    assert payload["fleet_data"]["vehicle_ids"] == ["R1", "R2", "R3"]
    assert payload["task_data"]["order_vehicle_match"] == [
        {"order_id": 0, "vehicle_ids": [1]}
    ]
    assert payload["solver_config"]["time_limit"] == 3
    assert "drop_infeasible_tasks" not in payload["solver_config"]
    assert context["task_ids"] == ["T1", "T2", "T3"]


def test_rest_cuopt_posts_bearer_key_and_normalizes_plan() -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def post(self, url, json):
            captured["url"] = url
            captured["body"] = json
            return httpx.Response(200, json=solution(), request=httpx.Request("POST", url))

        def close(self):
            captured["closed"] = True

    result = CuOptRestOptimizer(
        api_key="nvapi-secret",
        local_optimizer=local_optimizer(),
        solver_time_limit_seconds=1,
        client_factory=FakeClient,
    ).optimize(base_problem())

    assert captured["headers"]["Authorization"] == "Bearer nvapi-secret"
    assert captured["url"] == "https://optimize.api.nvidia.com/v1/nvidia/cuopt"
    assert captured["body"]["action"] == "cuOpt_OptimizedRouting"
    assert captured["body"]["client_version"] == "custom"
    task_data = captured["body"]["data"]["task_data"]
    assert task_data["task_locations"] == [2, 3, 1]
    assert "task_ids" not in task_data
    assert "priorities" not in task_data
    assert "mandatory_task_ids" not in task_data
    assert "drop_infeasible_tasks" not in captured["body"]["data"]["solver_config"]
    assert result.request_id == "req-1"
    assignments = {task.task_id: task.robot_id for task in result.plan.scheduled_tasks}
    assert assignments == {"T1": "R2", "T2": "R2", "T3": "R3"}
    assert result.plan.metadata["backend"] == "cuopt_rest"
    assert result.plan.metadata["schedule_postprocessor"] == "LOCAL_WAREHOUSE_NORMALIZER"
    assert captured["closed"] is True


def test_rest_cuopt_polls_202_response() -> None:
    calls: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def post(self, url, json):
            calls.append("post")
            return httpx.Response(
                202,
                json={"reqId": "req-poll", "status": "pending-evaluation"},
                request=httpx.Request("POST", url),
            )

        def get(self, url):
            calls.append(url)
            return httpx.Response(200, json=solution("req-poll"), request=httpx.Request("GET", url))

        def close(self):
            pass

    result = CuOptRestOptimizer(
        api_key="nvapi-secret",
        local_optimizer=local_optimizer(),
        poll_interval_seconds=0.001,
        client_factory=FakeClient,
    ).optimize(base_problem())

    assert result.request_id == "req-poll"
    assert calls == [
        "post",
        "https://optimize.api.nvidia.com/v1/status/req-poll",
    ]


def test_auto_without_key_uses_cpu_without_failure_warning() -> None:
    outcome = optimizer_module.optimize_problem(base_problem(), settings())
    assert outcome.backend == "local"
    assert outcome.execution["requested_provider"] == "AUTO"
    assert outcome.execution["used_provider"] == "CPU"
    assert outcome.execution["fallback_used"] is False
    assert outcome.warnings == []


def test_auto_with_key_uses_rest_cuopt(monkeypatch) -> None:
    class FakeRest:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "secret-key"
            self.local = kwargs["local_optimizer"]

        def optimize(self, problem):
            plan = self.local.optimize(problem)
            return SimpleNamespace(
                plan=plan,
                request_id="req-auto",
                solver_status=0,
                solution_cost=12.0,
                raw_task_order_by_robot={},
            )

    monkeypatch.setattr(optimizer_module, "CuOptRestOptimizer", FakeRest)
    outcome = optimizer_module.optimize_problem(
        base_problem(), settings(cuopt_api_key="secret-key")
    )

    assert outcome.backend == "cuopt"
    assert outcome.execution["used_provider"] == "CUOPT"
    assert outcome.execution["transport"] == "HTTPS_REST"
    assert outcome.execution["fallback_used"] is False
    assert outcome.execution["attempts"][0]["request_id"] == "req-auto"


def test_cuopt_rest_failure_falls_back_to_cpu(monkeypatch) -> None:
    class BrokenRest:
        def __init__(self, **kwargs):
            pass

        def optimize(self, problem):
            raise CuOptRestError("CUOPT_SUBMIT_REQUEST_FAILED:TimeoutException")

    monkeypatch.setattr(optimizer_module, "CuOptRestOptimizer", BrokenRest)
    outcome = optimizer_module.optimize_problem(
        base_problem(), settings(cuopt_api_key="secret-key")
    )

    assert outcome.backend == "local"
    assert outcome.plan.unassigned_task_ids == []
    assert outcome.execution["used_provider"] == "CPU"
    assert outcome.execution["fallback_used"] is True
    assert "CUOPT_SUBMIT_REQUEST_FAILED" in outcome.execution["fallback_reason"]
    assert [row["provider"] for row in outcome.execution["attempts"]] == [
        "CUOPT_REST",
        "CPU",
    ]


def test_cuopt_rest_failure_without_fallback_raises(monkeypatch) -> None:
    class BrokenRest:
        def __init__(self, **kwargs):
            pass

        def optimize(self, problem):
            raise CuOptRestError("CUOPT_SUBMIT_HTTP_401")

    monkeypatch.setattr(optimizer_module, "CuOptRestOptimizer", BrokenRest)
    with pytest.raises(CuOptRestError, match="CUOPT_SUBMIT_HTTP_401"):
        optimizer_module.optimize_problem(
            base_problem(),
            settings(
                optimizer_backend="cuopt",
                cuopt_api_key="secret-key",
                cuopt_fallback_to_local=False,
            ),
        )


def test_legacy_local_backend_with_api_key_auto_enables_cuopt(monkeypatch) -> None:
    class FakeRest:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "friendly-key"
            self.local = kwargs["local_optimizer"]

        def optimize(self, problem):
            return SimpleNamespace(
                plan=self.local.optimize(problem),
                request_id="req-legacy",
                solver_status=0,
                solution_cost=8.0,
                raw_task_order_by_robot={},
            )

    monkeypatch.setattr(optimizer_module, "CuOptRestOptimizer", FakeRest)
    outcome = optimizer_module.optimize_problem(
        base_problem(),
        settings(optimizer_backend="local", cuopt_api_key="friendly-key"),
    )

    assert outcome.execution["used_provider"] == "CUOPT"
    assert outcome.execution["configured_backend"] == "local"
    assert outcome.execution["auto_enabled_by_credentials"] is True


def test_auto_enable_false_keeps_cpu_even_when_key_exists(monkeypatch) -> None:
    class MustNotRunRest:
        def __init__(self, **kwargs):
            raise AssertionError("cuOpt must not be called when auto enable is false")

    monkeypatch.setattr(optimizer_module, "CuOptRestOptimizer", MustNotRunRest)
    outcome = optimizer_module.optimize_problem(
        base_problem(),
        settings(
            optimizer_backend="local",
            cuopt_api_key="friendly-key",
            cuopt_auto_enable=False,
        ),
    )

    assert outcome.execution["used_provider"] == "CPU"
    assert outcome.execution["auto_enabled_by_credentials"] is False


def test_rest_cuopt_retries_after_extra_forbidden_422(monkeypatch) -> None:
    import app.services.cuopt_rest as cuopt_module

    original_builder = cuopt_module.build_cuopt_routing_payload

    def builder_with_legacy_field(problem, *, solver_time_limit_seconds):
        payload, context = original_builder(
            problem, solver_time_limit_seconds=solver_time_limit_seconds
        )
        payload["solver_config"]["legacy_flag"] = False
        return payload, context

    monkeypatch.setattr(cuopt_module, "build_cuopt_routing_payload", builder_with_legacy_field)
    posted_bodies: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def post(self, url, json):
            import copy
            posted_bodies.append(copy.deepcopy(json))
            if len(posted_bodies) == 1:
                return httpx.Response(
                    422,
                    json={
                        "error": (
                            "unable to validate optimization data stream, 1 validation error "
                            "for OptimizedRoutingData\nsolver_config.legacy_flag\n  Extra inputs "
                            "are not permitted [type=extra_forbidden]"
                        ),
                        "error_result": True,
                    },
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200, json=solution("req-retry"), request=httpx.Request("POST", url)
            )

        def close(self):
            pass

    result = CuOptRestOptimizer(
        api_key="nvapi-secret",
        local_optimizer=local_optimizer(),
        client_factory=FakeClient,
    ).optimize(base_problem())

    assert len(posted_bodies) == 2
    assert posted_bodies[0]["data"]["solver_config"]["legacy_flag"] is False
    assert "legacy_flag" not in posted_bodies[1]["data"]["solver_config"]
    assert result.request_id == "req-retry"
    assert result.plan.metadata["cuopt_schema_retry_count"] == 1
    assert result.plan.metadata["cuopt_schema_removed_fields"] == [
        "solver_config.legacy_flag"
    ]

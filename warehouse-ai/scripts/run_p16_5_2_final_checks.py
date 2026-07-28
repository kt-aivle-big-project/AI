"""Run deterministic P16.5.2 NVIDIA cuOpt live-schema compatibility checks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from app.models import AtomicTask
from app.services import optimizer as optimizer_module
from app.services.cuopt_rest import CuOptRestError, build_cuopt_routing_payload


def _problem() -> dict:
    return {
        "warehouse_id": 1,
        "captured_at": "2026-07-25T00:00:00+00:00",
        "reference_time": "2026-07-25T00:00:00+00:00",
        "plan_mode": "INITIAL_PLAN",
        "nodes": [{"node_id": value, "active": True} for value in (1, 2, 3)],
        "edges": [
            {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
        ],
        "robots": [
            {"robot_id": "R1", "node_id": 1, "battery": 90, "status": "IDLE", "max_load": 100, "current_load": 0}
        ],
        "tasks": [
            AtomicTask(task_id="T1", action="MOVE", source_candidates=[1], target_candidates=[3], priority=1).model_dump(mode="json")
        ],
        "temporary_closures": [],
        "active_plan": None,
        "fixed_task_ids": [],
        "changeable_task_ids": [],
        "weights": {},
    }


def _settings(**overrides):
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


def main() -> int:
    results: dict[str, bool] = {}
    payload, context = build_cuopt_routing_payload(_problem(), solver_time_limit_seconds=1)
    results["rest_payload_contract"] = (
        payload["task_data"]["task_locations"] == [1]
        and "task_ids" not in payload["task_data"]
        and "priorities" not in payload["task_data"]
        and "mandatory_task_ids" not in payload["task_data"]
        and payload["fleet_data"]["vehicle_ids"] == ["R1"]
        and payload["solver_config"] == {"time_limit": 1}
        and context["task_ids"] == ["T1"]
    )

    cpu = optimizer_module.optimize_problem(_problem(), _settings())
    results["auto_without_key_uses_cpu"] = (
        cpu.execution["used_provider"] == "CPU" and not cpu.execution["fallback_used"]
    )

    class FakeRest:
        def __init__(self, **kwargs):
            self.local = kwargs["local_optimizer"]
            self.api_key = kwargs["api_key"]

        def optimize(self, problem):
            return SimpleNamespace(
                plan=self.local.optimize(problem),
                request_id="req-check",
                solver_status=0,
                solution_cost=1.0,
                raw_task_order_by_robot={"R1": ["T1"]},
            )

    with patch.object(optimizer_module, "CuOptRestOptimizer", FakeRest):
        cuopt = optimizer_module.optimize_problem(_problem(), _settings(cuopt_api_key="secret"))
    results["api_key_enables_rest_cuopt"] = (
        cuopt.execution["used_provider"] == "CUOPT"
        and cuopt.execution["transport"] == "HTTPS_REST"
        and not cuopt.execution["fallback_used"]
    )

    with patch.object(optimizer_module, "CuOptRestOptimizer", FakeRest):
        legacy_key_only = optimizer_module.optimize_problem(
            _problem(), _settings(optimizer_backend="local", cuopt_api_key="secret")
        )
    results["legacy_local_env_key_only_enables_cuopt"] = (
        legacy_key_only.execution["used_provider"] == "CUOPT"
        and legacy_key_only.execution["auto_enabled_by_credentials"]
    )

    forced_cpu = optimizer_module.optimize_problem(
        _problem(),
        _settings(optimizer_backend="local", cuopt_api_key="secret", cuopt_auto_enable=False),
    )
    results["auto_enable_false_forces_cpu"] = (
        forced_cpu.execution["used_provider"] == "CPU"
        and not forced_cpu.execution["auto_enabled_by_credentials"]
    )

    class BrokenRest:
        def __init__(self, **kwargs):
            pass

        def optimize(self, problem):
            raise CuOptRestError("CUOPT_SUBMIT_REQUEST_FAILED:TimeoutException")

    with patch.object(optimizer_module, "CuOptRestOptimizer", BrokenRest):
        fallback = optimizer_module.optimize_problem(
            _problem(), _settings(cuopt_api_key="secret")
        )
    results["rest_failure_uses_cpu"] = (
        fallback.execution["used_provider"] == "CPU"
        and fallback.execution["fallback_used"]
        and fallback.execution["attempts"][0]["provider"] == "CUOPT_REST"
        and fallback.plan.unassigned_task_ids == []
    )
    results["optimizer_provenance_recorded"] = bool(
        fallback.plan.metadata.get("optimizer_execution")
    )

    requirements = open("requirements.txt", encoding="utf-8").read().lower()
    results["no_cuopt_package_dependency"] = (
        "cuopt-thin-client" not in requirements and "pypi.nvidia.com" not in requirements
    )

    output = {"all_passed": all(results.values()), "checks": results}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

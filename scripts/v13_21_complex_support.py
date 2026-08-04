"""Shared loader and HTTP client for v13.21 complex API scenarios."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "scenarios" / "v13_21_complex_api"
OUTPUT_DIR = ROOT / "runtime_outputs" / "v13_21_complex_runs"


def load_scenario(identifier: str) -> dict[str, Any]:
    path = Path(identifier)
    if not path.exists():
        path = SCENARIO_DIR / f"{identifier}.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown complex scenario: {identifier}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Scenario must be a JSON object: {path}")
    value["_path"] = str(path.resolve())
    return value


def list_scenarios() -> list[dict[str, Any]]:
    index = json.loads((SCENARIO_DIR / "index.json").read_text(encoding="utf-8"))
    return list(index["scenarios"])


def with_backend(payload: dict[str, Any], backend: str | None) -> dict[str, Any]:
    value = copy.deepcopy(payload)
    if backend:
        value["optimization_backend"] = backend
    return value


def api_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: int = 600,
) -> tuple[int, dict[str, Any]]:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    req = request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"detail": raw}
        return exc.code, parsed
    except error.URLError as exc:
        return 0, {"detail": f"API connection failed: {exc.reason}"}


def load_server_health(api_url: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
    status, payload = api_json(
        "GET", f"{api_url.rstrip('/')}/health", timeout_seconds=timeout_seconds
    )
    if status != 200:
        raise RuntimeError(f"FastAPI health check failed: HTTP {status}: {payload}")
    return payload


def input_requires_openai(scenario: dict[str, Any]) -> bool:
    request_payload = scenario.get("initial_request") or {}
    replan_payload = (scenario.get("replan") or {}).get("mission") or {}
    return bool(
        str(request_payload.get("user_command") or "").strip()
        or str(replan_payload.get("user_command") or "").strip()
    )


def execution_requires_openai(
    scenario: dict[str, Any], server_health: dict[str, Any]
) -> bool:
    """Resolve provider requirements from the actual running server mode."""

    mode = str(server_health.get("default_planning_mode") or "llm_router")
    if mode == "llm_router":
        return True
    if mode == "force_agent":
        return True
    # force_rule can normalize exact structured events deterministically, but
    # natural/mixed commands still require the common LLM normalizer.
    return input_requires_openai(scenario)


def bootstrap_scenario_runtime(
    *,
    api_url: str,
    warehouse_id: str,
    target_simulation_id: str,
    source_simulation_id: str | None = None,
    timeout_seconds: int = 60,
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {
        "warehouse_id": warehouse_id,
        "target_simulation_id": target_simulation_id,
        "reset": True,
        "copy_robot_runtime": True,
        "copy_edge_runtime": True,
        "copy_station_runtime": True,
        "copy_reservations": False,
    }
    if source_simulation_id:
        payload["source_simulation_id"] = source_simulation_id
    return api_json(
        "POST",
        f"{api_url.rstrip('/')}/api/v1/debug/scenarios/bootstrap-runtime",
        payload,
        timeout_seconds=timeout_seconds,
    )


def choose_replan_time(plan: dict[str, Any], spec: dict[str, Any]) -> int:
    mode = spec.get("mode")
    start = int(plan.get("plan_start_sim_time_ms") or 0)
    finish = int(plan.get("absolute_finish_at_ms") or start)
    if finish <= start:
        return start
    if mode == "fixed_ms":
        value = int(spec.get("value", start))
        return max(start, min(value, finish - 1))
    if mode == "fraction":
        fraction = float(spec.get("value", 0.5))
        fraction = min(max(fraction, 0.0), 0.999)
        return min(finish - 1, start + int((finish - start) * fraction))
    if mode == "service_midpoint":
        wanted = str(spec.get("service_kind") or "").upper()
        candidates: list[dict[str, Any]] = []
        for robot in plan.get("robots", []):
            for step in robot.get("steps", []):
                if step.get("step_type") != "SERVICE":
                    continue
                if wanted and str(step.get("service_kind") or "").upper() != wanted:
                    continue
                candidates.append(step)
        if candidates:
            selected = sorted(candidates, key=lambda item: item["start_at_ms"])[0]
            return int((int(selected["start_at_ms"]) + int(selected["end_at_ms"])) / 2)
        return start + int((finish - start) * 0.5)
    raise ValueError(f"Unsupported replan time mode: {mode}")


def operation_ids(response: dict[str, Any]) -> set[str]:
    plan = response.get("plan") or {}
    return {
        str(value.get("operation_id"))
        for value in plan.get("logical_operations", [])
        if value.get("operation_id")
    }


def assigned_robot_ids(response: dict[str, Any]) -> set[str]:
    plan = response.get("plan") or {}
    return {
        str(value.get("robot_id"))
        for value in plan.get("robots", [])
        if value.get("robot_id")
    }


def response_reason_codes(response: dict[str, Any]) -> set[str]:
    codes = {
        str(value.get("code"))
        for value in response.get("errors", [])
        if value.get("code")
    }
    for key in (
        "input_rejection",
        "workflow_hold",
        "pending_human_interaction",
    ):
        value = response.get(key)
        if isinstance(value, dict) and value.get("reason_code"):
            codes.add(str(value["reason_code"]))
    return codes


def response_failure_stages(response: dict[str, Any]) -> set[str]:
    return {
        str(value.get("stage"))
        for value in response.get("errors", [])
        if value.get("stage")
    }


def _runtime_snapshot_for_phase(
    scenario: dict[str, Any], phase: str
) -> dict[str, Any] | None:
    payload = (
        scenario.get("initial_request")
        if phase == "initial"
        else (scenario.get("replan") or {}).get("mission")
    ) or {}
    value = payload.get("runtime_snapshot")
    return value if isinstance(value, dict) else None


def evaluate_expectations(
    scenario: dict[str, Any],
    response: dict[str, Any],
    *,
    phase: str,
) -> list[str]:
    expected = scenario.get("expected") or {}
    errors: list[str] = []
    status = response.get("status")
    allowed = expected.get("allowed_status") or []
    if allowed and status not in allowed:
        errors.append(f"{phase}: status={status!r} not in {allowed!r}")
    expected_route = set(expected.get("expected_route") or [])
    if expected_route and response.get("final_route") not in expected_route:
        errors.append(
            f"{phase}: final_route={response.get('final_route')!r}; "
            f"expected one of {sorted(expected_route)!r}"
        )
    expected_version = expected.get("plan_version")
    if expected_version is not None and response.get("plan"):
        observed = response["plan"].get("plan_version")
        if observed != expected_version:
            errors.append(
                f"{phase}: plan_version={observed!r}; expected={expected_version!r}"
            )
    observed_ops = operation_ids(response)
    preserve = set(expected.get("preserve_ids") or [])
    if response.get("plan") and not preserve.issubset(observed_ops):
        errors.append(
            f"{phase}: missing operations {sorted(preserve - observed_ops)}"
        )
    forbidden = set(expected.get("must_not_add_ids") or [])
    if observed_ops & forbidden:
        errors.append(
            f"{phase}: forbidden operations observed {sorted(observed_ops & forbidden)}"
        )
    excluded = set(expected.get("excluded_robot_ids") or [])
    assigned = assigned_robot_ids(response)
    if assigned & excluded:
        errors.append(
            f"{phase}: excluded robots assigned {sorted(assigned & excluded)}"
        )
    expected_codes = set(expected.get("expected_reason_codes") or [])
    observed_codes = response_reason_codes(response)
    if expected_codes and not (expected_codes & observed_codes):
        errors.append(
            f"{phase}: none of expected reason codes {sorted(expected_codes)} "
            f"were observed; observed={sorted(observed_codes)}"
        )
    forbidden_codes = set(expected.get("forbidden_reason_codes") or [])
    if forbidden_codes & observed_codes:
        errors.append(
            f"{phase}: forbidden reason codes observed "
            f"{sorted(forbidden_codes & observed_codes)}"
        )
    expected_stages = set(expected.get("expected_failure_stages") or [])
    observed_stages = response_failure_stages(response)
    if expected_stages and not (expected_stages & observed_stages):
        errors.append(
            f"{phase}: expected failure stage in {sorted(expected_stages)}, "
            f"observed={sorted(observed_stages)}"
        )

    if expected.get("must_not_assign_ineligible_robot") and response.get("plan"):
        snapshot = _runtime_snapshot_for_phase(scenario, phase)
        if snapshot:
            ineligible = {
                str(value.get("robot_id"))
                for value in snapshot.get("robot_states", [])
                if value.get("robot_id")
                and (
                    str(value.get("status", "idle")) != "idle"
                    or float(value.get("battery_pct", 0)) < 30.0
                    or not (value.get("current_node") or value.get("current_edge"))
                )
            }
            if assigned & ineligible:
                errors.append(
                    f"{phase}: ineligible robots assigned {sorted(assigned & ineligible)}"
                )

    if expected.get("deduplicate") and response.get("plan"):
        values = [
            str(value.get("operation_id"))
            for value in response["plan"].get("logical_operations", [])
            if value.get("operation_id")
        ]
        duplicates = sorted(
            value for value in set(values) if values.count(value) > 1
        )
        if duplicates:
            errors.append(f"{phase}: duplicate logical operations {duplicates}")

    if expected.get("must_not_plan_business_tasks") and response.get("plan"):
        plan = response["plan"]
        if plan.get("logical_operations") or plan.get("robots"):
            errors.append(f"{phase}: business tasks were planned for an incident-only scenario")
    return errors


def persist_run(scenario_id: str, payload: dict[str, Any]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"{scenario_id}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@dataclass
class ScenarioRunResult:
    scenario_id: str
    initial_http_status: int
    initial: dict[str, Any]
    replan_http_status: int | None
    replan: dict[str, Any] | None
    comparison_http_status: int | None
    comparison: dict[str, Any] | None
    assertion_errors: list[str]
    output_path: Path | None = None

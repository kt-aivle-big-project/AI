"""HTTP end-to-end check for the native LARO plan API.

This script intentionally does not call ``/optimize`` or ``/reoptimize``.  It
verifies the future replacement contract: native mission input -> PostgreSQL /
Redis / Neo4j -> Rule/Agent -> OR-Tools/cuOpt -> MAPF -> SimulationPlan.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the native LARO plan API over HTTP.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--warehouse-id", default="WH-001")
    parser.add_argument("--simulation-id", default="SIM-V18-MIXED")
    parser.add_argument("--backend", choices=("ortools", "cuopt", "cuopt_payload_only"), default="ortools")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "runtime_outputs" / "native_plan_api_checks"),
    )
    return parser.parse_args()


def request_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 300.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": raw}
        return int(exc.code), payload


def plan_signature(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "robots": [
            {
                "robot_id": robot["robot_id"],
                "step_types": [step["step_type"] for step in robot.get("steps", [])],
                "service_kinds": [
                    step.get("service_kind")
                    for step in robot.get("steps", [])
                    if step.get("service_kind") is not None
                ],
                "task_ids": [
                    step.get("task_id")
                    for step in robot.get("steps", [])
                    if step.get("task_id") is not None
                ],
            }
            for robot in plan.get("robots", [])
        ],
        "logical_operation_ids": [
            value["operation_id"] for value in plan.get("logical_operations", [])
        ],
        "station_reservation_count": len(plan.get("station_reservations", [])),
    }


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")

    base = args.base_url.rstrip("/")
    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    preflight_url = (
        f"{base}/api/v1/warehouses/{urllib.parse.quote(args.warehouse_id)}/"
        "missions/plan/preflight?"
        + urllib.parse.urlencode({"simulation_id": args.simulation_id})
    )
    preflight_status, preflight = request_json("GET", preflight_url, timeout=30)
    (run_dir / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if preflight_status != 200 or not preflight.get("ready"):
        print(
            json.dumps(
                {
                    "version": "13.24.0",
                    "status": "FAIL",
                    "stage": "preflight",
                    "http_status": preflight_status,
                    "preflight": preflight,
                    "output_dir": str(run_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    body = {
        "warehouse_id": args.warehouse_id,
        "simulation_id": args.simulation_id,
        "optimization_backend": args.backend,
        "events": [
            {"type": "new_order", "order_id": "ORD-001"},
            {"type": "inbound_item_arrived", "inbound_id": "IN-001"},
        ],
    }
    (run_dir / "request.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    endpoint = f"{base}/api/v1/warehouses/{urllib.parse.quote(args.warehouse_id)}/missions/plan"
    results: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    errors: list[str] = []

    for index in range(1, args.repeat + 1):
        http_status, response = request_json("POST", endpoint, body, timeout=600)
        (run_dir / f"response_{index}.json").write_text(
            json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        plan = response.get("plan")
        if http_status != 200:
            errors.append(f"run {index}: HTTP {http_status}")
        if args.backend == "cuopt_payload_only":
            if response.get("status") != "ready_for_cuopt":
                errors.append(
                    f"run {index}: expected ready_for_cuopt, got {response.get('status')}"
                )
            results.append({"http_status": http_status, "response": response})
            continue

        if response.get("status") != "plan_validated":
            errors.append(
                f"run {index}: expected plan_validated, got {response.get('status')}"
            )
        if not isinstance(plan, dict):
            errors.append(f"run {index}: response.plan is missing")
            results.append({"http_status": http_status, "response": response})
            continue
        robots = plan.get("robots") or []
        if not robots:
            errors.append(f"run {index}: plan contains no robot routes")
        if not any(step.get("step_type") == "MOVE" for robot in robots for step in robot.get("steps", [])):
            errors.append(f"run {index}: plan contains no MOVE step")
        if not any(step.get("step_type") == "SERVICE" for robot in robots for step in robot.get("steps", [])):
            errors.append(f"run {index}: plan contains no SERVICE step")

        plan_id = str(plan.get("plan_id") or "")
        if not plan_id:
            errors.append(f"run {index}: plan_id is missing")
            results.append({"http_status": http_status, "response": response})
            continue

        trace_url = (
            f"{base}/api/v1/warehouses/{urllib.parse.quote(args.warehouse_id)}/"
            f"missions/plans/{urllib.parse.quote(plan_id)}/trace"
        )
        trace_status, trace = request_json("GET", trace_url, timeout=60)
        (run_dir / f"trace_{index}.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if trace_status != 200:
            errors.append(f"run {index}: trace HTTP {trace_status}")
        checks = trace.get("checks") or {}
        for key in (
            "structured_keys_valid",
            "dynamic_input_valid",
            "payload_valid",
            "candidate_space_valid",
            "assignment_valid",
            "route_valid",
            "mapf_valid",
        ):
            if checks.get(key) is not True:
                errors.append(f"run {index}: trace check {key}={checks.get(key)!r}")

        signature = plan_signature(plan)
        signatures.append(signature)
        results.append(
            {
                "http_status": http_status,
                "status": response.get("status"),
                "plan_id": plan_id,
                "final_route": response.get("final_route"),
                "effective_planning_mode": response.get("effective_planning_mode"),
                "router_llm_executed": response.get("router_llm_executed"),
                "robot_count": len(robots),
                "step_count": sum(len(robot.get("steps", [])) for robot in robots),
                "makespan_ms": plan.get("makespan_ms"),
                "trace_checks": checks,
            }
        )

    deterministic = not signatures or all(value == signatures[0] for value in signatures[1:])
    if not deterministic:
        errors.append("Repeated plan signatures differ.")

    summary = {
        "version": "13.24.0",
        "status": "PASS" if not errors else "FAIL",
        "endpoint": endpoint,
        "warehouse_id": args.warehouse_id,
        "simulation_id": args.simulation_id,
        "backend": args.backend,
        "repeat": args.repeat,
        "preflight": {
            "postgres": preflight.get("postgres"),
            "redis": preflight.get("redis"),
            "neo4j": preflight.get("neo4j"),
        },
        "deterministic_signature": deterministic,
        "runs": results,
        "errors": errors,
        "output_dir": str(run_dir),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

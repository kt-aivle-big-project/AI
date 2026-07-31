"""Run one complex Native Plan scenario against LARO v4.1 and save evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.native_plan_complex_support_v41 import (
    DEFAULT_OUTPUT_DIR,
    LARO_TARGET_VERSION,
    PACK_VERSION,
    RunArtifacts,
    apply_backend,
    archive_directory,
    assigned_robot_ids,
    build_stage_contract,
    evaluate,
    latency_metrics,
    load_scenario,
    logical_operation_ids,
    plan_signature,
    request_json,
    response_reason_codes,
    save_json,
    step_metrics,
    utc_run_id,
    write_run_csv_artifacts,
    write_scenario_report,
    write_stage_contract_artifacts,
)


def execute_scenario(
    scenario_id: str,
    *,
    base_url: str = "http://localhost:8000",
    backend: str | None = None,
    repeat: int = 1,
    timeout_seconds: int = 900,
    output_dir: Path | None = None,
    save_debug: bool = True,
    archive: bool = False,
) -> tuple[list[RunArtifacts], dict[str, Any]]:
    scenario = load_scenario(scenario_id)
    scenario_dir = output_dir or (DEFAULT_OUTPUT_DIR / utc_run_id() / scenario_id)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    save_json(scenario_dir / "scenario.json", scenario)

    base = base_url.rstrip("/")
    health_status, health, health_ms = request_json(
        "GET", f"{base}/health", timeout_seconds=30
    )
    save_json(
        scenario_dir / "server_health.json",
        {"http_status": health_status, "duration_ms": round(health_ms, 3), "response": health},
    )

    request_body = apply_backend(scenario["request"], backend)
    actual_backend = str(request_body.get("optimization_backend") or "")
    warehouse_id = str(request_body["warehouse_id"])
    simulation_id = str(request_body["simulation_id"])
    preflight_url = (
        f"{base}/api/v1/warehouses/{urllib_quote(warehouse_id)}/missions/plan/preflight"
        f"?simulation_id={urllib_quote(simulation_id)}"
    )
    preflight_status, preflight, preflight_ms = request_json(
        "GET", preflight_url, timeout_seconds=min(timeout_seconds, 60)
    )
    save_json(scenario_dir / "preflight.json", preflight)

    results: list[RunArtifacts] = []
    if preflight_status != 200 or not preflight.get("ready"):
        summary = {
            "scenario_pack": PACK_VERSION,
            "target_laro_version": LARO_TARGET_VERSION,
            "scenario_id": scenario_id,
            "title": scenario.get("title"),
            "category": scenario.get("category"),
            "status": "FAIL",
            "stage": "preflight",
            "preflight_http_status": preflight_status,
            "preflight_ms": round(preflight_ms, 3),
            "preflight": preflight,
            "runs": [],
            "assertion_errors": ["Native Plan preflight is not READY."],
            "output_dir": str(scenario_dir),
        }
        save_json(scenario_dir / "scenario_summary.json", summary)
        write_scenario_report(scenario_dir, summary)
        if archive:
            summary["archive_path"] = str(archive_directory(scenario_dir))
            save_json(scenario_dir / "scenario_summary.json", summary)
        return results, summary

    endpoint = f"{base}/api/v1/warehouses/{urllib_quote(warehouse_id)}/missions/plan"
    signatures: list[str | None] = []
    all_errors: list[str] = []

    for index in range(1, repeat + 1):
        run_dir = scenario_dir / f"run_{index:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        save_json(run_dir / "request.json", request_body)

        http_status, response, http_duration_ms = request_json(
            "POST", endpoint, request_body, timeout_seconds=timeout_seconds
        )
        save_json(run_dir / "response.json", response)

        trace: dict[str, Any] | None = None
        debug: dict[str, Any] | None = None
        plan = response.get("plan")
        plan_id = str((plan or {}).get("plan_id") or "")

        if plan_id:
            trace_url = (
                f"{base}/api/v1/warehouses/{urllib_quote(warehouse_id)}/missions/plans/"
                f"{urllib_quote(plan_id)}/trace"
            )
            trace_status, trace_value, trace_duration_ms = request_json(
                "GET", trace_url, timeout_seconds=min(timeout_seconds, 120)
            )
            if trace_status == 200:
                trace = trace_value
                save_json(run_dir / "trace.json", trace)
            else:
                save_json(
                    run_dir / "trace_error.json",
                    {
                        "http_status": trace_status,
                        "duration_ms": round(trace_duration_ms, 3),
                        "response": trace_value,
                    },
                )

            if save_debug:
                debug_url = (
                    f"{base}/api/v1/warehouses/{urllib_quote(warehouse_id)}/missions/plans/"
                    f"{urllib_quote(plan_id)}/debug"
                )
                debug_status, debug_value, debug_duration_ms = request_json(
                    "GET", debug_url, timeout_seconds=min(timeout_seconds, 180)
                )
                if debug_status == 200:
                    debug = debug_value
                    save_json(run_dir / "debug.json", debug)
                else:
                    save_json(
                        run_dir / "debug_error.json",
                        {
                            "http_status": debug_status,
                            "duration_ms": round(debug_duration_ms, 3),
                            "response": debug_value,
                        },
                    )

        assertion_errors = evaluate(
            scenario,
            response,
            trace,
            backend=actual_backend,
        )
        stage_contract = build_stage_contract(
            scenario,
            response,
            trace,
            debug,
            backend=actual_backend,
        )
        write_stage_contract_artifacts(run_dir, stage_contract, debug)
        if (scenario.get("expected") or {}).get("require_stage_contract"):
            assertion_errors.extend(stage_contract.get("assertion_errors") or [])
        if http_status != 200:
            assertion_errors.append(f"HTTP status={http_status}")
        all_errors.extend(f"run {index}: {value}" for value in assertion_errors)

        latency = latency_metrics(http_duration_ms, trace)
        steps = step_metrics(plan or {})
        optimizer = (trace or {}).get("optimizer") or {}
        metrics = {
            "scenario_pack": PACK_VERSION,
            "target_laro_version": LARO_TARGET_VERSION,
            "scenario_id": scenario_id,
            "repeat_index": index,
            "http_status": http_status,
            "status": response.get("status"),
            "final_route": response.get("final_route"),
            "effective_planning_mode": response.get("effective_planning_mode"),
            "router_llm_executed": response.get("router_llm_executed"),
            "backend": actual_backend,
            "provider": optimizer.get("optimizer"),
            "optimizer_status": optimizer.get("status"),
            "plan_id": plan_id or None,
            "makespan_ms": (plan or {}).get("makespan_ms"),
            "robot_count": len((plan or {}).get("robots") or []),
            "step_count": sum(
                len(value.get("steps") or []) for value in (plan or {}).get("robots") or []
            ),
            "logical_operation_ids": logical_operation_ids(response),
            "assigned_robot_ids": assigned_robot_ids(response),
            "reason_codes": sorted(response_reason_codes(response)),
            "station_reservation_count": len((plan or {}).get("station_reservations") or []),
            "trace_checks": (trace or {}).get("checks"),
            "repository": (trace or {}).get("repository"),
            "stage_contract_status": stage_contract.get("status"),
            "stage_statuses": {
                value.get("stage"): value.get("status")
                for value in stage_contract.get("stages", []) or []
            },
            "latency": latency,
            **steps,
            "assertion_errors": assertion_errors,
            "output_dir": str(run_dir),
        }
        save_json(run_dir / "metrics.json", metrics)
        write_run_csv_artifacts(run_dir, response, trace)

        signatures.append(plan_signature(response))
        results.append(
            RunArtifacts(
                scenario_id=scenario_id,
                repeat_index=index,
                output_dir=run_dir,
                request=request_body,
                response=response,
                trace=trace,
                debug=debug,
                metrics=metrics,
                assertion_errors=assertion_errors,
            )
        )

    comparable = [value for value in signatures if value is not None]
    deterministic = not comparable or all(value == comparable[0] for value in comparable[1:])
    if repeat > 1 and not deterministic:
        all_errors.append("Repeated plan signatures differ.")

    summary = {
        "scenario_pack": PACK_VERSION,
        "target_laro_version": LARO_TARGET_VERSION,
        "scenario_id": scenario_id,
        "title": scenario.get("title"),
        "category": scenario.get("category"),
        "difficulty": scenario.get("difficulty"),
        "backend": actual_backend,
        "server_version": health.get("version") if isinstance(health, dict) else None,
        "server_planning_mode": health.get("default_planning_mode") if isinstance(health, dict) else None,
        "repeat": repeat,
        "status": "PASS" if not all_errors else "FAIL",
        "deterministic_signature": deterministic,
        "preflight_ms": round(preflight_ms, 3),
        "runs": [value.metrics for value in results],
        "assertion_errors": all_errors,
        "output_dir": str(scenario_dir),
    }
    save_json(scenario_dir / "scenario_summary.json", summary)
    write_scenario_report(scenario_dir, summary)
    if archive:
        summary["archive_path"] = str(archive_directory(scenario_dir))
        save_json(scenario_dir / "scenario_summary.json", summary)
    return results, summary


def urllib_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--backend", choices=("ortools", "cuopt", "cuopt_payload_only"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output-dir")
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")

    _results, summary = execute_scenario(
        args.scenario,
        base_url=args.base_url,
        backend=args.backend,
        repeat=args.repeat,
        timeout_seconds=args.timeout_seconds,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        save_debug=not args.no_debug,
        archive=args.archive,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if args.strict and summary.get("status") != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())

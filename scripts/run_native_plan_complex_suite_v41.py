"""Run a filtered v4.1 Native Plan complex-scenario suite and save reports."""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.native_plan_complex_support_v41 import (
    DEFAULT_OUTPUT_DIR,
    LARO_TARGET_VERSION,
    PACK_VERSION,
    archive_directory,
    list_scenarios,
    request_json,
    save_json,
    scenario_requires_openai,
    utc_run_id,
    write_suite_reports,
)
from scripts.run_native_plan_complex_scenario_v41 import execute_scenario


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--backend",
        default="ortools",
        choices=("ortools", "cuopt", "cuopt_payload_only"),
    )
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--min-difficulty", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--output-dir")
    parser.add_argument("--skip-openai", action="store_true")
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.max_workers < 1:
        raise SystemExit("--max-workers must be >= 1")

    suite_id = utc_run_id()
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR / suite_id
    output_dir.mkdir(parents=True, exist_ok=True)

    health_status, health, health_ms = request_json(
        "GET", f"{args.base_url.rstrip('/')}/health", timeout_seconds=30
    )
    save_json(
        output_dir / "server_health.json",
        {"http_status": health_status, "duration_ms": round(health_ms, 3), "response": health},
    )
    server_mode = str(health.get("default_planning_mode") or "") if health_status == 200 else ""

    include = {str(value).strip() for value in args.include if str(value).strip()}
    categories = {str(value).strip().upper() for value in args.category if str(value).strip()}
    tags = {str(value).strip().casefold() for value in args.tag if str(value).strip()}

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for value in list_scenarios():
        scenario_id = str(value["scenario_id"])
        scenario_tags = {str(tag).casefold() for tag in value.get("tags") or []}
        if include and scenario_id not in include:
            continue
        if categories and str(value.get("category") or "").upper() not in categories:
            continue
        if tags and not tags.issubset(scenario_tags):
            continue
        if int(value.get("difficulty") or 0) < args.min_difficulty:
            continue
        if args.skip_openai and scenario_requires_openai(value, server_mode):
            skipped.append(
                {
                    "scenario_id": scenario_id,
                    "reason": (
                        f"OpenAI is required under server mode {server_mode!r}; "
                        "--skip-openai was supplied"
                    ),
                }
            )
            continue
        selected.append(value)

    if not selected:
        raise SystemExit("No complex scenarios matched the requested filters.")

    started = time.perf_counter()
    scenario_summaries: list[dict[str, Any]] = []

    def run_one(value: dict[str, Any]) -> dict[str, Any]:
        scenario_id = str(value["scenario_id"])
        scenario_dir = output_dir / scenario_id
        try:
            _runs, summary = execute_scenario(
                scenario_id,
                base_url=args.base_url,
                backend=args.backend,
                repeat=args.repeat,
                timeout_seconds=args.timeout_seconds,
                output_dir=scenario_dir,
                save_debug=not args.no_debug,
                archive=False,
            )
            return summary
        except Exception as exc:
            return {
                "scenario_pack": PACK_VERSION,
                "target_laro_version": LARO_TARGET_VERSION,
                "scenario_id": scenario_id,
                "title": value.get("title"),
                "category": value.get("category"),
                "difficulty": value.get("difficulty"),
                "status": "FAIL",
                "assertion_errors": [f"{type(exc).__name__}: {exc}"],
                "runs": [],
                "output_dir": str(scenario_dir),
            }

    workers = max(1, int(args.max_workers))
    if workers == 1:
        for index, value in enumerate(selected, start=1):
            print(f"[{index}/{len(selected)}] {value['scenario_id']}", flush=True)
            summary = run_one(value)
            scenario_summaries.append(summary)
            print(
                json.dumps(
                    {
                        "scenario_id": summary.get("scenario_id"),
                        "status": summary.get("status"),
                        "errors": summary.get("assertion_errors"),
                        "output_dir": summary.get("output_dir"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
    else:
        # Use >1 only for local OR-Tools runs.  OpenAI and NVIDIA calls may be
        # slower or rate-limited when multiple scenarios run concurrently.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_one, value): value for value in selected}
            for future in as_completed(futures):
                summary = future.result()
                scenario_summaries.append(summary)
                print(
                    json.dumps(
                        {
                            "scenario_id": summary.get("scenario_id"),
                            "status": summary.get("status"),
                            "errors": summary.get("assertion_errors"),
                            "output_dir": summary.get("output_dir"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
                )

    records: list[dict[str, Any]] = []
    for summary in sorted(scenario_summaries, key=lambda value: str(value.get("scenario_id"))):
        for run in summary.get("runs") or []:
            latency = run.get("latency") or {}
            records.append(
                {
                    "scenario_id": summary.get("scenario_id"),
                    "repeat_index": run.get("repeat_index"),
                    "status": run.get("status"),
                    "final_route": run.get("final_route"),
                    "backend": run.get("backend"),
                    "provider": run.get("provider"),
                    "http_total_ms": latency.get("http_total_ms"),
                    "llm_ms": latency.get("llm_ms"),
                    "llm_call_count": latency.get("llm_call_count"),
                    "db_context_ms": latency.get("db_context_ms"),
                    "formulation_and_payload_ms": latency.get("formulation_and_payload_ms"),
                    "solver_ms": latency.get("solver_ms"),
                    "mapf_and_plan_ms": latency.get("mapf_and_plan_ms"),
                    "makespan_ms": run.get("makespan_ms"),
                    "robot_count": run.get("robot_count"),
                    "step_count": run.get("step_count"),
                    "total_distance_m": run.get("total_distance_m"),
                    "assertion_error_count": len(run.get("assertion_errors") or []),
                    "assertion_errors": run.get("assertion_errors") or [],
                    "llm_nodes": latency.get("llm_nodes") or [],
                    "output_dir": run.get("output_dir"),
                }
            )

    fail_count = sum(1 for value in scenario_summaries if value.get("status") != "PASS")
    suite_summary = {
        "scenario_pack": PACK_VERSION,
        "target_laro_version": LARO_TARGET_VERSION,
        "suite_id": suite_id,
        "backend": args.backend,
        "repeat": args.repeat,
        "max_workers": workers,
        "server": {
            "health_http_status": health_status,
            "default_planning_mode": server_mode or None,
            "version": health.get("version") if isinstance(health, dict) else None,
            "openai_configured": health.get("openai_configured") if isinstance(health, dict) else None,
        },
        "filters": {
            "include": sorted(include),
            "categories": sorted(categories),
            "tags": sorted(tags),
            "min_difficulty": args.min_difficulty,
            "skip_openai": bool(args.skip_openai),
            "save_debug": not args.no_debug,
            "strict": bool(args.strict),
        },
        "scenario_count": len(selected),
        "skipped_scenarios": skipped,
        "run_count": len(records),
        "pass_count": len(selected) - fail_count,
        "fail_count": fail_count,
        "total_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "scenario_summaries": scenario_summaries,
        "records": records,
        "output_dir": str(output_dir),
    }
    write_suite_reports(output_dir, suite_summary)

    if args.archive:
        suite_summary["archive_path"] = str(archive_directory(output_dir))
        save_json(output_dir / "suite_summary.json", suite_summary)

    print(
        json.dumps(
            {
                "scenario_pack": PACK_VERSION,
                "target_laro_version": LARO_TARGET_VERSION,
                "suite_id": suite_id,
                "backend": args.backend,
                "scenario_count": len(selected),
                "pass_count": suite_summary["pass_count"],
                "fail_count": fail_count,
                "total_wall_ms": suite_summary["total_wall_ms"],
                "output_dir": str(output_dir),
                "summary_json": str(output_dir / "suite_summary.json"),
                "summary_csv": str(output_dir / "suite_summary.csv"),
                "summary_md": str(output_dir / "suite_summary.md"),
                "archive_path": suite_summary.get("archive_path"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Materialize the 30-case operational catalog and download reports."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.planning_evaluation_cli_support import (  # noqa: E402
    _archive,
    _record,
    _request_json,
    _write_json,
    _write_reports,
)


TERMINAL = {"SUCCEEDED", "PARTIAL_FAILURE", "FAILED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--scenario-id", action="append", default=[])
    parser.add_argument(
        "--scenario-group",
        action="append",
        choices=("INITIAL", "REPLAN", "HUMAN_REVIEW"),
        default=[],
    )
    parser.add_argument("--backend", choices=("cuopt",), default="cuopt")
    parser.add_argument("--agent-repeats", type=int, default=5)
    parser.add_argument("--min-valid-agent-runs", type=int, default=3)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--output-dir")
    parser.add_argument("--materialize-only", action="store_true")
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.agent_repeats <= 5:
        parser.error("--agent-repeats must be between 1 and 5")
    if not 1 <= args.min_valid_agent_runs <= args.agent_repeats:
        parser.error("--min-valid-agent-runs must be <= --agent-repeats")

    started_at = datetime.now(timezone.utc)
    local_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir) if args.output_dir else (
        ROOT / "runtime_outputs" / "planning_scenario_suites" / local_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    request_body = {
        "scenario_ids": list(dict.fromkeys(args.scenario_id)),
        "scenario_groups": list(dict.fromkeys(args.scenario_group)),
        "materialize_only": args.materialize_only,
        "backend": args.backend,
        "depth": "mapf",
        "agent_repeats": args.agent_repeats,
        "min_valid_agent_runs": args.min_valid_agent_runs,
        "required_objective_profile": "BALANCED",
        "require_mapf_gate": True,
        "idempotency_key": f"local-scenario-suite-{local_id}",
    }
    status, suite = _request_json(
        "POST",
        f"{base_url}/api/v1/debug/evaluation-suites/run-async",
        request_body,
        timeout_seconds=min(args.timeout_seconds, 300),
    )
    _write_json(output_dir / "submission.json", {"status": status, "body": suite})
    if status != 202:
        print(json.dumps(suite, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    suite_id = str(suite["suite_id"])
    deadline = time.monotonic() + args.timeout_seconds
    while suite.get("status") not in TERMINAL:
        if time.monotonic() >= deadline:
            _write_json(output_dir / "suite_status.json", suite)
            print(f"Timed out while waiting for {suite_id}.", file=sys.stderr)
            return 2
        print(
            f"{suite_id}: {suite.get('status')} "
            f"{suite.get('completed_count', 0)}/{suite.get('scenario_count', 0)}",
            flush=True,
        )
        time.sleep(max(0.25, args.poll_seconds))
        status, suite = _request_json(
            "GET",
            f"{base_url}/api/v1/debug/evaluation-suites/{suite_id}",
            timeout_seconds=min(args.timeout_seconds, 120),
        )
        if status != 200:
            _write_json(output_dir / "suite_poll_error.json", suite)
            return 2

    _write_json(output_dir / "suite_status.json", suite)
    if args.materialize_only:
        snapshots: list[dict[str, Any]] = []
        for item in suite.get("scenarios") or []:
            scenario_id = str(item.get("scenario_id"))
            evaluation_id = str(item.get("evaluation_id"))
            scenario_dir = output_dir / scenario_id
            detail_status, detail = _request_json(
                "GET",
                f"{base_url}/api/v1/debug/evaluations/{evaluation_id}",
                timeout_seconds=min(args.timeout_seconds, 120),
            )
            _write_json(scenario_dir / "evaluation_detail.json", detail)
            files = detail.get("files") if isinstance(detail, dict) else None
            files = files if isinstance(files, dict) else {}
            before = files.get("materialization_report.json")
            after = files.get("post_materialization_report.json")
            if isinstance(before, dict):
                _write_json(scenario_dir / "materialization_report.json", before)
            if isinstance(after, dict):
                _write_json(
                    scenario_dir / "post_materialization_report.json", after
                )
            snapshot = after.get("snapshot") if isinstance(after, dict) else {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            snapshots.append(
                {
                    "scenario_id": scenario_id,
                    "scenario_group": item.get("scenario_group"),
                    "evaluation_id": evaluation_id,
                    "detail_http_status": detail_status,
                    "passed": (
                        after.get("passed") if isinstance(after, dict) else False
                    ),
                    "input_fingerprint": (
                        after.get("input_fingerprint")
                        if isinstance(after, dict)
                        else None
                    ),
                    "operation_count": snapshot.get("operation_count"),
                    "inventory_box_count": snapshot.get("inventory_box_count"),
                    "robot_count": len(snapshot.get("robot_states") or []),
                    "eligible_robot_count": snapshot.get(
                        "eligible_robot_count"
                    ),
                    "low_battery_robot_count": snapshot.get(
                        "low_battery_robot_count"
                    ),
                }
            )
        materialization_summary = {
            "suite_id": suite_id,
            "status": suite.get("status"),
            "scenario_count": len(snapshots),
            "passed_count": sum(value["passed"] is True for value in snapshots),
            "failed_count": sum(value["passed"] is not True for value in snapshots),
            "snapshots": snapshots,
        }
        _write_json(
            output_dir / "materialization_summary.json",
            materialization_summary,
        )
        print(json.dumps(materialization_summary, ensure_ascii=False, indent=2))
        return 0 if suite.get("status") == "SUCCEEDED" else 1

    records: list[dict[str, Any]] = []
    for item in suite.get("scenarios") or []:
        scenario_id = str(item.get("scenario_id"))
        evaluation_id = str(item.get("evaluation_id"))
        job_id = item.get("job_id")
        scenario_dir = output_dir / scenario_id
        _write_json(scenario_dir / "status.json", item)
        if item.get("job_status") != "SUCCEEDED" or not job_id:
            records.append(
                {
                    "evaluation_id": evaluation_id,
                    "scenario_id": scenario_id,
                    "scenario_group": item.get("scenario_group"),
                    "backend": args.backend,
                    "depth": (
                        "mapf"
                        if item.get("scenario_group") == "INITIAL"
                        else "dynamic"
                    ),
                    "verdict": "JOB_FAILED",
                    "comparable": False,
                    "strict_pass": False,
                    "reasons": item.get("error_message")
                    or "comparison job failed",
                }
            )
            continue
        if item.get("scenario_group") != "INITIAL":
            detail_status, detail = _request_json(
                "GET",
                f"{base_url}/api/v1/debug/evaluations/{evaluation_id}",
                timeout_seconds=min(args.timeout_seconds, 120),
            )
            _write_json(scenario_dir / "evaluation_detail.json", detail)
            files = detail.get("files") if isinstance(detail, dict) else {}
            dynamic = files.get("dynamic_contract_report.json") if isinstance(files, dict) else None
            _write_json(scenario_dir / "dynamic_contract_report.json", dynamic or {})
            result_status, report = _request_json(
                "GET",
                f"{base_url}/api/v1/debug/evaluation-jobs/{job_id}/result",
                timeout_seconds=min(args.timeout_seconds, 120),
            )
            _write_json(scenario_dir / "dynamic_comparison_report.json", report)
            statistics = (
                report.get("agent_statistics")
                if isinstance(report, dict)
                else {}
            )
            statistics = statistics if isinstance(statistics, dict) else {}
            records.append(
                {
                    "evaluation_id": evaluation_id,
                    "scenario_id": scenario_id,
                    "scenario_group": item.get("scenario_group"),
                    "backend": args.backend,
                    "depth": "dynamic",
                    "verdict": (
                        report.get("verdict")
                        if result_status == 200 and isinstance(report, dict)
                        else "RESULT_DOWNLOAD_FAILED"
                    ),
                    "comparable": bool(
                        result_status == 200
                        and isinstance(report, dict)
                        and report.get("strict_pass") is not None
                    ),
                    "strict_pass": bool(
                        result_status == 200
                        and isinstance(report, dict)
                        and report.get("strict_pass") is True
                    ),
                    "requested_agent_runs": statistics.get("requested_runs"),
                    "valid_agent_runs": statistics.get("valid_runs"),
                    "applicable_agent_runs": statistics.get("applicable_runs"),
                    "reasons": (
                        []
                        if result_status == 200
                        else [f"HTTP {result_status}: {report}"]
                    ),
                    "detail_http_status": detail_status,
                }
            )
            continue
        result_status, report = _request_json(
            "GET",
            f"{base_url}/api/v1/debug/evaluation-jobs/{job_id}/result",
            timeout_seconds=min(args.timeout_seconds, 120),
        )
        if result_status != 200:
            _write_json(scenario_dir / "result_error.json", report)
            records.append(
                {
                    "evaluation_id": evaluation_id,
                    "scenario_id": scenario_id,
                    "backend": args.backend,
                    "depth": "mapf",
                    "verdict": "RESULT_DOWNLOAD_FAILED",
                    "comparable": False,
                    "strict_pass": False,
                    "reasons": f"HTTP {result_status}: {report}",
                }
            )
            continue
        _write_json(scenario_dir / "comparison_report.json", report)
        record = _record(evaluation_id, report)
        record["scenario_id"] = scenario_id
        records.append(record)

    summary = {
        "suite_id": suite_id,
        "created_at": started_at.isoformat(),
        "base_url": base_url,
        "backend": args.backend,
        "required_objective_profile": "BALANCED",
        "agent_repeats": args.agent_repeats,
        "min_valid_agent_runs": args.min_valid_agent_runs,
        "evaluation_count": len(records),
        "comparable_count": sum(bool(value.get("comparable")) for value in records),
        "pass_count": sum(bool(value.get("strict_pass")) for value in records),
        "fail_count": sum(not bool(value.get("strict_pass")) for value in records),
        "verdict_counts": {
            verdict: sum(value.get("verdict") == verdict for value in records)
            for verdict in sorted({str(value.get("verdict")) for value in records})
        },
        "records": records,
    }
    _write_reports(output_dir, summary)
    if args.archive:
        summary["archive_path"] = str(_archive(output_dir))
        _write_json(output_dir / "suite_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if (args.strict and summary["fail_count"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

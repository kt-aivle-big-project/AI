"""Run the reviewed 30-case planning catalog directly in this process."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.planning_evaluation import PlanningScenarioSuiteRequest  # noqa: E402
from app.services.planning_evaluation_service import PlanningEvaluationStore  # noqa: E402
from app.services.planning_scenario_suite_service import (  # noqa: E402
    PlanningScenarioSuiteService,
)
from scripts.planning_evaluation_cli_support import (  # noqa: E402
    _archive,
    _record,
    _write_json,
    _write_reports,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _report_path(
    store: PlanningEvaluationStore,
    evaluation_id: str,
    scenario_group: str,
) -> Path:
    filename = (
        "comparison_report.json"
        if scenario_group == "INITIAL"
        else "dynamic_comparison_report.json"
    )
    return store.comparisons / evaluation_id / filename


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    request = PlanningScenarioSuiteRequest(
        scenario_ids=list(dict.fromkeys(args.scenario_id)),
        scenario_groups=list(dict.fromkeys(args.scenario_group)),
        materialize_only=args.materialize_only,
        backend=args.backend,
        depth="mapf",
        agent_repeats=args.agent_repeats,
        min_valid_agent_runs=args.min_valid_agent_runs,
        required_objective_profile="BALANCED",
        require_mapf_gate=True,
    )
    _write_json(output_dir / "request.json", request.model_dump(mode="json"))

    store = PlanningEvaluationStore()
    service = PlanningScenarioSuiteService(store=store)

    def progress(scenario_id: str, completed: int, total: int, stage: str) -> None:
        print(f"{scenario_id}: {stage} {completed}/{total}", flush=True)

    try:
        suite = service.run(request, progress_callback=progress)
    except Exception as exc:
        failure = {"error_type": type(exc).__name__, "error_message": str(exc)}
        _write_json(output_dir / "suite_error.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    _write_json(output_dir / "suite_status.json", suite)
    suite_id = str(suite["suite_id"])

    if args.materialize_only:
        snapshots: list[dict[str, Any]] = []
        for item in suite.get("scenarios") or []:
            scenario_id = str(item["scenario_id"])
            evaluation_id = str(item["evaluation_id"])
            scenario_dir = output_dir / scenario_id
            detail = store.detail(evaluation_id)
            _write_json(scenario_dir / "evaluation_detail.json", detail)
            files = detail.get("files") if isinstance(detail.get("files"), dict) else {}
            before = files.get("materialization_report.json")
            after = files.get("post_materialization_report.json")
            if isinstance(before, dict):
                _write_json(scenario_dir / "materialization_report.json", before)
            if isinstance(after, dict):
                _write_json(scenario_dir / "post_materialization_report.json", after)
            snapshot = after.get("snapshot") if isinstance(after, dict) else {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            snapshots.append(
                {
                    "scenario_id": scenario_id,
                    "scenario_group": item.get("scenario_group"),
                    "evaluation_id": evaluation_id,
                    "passed": after.get("passed") is True if isinstance(after, dict) else False,
                    "input_fingerprint": after.get("input_fingerprint") if isinstance(after, dict) else None,
                    "operation_count": snapshot.get("operation_count"),
                    "inventory_box_count": snapshot.get("inventory_box_count"),
                    "robot_count": len(snapshot.get("robot_states") or []),
                    "eligible_robot_count": snapshot.get("eligible_robot_count"),
                    "low_battery_robot_count": snapshot.get("low_battery_robot_count"),
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
        _write_json(output_dir / "materialization_summary.json", materialization_summary)
        if args.archive:
            materialization_summary["archive_path"] = str(_archive(output_dir))
        print(json.dumps(materialization_summary, ensure_ascii=False, indent=2))
        return 0 if suite.get("status") == "SUCCEEDED" else 1

    records: list[dict[str, Any]] = []
    for item in suite.get("scenarios") or []:
        scenario_id = str(item["scenario_id"])
        evaluation_id = str(item["evaluation_id"])
        group = str(item.get("scenario_group") or "INITIAL")
        scenario_dir = output_dir / scenario_id
        _write_json(scenario_dir / "status.json", item)
        if item.get("comparison_status") != "SUCCEEDED":
            records.append(
                {
                    "evaluation_id": evaluation_id,
                    "scenario_id": scenario_id,
                    "scenario_group": group,
                    "backend": args.backend,
                    "depth": "mapf" if group == "INITIAL" else "dynamic",
                    "verdict": "COMPARISON_FAILED",
                    "comparable": False,
                    "strict_pass": False,
                    "reasons": item.get("error_message") or "comparison failed",
                }
            )
            continue

        report = _read_json(_report_path(store, evaluation_id, group))
        if group == "INITIAL":
            _write_json(scenario_dir / "comparison_report.json", report)
            record = _record(evaluation_id, report)
            record["scenario_id"] = scenario_id
            record["scenario_group"] = group
            records.append(record)
            continue

        detail = store.detail(evaluation_id)
        _write_json(scenario_dir / "evaluation_detail.json", detail)
        files = detail.get("files") if isinstance(detail.get("files"), dict) else {}
        dynamic_contract = files.get("dynamic_contract_report.json")
        _write_json(scenario_dir / "dynamic_contract_report.json", dynamic_contract or {})
        _write_json(scenario_dir / "dynamic_comparison_report.json", report)
        statistics = report.get("agent_statistics")
        statistics = statistics if isinstance(statistics, dict) else {}
        records.append(
            {
                "evaluation_id": evaluation_id,
                "scenario_id": scenario_id,
                "scenario_group": group,
                "backend": args.backend,
                "depth": "dynamic",
                "verdict": report.get("verdict"),
                "comparable": report.get("strict_pass") is not None,
                "strict_pass": report.get("strict_pass") is True,
                "requested_agent_runs": statistics.get("requested_runs"),
                "valid_agent_runs": statistics.get("valid_runs"),
                "applicable_agent_runs": statistics.get("applicable_runs"),
                "reasons": [],
            }
        )

    summary = {
        "suite_id": suite_id,
        "created_at": started_at.isoformat(),
        "execution_mode": "LOCAL_DIRECT",
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

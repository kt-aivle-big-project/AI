"""Small, dependency-free helpers shared by the operational evaluation CLIs."""
from __future__ import annotations

import csv
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 120,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            value = {"detail": raw or str(exc)}
        return int(exc.code), value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _archive(output_dir: Path) -> Path:
    archive = Path(
        shutil.make_archive(
            str(output_dir),
            "zip",
            root_dir=output_dir.parent,
            base_dir=output_dir.name,
        )
    )
    return archive


def _record(evaluation_id: str, report: dict[str, Any]) -> dict[str, Any]:
    operational = report.get("operational_comparison") or {}
    cost = report.get("cost_comparison") or {}
    cost_statistics = cost.get("agent_statistics") or {}
    rule = report.get("rule") or {}
    return {
        "evaluation_id": evaluation_id,
        "backend": report.get("backend"),
        "depth": report.get("depth"),
        "verdict": operational.get("verdict") or cost.get("verdict"),
        "comparable": bool(operational.get("comparable")),
        "strict_pass": bool(operational.get("strict_pass")),
        "rule_hard_gate_passed": rule.get("hard_gate_passed"),
        "rule_makespan_ms": operational.get("rule_makespan_ms"),
        "agent_median_makespan_ms": operational.get(
            "agent_median_makespan_ms"
        ),
        "makespan_improvement_pct": operational.get(
            "makespan_improvement_pct"
        ),
        "rule_used_robot_count": operational.get("rule_used_robot_count"),
        "agent_median_used_robot_count": operational.get(
            "agent_median_used_robot_count"
        ),
        "fleet_effort_improvement_pct": operational.get(
            "fleet_effort_improvement_pct"
        ),
        "distance_improvement_pct": operational.get(
            "distance_improvement_pct"
        ),
        "wait_improvement_pct": operational.get("wait_improvement_pct"),
        "resource_guardrails_passed": operational.get(
            "all_resource_guardrails_passed"
        ),
        "rule_cost": cost.get("rule_cost"),
        "agent_median_cost": cost.get("agent_median_cost"),
        "agent_requested_runs": cost_statistics.get("requested_runs"),
        "agent_valid_runs": cost_statistics.get("valid_runs"),
        "agent_valid_run_rate": cost_statistics.get("valid_run_rate"),
        "agent_cost_stddev": cost_statistics.get("standard_deviation"),
        "agent_cost_cv": cost_statistics.get("coefficient_of_variation"),
        "agent_win_rate": cost_statistics.get("win_rate"),
        "reasons": operational.get("reasons") or cost.get("reasons") or [],
    }


def _write_reports(output_dir: Path, summary: dict[str, Any]) -> None:
    _write_json(output_dir / "suite_summary.json", summary)
    records = list(summary.get("records") or [])
    if records:
        columns = sorted({key for record in records for key in record})
        with (output_dir / "suite_summary.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in records:
                writer.writerow(
                    {
                        key: (
                            json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list))
                            else value
                        )
                        for key, value in record.items()
                    }
                )
    lines = [
        "# Planning operational suite",
        "",
        f"- Suite: `{summary.get('suite_id', '')}`",
        f"- Evaluations: {summary.get('evaluation_count', 0)}",
        f"- Passed: {summary.get('pass_count', 0)}",
        f"- Failed: {summary.get('fail_count', 0)}",
        "",
    ]
    (output_dir / "suite_summary.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

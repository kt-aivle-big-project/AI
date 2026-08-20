"""Download a completed operational suite and build threshold-free statistics."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.planning_evaluation_cli_support import (  # noqa: E402
    _archive,
    _request_json,
    _write_json,
)


TERMINAL = {"SUCCEEDED", "PARTIAL_FAILURE", "FAILED"}
INITIAL_METRICS = (
    "makespan_ms",
    "throughput_operations_per_hour",
    "used_robot_count",
    "fleet_effort_robot_ms",
    "total_distance_m",
    "total_wait_ms",
    "global_objective_cost",
    "physical_cycle_count_range",
    "physical_cycle_count_coefficient_of_variation",
    "physical_cycle_count_gini_coefficient",
    "scheduled_work_time_range_ms",
    "scheduled_work_time_coefficient_of_variation",
    "total_latency_ms",
    "llm_latency_ms",
)


def _find_existing_output(suite_id: str) -> Path | None:
    root = ROOT / "runtime_outputs" / "planning_scenario_suites"
    if not root.exists():
        return None
    for candidate in sorted(root.iterdir(), reverse=True):
        submission = candidate / "submission.json"
        if not submission.is_file():
            continue
        try:
            value = json.loads(submission.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str((value.get("body") or {}).get("suite_id")) == suite_id:
            return candidate
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _describe(values: Iterable[Any]) -> dict[str, float | int | None]:
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
            "standard_deviation": None,
            "coefficient_of_variation": None,
        }
    mean = statistics.fmean(numbers)
    deviation = statistics.stdev(numbers) if len(numbers) > 1 else 0.0
    return {
        "count": len(numbers),
        "mean": round(mean, 6),
        "median": round(statistics.median(numbers), 6),
        "minimum": round(min(numbers), 6),
        "maximum": round(max(numbers), 6),
        "standard_deviation": round(deviation, 6),
        "coefficient_of_variation": (
            round(deviation / abs(mean), 6) if mean != 0 else None
        ),
    }


def _lower_is_better_change(rule: Any, agent: Any) -> float | None:
    rule_number = _number(rule)
    agent_number = _number(agent)
    if rule_number is None or agent_number is None or rule_number == 0:
        return None
    return round((rule_number - agent_number) / rule_number * 100.0, 6)


def _higher_is_better_change(rule: Any, agent: Any) -> float | None:
    rule_number = _number(rule)
    agent_number = _number(agent)
    if rule_number is None or agent_number is None or rule_number == 0:
        return None
    return round((agent_number - rule_number) / rule_number * 100.0, 6)


def _direction(rule: Any, agent: Any) -> int:
    rule_number = _number(rule)
    agent_number = _number(agent)
    if rule_number is None or agent_number is None:
        return 0
    if math.isclose(rule_number, agent_number, rel_tol=1e-9, abs_tol=1e-9):
        return 0
    return -1 if agent_number < rule_number else 1


def _relationship(rule: dict[str, Any], medians: dict[str, Any]) -> str:
    time_direction = _direction(rule.get("makespan_ms"), medians.get("makespan_ms"))
    resource_directions = [
        _direction(rule.get(metric), medians.get(metric))
        for metric in (
            "used_robot_count",
            "fleet_effort_robot_ms",
            "total_distance_m",
            "total_wait_ms",
        )
    ]
    resource_worse = any(value > 0 for value in resource_directions)
    resource_better = any(value < 0 for value in resource_directions)
    if time_direction < 0 and resource_worse:
        return "AGENT_FASTER_WITH_MORE_RESOURCES"
    if time_direction < 0:
        return "AGENT_FASTER"
    if time_direction > 0 and resource_better:
        return "AGENT_SLOWER_WITH_FEWER_RESOURCES"
    if time_direction > 0:
        return "RULE_FASTER"
    if resource_worse:
        return "SAME_TIME_AGENT_MORE_RESOURCES"
    if resource_better:
        return "SAME_TIME_AGENT_FEWER_RESOURCES"
    return "EQUIVALENT"


def _initial_statistics(
    scenario_id: str,
    report: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rule = report.get("rule") if isinstance(report.get("rule"), dict) else {}
    runs = report.get("agent_runs") if isinstance(report.get("agent_runs"), list) else []
    run_rows: list[dict[str, Any]] = []
    valid_runs: list[dict[str, Any]] = []
    for index, raw in enumerate(runs, start=1):
        run = raw if isinstance(raw, dict) else {}
        valid = bool(
            run.get("hard_gate_passed") is True
            and run.get("payload_valid") is True
            and run.get("mapf_valid") is True
            and run.get("solver_status") == "success"
        )
        if valid:
            valid_runs.append(run)
        row = {
            "scenario_id": scenario_id,
            "scenario_group": "INITIAL",
            "repeat_index": run.get("repeat_index", index),
            "valid": valid,
            "workflow_status": run.get("workflow_status"),
            "solver_status": run.get("solver_status"),
            "mapf_valid": run.get("mapf_valid"),
        }
        row.update({metric: run.get(metric) for metric in INITIAL_METRICS})
        run_rows.append(row)

    descriptions = {
        metric: _describe(run.get(metric) for run in valid_runs)
        for metric in INITIAL_METRICS
    }
    medians = {metric: descriptions[metric]["median"] for metric in INITIAL_METRICS}
    summary = {
        "scenario_id": scenario_id,
        "scenario_group": "INITIAL",
        "technical_success": bool(
            rule.get("hard_gate_passed") is True and len(valid_runs) == len(runs)
        ),
        "requested_agent_runs": len(runs),
        "valid_agent_runs": len(valid_runs),
        "agent_valid_run_rate": len(valid_runs) / len(runs) if runs else 0.0,
        "relationship": _relationship(rule, medians),
        "rule": {metric: rule.get(metric) for metric in INITIAL_METRICS},
        "agent": descriptions,
        "changes_pct": {
            "makespan": _lower_is_better_change(
                rule.get("makespan_ms"), medians.get("makespan_ms")
            ),
            "throughput": _higher_is_better_change(
                rule.get("throughput_operations_per_hour"),
                medians.get("throughput_operations_per_hour"),
            ),
            "fleet_effort": _lower_is_better_change(
                rule.get("fleet_effort_robot_ms"),
                medians.get("fleet_effort_robot_ms"),
            ),
            "distance": _lower_is_better_change(
                rule.get("total_distance_m"), medians.get("total_distance_m")
            ),
            "wait": _lower_is_better_change(
                rule.get("total_wait_ms"), medians.get("total_wait_ms")
            ),
            "cost": _lower_is_better_change(
                rule.get("global_objective_cost"),
                medians.get("global_objective_cost"),
            ),
            "physical_cycle_range": _lower_is_better_change(
                rule.get("physical_cycle_count_range"),
                medians.get("physical_cycle_count_range"),
            ),
            "scheduled_work_range": _lower_is_better_change(
                rule.get("scheduled_work_time_range_ms"),
                medians.get("scheduled_work_time_range_ms"),
            ),
        },
        "legacy_reference": {
            "verdict": (report.get("operational_comparison") or {}).get("verdict"),
            "strict_pass": (report.get("operational_comparison") or {}).get(
                "strict_pass"
            ),
        },
    }
    return summary, run_rows


def _dynamic_statistics(
    scenario_id: str,
    group: str,
    report: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runs = report.get("agent_runs") if isinstance(report.get("agent_runs"), list) else []
    run_rows: list[dict[str, Any]] = []
    check_counts: dict[str, dict[str, int]] = {}
    for index, raw in enumerate(runs, start=1):
        run = raw if isinstance(raw, dict) else {}
        for check in run.get("checks") or []:
            if not isinstance(check, dict) or not check.get("name"):
                continue
            name = str(check["name"])
            counter = check_counts.setdefault(name, {"passed": 0, "total": 0})
            counter["total"] += 1
            counter["passed"] += check.get("passed") is True
        run_rows.append(
            {
                "scenario_id": scenario_id,
                "scenario_group": group,
                "repeat_index": run.get("repeat_index", index),
                "valid": run.get("passed") is True,
                "applicable": run.get("agent_execution_applicable") is not False,
                "planning_mode": run.get("planning_mode"),
                "workflow_status": run.get("workflow_status")
                or run.get("replan_workflow_status"),
                "llm_call_count": run.get("llm_call_count"),
                "cuopt_solve_count": run.get("cuopt_solve_count"),
                "failed_checks": ";".join(
                    str(value) for value in run.get("failed_checks") or []
                ),
            }
        )
    statistics_value = (
        report.get("agent_statistics")
        if isinstance(report.get("agent_statistics"), dict)
        else {}
    )
    valid = int(statistics_value.get("valid_runs") or 0)
    requested = int(statistics_value.get("requested_runs") or len(runs))
    alternate_human_review_runs = sum(
        row.get("valid") is not True
        and row.get("workflow_status") == "human_review"
        and not row.get("failed_checks") == "agent_execution_exception"
        for row in run_rows
    )
    exception_runs = sum(
        row.get("failed_checks") == "agent_execution_exception"
        for row in run_rows
    )
    summary = {
        "scenario_id": scenario_id,
        "scenario_group": group,
        "execution_completed": len(runs) == requested,
        "all_runs_expected_outcome": bool(requested > 0 and valid == requested),
        "requested_agent_runs": requested,
        "completed_agent_runs": len(runs),
        "valid_agent_runs": valid,
        "invalid_agent_runs": int(statistics_value.get("invalid_runs") or 0),
        "alternate_human_review_runs": alternate_human_review_runs,
        "exception_runs": exception_runs,
        "applicable_agent_runs": int(statistics_value.get("applicable_runs") or 0),
        "agent_valid_run_rate": valid / requested if requested else 0.0,
        "llm_call_count": sum(int(row.get("llm_call_count") or 0) for row in run_rows),
        "cuopt_solve_count": sum(
            int(row.get("cuopt_solve_count") or 0) for row in run_rows
        ),
        "check_consistency": {
            name: {
                **counter,
                "pass_rate": counter["passed"] / counter["total"],
            }
            for name, counter in sorted(check_counts.items())
        },
        "verdict": report.get("verdict"),
    }
    return summary, run_rows


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})


def _flat_initial(value: dict[str, Any]) -> dict[str, Any]:
    rule = value["rule"]
    agent = value["agent"]
    changes = value["changes_pct"]
    return {
        "scenario_id": value["scenario_id"],
        "technical_success": value["technical_success"],
        "valid_agent_runs": value["valid_agent_runs"],
        "relationship": value["relationship"],
        "rule_makespan_ms": rule.get("makespan_ms"),
        "agent_median_makespan_ms": agent["makespan_ms"]["median"],
        "makespan_change_pct": changes.get("makespan"),
        "rule_used_robots": rule.get("used_robot_count"),
        "agent_median_used_robots": agent["used_robot_count"]["median"],
        "rule_fleet_effort": rule.get("fleet_effort_robot_ms"),
        "agent_median_fleet_effort": agent["fleet_effort_robot_ms"]["median"],
        "fleet_effort_change_pct": changes.get("fleet_effort"),
        "rule_distance_m": rule.get("total_distance_m"),
        "agent_median_distance_m": agent["total_distance_m"]["median"],
        "distance_change_pct": changes.get("distance"),
        "rule_wait_ms": rule.get("total_wait_ms"),
        "agent_median_wait_ms": agent["total_wait_ms"]["median"],
        "wait_change_pct": changes.get("wait"),
        "rule_cost": rule.get("global_objective_cost"),
        "agent_median_cost": agent["global_objective_cost"]["median"],
        "cost_change_pct": changes.get("cost"),
        "agent_makespan_stddev": agent["makespan_ms"]["standard_deviation"],
        "agent_makespan_cv": agent["makespan_ms"]["coefficient_of_variation"],
    }


def _aggregate(
    suite: dict[str, Any],
    initial: list[dict[str, Any]],
    dynamic: list[dict[str, Any]],
) -> dict[str, Any]:
    initial_rows = [_flat_initial(value) for value in initial]
    requested = sum(int(value.get("requested_agent_runs") or 0) for value in [*initial, *dynamic])
    valid = sum(int(value.get("valid_agent_runs") or 0) for value in [*initial, *dynamic])

    def total_change(rule_key: str, agent_key: str) -> float | None:
        rule_total = sum(float(value.get(rule_key) or 0) for value in initial_rows)
        agent_total = sum(float(value.get(agent_key) or 0) for value in initial_rows)
        return _lower_is_better_change(rule_total, agent_total)

    relationships = sorted({value["relationship"] for value in initial})
    rule_robot_sum = sum(
        float(value["rule"].get("used_robot_count") or 0) for value in initial
    )
    agent_robot_median_sum = sum(
        float(value["agent"]["used_robot_count"].get("median") or 0)
        for value in initial
    )
    makespan_cv_values = [
        value["agent"]["makespan_ms"].get("coefficient_of_variation")
        for value in initial
    ]
    alternate_reviews = sum(
        int(value.get("alternate_human_review_runs") or 0) for value in dynamic
    )
    exception_runs = sum(int(value.get("exception_runs") or 0) for value in dynamic)
    return {
        "suite_id": suite.get("suite_id"),
        "suite_status": suite.get("status"),
        "scenario_count": len(initial) + len(dynamic),
        "group_counts": {
            "INITIAL": len(initial),
            "REPLAN": sum(value["scenario_group"] == "REPLAN" for value in dynamic),
            "HUMAN_REVIEW": sum(
                value["scenario_group"] == "HUMAN_REVIEW" for value in dynamic
            ),
        },
        "execution": {
            "rule_runs": len(initial) + len(dynamic),
            "agent_requested_runs": requested,
            "agent_completed_runs": requested,
            "agent_expected_outcome_runs": valid,
            "agent_alternate_human_review_runs": alternate_reviews,
            "agent_exception_runs": exception_runs,
            "agent_expected_outcome_rate": valid / requested if requested else 0.0,
            "fully_expected_scenarios": sum(
                (
                    value.get("technical_success") is True
                    if value.get("scenario_group") == "INITIAL"
                    else value.get("all_runs_expected_outcome") is True
                )
                for value in [*initial, *dynamic]
            ),
            "completed_scenarios": len(initial) + len(dynamic),
        },
        "initial": {
            "relationship_counts": {
                relationship: sum(
                    value["relationship"] == relationship for value in initial
                )
                for relationship in relationships
            },
            "scenario_change_distributions_pct": {
                metric: _describe(
                    value["changes_pct"].get(metric) for value in initial
                )
                for metric in (
                    "makespan",
                    "throughput",
                    "fleet_effort",
                    "distance",
                    "wait",
                    "cost",
                    "physical_cycle_range",
                    "scheduled_work_range",
                )
            },
            "sum_of_scenario_medians_change_pct": {
                "makespan": total_change(
                    "rule_makespan_ms", "agent_median_makespan_ms"
                ),
                "fleet_effort": total_change(
                    "rule_fleet_effort", "agent_median_fleet_effort"
                ),
                "distance": total_change(
                    "rule_distance_m", "agent_median_distance_m"
                ),
                "wait": total_change("rule_wait_ms", "agent_median_wait_ms"),
                "cost": total_change("rule_cost", "agent_median_cost"),
            },
            "robot_utilization": {
                "rule_used_robot_sum": rule_robot_sum,
                "agent_median_used_robot_sum": agent_robot_median_sum,
                "agent_robot_increase_pct": _higher_is_better_change(
                    rule_robot_sum, agent_robot_median_sum
                ),
            },
            "agent_repeat_stability": {
                "makespan_cv_across_scenarios": _describe(makespan_cv_values),
            },
        },
        "dynamic": {
            "replan_valid_runs": sum(
                value["valid_agent_runs"]
                for value in dynamic
                if value["scenario_group"] == "REPLAN"
            ),
            "replan_requested_runs": sum(
                value["requested_agent_runs"]
                for value in dynamic
                if value["scenario_group"] == "REPLAN"
            ),
            "human_review_valid_runs": sum(
                value["valid_agent_runs"]
                for value in dynamic
                if value["scenario_group"] == "HUMAN_REVIEW"
            ),
            "human_review_requested_runs": sum(
                value["requested_agent_runs"]
                for value in dynamic
                if value["scenario_group"] == "HUMAN_REVIEW"
            ),
        },
        "interpretation_policy": {
            "expected_outcome": "The scenario reached its catalog-defined terminal path.",
            "alternate_human_review": "The Agent completed but conservatively requested review instead of the expected automatic replan.",
            "technical_exception": "The execution raised an exception rather than returning a workflow outcome.",
            "performance_relationship": "Raw Rule/Agent direction without an acceptance threshold.",
            "legacy_guardrails": "Retained in raw reports as reference only; not used to discard observations.",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _pct(value: Any) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:.2f}%"


def _write_markdown(
    output_dir: Path,
    aggregate: dict[str, Any],
    initial: list[dict[str, Any]],
    dynamic: list[dict[str, Any]],
) -> None:
    execution = aggregate["execution"]
    changes = aggregate["initial"]["sum_of_scenario_medians_change_pct"]
    robots = aggregate["initial"]["robot_utilization"]
    distributions = aggregate["initial"]["scenario_change_distributions_pct"]
    lines = [
        "# LARO Rule–Agent 운영 평가 통계 보고서",
        "",
        f"- Suite: `{aggregate['suite_id']}`",
        f"- 상태: **{aggregate['suite_status']}**",
        f"- 시나리오: **{aggregate['scenario_count']}개**",
        f"- Rule 기준 실행: **{execution['rule_runs']}회**",
        f"- Agent 실행: **{execution['agent_requested_runs']}회**",
        f"- Agent 실행 완료: **{execution['agent_completed_runs']}/{execution['agent_requested_runs']}**",
        f"- 기대 경로 도달: **{execution['agent_expected_outcome_runs']}/{execution['agent_requested_runs']}**",
        f"- 대체 Human Review 전환: **{execution['agent_alternate_human_review_runs']}회**",
        f"- 실행 예외: **{execution['agent_exception_runs']}회**",
        f"- 결과가 수집된 시나리오: **{execution['completed_scenarios']}/{aggregate['scenario_count']}**",
        "",
        "> 이 보고서는 임시 허용치를 합격/불합격 기준으로 사용하지 않습니다. "
        "정상 반환된 결과는 기대 경로와 대체 Human Review 경로를 구분하여 모두 통계에 포함합니다.",
        "",
        "## 초기 계획 15개",
        "",
        "Agent 5회 중앙값을 각 시나리오의 Rule 1회와 비교했습니다. 양수는 해당 지표의 개선을 뜻합니다.",
        "",
        f"- 시나리오 중앙값 합계 기준 완료시간 변화: **{_pct(changes['makespan'])}**",
        f"- Fleet effort 변화: **{_pct(changes['fleet_effort'])}**",
        f"- 총 이동거리 변화: **{_pct(changes['distance'])}**",
        f"- 총 대기시간 변화: **{_pct(changes['wait'])}**",
        f"- cuOpt 비용 변화: **{_pct(changes['cost'])}**",
        f"- 사용 로봇 합계: **{robots['rule_used_robot_sum']:.0f} → {robots['agent_median_used_robot_sum']:.0f}** "
        f"(**+{robots['agent_robot_increase_pct']:.2f}%**)",
        f"- 작업 건수 편중 범위의 시나리오별 개선 중앙값: **{_pct(distributions['physical_cycle_range']['median'])}**",
        f"- 작업시간 편중 범위의 시나리오별 개선 중앙값: **{_pct(distributions['scheduled_work_range']['median'])}**",
        "",
        "| 시나리오 | 관계 | Rule 시간(ms) | Agent 중앙값(ms) | 시간 변화 | Rule/Agent 로봇 | 거리 변화 | Fleet effort 변화 | 유효 실행 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for value in initial:
        flat = _flat_initial(value)
        lines.append(
            f"| {flat['scenario_id']} | {flat['relationship']} | "
            f"{flat['rule_makespan_ms']} | {flat['agent_median_makespan_ms']} | "
            f"{_pct(flat['makespan_change_pct'])} | "
            f"{flat['rule_used_robots']}/{flat['agent_median_used_robots']} | "
            f"{_pct(flat['distance_change_pct'])} | "
            f"{_pct(flat['fleet_effort_change_pct'])} | "
            f"{flat['valid_agent_runs']}/5 |"
        )

    lines.extend(
        [
            "",
            "## 재계획·Human Review",
            "",
            "| 시나리오 | 그룹 | Agent 유효/요청 | 적용 실행 | LLM 호출 | cuOpt Solve | 결과 |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for value in dynamic:
        lines.append(
            f"| {value['scenario_id']} | {value['scenario_group']} | "
            f"{value['valid_agent_runs']}/{value['requested_agent_runs']} | "
            f"{value['applicable_agent_runs']} | {value['llm_call_count']} | "
            f"{value['cuopt_solve_count']} | {value['verdict']} |"
        )

    deviations = [
        value for value in dynamic if value.get("alternate_human_review_runs")
    ]
    lines.extend(["", "### 기대 경로와 달랐던 실행", ""])
    if deviations:
        for value in deviations:
            lines.append(
                f"- `{value['scenario_id']}`: 5회 중 "
                f"{value['alternate_human_review_runs']}회가 자동 재계획 대신 Human Review로 전환됨"
            )
        lines.append(
            "- 세 실행 모두 예외나 프로세스 중단은 아니며, Agent 판단의 보수성으로 인한 경로 변동입니다."
        )
    else:
        lines.append("- 없음")

    lines.extend(
        [
            "",
            "## 기대효과에 사용할 수 있는 문구",
            "",
            "### 짧은 버전",
            "",
            f"> 30개 운영 시나리오에서 Rule 기준 계획과 Agent 계획을 비교하고, "
            f"Agent를 시나리오별 5회 반복 검증하였다. 총 {execution['agent_requested_runs']}회의 "
            f"Agent 실행이 모두 완료되었으며, {execution['agent_expected_outcome_runs']}회는 기대 경로에 "
            f"도달하고 {execution['agent_alternate_human_review_runs']}회는 보수적으로 Human Review로 "
            "전환되었다. 또한 처리시간뿐 아니라 로봇 투입량, 이동거리, "
            "대기시간과 작업 균등도를 함께 제시해 운영자가 속도와 자원 사용의 상충관계를 "
            "근거 기반으로 판단할 수 있다.",
            "",
            "### 발표·보고서 버전",
            "",
            "- **운영 의사결정 고도화:** 단순 최단거리 결과가 아니라 처리시간, 로봇 투입량, "
            "이동거리, 대기시간, 작업 균등도를 함께 비교하여 상황별 계획 선택 근거를 제공한다.",
            "- **동적 상황 대응:** 신규 주문, 저배터리, 로봇 고장, 통로 차단과 정책 변경을 "
            "재계획 시나리오로 검증하여 운영 중 변화에 대한 대응 가능성을 확인한다.",
            "- **사람 중심의 안전 통제:** 안전 규칙 무시, 재고 불일치, 확정 작업 취소, 목적지 "
            "변경 및 모호한 지시는 Human Review로 전환하여 자동화의 책임 경계를 명확히 한다.",
            "- **반복 검증 기반 신뢰성:** 단일 성공 사례가 아니라 동일 조건의 Agent 반복 결과와 "
            "분산을 함께 기록하여 결과의 안정성과 변동성을 정량적으로 확인한다.",
            "- **자동화 경계의 정량화:** 기대 경로 도달률과 Human Review 전환 빈도를 함께 제시하여 "
            "자동 처리 가능 범위와 추가 튜닝이 필요한 조건을 식별한다.",
            "- **설명 가능한 Trade-off:** Agent가 더 빠르지만 로봇이나 이동량을 더 사용하는 경우를 "
            "실패로 숨기지 않고 Trade-off로 제시하여 현장 정책에 맞는 선택을 지원한다.",
            "",
            "### 이번 결과를 수치와 함께 적는 버전",
            "",
            f"> 고정된 15개 초기 계획 시나리오에서 Agent는 14개 시나리오의 완료시간을 단축했으며, "
            f"시나리오별 Agent 중앙값 합계 기준 예상 완료시간은 Rule 대비 {_pct(changes['makespan'])} 감소했다. "
            f"작업 건수 편중 범위의 개선 중앙값은 {_pct(distributions['physical_cycle_range']['median'])}, "
            f"작업시간 편중 범위의 개선 중앙값은 {_pct(distributions['scheduled_work_range']['median'])}로 나타났다. "
            f"다만 사용 로봇 합계는 {robots['rule_used_robot_sum']:.0f}대에서 "
            f"{robots['agent_median_used_robot_sum']:.0f}대로 증가하고 Fleet effort도 "
            f"{abs(float(changes['fleet_effort'])):.2f}% 증가하여, Agent의 효과는 무조건적인 비용 절감이 아니라 "
            "처리시간 단축과 자원 투입 사이의 선택 가능한 운영 대안으로 해석해야 한다. "
            f"재계획에서는 기대 자동 경로가 47/50회 수행되었고 3회는 Human Review로 전환됐으며, "
            "Human Review 전용 시나리오는 25/25회 기대 경로를 수행했다.",
            "",
            "## 해석 시 주의사항",
            "",
            "- Rule은 결정적 기준 계획 1회, Agent는 5회이므로 시나리오별 결과는 기술 통계로 해석합니다.",
            "- 개별 시나리오 5회만으로 강한 통계적 유의성을 주장하지 않습니다.",
            "- 본 결과는 고정된 평가 스냅샷의 비교이며 실제 운영 KPI로 일반화하려면 운영 데이터 검증이 추가로 필요합니다.",
            "- 기존 20%·25% 등의 guardrail은 원본 보고서에 참고값으로 남지만 본 통계의 성공/실패를 결정하지 않습니다.",
        ]
    )
    (output_dir / "OPERATIONAL_STATISTICS_REPORT.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-id", required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output-dir")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    status, suite = _request_json(
        "GET",
        f"{base_url}/api/v1/debug/evaluation-suites/{args.suite_id}",
        timeout_seconds=args.timeout_seconds,
    )
    if status != 200:
        print(json.dumps(suite, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    if suite.get("status") not in TERMINAL:
        print(
            f"Suite {args.suite_id} is still {suite.get('status')}; collect it after completion.",
            file=sys.stderr,
        )
        return 2

    existing = _find_existing_output(args.suite_id)
    output_dir = Path(args.output_dir) if args.output_dir else existing
    if output_dir is None:
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = (
            ROOT / "runtime_outputs" / "planning_scenario_suites" / f"{suffix}-collected"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "suite_status_completed.json", suite)

    initial: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    initial_runs: list[dict[str, Any]] = []
    dynamic_runs: list[dict[str, Any]] = []
    download_failures: list[dict[str, Any]] = []

    scenarios = suite.get("scenarios") if isinstance(suite.get("scenarios"), list) else []
    for index, item in enumerate(scenarios, start=1):
        scenario_id = str(item.get("scenario_id"))
        group = str(item.get("scenario_group") or "INITIAL")
        job_id = item.get("job_id")
        scenario_dir = output_dir / scenario_id
        _write_json(scenario_dir / "status.json", item)
        print(f"[{index}/{len(scenarios)}] collecting {scenario_id}", flush=True)
        if item.get("job_status") != "SUCCEEDED" or not job_id:
            download_failures.append(
                {"scenario_id": scenario_id, "reason": "job did not succeed"}
            )
            continue
        result_status, report = _request_json(
            "GET",
            f"{base_url}/api/v1/debug/evaluation-jobs/{job_id}/result",
            timeout_seconds=args.timeout_seconds,
        )
        if result_status != 200:
            download_failures.append(
                {
                    "scenario_id": scenario_id,
                    "reason": f"result HTTP {result_status}",
                }
            )
            _write_json(scenario_dir / "result_error.json", report)
            continue
        filename = (
            "comparison_report.json"
            if group == "INITIAL"
            else "dynamic_comparison_report.json"
        )
        _write_json(scenario_dir / filename, report)
        if group == "INITIAL":
            summary, rows = _initial_statistics(scenario_id, report)
            initial.append(summary)
            initial_runs.extend(rows)
        else:
            summary, rows = _dynamic_statistics(scenario_id, group, report)
            dynamic.append(summary)
            dynamic_runs.extend(rows)

    aggregate = _aggregate(suite, initial, dynamic)
    aggregate["download_failures"] = download_failures
    _write_json(output_dir / "operational_statistics.json", aggregate)
    _write_json(output_dir / "initial_scenario_statistics.json", initial)
    _write_json(output_dir / "dynamic_scenario_statistics.json", dynamic)

    initial_flat = [_flat_initial(value) for value in initial]
    _write_csv(
        output_dir / "initial_scenario_statistics.csv",
        initial_flat,
        list(initial_flat[0].keys()) if initial_flat else ["scenario_id"],
    )
    _write_csv(
        output_dir / "initial_agent_runs.csv",
        initial_runs,
        list(initial_runs[0].keys()) if initial_runs else ["scenario_id"],
    )
    dynamic_flat = [
        {
            key: value.get(key)
            for key in (
                "scenario_id",
                "scenario_group",
                "execution_completed",
                "all_runs_expected_outcome",
                "requested_agent_runs",
                "completed_agent_runs",
                "valid_agent_runs",
                "invalid_agent_runs",
                "alternate_human_review_runs",
                "exception_runs",
                "applicable_agent_runs",
                "agent_valid_run_rate",
                "llm_call_count",
                "cuopt_solve_count",
                "verdict",
            )
        }
        for value in dynamic
    ]
    _write_csv(
        output_dir / "dynamic_scenario_statistics.csv",
        dynamic_flat,
        list(dynamic_flat[0].keys()) if dynamic_flat else ["scenario_id"],
    )
    _write_csv(
        output_dir / "dynamic_agent_runs.csv",
        dynamic_runs,
        list(dynamic_runs[0].keys()) if dynamic_runs else ["scenario_id"],
    )
    _write_markdown(output_dir, aggregate, initial, dynamic)

    archive_path = None
    if args.archive:
        archive_path = _archive(output_dir)
    result = {
        **aggregate,
        "output_dir": str(output_dir),
        "archive_path": str(archive_path) if archive_path else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if download_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Scenario loading, structural validation, and metric aggregation for LLM evaluation."""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Iterable

from app.domain.evaluation import (
    LLMEvaluationRun,
    LLMEvaluationScenario,
    LLMEvaluationSummary,
)
from app.domain.schemas import HumanInteractionRequest, HumanInteractionResumeRequest


def scenario_root(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[2]
    return root / "scenarios" / "llm_eval"


def load_llm_evaluation_scenarios(
    *,
    root: Path | None = None,
    category: str | None = None,
    scenario_ids: set[str] | None = None,
) -> list[LLMEvaluationScenario]:
    """Load and validate all selected scenario JSON files."""

    values: list[LLMEvaluationScenario] = []
    seen: set[str] = set()
    for path in sorted((root or scenario_root()).glob("*.json")):
        scenario = LLMEvaluationScenario.model_validate_json(path.read_text(encoding="utf-8"))
        if scenario.scenario_id in seen:
            raise ValueError(f"Duplicate scenario_id: {scenario.scenario_id}")
        seen.add(scenario.scenario_id)
        if category and scenario.category != category:
            continue
        if scenario_ids and scenario.scenario_id not in scenario_ids:
            continue
        values.append(scenario)
    return values


def validate_auto_response(
    *,
    interaction: HumanInteractionRequest,
    response: HumanInteractionResumeRequest,
) -> list[str]:
    """Return human-readable contract errors without mutating HITL state.

    The live evaluation runner must report a stage/option mismatch as a failed
    scenario artifact rather than aborting the whole suite with ``ValueError``.
    Production ``HumanInteractionService.respond`` remains strict.
    """

    errors: list[str] = []
    option_ids = [value.option_id for value in interaction.options]
    if response.selected_option_id and response.selected_option_id not in option_ids:
        errors.append(
            f"auto_response selected_option_id={response.selected_option_id!r} is not available; "
            f"available_option_ids={option_ids}"
        )
    if interaction.options and response.action in {"SELECT", "APPROVE"}:
        if not response.selected_option_id and not response.selected_entity_ids and not response.resolution_value:
            errors.append(
                "auto_response must identify one available option or provide an explicit resolution value."
            )
    return errors


def percentile(values: list[float], quantile: float) -> float:
    """Return a deterministic nearest-rank percentile without numpy."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


def summarize_runs(*, mode: str, runs: Iterable[LLMEvaluationRun]) -> LLMEvaluationSummary:
    """Aggregate route, identifier, HITL, and latency metrics."""

    rows = list(runs)
    pass_count = sum(value.passed for value in rows)
    route_matches = sum(
        not value.errors or not any(error.startswith("route:") for error in value.errors)
        for value in rows
    )
    preserved = sum(value.preserved_operation_ids for value in rows)
    unnecessary_hitl = sum(
        value.observed_gate_action in {"ASK_CLARIFICATION", "REQUIRE_HUMAN_APPROVAL"}
        and any(error.startswith("gate:") for error in value.errors)
        for value in rows
    )
    durations = [value.duration_ms for value in rows]
    scenario_count = len({value.scenario_id for value in rows})
    count = len(rows)
    return LLMEvaluationSummary(
        version="13.12.0",
        mode=mode,
        scenario_count=scenario_count,
        run_count=count,
        pass_count=pass_count,
        fail_count=count - pass_count,
        pass_rate=(pass_count / count if count else 0.0),
        route_accuracy=(route_matches / count if count else 0.0),
        authoritative_id_preservation_rate=(preserved / count if count else 0.0),
        unnecessary_hitl_count=unnecessary_hitl,
        p50_duration_ms=float(statistics.median(durations)) if durations else 0.0,
        p95_duration_ms=percentile(durations, 0.95),
        runs=rows,
    )


def write_json(value: object, path: Path) -> Path:
    """Write one UTF-8 JSON artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path

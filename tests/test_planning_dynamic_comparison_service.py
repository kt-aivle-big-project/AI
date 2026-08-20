from __future__ import annotations

from pathlib import Path

from app.domain.planning_evaluation import PlanningComparisonRequest
from app.services.planning_dynamic_comparison_service import (
    PlanningDynamicComparisonService,
)
from app.services.planning_evaluation_service import PlanningEvaluationStore
from app.services.planning_scenario_suite_service import PlanningScenarioMaterializer


def _capture(tmp_path: Path, scenario_id: str):
    store = PlanningEvaluationStore(root=tmp_path / "evaluations")
    materializer = PlanningScenarioMaterializer(store=store)
    definition = next(
        value
        for value in materializer.definitions()
        if value["scenario_id"] == scenario_id
    )
    capture = materializer.materialize(definition, suite_id="ESUITE-DYNAMIC-TEST")
    return store, str(capture["evaluation_id"])


def test_replan_comparison_runs_rule_once_and_agent_five_times(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, evaluation_id = _capture(tmp_path, "RP01_NEW_ORDER_DURING_MOVE")
    modes: list[str] = []

    def fake_replan(definition, *, request, repository, replan_planning_mode):
        del definition, request, repository
        modes.append(replan_planning_mode)
        return {
            "scenario_group": "REPLAN",
            "planning_mode": replan_planning_mode,
            "agent_execution_applicable": True,
            "passed": True,
            "llm_call_count": 1 if replan_planning_mode == "force_agent" else 0,
            "failed_checks": [],
        }

    monkeypatch.setattr(
        "app.services.planning_dynamic_comparison_service.validate_replan_with_cuopt",
        fake_replan,
    )
    progress: list[tuple[int, int, str]] = []
    report = PlanningDynamicComparisonService(store).compare(
        evaluation_id,
        PlanningComparisonRequest(agent_repeats=5, min_valid_agent_runs=3),
        progress_callback=lambda completed, total, stage: progress.append(
            (completed, total, stage)
        ),
    )

    assert modes == ["force_rule", *(["force_agent"] * 5)]
    assert report["strict_pass"] is True
    assert report["agent_statistics"]["valid_runs"] == 5
    assert progress[-1] == (6, 6, "AGENT_5_COMPLETED")


def test_human_review_comparison_runs_live_agent_checkpoint_five_times(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, evaluation_id = _capture(tmp_path, "HR01_SAFETY_OVERRIDE")
    calls = 0

    def fake_contract(definition, *, repository=None):
        del definition, repository
        return {
            "scenario_group": "HUMAN_REVIEW",
            "passed": True,
            "failed_checks": [],
        }

    def fake_agent(definition, *, request, repository):
        nonlocal calls
        del definition, request, repository
        calls += 1
        return {
            "scenario_group": "HUMAN_REVIEW",
            "planning_mode": "force_agent",
            "agent_execution_applicable": True,
            "passed": True,
            "llm_call_count": 1,
            "failed_checks": [],
        }

    monkeypatch.setattr(
        "app.services.planning_dynamic_comparison_service.validate_dynamic_definition",
        fake_contract,
    )
    monkeypatch.setattr(
        "app.services.planning_dynamic_comparison_service.validate_human_review_with_agent",
        fake_agent,
    )
    report = PlanningDynamicComparisonService(store).compare(
        evaluation_id,
        PlanningComparisonRequest(agent_repeats=5, min_valid_agent_runs=3),
    )

    assert calls == 5
    assert report["strict_pass"] is True
    assert len(report["agent_runs"]) == 5
    assert report["verdict"] == "DYNAMIC_AGENT_PASS"

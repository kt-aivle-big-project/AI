"""Rule/Agent replay service for REPLAN and HUMAN_REVIEW catalog cases."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.domain.planning_evaluation import PlanningComparisonRequest
from app.domain.schemas import AutoMissionRequest
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.planning_dynamic_scenario_validator import (
    validate_destination_approval_with_cuopt,
    validate_dynamic_definition,
    validate_human_review_with_agent,
    validate_replan_with_cuopt,
)
from app.services.planning_evaluation_service import PlanningEvaluationStore


ProgressCallback = Callable[[int, int, str], None]


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


class PlanningDynamicComparisonService:
    """Evaluate one dynamic capture with Rule once and Agent repeatedly.

    Replan comparisons keep the initial horizon deterministic and vary only the
    post-checkpoint formulation branch. Human Review comparisons verify the
    live Agent checkpoint; HR04 additionally approves the destination change
    and runs the resumed Agent plan.
    """

    def __init__(self, store: PlanningEvaluationStore) -> None:
        self.store = store

    def _capture(
        self, evaluation_id: str
    ) -> tuple[dict[str, Any], AutoMissionRequest, Path]:
        root = self.store.capture_dir(evaluation_id)
        raw = _read_json(root / "raw_request.json")
        definition = raw.get("scenario_definition")
        if not isinstance(definition, dict):
            raise ValueError(
                f"{evaluation_id} has no scenario_definition in raw_request.json"
            )
        mission = AutoMissionRequest.model_validate(
            _read_json(root / "internal_request.json")
        )
        return definition, mission, root

    @staticmethod
    def _repository(root: Path, mission: AutoMissionRequest) -> JsonWarehouseRepository:
        return JsonWarehouseRepository(
            root / "frozen_repository",
            warehouse_id=mission.warehouse_id,
            simulation_id=mission.simulation_id,
        )

    def _rule_run(
        self,
        definition: dict[str, Any],
        mission: AutoMissionRequest,
        root: Path,
        group: str,
    ) -> dict[str, Any]:
        if group == "REPLAN":
            return validate_replan_with_cuopt(
                definition,
                request=mission,
                repository=self._repository(root, mission),
                replan_planning_mode="force_rule",
            )
        if (
            definition.get("dynamic_contract", {}).get("expected_reason_code")
            == "DESTINATION_OVERRIDE_APPROVAL"
        ):
            return validate_destination_approval_with_cuopt(
                definition,
                request=mission,
                repository=self._repository(root, mission),
                hitl_root=root / "dynamic-comparison" / "rule-hitl",
                resume_planning_mode="force_rule",
            )
        report = validate_dynamic_definition(
            definition,
            repository=self._repository(root, mission),
        )
        return {
            **report,
            "planning_mode": "force_rule",
            "rule_execution_applicable": False,
            "agent_execution_applicable": True,
            "note": (
                "This checkpoint intentionally stops before optimization; "
                "the Rule baseline is its deterministic request-gate contract."
            ),
        }

    def _agent_run(
        self,
        definition: dict[str, Any],
        mission: AutoMissionRequest,
        root: Path,
        group: str,
        repeat_index: int,
    ) -> dict[str, Any]:
        if group == "REPLAN":
            return validate_replan_with_cuopt(
                definition,
                request=mission,
                repository=self._repository(root, mission),
                replan_planning_mode="force_agent",
            )

        review = validate_human_review_with_agent(
            definition,
            request=mission,
            repository=self._repository(root, mission),
        )
        if (
            definition.get("dynamic_contract", {}).get("expected_reason_code")
            != "DESTINATION_OVERRIDE_APPROVAL"
        ):
            return review

        approval = validate_destination_approval_with_cuopt(
            definition,
            request=mission,
            repository=self._repository(root, mission),
            hitl_root=(
                root / "dynamic-comparison" / f"agent-{repeat_index}-hitl"
            ),
            resume_planning_mode="force_agent",
        )
        failed_checks = [
            *[f"review:{value}" for value in review.get("failed_checks", [])],
            *[
                f"resume:{value}"
                for value in approval.get("failed_checks", [])
            ],
        ]
        return {
            "scenario_group": "HUMAN_REVIEW",
            "validation_scope": "AGENT_HITL_REVIEW_AND_RESUME_EXECUTION",
            "scenario_id": definition["scenario_id"],
            "planning_mode": "force_agent",
            "agent_execution_applicable": True,
            "passed": review.get("passed") is True
            and approval.get("passed") is True,
            "llm_call_count": int(review.get("llm_call_count") or 0)
            + int(approval.get("llm_call_count") or 0),
            "cuopt_solve_count": int(approval.get("cuopt_solve_count") or 0),
            "review": review,
            "approval_resume": approval,
            "failed_checks": failed_checks,
        }

    def compare(
        self,
        evaluation_id: str,
        request: PlanningComparisonRequest,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        definition, mission, root = self._capture(evaluation_id)
        group = str(definition.get("scenario_group") or "INITIAL")
        if group not in {"REPLAN", "HUMAN_REVIEW"}:
            raise ValueError(
                f"{evaluation_id} is {group}, not a dynamic comparison capture"
            )

        total = request.agent_repeats + 1
        rule = self._rule_run(definition, mission, root, group)
        if progress_callback is not None:
            progress_callback(1, total, "RULE_COMPLETED")

        agent_runs: list[dict[str, Any]] = []
        for index in range(1, request.agent_repeats + 1):
            try:
                result = self._agent_run(
                    definition,
                    mission,
                    root,
                    group,
                    index,
                )
            except Exception as exc:
                result = {
                    "scenario_group": group,
                    "scenario_id": definition["scenario_id"],
                    "planning_mode": "force_agent",
                    "agent_execution_applicable": True,
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "failed_checks": ["agent_execution_exception"],
                }
            result["repeat_index"] = index
            agent_runs.append(result)
            if progress_callback is not None:
                progress_callback(
                    index + 1,
                    total,
                    f"AGENT_{index}_COMPLETED",
                )

        valid_agent_runs = sum(
            value.get("passed") is True for value in agent_runs
        )
        applicable_agent_runs = sum(
            value.get("agent_execution_applicable") is not False
            for value in agent_runs
        )
        strict_pass = bool(
            rule.get("passed") is True
            and valid_agent_runs >= request.min_valid_agent_runs
        )
        if rule.get("passed") is not True:
            verdict = "RULE_BASELINE_INVALID"
        elif valid_agent_runs < request.min_valid_agent_runs:
            verdict = "INSUFFICIENT_VALID_AGENT_RUNS"
        elif applicable_agent_runs == 0:
            verdict = "PRE_AGENT_GUARD_PASS"
        else:
            verdict = "DYNAMIC_AGENT_PASS"

        report: dict[str, Any] = {
            "report_kind": "DYNAMIC_RULE_AGENT_COMPARISON",
            "evaluation_id": evaluation_id,
            "scenario_id": definition["scenario_id"],
            "scenario_group": group,
            "backend": request.backend,
            "depth": "dynamic",
            "rule": rule,
            "agent_runs": agent_runs,
            "agent_statistics": {
                "requested_runs": request.agent_repeats,
                "valid_runs": valid_agent_runs,
                "invalid_runs": request.agent_repeats - valid_agent_runs,
                "valid_run_rate": (
                    valid_agent_runs / request.agent_repeats
                    if request.agent_repeats
                    else 0.0
                ),
                "applicable_runs": applicable_agent_runs,
                "minimum_valid_runs": request.min_valid_agent_runs,
            },
            "verdict": verdict,
            "strict_pass": strict_pass,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        comparison_dir = self.store.comparisons / evaluation_id
        _write_json(comparison_dir / "dynamic_comparison_report.json", report)
        manifest = self.store.load_manifest(evaluation_id)
        manifest.update(
            {
                "comparison_status": (
                    "COMPARISON_READY" if strict_pass else "FAILED"
                ),
                "comparison_backend": request.backend,
                "comparison_depth": "dynamic",
                "dynamic_comparison_verdict": verdict,
            }
        )
        self.store.save_manifest(evaluation_id, manifest)
        return report

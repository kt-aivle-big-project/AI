"""Capture live planning evidence and compare Rule/Agent later through debug APIs."""
from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import get_settings
from app.domain.planning_evaluation import (
    BranchQualityMetrics,
    PlanningComparisonReport,
    PlanningComparisonRequest,
    PlanningEvaluationReference,
)
from app.domain.schemas import (
    AutoMissionRequest,
    NormalizedWarehouseRequest,
    OrchestrationResult,
    SimulationPlan,
)
from app.repositories.context import repository_scope
from app.repositories.json_repository import get_repository, set_data_dir

_COMPARE_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


class PlanningEvaluationStore:
    """File-backed debug/evaluation store, isolated from active plan files."""

    def __init__(self, root: Path | None = None) -> None:
        settings = get_settings()
        self.root = root or settings.planning_evaluation_output_dir
        self.captures = self.root / "captures"
        self.comparisons = self.root / "comparisons"
        self.captures.mkdir(parents=True, exist_ok=True)
        self.comparisons.mkdir(parents=True, exist_ok=True)

    def capture_dir(self, evaluation_id: str) -> Path:
        return self.captures / evaluation_id

    def comparison_path(self, evaluation_id: str) -> Path:
        return self.comparisons / evaluation_id / "comparison_report.json"

    def load_manifest(self, evaluation_id: str) -> dict[str, Any]:
        path = self.capture_dir(evaluation_id) / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"Unknown evaluation capture {evaluation_id}.")
        return _read_json(path)

    def save_manifest(self, evaluation_id: str, manifest: dict[str, Any]) -> None:
        _write_json(self.capture_dir(evaluation_id) / "manifest.json", manifest)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for path in sorted(
            self.captures.glob("*/manifest.json"),
            key=lambda value: value.stat().st_mtime,
            reverse=True,
        )[:limit]:
            try:
                values.append(_read_json(path))
            except Exception:
                continue
        return values

    def detail(self, evaluation_id: str) -> dict[str, Any]:
        root = self.capture_dir(evaluation_id)
        manifest = self.load_manifest(evaluation_id)
        payload: dict[str, Any] = {"manifest": manifest, "files": {}}
        for name in (
            "raw_request.json",
            "internal_request.json",
            "normalized_request.json",
            "context_snapshot.json",
            "primary_result.json",
            "primary_plan.json",
        ):
            path = root / name
            if path.exists():
                payload["files"][name] = _read_json(path)
        report = self.comparison_path(evaluation_id)
        payload["comparison"] = _read_json(report) if report.exists() else None
        return payload


class PlanningEvaluationCaptureService:
    """Persist a frozen replay bundle without delaying or changing the primary plan."""

    def __init__(self, store: PlanningEvaluationStore | None = None) -> None:
        self.settings = get_settings()
        self.store = store or PlanningEvaluationStore()

    @property
    def enabled(self) -> bool:
        return (
            self.settings.planning_evaluation_mode == "capture_only"
            and self.settings.planning_evaluation_persist
        )

    def capture(
        self,
        *,
        raw_request: object,
        internal_request: AutoMissionRequest,
        result: OrchestrationResult,
        request_kind: str,
        plan: SimulationPlan | None = None,
        source_plan_id: str | None = None,
    ) -> PlanningEvaluationReference | None:
        if not self.enabled:
            return None
        evaluation_id = (
            f"EVAL-{internal_request.warehouse_id}-{internal_request.simulation_id}-"
            f"{uuid4().hex[:12].upper()}"
        )
        root = self.store.capture_dir(evaluation_id)
        frozen = root / "frozen_repository"
        frozen.mkdir(parents=True, exist_ok=True)

        with repository_scope(
            internal_request.warehouse_id, internal_request.simulation_id
        ):
            repository = get_repository(
                internal_request.warehouse_id, internal_request.simulation_id
            )
            documents = {
                "warehouse_graph.json": dict(repository.graph),
                "rack_inventory.json": dict(repository.inventory),
                "scenario_state.json": dict(repository.scenario),
                "facility_resources.json": dict(repository.facility),
            }
            for name, document in documents.items():
                document.setdefault("warehouse_id", internal_request.warehouse_id)
                _write_json(frozen / name, document)
            versions = dict(repository.versions)

        raw_payload = (
            raw_request.model_dump(mode="json")
            if hasattr(raw_request, "model_dump")
            else raw_request
        )
        _write_json(root / "raw_request.json", raw_payload)
        _write_json(
            root / "internal_request.json",
            internal_request.model_dump(mode="json"),
        )
        _write_json(
            root / "normalized_request.json",
            result.normalized_request.model_dump(mode="json")
            if result.normalized_request
            else {},
        )
        _write_json(
            root / "context_snapshot.json",
            {
                "context_snapshot": (
                    result.context_snapshot.model_dump(mode="json")
                    if result.context_snapshot
                    else None
                ),
                "inventory_context": (
                    result.inventory_context.model_dump(mode="json")
                    if result.inventory_context
                    else None
                ),
                "robot_context": (
                    result.robot_context.model_dump(mode="json")
                    if result.robot_context
                    else None
                ),
                "map_context": (
                    result.map_context.model_dump(mode="json")
                    if result.map_context
                    else None
                ),
                "repository_versions": versions,
            },
        )
        _write_json(root / "primary_result.json", result.model_dump(mode="json"))
        if plan is not None:
            _write_json(root / "primary_plan.json", plan.model_dump(mode="json"))

        primary_route = (
            result.orchestration_plan.formulation_route
            if result.orchestration_plan
            else None
        )
        manifest = {
            "evaluation_id": evaluation_id,
            "status": "CAPTURED",
            "created_at": _utc_now(),
            "request_kind": request_kind,
            "warehouse_id": internal_request.warehouse_id,
            "simulation_id": internal_request.simulation_id,
            "source_plan_id": source_plan_id,
            "primary_route": primary_route,
            "primary_status": result.status,
            "primary_plan_id": plan.plan_id if plan else None,
            "context_snapshot_id": (
                result.context_snapshot.snapshot_id if result.context_snapshot else None
            ),
            "repository_versions": versions,
            "comparison_status": "NOT_STARTED",
            "comparison_backend": None,
            "comparison_depth": None,
        }
        self.store.save_manifest(evaluation_id, manifest)
        return PlanningEvaluationReference(
            evaluation_id=evaluation_id,
            status="CAPTURED",
            detail_url=f"/api/v1/debug/evaluations/{evaluation_id}",
            compare_url=f"/api/v1/debug/evaluations/{evaluation_id}/compare",
        )


class PlanningComparisonService:
    """Replay Rule and Agent against one frozen capture and compute common metrics."""

    def __init__(self, store: PlanningEvaluationStore | None = None) -> None:
        self.store = store or PlanningEvaluationStore()

    @staticmethod
    def _expected_operation_ids(normalized: dict[str, Any]) -> list[str]:
        return sorted(
            {
                str(value.get("operation_id"))
                for value in normalized.get("operations", [])
                if isinstance(value, dict) and value.get("operation_id")
            }
        )

    @staticmethod
    def _rule_applicable(normalized: dict[str, Any]) -> tuple[bool, list[str]]:
        """Return whether the deterministic branch has a closed typed input.

        Natural language itself does not make Rule inapplicable.  The shared
        normalizer may produce canonical IDs and typed policies.  Rule becomes
        inapplicable only when unresolved semantic references, clarification,
        incident-only operations, or unsupported operation types remain.
        """

        operations = normalized.get("operations", [])
        reasons: list[str] = []
        if not operations:
            reasons.append("NO_NORMALIZED_OPERATIONS")
        for value in operations:
            if not isinstance(value, dict):
                reasons.append("INVALID_OPERATION_RECORD")
                continue
            if value.get("operation_type") in {"UNKNOWN", "QUERY", "INCIDENT"}:
                reasons.append("UNSUPPORTED_OPERATION_TYPE")
            if not value.get("operation_id"):
                reasons.append("MISSING_CANONICAL_OPERATION_ID")

        constraints = normalized.get("constraints") or {}
        if isinstance(constraints, dict):
            unresolved_keys = (
                "excluded_robot_references",
                "excluded_robot_status_references",
                "soft_avoid_edge_references",
                "hard_block_edge_references",
            )
            if any(constraints.get(key) for key in unresolved_keys):
                reasons.append("UNRESOLVED_SEMANTIC_REFERENCE")
        if normalized.get("user_clarification_questions"):
            reasons.append("USER_CLARIFICATION_REQUIRED")
        if normalized.get("incidents"):
            reasons.append("INCIDENT_ROUTE_NOT_RULE_BASELINE")
        return not reasons, sorted(set(reasons))

    @staticmethod
    def _metrics(
        *,
        route: str,
        result: OrchestrationResult,
        expected_ids: list[str],
        repository: object,
        applicability: str = "APPLICABLE",
        extra_errors: list[str] | None = None,
    ) -> BranchQualityMetrics:
        observed_ids = sorted(
            {
                value.operation_id
                for value in (
                    result.normalized_request.operations
                    if result.normalized_request
                    else []
                )
            }
        )
        total_distance = 0.0
        if result.traffic_schedule is not None:
            for timed_route in result.traffic_schedule.routes:
                for step in timed_route.steps:
                    if step.step_type == "MOVE" and step.edge_id:
                        try:
                            total_distance += float(repository.base_edge_metrics(step.edge_id)[0])
                        except Exception:
                            pass
        per_robot_wait: list[int] = []
        if result.traffic_schedule is not None:
            for timed_route in result.traffic_schedule.routes:
                per_robot_wait.append(
                    sum(
                        int(step.end_at_ms - step.start_at_ms)
                        for step in timed_route.steps
                        if step.step_type == "WAIT"
                    )
                )
        relocation_records = (
            list(result.terminal_relocation.relocations)
            if result.terminal_relocation is not None
            else []
        )
        node_latency = sum(value.duration_ms for value in result.node_execution_log)
        llm_nodes = [value for value in result.node_execution_log if value.llm_used]
        policy_violations = 0
        if result.payload_validation is not None and not result.payload_validation.valid:
            policy_violations += len(result.payload_validation.errors)
        if (
            result.optimizer_assignment_validation is not None
            and not result.optimizer_assignment_validation.valid
        ):
            policy_violations += len(result.optimizer_assignment_validation.errors)
        if result.mapf_validation is not None and not result.mapf_validation.valid:
            policy_violations += len(result.mapf_validation.errors)
        deferred = (
            list(result.cuopt_dynamic_input_draft.deferred_order_ids)
            if result.cuopt_dynamic_input_draft
            else []
        )
        return BranchQualityMetrics(
            route=route,  # type: ignore[arg-type]
            applicability=applicability,  # type: ignore[arg-type]
            workflow_status=result.status,
            operation_ids=observed_ids,
            missing_operation_ids=sorted(set(expected_ids) - set(observed_ids)),
            hallucinated_operation_ids=sorted(set(observed_ids) - set(expected_ids)),
            optimization_task_count=(
                len(result.optimization_request.tasks)
                if result.optimization_request
                else 0
            ),
            deferred_task_ids=deferred,
            candidate_robot_count=(
                len(result.optimization_request.vehicles)
                if result.optimization_request
                else 0
            ),
            payload_valid=(
                result.payload_validation.valid
                if result.payload_validation is not None
                else None
            ),
            solver_status=(
                result.optimizer_result.status if result.optimizer_result else None
            ),
            unassigned_task_count=(
                len(result.optimizer_result.unassigned_task_ids)
                if result.optimizer_result
                else 0
            ),
            mapf_valid=(
                result.mapf_validation.valid
                if result.mapf_validation is not None
                else None
            ),
            makespan_ms=(
                result.traffic_schedule.makespan_ms
                if result.traffic_schedule is not None
                else None
            ),
            total_distance_m=round(total_distance, 6) if total_distance else None,
            total_wait_ms=(
                result.traffic_schedule.total_wait_ms
                if result.traffic_schedule is not None
                else None
            ),
            total_service_ms=(
                result.traffic_schedule.total_service_ms
                if result.traffic_schedule is not None
                else None
            ),
            used_robot_count=(
                len(result.optimizer_result.routes) if result.optimizer_result else 0
            ),
            operation_preservation_ratio=(
                len(set(expected_ids) & set(observed_ids)) / len(set(expected_ids))
                if expected_ids
                else 1.0
            ),
            completed_task_ratio=(
                (
                    len(result.optimization_request.tasks)
                    - len(result.optimizer_result.unassigned_task_ids)
                )
                / len(result.optimization_request.tasks)
                if result.optimization_request
                and result.optimization_request.tasks
                and result.optimizer_result is not None
                else None
            ),
            max_robot_wait_ms=max(per_robot_wait, default=0)
            if result.traffic_schedule is not None
            else None,
            station_reservation_count=(
                len(result.traffic_schedule.station_reservations)
                if result.traffic_schedule is not None
                else 0
            ),
            g2p_batch_count=(
                len(result.goods_to_person_compilation.batches)
                if result.goods_to_person_compilation is not None
                else 0
            ),
            terminal_relocation_count=len(relocation_records),
            charge_relocation_count=sum(
                value.policy == "CHARGE" for value in relocation_records
            ),
            park_relocation_count=sum(
                value.policy == "PARK" for value in relocation_records
            ),
            route_locked=(
                result.orchestration_plan.route_locked
                if result.orchestration_plan is not None
                else None
            ),
            hitl_required=(
                result.pending_human_interaction is not None
                or result.status
                in {"human_review", "awaiting_human_approval", "awaiting_clarification"}
            ),
            input_rejected=result.input_rejection is not None,
            validation_issue_count=len(result.validation_issues),
            policy_violation_count=policy_violations,
            total_latency_ms=round(node_latency, 3),
            llm_latency_ms=round(sum(value.duration_ms for value in llm_nodes), 3),
            llm_call_count=len(llm_nodes),
            errors=[
                *[f"{value.stage}:{value.code}:{value.message}" for value in result.errors],
                *(extra_errors or []),
            ],
        )

    def compare(
        self,
        evaluation_id: str,
        request: PlanningComparisonRequest,
    ) -> PlanningComparisonReport:
        capture_root = self.store.capture_dir(evaluation_id)
        manifest = self.store.load_manifest(evaluation_id)
        internal = AutoMissionRequest.model_validate(
            _read_json(capture_root / "internal_request.json")
        )
        normalized = _read_json(capture_root / "normalized_request.json")
        shared_normalized = NormalizedWarehouseRequest.model_validate(normalized)
        internal = internal.model_copy(
            update={
                "normalized_request_override": shared_normalized,
                "evaluation_shadow_mode": True,
            }
        )
        expected_ids = self._expected_operation_ids(normalized)
        frozen = capture_root / "frozen_repository"
        backend = (
            "cuopt_payload_only"
            if request.depth in {"formulation", "payload"}
            else request.backend
        )

        manifest.update(
            {
                "status": "COMPARING",
                "comparison_status": "RUNNING",
                "comparison_backend": backend,
                "comparison_depth": request.depth,
            }
        )
        self.store.save_manifest(evaluation_id, manifest)

        from app.services.orchestration_service import OrchestrationService

        with _COMPARE_LOCK:
            set_data_dir(frozen)
            try:
                with repository_scope(internal.warehouse_id, internal.simulation_id):
                    repository = get_repository(
                        internal.warehouse_id, internal.simulation_id
                    )
                    applicable, reasons = self._rule_applicable(normalized)
                    if applicable:
                        rule_result = OrchestrationService().run(
                            internal.model_copy(update={"optimization_backend": backend}),
                            trusted_planning_mode="force_rule",
                            persist_simulation_plan=False,
                        )
                        rule_metrics = self._metrics(
                            route="RULE_FORMULATION",
                            result=rule_result,
                            expected_ids=expected_ids,
                            repository=repository,
                        )
                        _write_json(
                            self.store.comparisons
                            / evaluation_id
                            / "rule_result.json",
                            rule_result.model_dump(mode="json"),
                        )
                    else:
                        rule_metrics = BranchQualityMetrics(
                            route="RULE_FORMULATION",
                            applicability="NOT_APPLICABLE",
                            operation_ids=[],
                            missing_operation_ids=expected_ids,
                            errors=reasons,
                        )

                    agent_metrics: list[BranchQualityMetrics] = []
                    for index in range(request.agent_repeats):
                        agent_result = OrchestrationService().run(
                            internal.model_copy(update={"optimization_backend": backend}),
                            trusted_planning_mode="force_agent",
                            persist_simulation_plan=False,
                        )
                        metrics = self._metrics(
                            route="AGENT_FORMULATION",
                            result=agent_result,
                            expected_ids=expected_ids,
                            repository=repository,
                        )
                        agent_metrics.append(metrics)
                        _write_json(
                            self.store.comparisons
                            / evaluation_id
                            / f"agent_result_{index + 1}.json",
                            agent_result.model_dump(mode="json"),
                        )
            finally:
                set_data_dir(None)

        status_counts = Counter(value.workflow_status for value in agent_metrics)
        operation_signatures = Counter(
            tuple(value.operation_ids) for value in agent_metrics
        )
        robot_counts = [value.used_robot_count for value in agent_metrics]
        stability = {
            "runs": len(agent_metrics),
            "workflow_status_counts": dict(status_counts),
            "operation_signature_counts": {
                "|".join(key): value for key, value in operation_signatures.items()
            },
            "payload_valid_rate": (
                sum(value.payload_valid is True for value in agent_metrics)
                / len(agent_metrics)
                if agent_metrics
                else 0.0
            ),
            "mapf_valid_rate": (
                sum(value.mapf_valid is True for value in agent_metrics)
                / len(agent_metrics)
                if agent_metrics
                else 0.0
            ),
            "used_robot_counts": robot_counts,
        }
        agent_first = agent_metrics[0] if agent_metrics else None
        comparison = {
            "both_applicable": (
                rule_metrics.applicability == "APPLICABLE"
                and agent_first is not None
                and agent_first.applicability == "APPLICABLE"
            ),
            "operation_sets_equal": (
                agent_first is not None
                and set(rule_metrics.operation_ids) == set(agent_first.operation_ids)
            ),
            "task_count_delta_agent_minus_rule": (
                agent_first.optimization_task_count
                - rule_metrics.optimization_task_count
                if agent_first is not None
                else None
            ),
            "makespan_delta_agent_minus_rule_ms": (
                agent_first.makespan_ms - rule_metrics.makespan_ms
                if agent_first is not None
                and agent_first.makespan_ms is not None
                and rule_metrics.makespan_ms is not None
                else None
            ),
            "distance_delta_agent_minus_rule_m": (
                round(agent_first.total_distance_m - rule_metrics.total_distance_m, 6)
                if agent_first is not None
                and agent_first.total_distance_m is not None
                and rule_metrics.total_distance_m is not None
                else None
            ),
            "wait_delta_agent_minus_rule_ms": (
                agent_first.total_wait_ms - rule_metrics.total_wait_ms
                if agent_first is not None
                and agent_first.total_wait_ms is not None
                and rule_metrics.total_wait_ms is not None
                else None
            ),
            "quality_note": (
                "Latency is recorded as operating cost only; feasibility, task preservation, "
                "policy compliance, distance, wait, and MAPF validity are the primary metrics."
            ),
        }
        report = PlanningComparisonReport(
            evaluation_id=evaluation_id,
            backend=backend,
            depth=request.depth,
            expected_operation_ids=expected_ids,
            rule=rule_metrics,
            agent_runs=agent_metrics,
            agent_stability=stability,
            comparison=comparison,
            created_at=_utc_now(),
        )
        _write_json(
            self.store.comparison_path(evaluation_id),
            report.model_dump(mode="json"),
        )
        manifest.update(
            {
                "status": "COMPARISON_READY",
                "comparison_status": "COMPLETED",
                "comparison_completed_at": report.created_at,
            }
        )
        self.store.save_manifest(evaluation_id, manifest)
        return report

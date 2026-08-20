"""Capture live planning evidence and compare Rule/Agent later through debug APIs."""
from __future__ import annotations

import json
import math
import statistics
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.core.config import get_settings
from app.domain.planning_evaluation import (
    AgentCostStatistics,
    BranchQualityMetrics,
    EvaluationGateFailure,
    PlanningComparisonReport,
    PlanningComparisonRequest,
    PlanningCostComparison,
    PlanningEvaluationReference,
    PlanningOperationalComparison,
)
from app.domain.schemas import (
    AutoMissionRequest,
    CuOptPayload,
    NormalizedWarehouseRequest,
    OptimizerResult,
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


def _comparison_task_identity(task: Any) -> str:
    """Return a branch-independent identity for one solver task.

    Rule assigns sequential ``TASK-###`` IDs to direct inbound/recovery work,
    while Agent preserves the canonical operation ID.  Those labels describe
    the same required work and must not make an otherwise like-for-like run
    incomparable.  G2P tasks deliberately retain their physical handling-unit
    IDs because changing the compiled BOX cycles is a real plan difference.
    """

    operation_type = str(getattr(task, "operation_type", "") or "")
    order_id = getattr(task, "order_id", None)
    if operation_type in {"INBOUND_ITEM", "RECOVERY"} and order_id:
        return f"{operation_type}:{order_id}"
    return str(getattr(task, "task_id"))


def _distribution_summary(values: list[float]) -> dict[str, float | None]:
    """Return deterministic population-spread metrics for non-negative work."""

    if not values:
        return {
            "minimum": None,
            "maximum": None,
            "range": None,
            "standard_deviation": None,
            "coefficient_of_variation": None,
            "gini_coefficient": None,
        }
    normalized = [max(0.0, float(value)) for value in values]
    minimum = min(normalized)
    maximum = max(normalized)
    mean = statistics.fmean(normalized)
    standard_deviation = statistics.pstdev(normalized)
    coefficient_of_variation = standard_deviation / mean if mean > 0 else None
    total = sum(normalized)
    gini = (
        sum(abs(left - right) for left in normalized for right in normalized)
        / (2.0 * len(normalized) * total)
        if total > 0
        else None
    )
    return {
        "minimum": minimum,
        "maximum": maximum,
        "range": maximum - minimum,
        "standard_deviation": round(standard_deviation, 6),
        "coefficient_of_variation": (
            round(coefficient_of_variation, 6)
            if coefficient_of_variation is not None
            else None
        ),
        "gini_coefficient": round(gini, 6) if gini is not None else None,
    }


def _physical_cycle_counts_by_robot(
    *,
    optimizer: OptimizerResult | None,
    payload: CuOptPayload | None,
) -> dict[str, int]:
    """Count one physical BOX cycle per pickup-delivery pair for every candidate."""

    if optimizer is None:
        return {}
    candidate_ids = list(payload.fleet_data.vehicle_ids) if payload is not None else []
    counts = {str(robot_id): 0 for robot_id in candidate_ids}
    pair_by_task_id: dict[str, int] = {}
    if payload is not None:
        task_ids = list(payload.task_data.task_ids)
        for pair_number, pair in enumerate(payload.task_data.pickup_and_delivery_pairs):
            for task_index in pair:
                if 0 <= int(task_index) < len(task_ids):
                    pair_by_task_id[task_ids[int(task_index)]] = pair_number

    for route in optimizer.routes:
        robot_id = str(route.vehicle_id)
        counts.setdefault(robot_id, 0)
        if pair_by_task_id:
            counts[robot_id] = len(
                {
                    pair_by_task_id[task_id]
                    for task_id in route.task_sequence
                    if task_id in pair_by_task_id
                }
            )
            continue
        # Compatibility fallback for older persisted payloads without pair rows.
        counts[robot_id] = len(
            {
                task_id.removesuffix("_PICK").removesuffix("_DROP")
                for task_id in route.task_sequence
                if task_id.endswith(("_PICK", "_DROP"))
            }
        )
    return dict(sorted(counts.items()))


def _scheduled_robot_times(
    *,
    result: OrchestrationResult,
    optimizer: OptimizerResult | None,
    payload: CuOptPayload | None,
) -> tuple[dict[str, int], dict[str, float]]:
    """Return scheduled work duration and absolute route finish time per robot."""

    candidate_ids = list(payload.fleet_data.vehicle_ids) if payload is not None else []
    work_ms = {str(robot_id): 0 for robot_id in candidate_ids}
    finish_at_ms: dict[str, float] = {}
    if result.traffic_schedule is not None:
        for route in result.traffic_schedule.routes:
            robot_id = str(route.robot_id)
            work_ms[robot_id] = sum(
                max(0, int(step.end_at_ms - step.start_at_ms))
                for step in route.steps
            )
            finish_at_ms[robot_id] = float(route.finish_at_ms)
        return dict(sorted(work_ms.items())), dict(sorted(finish_at_ms.items()))

    if optimizer is None:
        return dict(sorted(work_ms.items())), finish_at_ms
    available_at: dict[str, int] = {}
    if payload is not None:
        available_values = list(payload.fleet_data.vehicle_available_at_ms)
        available_at = {
            str(robot_id): int(
                available_values[index] if index < len(available_values) else 0
            )
            for index, robot_id in enumerate(payload.fleet_data.vehicle_ids)
        }
    for route in optimizer.routes:
        if route.completion_ms is None:
            continue
        robot_id = str(route.vehicle_id)
        completion = float(route.completion_ms)
        finish_at_ms[robot_id] = completion
        work_ms[robot_id] = max(
            0,
            int(round(completion - float(available_at.get(robot_id, 0)))),
        )
    return dict(sorted(work_ms.items())), dict(sorted(finish_at_ms.items()))


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
            "materialization_report.json",
            "post_materialization_report.json",
            "dynamic_contract_report.json",
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
            artifact_path=str(root),
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
        repeat_index: int,
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
        if result.route_validation is not None and not result.route_validation.valid:
            policy_violations += len(result.route_validation.errors)
        if (
            result.logical_operation_coverage_validation is not None
            and not result.logical_operation_coverage_validation.valid
        ):
            policy_violations += len(result.logical_operation_coverage_validation.errors)
        deferred = (
            list(result.cuopt_dynamic_input_draft.deferred_order_ids)
            if result.cuopt_dynamic_input_draft
            else []
        )
        optimization_tasks = (
            list(result.optimization_request.tasks)
            if result.optimization_request
            else []
        )
        optimization_task_ids = sorted(value.task_id for value in optimization_tasks)
        comparison_identity_by_task_id = {
            value.task_id: _comparison_task_identity(value)
            for value in optimization_tasks
        }
        payload = result.execution_payload or result.cuopt_payload
        raw_optional_task_ids = list(payload.task_data.optional_task_ids) if payload else []
        optional_task_ids = sorted(
            comparison_identity_by_task_id.get(value, value)
            for value in raw_optional_task_ids
        )
        mandatory_task_ids = sorted(
            {
                _comparison_task_identity(value)
                for value in optimization_tasks
                if value.task_id not in set(raw_optional_task_ids)
            }
        )
        # Keep the optimizer result paired with the payload that is actually
        # executed. Terminal relocation may replace both objects, so mixing the
        # enriched payload with the pre-enrichment result skews route workload.
        optimizer = result.execution_optimizer_result or result.optimizer_result
        physical_cycle_counts = _physical_cycle_counts_by_robot(
            optimizer=optimizer,
            payload=payload,
        )
        physical_cycle_distribution = _distribution_summary(
            [float(value) for value in physical_cycle_counts.values()]
        )
        scheduled_work_ms, route_finish_at_ms = _scheduled_robot_times(
            result=result,
            optimizer=optimizer,
            payload=payload,
        )
        scheduled_work_distribution = _distribution_summary(
            [float(value) for value in scheduled_work_ms.values()]
        )
        makespan_ms = (
            result.traffic_schedule.makespan_ms
            if result.traffic_schedule is not None
            else None
        )
        total_wait_ms = (
            result.traffic_schedule.total_wait_ms
            if result.traffic_schedule is not None
            else None
        )
        used_robot_count = len(optimizer.routes) if optimizer else 0
        operation_count = len(set(expected_ids))
        fleet_effort_robot_ms = (
            int(makespan_ms * used_robot_count)
            if makespan_ms is not None and used_robot_count > 0
            else None
        )
        return BranchQualityMetrics(
            route=route,  # type: ignore[arg-type]
            repeat_index=repeat_index,
            applicability=applicability,  # type: ignore[arg-type]
            workflow_status=result.status,
            snapshot_id=(
                result.optimization_request.snapshot_id
                if result.optimization_request is not None
                else (
                    result.context_snapshot.snapshot_id
                    if result.context_snapshot
                    else None
                )
            ),
            objective_profile=(
                result.optimization_request.objective_profile
                if result.optimization_request is not None
                else None
            ),
            operation_ids=observed_ids,
            missing_operation_ids=sorted(set(expected_ids) - set(observed_ids)),
            hallucinated_operation_ids=sorted(set(observed_ids) - set(expected_ids)),
            optimization_task_count=(
                len(result.optimization_request.tasks)
                if result.optimization_request
                else 0
            ),
            optimization_task_ids=optimization_task_ids,
            mandatory_task_ids=mandatory_task_ids,
            optional_task_ids=optional_task_ids,
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
            solver_backend=(optimizer.backend if optimizer else None),
            solver_status=(optimizer.status if optimizer else None),
            unassigned_task_count=(
                len(optimizer.unassigned_task_ids)
                if optimizer
                else 0
            ),
            unassigned_task_ids=(
                sorted(
                    comparison_identity_by_task_id.get(value, value)
                    for value in optimizer.unassigned_task_ids
                )
                if optimizer
                else []
            ),
            mapf_valid=(
                result.mapf_validation.valid
                if result.mapf_validation is not None
                else None
            ),
            global_objective_cost=(
                optimizer.global_objective_cost if optimizer else None
            ),
            objective_values=(
                {value.name: value.value for value in optimizer.objective_values}
                if optimizer
                else {}
            ),
            optimizer_estimated_makespan_ms=(
                optimizer.estimated_makespan_ms if optimizer else None
            ),
            makespan_ms=makespan_ms,
            total_distance_m=(
                round(total_distance, 6)
                if result.traffic_schedule is not None
                else None
            ),
            total_wait_ms=total_wait_ms,
            total_service_ms=(
                result.traffic_schedule.total_service_ms
                if result.traffic_schedule is not None
                else None
            ),
            used_robot_count=used_robot_count,
            fleet_effort_robot_ms=fleet_effort_robot_ms,
            throughput_operations_per_hour=(
                round(operation_count * 3_600_000.0 / makespan_ms, 6)
                if operation_count and makespan_ms not in (None, 0)
                else None
            ),
            distance_per_operation_m=(
                round(total_distance / operation_count, 6)
                if operation_count and result.traffic_schedule is not None
                else None
            ),
            wait_per_operation_ms=(
                round(total_wait_ms / operation_count, 6)
                if operation_count and total_wait_ms is not None
                else None
            ),
            operations_per_used_robot=(
                round(operation_count / used_robot_count, 6)
                if operation_count and used_robot_count
                else None
            ),
            physical_cycle_count_by_robot=physical_cycle_counts,
            min_physical_cycles_per_robot=(
                int(physical_cycle_distribution["minimum"])
                if physical_cycle_distribution["minimum"] is not None
                else None
            ),
            max_physical_cycles_per_robot=(
                int(physical_cycle_distribution["maximum"])
                if physical_cycle_distribution["maximum"] is not None
                else None
            ),
            physical_cycle_count_range=(
                int(physical_cycle_distribution["range"])
                if physical_cycle_distribution["range"] is not None
                else None
            ),
            physical_cycle_count_standard_deviation=(
                physical_cycle_distribution["standard_deviation"]
            ),
            physical_cycle_count_coefficient_of_variation=(
                physical_cycle_distribution["coefficient_of_variation"]
            ),
            physical_cycle_count_gini_coefficient=(
                physical_cycle_distribution["gini_coefficient"]
            ),
            scheduled_work_ms_by_robot=scheduled_work_ms,
            scheduled_work_time_range_ms=(
                int(scheduled_work_distribution["range"])
                if scheduled_work_distribution["range"] is not None
                else None
            ),
            scheduled_work_time_standard_deviation_ms=(
                scheduled_work_distribution["standard_deviation"]
            ),
            scheduled_work_time_coefficient_of_variation=(
                scheduled_work_distribution["coefficient_of_variation"]
            ),
            route_finish_at_ms_by_robot=route_finish_at_ms,
            max_robot_finish_at_ms=(
                max(route_finish_at_ms.values()) if route_finish_at_ms else None
            ),
            operation_preservation_ratio=(
                len(set(expected_ids) & set(observed_ids)) / len(set(expected_ids))
                if expected_ids
                else 1.0
            ),
            completed_task_ratio=(
                (
                    len(result.optimization_request.tasks)
                    - len(optimizer.unassigned_task_ids)
                )
                / len(result.optimization_request.tasks)
                if result.optimization_request
                and result.optimization_request.tasks
                and optimizer is not None
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

    @staticmethod
    def _apply_hard_gate(
        metrics: BranchQualityMetrics,
        *,
        required_objective_profile: str,
        require_mapf: bool,
        require_cost: bool,
    ) -> BranchQualityMetrics:
        """Mark whether a branch is eligible for an objective-cost comparison."""

        failures: list[EvaluationGateFailure] = []

        def fail(code: str, message: str) -> None:
            failures.append(EvaluationGateFailure(code=code, message=message))

        if metrics.applicability != "APPLICABLE":
            fail("H00_BRANCH_NOT_APPLICABLE", "The formulation branch is not applicable.")
        if metrics.missing_operation_ids:
            fail(
                "H01_MANDATORY_OPERATION_MISSING",
                f"Missing operations: {metrics.missing_operation_ids}",
            )
        if metrics.hallucinated_operation_ids:
            fail(
                "H02_HALLUCINATED_OPERATION",
                f"Unexpected operations: {metrics.hallucinated_operation_ids}",
            )
        if metrics.payload_valid is not True:
            fail("H03_INVALID_SOLVER_PAYLOAD", "The solver payload did not pass validation.")
        if metrics.solver_backend != "cuopt":
            fail(
                "H03A_SOLVER_BACKEND_NOT_CUOPT",
                f"Solver backend is {metrics.solver_backend!r}, not 'cuopt'.",
            )
        if metrics.solver_status != "success":
            fail(
                "H04_SOLVER_NOT_SUCCESSFUL",
                f"Solver status is {metrics.solver_status!r}, not 'success'.",
            )
        mandatory_unassigned = sorted(
            set(metrics.unassigned_task_ids) & set(metrics.mandatory_task_ids)
        )
        if mandatory_unassigned:
            fail(
                "H05_MANDATORY_TASK_UNASSIGNED",
                f"Unassigned mandatory tasks: {mandatory_unassigned}",
            )
        elif metrics.unassigned_task_count and not metrics.unassigned_task_ids:
            fail(
                "H05_UNASSIGNED_TASK_DETAILS_MISSING",
                "The solver reported unassigned tasks without their identifiers.",
            )
        if require_mapf and metrics.mapf_valid is not True:
            fail("H06_MAPF_INVALID", "The MAPF/traffic schedule did not pass validation.")
        if metrics.policy_violation_count:
            fail(
                "H07_POLICY_OR_ROUTE_VIOLATION",
                f"Detected {metrics.policy_violation_count} policy/route validation errors.",
            )
        if metrics.input_rejected or metrics.hitl_required:
            fail(
                "H08_NON_AUTOMATED_TERMINAL_STATE",
                "The branch was rejected or requires human interaction.",
            )
        if metrics.objective_profile != required_objective_profile:
            fail(
                "H09_OBJECTIVE_PROFILE_MISMATCH",
                f"Objective profile is {metrics.objective_profile!r}; expected "
                f"{required_objective_profile!r}.",
            )
        if not metrics.snapshot_id:
            fail("H10_SNAPSHOT_ID_MISSING", "No frozen snapshot identifier was preserved.")
        if require_cost and (
            metrics.global_objective_cost is None
            or not math.isfinite(metrics.global_objective_cost)
            or metrics.global_objective_cost < 0
        ):
            fail(
                "H11_OBJECTIVE_COST_MISSING",
                "The optimizer did not expose a finite non-negative global objective cost.",
            )
        if set(metrics.mandatory_task_ids) & set(metrics.optional_task_ids):
            fail(
                "H12_TASK_CLASSIFICATION_OVERLAP",
                "A task cannot be both mandatory and optional.",
            )
        return metrics.model_copy(
            update={
                "hard_gate_passed": not failures,
                "hard_gate_failures": failures,
            }
        )

    @staticmethod
    def _cost_comparison(
        rule: BranchQualityMetrics,
        agent_runs: list[BranchQualityMetrics],
        request: PlanningComparisonRequest,
    ) -> PlanningCostComparison:
        """Compare cuOpt global costs only for gate-passing, like-for-like runs."""

        if not rule.hard_gate_passed:
            return PlanningCostComparison(
                verdict="BASELINE_INVALID",
                reasons=[value.code for value in rule.hard_gate_failures],
                rule_cost=rule.global_objective_cost,
                min_valid_agent_runs=request.min_valid_agent_runs,
                tie_tolerance_pct=request.cost_tie_tolerance_pct,
                max_regression_pct=request.max_agent_cost_regression_pct,
                agent_statistics=AgentCostStatistics(
                    requested_runs=len(agent_runs), invalid_runs=len(agent_runs)
                ),
            )

        compatible: list[BranchQualityMetrics] = []
        incompatibility_reasons: list[str] = []
        for value in agent_runs:
            prefix = f"AGENT_RUN_{value.repeat_index}"
            if not value.hard_gate_passed:
                incompatibility_reasons.extend(
                    f"{prefix}:{failure.code}" for failure in value.hard_gate_failures
                )
                continue
            if value.snapshot_id != rule.snapshot_id:
                incompatibility_reasons.append(f"{prefix}:SNAPSHOT_MISMATCH")
                continue
            if value.objective_profile != rule.objective_profile:
                incompatibility_reasons.append(f"{prefix}:OBJECTIVE_PROFILE_MISMATCH")
                continue
            if set(value.operation_ids) != set(rule.operation_ids):
                incompatibility_reasons.append(f"{prefix}:OPERATION_SET_MISMATCH")
                continue
            if set(value.mandatory_task_ids) != set(rule.mandatory_task_ids):
                incompatibility_reasons.append(f"{prefix}:MANDATORY_TASK_SET_MISMATCH")
                continue
            compatible.append(value)

        costs = [
            float(value.global_objective_cost)
            for value in compatible
            if value.global_objective_cost is not None
        ]
        rule_cost = (
            float(rule.global_objective_cost)
            if rule.global_objective_cost is not None
            else None
        )
        tolerance = request.cost_tie_tolerance_pct
        wins = ties = losses = 0
        if rule_cost is not None:
            for cost in costs:
                pct = (
                    (rule_cost - cost) / rule_cost * 100.0
                    if rule_cost
                    else (0.0 if cost == 0 else None)
                )
                if pct is None:
                    losses += 1
                    continue
                if abs(pct) <= tolerance:
                    ties += 1
                elif pct > 0:
                    wins += 1
                else:
                    losses += 1
        mean_cost = statistics.fmean(costs) if costs else None
        median_cost = statistics.median(costs) if costs else None
        deviation = (
            statistics.pstdev(costs)
            if len(costs) > 1
            else (0.0 if costs else None)
        )
        stats = AgentCostStatistics(
            requested_runs=len(agent_runs),
            valid_runs=len(costs),
            invalid_runs=len(agent_runs) - len(costs),
            valid_run_rate=(len(costs) / len(agent_runs) if agent_runs else 0.0),
            costs=costs,
            minimum_cost=min(costs) if costs else None,
            maximum_cost=max(costs) if costs else None,
            mean_cost=mean_cost,
            median_cost=median_cost,
            standard_deviation=deviation,
            coefficient_of_variation=(
                deviation / mean_cost
                if deviation is not None and mean_cost not in (None, 0.0)
                else None
            ),
            wins=wins,
            ties=ties,
            losses=losses,
            win_rate=(wins / len(costs) if costs else 0.0),
        )
        common = {
            "reasons": sorted(set(incompatibility_reasons)),
            "rule_cost": rule_cost,
            "agent_median_cost": median_cost,
            "min_valid_agent_runs": request.min_valid_agent_runs,
            "tie_tolerance_pct": tolerance,
            "max_regression_pct": request.max_agent_cost_regression_pct,
            "agent_statistics": stats,
        }
        gate_passing_runs = sum(value.hard_gate_passed for value in agent_runs)
        if (
            gate_passing_runs >= request.min_valid_agent_runs
            and len(costs) < request.min_valid_agent_runs
        ):
            return PlanningCostComparison(
                comparable=False,
                verdict="NOT_COMPARABLE",
                **common,
            )
        if len(costs) < request.min_valid_agent_runs:
            return PlanningCostComparison(
                comparable=False,
                verdict="INSUFFICIENT_VALID_AGENT_RUNS",
                **common,
            )
        if rule_cost is None or median_cost is None:
            return PlanningCostComparison(
                comparable=False,
                verdict="NOT_COMPARABLE",
                **common,
            )

        delta = median_cost - rule_cost
        improvement = (
            (rule_cost - median_cost) / rule_cost * 100.0
            if rule_cost
            else (0.0 if median_cost == 0 else None)
        )
        if improvement is None:
            verdict = "RULE_WIN"
        elif abs(improvement) <= tolerance:
            verdict = "TIE"
        elif improvement > 0:
            verdict = "AGENT_WIN"
        else:
            verdict = "RULE_WIN"
        regression_within_limit = (
            improvement >= -request.max_agent_cost_regression_pct
            if improvement is not None
            else False
        )
        return PlanningCostComparison(
            comparable=True,
            verdict=verdict,  # type: ignore[arg-type]
            delta_agent_minus_rule=delta,
            improvement_pct=improvement,
            regression_within_limit=regression_within_limit,
            **common,
        )

    @staticmethod
    def _operational_comparison(
        rule: BranchQualityMetrics,
        agent_runs: list[BranchQualityMetrics],
        request: PlanningComparisonRequest,
    ) -> PlanningOperationalComparison:
        """Judge practical plan quality without rewarding a trivial one-robot plan.

        Makespan is the primary service KPI.  Distance, fleet effort
        (used robots multiplied by makespan), and MAPF waiting are resource
        guardrails.  Raw cuOpt objective cost remains a separate diagnostic.
        """

        common = {
            "requested_agent_runs": len(agent_runs),
            "min_valid_agent_runs": request.min_valid_agent_runs,
            "rule_makespan_ms": rule.makespan_ms,
            "rule_throughput_operations_per_hour": (
                rule.throughput_operations_per_hour
            ),
            "rule_used_robot_count": rule.used_robot_count,
            "rule_fleet_effort_robot_ms": rule.fleet_effort_robot_ms,
            "rule_total_distance_m": rule.total_distance_m,
            "rule_total_wait_ms": rule.total_wait_ms,
            "rule_physical_cycle_count_range": rule.physical_cycle_count_range,
            "rule_physical_cycle_count_standard_deviation": (
                rule.physical_cycle_count_standard_deviation
            ),
            "rule_physical_cycle_count_coefficient_of_variation": (
                rule.physical_cycle_count_coefficient_of_variation
            ),
            "rule_physical_cycle_count_gini_coefficient": (
                rule.physical_cycle_count_gini_coefficient
            ),
            "rule_scheduled_work_time_range_ms": (
                rule.scheduled_work_time_range_ms
            ),
            "rule_scheduled_work_time_standard_deviation_ms": (
                rule.scheduled_work_time_standard_deviation_ms
            ),
            "rule_scheduled_work_time_coefficient_of_variation": (
                rule.scheduled_work_time_coefficient_of_variation
            ),
            "rule_max_robot_finish_at_ms": rule.max_robot_finish_at_ms,
            "operational_tie_tolerance_pct": (
                request.operational_tie_tolerance_pct
            ),
            "min_makespan_improvement_pct": (
                request.min_agent_makespan_improvement_pct
            ),
            "max_distance_regression_pct": (
                request.max_agent_distance_regression_pct
            ),
            "max_fleet_effort_regression_pct": (
                request.max_agent_fleet_effort_regression_pct
            ),
            "max_wait_regression_pct": request.max_agent_wait_regression_pct,
        }
        if not rule.hard_gate_passed:
            return PlanningOperationalComparison(
                verdict="BASELINE_INVALID",
                reasons=[value.code for value in rule.hard_gate_failures],
                **common,
            )

        required_rule_metrics = {
            "MAKESPAN": rule.makespan_ms,
            "FLEET_EFFORT": rule.fleet_effort_robot_ms,
            "DISTANCE": rule.total_distance_m,
            "WAIT": rule.total_wait_ms,
        }
        missing_rule = [
            name for name, value in required_rule_metrics.items() if value is None
        ]
        if missing_rule:
            return PlanningOperationalComparison(
                verdict="NOT_COMPARABLE",
                reasons=[f"RULE_{name}_MISSING" for name in missing_rule],
                **common,
            )

        compatible: list[BranchQualityMetrics] = []
        reasons: list[str] = []
        for value in agent_runs:
            prefix = f"AGENT_RUN_{value.repeat_index}"
            if not value.hard_gate_passed:
                reasons.extend(
                    f"{prefix}:{failure.code}"
                    for failure in value.hard_gate_failures
                )
                continue
            if value.snapshot_id != rule.snapshot_id:
                reasons.append(f"{prefix}:SNAPSHOT_MISMATCH")
                continue
            if value.objective_profile != rule.objective_profile:
                reasons.append(f"{prefix}:OBJECTIVE_PROFILE_MISMATCH")
                continue
            if set(value.operation_ids) != set(rule.operation_ids):
                reasons.append(f"{prefix}:OPERATION_SET_MISMATCH")
                continue
            if set(value.mandatory_task_ids) != set(rule.mandatory_task_ids):
                reasons.append(f"{prefix}:MANDATORY_TASK_SET_MISMATCH")
                continue
            missing_metrics = [
                name
                for name, metric in {
                    "MAKESPAN": value.makespan_ms,
                    "FLEET_EFFORT": value.fleet_effort_robot_ms,
                    "DISTANCE": value.total_distance_m,
                    "WAIT": value.total_wait_ms,
                }.items()
                if metric is None
            ]
            if missing_metrics:
                reasons.extend(f"{prefix}:{name}_MISSING" for name in missing_metrics)
                continue
            compatible.append(value)

        if len(compatible) < request.min_valid_agent_runs:
            return PlanningOperationalComparison(
                verdict="INSUFFICIENT_VALID_AGENT_RUNS",
                reasons=sorted(set(reasons)),
                valid_agent_runs=len(compatible),
                **common,
            )

        def median(attribute: str) -> float:
            return float(
                statistics.median(
                    float(getattr(value, attribute)) for value in compatible
                )
            )

        def optional_median(attribute: str) -> float | None:
            values = [
                float(metric)
                for value in compatible
                if (metric := getattr(value, attribute)) is not None
            ]
            return float(statistics.median(values)) if values else None

        agent_makespan = median("makespan_ms")
        agent_throughput = optional_median("throughput_operations_per_hour")
        agent_robots = median("used_robot_count")
        agent_fleet_effort = median("fleet_effort_robot_ms")
        agent_distance = median("total_distance_m")
        agent_wait = median("total_wait_ms")
        agent_cycle_range = optional_median("physical_cycle_count_range")
        agent_cycle_standard_deviation = optional_median(
            "physical_cycle_count_standard_deviation"
        )
        agent_cycle_cv = optional_median(
            "physical_cycle_count_coefficient_of_variation"
        )
        agent_cycle_gini = optional_median("physical_cycle_count_gini_coefficient")
        agent_work_time_range = optional_median("scheduled_work_time_range_ms")
        agent_work_time_standard_deviation = optional_median(
            "scheduled_work_time_standard_deviation_ms"
        )
        agent_work_time_cv = optional_median(
            "scheduled_work_time_coefficient_of_variation"
        )
        agent_max_finish = optional_median("max_robot_finish_at_ms")

        def lower_is_better(rule_value: float, agent_value: float) -> float | None:
            if rule_value == 0:
                return 0.0 if agent_value == 0 else None
            return (rule_value - agent_value) / rule_value * 100.0

        def higher_is_better(rule_value: float | None, agent_value: float) -> float | None:
            if rule_value is None:
                return None
            if rule_value == 0:
                return 0.0 if agent_value == 0 else None
            return (agent_value - rule_value) / rule_value * 100.0

        makespan_improvement = lower_is_better(
            float(rule.makespan_ms), agent_makespan
        )
        fleet_effort_improvement = lower_is_better(
            float(rule.fleet_effort_robot_ms), agent_fleet_effort
        )
        distance_improvement = lower_is_better(
            float(rule.total_distance_m), agent_distance
        )
        wait_improvement = lower_is_better(float(rule.total_wait_ms), agent_wait)
        throughput_improvement = higher_is_better(
            rule.throughput_operations_per_hour, agent_throughput
        ) if agent_throughput is not None else None
        cycle_range_improvement = (
            lower_is_better(float(rule.physical_cycle_count_range), agent_cycle_range)
            if rule.physical_cycle_count_range is not None
            and agent_cycle_range is not None
            else None
        )
        cycle_cv_improvement = (
            lower_is_better(
                float(rule.physical_cycle_count_coefficient_of_variation),
                agent_cycle_cv,
            )
            if rule.physical_cycle_count_coefficient_of_variation is not None
            and agent_cycle_cv is not None
            else None
        )
        cycle_standard_deviation_improvement = (
            lower_is_better(
                float(rule.physical_cycle_count_standard_deviation),
                agent_cycle_standard_deviation,
            )
            if rule.physical_cycle_count_standard_deviation is not None
            and agent_cycle_standard_deviation is not None
            else None
        )
        cycle_gini_improvement = (
            lower_is_better(
                float(rule.physical_cycle_count_gini_coefficient),
                agent_cycle_gini,
            )
            if rule.physical_cycle_count_gini_coefficient is not None
            and agent_cycle_gini is not None
            else None
        )
        work_time_range_improvement = (
            lower_is_better(
                float(rule.scheduled_work_time_range_ms),
                agent_work_time_range,
            )
            if rule.scheduled_work_time_range_ms is not None
            and agent_work_time_range is not None
            else None
        )
        work_time_cv_improvement = (
            lower_is_better(
                float(rule.scheduled_work_time_coefficient_of_variation),
                agent_work_time_cv,
            )
            if rule.scheduled_work_time_coefficient_of_variation is not None
            and agent_work_time_cv is not None
            else None
        )
        work_time_standard_deviation_improvement = (
            lower_is_better(
                float(rule.scheduled_work_time_standard_deviation_ms),
                agent_work_time_standard_deviation,
            )
            if rule.scheduled_work_time_standard_deviation_ms is not None
            and agent_work_time_standard_deviation is not None
            else None
        )
        max_finish_improvement = (
            lower_is_better(float(rule.max_robot_finish_at_ms), agent_max_finish)
            if rule.max_robot_finish_at_ms is not None
            and agent_max_finish is not None
            else None
        )

        def guardrail(
            improvement: float | None,
            *,
            rule_value: float,
            agent_value: float,
            max_regression: float,
        ) -> bool:
            if rule_value == 0:
                return agent_value == 0
            return improvement is not None and improvement >= -max_regression

        distance_guard = guardrail(
            distance_improvement,
            rule_value=float(rule.total_distance_m),
            agent_value=agent_distance,
            max_regression=request.max_agent_distance_regression_pct,
        )
        fleet_guard = guardrail(
            fleet_effort_improvement,
            rule_value=float(rule.fleet_effort_robot_ms),
            agent_value=agent_fleet_effort,
            max_regression=request.max_agent_fleet_effort_regression_pct,
        )
        wait_guard = guardrail(
            wait_improvement,
            rule_value=float(rule.total_wait_ms),
            agent_value=agent_wait,
            max_regression=request.max_agent_wait_regression_pct,
        )
        all_guards = distance_guard and fleet_guard and wait_guard
        tolerance = request.operational_tie_tolerance_pct
        improvements = [
            value
            for value in (
                makespan_improvement,
                fleet_effort_improvement,
                distance_improvement,
                wait_improvement,
            )
            if value is not None
        ]
        is_tie = bool(improvements) and all(abs(value) <= tolerance for value in improvements)
        agent_dominates = bool(improvements) and all(
            value >= -tolerance for value in improvements
        ) and any(value > tolerance for value in improvements)
        rule_dominates = bool(improvements) and all(
            value <= tolerance for value in improvements
        ) and any(value < -tolerance for value in improvements)
        speed_win = (
            makespan_improvement is not None
            and makespan_improvement >= request.min_agent_makespan_improvement_pct
        )
        speed_loss = (
            makespan_improvement is not None
            and makespan_improvement <= -request.min_agent_makespan_improvement_pct
        )

        if is_tie:
            verdict = "TIE"
        elif agent_dominates or (speed_win and all_guards):
            verdict = "AGENT_OPERATIONAL_WIN"
        elif rule_dominates or (
            speed_loss
            and not any(
                value is not None and value > tolerance
                for value in (
                    fleet_effort_improvement,
                    distance_improvement,
                    wait_improvement,
                )
            )
        ):
            verdict = "RULE_OPERATIONAL_WIN"
        else:
            verdict = "TRADEOFF"

        strict_pass = verdict in {"AGENT_OPERATIONAL_WIN", "TIE"} and all_guards
        return PlanningOperationalComparison(
            comparable=True,
            reasons=sorted(set(reasons)),
            verdict=verdict,  # type: ignore[arg-type]
            strict_pass=strict_pass,
            valid_agent_runs=len(compatible),
            agent_median_makespan_ms=agent_makespan,
            makespan_improvement_pct=makespan_improvement,
            agent_median_throughput_operations_per_hour=agent_throughput,
            throughput_improvement_pct=throughput_improvement,
            agent_median_used_robot_count=agent_robots,
            used_robot_delta_agent_minus_rule=(
                agent_robots - rule.used_robot_count
            ),
            agent_median_fleet_effort_robot_ms=agent_fleet_effort,
            fleet_effort_improvement_pct=fleet_effort_improvement,
            agent_median_total_distance_m=agent_distance,
            distance_improvement_pct=distance_improvement,
            agent_median_total_wait_ms=agent_wait,
            wait_improvement_pct=wait_improvement,
            agent_median_physical_cycle_count_range=agent_cycle_range,
            physical_cycle_count_range_improvement_pct=cycle_range_improvement,
            agent_median_physical_cycle_count_standard_deviation=(
                agent_cycle_standard_deviation
            ),
            physical_cycle_count_standard_deviation_improvement_pct=(
                cycle_standard_deviation_improvement
            ),
            agent_median_physical_cycle_count_coefficient_of_variation=(
                agent_cycle_cv
            ),
            physical_cycle_count_cv_improvement_pct=cycle_cv_improvement,
            agent_median_physical_cycle_count_gini_coefficient=agent_cycle_gini,
            physical_cycle_count_gini_improvement_pct=cycle_gini_improvement,
            agent_median_scheduled_work_time_range_ms=agent_work_time_range,
            scheduled_work_time_range_improvement_pct=work_time_range_improvement,
            agent_median_scheduled_work_time_standard_deviation_ms=(
                agent_work_time_standard_deviation
            ),
            scheduled_work_time_standard_deviation_improvement_pct=(
                work_time_standard_deviation_improvement
            ),
            agent_median_scheduled_work_time_coefficient_of_variation=(
                agent_work_time_cv
            ),
            scheduled_work_time_cv_improvement_pct=work_time_cv_improvement,
            agent_median_max_robot_finish_at_ms=agent_max_finish,
            max_robot_finish_at_improvement_pct=max_finish_improvement,
            distance_guardrail_passed=distance_guard,
            fleet_effort_guardrail_passed=fleet_guard,
            wait_guardrail_passed=wait_guard,
            all_resource_guardrails_passed=all_guards,
            **common,
        )

    def compare(
        self,
        evaluation_id: str,
        request: PlanningComparisonRequest,
        *,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> PlanningComparisonReport:
        capture_root = self.store.capture_dir(evaluation_id)
        manifest = self.store.load_manifest(evaluation_id)
        internal = AutoMissionRequest.model_validate(
            _read_json(capture_root / "internal_request.json")
        )
        normalized = _read_json(capture_root / "normalized_request.json")
        shared_normalized = NormalizedWarehouseRequest.model_validate(normalized)
        shared_normalized = shared_normalized.model_copy(
            update={
                "constraints": shared_normalized.constraints.model_copy(
                    update={
                        "objective_profile": request.required_objective_profile,
                        "objective_profile_explicit": True,
                    }
                )
            }
        )
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

        total_runs = request.agent_repeats + 1

        def notify_progress(completed: int, stage: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(completed, total_runs, stage)
            except Exception:
                # Progress reporting is diagnostic metadata and must never make a
                # valid formulation/optimization comparison fail.
                return

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
                            repeat_index=1,
                            result=rule_result,
                            expected_ids=expected_ids,
                            repository=repository,
                        )
                        rule_metrics = self._apply_hard_gate(
                            rule_metrics,
                            required_objective_profile=request.required_objective_profile,
                            require_mapf=(
                                request.require_mapf_gate and request.depth == "mapf"
                            ),
                            require_cost=backend != "cuopt_payload_only",
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
                        rule_metrics = self._apply_hard_gate(
                            rule_metrics,
                            required_objective_profile=request.required_objective_profile,
                            require_mapf=(
                                request.require_mapf_gate and request.depth == "mapf"
                            ),
                            require_cost=backend != "cuopt_payload_only",
                        )
                    notify_progress(1, "RULE_COMPLETED")

                    agent_metrics: list[BranchQualityMetrics] = []
                    for index in range(request.agent_repeats):
                        agent_result = OrchestrationService().run(
                            internal.model_copy(update={"optimization_backend": backend}),
                            trusted_planning_mode="force_agent",
                            persist_simulation_plan=False,
                        )
                        metrics = self._metrics(
                            route="AGENT_FORMULATION",
                            repeat_index=index + 1,
                            result=agent_result,
                            expected_ids=expected_ids,
                            repository=repository,
                        )
                        metrics = self._apply_hard_gate(
                            metrics,
                            required_objective_profile=request.required_objective_profile,
                            require_mapf=(
                                request.require_mapf_gate and request.depth == "mapf"
                            ),
                            require_cost=backend != "cuopt_payload_only",
                        )
                        agent_metrics.append(metrics)
                        _write_json(
                            self.store.comparisons
                            / evaluation_id
                            / f"agent_result_{index + 1}.json",
                            agent_result.model_dump(mode="json"),
                        )
                        notify_progress(index + 2, f"AGENT_{index + 1}_COMPLETED")
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
            "hard_gate_pass_rate": (
                sum(value.hard_gate_passed for value in agent_metrics)
                / len(agent_metrics)
                if agent_metrics
                else 0.0
            ),
            "global_objective_costs": [
                value.global_objective_cost for value in agent_metrics
            ],
            "makespan_ms": [value.makespan_ms for value in agent_metrics],
            "fleet_effort_robot_ms": [
                value.fleet_effort_robot_ms for value in agent_metrics
            ],
            "total_distance_m": [
                value.total_distance_m for value in agent_metrics
            ],
            "total_wait_ms": [value.total_wait_ms for value in agent_metrics],
            "physical_cycle_count_ranges": [
                value.physical_cycle_count_range for value in agent_metrics
            ],
            "physical_cycle_count_standard_deviations": [
                value.physical_cycle_count_standard_deviation
                for value in agent_metrics
            ],
            "physical_cycle_count_coefficients_of_variation": [
                value.physical_cycle_count_coefficient_of_variation
                for value in agent_metrics
            ],
            "physical_cycle_count_gini_coefficients": [
                value.physical_cycle_count_gini_coefficient
                for value in agent_metrics
            ],
            "scheduled_work_time_ranges_ms": [
                value.scheduled_work_time_range_ms for value in agent_metrics
            ],
            "scheduled_work_time_standard_deviations_ms": [
                value.scheduled_work_time_standard_deviation_ms
                for value in agent_metrics
            ],
            "scheduled_work_time_coefficients_of_variation": [
                value.scheduled_work_time_coefficient_of_variation
                for value in agent_metrics
            ],
            "max_robot_finish_at_ms": [
                value.max_robot_finish_at_ms for value in agent_metrics
            ],
        }
        operational_comparison = self._operational_comparison(
            rule_metrics, agent_metrics, request
        )
        cost_comparison = self._cost_comparison(
            rule_metrics, agent_metrics, request
        )
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
            "physical_cycle_count_range_delta_agent_minus_rule": (
                agent_first.physical_cycle_count_range
                - rule_metrics.physical_cycle_count_range
                if agent_first is not None
                and agent_first.physical_cycle_count_range is not None
                and rule_metrics.physical_cycle_count_range is not None
                else None
            ),
            "physical_cycle_count_cv_delta_agent_minus_rule": (
                round(
                    agent_first.physical_cycle_count_coefficient_of_variation
                    - rule_metrics.physical_cycle_count_coefficient_of_variation,
                    6,
                )
                if agent_first is not None
                and agent_first.physical_cycle_count_coefficient_of_variation
                is not None
                and rule_metrics.physical_cycle_count_coefficient_of_variation
                is not None
                else None
            ),
            "physical_cycle_count_standard_deviation_delta_agent_minus_rule": (
                round(
                    agent_first.physical_cycle_count_standard_deviation
                    - rule_metrics.physical_cycle_count_standard_deviation,
                    6,
                )
                if agent_first is not None
                and agent_first.physical_cycle_count_standard_deviation is not None
                and rule_metrics.physical_cycle_count_standard_deviation is not None
                else None
            ),
            "physical_cycle_count_gini_delta_agent_minus_rule": (
                round(
                    agent_first.physical_cycle_count_gini_coefficient
                    - rule_metrics.physical_cycle_count_gini_coefficient,
                    6,
                )
                if agent_first is not None
                and agent_first.physical_cycle_count_gini_coefficient is not None
                and rule_metrics.physical_cycle_count_gini_coefficient is not None
                else None
            ),
            "scheduled_work_time_range_delta_agent_minus_rule_ms": (
                agent_first.scheduled_work_time_range_ms
                - rule_metrics.scheduled_work_time_range_ms
                if agent_first is not None
                and agent_first.scheduled_work_time_range_ms is not None
                and rule_metrics.scheduled_work_time_range_ms is not None
                else None
            ),
            "scheduled_work_time_cv_delta_agent_minus_rule": (
                round(
                    agent_first.scheduled_work_time_coefficient_of_variation
                    - rule_metrics.scheduled_work_time_coefficient_of_variation,
                    6,
                )
                if agent_first is not None
                and agent_first.scheduled_work_time_coefficient_of_variation
                is not None
                and rule_metrics.scheduled_work_time_coefficient_of_variation
                is not None
                else None
            ),
            "scheduled_work_time_standard_deviation_delta_agent_minus_rule_ms": (
                round(
                    agent_first.scheduled_work_time_standard_deviation_ms
                    - rule_metrics.scheduled_work_time_standard_deviation_ms,
                    6,
                )
                if agent_first is not None
                and agent_first.scheduled_work_time_standard_deviation_ms is not None
                and rule_metrics.scheduled_work_time_standard_deviation_ms is not None
                else None
            ),
            "max_robot_finish_at_delta_agent_minus_rule_ms": (
                round(
                    agent_first.max_robot_finish_at_ms
                    - rule_metrics.max_robot_finish_at_ms,
                    6,
                )
                if agent_first is not None
                and agent_first.max_robot_finish_at_ms is not None
                and rule_metrics.max_robot_finish_at_ms is not None
                else None
            ),
            "quality_note": (
                "Operational quality is primary: makespan is judged with fleet-effort, "
                "distance, and MAPF-wait guardrails on the same frozen snapshot and "
                f"{request.required_objective_profile} profile. Raw cuOpt objective cost and "
                "latency remain secondary diagnostics. Physical-cycle and scheduled-work "
                "distribution metrics expose concentration but remain diagnostic until "
                "scenario-specific acceptance thresholds are calibrated."
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
            operational_comparison=operational_comparison,
            cost_comparison=cost_comparison,
            created_at=_utc_now(),
        )
        _write_json(
            self.store.comparison_path(evaluation_id),
            report.model_dump(mode="json"),
        )
        notify_progress(total_runs, "REPORT_COMPLETED")
        manifest.update(
            {
                "status": "COMPARISON_READY",
                "comparison_status": "COMPLETED",
                "comparison_completed_at": report.created_at,
            }
        )
        self.store.save_manifest(evaluation_id, manifest)
        return report

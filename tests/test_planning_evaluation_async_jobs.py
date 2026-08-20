from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.domain.planning_evaluation import (
    PlanningComparisonJob,
    PlanningComparisonJobRequest,
)
from app.services.planning_evaluation_job_service import (
    PlanningEvaluationJobService,
    PlanningEvaluationJobStore,
)
from app.services.planning_evaluation_service import PlanningEvaluationStore


def _capture(store: PlanningEvaluationStore, evaluation_id: str) -> None:
    store.save_manifest(
        evaluation_id,
        {
            "evaluation_id": evaluation_id,
            "status": "CAPTURED",
            "comparison_status": "NOT_STARTED",
        },
    )


def _wait_for_terminal(
    service: PlanningEvaluationJobService,
    job_id: str,
    timeout: float = 2.0,
) -> PlanningComparisonJob:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job.status in {"SUCCEEDED", "FAILED"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"Job {job_id} did not finish")


class _ImmediateComparison:
    def __init__(self, store: PlanningEvaluationStore) -> None:
        self.store = store

    def compare(self, evaluation_id, request, *, progress_callback=None):
        total = request.agent_repeats + 1
        if progress_callback:
            progress_callback(1, total, "RULE_COMPLETED")
            for index in range(request.agent_repeats):
                progress_callback(
                    index + 2,
                    total,
                    f"AGENT_{index + 1}_COMPLETED",
                )
        class _Report:
            def model_dump(self, mode="json"):
                del mode
                return {
                    "evaluation_id": evaluation_id,
                    "agent_repeats": request.agent_repeats,
                }

        return _Report()


def test_async_job_completes_and_same_request_is_idempotent(tmp_path: Path) -> None:
    evaluation_store = PlanningEvaluationStore(tmp_path / "evaluations")
    job_store = PlanningEvaluationJobStore(evaluation_store.root)
    _capture(evaluation_store, "EVAL-001")
    service = PlanningEvaluationJobService(
        evaluation_store=evaluation_store,
        job_store=job_store,
        max_workers=1,
        comparison_factory=_ImmediateComparison,
    )
    try:
        request = PlanningComparisonJobRequest(
            backend="cuopt",
            depth="mapf",
            agent_repeats=5,
            min_valid_agent_runs=3,
            idempotency_key="PC01-v1",
        )
        submitted = service.submit("EVAL-001", request)
        completed = _wait_for_terminal(service, submitted.job_id)

        assert completed.status == "SUCCEEDED"
        assert completed.completed_runs == 6
        assert completed.total_runs == 6
        assert completed.current_stage == "COMPLETED"
        assert completed.comparison_request["agent_repeats"] == 5
        assert service.get_result(submitted.job_id) == {
            "evaluation_id": "EVAL-001",
            "agent_repeats": 5,
        }

        duplicate = service.submit("EVAL-001", request)
        assert duplicate.job_id == submitted.job_id

        forced = service.submit(
            "EVAL-001",
            request.model_copy(update={"force_new": True}),
        )
        assert forced.job_id != submitted.job_id
        assert _wait_for_terminal(service, forced.job_id).status == "SUCCEEDED"
    finally:
        service.shutdown(wait=True)


def test_async_jobs_are_queued_and_run_one_at_a_time(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    execution_order: list[str] = []

    class _BlockingComparison:
        def __init__(self, store: PlanningEvaluationStore) -> None:
            self.store = store

        def compare(self, evaluation_id, request, *, progress_callback=None):
            del request, progress_callback
            execution_order.append(evaluation_id)
            if evaluation_id == "EVAL-001":
                first_started.set()
                assert release_first.wait(timeout=2.0)
            return object()

    evaluation_store = PlanningEvaluationStore(tmp_path / "evaluations")
    job_store = PlanningEvaluationJobStore(evaluation_store.root)
    _capture(evaluation_store, "EVAL-001")
    _capture(evaluation_store, "EVAL-002")
    service = PlanningEvaluationJobService(
        evaluation_store=evaluation_store,
        job_store=job_store,
        max_workers=1,
        comparison_factory=_BlockingComparison,
    )
    try:
        first = service.submit(
            "EVAL-001",
            PlanningComparisonJobRequest(idempotency_key="first"),
        )
        assert first_started.wait(timeout=1.0)
        second = service.submit(
            "EVAL-002",
            PlanningComparisonJobRequest(idempotency_key="second"),
        )
        assert service.get(first.job_id).status == "RUNNING"
        assert service.get(second.job_id).status == "QUEUED"

        release_first.set()
        assert _wait_for_terminal(service, first.job_id).status == "SUCCEEDED"
        assert _wait_for_terminal(service, second.job_id).status == "SUCCEEDED"
        assert execution_order == ["EVAL-001", "EVAL-002"]
    finally:
        release_first.set()
        service.shutdown(wait=True)


def test_previous_process_active_job_is_marked_interrupted(tmp_path: Path) -> None:
    evaluation_store = PlanningEvaluationStore(tmp_path / "evaluations")
    job_store = PlanningEvaluationJobStore(evaluation_store.root)
    _capture(evaluation_store, "EVAL-001")
    job_store.save(
        PlanningComparisonJob(
            job_id="EJOB-0123456789ABCDEF",
            evaluation_id="EVAL-001",
            status="RUNNING",
            request_fingerprint="fingerprint",
            total_runs=6,
            created_at="2026-08-11T00:00:00+00:00",
            status_url="/status",
            result_url="/result",
        )
    )
    service = PlanningEvaluationJobService(
        evaluation_store=evaluation_store,
        job_store=job_store,
        max_workers=1,
        comparison_factory=_ImmediateComparison,
    )
    try:
        recovered = service.get("EJOB-0123456789ABCDEF")
        assert recovered.status == "FAILED"
        assert recovered.current_stage == "INTERRUPTED"
        assert recovered.error_type == "LocalProcessRestarted"
    finally:
        service.shutdown(wait=True)


def test_evaluation_http_surface_is_small_and_feature_gated(monkeypatch) -> None:
    with TestClient(app) as client:
        exposed_paths = set(client.get("/openapi.json").json()["paths"])
        disabled = client.post(
            "/api/v1/debug/evaluation-suites/run-async",
            json={"materialize_only": True},
        )
        retired = client.post(
            "/api/v1/debug/evaluations/EVAL-API/compare-async",
            json={},
        )
    assert exposed_paths == {
        "/health",
        "/optimize",
        "/reoptimize",
        "/api/v1/simulation-runs/{simulation_run_id}/missions/plan/preflight",
        "/api/v1/simulation-runs/{simulation_run_id}/fulfillment-commands/generate",
        "/api/v1/simulation-runs/{simulation_run_id}/missions/plan",
        "/api/v1/simulation-runs/{simulation_run_id}/missions/replan",
        "/api/v1/simulation-runs/{simulation_run_id}/hitl/{interaction_id}/respond",
        "/api/v1/debug/evaluation-suites/run-async",
        "/api/v1/debug/evaluation-suites/{suite_id}",
        "/api/v1/debug/evaluations/{evaluation_id}",
        "/api/v1/debug/evaluation-jobs/{job_id}/result",
    }
    assert disabled.status_code == 404
    assert retired.status_code == 404

    class _Settings:
        planning_evaluation_api_enabled = True

    class _SuiteService:
        def start(self, request):
            assert request.materialize_only is True
            return {
                "suite_id": "ESUITE-API",
                "status": "SUCCEEDED",
                "scenario_count": 30,
            }

    monkeypatch.setattr("app.api.routes.get_settings", lambda: _Settings())
    monkeypatch.setattr(
        "app.api.routes.get_planning_scenario_suite_service",
        lambda: _SuiteService(),
    )
    with TestClient(app) as client:
        enabled = client.post(
            "/api/v1/debug/evaluation-suites/run-async",
            json={"materialize_only": True},
        )
    assert enabled.status_code == 202
    assert enabled.json()["scenario_count"] == 30

"""Local file-backed asynchronous jobs for deferred planning comparisons."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable
from uuid import uuid4

from app.core.config import get_settings
from app.domain.planning_evaluation import (
    PlanningComparisonJob,
    PlanningComparisonJobRequest,
    PlanningComparisonRequest,
)
from app.services.planning_evaluation_service import (
    PlanningComparisonService,
    PlanningEvaluationStore,
)
from app.services.planning_dynamic_comparison_service import (
    PlanningDynamicComparisonService,
)


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


_JOB_ID_PATTERN = re.compile(r"^EJOB-[A-F0-9]{16}$")


def _read_job(path: Path) -> PlanningComparisonJob:
    """Read through short Windows replace/antivirus locks on status files."""

    for attempt in range(10):
        try:
            return PlanningComparisonJob.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


class PlanningEvaluationJobStore:
    """Atomic JSON status store outside the active planning repositories."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_settings().planning_evaluation_output_dir
        self.jobs = self.root / "jobs"
        self.results = self.root / "job-results"
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)

    def path(self, job_id: str) -> Path:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise FileNotFoundError(f"Unknown evaluation job {job_id}.")
        return self.jobs / f"{job_id}.json"

    def save(self, job: PlanningComparisonJob) -> None:
        _write_json(self.path(job.job_id), job.model_dump(mode="json"))

    def result_dir(self, job_id: str) -> Path:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise FileNotFoundError(f"Unknown evaluation job {job_id}.")
        return self.results / job_id

    def result_path(self, job_id: str) -> Path:
        return self.result_dir(job_id) / "comparison_report.json"

    def save_result_bundle(
        self,
        job_id: str,
        source_dir: Path,
        report: object,
    ) -> None:
        destination = self.result_dir(job_id)
        if destination.exists():
            shutil.rmtree(destination)
        if source_dir.exists():
            shutil.copytree(source_dir, destination)
        else:
            destination.mkdir(parents=True, exist_ok=True)
        model_dump = getattr(report, "model_dump", None)
        if callable(model_dump):
            _write_json(
                self.result_path(job_id),
                model_dump(mode="json"),
            )
        elif isinstance(report, dict):
            _write_json(self.result_path(job_id), report)

    def get_result(self, job_id: str) -> dict[str, object]:
        path = self.result_path(job_id)
        if not path.exists():
            raise FileNotFoundError(
                f"Evaluation job {job_id} has no completed result."
            )
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Evaluation job {job_id} result must be an object.")
        return value

    def get(self, job_id: str) -> PlanningComparisonJob:
        path = self.path(job_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown evaluation job {job_id}.")
        return _read_job(path)

    def list(self, *, limit: int = 100) -> list[PlanningComparisonJob]:
        values: list[PlanningComparisonJob] = []
        for path in sorted(
            self.jobs.glob("*.json"),
            key=lambda value: value.stat().st_mtime,
            reverse=True,
        )[:limit]:
            try:
                values.append(_read_job(path))
            except Exception:
                continue
        return values

    def mark_abandoned(self) -> None:
        """Mark jobs left active by a previous local process as interrupted."""

        for job in self.list(limit=10000):
            if job.status not in {"QUEUED", "RUNNING"}:
                continue
            self.save(
                job.model_copy(
                    update={
                        "status": "FAILED",
                        "current_stage": "INTERRUPTED",
                        "completed_at": _utc_now(),
                        "error_type": "LocalProcessRestarted",
                        "error_message": (
                            "The local evaluation process stopped before this job "
                            "finished. Submit it again to retry."
                        ),
                    }
                )
            )


ComparisonFactory = Callable[[PlanningEvaluationStore], PlanningComparisonService]


class PlanningEvaluationJobService:
    """Queue local comparisons while keeping every replay serialized by default."""

    def __init__(
        self,
        *,
        evaluation_store: PlanningEvaluationStore | None = None,
        job_store: PlanningEvaluationJobStore | None = None,
        max_workers: int | None = None,
        comparison_factory: ComparisonFactory | None = None,
        dynamic_comparison_factory: ComparisonFactory | None = None,
    ) -> None:
        self.evaluation_store = evaluation_store or PlanningEvaluationStore()
        self.job_store = job_store or PlanningEvaluationJobStore(
            self.evaluation_store.root
        )
        # PlanningComparisonService temporarily selects one frozen JSON
        # repository globally. Serial jobs prevent cross-capture leakage.
        workers = max_workers or 1
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="planning-evaluation",
        )
        self._comparison_factory = comparison_factory
        self._dynamic_comparison_factory = (
            dynamic_comparison_factory or PlanningDynamicComparisonService
        )
        self._lock = threading.RLock()
        self._futures: dict[str, Future[object]] = {}
        # Status is persisted for restart recovery, but active-process polling
        # should not repeatedly compete with Windows file replacement and
        # antivirus scans.  Keep the latest immutable model in memory and use
        # disk as the durable fallback after a process restart.
        self._jobs: dict[str, PlanningComparisonJob] = {}
        self.job_store.mark_abandoned()

    @staticmethod
    def _fingerprint(
        evaluation_id: str,
        comparison: PlanningComparisonRequest,
        idempotency_key: str | None,
    ) -> str:
        payload = {
            "evaluation_id": evaluation_id,
            "comparison": comparison.model_dump(mode="json"),
            "idempotency_key": idempotency_key,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def submit(
        self,
        evaluation_id: str,
        request: PlanningComparisonJobRequest,
    ) -> PlanningComparisonJob:
        # Fail before enqueueing when the capture is missing.
        self.evaluation_store.load_manifest(evaluation_id)
        comparison = request.comparison_request()
        fingerprint = self._fingerprint(
            evaluation_id, comparison, request.idempotency_key
        )
        with self._lock:
            if not request.force_new:
                existing = next(
                    (
                        value
                        for value in self.job_store.list(limit=1000)
                        if value.request_fingerprint == fingerprint
                    ),
                    None,
                )
                if existing is not None:
                    return existing

            job_id = f"EJOB-{uuid4().hex[:16].upper()}"
            job = PlanningComparisonJob(
                job_id=job_id,
                evaluation_id=evaluation_id,
                status="QUEUED",
                request_fingerprint=fingerprint,
                idempotency_key=request.idempotency_key,
                comparison_request=comparison.model_dump(mode="json"),
                total_runs=comparison.agent_repeats + 1,
                created_at=_utc_now(),
                status_url=f"/api/v1/debug/evaluation-jobs/{job_id}",
                result_url=(
                    f"/api/v1/debug/evaluation-jobs/{job_id}/result"
                ),
            )
            self.job_store.save(job)
            self._jobs[job_id] = job
            self._futures[job_id] = self._executor.submit(
                self._run, job_id, comparison
            )
            return job

    def _update(self, job_id: str, **changes: object) -> PlanningComparisonJob:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                current = self.job_store.get(job_id)
            updated = current.model_copy(update=changes)
            self.job_store.save(updated)
            self._jobs[job_id] = updated
            return updated

    def _run(
        self,
        job_id: str,
        request: PlanningComparisonRequest,
    ) -> None:
        current = self._update(
            job_id,
            status="RUNNING",
            current_stage="STARTING",
            started_at=_utc_now(),
        )

        def progress(completed: int, total: int, stage: str) -> None:
            self._update(
                job_id,
                completed_runs=completed,
                total_runs=total,
                current_stage=stage,
            )

        try:
            manifest = self.evaluation_store.load_manifest(current.evaluation_id)
            group = str(manifest.get("scenario_group") or "INITIAL")
            if self._comparison_factory is not None:
                factory = self._comparison_factory
            elif group in {"REPLAN", "HUMAN_REVIEW"}:
                factory = self._dynamic_comparison_factory
            else:
                factory = PlanningComparisonService
            service = factory(self.evaluation_store)
            report = service.compare(
                current.evaluation_id,
                request,
                progress_callback=progress,
            )
            self.job_store.save_result_bundle(
                job_id,
                self.evaluation_store.comparisons / current.evaluation_id,
                report,
            )
            self._update(
                job_id,
                status="SUCCEEDED",
                current_stage="COMPLETED",
                completed_runs=current.total_runs,
                completed_at=_utc_now(),
            )
        except Exception as exc:
            self._update(
                job_id,
                status="FAILED",
                current_stage="FAILED",
                completed_at=_utc_now(),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def get(self, job_id: str) -> PlanningComparisonJob:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is not None:
                return current
            current = self.job_store.get(job_id)
            self._jobs[job_id] = current
            return current

    def list(self, *, limit: int = 100) -> list[PlanningComparisonJob]:
        return self.job_store.list(limit=limit)

    def get_result(self, job_id: str) -> dict[str, object]:
        job = self.get(job_id)
        if job.status != "SUCCEEDED":
            raise RuntimeError(
                f"Evaluation job {job_id} is {job.status}, not SUCCEEDED."
            )
        return self.job_store.get_result(job_id)

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


@lru_cache(maxsize=1)
def get_planning_evaluation_job_service() -> PlanningEvaluationJobService:
    return PlanningEvaluationJobService()


def shutdown_planning_evaluation_job_service() -> None:
    if get_planning_evaluation_job_service.cache_info().currsize:
        get_planning_evaluation_job_service().shutdown(wait=False)
        get_planning_evaluation_job_service.cache_clear()

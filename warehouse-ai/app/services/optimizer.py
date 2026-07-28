from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models import CuOptPlan, ObjectiveBreakdown, TaskOptimizationEvidence
from app.services.cuopt import CuOptHttpOptimizer
from app.services.cuopt_rest import CuOptRestOptimizer
from app.services.local_optimizer import LocalOptimizer


class OptimizerSettings(Protocol):
    optimizer_backend: str
    cuopt_url: str
    cuopt_client_sak: str
    cuopt_api_key: str
    cuopt_rest_url: str
    cuopt_status_url: str
    cuopt_auto_enable: bool
    cuopt_fallback_to_local: bool
    cuopt_poll_timeout_seconds: float
    cuopt_poll_interval_seconds: float
    cuopt_solver_time_limit_seconds: int
    request_timeout_seconds: float
    time_step_seconds: int
    min_robot_battery: float
    battery_safety_margin_percent: float
    energy_per_distance: float
    charge_target_battery: float
    charge_rate_percent_per_minute: float


class Optimizer(Protocol):
    def optimize(self, problem: dict[str, Any]) -> CuOptPlan: ...


@dataclass(frozen=True)
class OptimizationOutcome:
    plan: CuOptPlan
    backend: str
    warnings: list[str]
    optimization_evidence: list[TaskOptimizationEvidence]
    objective_breakdown: ObjectiveBreakdown | None
    execution: dict[str, Any] = field(default_factory=dict)


def _setting(settings: OptimizerSettings, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _cuopt_api_key(settings: OptimizerSettings) -> str:
    """Return the NVIDIA API key using the friendly name or legacy SAK alias."""
    return str(
        _setting(settings, "cuopt_api_key", "")
        or _setting(settings, "cuopt_client_sak", "")
        or ""
    ).strip()


def _cuopt_key_source(settings: OptimizerSettings) -> str | None:
    if str(_setting(settings, "cuopt_api_key", "") or "").strip():
        return "CUOPT_API_KEY"
    if str(_setting(settings, "cuopt_client_sak", "") or "").strip():
        return "CUOPT_CLIENT_SAK_COMPAT"
    return None


def _local(settings: OptimizerSettings) -> LocalOptimizer:
    return LocalOptimizer(
        time_step_seconds=settings.time_step_seconds,
        min_robot_battery=settings.min_robot_battery,
        energy_per_distance=settings.energy_per_distance,
        charge_target_battery=_setting(settings, "charge_target_battery", 80.0),
        charge_rate_percent_per_minute=_setting(
            settings, "charge_rate_percent_per_minute", 5.0
        ),
        battery_safety_margin_percent=_setting(
            settings, "battery_safety_margin_percent", 0.5
        ),
    )


def _local_outcome(
    problem: dict[str, Any],
    settings: OptimizerSettings,
    *,
    requested_provider: str,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    warnings: list[str] | None = None,
    attempts: list[dict[str, Any]] | None = None,
) -> OptimizationOutcome:
    optimizer = _local(settings)
    plan = optimizer.optimize(problem)
    execution = {
        "requested_provider": requested_provider,
        "used_provider": "CPU",
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "attempts": attempts
        or [{"provider": "CPU", "status": "SUCCESS"}],
    }
    plan.metadata = {**plan.metadata, "optimizer_execution": execution}
    return OptimizationOutcome(
        plan,
        "local",
        warnings or [],
        optimizer.last_optimization_evidence,
        optimizer.last_objective_breakdown,
        execution,
    )


def optimize_problem_locally(
    problem: dict[str, Any],
    settings: OptimizerSettings,
    *,
    requested_provider: str = "CUOPT",
    fallback_reason: str | None = None,
    warnings: list[str] | None = None,
    attempts: list[dict[str, Any]] | None = None,
) -> OptimizationOutcome:
    """Run the deterministic CPU optimizer regardless of configured backend.

    This is used for bounded recovery after a managed optimizer returned a
    syntactically valid but warehouse-contract-invalid result.  It must not
    re-enter AUTO selection because credentials could select cuOpt again.
    """

    return _local_outcome(
        problem,
        settings,
        requested_provider=requested_provider,
        fallback_used=True,
        fallback_reason=fallback_reason,
        warnings=warnings,
        attempts=attempts,
    )


def validate_or_fallback_charge_visit_second_pass(
    problem: dict[str, Any],
    settings: OptimizerSettings,
    outcome: OptimizationOutcome,
    contract: dict[str, Any],
) -> tuple[OptimizationOutcome, bool]:
    """Enforce task preservation after the managed charge-visit second pass.

    Managed cuOpt may return a formally successful route that omits warehouse
    chain members after standalone CHARGE/MOVE visits are introduced.  Such a
    result is unusable.  Recover once with the deterministic CPU optimizer and
    validate the same robot/task contract again.
    """

    from app.services.charge_visit_optimization import (
        charge_visit_robot_binding_errors,
    )

    binding_errors = charge_visit_robot_binding_errors(outcome.plan, contract)
    if not binding_errors:
        return outcome, False

    failure_reason = "; ".join(binding_errors)
    prior_attempts = list(outcome.execution.get("attempts") or [])
    prior_attempts.append(
        {
            "provider": "SECOND_PASS_CONTRACT_VALIDATION",
            "status": "FAILED",
            "error_code": failure_reason,
        }
    )
    fallback = optimize_problem_locally(
        problem,
        settings,
        requested_provider="CUOPT_SECOND_PASS",
        fallback_reason=failure_reason,
        warnings=[
            "cuOpt 2차 충전 방문 결과가 작업 보존 계약을 위반해 "
            "CPU optimizer로 재실행했습니다: " + failure_reason
        ],
        attempts=prior_attempts
        + [
            {
                "provider": "CPU_SECOND_PASS_FALLBACK",
                "status": "SUCCESS",
            }
        ],
    )
    fallback_errors = charge_visit_robot_binding_errors(fallback.plan, contract)
    if fallback_errors:
        raise RuntimeError("; ".join(fallback_errors))
    return fallback, True


def _nvidia_rest_cuopt_outcome(
    problem: dict[str, Any], settings: OptimizerSettings
) -> OptimizationOutcome:
    local = _local(settings)
    managed = CuOptRestOptimizer(
        api_key=_cuopt_api_key(settings),
        rest_url=_setting(
            settings,
            "cuopt_rest_url",
            "https://optimize.api.nvidia.com/v1/nvidia/cuopt",
        ),
        status_url=_setting(
            settings,
            "cuopt_status_url",
            "https://optimize.api.nvidia.com/v1/status/{request_id}",
        ),
        request_timeout_seconds=settings.request_timeout_seconds,
        poll_timeout_seconds=_setting(settings, "cuopt_poll_timeout_seconds", 30.0),
        poll_interval_seconds=_setting(
            settings, "cuopt_poll_interval_seconds", 1.0
        ),
        solver_time_limit_seconds=_setting(
            settings, "cuopt_solver_time_limit_seconds", 10
        ),
        local_optimizer=local,
    )
    result = managed.optimize(problem)
    execution = {
        "requested_provider": "CUOPT",
        "used_provider": "CUOPT",
        "fallback_used": False,
        "fallback_reason": None,
        "attempts": [
            {
                "provider": "CUOPT_REST",
                "status": "SUCCESS",
                "request_id": result.request_id,
                "solver_status": result.solver_status,
                "solution_cost": result.solution_cost,
            }
        ],
        "schedule_postprocessor": "LOCAL_WAREHOUSE_NORMALIZER",
        "transport": "HTTPS_REST",
    }
    result.plan.metadata = {
        **result.plan.metadata,
        "optimizer_execution": execution,
    }
    return OptimizationOutcome(
        result.plan,
        "cuopt",
        [],
        local.last_optimization_evidence,
        local.last_objective_breakdown,
        execution,
    )


def _custom_http_outcome(
    problem: dict[str, Any], settings: OptimizerSettings
) -> OptimizationOutcome:
    plan = CuOptHttpOptimizer(
        settings.cuopt_url,
        settings.request_timeout_seconds,
    ).optimize(problem)
    execution = {
        "requested_provider": "CUOPT",
        "used_provider": "CUOPT_HTTP",
        "fallback_used": False,
        "fallback_reason": None,
        "attempts": [{"provider": "CUOPT_HTTP", "status": "SUCCESS"}],
    }
    plan.metadata = {**plan.metadata, "optimizer_execution": execution}
    return OptimizationOutcome(plan, "cuopt", [], [], None, execution)


def optimize_problem(
    problem: dict[str, Any],
    settings: OptimizerSettings,
) -> OptimizationOutcome:
    configured_backend = str(settings.optimizer_backend or "auto").lower()
    api_key = _cuopt_api_key(settings)
    cuopt_url = str(_setting(settings, "cuopt_url", "") or "").strip()
    cuopt_auto_enable = bool(_setting(settings, "cuopt_auto_enable", True))
    has_cuopt_credentials = bool(cuopt_url or api_key)
    credential_source = "CUOPT_URL" if cuopt_url else _cuopt_key_source(settings)

    if configured_backend not in {"auto", "local", "cuopt"}:
        raise RuntimeError(
            f"지원하지 않는 OPTIMIZER_BACKEND: {configured_backend}"
        )

    # Backward-compatible key-only setup: older .env files may still contain
    # OPTIMIZER_BACKEND=local. When a cuOpt key/URL is added, cuOpt becomes
    # primary automatically unless CUOPT_AUTO_ENABLE=false is explicitly set.
    auto_enabled_by_credentials = (
        configured_backend == "local"
        and cuopt_auto_enable
        and has_cuopt_credentials
    )
    backend = "auto" if auto_enabled_by_credentials else configured_backend

    if backend == "local":
        outcome = _local_outcome(
            problem,
            settings,
            requested_provider="CPU",
        )
        outcome.execution.update(
            {
                "configured_backend": configured_backend,
                "cuopt_auto_enable": cuopt_auto_enable,
                "auto_enabled_by_credentials": False,
                "credentials_detected": has_cuopt_credentials,
                "credential_source": credential_source,
            }
        )
        outcome.plan.metadata = {
            **outcome.plan.metadata,
            "optimizer_execution": outcome.execution,
        }
        return outcome

    # Existing team-operated HTTP adapter takes precedence when explicitly set.
    # Otherwise a single API key invokes NVIDIA API Catalog over plain HTTPS.
    provider = "CUOPT_HTTP" if cuopt_url else "CUOPT_REST"
    should_try_cuopt = has_cuopt_credentials
    if not should_try_cuopt:
        if backend == "cuopt" and not settings.cuopt_fallback_to_local:
            raise RuntimeError("CUOPT_API_KEY 또는 CUOPT_URL이 필요합니다.")
        outcome = _local_outcome(
            problem,
            settings,
            requested_provider="CUOPT" if backend == "cuopt" else "AUTO",
            fallback_used=backend == "cuopt",
            fallback_reason=(
                "CUOPT_CREDENTIALS_MISSING" if backend == "cuopt" else None
            ),
            warnings=(
                ["cuOpt 자격정보가 없어 CPU optimizer를 사용했습니다."]
                if backend == "cuopt"
                else []
            ),
        )
        outcome.execution.update(
            {
                "configured_backend": configured_backend,
                "cuopt_auto_enable": cuopt_auto_enable,
                "auto_enabled_by_credentials": False,
                "credentials_detected": has_cuopt_credentials,
                "credential_source": credential_source,
            }
        )
        outcome.plan.metadata = {
            **outcome.plan.metadata,
            "optimizer_execution": outcome.execution,
        }
        return outcome

    try:
        outcome = (
            _custom_http_outcome(problem, settings)
            if cuopt_url
            else _nvidia_rest_cuopt_outcome(problem, settings)
        )
        outcome.execution.update(
            {
                "configured_backend": configured_backend,
                "cuopt_auto_enable": cuopt_auto_enable,
                "auto_enabled_by_credentials": auto_enabled_by_credentials,
                "credentials_detected": has_cuopt_credentials,
                "credential_source": credential_source,
            }
        )
        outcome.plan.metadata = {
            **outcome.plan.metadata,
            "optimizer_execution": outcome.execution,
        }
        return outcome
    except Exception as exc:
        if not settings.cuopt_fallback_to_local:
            raise
        reason = f"{type(exc).__name__}:{exc}"
        warning = f"cuOpt 호출 실패로 CPU optimizer를 사용했습니다: {exc}"
        outcome = _local_outcome(
            problem,
            settings,
            requested_provider="CUOPT",
            fallback_used=True,
            fallback_reason=reason,
            warnings=[warning],
            attempts=[
                {
                    "provider": provider,
                    "status": "FAILED",
                    "error_code": reason,
                },
                {"provider": "CPU", "status": "SUCCESS"},
            ],
        )
        outcome.execution.update(
            {
                "configured_backend": configured_backend,
                "cuopt_auto_enable": cuopt_auto_enable,
                "auto_enabled_by_credentials": auto_enabled_by_credentials,
                "credentials_detected": has_cuopt_credentials,
                "credential_source": credential_source,
            }
        )
        outcome.plan.metadata = {
            **outcome.plan.metadata,
            "optimizer_execution": outcome.execution,
        }
        return outcome

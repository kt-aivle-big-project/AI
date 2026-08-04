"""Local/test-only helpers for deterministic HTTP scenario execution."""
from __future__ import annotations

from app.core.config import Settings, get_settings
from app.domain.schemas import (
    ScenarioRuntimeBootstrapRequest,
    ScenarioRuntimeBootstrapResult,
)
from app.infrastructure.manager import InfrastructureManager, get_infrastructure_manager
from app.repositories.json_repository import get_repository


class ScenarioDebugDisabledError(RuntimeError):
    """Raised when a debug-only scenario endpoint is disabled."""


class ScenarioRuntimeBootstrapService:
    """Clone the configured baseline Redis runtime into one scenario namespace."""

    def __init__(
        self,
        settings: Settings | None = None,
        manager: InfrastructureManager | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.manager = manager or get_infrastructure_manager()

    def bootstrap(
        self, request: ScenarioRuntimeBootstrapRequest
    ) -> ScenarioRuntimeBootstrapResult:
        if not self.settings.debug_scenario_api_enabled:
            raise ScenarioDebugDisabledError(
                "Debug scenario runtime bootstrap is disabled. Set "
                "DEBUG_SCENARIO_API_ENABLED=true in the local/test environment."
            )
        if self.settings.warehouse_repository_backend not in {"live", "embedded"}:
            raise ValueError(
                "Scenario runtime bootstrap requires WAREHOUSE_REPOSITORY_BACKEND=live or embedded."
            )
        source = str(
            request.source_simulation_id or self.settings.runtime_simulation_id
        )
        if not self.manager.redis.all_robots(request.warehouse_id, source):
            candidates = self.manager.redis.list_simulation_ids(
                request.warehouse_id
            )
            preferred = [
                value
                for value in candidates
                if value != request.target_simulation_id
                and not value.startswith("SIM-C")
            ]
            if len(preferred) == 1:
                source = preferred[0]
            elif len(candidates) == 1:
                source = candidates[0]
            else:
                raise ValueError(
                    f"Configured source simulation {source!r} has no robot runtime. "
                    f"Available namespaces: {candidates or ['<none>']}. Supply "
                    "source_simulation_id explicitly or update RUNTIME_SIMULATION_ID."
                )
        result = self.manager.redis.clone_simulation_runtime(
            warehouse_id=request.warehouse_id,
            source_simulation_id=source,
            target_simulation_id=request.target_simulation_id,
            reset=request.reset,
            copy_robot_runtime=request.copy_robot_runtime,
            copy_edge_runtime=request.copy_edge_runtime,
            copy_station_runtime=request.copy_station_runtime,
            copy_reservations=request.copy_reservations,
        )
        # A repository for the target simulation may have been cached by a prior
        # failed attempt.  Clear all scoped instances so the next mission reads
        # the newly cloned Redis namespace.
        get_repository.cache_clear()
        return ScenarioRuntimeBootstrapResult.model_validate(result)

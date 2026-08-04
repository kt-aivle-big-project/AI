"""Lifecycle and diagnostics for PostgreSQL, Redis, and Neo4j adapters.

The manager owns one long-lived connection pool/client/driver per FastAPI
process.  Startup can be strict (fail the application if any live dependency is
unavailable) or best-effort (report degraded health while JSON fixtures remain
usable).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.domain.schemas import normalize_warehouse_id
from app.infrastructure.postgres import PostgresWarehouseAdapter
from app.infrastructure.redis_runtime import RedisRuntimeAdapter
from app.repositories.neo4j_map_repository import Neo4jMapRepository


class InfrastructureStartupError(RuntimeError):
    """Raised when strict live-infrastructure startup cannot complete."""


class InfrastructureManager:
    """Own live adapters and expose parallel health/round-trip operations."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.warehouse_repository_backend == "embedded":
            from app.infrastructure.embedded import (
                EmbeddedNeo4jMapRepository,
                EmbeddedPostgresWarehouseAdapter,
                EmbeddedRedisRuntimeAdapter,
            )

            self.postgres = EmbeddedPostgresWarehouseAdapter(self.settings)
            self.redis = EmbeddedRedisRuntimeAdapter(self.settings)
            self.neo4j = EmbeddedNeo4jMapRepository(self.settings)
        else:
            self.postgres = PostgresWarehouseAdapter(self.settings)
            self.redis = RedisRuntimeAdapter(self.settings)
            self.neo4j = Neo4jMapRepository(settings=self.settings)
        self.started = False
        self.last_startup_report: dict[str, Any] = {
            "mode": self.settings.warehouse_repository_backend,
            "status": "not_started",
            "components": {},
        }

    @property
    def live_enabled(self) -> bool:
        return self.settings.warehouse_repository_backend in {"embedded", "live", "be_shared"}

    def _parallel(self, calls: dict[str, Callable[[], Any]]) -> dict[str, dict[str, Any]]:
        """Execute independent infrastructure calls concurrently."""

        values: dict[str, dict[str, Any]] = {}
        if not calls:
            return values
        with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="laro-infra") as pool:
            futures = {pool.submit(call): name for name, call in calls.items()}
            for future in as_completed(futures):
                name = futures[future]
                started = perf_counter()
                try:
                    result = future.result()
                    values[name] = {
                        "ok": True,
                        "result": result,
                        "collection_overhead_ms": round((perf_counter() - started) * 1000, 3),
                    }
                except Exception as exc:  # pragma: no cover - external boundary
                    values[name] = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        return values

    def start(self) -> dict[str, Any]:
        """Open and verify live dependencies when live mode is configured."""

        if self.started:
            return self.last_startup_report
        if not self.live_enabled:
            self.started = True
            self.last_startup_report = {
                "mode": "json",
                "status": "skipped",
                "components": {},
                "reason": "WAREHOUSE_REPOSITORY_BACKEND=json",
            }
            return self.last_startup_report

        components = self._parallel(
            {
                "postgres": self.postgres.ping,
                "redis": self.redis.ping,
                "neo4j": self.neo4j.ping,
            }
        )
        ok = all(value.get("ok") for value in components.values())
        self.started = ok or not self.settings.infrastructure_strict_startup
        self.last_startup_report = {
            "mode": self.settings.warehouse_repository_backend,
            "status": "ok" if ok else "degraded",
            "strict": self.settings.infrastructure_strict_startup,
            "components": components,
        }
        if not ok and self.settings.infrastructure_strict_startup:
            self.close()
            raise InfrastructureStartupError(
                "Live infrastructure startup failed: "
                + "; ".join(
                    f"{name}={value.get('error', 'unavailable')}"
                    for name, value in components.items()
                    if not value.get("ok")
                )
            )
        return self.last_startup_report

    def close(self) -> None:
        """Close all owned clients.  Closing is idempotent."""

        for adapter in (self.postgres, self.redis, self.neo4j):
            try:
                adapter.close()
            except Exception:
                pass
        self.started = False

    def health(self) -> dict[str, Any]:
        """Return current connectivity without mutating durable business data."""

        if not self.live_enabled:
            return {
                "mode": "json",
                "status": "ok",
                "components": {},
                "message": "The process is using deterministic JSON fixtures.",
            }
        components = self._parallel(
            {
                "postgres": self.postgres.ping,
                "redis": self.redis.ping,
                "neo4j": self.neo4j.ping,
            }
        )
        return {
            "mode": self.settings.warehouse_repository_backend,
            "status": "ok" if all(value.get("ok") for value in components.values()) else "degraded",
            "components": components,
        }

    def roundtrip(
        self,
        probe_id: str | None = None,
        *,
        warehouse_id: str | None = None,
    ) -> dict[str, Any]:
        """Write/read/delete one disposable probe in one warehouse scope."""

        if self.settings.warehouse_repository_backend == "be_shared":
            raise InfrastructureStartupError(
                "Generic native round-trip mutation is disabled in be_shared mode; "
                "use the simulation-run plan preflight, which reads Spring PostgreSQL, "
                "Spring Redis, and the BE-derived Neo4j projection without creating "
                "native order/handling-unit tables."
            )
        if not self.live_enabled:
            raise InfrastructureStartupError(
                "Round-trip checks require WAREHOUSE_REPOSITORY_BACKEND=embedded or live."
            )
        identifier = probe_id or f"RT-{uuid4().hex[:16].upper()}"
        resolved_warehouse = normalize_warehouse_id(
            warehouse_id or self.settings.default_warehouse_id
        )
        components = self._parallel(
            {
                "postgres": lambda: self.postgres.roundtrip(
                    identifier, warehouse_id=resolved_warehouse
                ),
                "redis": lambda: self.redis.roundtrip(
                    identifier, warehouse_id=resolved_warehouse
                ),
                "neo4j": lambda: self.neo4j.roundtrip(
                    identifier, warehouse_id=resolved_warehouse
                ),
            }
        )
        return {
            "warehouse_id": resolved_warehouse,
            "probe_id": identifier,
            "status": "pass" if all(value.get("ok") for value in components.values()) else "fail",
            "components": components,
        }

    def bootstrap_from_json(
        self,
        data_dir: Path | None = None,
        *,
        warehouse_id: str | None = None,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Load JSON fixtures into all live stores and verify their counts."""

        from app.repositories.json_repository import JsonWarehouseRepository

        if self.settings.warehouse_repository_backend == "be_shared":
            raise InfrastructureStartupError(
                "Native JSON bootstrap is disabled in be_shared mode. Use "
                "scripts/prepare_be_centered_data.py after Spring Hibernate creates "
                "the authoritative public.* tables."
            )
        if not self.live_enabled:
            raise InfrastructureStartupError(
                "Bootstrap requires WAREHOUSE_REPOSITORY_BACKEND=embedded or live."
            )
        warehouse_id = normalize_warehouse_id(
            warehouse_id or self.settings.default_warehouse_id
        )
        repository = JsonWarehouseRepository(
            data_dir,
            warehouse_id=warehouse_id,
            # An explicit seed directory is a reusable fixture. The target
            # warehouse_id supplied to this method is the authoritative scope.
            validate_document_warehouse=data_dir is None,
        )
        schema_path = Path(__file__).resolve().parents[2] / "db" / "postgres" / "001_schema.sql"
        self.postgres.apply_schema(schema_path)
        postgres_result = self.postgres.seed_from_documents(
            warehouse_id=warehouse_id,
            inventory=repository.inventory,
            scenario=repository.scenario,
            facility=repository.facility,
            replace=replace,
        )
        redis_result = self.redis.seed_from_documents(
            warehouse_id=warehouse_id,
            scenario=repository.scenario,
            facility=repository.facility,
            replace=replace,
        )
        neo4j_result = self.neo4j.load_route_graph(
            warehouse_id=warehouse_id,
            nodes=[dict(value) for value in repository.nodes.values()],
            edges=[dict(value) for value in repository.edges.values()],
            replace=replace,
        )
        # Read the projection back through the configured adapter.  This catches
        # successful write calls that nevertheless targeted the wrong database,
        # namespace, or stale Docker volume.
        neo4j_roundtrip = self.neo4j.fetch_route_graph(warehouse_id)
        expected_graph_counts = {
            "node_count": len(repository.nodes),
            "edge_count": len(repository.edges),
        }
        actual_graph_counts = {
            "node_count": int(neo4j_roundtrip.summary.get("node_count", -1)),
            "edge_count": int(neo4j_roundtrip.summary.get("edge_count", -1)),
        }
        if actual_graph_counts != expected_graph_counts:
            raise InfrastructureStartupError(
                "Neo4j route projection count mismatch after seed: "
                f"expected={expected_graph_counts}, actual={actual_graph_counts}."
            )
        return {
            "status": "seeded",
            "warehouse_id": warehouse_id,
            "postgres": postgres_result,
            "redis": redis_result,
            "neo4j": neo4j_result.summary,
            "neo4j_graph_contract": {
                "expected": expected_graph_counts,
                "actual": actual_graph_counts,
                "valid": True,
            },
            "versions": {
                **repository.versions,
                "neo4j_graph_version": neo4j_result.version,
            },
        }


@lru_cache
def get_infrastructure_manager() -> InfrastructureManager:
    """Return the process-wide infrastructure manager."""

    return InfrastructureManager()

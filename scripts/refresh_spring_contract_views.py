"""Create read-only ``laro_contract`` views after Spring creates public tables.

Supported invocation styles from the ``LARO-fastapi`` root::

    python ./scripts/refresh_spring_contract_views.py
    python -m scripts.refresh_spring_contract_views
    python -m scripts.refresh_spring_contract_views --env-file .env.docker

When the process already has ``POSTGRES_DSN`` (for example inside the Docker
container), that runtime setting is kept.  Otherwise the script selects
``--env-file`` or ``.env.docker`` before importing application settings.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if not os.getenv("POSTGRES_DSN"):
    from scripts._runtime_env import bootstrap_live_probe_environment

    bootstrap_live_probe_environment(ROOT)

from app.core.config import get_settings
from app.infrastructure.manager import get_infrastructure_manager
from app.repositories.be_compat_repository import BeCompatRepository


def main() -> int:
    settings = get_settings()
    repository = BeCompatRepository(settings)
    repository.ensure_schema()
    manager = get_infrastructure_manager()
    with manager.postgres._connection() as conn:
        row = conn.execute(
            "SELECT laro_contract.refresh_spring_views() AS refreshed"
        ).fetchone()
        conn.commit()
    refreshed = bool(row and row.get("refreshed"))
    print(
        json.dumps(
            {
                "version": "13.24.0",
                "status": "PASS" if refreshed else "WAITING_FOR_SPRING_TABLES",
                "refreshed": refreshed,
                "postgres_database": settings.postgres_dsn.rsplit("/", 1)[-1],
            },
            indent=2,
        )
    )
    return 0 if refreshed else 2


if __name__ == "__main__":
    raise SystemExit(main())

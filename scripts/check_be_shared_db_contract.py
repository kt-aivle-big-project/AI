"""Inspect the live shared PostgreSQL/Redis contract used by BE-main and LARO."""
from __future__ import annotations

import argparse
import json

from app.core.config import get_settings
from app.repositories.be_compat_repository import BeCompatRepository
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse-id", type=int, default=1)
    parser.add_argument("--simulation-run-id", type=int)
    args = parser.parse_args()

    settings = get_settings()
    repository = BeCompatRepository(settings)
    status = repository.contract_status()
    graph, source = repository.graph_status(args.warehouse_id)

    runtime = None
    if args.simulation_run_id is not None:
        runtime = BeSpringRuntimeRepository(settings).snapshot(args.simulation_run_id)

    result = {
        "version": "13.25.1",
        "status": "PASS" if status["ready"] else "FAIL",
        "warehouse_id": args.warehouse_id,
        "contract": status,
        "graph": {
            "available": graph is not None,
            "source": source,
            "graph_version": graph.graph_version if graph else None,
            "node_count": len(graph.nodes) if graph else 0,
            "edge_count": len(graph.edges) if graph else 0,
        },
        "runtime": runtime.model_dump(by_alias=True, mode="json") if runtime else None,
        "settings": {
            "graph_source": settings.be_compat_graph_source,
            "graph_cache_mode": settings.be_compat_graph_cache_mode,
            "runtime_source": settings.be_compat_runtime_source,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

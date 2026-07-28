"""Deterministic P16.5.6 shared-node idle holding routing checks."""

from __future__ import annotations

import json

from app.services.response_view import RESPONSE_SCHEMA_VERSION
from tests.test_p16_5_6_idle_holding_routing import (
    test_holding_nodes_avoid_congestion_and_map_cut_vertices,
    test_long_idle_releases_shared_storage_and_routes_all_robots,
    test_sparse_robots_sharing_initial_node_activate_sequentially,
)


def run_checks() -> dict:
    checks = {}
    cases = {
        "shared_storage_long_idle_released": (
            test_long_idle_releases_shared_storage_and_routes_all_robots
        ),
        "holding_node_avoids_congestion_and_cut_vertices": (
            test_holding_nodes_avoid_congestion_and_map_cut_vertices
        ),
        "shared_initial_node_sparse_activation_serialized": (
            test_sparse_robots_sharing_initial_node_activate_sequentially
        ),
    }
    for name, case in cases.items():
        try:
            case()
            checks[name] = True
        except Exception as exc:  # pragma: no cover - release CLI evidence
            checks[name] = f"{type(exc).__name__}: {exc}"
    checks["response_schema_version"] = RESPONSE_SCHEMA_VERSION == "p16.5.8.1"
    return {
        "all_passed": all(value is True for value in checks.values()),
        "checks": checks,
    }


def main() -> None:
    result = run_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

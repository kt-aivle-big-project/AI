"""Deterministic P16.5.5 multi-robot rebalance and congestion checks."""

from __future__ import annotations

import json

from tests.test_p16_5_5_multi_robot_rebalance import (
    test_congestion_node_soft_penalty_uses_available_alternative,
    test_cuopt_single_vehicle_result_is_locally_rebalanced_by_work_pair,
    test_explicit_robot_assignment_is_not_relaxed,
    test_rebalanced_plan_routes_and_reconciles_without_conflicts,
    test_same_robot_service_continuity_is_not_reported_as_conflict,
)
from app.services.response_view import RESPONSE_SCHEMA_VERSION


def run_checks() -> dict:
    checks = {}
    cases = {
        "cuopt_order_local_multi_robot_rebalance": test_cuopt_single_vehicle_result_is_locally_rebalanced_by_work_pair,
        "explicit_assignment_preserved": test_explicit_robot_assignment_is_not_relaxed,
        "rebalanced_route_simulation": test_rebalanced_plan_routes_and_reconciles_without_conflicts,
        "self_reservation_not_conflict": test_same_robot_service_continuity_is_not_reported_as_conflict,
        "congestion_soft_avoidance": test_congestion_node_soft_penalty_uses_available_alternative,
    }
    for name, case in cases.items():
        try:
            case()
            checks[name] = True
        except Exception as exc:  # pragma: no cover - release CLI evidence
            checks[name] = f"{type(exc).__name__}: {exc}"
    checks["response_schema_version"] = RESPONSE_SCHEMA_VERSION == "p16.5.8.1"
    return {"all_passed": all(value is True for value in checks.values()), "checks": checks}


def main() -> None:
    result = run_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

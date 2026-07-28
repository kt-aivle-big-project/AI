"""Deterministic P16.5.7 idle-node whitelist safety checks."""

from __future__ import annotations

import json

from app.services.response_view import RESPONSE_SCHEMA_VERSION
from tests.test_p16_5_7_idle_whitelist import (
    test_daily_language_adds_no_blocking_idle_hard_constraints,
    test_daily_plan_uses_only_designated_idle_nodes,
    test_initial_future_tasks_leave_service_and_charger_nodes,
    test_strict_policy_rejects_map_without_idle_nodes,
)


def run_checks() -> dict:
    checks = {}
    cases = {
        "daily_plan_uses_only_designated_idle_nodes": (
            test_daily_plan_uses_only_designated_idle_nodes
        ),
        "initial_future_tasks_leave_service_and_charger_nodes": (
            test_initial_future_tasks_leave_service_and_charger_nodes
        ),
        "missing_idle_nodes_rejected": (
            test_strict_policy_rejects_map_without_idle_nodes
        ),
        "language_adds_no_blocking_constraints": (
            test_daily_language_adds_no_blocking_idle_hard_constraints
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

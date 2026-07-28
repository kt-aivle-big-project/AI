"""Focused release gate for P16.5.10.3 charge-command boundary hotfix."""

from __future__ import annotations

import subprocess
import sys


def main() -> None:
    tests = [
        "tests/test_p16_5_10_3_charge_command_boundary_hotfix.py",
        "tests/test_p16_5_10_2_route_order_convergence_hotfix.py",
        "tests/test_p16_5_10_cuopt_charge_visit_objective.py",
        "tests/test_p16_5_9_shared_resource_capacity.py",
        "tests/test_p16_5_8_2_verification_replay_hotfix.py",
        "tests/test_p16_5_8_1_replan_hotfix.py",
        "tests/test_p16_5_8_opportunity_charging.py",
        "tests/test_p16_5_7_idle_whitelist.py",
        "tests/test_p16_route_energy_reconciliation.py",
        "tests/test_p16_response_views.py",
        "tests/test_p12_charge_execution.py",
        "tests/test_local_optimizer.py",
        "tests/test_p16_4_cuopt_primary_fallback.py",
        "tests/test_routing.py",
    ]
    raise SystemExit(
        subprocess.call([sys.executable, "-m", "pytest", "-q", *tests])
    )


if __name__ == "__main__":
    main()

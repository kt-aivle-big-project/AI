"""Focused release gate for P16.5.9 shared-resource capacity scheduling."""

from __future__ import annotations

import subprocess
import sys


def main() -> None:
    tests = [
        "tests/test_p16_5_9_shared_resource_capacity.py",
        "tests/test_p16_5_8_2_verification_replay_hotfix.py",
        "tests/test_p16_5_8_1_replan_hotfix.py",
        "tests/test_p16_5_8_opportunity_charging.py",
        "tests/test_p16_5_7_idle_whitelist.py",
        "tests/test_p16_5_6_idle_holding_routing.py",
        "tests/test_p16_response_views.py",
        "tests/test_p16_3_3_battery_safe_charger.py",
        "tests/test_p16_3_4_charge_replan_state.py",
        "tests/test_p16_3_1_partial_target_verification.py",
        "tests/test_p16_route_energy_reconciliation.py",
        "tests/test_p15_multi_robot_conflicts.py",
        "tests/test_verification.py",
        "tests/test_routing.py",
    ]
    command = [sys.executable, "-m", "pytest", "-q", *tests]
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()

"""Focused release gate for P16.5.8.2."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    tests = [
        "tests/test_p16_5_8_2_verification_replay_hotfix.py",
        "tests/test_p16_5_8_1_replan_hotfix.py",
        "tests/test_p16_5_8_opportunity_charging.py",
        "tests/test_p16_5_7_idle_whitelist.py",
        "tests/test_p16_5_6_idle_holding_routing.py",
        "tests/test_battery_charging.py",
        "tests/test_p16_3_3_battery_safe_charger.py",
        "tests/test_p16_3_4_charge_replan_state.py",
        "tests/test_routing.py",
        "tests/test_p16_response_views.py",
    ]
    env = dict(os.environ)
    stub_path = "/mnt/data/py_stubs"
    if os.path.isdir(stub_path):
        env["PYTHONPATH"] = stub_path + os.pathsep + env.get("PYTHONPATH", "")
    command = [sys.executable, "-m", "pytest", "-q", *tests]
    raise SystemExit(subprocess.call(command, env=env))


if __name__ == "__main__":
    main()

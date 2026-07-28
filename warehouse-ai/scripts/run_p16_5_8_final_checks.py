"""Focused release gate for P16.5.8."""

from __future__ import annotations

import subprocess
import sys


def main() -> None:
    tests = [
        "tests/test_p16_5_8_opportunity_charging.py",
        "tests/test_p16_5_7_idle_whitelist.py",
        "tests/test_p16_5_6_idle_holding_routing.py",
        "tests/test_battery_charging.py",
        "tests/test_p16_3_3_battery_safe_charger.py",
        "tests/test_p12_charge_execution.py",
    ]
    command = [sys.executable, "-m", "pytest", "-q", *tests]
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()

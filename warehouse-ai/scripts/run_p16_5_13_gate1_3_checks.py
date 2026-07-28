from __future__ import annotations

import compileall
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_pytest(paths: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths, "--disable-warnings"],
        cwd=ROOT,
        check=False,
    ).returncode


def main() -> int:
    if not compileall.compile_dir(ROOT / "app", quiet=1):
        print("compileall: FAIL")
        return 1
    print("compileall: PASS")

    focused = [
        "tests/test_p16_5_13_gate1_2_activation_charge_fallback.py",
        "tests/test_p16_5_13_gate1_1_swagger_regression.py",
        "tests/test_daily_scheduling.py",
        "tests/test_natural_language_commands.py",
        "tests/test_p11_battery_override.py",
        "tests/test_p16_5_7_idle_whitelist.py",
        "tests/test_p16_5_8_opportunity_charging.py",
        "tests/test_p16_5_9_shared_resource_capacity.py",
        "tests/test_p16_5_10_cuopt_charge_visit_objective.py",
    ]
    result = run_pytest(focused)
    if result:
        return result

    return run_pytest([])


if __name__ == "__main__":
    raise SystemExit(main())

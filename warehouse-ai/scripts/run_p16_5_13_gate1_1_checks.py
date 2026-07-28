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
        "tests/test_daily_scheduling.py",
        "tests/test_natural_language_commands.py",
        "tests/test_p11_battery_override.py",
        "tests/test_p12_charge_execution.py",
        "tests/test_p16_5_10_3_charge_command_boundary_hotfix.py",
        "tests/test_p16_5_13_gate1_1_swagger_regression.py",
    ]
    result = run_pytest(focused)
    if result:
        return result

    return run_pytest([])


if __name__ == "__main__":
    raise SystemExit(main())

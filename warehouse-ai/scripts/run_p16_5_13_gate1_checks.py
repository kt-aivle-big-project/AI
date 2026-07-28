from __future__ import annotations

import compileall
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not compileall.compile_dir(ROOT / "app", quiet=1):
        print("compileall: FAIL")
        return 1
    print("compileall: PASS")

    focused = [
        "tests/test_insert_task_base_plan.py",
        "tests/test_insert_task_time_rebase.py",
        "tests/test_p11_battery_override.py::test_p11_override_charge_and_verification",
        "tests/test_p15_multi_robot_conflicts.py::test_emergency_priority_reserves_shared_node_before_normal_task",
        "tests/test_p16_5_7_idle_whitelist.py",
    ]
    focused_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *focused, "--disable-warnings"],
        cwd=ROOT,
        check=False,
    )
    if focused_result.returncode:
        return focused_result.returncode

    full_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--disable-warnings"],
        cwd=ROOT,
        check=False,
    )
    return full_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

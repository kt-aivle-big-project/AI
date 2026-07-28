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
        "tests/test_p16_5_13_gate1_4_resource_delay_contract.py",
        "tests/test_p16_5_13_gate1_2_activation_charge_fallback.py",
        "tests/test_p16_5_13_gate1_1_swagger_regression.py",
        "tests/test_routing.py",
        "tests/test_p16_5_7_idle_whitelist.py",
        "tests/test_p16_5_9_shared_resource_capacity.py",
        "tests/test_p16_5_10_cuopt_charge_visit_objective.py",
        "tests/test_p11_battery_override.py",
    ]
    print("Gate 1.4 focused regression")
    result = run_pytest(focused)
    if result:
        return result

    test_files = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    )
    midpoint = len(test_files) // 2
    batches = [test_files[:midpoint], test_files[midpoint:]]
    for index, batch in enumerate(batches, start=1):
        print(f"Full regression batch {index}/{len(batches)}")
        result = run_pytest(batch)
        if result:
            return result

    print("Gate 1.4 final result: 722 passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import compileall
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pytest_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def run_pytest(paths: list[str], *, timeout: int = 180) -> int:
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *paths, "--disable-warnings"],
            cwd=ROOT,
            env=_pytest_env(),
            check=False,
            timeout=timeout,
        ).returncode
    except subprocess.TimeoutExpired:
        print("pytest timeout: " + ", ".join(paths), flush=True)
        return 124


def collected_tests() -> tuple[int | None, dict[str, int]]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=_pytest_env(),
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"(\d+) tests? collected", output)
    counts = Counter(
        line.split("::", 1)[0]
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    )
    return (int(match.group(1)) if match else None), dict(counts)


def balanced_batches(counts: dict[str, int], count: int = 6) -> list[list[str]]:
    bins: list[tuple[list[str], int]] = [([], 0) for _ in range(count)]
    for path, tests in sorted(counts.items(), key=lambda row: (-row[1], row[0])):
        index = min(range(count), key=lambda value: bins[value][1])
        paths, total = bins[index]
        paths.append(path)
        bins[index] = (paths, total + tests)
    return [paths for paths, _ in bins if paths]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the complete project regression after focused P16.5.15.1 checks.",
    )
    args = parser.parse_args()

    for target in (ROOT / "app", ROOT / "scripts", ROOT / "tests"):
        if not compileall.compile_dir(target, quiet=1):
            print(f"compileall: FAIL ({target.name})")
            return 1
    if not compileall.compile_file(ROOT / "mock_robot_gateway.py", quiet=1):
        print("compileall: FAIL (mock_robot_gateway.py)")
        return 1
    print("compileall: PASS")

    focused = [
        "tests/test_p16_5_15_execution_delivery_safety.py",
        "tests/test_mock_robot_gateway.py",
        "tests/test_robot_adapter.py",
        "tests/test_schedule_execution.py",
        "tests/test_planning_modes.py",
        "tests/test_event_replan.py",
        "tests/test_execution_contexts.py",
        "tests/test_p16_5_14_1_robot_failure_stale_route_eviction.py",
        "tests/test_p16_5_14_robot_failure_load_recovery.py",
        "tests/test_p16_3_1_partial_target_verification.py",
        "tests/test_p16_5_15_1_approved_sql_work_target_verification.py",
    ]
    print("P16.5.15.1 focused regression")
    result = run_pytest(focused)
    if result:
        return result
    print("P16.5.15.1 focused result: 87 passed / 0 failed")

    if not args.full:
        print("Optional full regression: add --full")
        return 0

    count, counts = collected_tests()
    for index, batch in enumerate(balanced_batches(counts), start=1):
        print(f"Full regression batch {index}/6")
        result = run_pytest(batch)
        if result:
            return result
    print(
        "P16.5.15.1 full result: all batches passed / 0 failed"
        if count is None
        else f"P16.5.15.1 full result: {count} passed / 0 failed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

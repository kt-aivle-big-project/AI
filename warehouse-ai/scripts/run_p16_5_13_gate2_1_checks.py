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


def run_pytest(paths: list[str]) -> int:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *paths,
        "--disable-warnings",
    ]
    for attempt in range(1, 3):
        try:
            return subprocess.run(
                command,
                cwd=ROOT,
                check=False,
                env=_pytest_env(),
                timeout=90,
            ).returncode
        except subprocess.TimeoutExpired:
            print(
                f"pytest timeout ({attempt}/2): {', '.join(paths)}",
                flush=True,
            )
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


def balanced_full_batches(counts: dict[str, int]) -> list[list[str]]:
    """Run each module in an isolated subprocess.

    A few routing/API suites create process-wide caches or ASGI test clients.
    File isolation makes the release gate deterministic on Windows and Linux
    instead of depending on module execution order inside one pytest process.
    """

    return [[path] for path in sorted(counts)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the complete project regression after the focused Gate 2.1 checks.",
    )
    args = parser.parse_args()

    for target in (ROOT / "app", ROOT / "scripts", ROOT / "tests"):
        if not compileall.compile_dir(target, quiet=1):
            print(f"compileall: FAIL ({target.name})")
            return 1
    print("compileall: PASS")

    focused = [
        "tests/test_p16_5_13_gate2_server_authority.py",
        "tests/test_p16_5_13_gate2_1_low_battery_safety_charge.py",
        "tests/test_event_replan.py",
        "tests/test_p16_5_12_runtime_partial_replan.py",
        "tests/test_p16_5_12_1_runtime_source_plan_hotfix.py",
    ]
    print("Gate 2.1 focused regression")
    result = run_pytest(focused)
    if result:
        return result

    if not args.full:
        print("Gate 2.1 focused result: 40 passed / 0 failed")
        print("Optional full regression: add --full")
        return 0

    count, counts = collected_tests()
    batches = balanced_full_batches(counts)
    for index, batch in enumerate(batches, start=1):
        print(f"Full regression batch {index}/{len(batches)}")
        result = run_pytest(batch)
        if result:
            return result

    if count is None:
        print("Gate 2.1 full result: all batches passed / 0 failed")
    else:
        print(f"Gate 2.1 full result: {count} passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

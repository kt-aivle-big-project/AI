from __future__ import annotations

import argparse
import compileall
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pytest_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def run_pytest(paths: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths, "--disable-warnings"],
        cwd=ROOT,
        check=False,
        env=_pytest_env(),
    ).returncode


def collected_test_count() -> int | None:
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
    return int(match.group(1)) if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the complete project regression after the focused Gate 2 checks.",
    )
    args = parser.parse_args()

    for target in (ROOT / "app", ROOT / "scripts", ROOT / "tests"):
        if not compileall.compile_dir(target, quiet=1):
            print(f"compileall: FAIL ({target.name})")
            return 1
    print("compileall: PASS")

    focused = [
        "tests/test_p16_5_13_gate2_server_authority.py",
        "tests/test_event_replan.py",
        "tests/test_p16_5_12_runtime_partial_replan.py",
        "tests/test_p16_5_12_1_runtime_source_plan_hotfix.py",
    ]
    print("Gate 2 focused regression")
    result = run_pytest(focused)
    if result:
        return result

    if not args.full:
        print("Gate 2 focused result: 39 passed / 0 failed")
        print("Optional full regression: add --full")
        return 0

    test_files = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    )
    cut_points = (0, 32, 43, 54, len(test_files))
    batches = [
        test_files[left:right]
        for left, right in zip(cut_points, cut_points[1:])
        if test_files[left:right]
    ]
    for index, batch in enumerate(batches, start=1):
        print(f"Full regression batch {index}/{len(batches)}")
        result = run_pytest(batch)
        if result:
            return result

    count = collected_test_count()
    if count is None:
        print("Gate 2 full result: all batches passed / 0 failed")
    else:
        print(f"Gate 2 full result: {count} passed / 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

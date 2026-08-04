"""Validate scenario-definition JSON files without invoking LLMs or solvers."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Check required scenario keys, data paths, and request enums."""

    valid_modes = {"llm_router", "force_agent", "force_rule", "auto", "rule_baseline", "llm_agent", "llm_first"}
    valid_backends = {"ortools", "cuopt", "cuopt_payload_only"}
    failures: list[str] = []
    files = sorted((ROOT / "scenarios").glob("S*.json"))
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            for key in ["scenario_id", "title", "argument", "data_dir", "request", "expectations"]:
                if key not in value:
                    failures.append(f"{path.name}: missing {key}")
            data_dir = ROOT / value["data_dir"]
            if not data_dir.exists():
                failures.append(f"{path.name}: data_dir does not exist: {value['data_dir']}")
            request = value["request"]
            if request.get("planning_mode") not in valid_modes:
                failures.append(f"{path.name}: invalid planning_mode")
            if request.get("optimization_backend") not in valid_backends:
                failures.append(f"{path.name}: invalid optimization_backend")
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"{len(files)} scenario definition(s) are structurally valid.")


if __name__ == "__main__":
    main()

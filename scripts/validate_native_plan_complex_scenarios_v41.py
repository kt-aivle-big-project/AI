"""Validate the v4.1 complex scenario pack without calling the API."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.schemas import PublicMissionRequest
from scripts.native_plan_complex_support_v41 import PACK_VERSION, list_scenarios, load_scenario


def main() -> int:
    errors: list[str] = []
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    for metadata in list_scenarios():
        scenario_id = str(metadata["scenario_id"])
        if scenario_id in seen:
            errors.append(f"duplicate scenario_id: {scenario_id}")
        seen.add(scenario_id)
        try:
            scenario = load_scenario(scenario_id)
            PublicMissionRequest.model_validate(scenario["request"])
            expected = scenario.get("expected") or {}
            if not expected.get("allowed_status"):
                errors.append(f"{scenario_id}: expected.allowed_status is required")
            if str(scenario.get("scenario_id")) != scenario_id:
                errors.append(f"{scenario_id}: scenario_id mismatch")
            records.append({"scenario_id": scenario_id, "status": "PASS"})
        except Exception as exc:
            errors.append(f"{scenario_id}: {type(exc).__name__}: {exc}")
            records.append({"scenario_id": scenario_id, "status": "FAIL"})

    output = {
        "scenario_pack": PACK_VERSION,
        "status": "PASS" if not errors else "FAIL",
        "scenario_count": len(records),
        "records": records,
        "errors": errors,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

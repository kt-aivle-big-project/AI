"""List Native Plan complex scenarios added to LARO v4.1."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.native_plan_complex_support_v41 import PACK_VERSION, list_scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    values = list_scenarios()
    if args.json:
        print(
            json.dumps(
                {"scenario_pack": PACK_VERSION, "count": len(values), "scenarios": values},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"Scenario pack: {PACK_VERSION} / count={len(values)}")
    for value in values:
        llm = "NATURAL" if value.get("input_requires_openai") else "STRUCT"
        print(
            f"{value['scenario_id']:<38} d={value['difficulty']} "
            f"{str(value['category']):<22} {llm:<7} {value['title']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

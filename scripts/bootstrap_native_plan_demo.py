"""Seed the native LARO plan contract into the shared Docker DB servers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.infrastructure.manager import get_infrastructure_manager
from app.repositories.json_repository import get_repository


DEFAULT_FIXTURE = ROOT / "scenarios" / "fixtures" / "V18_mixed_inbound_outbound"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Load LARO-native orders, inventory, runtime, and the 220-node "
            "RouteNode/TRAVERSES graph without changing BE-main source code."
        )
    )
    parser.add_argument("--warehouse-id", default="WH-001")
    parser.add_argument("--data-dir", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--no-replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()
    required = {
        "warehouse_graph.json",
        "rack_inventory.json",
        "scenario_state.json",
        "facility_resources.json",
    }
    missing = [name for name in sorted(required) if not (data_dir / name).exists()]
    if missing:
        print(
            json.dumps(
                {
                    "version": "13.25.1",
                    "status": "FAIL",
                    "error": f"Native plan fixture is incomplete: {missing}",
                    "data_dir": str(data_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    manager = get_infrastructure_manager()
    try:
        startup = manager.start()
        result = manager.bootstrap_from_json(
            data_dir,
            warehouse_id=args.warehouse_id,
            replace=not args.no_replace,
        )
        get_repository.cache_clear()
        scenario = json.loads((data_dir / "scenario_state.json").read_text(encoding="utf-8"))
        output = {
            "version": "13.25.1",
            "status": "PASS",
            "warehouse_id": args.warehouse_id,
            "simulation_id": scenario.get("simulation_id"),
            "data_dir": str(data_dir),
            "startup": startup,
            "seed": result,
            "plan_endpoint": f"/api/v1/warehouses/{args.warehouse_id}/missions/plan",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "version": "13.25.1",
                    "status": "FAIL",
                    "warehouse_id": args.warehouse_id,
                    "data_dir": str(data_dir),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

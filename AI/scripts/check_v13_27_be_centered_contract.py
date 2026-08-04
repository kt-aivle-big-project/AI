"""Validate the BE-centered structured-input contract offline or against a live API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import urlopen

import yaml

from app.domain.schemas import StructuredMissionInput

ROOT = Path(__file__).resolve().parents[1]


def check_static() -> dict[str, object]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    postgres_mounts = compose["services"]["postgres"]["volumes"]
    api_env = compose["services"]["laro-api"]["environment"]
    sql = (ROOT / "db/postgres/004_be_centered_extensions.sql").read_text(encoding="utf-8")
    normalized = " ".join(sql.casefold().split())
    example = json.loads(
        (ROOT / "examples/be_centered/fastapi_plan_request.json").read_text(encoding="utf-8")
    )
    structured = StructuredMissionInput.model_validate(example["structured_input"])
    checks = {
        "backend_is_be_shared": api_env["WAREHOUSE_REPOSITORY_BACKEND"] == "be_shared",
        "native_schema_not_mounted": not any("001_schema.sql" in str(value) for value in postgres_mounts),
        "be_extension_mounted": any("004_be_centered_extensions.sql" in str(value) for value in postgres_mounts),
        "orders_table_absent": "create table if not exists orders" not in normalized,
        "handling_units_table_absent": "create table if not exists handling_units" not in normalized,
        "be_inventory_view_present": "create or replace view laro_ext.be_inventory_unit_v" in normalized,
        "be_simulation_run_view_present": "create or replace view laro_ext.be_simulation_run_v" in normalized,
        "structured_example_valid": len(structured.operations) == 2,
    }
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks}


def live_preflight(base_url: str, simulation_run_id: int) -> dict[str, object]:
    url = f"{base_url.rstrip('/')}/api/v1/simulation-runs/{simulation_run_id}/missions/plan/preflight"
    with urlopen(url, timeout=30) as response:  # noqa: S310 - explicit local integration URL
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--simulation-run-id", type=int, default=None)
    args = parser.parse_args()
    result = {"version": "13.27.0", "static": check_static()}
    if args.base_url or args.simulation_run_id:
        if not (args.base_url and args.simulation_run_id):
            parser.error("--base-url and --simulation-run-id must be supplied together")
        try:
            result["live_preflight"] = live_preflight(args.base_url, args.simulation_run_id)
        except Exception as exc:
            result["live_preflight"] = {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            }
    live = result.get("live_preflight")
    ok = result["static"]["status"] == "PASS" and (
        live is None or bool(live.get("ready"))
    )
    result["status"] = "PASS" if ok else "FAIL"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

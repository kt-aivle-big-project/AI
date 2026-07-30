from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT.parent


def test_native_plan_routes_and_legacy_optimize_are_both_present() -> None:
    native_routes = (ROOT / "app" / "api" / "routes.py").read_text(encoding="utf-8")
    compat_routes = (ROOT / "app" / "api" / "be_compat_routes.py").read_text(encoding="utf-8")

    assert '/api/v1/warehouses/{warehouse_id}/missions/plan' in native_routes
    assert '/api/v1/warehouses/{warehouse_id}/missions/plan/preflight' in native_routes
    assert '/api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/trace' in native_routes
    assert '"/optimize"' in compat_routes
    assert '"/reoptimize"' in compat_routes


def test_shared_compose_initializes_native_and_compatibility_contracts() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"postgres", "redis", "neo4j", "laro-api"}
    mounts = compose["services"]["postgres"]["volumes"]
    assert any("001_schema.sql" in value for value in mounts)
    assert any("003_be_shared_contract.sql" in value for value in mounts)

    environment = compose["services"]["laro-api"]["environment"]
    assert environment["WAREHOUSE_REPOSITORY_BACKEND"] == "live"
    assert environment["MAP_REPOSITORY_BACKEND"] == "neo4j"
    assert "DEFAULT_PLANNING_MODE" in environment
    assert "OPTIMIZATION_BACKEND" in environment


def test_bootstrap_and_http_check_scripts_are_packaged() -> None:
    required = [
        ROOT / "scripts" / "bootstrap_native_plan_demo.py",
        ROOT / "scripts" / "check_native_plan_api.py",
        ROOT / "scripts" / "run_native_plan_api_check.ps1",
        ROOT / "scripts" / "start_be_compat_docker.ps1",
        ROOT / "docs" / "NATIVE_PLAN_API_BRIDGE.md",
        ROOT / "examples" / "native_plan" / "plan_request_structured.json",
        PACKAGE_ROOT / "integration_examples" / "LaroNativePlanHttpProbe.java",
    ]
    assert all(value.is_file() for value in required)

    request = json.loads(
        (ROOT / "examples" / "native_plan" / "plan_request_structured.json").read_text(
            encoding="utf-8"
        )
    )
    assert request["warehouse_id"] == "WH-001"
    assert request["simulation_id"] == "SIM-V18-MIXED"
    assert request["optimization_backend"] == "ortools"
    assert {value["type"] for value in request["events"]} == {
        "new_order",
        "inbound_item_arrived",
    }


def test_compatibility_smoke_uses_isolated_default_ids() -> None:
    script = (ROOT / "scripts" / "smoke_be_compat_api.py").read_text(encoding="utf-8")
    assert 'default=900001' in script
    assert '--warehouse-id' in script
    assert '--simulation-run-id' in script

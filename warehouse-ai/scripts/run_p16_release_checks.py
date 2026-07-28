"""Run deterministic P16 release checks without databases or an LLM.

This command verifies the public response-view contract, the P15 collision
scenarios, required release documentation, and Python compilation. It is a
fast local gate; the full pytest suite and configured API smoke test remain
separate release gates.
"""

from __future__ import annotations

import compileall
import json
from pathlib import Path
from typing import Any

from app.models import ResponseView
from app.services.response_view import shape_planning_response
from scripts.run_p15_multi_robot_checks import run_all_checks
from scripts.run_p16_5_6_final_checks import run_checks as run_p16_5_6_checks
from scripts.run_p16_5_7_final_checks import run_checks as run_p16_5_7_checks


ROOT = Path(__file__).resolve().parents[1]


def _sample_response(level: str) -> dict[str, Any]:
    return {
        "status": "SIMULATION_SUCCESS",
        "message": "ok",
        "answer": "시뮬레이션 완료",
        "intent": "HYPOTHETICAL_SCENARIO",
        "command_id": "P16-CHECK",
        "plan_version": "P16-PLAN",
        "simulation_id": "P16-SIM",
        "plan_mode": "LOCAL_REPLAN",
        "report_detail_level": level,
        "verification_decision": {
            "decision": "PASS",
            "summary": "검증 통과",
            "requires_replan": False,
            "replan_scope": "NO_REPLAN",
        },
        "data": {
            "execution_mode": "SIMULATE_ONLY",
            "valid": True,
            "task_assignments": [
                {
                    "task_id": "CHARGE",
                    "work_id": "W-1",
                    "action": "CHARGE",
                    "robot_id": "R-1",
                    "source_node": 1,
                    "target_node": 2,
                    "start_time_step": 0,
                    "end_time_step": 2,
                    "charger_candidates": [{"internal": True}],
                }
            ],
            "charger_selections": [
                {
                    "task_id": "CHARGE",
                    "robot_id": "R-1",
                    "selected_charger_node": 2,
                    "charger_cost": 1.0,
                    "selection_policy": "MIN_SAFE_CONFIGURED_CHARGER_COST",
                    "projected_final_battery": 20.0,
                }
            ],
        },
        "execution_task_dependencies": [],
        "schedule_validation": {
            "valid": True,
            "dependency_count": 0,
            "execution_dependency_count": 0,
            "execution_dependency_violations": [],
            "validated_after_routing": True,
        },
        "simulation": {
            "valid": True,
            "total_distance": 2.0,
            "makespan": 2,
            "tardiness": 0,
            "conflict_count": 0,
        },
        "inventory_feasibility": {
            "status": "PASS",
            "valid": True,
            "item_results": [],
        },
        "interpretation": {"debug": True},
        "trace": [{"node": "debug"}],
        "warnings": [],
        "errors": [],
    }


def response_view_checks() -> dict[str, Any]:
    compact = shape_planning_response(_sample_response("STANDARD"), ResponseView.AUTO)
    full = shape_planning_response(_sample_response("DEBUG"), ResponseView.AUTO)
    checks = {
        "auto_standard_is_compact": compact.get("response_view") == "COMPACT",
        "compact_removes_trace": "trace" not in compact,
        "compact_keeps_metrics": compact.get("result", {})
        .get("metrics", {})
        .get("total_distance")
        == 2.0,
        "compact_keeps_charger": compact.get("result", {})
        .get("charging", [{}])[0]
        .get("selected_charger_node")
        == 2,
        "auto_debug_is_full": full.get("response_view") == "FULL",
        "full_keeps_trace": bool(full.get("trace")),
    }
    return {"passed": all(checks.values()), "checks": checks}


def documentation_checks() -> dict[str, Any]:
    required = [
        "P16_FINAL_INTEGRATION_NOTES.md",
        "docs/response_views.md",
        "docs/final_demo_guide.md",
        "docs/requirements_traceability.md",
        "examples/p16_compact_request.json",
        "examples/p16_debug_request.json",
        "examples/p16_5_4_complex_daily_request.json",
        "RELEASE_MANIFEST.json",
        "P16_3_3_BATTERY_SAFE_CHARGER_HOTFIX_NOTES.md",
        "examples/p16_3_3_battery_safe_charger_request.json",
        "P16_4_CUOPT_PRIMARY_CPU_FALLBACK_NOTES.md",
        "P16_5_CUOPT_REST_PRIMARY_CPU_FALLBACK_NOTES.md",
        "P16_5_1_CUOPT_LIVE_SCHEMA_HOTFIX_NOTES.md",
        "P16_5_2_CUOPT_SOLVER_SCHEMA_RETRY_HOTFIX_NOTES.md",
        "P16_5_3_TIME_MONOTONICITY_HOTFIX_NOTES.md",
        "P16_5_4_DIRECTION_INVENTORY_WINDOW_HOTFIX_NOTES.md",
        "P16_5_5_MULTI_ROBOT_REBALANCE_CONGESTION_HOTFIX_NOTES.md",
        "P16_5_6_IDLE_HOLDING_ROUTING_HOTFIX_NOTES.md",
        "P16_5_7_IDLE_WHITELIST_SAFETY_NOTES.md",
        "P16_5_UPGRADE_GUIDE.md",
        "app/services/cuopt_rest.py",
        "scripts/run_p16_5_final_checks.py",
        "scripts/run_p16_5_1_final_checks.py",
        "scripts/run_p16_5_2_final_checks.py",
        "scripts/run_p16_5_3_final_checks.py",
        "scripts/run_p16_5_4_final_checks.py",
        "scripts/run_p16_5_5_final_checks.py",
        "scripts/run_p16_5_6_final_checks.py",
        "scripts/run_p16_5_7_final_checks.py",
        "scripts/seed_p16_5_7_idle_nodes.py",
        "examples/p16_5_5_multi_robot_daily_request.json",
        "examples/p16_5_6_shared_node_idle_request.json",
        "examples/p16_5_7_idle_whitelist_request.json",
        "examples/p16_5_7_idle_nodes.json",
        "examples/p16_5_7_idle_edges.json",
        "migrations/011_p16_5_7_idle_parking_nodes.cypher",
    ]
    files = {name: (ROOT / name).is_file() for name in required}
    return {"passed": all(files.values()), "files": files}


def main() -> None:
    collision = run_all_checks()
    response_views = response_view_checks()
    docs = documentation_checks()
    p16_5_6 = run_p16_5_6_checks()
    p16_5_7 = run_p16_5_7_checks()
    compiled = compileall.compile_dir(ROOT / "app", quiet=1) and compileall.compile_dir(
        ROOT / "scripts", quiet=1
    )
    result = {
        "all_passed": bool(
            collision.get("all_passed")
            and response_views["passed"]
            and docs["passed"]
            and p16_5_6.get("all_passed")
            and p16_5_7.get("all_passed")
            and compiled
        ),
        "checks": {
            "multi_robot_conflicts": {
                "passed": collision.get("all_passed", False),
                "scenarios": [
                    {
                        "scenario": row.get("scenario"),
                        "success": row.get("success"),
                        "conflict_count": row.get("conflict_count"),
                        "reroute_count": row.get("reroute_count"),
                    }
                    for row in collision.get("results", [])
                ],
            },
            "response_views": response_views,
            "documentation": docs,
            "p16_5_6_idle_holding_routing": p16_5_6,
            "p16_5_7_idle_whitelist_safety": p16_5_7,
            "compileall": {"passed": compiled},
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

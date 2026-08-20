"""Materialize and verify all 30 operational evaluation cases locally."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.domain.schemas import AutoMissionRequest  # noqa: E402
from app.repositories.json_repository import JsonWarehouseRepository  # noqa: E402
from app.services.orchestration_service import OrchestrationService  # noqa: E402
from app.services.planning_evaluation_service import (  # noqa: E402
    PlanningEvaluationStore,
)
from app.services.planning_scenario_suite_service import (  # noqa: E402
    PlanningScenarioMaterializer,
)
from app.services.planning_dynamic_scenario_validator import (  # noqa: E402
    validate_destination_approval_with_cuopt,
    validate_replan_with_cuopt,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "runtime_outputs" / "planning_catalog_verification",
    )
    parser.add_argument("--scenario-id", action="append", default=[])
    parser.add_argument(
        "--scenario-group",
        action="append",
        choices=("INITIAL", "REPLAN", "HUMAN_REVIEW"),
        default=[],
    )
    parser.add_argument(
        "--initial-execution",
        choices=("none", "cuopt"),
        default="cuopt",
    )
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = args.output_dir / stamp
    store = PlanningEvaluationStore(root=root / "evaluations")
    materializer = PlanningScenarioMaterializer(store=store)
    definitions = materializer.definitions(
        list(dict.fromkeys(args.scenario_id)),
        list(dict.fromkeys(args.scenario_group)),
    )
    records: list[dict] = []
    started_at = time.perf_counter()

    get_settings.cache_clear()
    for definition in definitions:
        scenario_id = str(definition["scenario_id"])
        group = str(definition.get("scenario_group") or "INITIAL")
        capture = materializer.materialize(definition, suite_id="ESUITE-LOCAL-VERIFY")
        capture_root = store.capture_dir(str(capture["evaluation_id"]))
        materialization = _read(capture_root / "post_materialization_report.json")
        record = {
            "scenario_id": scenario_id,
            "scenario_group": group,
            "title": str(definition.get("title") or scenario_id),
            "operation_count": int(definition["workload"]["operation_count"]),
            "outbound_count": int(
                definition["workload"]["operation_mix"]["OUTBOUND"]
            ),
            "inbound_count": int(
                definition["workload"]["operation_mix"]["INBOUND"]
            ),
            "total_robot_count": int(definition["robots"]["total_robot_count"]),
            "eligible_robot_count": int(
                definition["robots"]["eligible_robot_count"]
            ),
            "low_battery_robot_count": int(
                definition["robots"]["low_battery_robot_count"]
            ),
            "materialization_passed": materialization.get("passed") is True,
            "execution_passed": None,
            "status": "MATERIALIZED",
            "errors": [],
        }
        if group == "REPLAN" and args.initial_execution == "cuopt":
            mission = AutoMissionRequest.model_validate(
                _read(capture_root / "internal_request.json")
            )
            repository = JsonWarehouseRepository(
                capture_root / "frozen_repository",
                warehouse_id=mission.warehouse_id,
                simulation_id=mission.simulation_id,
            )
            dynamic = validate_replan_with_cuopt(
                definition,
                request=mission,
                repository=repository,
            )
            (capture_root / "cuopt_replan_report.json").write_text(
                json.dumps(dynamic, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            record.update(
                {
                    "execution_passed": dynamic.get("passed") is True,
                    "status": "CUOPT_REPLAN_VERIFIED",
                    "solver_backend": "cuopt",
                    "solver_status": (
                        "success" if dynamic.get("passed") is True else "failed"
                    ),
                    "cuopt_solve_count": int(dynamic.get("cuopt_solve_count") or 0),
                    "initial_solver_backend": dynamic.get("initial_solver_backend"),
                    "initial_solver_status": dynamic.get("initial_solver_status"),
                    "replan_solver_backend": dynamic.get("replan_solver_backend"),
                    "replan_solver_status": dynamic.get("replan_solver_status"),
                    "errors": dynamic.get("failed_checks") or [],
                }
            )
        elif (
            group == "HUMAN_REVIEW"
            and args.initial_execution == "cuopt"
            and definition.get("dynamic_contract", {}).get(
                "expected_reason_code"
            )
            == "DESTINATION_OVERRIDE_APPROVAL"
        ):
            mission = AutoMissionRequest.model_validate(
                _read(capture_root / "internal_request.json")
            )
            repository = JsonWarehouseRepository(
                capture_root / "frozen_repository",
                warehouse_id=mission.warehouse_id,
                simulation_id=mission.simulation_id,
            )
            dynamic = validate_destination_approval_with_cuopt(
                definition,
                request=mission,
                repository=repository,
                hitl_root=capture_root / "hitl",
            )
            (capture_root / "cuopt_hitl_resume_report.json").write_text(
                json.dumps(dynamic, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            record.update(
                {
                    "execution_passed": dynamic.get("passed") is True,
                    "status": (
                        "CUOPT_HITL_RESUME_VERIFIED"
                        if dynamic.get("passed") is True
                        else "CUOPT_HITL_RESUME_FAILED"
                    ),
                    "solver_backend": dynamic.get("solver_backend"),
                    "solver_status": dynamic.get("solver_status"),
                    "cuopt_solve_count": int(
                        dynamic.get("cuopt_solve_count") or 0
                    ),
                    "approved_destination": dynamic.get(
                        "approved_destination"
                    ),
                    "errors": dynamic.get("failed_checks") or [],
                }
            )
        elif group != "INITIAL":
            dynamic = _read(capture_root / "dynamic_contract_report.json")
            record.update(
                {
                    "execution_passed": dynamic.get("passed") is True,
                    "status": "DYNAMIC_CONTRACT_VERIFIED",
                    "solver_backend": "not_applicable",
                    "solver_status": "not_applicable",
                    "cuopt_solve_count": 0,
                    "errors": dynamic.get("failed_checks") or [],
                }
            )
        elif args.initial_execution == "cuopt":
            mission = AutoMissionRequest.model_validate(
                _read(capture_root / "internal_request.json")
            )
            repository = JsonWarehouseRepository(
                capture_root / "frozen_repository",
                warehouse_id=mission.warehouse_id,
                simulation_id=mission.simulation_id,
            )
            result = OrchestrationService().run(
                mission.model_copy(update={"optimization_backend": "cuopt"}),
                trusted_planning_mode="force_rule",
                persist_simulation_plan=False,
                repository=repository,
            )
            optimizer = result.execution_optimizer_result or result.optimizer_result
            passed = bool(
                result.status == "plan_validated"
                and optimizer is not None
                and optimizer.backend == "cuopt"
                and optimizer.status == "success"
                and result.route_validation is not None
                and result.route_validation.valid
                and result.mapf_validation is not None
                and result.mapf_validation.valid
            )
            record.update(
                {
                    "execution_passed": passed,
                    "status": result.status,
                    "solver_backend": optimizer.backend if optimizer else None,
                    "solver_status": optimizer.status if optimizer else None,
                    "cuopt_solve_count": 1 if optimizer is not None else 0,
                    "errors": [
                        value.model_dump(mode="json") for value in result.errors
                    ],
                }
            )
        records.append(record)

    groups = Counter(value["scenario_group"] for value in records)
    passed = sum(
        value["materialization_passed"] and value["execution_passed"] is not False
        for value in records
    )
    summary = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "scenario_count": len(records),
        "group_counts": dict(sorted(groups.items())),
        "passed_count": passed,
        "failed_count": len(records) - passed,
        "initial_execution": args.initial_execution,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "records": records,
    }
    output = root / "catalog_verification_summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**summary, "records": []}, ensure_ascii=False, indent=2))
    print(f"Full report: {output}")
    expected_count = len(definitions)
    return 0 if len(records) == expected_count and passed == expected_count else 1


if __name__ == "__main__":
    raise SystemExit(main())

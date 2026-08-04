"""Scenario-pack utilities for LARO Native Plan Bridge v4.1 / LARO 13.25.1.

This module is intentionally HTTP-only and does not modify the orchestration
core.  It sends requests to the already-running Native Plan API, persists raw
evidence, derives review metrics from the v4.1 trace, and evaluates scenario
contracts.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
PACK_VERSION = "4.1-scenario-pack-2.0"
LARO_TARGET_VERSION = "13.25.1"
SCENARIO_DIR = ROOT / "scenarios" / "native_plan_complex_v4_1"
DEFAULT_OUTPUT_DIR = ROOT / "runtime_outputs" / "native_plan_complex_v4_1"

COMMON_TRACE_CHECK_KEYS = (
    "dynamic_input_valid",
    "payload_valid",
    "candidate_space_valid",
    "assignment_valid",
    "route_valid",
    "mapf_valid",
    "logical_operation_coverage_valid",
)

# ``structured_keys_valid`` is produced only by the Rule fast path.
# Agent formulation validates canonical identifiers through its retrieval-plan,
# situation-graph, and dynamic-input contracts instead, so a missing value is
# intentionally N/A rather than a failed check.
RULE_ONLY_TRACE_CHECK_KEYS = ("structured_keys_valid",)

PLAN_TERMINAL_NONEXECUTION_STATUSES = {
    "DEFERRED",
    "REJECTED",
    "HITL_REQUIRED",
    "CANCELLED",
    "HELD",
}


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def load_index() -> dict[str, Any]:
    path = SCENARIO_DIR / "index.json"
    if not path.exists():
        raise FileNotFoundError(f"Scenario index is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_scenarios() -> list[dict[str, Any]]:
    return list(load_index().get("scenarios") or [])


def load_scenario(identifier: str) -> dict[str, Any]:
    path = Path(identifier)
    if not path.exists():
        path = SCENARIO_DIR / f"{identifier}.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown v4.1 complex scenario: {identifier}")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["_path"] = str(path.resolve())
    return value




def scenario_requires_openai(scenario: dict[str, Any], server_mode: str | None) -> bool:
    """Return whether this scenario will call OpenAI under the observed server mode."""

    if bool(scenario.get("input_requires_openai")):
        return True
    if str(server_mode or "").strip().casefold() == "llm_router":
        return bool(scenario.get("requires_openai_if_llm_router", True))
    return False


def request_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 900.0,
) -> tuple[int, dict[str, Any], float]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw.strip() else {}
            return int(response.status), payload, (time.perf_counter() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": raw}
        return int(exc.code), payload, (time.perf_counter() - started) * 1000.0
    except urllib.error.URLError as exc:
        return 0, {"detail": f"{type(exc).__name__}: {exc}"}, (time.perf_counter() - started) * 1000.0


def deep_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def apply_backend(request_body: dict[str, Any], backend: str | None) -> dict[str, Any]:
    value = deep_copy(request_body)
    if backend:
        value["optimization_backend"] = backend
    return value


def logical_operation_ids(response: dict[str, Any]) -> list[str]:
    plan = response.get("plan") or {}
    return [
        str(value.get("operation_id"))
        for value in plan.get("logical_operations", []) or []
        if value.get("operation_id")
    ]


def assigned_robot_ids(response: dict[str, Any]) -> list[str]:
    plan = response.get("plan") or {}
    return [
        str(value.get("robot_id"))
        for value in plan.get("robots", []) or []
        if value.get("robot_id")
    ]


def response_reason_codes(response: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for value in response.get("errors", []) or []:
        if isinstance(value, dict):
            for key in ("code", "reason_code", "error_code"):
                if value.get(key):
                    codes.add(str(value[key]))
    for key in (
        "input_rejection",
        "workflow_hold",
        "pending_human_interaction",
        "human_review",
        "failure",
    ):
        value = response.get(key)
        if isinstance(value, dict):
            for field in ("reason_code", "code", "error_code"):
                if value.get(field):
                    codes.add(str(value[field]))
    return codes


def plan_signature(response: dict[str, Any]) -> str | None:
    plan = response.get("plan")
    if not isinstance(plan, dict):
        return None
    normalized = {
        "robots": [
            {
                "robot_id": robot.get("robot_id"),
                "initial_node": robot.get("initial_node"),
                "available_at_ms": robot.get("available_at_ms"),
                "steps": [
                    {
                        "step_type": step.get("step_type"),
                        "edge_id": step.get("edge_id"),
                        "from_node": step.get("from_node"),
                        "to_node": step.get("to_node"),
                        "node_id": step.get("node_id"),
                        "task_id": step.get("task_id"),
                        "service_kind": step.get("service_kind"),
                        "start_at_ms": step.get("start_at_ms"),
                        "end_at_ms": step.get("end_at_ms"),
                    }
                    for step in robot.get("steps", []) or []
                ],
            }
            for robot in plan.get("robots", []) or []
        ],
        "logical_operations": [
            {
                "operation_id": operation.get("operation_id"),
                "operation_type": operation.get("operation_type"),
                "status": operation.get("status"),
                "assigned_robot_id": operation.get("assigned_robot_id"),
                "task_ids": operation.get("task_ids"),
            }
            for operation in plan.get("logical_operations", []) or []
        ],
        "station_reservations": plan.get("station_reservations", []),
        "makespan_ms": plan.get("makespan_ms"),
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def step_metrics(plan: dict[str, Any]) -> dict[str, Any]:
    counts = {"MOVE": 0, "WAIT": 0, "SERVICE": 0}
    duration = {"MOVE": 0, "WAIT": 0, "SERVICE": 0}
    service_kinds: dict[str, int] = {}
    total_distance = 0.0
    robot_metrics: list[dict[str, Any]] = []

    for robot in plan.get("robots", []) or []:
        robot_counts = {"MOVE": 0, "WAIT": 0, "SERVICE": 0}
        robot_duration = {"MOVE": 0, "WAIT": 0, "SERVICE": 0}
        robot_distance = 0.0
        for step in robot.get("steps", []) or []:
            kind = str(step.get("step_type") or "UNKNOWN")
            elapsed = max(
                0,
                int(step.get("end_at_ms") or 0) - int(step.get("start_at_ms") or 0),
            )
            if kind in counts:
                counts[kind] += 1
                duration[kind] += elapsed
                robot_counts[kind] += 1
                robot_duration[kind] += elapsed
            distance = float(step.get("distance_m") or 0.0)
            total_distance += distance
            robot_distance += distance
            service_kind = step.get("service_kind")
            if service_kind:
                key = str(service_kind)
                service_kinds[key] = service_kinds.get(key, 0) + 1
        robot_metrics.append(
            {
                "robot_id": robot.get("robot_id"),
                "finish_at_ms": robot.get("finish_at_ms"),
                "step_counts": robot_counts,
                "step_durations_ms": robot_duration,
                "distance_m": round(robot_distance, 3),
            }
        )

    return {
        "step_counts": counts,
        "step_durations_ms": duration,
        "service_kind_counts": service_kinds,
        "total_distance_m": round(total_distance, 3),
        "robot_metrics": robot_metrics,
    }


def _node_duration(nodes: list[dict[str, Any]], names: Iterable[str]) -> float:
    wanted = set(names)
    return sum(
        float(value.get("duration_ms") or 0.0)
        for value in nodes
        if str(value.get("node_name") or "") in wanted
    )


def latency_metrics(http_duration_ms: float, trace: dict[str, Any] | None) -> dict[str, Any]:
    """Derive a complete latency breakdown from the v4.1 trace itself.

    v4.1 does not require the v4.2 ``latency_summary`` core change.  When that
    field is absent, this function computes the same review metrics from
    ``trace.nodes``.
    """

    if not trace:
        return {
            "http_total_ms": round(http_duration_ms, 3),
            "trace_available": False,
            "llm_call_count": 0,
            "llm_ms": 0.0,
        }

    nodes = list(trace.get("nodes") or [])
    node_total = sum(float(value.get("duration_ms") or 0.0) for value in nodes)
    llm_nodes = [
        {
            "node_name": str(value.get("node_name") or ""),
            "duration_ms": round(float(value.get("duration_ms") or 0.0), 3),
        }
        for value in nodes
        if value.get("llm_used")
    ]
    llm_ms = sum(float(value["duration_ms"]) for value in llm_nodes)

    db_context_names = {
        "inventory_context",
        "map_context",
        "robot_runtime",
        "parallel_retrieval_executor",
        "agent_context_materializer",
        "context_snapshot_finalize",
        "warehouse_situation_graph_builder",
        "situation_graph_sufficiency_guard",
    }
    solver_names = {"optimizer"}
    mapf_plan_names = {
        "optimizer_assignment_validator",
        "goods_to_person_execution_enricher",
        "prioritized_mapf_planner",
        "route_static_validator",
        "mapf_plan_validator",
        "simulation_plan_builder",
        "logical_operation_coverage_validator",
    }
    persistence_names = {"persist_result", "dashboard_event"}
    formulator_names = {
        "rule_cuopt_formulator_direct",
        "llm_cuopt_formulator",
        "cuopt_formulation_retry_prepare",
        "cuopt_dynamic_input_validator",
        "optimization_request_from_dynamic_input",
        "goods_to_person_compiler",
        "cuopt_payload",
        "cuopt_schema_validator",
        "candidate_space_guard",
    }

    db_context_ms = _node_duration(nodes, db_context_names)
    solver_ms = _node_duration(nodes, solver_names)
    mapf_plan_ms = _node_duration(nodes, mapf_plan_names)
    persistence_ms = _node_duration(nodes, persistence_names)
    formulation_ms = _node_duration(nodes, formulator_names)
    classified = db_context_ms + solver_ms + mapf_plan_ms + persistence_ms + formulation_ms

    slowest_nodes = sorted(
        [
            {
                "node_name": str(value.get("node_name") or ""),
                "duration_ms": round(float(value.get("duration_ms") or 0.0), 3),
                "llm_used": bool(value.get("llm_used")),
                "status": value.get("status"),
            }
            for value in nodes
        ],
        key=lambda value: float(value.get("duration_ms") or 0.0),
        reverse=True,
    )[:15]

    return {
        "http_total_ms": round(http_duration_ms, 3),
        "trace_available": True,
        "node_total_ms": round(node_total, 3),
        "http_minus_node_ms": round(http_duration_ms - node_total, 3),
        "llm_call_count": len(llm_nodes),
        "llm_ms": round(llm_ms, 3),
        "llm_share_pct": round(llm_ms / node_total * 100.0, 2) if node_total else 0.0,
        "db_context_ms": round(db_context_ms, 3),
        "formulation_and_payload_ms": round(formulation_ms, 3),
        "solver_ms": round(solver_ms, 3),
        "mapf_and_plan_ms": round(mapf_plan_ms, 3),
        "persistence_ms": round(persistence_ms, 3),
        "other_node_ms": round(max(0.0, node_total - classified), 3),
        "llm_nodes": llm_nodes,
        "slowest_nodes": slowest_nodes,
    }


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_run_csv_artifacts(
    run_dir: Path,
    response: dict[str, Any],
    trace: dict[str, Any] | None,
) -> None:
    node_columns = ("node_name", "status", "duration_ms", "llm_used", "error_code")
    with (run_dir / "node_timings.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=node_columns)
        writer.writeheader()
        for node in (trace or {}).get("nodes", []) or []:
            writer.writerow({key: node.get(key) for key in node_columns})

    plan = response.get("plan") or {}
    operation_columns = (
        "operation_id",
        "operation_type",
        "status",
        "assigned_robot_id",
        "task_ids",
        "item_id",
        "quantity",
        "source_port_id",
        "handling_unit_id",
    )
    with (run_dir / "logical_operations.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=operation_columns)
        writer.writeheader()
        for operation in plan.get("logical_operations", []) or []:
            row = {key: operation.get(key) for key in operation_columns}
            row["task_ids"] = ";".join(str(value) for value in operation.get("task_ids", []) or [])
            writer.writerow(row)

    step_columns = (
        "robot_id",
        "sequence",
        "step_id",
        "step_type",
        "start_at_ms",
        "end_at_ms",
        "duration_ms",
        "from_node",
        "to_node",
        "node_id",
        "edge_id",
        "task_id",
        "service_kind",
        "distance_m",
        "reason",
    )
    with (run_dir / "robot_steps.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=step_columns)
        writer.writeheader()
        for robot in plan.get("robots", []) or []:
            for step in robot.get("steps", []) or []:
                row = {key: step.get(key) for key in step_columns}
                row["robot_id"] = robot.get("robot_id")
                row["duration_ms"] = max(
                    0,
                    int(step.get("end_at_ms") or 0) - int(step.get("start_at_ms") or 0),
                )
                writer.writerow(row)

    reservation_columns = (
        "reservation_id",
        "station_id",
        "station_robot_id",
        "handling_unit_id",
        "mobile_robot_id",
        "start_at_ms",
        "end_at_ms",
        "processed_quantity",
    )
    with (run_dir / "station_reservations.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=reservation_columns)
        writer.writeheader()
        for reservation in plan.get("station_reservations", []) or []:
            writer.writerow({key: reservation.get(key) for key in reservation_columns})


def _expected_for_backend(scenario: dict[str, Any], backend: str | None) -> dict[str, Any]:
    expected = deep_copy(scenario.get("expected") or {})
    backend_overrides = (scenario.get("expected_by_backend") or {}).get(backend or "")
    if isinstance(backend_overrides, dict):
        expected.update(deep_copy(backend_overrides))
    if backend == "cuopt_payload_only":
        expected.update(
            {
                "allowed_status": ["ready_for_cuopt"],
                "require_plan": False,
                "forbid_plan": True,
                "require_all_trace_checks": False,
            }
        )
    return expected


def _stage(
    name: str,
    *,
    applicable: bool,
    checks: dict[str, Any],
    errors: list[str],
    evidence_keys: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "stage": name,
        "status": "SKIPPED" if not applicable else ("PASS" if not errors else "FAIL"),
        "applicable": applicable,
        "checks": checks,
        "errors": errors,
        "evidence_keys": evidence_keys or [],
    }


def _operation_ids_from_debug(debug: dict[str, Any] | None) -> list[str]:
    normalized = (debug or {}).get("normalized_request") or {}
    return [
        str(value.get("operation_id"))
        for value in normalized.get("operations", []) or []
        if value.get("operation_id")
    ]


def build_stage_contract(
    scenario: dict[str, Any],
    response: dict[str, Any],
    trace: dict[str, Any] | None,
    debug: dict[str, Any] | None,
    *,
    backend: str | None,
) -> dict[str, Any]:
    """Validate semantic output at every major planning boundary.

    Node ``status=success`` only proves that a function returned.  These checks
    prove that each stage produced the right business object before the next
    stage consumed it.
    """

    expected = _expected_for_backend(scenario, backend)
    stages: list[dict[str, Any]] = []
    trace_nodes = {
        str(value.get("node_name") or "")
        for value in (trace or {}).get("nodes", []) or []
    }
    expected_ops = set(
        expected.get("expected_operations")
        or expected.get("expected_operations_if_plan")
        or []
    )

    # 1. Router / normalization.
    router_errors: list[str] = []
    normalized = (debug or {}).get("normalized_request") or {}
    normalized_ops = set(_operation_ids_from_debug(debug))
    if not normalized:
        router_errors.append("normalized_request is missing")
    if expected_ops and normalized_ops != expected_ops:
        router_errors.append(
            f"normalized operations expected={sorted(expected_ops)!r} actual={sorted(normalized_ops)!r}"
        )
    if expected.get("require_router_llm") and response.get("router_llm_executed") is not True:
        router_errors.append("router_llm_executed is not true")
    allowed_routes = set(expected.get("allowed_final_routes") or [])
    if allowed_routes and response.get("final_route") not in allowed_routes:
        router_errors.append(
            f"final_route={response.get('final_route')!r} not in {sorted(allowed_routes)!r}"
        )
    constraint_checks: dict[str, Any] = {}
    expected_constraints = expected.get("expected_constraints") or {}
    constraints = normalized.get("constraints") or {}
    for key in ("objective_profile", "reserve_robot_count", "reserve_robot_min_battery_pct"):
        if key in expected_constraints:
            observed = constraints.get(key)
            constraint_checks[key] = observed
            if observed != expected_constraints[key]:
                router_errors.append(
                    f"constraint {key}={observed!r}; expected {expected_constraints[key]!r}"
                )
    if "objective_terms" in expected_constraints:
        observed = set(constraints.get("objective_terms") or [])
        wanted = set(expected_constraints["objective_terms"])
        constraint_checks["objective_terms"] = sorted(observed)
        if observed != wanted:
            router_errors.append(
                f"objective_terms={sorted(observed)!r}; expected {sorted(wanted)!r}"
            )
    if "conditional_edge_policy" in expected_constraints:
        wanted = expected_constraints["conditional_edge_policy"]
        policies = constraints.get("conditional_edge_policies") or []
        observed = next(
            (value for value in policies if value.get("edge_id") == wanted.get("edge_id")),
            None,
        )
        constraint_checks["conditional_edge_policy"] = observed
        if observed is None:
            router_errors.append(f"conditional policy for {wanted.get('edge_id')} is missing")
        else:
            for key, value in wanted.items():
                if observed.get(key) != value:
                    router_errors.append(
                        f"conditional policy {key}={observed.get(key)!r}; expected {value!r}"
                    )
    stages.append(
        _stage(
            "01_ROUTER_NORMALIZATION",
            applicable=True,
            checks={
                "normalized_operation_ids": sorted(normalized_ops),
                "final_route": response.get("final_route"),
                "router_llm_executed": response.get("router_llm_executed"),
                "constraints": constraint_checks,
            },
            errors=router_errors,
            evidence_keys=["normalized_request", "request_gate_decision", "orchestration_plan"],
        )
    )

    # 2. Agent retrieval is required only for Agent formulation.
    agent_route = response.get("final_route") == "AGENT_FORMULATION"
    retrieval_errors: list[str] = []
    if agent_route:
        required = {
            "canonical_retrieval_key_builder",
            "parallel_retrieval_executor",
            "warehouse_situation_graph_builder",
        }
        missing = required - trace_nodes
        if missing:
            retrieval_errors.append(f"Agent retrieval nodes missing={sorted(missing)!r}")
        execution = (debug or {}).get("parallel_retrieval_execution") or {}
        if not execution:
            retrieval_errors.append("parallel_retrieval_execution is missing")
        situation_validation = (debug or {}).get("situation_graph_validation") or {}
        if situation_validation.get("valid") is not True:
            retrieval_errors.append(
                f"situation_graph_validation.valid={situation_validation.get('valid')!r}"
            )
    stages.append(
        _stage(
            "02_AGENT_RETRIEVAL",
            applicable=agent_route,
            checks={
                "trace_nodes": sorted(trace_nodes),
                "completed_tools": (debug or {}).get("completed_retrieval_tools") or [],
                "observation_count": len((debug or {}).get("retrieval_observations") or []),
            },
            errors=retrieval_errors,
            evidence_keys=[
                "parallel_retrieval_plan",
                "parallel_retrieval_execution",
                "retrieval_observations",
                "warehouse_situation_graph",
                "situation_graph_validation",
            ],
        )
    )

    # 3. Context snapshot and authoritative data sources.
    context_errors: list[str] = []
    context_snapshot = (debug or {}).get("context_snapshot") or {}
    inventory_context = (debug or {}).get("inventory_context") or {}
    map_context = (debug or {}).get("map_context") or {}
    robot_context = (debug or {}).get("robot_context") or {}
    if not context_snapshot:
        context_errors.append("context_snapshot is missing")
    if not inventory_context:
        context_errors.append("inventory_context is missing")
    if not map_context:
        context_errors.append("map_context is missing")
    if not robot_context:
        context_errors.append("robot_context is missing")
    repository = (trace or {}).get("repository") or {}
    if expected.get("require_live_repository") and repository.get("repository_type") != "LiveWarehouseRepository":
        context_errors.append(
            f"repository_type={repository.get('repository_type')!r}; expected LiveWarehouseRepository"
        )
    stages.append(
        _stage(
            "03_CONTEXT_SNAPSHOT",
            applicable=True,
            checks={
                "snapshot_id": context_snapshot.get("snapshot_id"),
                "repository": repository,
                "inventory_task_count": len(inventory_context.get("task_needs") or []),
                "inbound_task_count": len(inventory_context.get("inbound_needs") or []),
                "candidate_robot_ids": robot_context.get("candidate_robot_ids") or [],
                "map_node_count": map_context.get("node_count"),
                "map_edge_count": map_context.get("edge_count"),
            },
            errors=context_errors,
            evidence_keys=["context_snapshot", "inventory_context", "map_context", "robot_context"],
        )
    )

    # 4. Rule/Agent formulation and operation coverage.
    formulation_errors: list[str] = []
    draft = (debug or {}).get("cuopt_dynamic_input_draft") or {}
    validation = (debug or {}).get("cuopt_dynamic_input_validation") or {}
    represented = set(draft.get("g2p_order_ids") or [])
    represented |= {
        str(value.get("order_id"))
        for value in draft.get("tasks", []) or []
        if value.get("order_id")
    }
    represented |= set(draft.get("deferred_order_ids") or [])
    if not draft:
        formulation_errors.append("cuopt_dynamic_input_draft is missing")
    if validation.get("valid") is not True:
        formulation_errors.append(f"cuopt_dynamic_input_validation.valid={validation.get('valid')!r}")
    if expected_ops and represented != expected_ops:
        formulation_errors.append(
            f"draft operation coverage expected={sorted(expected_ops)!r} actual={sorted(represented)!r}"
        )
    fleet = draft.get("fleet") or {}
    expected_reserved_count = expected.get("expected_reserved_robot_count")
    reserved_ids = set(fleet.get("reserved_robot_ids") or [])
    if expected_reserved_count is not None and len(reserved_ids) != int(expected_reserved_count):
        formulation_errors.append(
            f"reserved_robot_count={len(reserved_ids)}; expected {expected_reserved_count}"
        )
    expected_reserved_ids = set(expected.get("expected_reserved_robot_ids") or [])
    if expected_reserved_ids and reserved_ids != expected_reserved_ids:
        formulation_errors.append(
            f"reserved_robot_ids={sorted(reserved_ids)!r}; expected {sorted(expected_reserved_ids)!r}"
        )
    stages.append(
        _stage(
            "04_FORMULATION",
            applicable=bool(draft) or response.get("status") == "plan_validated",
            checks={
                "formulation_source": draft.get("formulation_source"),
                "formulation_mode": draft.get("formulation_mode"),
                "represented_operation_ids": sorted(represented),
                "included_robot_ids": fleet.get("included_robot_ids") or [],
                "excluded_robot_ids": fleet.get("excluded_robot_ids") or [],
                "reserved_robot_ids": sorted(reserved_ids),
                "objective_profile": draft.get("objective_profile"),
                "objective_terms": draft.get("objective_terms") or [],
                "validation_errors": validation.get("errors") or [],
            },
            errors=formulation_errors,
            evidence_keys=["cuopt_dynamic_input_draft", "cuopt_dynamic_input_validation"],
        )
    )

    # 5. Solver payload.
    payload_errors: list[str] = []
    payload = (debug or {}).get("cuopt_payload") or {}
    payload_validation = (debug or {}).get("payload_validation") or {}
    candidate_validation = (debug or {}).get("candidate_space_validation") or {}
    payload_applicable = backend != "cuopt_payload_only" or bool(payload)
    if not payload:
        payload_errors.append("cuopt_payload is missing")
    if payload_validation.get("valid") is not True:
        payload_errors.append(f"payload_validation.valid={payload_validation.get('valid')!r}")
    if candidate_validation.get("valid") is not True:
        payload_errors.append(
            f"candidate_space_validation.valid={candidate_validation.get('valid')!r}"
        )
    task_data = payload.get("task_data") or {}
    fleet_data = payload.get("fleet_data") or {}
    stages.append(
        _stage(
            "05_SOLVER_PAYLOAD",
            applicable=payload_applicable,
            checks={
                "task_row_count": len(task_data.get("task_ids") or []),
                "pickup_delivery_pair_count": len(task_data.get("pickup_and_delivery_pairs") or []),
                "vehicle_ids": fleet_data.get("vehicle_ids") or [],
                "min_vehicles": fleet_data.get("min_vehicles"),
                "location_count": len(payload.get("location_index_map") or {}),
                "directed_edge_count": len((payload.get("waypoint_graph_data") or {}).get("edge_ids") or []),
            },
            errors=payload_errors,
            evidence_keys=["optimization_request", "cuopt_payload", "payload_validation", "candidate_space_validation"],
        )
    )

    # 6. Optimizer assignment.
    optimizer_applicable = backend != "cuopt_payload_only" and isinstance(response.get("plan"), dict)
    optimizer_errors: list[str] = []
    optimizer = (debug or {}).get("optimizer_result") or {}
    assignment_validation = (debug or {}).get("optimizer_assignment_validation") or {}
    if optimizer_applicable:
        if optimizer.get("status") != "success":
            optimizer_errors.append(f"optimizer.status={optimizer.get('status')!r}")
        if optimizer.get("unassigned_task_ids"):
            optimizer_errors.append(
                f"unassigned_task_ids={optimizer.get('unassigned_task_ids')!r}"
            )
        if assignment_validation.get("valid") is not True:
            optimizer_errors.append(
                f"optimizer_assignment_validation.valid={assignment_validation.get('valid')!r}"
            )
        if backend == "cuopt" and optimizer.get("optimizer") != "nvidia-cuopt":
            optimizer_errors.append(
                f"optimizer provider={optimizer.get('optimizer')!r}; expected nvidia-cuopt"
            )
    stages.append(
        _stage(
            "06_OPTIMIZER",
            applicable=optimizer_applicable,
            checks={
                "backend": optimizer.get("backend"),
                "provider": optimizer.get("optimizer"),
                "route_count": len(optimizer.get("routes") or []),
                "estimated_makespan_ms": optimizer.get("estimated_makespan_ms"),
                "unassigned_task_ids": optimizer.get("unassigned_task_ids") or [],
            },
            errors=optimizer_errors,
            evidence_keys=["optimizer_result", "optimizer_assignment_validation"],
        )
    )

    # 7. MAPF / traffic safety.
    mapf_applicable = optimizer_applicable
    mapf_errors: list[str] = []
    schedule = (debug or {}).get("traffic_schedule") or {}
    route_validation = (debug or {}).get("route_validation") or {}
    mapf_validation = (debug or {}).get("mapf_validation") or {}
    if mapf_applicable:
        if schedule.get("valid") is not True:
            mapf_errors.append(f"traffic_schedule.valid={schedule.get('valid')!r}")
        if schedule.get("conflicts"):
            mapf_errors.append(f"traffic conflicts={schedule.get('conflicts')!r}")
        if route_validation.get("valid") is not True:
            mapf_errors.append(f"route_validation.valid={route_validation.get('valid')!r}")
        if mapf_validation.get("valid") is not True:
            mapf_errors.append(f"mapf_validation.valid={mapf_validation.get('valid')!r}")
        min_wait = expected.get("min_total_wait_ms")
        if min_wait is not None and int(schedule.get("total_wait_ms") or 0) < int(min_wait):
            mapf_errors.append(
                f"traffic_schedule.total_wait_ms={schedule.get('total_wait_ms')!r}; expected >= {min_wait}"
            )
    stages.append(
        _stage(
            "07_MAPF_TRAFFIC",
            applicable=mapf_applicable,
            checks={
                "planner": schedule.get("planner"),
                "route_count": len(schedule.get("routes") or []),
                "reservation_count": len(schedule.get("reservations") or []),
                "station_reservation_count": len(schedule.get("station_reservations") or []),
                "total_wait_ms": schedule.get("total_wait_ms"),
                "total_service_ms": schedule.get("total_service_ms"),
                "makespan_ms": schedule.get("makespan_ms"),
                "conflicts": schedule.get("conflicts") or [],
            },
            errors=mapf_errors,
            evidence_keys=["traffic_schedule", "route_validation", "mapf_validation"],
        )
    )

    # 8. Final simulation plan and independent operation coverage.
    plan = response.get("plan") or {}
    plan_errors: list[str] = []
    coverage = (debug or {}).get("logical_operation_coverage_validation") or {}
    plan_applicable = isinstance(response.get("plan"), dict)
    if plan_applicable:
        planned_ops = set(logical_operation_ids(response))
        if expected_ops and planned_ops != expected_ops:
            plan_errors.append(
                f"final operations expected={sorted(expected_ops)!r} actual={sorted(planned_ops)!r}"
            )
        if coverage.get("valid") is not True:
            plan_errors.append(
                f"logical_operation_coverage_validation.valid={coverage.get('valid')!r}"
            )
        for operation in plan.get("logical_operations", []) or []:
            if not operation.get("task_ids"):
                plan_errors.append(f"{operation.get('operation_id')} has no task_ids")
            if not operation.get("assigned_robot_id"):
                plan_errors.append(f"{operation.get('operation_id')} has no assigned_robot_id")
    stages.append(
        _stage(
            "08_SIMULATION_PLAN",
            applicable=plan_applicable,
            checks={
                "plan_id": plan.get("plan_id"),
                "plan_version": plan.get("plan_version"),
                "robot_count": len(plan.get("robots") or []),
                "step_count": sum(len(value.get("steps") or []) for value in plan.get("robots", []) or []),
                "logical_operation_ids": logical_operation_ids(response),
                "makespan_ms": plan.get("makespan_ms"),
                "coverage": coverage,
            },
            errors=plan_errors,
            evidence_keys=["simulation_plan", "logical_operation_coverage_validation"],
        )
    )

    all_errors = [
        f"{stage['stage']}: {error}"
        for stage in stages
        for error in stage.get("errors", [])
    ]
    return {
        "scenario_id": scenario.get("scenario_id"),
        "backend": backend,
        "status": "PASS" if not all_errors else "FAIL",
        "stages": stages,
        "assertion_errors": all_errors,
    }


def write_stage_contract_artifacts(
    run_dir: Path,
    stage_contract: dict[str, Any],
    debug: dict[str, Any] | None,
) -> None:
    """Persist one machine-readable and one human-readable stage report."""

    save_json(run_dir / "stage_contract.json", stage_contract)
    stage_dir = run_dir / "stage_outputs"
    stage_dir.mkdir(parents=True, exist_ok=True)
    debug = debug or {}
    stage_payloads = {
        "01_router.json": {
            key: debug.get(key)
            for key in ("normalized_request", "request_gate_decision", "orchestration_plan")
        },
        "02_retrieval.json": {
            key: debug.get(key)
            for key in (
                "parallel_retrieval_plan",
                "parallel_retrieval_execution",
                "retrieval_observations",
                "warehouse_situation_graph",
                "situation_graph_validation",
            )
        },
        "03_context.json": {
            key: debug.get(key)
            for key in ("context_snapshot", "inventory_context", "map_context", "robot_context")
        },
        "04_formulation.json": {
            key: debug.get(key)
            for key in (
                "cuopt_dynamic_input_draft",
                "cuopt_evidence_enrichment",
                "cuopt_dynamic_input_validation",
                "cuopt_dynamic_input_validation_history",
            )
        },
        "05_payload.json": {
            key: debug.get(key)
            for key in (
                "optimization_request",
                "cuopt_payload",
                "payload_validation",
                "candidate_space_validation",
            )
        },
        "06_optimizer.json": {
            key: debug.get(key)
            for key in ("optimizer_result", "optimizer_assignment_validation")
        },
        "07_mapf.json": {
            key: debug.get(key)
            for key in ("traffic_schedule", "route_validation", "mapf_validation")
        },
        "08_plan.json": {
            key: debug.get(key)
            for key in ("simulation_plan", "logical_operation_coverage_validation")
        },
    }
    for filename, payload in stage_payloads.items():
        save_json(stage_dir / filename, payload)

    columns = ("stage", "status", "applicable", "error_count", "errors")
    with (run_dir / "stage_contract.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for stage in stage_contract.get("stages", []) or []:
            writer.writerow(
                {
                    "stage": stage.get("stage"),
                    "status": stage.get("status"),
                    "applicable": stage.get("applicable"),
                    "error_count": len(stage.get("errors") or []),
                    "errors": "; ".join(stage.get("errors") or []),
                }
            )

    lines = [
        f"# Stage Contract — {stage_contract.get('scenario_id')}",
        "",
        f"- Backend: `{stage_contract.get('backend')}`",
        f"- Status: **{stage_contract.get('status')}**",
        "",
        "| Stage | Status | Applicable | Errors |",
        "|---|---|---:|---|",
    ]
    for stage in stage_contract.get("stages", []) or []:
        lines.append(
            f"| {_md_escape(stage.get('stage'))} | {_md_escape(stage.get('status'))} | "
            f"{_md_escape(stage.get('applicable'))} | "
            f"{_md_escape('; '.join(stage.get('errors') or []))} |"
        )
    lines.extend(["", "## Detailed checks", ""])
    for stage in stage_contract.get("stages", []) or []:
        lines.extend(
            [
                f"### {stage.get('stage')} — {stage.get('status')}",
                "",
                "```json",
                json.dumps(stage.get("checks") or {}, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    (run_dir / "stage_contract.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluate(
    scenario: dict[str, Any],
    response: dict[str, Any],
    trace: dict[str, Any] | None,
    *,
    backend: str | None,
) -> list[str]:
    expected = _expected_for_backend(scenario, backend)
    errors: list[str] = []
    status = response.get("status")
    allowed_status = list(expected.get("allowed_status") or [])
    if allowed_status and status not in allowed_status:
        errors.append(f"status={status!r} not in {allowed_status!r}")

    plan = response.get("plan")
    if expected.get("require_plan") and not isinstance(plan, dict):
        errors.append("plan is required but missing")
    if expected.get("forbid_plan") and isinstance(plan, dict):
        errors.append("plan must not be created")

    allowed_routes = list(expected.get("allowed_final_routes") or [])
    if allowed_routes and response.get("final_route") not in allowed_routes:
        errors.append(f"final_route={response.get('final_route')!r} not in {allowed_routes!r}")
    if expected.get("require_router_llm") and response.get("router_llm_executed") is not True:
        errors.append("router_llm_executed is not true")

    trace_nodes = {
        str(value.get("node_name") or "")
        for value in (trace or {}).get("nodes", []) or []
    }
    required_trace_nodes = set(expected.get("required_trace_nodes") or [])
    forbidden_trace_nodes = set(expected.get("forbidden_trace_nodes") or [])
    missing_trace_nodes = required_trace_nodes - trace_nodes
    observed_forbidden_nodes = forbidden_trace_nodes & trace_nodes
    if missing_trace_nodes:
        errors.append(f"required trace nodes missing={sorted(missing_trace_nodes)!r}")
    if observed_forbidden_nodes:
        errors.append(f"forbidden trace nodes observed={sorted(observed_forbidden_nodes)!r}")

    operations = logical_operation_ids(response)
    operation_set = set(operations)
    expected_operations = set(expected.get("expected_operations") or [])
    expected_subset = set(expected.get("expected_operations_if_plan") or [])
    if expected_operations and operation_set != expected_operations:
        errors.append(
            f"operation coverage expected={sorted(expected_operations)!r} actual={sorted(operation_set)!r}"
        )
    if isinstance(plan, dict) and expected_subset and not expected_subset.issubset(operation_set):
        errors.append(f"operation coverage missing={sorted(expected_subset - operation_set)!r}")
    forbidden_operations = set(expected.get("forbidden_operations") or [])
    if operation_set & forbidden_operations:
        errors.append(f"forbidden operations observed={sorted(operation_set & forbidden_operations)!r}")
    if expected.get("require_unique_operations") and len(operations) != len(operation_set):
        errors.append(f"duplicate logical operations observed={operations!r}")

    assigned = set(assigned_robot_ids(response))
    forbidden_robots = set(expected.get("forbidden_robots") or [])
    allowed_robots = set(expected.get("allowed_robots") or [])
    required_robots = set(expected.get("required_robots") or [])
    if assigned & forbidden_robots:
        errors.append(f"forbidden robots assigned={sorted(assigned & forbidden_robots)!r}")
    if allowed_robots and not assigned.issubset(allowed_robots):
        errors.append(f"robots outside allowed set assigned={sorted(assigned - allowed_robots)!r}")
    if required_robots and not required_robots.issubset(assigned):
        errors.append(f"required robots not assigned={sorted(required_robots - assigned)!r}")

    min_robots = expected.get("min_robot_count")
    max_robots = expected.get("max_robot_count")
    if isinstance(plan, dict) and min_robots is not None and len(assigned) < int(min_robots):
        errors.append(f"robot_count={len(assigned)} < {min_robots}")
    if isinstance(plan, dict) and max_robots is not None and len(assigned) > int(max_robots):
        errors.append(f"robot_count={len(assigned)} > {max_robots}")

    observed_codes = response_reason_codes(response)
    forbidden_codes = set(expected.get("forbidden_reason_codes") or [])
    required_codes_any = set(expected.get("expected_reason_codes_any") or [])
    if observed_codes & forbidden_codes:
        errors.append(f"forbidden reason codes observed={sorted(observed_codes & forbidden_codes)!r}")
    if required_codes_any and not (observed_codes & required_codes_any):
        errors.append(
            f"none of expected reason codes observed; expected_any={sorted(required_codes_any)!r} "
            f"actual={sorted(observed_codes)!r}"
        )

    if isinstance(plan, dict):
        reservations = plan.get("station_reservations") or []
        min_reservations = expected.get("min_station_reservations")
        if min_reservations is not None and len(reservations) < int(min_reservations):
            errors.append(f"station_reservations={len(reservations)} < {min_reservations}")

        metrics = step_metrics(plan)
        min_wait_steps = expected.get("min_wait_steps")
        min_total_wait_ms = expected.get("min_total_wait_ms")
        min_service_steps = expected.get("min_service_steps")
        if min_wait_steps is not None and metrics["step_counts"]["WAIT"] < int(min_wait_steps):
            errors.append(f"WAIT steps={metrics['step_counts']['WAIT']} < {min_wait_steps}")
        if (
            min_total_wait_ms is not None
            and metrics["step_durations_ms"]["WAIT"] < int(min_total_wait_ms)
        ):
            errors.append(
                f"WAIT duration={metrics['step_durations_ms']['WAIT']} < {min_total_wait_ms}"
            )
        if min_service_steps is not None and metrics["step_counts"]["SERVICE"] < int(min_service_steps):
            errors.append(f"SERVICE steps={metrics['step_counts']['SERVICE']} < {min_service_steps}")

        if expected.get("require_positive_mapf_delay"):
            optimizer_makespan = float(((trace or {}).get("optimizer") or {}).get("estimated_makespan_ms") or 0.0)
            final_makespan = float(plan.get("makespan_ms") or 0.0)
            if final_makespan <= optimizer_makespan:
                errors.append(
                    f"MAPF delay is not positive: plan={final_makespan}, optimizer={optimizer_makespan}"
                )

        for operation in plan.get("logical_operations", []) or []:
            operation_status = str(operation.get("status") or "").upper()
            if operation_status in PLAN_TERMINAL_NONEXECUTION_STATUSES:
                continue
            if not operation.get("task_ids"):
                errors.append(f"{operation.get('operation_id')} has no task_ids")
            if not operation.get("assigned_robot_id"):
                errors.append(f"{operation.get('operation_id')} has no assigned_robot_id")

    if expected.get("require_all_trace_checks"):
        if not trace:
            errors.append("trace is required but missing")
        else:
            checks = trace.get("checks") or {}

            # These contracts are common to both Rule and Agent formulation.
            for key in COMMON_TRACE_CHECK_KEYS:
                if checks.get(key) is not True:
                    errors.append(f"trace check {key}={checks.get(key)!r}")

            final_route = str(response.get("final_route") or "")
            structured_validator_ran = "structured_key_validator" in trace_nodes
            structured_keys_value = checks.get("structured_keys_valid")

            if final_route == "RULE_FORMULATION" or structured_validator_ran:
                # Rule fast path must execute and pass the exact-ID validator.
                if structured_keys_value is not True:
                    errors.append(
                        "trace check structured_keys_valid="
                        f"{structured_keys_value!r} for Rule formulation"
                    )
            elif final_route == "AGENT_FORMULATION":
                # Agent path does not execute structured_key_validator.  None is
                # expected/N/A.  A concrete False still indicates a real failure.
                if structured_keys_value is False:
                    errors.append(
                        "trace check structured_keys_valid=False on Agent formulation"
                    )
            elif structured_keys_value is False:
                # Defensive handling for any future formulation route.
                errors.append("trace check structured_keys_valid=False")

    if expected.get("require_live_repository") and trace:
        repository = trace.get("repository") or {}
        if repository.get("repository_type") != "LiveWarehouseRepository":
            errors.append(f"repository_type={repository.get('repository_type')!r}")
        source_manifest = repository.get("source_manifest") or {}
        required_sources = expected.get("required_repository_sources") or {
            "route_nodes": "neo4j_snapshot",
            "route_edges": "neo4j_snapshot",
            "racks": "postgres_snapshot",
            "robots": "redis_live",
        }
        for key, expected_source in required_sources.items():
            if source_manifest.get(key) != expected_source:
                errors.append(
                    f"repository source {key}={source_manifest.get(key)!r}; expected {expected_source!r}"
                )

    if isinstance(plan, dict) and backend == "cuopt" and trace:
        optimizer = trace.get("optimizer") or {}
        if optimizer.get("backend") != "cuopt":
            errors.append(f"optimizer backend={optimizer.get('backend')!r}, expected 'cuopt'")
        if optimizer.get("status") != "success":
            errors.append(f"optimizer status={optimizer.get('status')!r}, expected 'success'")
        provider = optimizer.get("optimizer")
        if provider != "nvidia-cuopt":
            errors.append(f"optimizer provider={provider!r}, expected 'nvidia-cuopt'")

    expected_constraints = expected.get("expected_constraints") or {}
    if expected_constraints:
        normalized = (response.get("normalized_request") or {})
        # The compact response does not expose normalized_request, therefore
        # this assertion is completed by the stage contract when debug output
        # is available.  Keep a clear error only when a debug-aware caller has
        # already injected the value into the response envelope.
        constraints = normalized.get("constraints") if isinstance(normalized, dict) else None
        if isinstance(constraints, dict):
            for key in ("objective_profile", "reserve_robot_count"):
                if key in expected_constraints and constraints.get(key) != expected_constraints[key]:
                    errors.append(
                        f"constraint {key}={constraints.get(key)!r}; expected {expected_constraints[key]!r}"
                    )

    return errors


@dataclass
class RunArtifacts:
    scenario_id: str
    repeat_index: int
    output_dir: Path
    request: dict[str, Any]
    response: dict[str, Any]
    trace: dict[str, Any] | None
    debug: dict[str, Any] | None
    metrics: dict[str, Any]
    assertion_errors: list[str]


def _md_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_scenario_report(output_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        f"# {summary.get('scenario_id')} — {summary.get('title')}",
        "",
        f"- Scenario pack: `{PACK_VERSION}`",
        f"- Target LARO: `{LARO_TARGET_VERSION}`",
        f"- Backend: `{summary.get('backend')}`",
        f"- Status: **{summary.get('status')}**",
        f"- Repeat: **{summary.get('repeat')}**",
        f"- Deterministic signature: **{summary.get('deterministic_signature')}**",
        "",
        "## Runs",
        "",
        "| Run | Status | Route | Provider | HTTP ms | LLM ms | LLM calls | Solver ms | MAPF/Plan ms | Makespan ms | Robots | Steps | Errors |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in summary.get("runs") or []:
        latency = run.get("latency") or {}
        lines.append(
            "| {repeat_index} | {status} | {final_route} | {provider} | {http_total_ms} | "
            "{llm_ms} | {llm_call_count} | {solver_ms} | {mapf_and_plan_ms} | "
            "{makespan_ms} | {robot_count} | {step_count} | {error_count} |".format(
                repeat_index=_md_escape(run.get("repeat_index")),
                status=_md_escape(run.get("status")),
                final_route=_md_escape(run.get("final_route")),
                provider=_md_escape(run.get("provider")),
                http_total_ms=_md_escape(latency.get("http_total_ms")),
                llm_ms=_md_escape(latency.get("llm_ms")),
                llm_call_count=_md_escape(latency.get("llm_call_count")),
                solver_ms=_md_escape(latency.get("solver_ms")),
                mapf_and_plan_ms=_md_escape(latency.get("mapf_and_plan_ms")),
                makespan_ms=_md_escape(run.get("makespan_ms")),
                robot_count=_md_escape(run.get("robot_count")),
                step_count=_md_escape(run.get("step_count")),
                error_count=_md_escape(len(run.get("assertion_errors") or [])),
            )
        )
    lines.extend(["", "## Assertion errors", ""])
    errors = summary.get("assertion_errors") or []
    if errors:
        lines.extend(f"- {value}" for value in errors)
    else:
        lines.append("- None")
    (output_dir / "scenario_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_suite_reports(output_dir: Path, summary: dict[str, Any]) -> None:
    save_json(output_dir / "suite_summary.json", summary)
    records = list(summary.get("records") or [])
    columns = [
        "scenario_id",
        "repeat_index",
        "status",
        "final_route",
        "backend",
        "provider",
        "http_total_ms",
        "llm_ms",
        "llm_call_count",
        "db_context_ms",
        "formulation_and_payload_ms",
        "solver_ms",
        "mapf_and_plan_ms",
        "makespan_ms",
        "robot_count",
        "step_count",
        "total_distance_m",
        "assertion_error_count",
        "output_dir",
    ]
    with (output_dir / "suite_summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key) for key in columns})

    lines = [
        f"# LARO v4.1 Native Plan Complex Suite — {summary.get('suite_id')}",
        "",
        f"- Scenario pack: `{PACK_VERSION}`",
        f"- Target LARO: `{LARO_TARGET_VERSION}`",
        f"- Backend: `{summary.get('backend')}`",
        f"- Scenarios: **{summary.get('scenario_count')}**",
        f"- Runs: **{summary.get('run_count')}**",
        f"- PASS: **{summary.get('pass_count')}**",
        f"- FAIL: **{summary.get('fail_count')}**",
        f"- Total wall time: **{summary.get('total_wall_ms')} ms**",
        "",
        "## Scenario summary",
        "",
        "| Scenario | Category | Status | Runs | Assertion errors |",
        "|---|---|---|---:|---|",
    ]
    for scenario in summary.get("scenario_summaries") or []:
        errors = scenario.get("assertion_errors") or []
        lines.append(
            f"| {_md_escape(scenario.get('scenario_id'))} | {_md_escape(scenario.get('category'))} | "
            f"{_md_escape(scenario.get('status'))} | {len(scenario.get('runs') or [])} | "
            f"{_md_escape('; '.join(str(value) for value in errors))} |"
        )

    lines.extend(
        [
            "",
            "## Run metrics",
            "",
            "| Scenario | Run | Status | Route | Provider | HTTP ms | LLM ms | Calls | DB/Context ms | Solver ms | MAPF/Plan ms | Makespan ms | Errors |",
            "|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for record in records:
        lines.append(
            "| {scenario_id} | {repeat_index} | {status} | {final_route} | {provider} | "
            "{http_total_ms} | {llm_ms} | {llm_call_count} | {db_context_ms} | "
            "{solver_ms} | {mapf_and_plan_ms} | {makespan_ms} | {assertion_error_count} |".format(
                **{
                    key: _md_escape(record.get(key))
                    for key in (
                        "scenario_id",
                        "repeat_index",
                        "status",
                        "final_route",
                        "provider",
                        "http_total_ms",
                        "llm_ms",
                        "llm_call_count",
                        "db_context_ms",
                        "solver_ms",
                        "mapf_and_plan_ms",
                        "makespan_ms",
                        "assertion_error_count",
                    )
                }
            )
        )

    lines.extend(["", "## Slowest LLM calls", ""])
    llm_nodes: list[dict[str, Any]] = []
    for record in records:
        for node in record.get("llm_nodes") or []:
            llm_nodes.append({"scenario_id": record.get("scenario_id"), **node})
    llm_nodes.sort(key=lambda value: float(value.get("duration_ms") or 0.0), reverse=True)
    if llm_nodes:
        lines.extend(["| Scenario | Node | Duration ms |", "|---|---|---:|"])
        for value in llm_nodes[:30]:
            lines.append(
                f"| {_md_escape(value.get('scenario_id'))} | {_md_escape(value.get('node_name'))} | "
                f"{_md_escape(value.get('duration_ms'))} |"
            )
    else:
        lines.append("No LLM nodes were recorded.")

    (output_dir / "suite_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def archive_directory(directory: Path) -> Path:
    archive_path = directory.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory.parent))
    return archive_path

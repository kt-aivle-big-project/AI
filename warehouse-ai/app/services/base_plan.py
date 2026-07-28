"""Persisted planning results used as safe INSERT/REPLAN base plans."""

from __future__ import annotations

import json
from typing import Any


PASS_DECISIONS = {"PASS", "PASS_WITH_WARNING"}


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def base_plan_from_evidence(
    evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Convert one append-only simulation_run result into an optimizer base.

    Only results accepted by the Verification Agent are eligible.  The result
    remains a candidate plan; this function never activates it.
    """

    if not evidence:
        return None
    output = _mapping(evidence.get("output_payload"))
    verification = _mapping(output.get("verification_decision"))
    if verification.get("decision") not in PASS_DECISIONS:
        return None
    cuopt_plan = _mapping(output.get("cuopt_plan"))
    if not cuopt_plan.get("scheduled_tasks"):
        return None
    interpretation = _mapping(output.get("interpretation"))
    plan_version = evidence.get("plan_version") or output.get(
        "current_plan_version"
    )
    if not plan_version:
        return None
    execution_mode = str(interpretation.get("execution_mode") or "PLAN_ONLY")
    return {
        "plan_version": str(plan_version),
        "command_id": evidence.get("command_id"),
        "scope": _mapping(output.get("scope")),
        "required_tasks": list(output.get("required_tasks") or []),
        "cuopt_plan": cuopt_plan,
        "collision_plan": _mapping(output.get("collision_plan")),
        "task_dependencies": list(interpretation.get("task_dependencies") or []),
        "scheduled_task_constraints": list(
            interpretation.get("scheduled_task_constraints") or []
        ),
        "same_robot_groups": list(interpretation.get("same_robot_groups") or []),
        "ready_task_ids": list(output.get("ready_task_ids") or []),
        "waiting_task_ids": list(output.get("waiting_task_ids") or []),
        "blocked_task_ids": list(output.get("blocked_task_ids") or []),
        "execution_mode": execution_mode,
        "candidate_plan": execution_mode in {"PLAN_ONLY", "SIMULATE_ONLY"},
        "base_plan_is_simulated": execution_mode == "SIMULATE_ONLY",
        "reference_time": output.get("reference_time"),
        "activated_at": output.get("reference_time"),
    }


def active_plan_base(active_plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not active_plan or not (active_plan.get("cuopt_plan") or {}).get(
        "scheduled_tasks"
    ):
        return None
    result = dict(active_plan)
    result["base_plan_is_simulated"] = False
    result["candidate_plan"] = False
    result["execution_mode"] = "EXECUTE"
    return result

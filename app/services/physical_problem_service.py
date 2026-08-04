"""Deterministic profiling and rule-vs-global route resolution for v9."""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.schemas import (
    CuOptPayload,
    MapContext,
    MissionIntent,
    PhysicalProblemProfile,
    PlanningRouteResolution,
)
from app.services.optimization_service import OneToOneRuleOptimizer
from app.services.mapf_service import PrioritizedSIPPPlanner


class PhysicalProblemProfiler:
    """Measure the one-to-one baseline before trusting an LLM route recommendation."""

    def profile(
        self,
        *,
        payload: CuOptPayload,
        map_context: MapContext,
        node_types: dict[str, str],
    ) -> tuple[PhysicalProblemProfile, object, object, object]:
        """Return profile plus baseline result, expansion, and schedule.

        The baseline intentionally allows partial assignment so it can quantify
        exactly how many task pairs a one-robot/one-task rule would defer.
        """

        baseline_result = OneToOneRuleOptimizer().solve(payload, allow_partial=True)
        expansion, schedule = PrioritizedSIPPPlanner().plan(
            payload=payload,
            result=baseline_result,
            map_context=map_context,
            node_types=node_types,
        )
        waits = [
            step.end_at_ms - step.start_at_ms
            for route in schedule.routes
            for step in route.steps
            if step.step_type == "WAIT"
        ]
        pair_count = len(payload.task_data.pickup_and_delivery_pairs)
        unassigned_pair_count = len(baseline_result.unassigned_task_ids) // 2
        reasons: list[str] = []
        settings = get_settings()
        if pair_count > len(payload.fleet_data.vehicle_ids):
            reasons.append("TASK_COUNT_EXCEEDS_AVAILABLE_ROBOTS")
        if unassigned_pair_count > 0:
            reasons.append("ONE_TO_ONE_BASELINE_DEFERS_TASKS")
        if max(waits, default=0) > settings.global_solver_wait_threshold_ms:
            reasons.append("ONE_TO_ONE_BASELINE_WAIT_EXCEEDS_THRESHOLD")
        profile = PhysicalProblemProfile(
            task_count=pair_count,
            eligible_robot_count=len(payload.fleet_data.vehicle_ids),
            pickup_delivery_pair_count=pair_count,
            baseline_deferred_count=unassigned_pair_count,
            baseline_total_wait_ms=sum(waits),
            baseline_max_wait_ms=max(waits, default=0),
            force_global_solver=bool(reasons),
            force_reasons=reasons,
        )
        return profile, baseline_result, expansion, schedule


class PlanningRouteResolver:
    """Resolve LLM advice against deterministic physical lower bounds."""

    def resolve(
        self,
        *,
        profile: PhysicalProblemProfile,
        intent: MissionIntent | None,
        optimization_backend: str,
    ) -> PlanningRouteResolution:
        """Return the route actually used by the graph."""

        recommendation = intent.planning_route if intent is not None else "RULE"
        overrides: list[str] = []
        resolved = recommendation
        if profile.force_global_solver:
            resolved = "GLOBAL_SOLVER"
            overrides.extend(profile.force_reasons)
        if optimization_backend in {"cuopt", "cuopt_payload_only"}:
            # A caller that explicitly selects cuOpt is asking to exercise the
            # global solver contract, even for a small batch.
            if resolved != "GLOBAL_SOLVER":
                overrides.append("EXPLICIT_GLOBAL_SOLVER_BACKEND")
            resolved = "GLOBAL_SOLVER"
        return PlanningRouteResolution(
            llm_recommended_route=recommendation,
            resolved_route=resolved,
            override_reasons=overrides,
        )

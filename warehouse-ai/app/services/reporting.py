from __future__ import annotations

from typing import Any


UNKNOWN = "확인되지 않음"


def build_report_evidence(state: dict[str, Any]) -> dict[str, Any]:
    command = state.get("command", {})
    interpretation = state.get("interpretation", {})
    supervisor = state.get("supervisor_decision", {})
    verification = state.get("verification_decision", {})
    optimizer_plan = state.get("cuopt_plan", {})
    simulation = state.get("simulation") or state.get("plan_validation", {})
    reservation = state.get("reservation_evidence", {})
    return {
        "user_command": {
            "command_id": command.get("command_id"),
            "text": command.get("text"),
            "requested_execution_mode": command.get("requested_execution_mode"),
            "resolved_execution_mode": interpretation.get("execution_mode"),
            "warehouse_id": command.get("warehouse_id"),
        },
        "supervisor": {
            "decision": supervisor,
            "source": state.get("supervisor_source"),
            "prompt_version": state.get("supervisor_prompt_version"),
            "warnings": state.get("supervisor_warnings", []),
        },
        "verification": {
            "decision": verification,
            "source": state.get("verification_source"),
            "prompt_version": state.get("verification_prompt_version"),
            "evidence": state.get("verification_evidence", []),
            "warnings": state.get("verification_warnings", []),
        },
        "assignments": optimizer_plan.get("scheduled_tasks", []),
        "optimization": {
            "profile": state.get("optimization_problem", {}).get(
                "optimization_profile"
            ),
            "weight_source": state.get("optimization_problem", {}).get(
                "optimization_weight_source"
            ),
            "weights": state.get("optimization_problem", {}).get("weights", {}),
            "task_evidence": state.get("optimization_evidence", []),
            "objective_breakdown": state.get("objective_breakdown", {}),
            "unassigned_task_ids": optimizer_plan.get("unassigned_task_ids", []),
        },
        "routes": state.get("routing_evidence", {}),
        "distance_comparison": state.get("distance_comparison", {}),
        "reservations": reservation,
        "waits": reservation.get("waits", []),
        "simulation": {
            key: simulation.get(key)
            for key in (
                "success",
                "valid",
                "status",
                "total_distance",
                "makespan",
                "tardiness",
                "conflict_count",
                "issues",
                "errors",
                "warnings",
            )
            if key in simulation
        },
        "tardiness_and_warnings": {
            "tardiness_seconds": simulation.get("tardiness"),
            "warnings": list(
                dict.fromkeys(
                    str(value)
                    for value in (
                        list(state.get("warnings", []))
                        + list(simulation.get("warnings", []))
                    )
                    if value
                )
            ),
        },
        "replan_history": state.get("replan_history", []),
        "recommendation_basis": {
            "verification_decision": verification.get("decision"),
            "verification_summary": verification.get("summary"),
            "errors": state.get("errors", []),
        },
    }


def report_evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    optimization = evidence.get("optimization", {})
    routes = evidence.get("routes", {})
    reservations = evidence.get("reservations", {})
    return {
        "assignment_count": len(evidence.get("assignments", [])),
        "optimization_task_count": len(optimization.get("task_evidence", [])),
        "candidate_count": sum(
            int(row.get("candidate_count", 0))
            for row in optimization.get("task_evidence", [])
        ),
        "route_segment_count": int(routes.get("route_segment_count", 0)),
        "vertex_reservation_count": int(
            reservations.get("vertex_reservation_count", 0)
        ),
        "edge_reservation_count": int(
            reservations.get("edge_reservation_count", 0)
        ),
        "wait_count": int(reservations.get("wait_count", 0)),
        "final_conflict_count": reservations.get("final_conflict_count"),
    }


def _lines_or_unknown(rows: list[str]) -> str:
    return "\n".join(rows) if rows else f"- {UNKNOWN}"


def _shown(value: Any) -> Any:
    return UNKNOWN if value is None else value


def deterministic_evidence_report(evidence: dict[str, Any]) -> str:
    command = evidence.get("user_command", {})
    supervisor = evidence.get("supervisor", {})
    verification = evidence.get("verification", {})
    assignments = evidence.get("assignments", [])
    optimization = evidence.get("optimization", {})
    routing = evidence.get("routes", {})
    comparison = evidence.get("distance_comparison", {})
    reservations = evidence.get("reservations", {})
    waits = evidence.get("waits", [])
    simulation = evidence.get("simulation", {})
    tardiness = evidence.get("tardiness_and_warnings", {})
    replan_history = evidence.get("replan_history", [])
    recommendation = evidence.get("recommendation_basis", {})

    assignment_lines = [
        "- {task_id}: 로봇 {robot_id}, {source_node}→{target_node}, "
        "step {start_time_step}~{end_time_step}, priority {priority}".format(
            **row
        )
        for row in assignments
    ]
    candidate_lines: list[str] = []
    for task in optimization.get("task_evidence", []):
        candidate_lines.append(
            f"- {task.get('task_id')}: 선택 {task.get('selected_robot_id') or UNKNOWN}, "
            f"후보 {task.get('candidate_count', 0)}대"
        )
        for candidate in task.get("candidates", []):
            result = (
                "선택"
                if candidate.get("selected")
                else (
                    f"제외({candidate.get('rejection_reason') or UNKNOWN})"
                    if not candidate.get("feasible")
                    or candidate.get("rejection_reason")
                    else "비선택"
                )
            )
            candidate_lines.append(
                f"  - {candidate.get('robot_id')}: {result}, "
                f"거리 {_shown(candidate.get('distance'))}, "
                f"종료 step {_shown(candidate.get('end_time_step'))}, "
                f"incremental objective {_shown(candidate.get('incremental_objective'))}"
            )

    route_lines: list[str] = []
    for route in routing.get("routes", []):
        route_lines.append(
            f"- 로봇 {route.get('robot_id')}: route distance "
            f"{route.get('route_distance', UNKNOWN)}, segment "
            f"{len(route.get('segments', []))}개"
        )
        route_lines.extend(
            f"  - {segment.get('from_node')}→{segment.get('to_node')} "
            f"step {segment.get('depart_step')}~{segment.get('arrive_step')} "
            f"{segment.get('action')} distance {_shown(segment.get('distance'))} "
            f"edge {segment.get('edge_identifier') or UNKNOWN} "
            f"source {segment.get('source') or UNKNOWN}"
            for segment in route.get("segments", [])
        )

    distance_lines = [
        f"- Optimizer 예상 거리: {_shown(comparison.get('optimizer_estimated_distance'))}",
        f"- Routing 최종 거리: {_shown(comparison.get('routing_final_distance'))}",
        f"- 차이: {_shown(comparison.get('difference'))} "
        f"({_shown(comparison.get('difference_percent'))}%)",
    ] if comparison else []
    distance_lines.extend(
        f"- {row.get('robot_id')}: 예상 {row.get('estimated_distance')}, "
        f"최종 {row.get('final_distance')}, 차이 {row.get('difference')}, "
        f"근거 {row.get('reason_code') or UNKNOWN}"
        for row in comparison.get("robot_differences", [])
    )

    wait_lines = [
        f"- {row.get('robot_id')} / {row.get('task_id') or UNKNOWN}: "
        f"node {row.get('node_id')}, step {row.get('time_step')}, "
        f"reason {row.get('reason') or UNKNOWN}, "
        f"conflict {row.get('conflict_type') or UNKNOWN}, "
        f"resource {row.get('blocked_resource') or UNKNOWN}, "
        f"delay {row.get('added_delay_steps')} step, "
        f"blocked by robot {row.get('blocked_by_robot_id') or UNKNOWN}, "
        f"task {row.get('blocked_by_task_id') or UNKNOWN}"
        for row in waits
    ]
    warning_lines = [f"- {value}" for value in tardiness.get("warnings", [])]
    replan_lines = [
        f"- attempt {row.get('attempt')}: {row.get('scope')}, "
        f"{row.get('previous_plan_version')}→{row.get('new_plan_version')}, "
        f"{row.get('verification_before')}→{row.get('verification_after')}"
        for row in replan_history
    ]

    decision = recommendation.get("verification_decision")
    if decision in {"PASS", "PASS_WITH_WARNING"}:
        recommendation_text = (
            f"- Verification 결정 {decision}: "
            f"{recommendation.get('verification_summary') or UNKNOWN}"
        )
    elif decision:
        recommendation_text = (
            f"- Verification 결정 {decision}에 따른 확인 또는 조치 필요: "
            f"{recommendation.get('verification_summary') or UNKNOWN}"
        )
    else:
        recommendation_text = f"- {UNKNOWN}"

    supervisor_decision = supervisor.get("decision", {})
    verification_decision = verification.get("decision", {})
    sections = [
        "## 1. 사용자 명령\n"
        f"- {command.get('text') or UNKNOWN}\n"
        f"- 요청/해석 모드: {command.get('requested_execution_mode') or UNKNOWN} / "
        f"{command.get('resolved_execution_mode') or UNKNOWN}",
        "## 2. Supervisor 판단\n"
        f"- source: {supervisor.get('source') or UNKNOWN}\n"
        f"- command kind: {supervisor_decision.get('command_kind') or UNKNOWN}\n"
        f"- plan mode: {supervisor_decision.get('plan_mode') or UNKNOWN}\n"
        f"- 요약: {supervisor_decision.get('reasoning_summary') or UNKNOWN}",
        "## 3. Verification 결과\n"
        f"- decision: {verification_decision.get('decision') or UNKNOWN}\n"
        f"- source: {verification.get('source') or UNKNOWN}\n"
        f"- 요약: {verification_decision.get('summary') or UNKNOWN}",
        "## 4. 작업별 로봇 배정\n" + _lines_or_unknown(assignment_lines),
        "## 5. 선택 로봇과 후보 평가 근거\n" + _lines_or_unknown(candidate_lines),
        "## 6. 로봇별 이동 경로\n" + _lines_or_unknown(route_lines),
        "## 7. Optimizer 예상값과 Routing 최종값 비교\n"
        + _lines_or_unknown(distance_lines),
        "## 8. vertex/edge 예약\n"
        f"- vertex 예약: {_shown(reservations.get('vertex_reservation_count'))}\n"
        f"- edge 예약: {_shown(reservations.get('edge_reservation_count'))}\n"
        f"- 우회 횟수: {_shown(reservations.get('reroute_count'))}\n"
        f"- 최종 deterministic 검증 충돌: "
        f"{_shown(reservations.get('final_conflict_count'))}",
        "## 9. WAIT와 우회\n" + _lines_or_unknown(wait_lines),
        "## 10. 시뮬레이션 결과\n"
        f"- valid: {_shown(simulation.get('valid'))}\n"
        f"- status: {_shown(simulation.get('status'))}\n"
        f"- distance: {_shown(simulation.get('total_distance'))}\n"
        f"- makespan step: {_shown(simulation.get('makespan'))}\n"
        f"- 최종 검증 conflict count: {_shown(simulation.get('conflict_count'))}",
        "## 11. tardiness와 경고\n"
        f"- tardiness seconds: {_shown(tardiness.get('tardiness_seconds'))}\n"
        + _lines_or_unknown(warning_lines),
        "## 12. 재계획 이력\n" + _lines_or_unknown(replan_lines),
        "## 13. 최종 권고\n" + recommendation_text,
    ]
    return "\n\n".join(sections)

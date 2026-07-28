from __future__ import annotations

import json
from types import SimpleNamespace

from app.models import FinalReportOutput
from app.planning import nodes
from app.services.reporting import (
    build_report_evidence,
    deterministic_evidence_report,
)


def report_state() -> dict:
    return {
        "command": {
            "command_id": "C-EVIDENCE",
            "warehouse_id": 1,
            "text": "긴급 작업을 시뮬레이션해줘",
            "requested_execution_mode": "SIMULATE_ONLY",
        },
        "interpretation": {
            "command_kind": "PLAN",
            "intent": "DAILY_PLAN",
            "objective": "근거 테스트",
            "execution_mode": "SIMULATE_ONLY",
            "summary": "구조화된 명령",
        },
        "supervisor_decision": {
            "command_kind": "PLAN",
            "execution_mode": "SIMULATE_ONLY",
            "plan_mode": "INITIAL_PLAN",
            "reasoning_summary": "초기 계획",
        },
        "supervisor_source": "deterministic_fallback",
        "supervisor_prompt_version": "supervisor_v1",
        "verification_decision": {
            "decision": "PASS_WITH_WARNING",
            "summary": "검증 경고 1건",
        },
        "verification_source": "deterministic_fallback",
        "verification_prompt_version": "verification_v1",
        "verification_evidence": [{"evidence_id": "E-1", "code": "WARNING"}],
        "scope": {"plan_mode": "INITIAL_PLAN"},
        "cuopt_plan": {
            "scheduled_tasks": [
                {
                    "task_id": "T1",
                    "robot_id": "R1",
                    "source_node": 1,
                    "target_node": 2,
                    "start_time_step": 0,
                    "end_time_step": 3,
                    "priority": 1,
                    "estimated_distance": 4.0,
                    "estimated_energy": 0.2,
                }
            ],
            "unassigned_task_ids": [],
        },
        "optimization_evidence": [
            {
                "task_id": "T1",
                "task_order": 1,
                "priority": 1,
                "selection_mode": "DETERMINISTIC_GREEDY_INSERTION",
                "candidate_count": 2,
                "selected_robot_id": "R1",
                "candidates": [
                    {
                        "task_id": "T1",
                        "robot_id": "R1",
                        "feasible": True,
                        "selected": True,
                        "distance": 4.0,
                        "end_time_step": 3,
                        "incremental_objective": 7.2,
                    },
                    {
                        "task_id": "T1",
                        "robot_id": "R2",
                        "feasible": False,
                        "selected": False,
                        "rejection_reason": "BATTERY_BELOW_MINIMUM",
                    },
                ],
            }
        ],
        "objective_breakdown": {"distance_component": 4.0, "total": 7.2},
        "routing_evidence": {
            "engine": "PRIORITIZED_TIME_ASTAR",
            "route_segment_count": 1,
            "complete": True,
            "issues": [],
            "routes": [
                {
                    "robot_id": "R1",
                    "task_ids": ["T1"],
                    "route_distance": 4.0,
                    "segment_distance": 4.0,
                    "distance_consistent": True,
                    "segments": [
                        {
                            "from_node": 1,
                            "to_node": 2,
                            "depart_step": 0,
                            "arrive_step": 2,
                            "action": "MOVE",
                            "distance": 4.0,
                            "travel_steps": 2,
                            "edge_identifier": "E-12",
                            "source": "INTERNAL_ROUTE_SEARCH",
                        }
                    ],
                }
            ],
        },
        "distance_comparison": {
            "optimizer_estimated_distance": 4.0,
            "routing_final_distance": 4.0,
            "difference": 0.0,
            "difference_percent": 0.0,
            "robot_differences": [
                {
                    "robot_id": "R1",
                    "estimated_distance": 4.0,
                    "final_distance": 4.0,
                    "difference": 0.0,
                    "reason_code": "UNKNOWN",
                }
            ],
        },
        "reservation_evidence": {
            "vertex_reservation_count": 2,
            "edge_reservation_count": 2,
            "wait_count": 0,
            "reroute_count": None,
            "final_conflict_count": 0,
            "waits": [],
        },
        "simulation": {
            "success": True,
            "valid": True,
            "status": "SUCCESS",
            "total_distance": 4.0,
            "makespan": 3,
            "tardiness": 5.0,
            "conflict_count": 0,
            "warnings": ["실제 경고"],
            "errors": [],
            "task_assignments": [],
            "robot_routes": [],
        },
        "collision_plan": {"time_step_seconds": 5, "routes": []},
        "replan_history": [
            {
                "attempt": 1,
                "scope": "LOCAL_REPLAN",
                "previous_plan_version": "P1",
                "new_plan_version": "P2",
                "verification_before": "REPLAN_LOCAL",
                "verification_after": "PASS_WITH_WARNING",
            }
        ],
        "warnings": [],
        "errors": [],
        "final_status": "SIMULATION_SUCCESS",
    }


def test_deterministic_report_uses_evidence_values_and_unknown_marker() -> None:
    evidence = build_report_evidence(report_state())

    answer = deterministic_evidence_report(evidence)

    assert "후보 2대" in answer
    assert "BATTERY_BELOW_MINIMUM" in answer
    assert "E-12" in answer
    assert "tardiness seconds: 5.0" in answer
    assert "priority 1" in answer
    assert "최종 deterministic 검증 충돌: 0" in answer
    assert "충돌 회피 성공" not in answer
    assert "확인되지 않음" in answer
    assert "LOCAL_REPLAN" in answer


class CapturingReportLlm:
    def __init__(self) -> None:
        self.payload = None

    def with_structured_output(self, _schema, **_kwargs):
        return self

    def invoke(self, messages):
        self.payload = json.loads(messages[-1].content)
        return FinalReportOutput(
            answer=(
                "T1 작업은 R1 로봇에 배정되었습니다. "
                "시스템 기준 이동거리 4, 납기 지연 5초, 경로 충돌 없음입니다."
            )
        )


def test_llm_receives_only_deterministic_user_report_payload(monkeypatch) -> None:
    state = report_state()
    capture = CapturingReportLlm()
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            report_with_llm=True,
            openai_api_key="external-test-key",
            time_step_seconds=5,
        ),
    )
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: capture)

    update = nodes.generate_final_report_node(state)

    assert capture.payload["report_detail_level"] == "STANDARD"
    assert capture.payload["user_report_summary"] == update["user_report_summary"]
    assert "debug_evidence" not in capture.payload
    assert "snapshot" not in capture.payload
    assert "optimization_problem" not in capture.payload
    assert "user_command" not in capture.payload
    assert update["report_source"] == "llm"
    assert "T1 작업은 R1" in update["answer"]


def test_llm_failure_uses_deterministic_template(monkeypatch) -> None:
    state = report_state()
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            report_with_llm=True,
            openai_api_key="external-test-key",
            time_step_seconds=5,
        ),
    )
    monkeypatch.setattr(
        nodes,
        "build_supervisor_llm",
        lambda: (_ for _ in ()).throw(RuntimeError("report unavailable")),
    )

    update = nodes.generate_final_report_node(state)

    assert update["report_source"] == "deterministic_template"
    assert "총 이동거리: 4 m" in update["answer"]
    assert "command_id" not in update["answer"]
    assert any(row["node"] == "report_template_fallback_used" for row in update["trace"])
    assert any(
        "템플릿" in warning
        for warning in update["report_generation_warnings"]
    )
    assert update["response"]["warnings"] == state["warnings"]
    assert update["response"]["report_generation_warnings"] == (
        update["report_generation_warnings"]
    )


def test_llm_fact_validation_fallback_is_not_a_user_warning(monkeypatch) -> None:
    state = report_state()
    capture = CapturingReportLlm()
    capture.invoke = lambda _messages: FinalReportOutput(
        answer="핵심 사실을 누락한 보고서"
    )
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            report_with_llm=True,
            openai_api_key="external-test-key",
            time_step_seconds=5,
        ),
    )
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: capture)

    update = nodes.generate_final_report_node(state)
    response = update["response"]

    assert update["report_source"] == "deterministic_template"
    assert response["status"] == state["final_status"]
    assert response["warnings"] == state["warnings"]
    assert not any("LLM 보고서" in value for value in response["warnings"])
    assert any(
        "결정론적 summary" in value
        for value in response["report_generation_warnings"]
    )
    assert not any(
        "LLM 보고서" in row["message"]
        for row in response["user_report_summary"]["warnings"]
    )
    assert any(
        row["node"] == "report_template_fallback_used"
        for row in response["trace"]
    )


def test_response_contains_only_evidence_summary_not_full_evidence(monkeypatch) -> None:
    state = report_state()
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            report_with_llm=False,
            openai_api_key="",
            time_step_seconds=5,
        ),
    )

    update = nodes.generate_final_report_node(state)

    response = update["response"]
    assert response["evidence_summary"]["candidate_count"] == 2
    assert "optimization_evidence" not in response
    assert "routing_evidence" not in response
    assert "reservation_evidence" not in response
    assert "report_evidence" not in response

"""Deterministic What-if scenario generation, execution, and comparison."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.models import (
    NaturalLanguageCommand,
    OptimizationWeights,
    ScenarioComparisonRequest,
    ScenarioComparisonResult,
    ScenarioDefinition,
    ScenarioResult,
)
from app.planning.graph import run_planning


SYSTEM_MAX_SCENARIOS = 6
PASS_DECISIONS = {"PASS", "PASS_WITH_WARNING"}
FAIL_DECISIONS = {"FAIL", "CLARIFICATION_REQUIRED"}
METRIC_DIRECTIONS = {
    "robot_count": "LOWER_IS_BETTER",
    "assigned_task_count": "HIGHER_IS_BETTER",
    "unassigned_task_count": "LOWER_IS_BETTER",
    "total_distance": "LOWER_IS_BETTER",
    "makespan_seconds": "LOWER_IS_BETTER",
    "tardiness_seconds": "LOWER_IS_BETTER",
    "energy": "LOWER_IS_BETTER",
    "conflict_count": "LOWER_IS_BETTER",
    "wait_count": "LOWER_IS_BETTER",
    "plan_change_count": "LOWER_IS_BETTER",
    "replan_attempts": "LOWER_IS_BETTER",
    "verification_decision": "PASS_ORDER",
}
PRIORITY_METRICS = {
    "MINIMIZE_DISTANCE": "total_distance",
    "MINIMIZE_TARDINESS": "tardiness_seconds",
    "MINIMIZE_MAKESPAN": "makespan_seconds",
    "MINIMIZE_ENERGY": "energy",
    "MINIMIZE_ROBOTS": "robot_count",
    "MINIMIZE_PLAN_CHANGE": "plan_change_count",
}
PRIORITY_LABELS = {
    "MINIMIZE_DISTANCE": ("거리 우선", "이동거리 최소화", "최단 거리"),
    "MINIMIZE_TARDINESS": ("납기 우선", "마감 준수", "지연 최소화"),
    "MINIMIZE_MAKESPAN": ("완료시간 우선", "완료 시간 우선", "최대한 빨리"),
    "MINIMIZE_ENERGY": ("에너지 우선", "에너지 최소화"),
    "MINIMIZE_ROBOTS": ("로봇 수 최소화", "최소 로봇"),
    "MINIMIZE_PLAN_CHANGE": ("계획 변경 최소화", "기존 계획 유지"),
}
PRIORITY_WEIGHT_FIELDS = {
    "MINIMIZE_DISTANCE": "total_distance",
    "MINIMIZE_TARDINESS": "tardiness",
    "MINIMIZE_MAKESPAN": "makespan",
    "MINIMIZE_ENERGY": "energy",
    "MINIMIZE_ROBOTS": "robot_activation",
    "MINIMIZE_PLAN_CHANGE": "plan_change",
}


class ScenarioComparisonLimitError(ValueError):
    pass


def _focused_weights(priority: str | None) -> dict[str, float]:
    values = OptimizationWeights().model_dump()
    field = PRIORITY_WEIGHT_FIELDS.get(priority or "")
    if field:
        values[field] *= 5.0
    return values


def _scenario(
    index: int,
    name: str,
    **values: Any,
) -> ScenarioDefinition:
    priority = values.get("optimization_priority")
    if priority and not values.get("optimization_weights"):
        values["optimization_weights"] = _focused_weights(priority)
    return ScenarioDefinition(
        scenario_id=f"scenario-{index}",
        name=name,
        **values,
    )


def _explicit_recommendation_goal(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if not any(token in normalized for token in ("추천", "선택", "더 좋은")):
        return None
    for priority, labels in PRIORITY_LABELS.items():
        if any(
            f"{label} 기준" in normalized
            or f"{label}으로 추천" in normalized
            for label in labels
        ):
            return priority
    direct = (
        ("MINIMIZE_DISTANCE", ("거리 기준", "거리가 짧")),
        ("MINIMIZE_TARDINESS", ("납기 기준", "지연이 적")),
        ("MINIMIZE_MAKESPAN", ("완료시간 기준", "완료 시간이 짧")),
        ("MINIMIZE_ENERGY", ("에너지 기준", "에너지가 적")),
        ("MINIMIZE_ROBOTS", ("로봇 수 기준", "로봇이 적")),
        ("MINIMIZE_PLAN_CHANGE", ("변경 수 기준", "변경이 적")),
    )
    return next(
        (priority for priority, phrases in direct if any(p in normalized for p in phrases)),
        None,
    )


def parse_scenario_definitions(
    text: str,
) -> tuple[list[ScenarioDefinition], str | None]:
    """Create only scenarios explicitly present in the Korean command."""

    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    scenarios: list[ScenarioDefinition] = []

    robot_counts = [int(value) for value in re.findall(r"(\d+)\s*대", normalized)]
    for count in dict.fromkeys(robot_counts):
        scenarios.append(
            _scenario(len(scenarios) + 1, f"로봇 {count}대", robot_limit=count)
        )

    priorities = [
        priority
        for priority, labels in PRIORITY_LABELS.items()
        if any(label in normalized for label in labels)
    ]
    if len(priorities) >= 2 and not scenarios:
        for priority in priorities:
            label = next(
                label
                for label in PRIORITY_LABELS[priority]
                if label in normalized
            )
            scenarios.append(
                _scenario(
                    len(scenarios) + 1,
                    label,
                    optimization_priority=priority,
                )
            )

    robot_match = re.search(r"\br\s*[-_]?\s*0*(\d+)", normalized, re.I)
    if (
        robot_match
        and "포함" in normalized
        and "제외" in normalized
        and not scenarios
    ):
        robot_id = f"R-{int(robot_match.group(1)):02d}"
        scenarios = [
            _scenario(1, f"{robot_id} 포함"),
            _scenario(2, f"{robot_id} 제외", excluded_robot_ids=[robot_id]),
        ]

    edge_match = re.search(r"(?:통로|간선)\s*([a-z0-9_-]+)", normalized)
    if (
        edge_match
        and any(token in normalized for token in ("폐쇄", "차단"))
        and any(token in normalized for token in ("정상", "전후"))
        and not scenarios
    ):
        edge_id = edge_match.group(1)
        scenarios = [
            _scenario(1, "정상 상태"),
            _scenario(2, f"통로 {edge_id} 폐쇄", excluded_edge_ids=[edge_id]),
        ]

    if "기존 계획" in normalized and "새 계획" in normalized and not scenarios:
        scenarios = [
            _scenario(
                1,
                "기존 계획 유지",
                optimization_priority="MINIMIZE_PLAN_CHANGE",
            ),
            _scenario(2, "새 계획"),
        ]

    if "고장" in normalized and any(token in normalized for token in ("전후", "전과", "정상")) and not scenarios:
        robot_id = (
            f"R-{int(robot_match.group(1)):02d}" if robot_match else None
        )
        if robot_id:
            scenarios = [
                _scenario(1, "고장 전"),
                _scenario(
                    2,
                    "고장 후",
                    excluded_robot_ids=[robot_id],
                    hypothetical_events=[
                        {
                            "event_type": "ROBOT_FAILURE",
                            "target_ids": [robot_id],
                            "parameters": {},
                        }
                    ],
                ),
            ]

    return scenarios, _explicit_recommendation_goal(normalized)


def _signature(scenario: ScenarioDefinition) -> str:
    payload = scenario.model_dump(
        exclude={"scenario_id", "name", "description"},
        mode="json",
    )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def normalize_scenarios(
    scenarios: list[ScenarioDefinition],
    *,
    limit: int,
) -> list[ScenarioDefinition]:
    effective_limit = min(limit, SYSTEM_MAX_SCENARIOS)
    if len(scenarios) > effective_limit:
        raise ScenarioComparisonLimitError(
            f"시나리오는 최대 {effective_limit}개까지 비교할 수 있습니다."
        )
    unique: list[ScenarioDefinition] = []
    seen: set[str] = set()
    for scenario in scenarios:
        key = _signature(scenario)
        if key in seen:
            continue
        seen.add(key)
        unique.append(
            scenario.model_copy(
                update={"scenario_id": f"scenario-{len(unique) + 1}"}
            )
        )
    return unique


def _request_key(request: ScenarioComparisonRequest) -> str:
    if request.idempotency_key:
        source = request.idempotency_key.strip()
    else:
        payload = request.model_dump(
            mode="json",
            exclude={"comparison_id", "idempotency_key"},
        )
        source = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _metric_value(result: ScenarioResult, metric: str) -> Any:
    return getattr(result, metric)


def compare_metrics(results: list[ScenarioResult]) -> list[dict[str, Any]]:
    if len(results) < 2:
        return []
    baseline = results[0]
    differences: list[dict[str, Any]] = []
    pass_order = {"PASS": 0, "PASS_WITH_WARNING": 1, "REPLAN_LOCAL": 2, "REPLAN_GLOBAL": 3, "CLARIFICATION_REQUIRED": 4, "FAIL": 5}
    for other in results[1:]:
        for metric, direction in METRIC_DIRECTIONS.items():
            left = _metric_value(baseline, metric)
            right = _metric_value(other, metric)
            if left is None or right is None:
                absolute = percentage = better = None
            elif direction == "PASS_ORDER":
                absolute = percentage = None
                left_rank = pass_order.get(str(left), 99)
                right_rank = pass_order.get(str(right), 99)
                better = (
                    baseline.scenario_id
                    if left_rank < right_rank
                    else other.scenario_id
                    if right_rank < left_rank
                    else None
                )
            else:
                absolute = abs(float(right) - float(left))
                percentage = (
                    None
                    if float(left) == 0.0
                    else round(absolute / abs(float(left)) * 100.0, 6)
                )
                if float(left) == float(right):
                    better = None
                elif direction == "LOWER_IS_BETTER":
                    better = baseline.scenario_id if float(left) < float(right) else other.scenario_id
                else:
                    better = baseline.scenario_id if float(left) > float(right) else other.scenario_id
            differences.append(
                {
                    "metric": metric,
                    "scenario_a_id": baseline.scenario_id,
                    "scenario_b_id": other.scenario_id,
                    "scenario_a": left,
                    "scenario_b": right,
                    "absolute_difference": (
                        round(absolute, 6) if absolute is not None else None
                    ),
                    "percentage_difference": percentage,
                    "better_scenario_id": better,
                    "direction": direction,
                }
            )
    return differences


def recommend_scenario(
    results: list[ScenarioResult],
    goal: str | None,
) -> tuple[str | None, list[str], list[str]]:
    eligible = [
        row
        for row in results
        if row.valid and row.verification_decision in PASS_DECISIONS
    ]
    tradeoffs = [
        (
            f"{row.scenario_id}: 로봇 {row.robot_count}대, "
            f"거리 {row.total_distance}, 완료 {row.makespan_seconds}초, "
            f"지연 {row.tardiness_seconds}초, 미배정 {row.unassigned_task_count}건"
        )
        for row in results
    ]
    metric = PRIORITY_METRICS.get(goal or "")
    if not metric or not eligible:
        return None, [], tradeoffs

    def key(row: ScenarioResult) -> tuple[Any, ...]:
        value = _metric_value(row, metric)
        return (
            row.unassigned_task_count > 0,
            row.unassigned_task_count,
            (row.conflict_count or 0) > 0,
            row.conflict_count or 0,
            float("inf") if value is None else value,
            row.scenario_id,
        )

    selected = min(eligible, key=key)
    evidence = [
        f"FAIL/CLARIFICATION 시나리오를 제외한 {len(eligible)}개를 비교했습니다.",
        f"미배정 작업과 충돌을 우선 최소화한 뒤 {metric} 기준을 적용했습니다.",
        f"선택 시나리오의 {metric}={_metric_value(selected, metric)}입니다.",
    ]
    return selected.scenario_id, evidence, tradeoffs


def scenario_result_from_response(
    scenario: ScenarioDefinition,
    response: dict[str, Any],
) -> ScenarioResult:
    simulation = response.get("simulation") or response.get("plan_validation") or {}
    plan = response.get("optimization_plan") or {}
    collision = response.get("collision_plan") or {}
    scheduled = plan.get("scheduled_tasks") or []
    unassigned = plan.get("unassigned_task_ids") or []
    metrics = simulation.get("metrics") or {}
    metadata = plan.get("metadata") or {}
    decision = (response.get("verification_decision") or {}).get("decision") or "FAIL"
    time_step_seconds = int(
        metrics.get("time_step_seconds")
        or collision.get("time_step_seconds")
        or metadata.get("time_step_seconds")
        or 1
    )
    makespan_seconds = metrics.get("makespan_seconds")
    if makespan_seconds is None and simulation.get("makespan") is not None:
        makespan_seconds = int(simulation["makespan"]) * time_step_seconds
    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        simulation_id=response.get("simulation_id"),
        command_id=str(response.get("command_id") or ""),
        valid=bool(simulation.get("valid")) and decision in PASS_DECISIONS,
        verification_decision=decision,
        robot_count=len({str(row.get("robot_id")) for row in scheduled}),
        assigned_task_count=len(scheduled),
        unassigned_task_count=len(unassigned),
        total_distance=(
            float(simulation["total_distance"])
            if simulation.get("total_distance") is not None
            else None
        ),
        makespan_seconds=(
            int(makespan_seconds) if makespan_seconds is not None else None
        ),
        tardiness_seconds=(
            int(metrics.get("tardiness_seconds", simulation.get("tardiness", 0)))
            if simulation
            else None
        ),
        energy=(float(metadata["energy"]) if metadata.get("energy") is not None else None),
        conflict_count=(
            int(simulation.get("conflict_count", 0)) if simulation else None
        ),
        wait_count=len((collision.get("metadata") or {}).get("wait_evidence") or []),
        plan_change_count=(
            int(metadata.get("plan_changes", 0)) if metadata else None
        ),
        replan_attempts=int(response.get("replan_attempt") or 0),
        warnings=[str(value) for value in response.get("warnings", [])],
        failure_reasons=[
            str(value)
            for value in [
                *response.get("errors", []),
                *simulation.get("errors", []),
            ]
            if value
        ],
    )


class ScenarioComparisonService:
    def __init__(
        self,
        services: Any,
        *,
        planner: Callable[[NaturalLanguageCommand], dict[str, Any]] = run_planning,
    ):
        self.services = services
        self.planner = planner

    @staticmethod
    def _stage(stage_name: str, **details: Any) -> dict[str, Any]:
        return {
            "node_name": stage_name,
            "status": "FAILED" if stage_name == "SCENARIO_FAILED" else "COMPLETED",
            "message": None,
            "details": details,
            "created_at": datetime.now(UTC),
        }

    def _persist_stages(self, command_id: str, stages: list[dict[str, Any]]) -> None:
        repository = self.services.postgres
        if not hasattr(repository, "persist_stage_logs"):
            return
        numbered = [
            {**stage, "sequence": index, "attempt": 1}
            for index, stage in enumerate(stages, start=1)
        ]
        try:
            repository.persist_stage_logs(command_id, numbered)
        except Exception:
            # 감사 로그 실패가 이미 계산된 비교 결과를 뒤집지 않게 한다.
            return

    def _scenario_command(
        self,
        request: ScenarioComparisonRequest,
        comparison_id: str,
        scenario: ScenarioDefinition,
    ) -> NaturalLanguageCommand:
        command_id = str(
            uuid5(NAMESPACE_URL, f"{comparison_id}:{scenario.scenario_id}")
        )
        return NaturalLanguageCommand(
            command_id=command_id,
            warehouse_id=request.warehouse_id,
            text=(
                f"What-if 시나리오 '{scenario.name}'로 오늘 미완료 작업을 "
                "실제 반영하지 말고 시뮬레이션해줘"
            ),
            requested_execution_mode="SIMULATE_ONLY",
            source="SYSTEM_EVENT",
            conversation_id=str(
                uuid5(NAMESPACE_URL, f"{comparison_id}:{scenario.scenario_id}:conversation")
            ),
            scenario_definition=scenario.model_dump(mode="json"),
        )

    def execute(self, request: ScenarioComparisonRequest) -> dict[str, Any]:
        parsed_goal: str | None = None
        scenarios = deepcopy(request.scenarios)
        if not scenarios and request.text:
            scenarios, parsed_goal = parse_scenario_definitions(request.text)
        scenarios = normalize_scenarios(scenarios, limit=request.max_scenarios)
        goal = request.optimization_priority or parsed_goal
        request_key = _request_key(request)
        comparison_id = request.comparison_id or str(
            uuid5(NAMESPACE_URL, f"scenario-comparison:{request_key}")
        )
        command_id = str(uuid5(NAMESPACE_URL, f"comparison-command:{request_key}"))
        repository = self.services.postgres

        if hasattr(repository, "create_or_get_command_history"):
            repository.create_or_get_command_history(
                {
                    "command_id": command_id,
                    "warehouse_id": request.warehouse_id,
                    "requested_execution_mode": "SIMULATE_ONLY",
                    "source": "USER",
                    "original_text": request.text or "structured scenario comparison",
                    "actor_id": None,
                    "status": "PROCESSING",
                    "simulation_id": None,
                    "parent_command_id": None,
                    "received_at": datetime.now(UTC),
                }
            )

        if hasattr(repository, "create_or_get_scenario_comparison"):
            stored = repository.create_or_get_scenario_comparison(
                {
                    "comparison_id": comparison_id,
                    "request_key": request_key,
                    "conversation_id": request.conversation_id,
                    "warehouse_id": request.warehouse_id,
                    "command_id": command_id,
                    "status": "PROCESSING",
                    "request_payload": request.model_dump(mode="json"),
                    "recommendation_summary": {},
                    "created_at": datetime.now(UTC),
                }
            )
            comparison_id = str(stored["comparison_id"])
            command_id = str(stored["command_id"])
            if stored.get("status") in {
                "COMPLETED",
                "PARTIAL_SUCCESS",
                "FAILED",
                "CLARIFICATION_REQUIRED",
            }:
                existing = repository.get_scenario_comparison(comparison_id)
                if existing:
                    existing["duplicate"] = True
                    return existing

        stages = [
            self._stage(
                "SCENARIO_COMPARISON_STARTED",
                comparison_id=comparison_id,
                scenario_count=len(scenarios),
            )
        ]
        if len(scenarios) < 2:
            result = ScenarioComparisonResult(
                comparison_id=comparison_id,
                conversation_id=request.conversation_id,
                warehouse_id=request.warehouse_id,
                status="CLARIFICATION_REQUIRED",
                tradeoffs=["비교할 서로 다른 조건을 두 개 이상 지정해 주세요."],
            )
            self._finish(repository, command_id, result, stages, request)
            return result.model_dump(mode="json")

        results: list[ScenarioResult] = []
        definitions_by_id = {row.scenario_id: row for row in scenarios}
        for scenario in scenarios:
            stages.append(
                self._stage(
                    "SCENARIO_CREATED",
                    comparison_id=comparison_id,
                    scenario_id=scenario.scenario_id,
                    name=scenario.name,
                )
            )
            stages.append(
                self._stage(
                    "SCENARIO_SIMULATION_STARTED",
                    comparison_id=comparison_id,
                    scenario_id=scenario.scenario_id,
                )
            )
            try:
                response = self.planner(
                    self._scenario_command(
                        request,
                        comparison_id,
                        scenario,
                    )
                )
                scenario_result = scenario_result_from_response(scenario, response)
                results.append(scenario_result)
                stage_name = (
                    "SCENARIO_SIMULATION_COMPLETED"
                    if scenario_result.valid
                    else "SCENARIO_FAILED"
                )
                stages.append(
                    self._stage(
                        stage_name,
                        comparison_id=comparison_id,
                        scenario_id=scenario.scenario_id,
                        simulation_id=scenario_result.simulation_id,
                        verification_decision=scenario_result.verification_decision,
                    )
                )
            except Exception as exc:
                scenario_result = ScenarioResult(
                    scenario_id=scenario.scenario_id,
                    command_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"{comparison_id}:{scenario.scenario_id}",
                        )
                    ),
                    valid=False,
                    verification_decision="FAIL",
                    failure_reasons=[str(exc)],
                )
                results.append(scenario_result)
                stages.append(
                    self._stage(
                        "SCENARIO_FAILED",
                        comparison_id=comparison_id,
                        scenario_id=scenario.scenario_id,
                        reason=str(exc),
                    )
                )
            if hasattr(repository, "upsert_scenario_comparison_run"):
                try:
                    repository.upsert_scenario_comparison_run(
                        {
                            "comparison_id": comparison_id,
                            "scenario_id": scenario.scenario_id,
                            "simulation_id": scenario_result.simulation_id,
                            "command_id": scenario_result.command_id,
                            "status": (
                                "COMPLETED" if scenario_result.valid else "FAILED"
                            ),
                            "scenario_definition": scenario.model_dump(mode="json"),
                            "result_summary": scenario_result.model_dump(mode="json"),
                            "created_at": datetime.now(UTC),
                            "completed_at": datetime.now(UTC),
                        }
                    )
                except Exception as exc:
                    scenario_result.warnings.append(
                        f"시나리오 실행 요약 저장 실패: {exc}"
                    )

        metrics = compare_metrics(results)
        stages.append(
            self._stage(
                "SCENARIO_METRICS_COMPARED",
                comparison_id=comparison_id,
                metric_count=len(metrics),
            )
        )
        recommended, recommendation_evidence, tradeoffs = recommend_scenario(
            results,
            goal,
        )
        if recommended:
            stages.append(
                self._stage(
                    "SCENARIO_RECOMMENDATION_CREATED",
                    comparison_id=comparison_id,
                    scenario_id=recommended,
                    optimization_priority=goal,
                )
            )
        success_count = sum(row.valid for row in results)
        status = (
            "COMPLETED"
            if success_count == len(results)
            else "PARTIAL_SUCCESS"
            if success_count
            else "FAILED"
        )
        result = ScenarioComparisonResult(
            comparison_id=comparison_id,
            conversation_id=request.conversation_id,
            warehouse_id=request.warehouse_id,
            scenarios=results,
            comparison_metrics=metrics,
            recommended_scenario_id=recommended,
            recommendation_evidence=recommendation_evidence,
            tradeoffs=tradeoffs,
            status=status,
        )
        stages.append(
            self._stage(
                "SCENARIO_COMPARISON_COMPLETED",
                comparison_id=comparison_id,
                status=status,
                recommended_scenario_id=recommended,
            )
        )
        self._finish(repository, command_id, result, stages, request)
        payload = result.model_dump(mode="json")
        payload["scenario_definitions"] = [
            definitions_by_id[row.scenario_id].model_dump(mode="json")
            for row in results
        ]
        return payload

    def _finish(
        self,
        repository: Any,
        command_id: str,
        result: ScenarioComparisonResult,
        stages: list[dict[str, Any]],
        request: ScenarioComparisonRequest,
    ) -> None:
        payload = result.model_dump(mode="json")
        if hasattr(repository, "finalize_scenario_comparison"):
            repository.finalize_scenario_comparison(
                result.comparison_id,
                status=result.status,
                recommendation_summary=payload,
            )
        self._persist_stages(command_id, stages)
        if hasattr(repository, "update_command_history"):
            repository.update_command_history(
                {
                    "command_id": command_id,
                    "command_type": "SCENARIO_COMPARISON",
                    "resolved_execution_mode": "SIMULATE_ONLY",
                    "status": result.status,
                    "simulation_id": None,
                    "plan_version": None,
                    "completed_at": datetime.now(UTC),
                    "result_summary": {
                        "comparison_id": result.comparison_id,
                        "status": result.status,
                        "scenario_count": len(result.scenarios),
                        "recommended_scenario_id": result.recommended_scenario_id,
                        "conversation_id": request.conversation_id,
                    },
                    "error_summary": (
                        None
                        if result.status not in {"FAILED", "CLARIFICATION_REQUIRED"}
                        else {"tradeoffs": result.tradeoffs}
                    ),
                }
            )

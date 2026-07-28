import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

from app.models import (
    CommandInterpretation,
    NaturalLanguageCommand,
    OptimizationWeights,
    ScopeDecision,
    SimulationIssue,
    SimulationResult,
    SupervisorDecision,
    VerificationDecision,
)
from app.planning import nodes
from app.planning.graph import run_planning


class FakePostgres:
    def __init__(self) -> None:
        self.recorded = 0
        self.snapshot_item_ids: list[str] = []
        self.command_history: dict[str, dict] = {}
        self.stage_logs: dict[str, list[dict]] = {}

    def snapshot(self, warehouse_id: int, _item_ids: list[str]) -> dict:
        self.snapshot_item_ids = list(_item_ids)
        return {
            "inventory": [],
            "robots": [
                {"robot_id": "R1", "node_id": 1, "battery": 90, "status": "IDLE", "max_load": 100, "current_load": 0},
                {"robot_id": "R2", "node_id": 2, "battery": 80, "status": "IDLE", "max_load": 100, "current_load": 0},
                {"robot_id": "R3", "node_id": 4, "battery": 70, "status": "IDLE", "max_load": 100, "current_load": 0},
            ],
            "works": [
                {"work_id": "W1", "status": "NEW", "source_node": 1, "target_node": 3, "priority": 1},
                {"work_id": "W2", "status": "NEW", "source_node": 2, "target_node": 4, "priority": 2},
                {"work_id": "W3", "status": "NEW", "source_node": 4, "target_node": 2, "priority": 3},
            ],
        }

    def record_simulation(self, _state: dict) -> None:
        self.recorded += 1

    def create_or_get_command_history(self, values: dict) -> dict:
        command_id = values["command_id"]
        self.command_history.setdefault(command_id, deepcopy(values))
        return deepcopy(self.command_history[command_id])

    def finalize_command_audit(self, history: dict, stages: list[dict]) -> None:
        command_id = history["command_id"]
        self.command_history.setdefault(command_id, {"command_id": command_id})
        self.command_history[command_id].update(deepcopy(history))
        existing = {
            (row["sequence"], row.get("attempt", 1))
            for row in self.stage_logs.get(command_id, [])
        }
        self.stage_logs.setdefault(command_id, []).extend(
            deepcopy(row)
            for row in stages
            if (row["sequence"], row.get("attempt", 1)) not in existing
        )


class FakeNeo4j:
    def fetch_topology(self, _warehouse_id: int) -> dict:
        return {
            "nodes": [{"node_id": value, "node_type": "AISLE"} for value in (1, 2, 3, 4)],
            "edges": [
                {"from_node": 1, "to_node": 2, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
                {"from_node": 2, "to_node": 3, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
                {"from_node": 3, "to_node": 4, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
                {"from_node": 4, "to_node": 1, "distance": 1, "travel_seconds": 1, "direction": "BOTH"},
            ],
        }

    def validate_node_ids(self, _warehouse_id: int, node_ids: list[int]) -> dict:
        values = sorted({int(value) for value in node_ids if value is not None})
        return {"valid": values, "missing": []}


class FakeRedis:
    def __init__(self) -> None:
        self.activation_count = 0
        self.simulations: dict[str, dict] = {}
        self.simulation_event_count = 0

    def live_snapshot(self, _warehouse_id: int) -> dict:
        return {
            "robots": [],
            "tasks": [],
            "executing_task_ids": [],
            "planned_task_ids": [],
            "active_plan_version": None,
            "active_plan": None,
            "temporary_closures": [],
        }

    def atomic_activate_plan(self, *_args, **_kwargs) -> str:
        self.activation_count += 1
        return "P1"

    def initialize_simulation_session(self, simulation_id: str, snapshot: dict) -> dict:
        if simulation_id not in self.simulations:
            self.simulations[simulation_id] = {
                "simulation_id": simulation_id,
                "inventory": deepcopy(snapshot["sql"]["inventory"]),
                "robots": deepcopy(snapshot["sql"]["robots"]),
                "works": deepcopy(snapshot["sql"]["works"]),
                "checkpoint": "0-0",
            }
        return deepcopy(self.simulations[simulation_id])

    def update_simulation_from_event(self, event) -> dict:
        session = self.simulations[event.simulation_id]
        self.simulation_event_count += 1
        session["checkpoint"] = f"{self.simulation_event_count}-0"
        for robot in session["robots"]:
            if str(robot["robot_id"]) == event.robot_id:
                if event.node_id is not None:
                    robot["node_id"] = event.node_id
                robot["last_event"] = event.event_type
        for work in session["works"]:
            if str(work["work_id"]) == str(event.work_id):
                if event.event_type == "TASK_STARTED":
                    work["status"] = "EXECUTING"
                elif event.event_type == "TASK_COMPLETED":
                    work["status"] = "COMPLETED"
        return deepcopy(session)

    def simulation_snapshot(self, simulation_id: str) -> dict:
        return deepcopy(self.simulations[simulation_id])


class FakeServices:
    def __init__(self) -> None:
        self.postgres = FakePostgres()
        self.neo4j = FakeNeo4j()
        self.redis = FakeRedis()


class FakeSupervisor:
    def __init__(self, max_replan_attempts: int = 1) -> None:
        self.schema = None
        self.max_replan_attempts = max_replan_attempts

    def with_structured_output(self, schema, **_kwargs):
        self.schema = schema
        return self

    def invoke(self, messages):
        if self.schema is CommandInterpretation:
            text = str(messages[-1].content)
            is_query = "조회" in text
            payload = json.loads(messages[-1].content)
            requested_mode = payload["requested_execution_mode"]
            llm_weights = (
                OptimizationWeights(total_distance=99.0, makespan=1.0)
                if requested_mode == "PLAN_ONLY"
                else OptimizationWeights(total_distance=1.0, makespan=99.0)
            )
            return CommandInterpretation(
                command_kind="QUERY" if is_query else "PLAN",
                intent="INVENTORY_QUERY" if is_query else "DAILY_PLAN",
                objective="테스트 명령",
                execution_mode=(
                    requested_mode if requested_mode != "AUTO" else "PLAN_ONLY"
                ),
                optimization_weights=llm_weights,
                summary="테스트",
            )
        if self.schema is SupervisorDecision:
            payload = json.loads(messages[-1].content)
            interpretation = payload["interpretation"]
            mode = interpretation["execution_mode"]
            command_kind = (
                "QUERY"
                if interpretation["command_kind"] == "QUERY"
                else "EXECUTE"
                if mode == "EXECUTE"
                else "PLAN"
            )
            return SupervisorDecision(
                intent=interpretation["intent"],
                command_kind=command_kind,
                execution_mode="PLAN_ONLY" if command_kind == "QUERY" else mode,
                required_tools=["SNAPSHOT"],
                plan_mode="NO_REPLAN" if command_kind == "QUERY" else "INITIAL_PLAN",
                risk_level="HIGH" if mode == "EXECUTE" else "LOW",
                allow_replan=command_kind != "QUERY",
                max_replan_attempts=(
                    self.max_replan_attempts if command_kind != "QUERY" else 0
                ),
                reasoning_summary="테스트 Supervisor 판단",
            )
        if self.schema is VerificationDecision:
            return VerificationDecision(
                decision="PASS",
                requires_replan=False,
                replan_scope="NO_REPLAN",
                confidence=1.0,
                summary="테스트 Verification 판단",
            )
        return ScopeDecision(
            plan_mode="LOCAL_REPLAN",
            optimization_goal="minimum cost",
            reason_summary="테스트",
        )


def fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key="test-key",
        openai_model="test-model",
        report_with_llm=False,
        optimizer_backend="local",
        routing_backend="internal",
        cuopt_url="",
        mapf_url="",
        robot_gateway_url="",
        cuopt_fallback_to_local=True,
        mapf_fallback_to_internal=True,
        request_timeout_seconds=1,
        freeze_horizon_seconds=2,
        max_replan_count=1,
        time_step_seconds=1,
        max_mapf_time_steps=30,
        min_robot_battery=20,
        energy_per_distance=0.05,
    )


def install_fakes(monkeypatch) -> FakeServices:
    services = FakeServices()
    monkeypatch.setattr(nodes, "get_services", lambda: services)
    monkeypatch.setattr(nodes, "get_settings", fake_settings)
    monkeypatch.setattr(nodes, "build_supervisor_llm", FakeSupervisor)
    return services


def trace_nodes(result: dict) -> list[str]:
    return [row["node"] for row in result["trace"]]


def trace_for(result: dict, node_name: str) -> dict:
    return next(row for row in result["trace"] if row["node"] == node_name)


def failed_simulation(code: str, *, task_id: str, robot_id: str) -> SimulationResult:
    message = f"{code}: {robot_id}/{task_id}"
    return SimulationResult(
        success=False,
        valid=False,
        status="FAILED",
        issues=[
            SimulationIssue(
                code=code,
                message=message,
                robot_ids=[robot_id],
                task_ids=[task_id],
            )
        ],
        errors=[message],
    )


def passed_simulation() -> SimulationResult:
    return SimulationResult(success=True, valid=True, status="SUCCESS")


def install_simulation_sequence(monkeypatch, outcomes: list[SimulationResult]) -> None:
    remaining = list(outcomes)

    def fake_simulate(*_args, **_kwargs):
        if not remaining:
            raise AssertionError("예상보다 simulate_plan 호출이 많습니다.")
        return remaining.pop(0)

    monkeypatch.setattr(nodes, "simulate_plan", fake_simulate)


def test_plan_pipeline_exposes_and_audits_verification(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획해줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    command_id = result["command_id"]
    assert result["verification_decision"]["decision"] == "PASS"
    assert result["verification_source"] == "llm"
    assert result["verification_prompt_version"] == "verification_v1"
    assert "verification_completed" in trace_nodes(result)

    history = services.postgres.command_history[command_id]
    assert history["result_summary"]["verification"]["decision"] == "PASS"
    stage_names = {
        row["node_name"] for row in services.postgres.stage_logs[command_id]
    }
    assert {
        "VERIFICATION_STARTED",
        "VERIFICATION_COMPLETED",
    }.issubset(stage_names)
    assert result["robot_command_batches"]
    assert result["adapter_validation"]["valid"] is True
    assert result["dispatched_robot_count"] == 0
    assert result["dispatched_command_count"] == 0
    assert result["gateway_dispatched"] is False
    assert result["data"]["robot_command_batches"] == result["robot_command_batches"]
    assert "robot_adapter_preview" in trace_nodes(result)


def test_query_pipeline_stops_before_optimizer(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="현재 재고를 조회해줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["plan_mode"] == "NO_REPLAN"
    assert "local_optimize" not in trace_nodes(result)
    assert "build_routes" not in trace_nodes(result)
    assert services.redis.activation_count == 0


def test_debug_simulation_command_runs_full_planning_pipeline(monkeypatch) -> None:
    install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text=(
                "W-003 작업을 가상 시뮬레이션하고 로봇 후보 점수, "
                "전체 이동 경로, 예약 정보, 검증 근거와 trace까지 "
                "개발자용으로 상세하게 보여줘"
            ),
            requested_execution_mode="AUTO",
        )
    )

    node_names = trace_nodes(result)
    assert result["status"] == "SIMULATION_SUCCESS"
    assert result["interpretation"]["command_kind"] == "PLAN"
    assert result["interpretation"]["execution_mode"] == "SIMULATE_ONLY"
    assert result["interpretation"]["target_task_ids"] == ["W-003"]
    assert result["report_detail_level"] == "DEBUG"
    assert result["data"]["task_count"] == 1
    assert result["optimization_plan"]["scheduled_tasks"]
    assert result["collision_plan"]["routes"]
    assert result["simulation"]["success"] is True
    assert result["verification_decision"]["decision"] == "PASS"
    assert result["robot_command_batches"]
    assert result["adapter_validation"]["valid"] is True
    assert result["dispatched_robot_count"] == 0
    assert result["dispatched_command_count"] == 0
    assert result["gateway_dispatched"] is False
    assert result["data"]["robot_command_batches"] == result["robot_command_batches"]
    assert "robot_adapter_preview" in node_names
    assert {
        "build_optimization_problem",
        "local_optimize",
        "build_routes",
        "simulation",
        "validate_simulation",
        "verification_completed",
    }.issubset(node_names)
    assert trace_for(result, "generate_final_report")[
        "planning_state_unchanged"
    ] is True
    assert "## 4. Optimization assignments" in result["answer"]
    assert '"scheduled_tasks"' not in result["answer"]
    scheduled_task_id = result["optimization_plan"]["scheduled_tasks"][0][
        "task_id"
    ]
    assert f'"task_id": "{scheduled_task_id}"' in result["answer"]
    assert "## 5. Candidate evaluation" in result["answer"]
    assert '"candidates": [' in result["answer"]
    assert "## 6. Routing and reservations" in result["answer"]
    assert '"routes": {' in result["answer"]
    assert '"reservations": {' in result["answer"]
    assert "## 7. Simulation metrics" in result["answer"]
    assert "## 8. Verification evidence" in result["answer"]
    assert '"evidence": [' in result["answer"]


def test_robot_count_query_works_without_openai(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    settings = fake_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        nodes,
        "build_supervisor_llm",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="로봇 갯수 알려줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["intent"] == "ROBOT_QUERY"
    assert result["plan_mode"] == "NO_REPLAN"
    assert result["data"]["robot_count"] == 3
    assert "3대" in result["answer"]
    assert trace_nodes(result) == [
        "interpret_command",
        "supervisor_started",
        "supervisor_fallback_used",
        "supervisor_completed",
        "build_snapshot",
        "route_by_command",
        "generate_final_report",
    ]
    stage_names = {
        row["node_name"]
        for row in services.postgres.stage_logs[result["command_id"]]
    }
    assert {
        "SUPERVISOR_STARTED",
        "SUPERVISOR_FALLBACK_USED",
        "SUPERVISOR_COMPLETED",
    }.issubset(stage_names)
    assert services.redis.activation_count == 0


def test_robot_status_query_uses_snapshot_values(monkeypatch) -> None:
    install_fakes(monkeypatch)
    settings = fake_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="현재 사용 가능한 로봇 알려줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["intent"] == "ROBOT_QUERY"
    assert result["data"]["available_robot_count"] == 3
    assert "사용 가능한 로봇은 3대" in result["answer"]


def test_complex_command_failure_still_generates_final_report(monkeypatch) -> None:
    install_fakes(monkeypatch)
    settings = fake_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="전체 제약을 분석해 복합 재계획을 만들어줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["status"] == "INTERPRETATION_FAILED"
    assert result["errors"]
    assert "완료하지 못했습니다" in result["answer"]
    assert trace_nodes(result) == [
        "interpret_command",
        "supervisor_started",
        "supervisor_fallback_used",
        "supervisor_completed",
        "generate_final_report",
    ]


def test_plan_only_runs_local_optimizer_and_internal_routing(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획해줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["status"] == "PLAN_READY"
    assert result["plan_validation"]["valid"] is True
    assert result["simulation"] == {}
    assert {"local_optimize", "build_routes", "validate_plan"}.issubset(trace_nodes(result))
    assert "작업" in result["answer"]
    assert services.redis.activation_count == 0


def test_simulate_only_returns_metrics_without_activation(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획하고 시뮬레이션해줘",
            requested_execution_mode="SIMULATE_ONLY",
        )
    )

    assert result["status"] == "SIMULATION_SUCCESS"
    assert result["simulation"]["valid"] is True
    assert result["simulation"]["conflict_count"] == 0
    assert "simulation" in trace_nodes(result)
    assert "validate_simulation" in trace_nodes(result)
    assert "충돌" in result["answer"]
    assert services.redis.activation_count == 0


def test_equivalent_plan_and_simulation_use_same_default_optimization(monkeypatch) -> None:
    install_fakes(monkeypatch)
    command_text = "현재 미완료 작업을 충돌 없이 처리하도록 계획해줘"

    planned = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text=command_text,
            requested_execution_mode="PLAN_ONLY",
        )
    )
    simulated = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text=command_text,
            requested_execution_mode="SIMULATE_ONLY",
        )
    )

    planned_trace = trace_for(planned, "build_optimization_problem")
    simulated_trace = trace_for(simulated, "build_optimization_problem")
    expected_weights = OptimizationWeights().model_dump()

    assert planned["optimization_plan"]["scheduled_tasks"] == simulated[
        "optimization_plan"
    ]["scheduled_tasks"]
    assert planned["data"]["robot_task_order"] == simulated["data"][
        "robot_task_order"
    ]
    assert planned_trace["optimization_profile"] == "DEFAULT"
    assert simulated_trace["optimization_profile"] == "DEFAULT"
    assert planned_trace["weights"] == expected_weights
    assert simulated_trace["weights"] == expected_weights


def test_explicit_makespan_goal_reaches_optimizer_response_and_trace(monkeypatch) -> None:
    install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text=(
                "W-003 작업을 로봇 최대 2대로 배정하고 전체 작업 완료시간을 "
                "최소화하는 가상 시뮬레이션을 실행해줘"
            ),
        )
    )
    build_trace = trace_for(result, "build_optimization_problem")
    weights = result["optimization_weights"]

    assert result["interpretation"]["optimization_priority"] == "MINIMIZE_MAKESPAN"
    assert result["optimization_profile"] == "MINIMIZE_MAKESPAN"
    assert result["optimization_weight_source"] == "PRIORITY_PROFILE"
    assert weights["makespan"] > OptimizationWeights().makespan
    assert build_trace["optimization_profile"] == "MINIMIZE_MAKESPAN"
    assert build_trace["weights"] == weights
    assert result["data"]["optimization_profile"] == "MINIMIZE_MAKESPAN"
    assert result["data"]["optimization_weights"] == weights
    assert result["optimization_plan"]["scheduled_tasks"]


def test_direct_scenario_weights_override_named_profile_defaults(monkeypatch) -> None:
    install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="사용자 지정 가중치로 가상 시뮬레이션해줘",
            scenario_definition={
                "name": "custom weights",
                "optimization_priority": "MINIMIZE_MAKESPAN",
                "optimization_weights": {
                    "total_distance": 0.25,
                    "makespan": 9.0,
                    "tardiness": 2.0,
                    "energy": 0.5,
                    "robot_activation": 0.1,
                    "plan_change": 0.75,
                },
            },
        )
    )
    build_trace = trace_for(result, "build_optimization_problem")

    assert result["optimization_profile"] == "MINIMIZE_MAKESPAN"
    assert result["optimization_weight_source"] == "EXPLICIT_WEIGHTS"
    assert result["optimization_weights"]["makespan"] == 9.0
    assert result["optimization_weights"]["total_distance"] == 0.25
    assert build_trace["weights"] == result["optimization_weights"]


def test_execute_without_gateway_never_activates_plan(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 실행해줘",
            requested_execution_mode="EXECUTE",
        )
    )

    assert result["status"] == "EXECUTION_BLOCKED"
    assert any("ROBOT_GATEWAY_URL" in error for error in result["errors"])
    assert services.redis.activation_count == 0


def test_llm_failures_use_query_and_report_templates(monkeypatch) -> None:
    install_fakes(monkeypatch)
    settings = fake_settings()
    settings.report_with_llm = True
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    calls = {"count": 0}

    def flaky_llm():
        calls["count"] += 1
        if calls["count"] >= 3:
            raise RuntimeError("report llm unavailable")
        return FakeSupervisor()

    monkeypatch.setattr(nodes, "build_supervisor_llm", flaky_llm)
    simulated = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획하고 시뮬레이션해줘",
            requested_execution_mode="SIMULATE_ONLY",
        )
    )

    assert simulated["simulation"]["valid"] is True
    assert "충돌" in simulated["answer"]
    assert not any("템플릿" in warning for warning in simulated["warnings"])
    assert any(
        "템플릿" in warning
        for warning in simulated["report_generation_warnings"]
    )

    monkeypatch.setattr(
        nodes,
        "build_supervisor_llm",
        lambda: (_ for _ in ()).throw(RuntimeError("command llm unavailable")),
    )
    queried = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="로봇 몇 대야?",
            requested_execution_mode="PLAN_ONLY",
        )
    )
    assert queried["data"]["robot_count"] == 3
    assert "3대" in queried["answer"]


def test_query_command_and_required_stages_are_audited(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    monkeypatch.setattr(
        nodes,
        "rule_based_query_interpretation",
        lambda _text: CommandInterpretation(
            command_kind="QUERY",
            intent="INVENTORY_QUERY",
            objective="inventory query",
            query_target="INVENTORY",
            query_action="COUNT",
            required_sql_reads=["INVENTORY"],
            execution_mode="PLAN_ONLY",
            summary="inventory query",
        ),
    )
    command = NaturalLanguageCommand(
        command_id="COMMAND-QUERY-1",
        warehouse_id=1,
        text="inventory query",
        requested_execution_mode="PLAN_ONLY",
    )

    result = run_planning(command)

    history = services.postgres.command_history[command.command_id]
    stage_names = [
        row["node_name"] for row in services.postgres.stage_logs[command.command_id]
    ]
    assert result["status"] == "COMPLETED"
    assert history["status"] == "SUCCESS"
    assert history["command_type"] == "QUERY"
    assert {
        "COMMAND_RECEIVED",
        "COMMAND_INTERPRETED",
        "SUPERVISOR_STARTED",
        "SUPERVISOR_COMPLETED",
        "SNAPSHOT_CREATED",
        "REPORT_GENERATED",
        "COMMAND_COMPLETED",
    }.issubset(stage_names)


def test_plan_audit_is_idempotent_for_same_command_id(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    command = NaturalLanguageCommand(
        command_id="COMMAND-PLAN-1",
        warehouse_id=1,
        text="plan",
        requested_execution_mode="PLAN_ONLY",
    )

    first = run_planning(command)
    first_stages = services.postgres.stage_logs[command.command_id]
    first_stage_count = len(first_stages)
    stage_order = [row["node_name"] for row in first_stages]
    assert [(row["sequence"], row["attempt"]) for row in first_stages] == sorted(
        (row["sequence"], row["attempt"]) for row in first_stages
    )
    expected_order = [
        "COMMAND_RECEIVED",
        "COMMAND_INTERPRETED",
        "SNAPSHOT_CREATED",
        "SCOPE_DECIDED",
        "TASKS_SELECTED",
        "OPTIMIZATION_PROBLEM_BUILT",
        "OPTIMIZATION_COMPLETED",
        "OPTIMIZATION_CANDIDATES_EVALUATED",
        "OBJECTIVE_BREAKDOWN_CREATED",
        "ROUTING_COMPLETED",
        "ROUTE_EVIDENCE_CREATED",
        "RESERVATION_EVIDENCE_CREATED",
        "DISTANCE_COMPARISON_CREATED",
        "PLAN_VALIDATED",
        "EVIDENCE_REPORT_GENERATED",
        "REPORT_GENERATED",
        "COMMAND_COMPLETED",
    ]
    assert [stage_order.index(name) for name in expected_order] == sorted(
        stage_order.index(name) for name in expected_order
    )
    second = run_planning(command)

    assert first["status"] == "PLAN_READY"
    assert second["status"] == "PLAN_READY"
    assert list(services.postgres.command_history) == [command.command_id]
    assert len(services.postgres.stage_logs[command.command_id]) == first_stage_count


def test_simulation_id_is_linked_to_command_history(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    command = NaturalLanguageCommand(
        command_id="COMMAND-SIM-1",
        warehouse_id=1,
        text="simulate",
        requested_execution_mode="SIMULATE_ONLY",
    )

    result = run_planning(command)

    history = services.postgres.command_history[command.command_id]
    stage_names = [
        row["node_name"] for row in services.postgres.stage_logs[command.command_id]
    ]
    assert history["simulation_id"] == result["simulation_id"]
    assert history["status"] == "SUCCESS"
    assert "SIMULATION_COMPLETED" in stage_names


def test_failed_command_is_audited_as_failed(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    settings = fake_settings()
    settings.openai_api_key = ""
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    command = NaturalLanguageCommand(
        command_id="COMMAND-FAILED-1",
        warehouse_id=1,
        text="unsupported complex command",
        requested_execution_mode="PLAN_ONLY",
    )

    result = run_planning(command)

    history = services.postgres.command_history[command.command_id]
    stage_names = [
        row["node_name"] for row in services.postgres.stage_logs[command.command_id]
    ]
    assert result["status"] == "INTERPRETATION_FAILED"
    assert history["status"] == "FAILED"
    assert history["error_summary"]["errors"]
    assert "COMMAND_FAILED" in stage_names


def test_audit_failure_does_not_change_successful_plan(monkeypatch, caplog) -> None:
    services = install_fakes(monkeypatch)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    services.postgres.create_or_get_command_history = fail_audit
    services.postgres.finalize_command_audit = fail_audit

    result = run_planning(
        NaturalLanguageCommand(
            command_id="COMMAND-AUDIT-FAIL-1",
            warehouse_id=1,
            text="plan",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["status"] == "PLAN_READY"
    assert result["plan_validation"]["valid"] is True
    assert result["audit_warnings"]
    assert "audit unavailable" in caplog.text


def test_local_replan_once_then_pass(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    install_simulation_sequence(
        monkeypatch,
        [
            failed_simulation(
                "VERTEX_CONFLICT", task_id="W1:move", robot_id="R1"
            ),
            passed_simulation(),
        ],
    )
    command = NaturalLanguageCommand(
        command_id="COMMAND-LOCAL-REPLAN",
        warehouse_id=1,
        text="미완료 작업을 계획해줘",
        requested_execution_mode="PLAN_ONLY",
    )

    result = run_planning(command)

    assert result["status"] == "PLAN_READY"
    assert result["command_id"] == command.command_id
    assert result["verification_decision"]["decision"] == "PASS"
    assert result["replan_attempt"] == 1
    assert len(result["replan_history"]) == 1
    history = result["replan_history"][0]
    assert history["scope"] == "LOCAL_REPLAN"
    assert history["verification_before"] == "REPLAN_LOCAL"
    assert history["verification_after"] == "PASS"
    assert history["status"] == "COMPLETED"
    assert history["previous_plan_version"] != history["new_plan_version"]
    assert result["original_plan_version"] == history["previous_plan_version"]
    assert result["current_plan_version"] == history["new_plan_version"]
    assert services.redis.activation_count == 0
    assert services.redis.simulation_event_count == 0
    assert services.postgres.recorded == 1
    audit_replan = services.postgres.command_history[command.command_id][
        "result_summary"
    ]["replan"]
    assert audit_replan["attempt"] == 1
    assert audit_replan["history"][0]["scope"] == "LOCAL_REPLAN"
    stage_names = {
        row["node_name"]
        for row in services.postgres.stage_logs[command.command_id]
    }
    assert {
        "REPLAN_REQUESTED",
        "LOCAL_REPLAN_STARTED",
        "REPLAN_COMPLETED",
    }.issubset(stage_names)


def test_global_replan_once_then_pass(monkeypatch) -> None:
    install_fakes(monkeypatch)
    install_simulation_sequence(
        monkeypatch,
        [
            failed_simulation(
                "DISCONNECTED_OR_CLOSED_EDGE",
                task_id="W1:move",
                robot_id="R1",
            ),
            passed_simulation(),
        ],
    )

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획해줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["verification_decision"]["decision"] == "PASS"
    assert result["replan_history"][0]["scope"] == "GLOBAL_REPLAN"
    assert len(result["replan_history"][0]["affected_task_ids"]) == 3
    assert "global_replan_started" in trace_nodes(result)


def test_two_replans_accumulate_history_and_unique_versions(monkeypatch) -> None:
    install_fakes(monkeypatch)
    settings = fake_settings()
    settings.max_replan_count = 2
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: FakeSupervisor(2))
    install_simulation_sequence(
        monkeypatch,
        [
            failed_simulation(
                "VERTEX_CONFLICT", task_id="W1:move", robot_id="R1"
            ),
            failed_simulation(
                "EDGE_SWAP_CONFLICT", task_id="W2:move", robot_id="R2"
            ),
            passed_simulation(),
        ],
    )
    command = NaturalLanguageCommand(
        command_id="COMMAND-TWO-REPLANS",
        warehouse_id=1,
        text="미완료 작업을 계획해줘",
        requested_execution_mode="PLAN_ONLY",
    )

    result = run_planning(command)

    assert result["status"] == "PLAN_READY"
    assert result["command_id"] == command.command_id
    assert result["replan_attempt"] == 2
    assert [row["attempt"] for row in result["replan_history"]] == [1, 2]
    versions = [result["original_plan_version"]] + [
        row["new_plan_version"] for row in result["replan_history"]
    ]
    assert len(versions) == len(set(versions)) == 3
    assert [row["status"] for row in result["replan_history"]] == [
        "FAILED",
        "COMPLETED",
    ]


def test_replan_limit_is_enforced(monkeypatch) -> None:
    install_fakes(monkeypatch)
    install_simulation_sequence(
        monkeypatch,
        [
            failed_simulation(
                "VERTEX_CONFLICT", task_id="W1:move", robot_id="R1"
            ),
            failed_simulation(
                "EDGE_SWAP_CONFLICT", task_id="W2:move", robot_id="R2"
            ),
        ],
    )

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획해줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["status"] == "VERIFICATION_FAILED"
    assert result["verification_decision"]["decision"] == "FAIL"
    assert result["replan_attempt"] == 1
    assert "replan_limit_reached" in trace_nodes(result)
    assert trace_nodes(result).count("local_optimize") == 2


def test_repeated_failure_signature_stops_immediately(monkeypatch) -> None:
    install_fakes(monkeypatch)
    monkeypatch.setattr(nodes, "build_supervisor_llm", lambda: FakeSupervisor(3))
    repeated = failed_simulation(
        "VERTEX_CONFLICT", task_id="W1:move", robot_id="R1"
    )
    install_simulation_sequence(monkeypatch, [repeated, repeated.model_copy(deep=True)])

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 계획해줘",
            requested_execution_mode="PLAN_ONLY",
        )
    )

    assert result["status"] == "VERIFICATION_FAILED"
    assert result["replan_attempt"] == 1
    assert "repeated_failure_detected" in trace_nodes(result)
    assert trace_nodes(result).count("local_optimize") == 2
    assert max(result["repeated_failure_signatures"].values()) == 2


def test_execute_activates_only_after_replan_final_pass(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    settings = fake_settings()
    settings.robot_gateway_url = "http://mock-gateway"
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    install_simulation_sequence(
        monkeypatch,
        [
            failed_simulation(
                "VERTEX_CONFLICT", task_id="W1:move", robot_id="R1"
            ),
            passed_simulation(),
        ],
    )

    class AcceptedGateway:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dispatch(self, plan_version, _payload):
            return {"accepted": True, "plan_version": plan_version}

    monkeypatch.setattr(nodes, "RobotGateway", AcceptedGateway)

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="미완료 작업을 실행해줘",
            requested_execution_mode="EXECUTE",
        )
    )

    trace_names = trace_nodes(result)
    verification_positions = [
        index
        for index, name in enumerate(trace_names)
        if name == "verification_completed"
    ]
    assert len(verification_positions) == 2
    assert trace_names.index("execution_precheck") > verification_positions[-1]
    assert result["verification_decision"]["decision"] == "PASS"
    assert result["status"] == "DISPATCHED"
    assert services.redis.activation_count == 1


def test_execute_explicit_work_scope_dispatches_only_requested_task(monkeypatch) -> None:
    services = install_fakes(monkeypatch)
    settings = fake_settings()
    settings.robot_gateway_url = "http://mock-gateway"
    monkeypatch.setattr(nodes, "get_settings", lambda: settings)
    dispatched: list[dict] = []

    class RecordingGateway:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dispatch(self, plan_version, payload):
            dispatched.append(payload)
            return {"accepted": True, "plan_version": plan_version}

    monkeypatch.setattr(nodes, "RobotGateway", RecordingGateway)

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="작업 W1 하나만 지금 실행해줘. 다른 작업은 포함하지 마.",
            requested_execution_mode="EXECUTE",
        )
    )

    assert result["status"] == "DISPATCHED"
    assert result["interpretation"]["target_task_ids"] == ["W-001"]
    assert result["data"]["task_count"] == 1
    assert {row["task_id"] for row in result["data"]["task_assignments"]} == {
        "W1:move"
    }
    assert result["data"]["schedule_validation"]["scope_work_ids"] == ["W-001"]
    assert result["data"]["inventory_operations"] == []
    assert result["data"]["emergency_review_items"] == []
    report_text = json.dumps(
        result["user_report_summary"],
        ensure_ascii=False,
    )
    assert "W2" not in report_text
    assert "W3" not in report_text
    assert len(dispatched) == 1
    assert {
        command["task_id"]
        for batch in dispatched[0]
        for command in batch["commands"]
        if command["task_id"]
    } == {"W1:move"}
    assert services.redis.activation_count == 1


def test_inventory_query_returns_sorted_item_summaries_and_inbound_details() -> None:
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="INVENTORY_QUERY",
        objective="재고 조회",
        query_target="INVENTORY",
        query_action="DETAIL",
        execution_mode="PLAN_ONLY",
        summary="재고 상세",
    )
    answer, data = nodes.query_report(
        interpretation,
        {
            "sql": {
                "inventory": [
                    {"item_id": "B", "available_quantity": 20},
                    {"item_id": "A", "available_quantity": 15},
                    {"item_id": "A", "available_quantity": 25},
                ],
                "inbound_orders": [
                    {
                        "inbound_id": "IN-A",
                        "item_id": "A",
                        "quantity_boxes": 20,
                        "status": "INSPECTING",
                        "expected_arrival_at": datetime(2026, 7, 23, 7, tzinfo=UTC),
                        "expected_available_at": "2026-07-23T07:10:00+00:00",
                        "actual_arrival_at": None,
                        "actual_available_at": None,
                        "storage_node_id": 2088,
                        "lot_id": "LOT-A-02",
                    }
                ],
                "outbound_orders": [],
            },
            "redis": {"inventory_reservations": []},
        },
    )

    assert data["total_available_quantity"] == 60
    assert data["item_summaries"] == [
        {"item_id": "A", "available_quantity_boxes": 40, "lot_count": 2, "unit": "BOX"},
        {"item_id": "B", "available_quantity_boxes": 20, "lot_count": 1, "unit": "BOX"},
    ]
    assert data["inbound_order_summaries"] == [
        {
            "inbound_id": "IN-A",
            "item_id": "A",
            "quantity_boxes": 20,
            "status": "INSPECTING",
            "expected_arrival_at": "2026-07-23T07:00:00+00:00",
            "expected_available_at": "2026-07-23T07:10:00+00:00",
            "actual_arrival_at": None,
            "actual_available_at": None,
            "storage_node_id": 2088,
            "lot_id": "LOT-A-02",
        }
    ]
    assert "- A: 40 BOX" in answer
    assert "- B: 20 BOX" in answer
    assert "예정 입고 주문은 1건입니다." in answer


def test_inventory_query_reports_no_inbound_orders() -> None:
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="INVENTORY_QUERY",
        objective="재고 조회",
        query_target="INVENTORY",
        query_action="DETAIL",
        execution_mode="PLAN_ONLY",
        summary="재고 상세",
    )
    answer, data = nodes.query_report(
        interpretation,
        {
            "sql": {
                "inventory": [{"item_id": "A", "available_quantity": 40}],
                "inbound_orders": [],
                "outbound_orders": [],
            },
            "redis": {"inventory_reservations": []},
        },
    )

    assert data["inbound_order_summaries"] == []
    assert data["open_inbound_order_count"] == 0
    assert "예정 입고 주문은 없습니다." in answer


def test_inventory_query_keeps_single_item_and_current_lots_only() -> None:
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="INVENTORY_QUERY",
        objective="A상품 현재 가용 재고와 lot ID, 저장 노드 ID를 알려줘. 다른 품목은 제외해줘.",
        item_ids=["A"],
        query_target="INVENTORY",
        query_action="DETAIL",
        execution_mode="PLAN_ONLY",
        summary="A 재고 상세",
    )

    answer, data = nodes.query_report(
        interpretation,
        {
            "sql": {
                "inventory": [
                    {
                        "item_id": "A", "lot_id": "LOT-A-01",
                        "available_quantity": 10, "node_id": 2088,
                        "status": "AVAILABLE", "available_at": "2026-07-23T00:00:00+00:00",
                    },
                    {"item_id": "B", "lot_id": "LOT-B-01", "available_quantity": 20, "node_id": 2090},
                ],
                "inbound_orders": [
                    {
                        "inbound_id": "IN-A", "item_id": "A", "lot_id": "LOT-A-FUTURE",
                        "quantity_boxes": 20, "status": "INSPECTING", "storage_node_id": 2088,
                    }
                ],
                "outbound_orders": [],
            },
            "redis": {"inventory_reservations": []},
        },
    )

    assert data["item_ids"] == ["A"]
    assert data["item_count"] == 1
    assert data["total_available_quantity"] == 10
    assert data["item_summaries"] == [
        {"item_id": "A", "available_quantity_boxes": 10, "lot_count": 1, "unit": "BOX"}
    ]
    assert data["available_lot_summaries"] == [
        {
            "item_id": "A", "lot_id": "LOT-A-01", "available_quantity_boxes": 10,
            "storage_node_id": 2088, "status": "AVAILABLE",
            "available_at": "2026-07-23T00:00:00+00:00",
        }
    ]
    assert data["inbound_order_summaries"] == []
    assert "B:" not in answer
    assert "LOT-A-01" in answer


def test_single_item_inventory_query_passes_filter_to_snapshot(monkeypatch) -> None:
    services = install_fakes(monkeypatch)

    result = run_planning(
        NaturalLanguageCommand(
            warehouse_id=1,
            text="A상품 현재 가용 재고를 조회해줘. 다른 품목은 제외해줘.",
            requested_execution_mode="AUTO",
        )
    )

    assert result["interpretation"]["item_ids"] == ["A"]
    assert services.postgres.snapshot_item_ids == ["A"]
    assert "local_optimize" not in trace_nodes(result)
    assert "build_routes" not in trace_nodes(result)


def test_inventory_quantity_filter_inbound_merge_and_storage_candidates() -> None:
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="INVENTORY_QUERY",
        objective=(
            "현재 가용 재고가 20 BOX 이하인 품목의 현재 수량과 예정 입고를 "
            "알려주고 active STORAGE 노드 후보도 알려줘."
        ),
        query_target="INVENTORY",
        query_action="COUNT",
        load_open_inventory_orders=True,
        target_node_type="STORAGE",
        required_graph_reads=["STORAGE_NODES"],
        query_filters=[
            {
                "field": "available_quantity_boxes",
                "operator": "LTE",
                "value": 20,
                "unit": "BOX",
            }
        ],
        execution_mode="PLAN_ONLY",
        summary="저재고 및 저장 노드 조회",
    )

    answer, data = nodes.query_report(
        interpretation,
        {
            "sql": {
                "inventory": [
                    {"item_id": "A", "lot_id": "LOT-A", "available_quantity": 10},
                    {"item_id": "B", "lot_id": "LOT-B", "available_quantity": 20},
                    {"item_id": "C", "lot_id": "LOT-C", "available_quantity": 60},
                    {"item_id": "D", "lot_id": "LOT-D", "available_quantity": 15},
                    {"item_id": "F", "lot_id": "LOT-F", "available_quantity": 30},
                ],
                "inbound_orders": [
                    {"inbound_id": "IN-A", "item_id": "A", "quantity_boxes": 50},
                    {"inbound_id": "IN-B", "item_id": "B", "quantity_boxes": 100},
                    {"inbound_id": "IN-F", "item_id": "F", "quantity_boxes": 20},
                ],
                "outbound_orders": [],
            },
            "redis": {"inventory_reservations": []},
            "graph": {
                "nodes": [
                    {
                        "node_id": 2088,
                        "node_type": "STORAGE",
                        "zone_id": "STORAGE",
                        "active": True,
                        "x": 10.0,
                        "y": 2.21,
                    },
                    {
                        "node_id": 2089,
                        "node_type": "STORAGE",
                        "zone_id": "STORAGE",
                        "active": False,
                        "x": 11.24,
                        "y": 2.21,
                    },
                    {
                        "node_id": 2090,
                        "node_type": "STORAGE",
                        "zone_id": "STORAGE",
                        "x": 12.48,
                        "y": 2.21,
                    },
                    {
                        "node_id": 2001,
                        "node_type": "ROUTE",
                        "zone_id": "ROUTE",
                        "active": True,
                        "x": 8.2,
                        "y": 1.44,
                    },
                ]
            },
        },
    )

    assert data["item_ids"] == ["A", "B", "D"]
    assert data["item_count"] == 3
    assert data["total_available_quantity"] == 45
    assert data["item_inventory_status_summaries"] == [
        {
            "item_id": "A",
            "available_quantity_boxes": 10,
            "has_scheduled_inbound": True,
            "scheduled_inbound_quantity_boxes": 50,
            "inbound_order_count": 1,
            "unit": "BOX",
        },
        {
            "item_id": "B",
            "available_quantity_boxes": 20,
            "has_scheduled_inbound": True,
            "scheduled_inbound_quantity_boxes": 100,
            "inbound_order_count": 1,
            "unit": "BOX",
        },
        {
            "item_id": "D",
            "available_quantity_boxes": 15,
            "has_scheduled_inbound": False,
            "scheduled_inbound_quantity_boxes": 0,
            "inbound_order_count": 0,
            "unit": "BOX",
        },
    ]
    assert {row["item_id"] for row in data["inbound_order_summaries"]} == {"A", "B"}
    assert data["storage_node_candidates"] == [
        {
            "node_id": 2088,
            "node_type": "STORAGE",
            "zone_id": "STORAGE",
            "active": True,
            "x": 10.0,
            "y": 2.21,
        },
        {
            "node_id": 2090,
            "node_type": "STORAGE",
            "zone_id": "STORAGE",
            "active": True,
            "x": 12.48,
            "y": 2.21,
        },
    ]
    assert "C:" not in answer
    assert "F:" not in answer
    assert "- D: 현재 15 BOX / 예정 입고 없음, 0 BOX" in answer
    assert "활성 STORAGE 노드 후보는 2개입니다." in answer

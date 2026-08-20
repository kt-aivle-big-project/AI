from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.domain.planning_evaluation import PlanningScenarioSuiteRequest
from app.domain.schemas import (
    AutoMissionRequest,
    EventInput,
    FormulationRecommendation,
    HumanInteractionResumeRequest,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    OrchestrationResult,
    StructuredMissionInput,
    StructuredOperationInput,
)
from app.graph.input_formulation import _structured_normalized_request
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.planning_evaluation_service import PlanningEvaluationStore
from app.services.orchestration_service import OrchestrationService
from app.services.hitl_service import HumanInteractionService, HumanInteractionStore
from app.services.request_gate_service import resolve_request_gate
from app.services.planning_scenario_suite_service import (
    DEFAULT_FIXTURE_DIR,
    PlanningScenarioMaterializer,
    PlanningScenarioSuiteService,
)
from app.services.planning_dynamic_scenario_validator import (
    validate_dynamic_definition,
)


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_materializer_exposes_reviewed_thirty_scenario_catalog(tmp_path: Path) -> None:
    store = PlanningEvaluationStore(root=tmp_path / "evaluations")
    materializer = PlanningScenarioMaterializer(
        store=store,
        fixture_dir=DEFAULT_FIXTURE_DIR,
    )

    definitions = materializer.definitions()
    assert len(definitions) == 30
    assert {
        value.get("scenario_group", "INITIAL") for value in definitions
    } == {"INITIAL", "REPLAN", "HUMAN_REVIEW"}
    counts = {
        group: sum(value.get("scenario_group", "INITIAL") == group for value in definitions)
        for group in ("INITIAL", "REPLAN", "HUMAN_REVIEW")
    }
    assert counts == {"INITIAL": 15, "REPLAN": 10, "HUMAN_REVIEW": 5}

    captures = [
        materializer.materialize(value, suite_id="ESUITE-TEST")
        for value in definitions
    ]
    by_id = {value["scenario_id"]: value for value in captures}

    for definition in definitions:
        capture = by_id[definition["scenario_id"]]
        root = store.capture_dir(str(capture["evaluation_id"]))
        before = _read(root / "materialization_report.json")
        after = _read(root / "post_materialization_report.json")
        assert before["passed"] is True
        assert after["passed"] is True
        assert before["input_fingerprint"] == after["input_fingerprint"]
        assert before["snapshot"] == after["snapshot"]
        assert (root / "frozen_repository" / "warehouse_graph.json").is_file()
        assert (root / "internal_request.json").is_file()
        internal_request = _read(root / "internal_request.json")
        operation_ids = [
            str(value["operation_id"])
            for value in internal_request["structured_input"]["operations"]
        ]
        assert all(re.fullmatch(r"(?:ORD|IN)-\d{3,}", value) for value in operation_ids)

    same_sku_capture = store.capture_dir(
        str(by_id["PC02_RULE_BOUNDARY_8_SAME_SKU"]["evaluation_id"])
    )
    same_sku_request = _read(same_sku_capture / "internal_request.json")
    operations = same_sku_request["structured_input"]["operations"]
    assert len({value["product_code"] for value in operations}) == 1
    same_sku_report = _read(same_sku_capture / "materialization_report.json")
    assert len(same_sku_report["snapshot"]["handling_unit_ids"]) == 8

    battery_capture = store.capture_dir(
        str(by_id["PC05_LOW_BATTERY_ROBOT_FILTER"]["evaluation_id"])
    )
    battery_report = _read(battery_capture / "materialization_report.json")
    robot_states = battery_report["snapshot"]["robot_states"]
    assert len(robot_states) == 6
    assert battery_report["snapshot"]["eligible_robot_count"] == 5
    assert battery_report["snapshot"]["low_battery_robot_count"] == 1
    assert sum(float(value["battery_pct"]) < 30 for value in robot_states) == 1
    assert min(float(value["battery_pct"]) for value in robot_states) == 18


def test_materialize_only_suite_finishes_without_starting_jobs(tmp_path: Path) -> None:
    store = PlanningEvaluationStore(root=tmp_path / "evaluations")
    materializer = PlanningScenarioMaterializer(store=store)
    service = PlanningScenarioSuiteService(store=store, materializer=materializer)
    request = PlanningScenarioSuiteRequest(
        scenario_ids=["PC01_LOW_4_DISTRIBUTED_OUTBOUND"],
        materialize_only=True,
    )

    suite = service.start(request)

    assert suite["status"] == "SUCCEEDED"
    assert suite["scenario_count"] == 1
    assert suite["completed_count"] == 1
    assert suite["failed_count"] == 0
    assert suite["scenarios"][0]["materialization_status"] == "PASSED"
    assert suite["scenarios"][0]["job_id"] is None


def test_dynamic_scenarios_enter_rule_agent_comparison_queue(
    tmp_path: Path,
) -> None:
    class RecordingJobService:
        def __init__(self) -> None:
            self.jobs: dict[str, SimpleNamespace] = {}

        def submit(self, evaluation_id, request):
            job_id = f"EJOB-{len(self.jobs) + 1:016X}"
            job = SimpleNamespace(
                job_id=job_id,
                evaluation_id=evaluation_id,
                status="SUCCEEDED",
                status_url=f"/status/{job_id}",
                result_url=f"/result/{job_id}",
                current_stage="COMPLETED",
                completed_runs=request.agent_repeats + 1,
                total_runs=request.agent_repeats + 1,
                error_type=None,
                error_message=None,
            )
            self.jobs[job_id] = job
            return job

        def get(self, job_id):
            return self.jobs[job_id]

    store = PlanningEvaluationStore(root=tmp_path / "evaluations")
    materializer = PlanningScenarioMaterializer(store=store)
    jobs = RecordingJobService()
    service = PlanningScenarioSuiteService(
        store=store,
        materializer=materializer,
        job_service=jobs,  # type: ignore[arg-type]
    )
    request = PlanningScenarioSuiteRequest(
        scenario_ids=["RP01_NEW_ORDER_DURING_MOVE", "HR01_SAFETY_OVERRIDE"],
        materialize_only=False,
    )

    suite = service.start(request)

    assert suite["status"] == "SUCCEEDED"
    assert suite["completed_count"] == 2
    assert len(jobs.jobs) == 2
    assert all(value["job_id"] is not None for value in suite["scenarios"])
    assert all(
        value["current_stage"] == "COMPLETED"
        for value in suite["scenarios"]
    )
    assert all(value["total_runs"] == 6 for value in suite["scenarios"])


def test_all_dynamic_definitions_pass_and_wrong_handover_fails(tmp_path: Path) -> None:
    materializer = PlanningScenarioMaterializer(
        store=PlanningEvaluationStore(root=tmp_path / "evaluations")
    )
    dynamic = [
        value
        for value in materializer.definitions()
        if value.get("scenario_group") in {"REPLAN", "HUMAN_REVIEW"}
    ]
    reports = [validate_dynamic_definition(value) for value in dynamic]
    assert len(reports) == 15
    assert all(value["passed"] for value in reports), json.dumps(
        reports, ensure_ascii=False, indent=2
    )

    invalid = next(
        value for value in dynamic if value["scenario_id"] == "RP01_NEW_ORDER_DURING_MOVE"
    ).copy()
    invalid["dynamic_contract"] = {
        **invalid["dynamic_contract"],
        "expected_handover_policy": "CURRENT_NODE",
    }
    report = validate_dynamic_definition(invalid)
    assert report["passed"] is False
    assert "safe_handover_policy" in report["failed_checks"]


def test_destination_approval_updates_exact_order_before_resume(
    tmp_path: Path,
) -> None:
    command = "override ORD-001 destination O_D with O_E"
    normalized = NormalizedWarehouseRequest(
        source="structured_events",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ITEM-EVAL",
            )
        ],
        constraints=NormalizedRequestConstraints(),
        raw_user_command=command,
        normalization_summary="destination approval test",
    )
    gate = resolve_request_gate(
        simulation_id="SIM-HITL-DESTINATION",
        request=normalized,
        recommendation=FormulationRecommendation(
            route="AGENT_FORMULATION",
            gate_action="PROCEED",
        ),
        original_user_command=command,
        has_structured_events=True,
        authoritative_structured_input=True,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=[],
    )
    interaction = gate.human_interaction
    assert interaction is not None
    assert interaction.evidence_ids == ["ORD-001", "O_D", "O_E"]

    structured = StructuredMissionInput(
        request_id="REQ-HITL-DESTINATION",
        operations=[
            StructuredOperationInput(
                operation_id="ORD-001",
                operation_type="OUTBOUND",
                product_code="ITEM-EVAL",
                destination_node_code="O_D",
            )
        ],
    )
    service = HumanInteractionService(HumanInteractionStore(tmp_path / "hitl"))
    pending = service.create_pending(
        interaction=interaction,
        state={
            "warehouse_id": "WH-001",
            "simulation_id": "SIM-HITL-DESTINATION",
            "request_mode": "mixed",
            "optimization_backend": "cuopt",
            "events": structured.to_events(),
            "user_command": command,
            "structured_input": structured,
            "normalized_request_override": normalized,
            "requested_planning_mode": None,
            "max_agent_steps": 8,
            "max_planner_retries": 1,
            "human_responses": [],
            "parent_interaction_id": None,
        },
    )
    captured: dict[str, object] = {}

    def runner(request: AutoMissionRequest, trusted_mode: str | None) -> OrchestrationResult:
        captured["request"] = request
        captured["trusted_mode"] = trusted_mode
        return OrchestrationResult(
            warehouse_id=request.warehouse_id,
            simulation_id=request.simulation_id,
            request_mode=request.request_mode,
            optimization_backend=request.optimization_backend or "cuopt",
            planning_mode="llm_router",
            effective_planning_mode="force_rule",
            status="plan_validated",
            workflow_trace=[],
            node_execution_log=[],
            llm_node_summaries=[],
            errors=[],
            events=request.events,
        )

    resumed = service.respond(
        pending.interaction.interaction_id,
        HumanInteractionResumeRequest(
            action="APPROVE",
            selected_option_id="APPROVE_ALTERNATIVE_DESTINATION",
            actor_id="operator-1",
        ),
        runner=runner,
    )
    approved = captured["request"]
    assert isinstance(approved, AutoMissionRequest)
    assert approved.structured_input is not None
    assert resumed.resume_outcome == "RESUMED"
    assert approved.structured_input.operations[0].destination_node_code == "O_E"
    assert approved.events[0].payload["destination_node_code"] == "O_E"
    assert approved.normalized_request_override == normalized
    assert captured["trusted_mode"] is None

    resolved = service.get(pending.interaction.interaction_id)
    assert resolved.status == "RESOLVED"
    assert (
        resolved.original_request["structured_input"]["operations"][0][
            "destination_node_code"
        ]
        == "O_D"
    )


def test_suite_request_builds_comparison_contract_without_suite_fields() -> None:
    request = PlanningScenarioSuiteRequest(
        scenario_ids=["PC01_LOW_4_DISTRIBUTED_OUTBOUND"],
        materialize_only=True,
        agent_repeats=5,
        min_valid_agent_runs=3,
    )

    comparison = request.comparison_request()
    job = request.job_request(scenario_id="PC01", suite_id="ESUITE-TEST")

    assert comparison.agent_repeats == 5
    assert job.agent_repeats == 5
    assert job.idempotency_key == "ESUITE-TEST:PC01"


def test_structured_edge_events_reach_typed_map_constraints() -> None:
    structured = StructuredMissionInput(
        operations=[
            StructuredOperationInput(
                operation_id="ORD-700001",
                operation_type="OUTBOUND",
                product_code="ITEM-001",
                destination_node_code="O_A",
            )
        ],
        constraints=NormalizedRequestConstraints(
            soft_avoid_edge_ids=["EDGE-EXISTING-SOFT"],
            hard_block_edge_ids=["EDGE-EXISTING-HARD"],
        ),
    )

    normalized = _structured_normalized_request(
        {
            "events": [
                *structured.to_events(),
                EventInput(type="edge_congested", edge_id="EDGE-NEW-SOFT"),
                EventInput(type="edge_blocked", edge_id="EDGE-NEW-HARD"),
            ],
            "structured_input": structured,
            "user_command": None,
        }
    )

    assert normalized.constraints.soft_avoid_edge_ids == [
        "EDGE-EXISTING-SOFT",
        "EDGE-NEW-SOFT",
    ]
    assert normalized.constraints.hard_block_edge_ids == [
        "EDGE-EXISTING-HARD",
        "EDGE-NEW-HARD",
    ]


def test_all_scenarios_pass_real_rule_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same request gate, solver, route, and MAPF path as production."""

    monkeypatch.setenv("OUTBOUND_FULFILLMENT_MODE", "goods_to_person")
    get_settings.cache_clear()
    store = PlanningEvaluationStore(root=tmp_path / "evaluations")
    materializer = PlanningScenarioMaterializer(store=store)
    definitions = {
        value["scenario_id"]: value for value in materializer.definitions()
    }

    for scenario_id in definitions:
        if definitions[scenario_id].get("scenario_group", "INITIAL") != "INITIAL":
            continue
        capture = materializer.materialize(
            definitions[scenario_id], suite_id="ESUITE-RUNTIME-CONTRACT"
        )
        root = store.capture_dir(str(capture["evaluation_id"]))
        request = AutoMissionRequest.model_validate(_read(root / "internal_request.json"))
        repository = JsonWarehouseRepository(
            root / "frozen_repository",
            warehouse_id=request.warehouse_id,
            simulation_id=request.simulation_id,
        )

        result = OrchestrationService().run(
            request.model_copy(update={"optimization_backend": "ortools"}),
            trusted_planning_mode="force_rule",
            persist_simulation_plan=False,
            repository=repository,
        )

        diagnostic = {
            "status": result.status,
            "errors": [value.model_dump(mode="json") for value in result.errors],
            "route_validation": (
                result.route_validation.model_dump(mode="json")
                if result.route_validation is not None
                else None
            ),
            "mapf_validation": (
                result.mapf_validation.model_dump(mode="json")
                if result.mapf_validation is not None
                else None
            ),
            "workflow_trace": result.workflow_trace,
            "pending_human_interaction": (
                result.pending_human_interaction.model_dump(mode="json")
                if result.pending_human_interaction is not None
                else None
            ),
            "formulation_decision": (
                result.formulation_decision.model_dump(mode="json")
                if result.formulation_decision is not None
                else None
            ),
            "structured_key_validation": (
                result.structured_key_validation.model_dump(mode="json")
                if result.structured_key_validation is not None
                else None
            ),
            "cuopt_dynamic_input_validation": (
                result.cuopt_dynamic_input_validation.model_dump(mode="json")
                if result.cuopt_dynamic_input_validation is not None
                else None
            ),
            "cuopt_dynamic_input_draft": (
                result.cuopt_dynamic_input_draft.model_dump(mode="json")
                if result.cuopt_dynamic_input_draft is not None
                else None
            ),
        }
        assert result.status == "plan_validated", json.dumps(
            diagnostic, ensure_ascii=False, indent=2
        )
        assert result.input_rejection is None
        assert result.route_validation is not None and result.route_validation.valid
        assert result.mapf_validation is not None and result.mapf_validation.valid

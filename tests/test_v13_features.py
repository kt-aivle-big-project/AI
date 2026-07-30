"""Regression tests for v13 evidence, cuOpt transport, UI summary, and mixed batch."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import types

from app.core.config import get_settings
from app.domain.schemas import (
    ContextSnapshot,
    CuOptDynamicInputDraft,
    CuOptFleetDraft,
    CuOptMapConstraintDraft,
    CuOptTaskDraft,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
)
from app.graph.frontend_explanation import _deterministic_summary
from app.graph.input_formulation import _canonicalize_normalized_request
from app.repositories.json_repository import JsonWarehouseRepository, get_repository
from app.services.context_service import WarehouseContextService
from app.services.cuopt_formulation_service import (
    CuOptDraftEvidenceEnricher,
    CuOptDynamicInputValidator,
    RuleCuOptFormulator,
)
from app.services.optimization_service import (
    CuOptNativeRequestBuilder,
    CuOptNativeResponseParser,
    CuOptPayloadBuilder,
    CuOptPayloadValidator,
)
from app.services.situation_graph_service import WarehouseSituationGraphBuilder
from scripts.run_v13_mixed_batch_scenario import build_problem, validate_fixture_operations

ROOT = Path(__file__).resolve().parents[1]


def _complete_vehicle_data(payload, vehicle_key: str | int = 0) -> dict:
    """Return one fake native route that covers every mandatory task row."""

    return {
        str(vehicle_key): {
            "task_id": [
                "Depot",
                *[str(index) for index in range(len(payload.task_data.task_ids))],
                "Depot",
            ]
        }
    }


def _graph_bundle():
    repository = get_repository()
    context = WarehouseContextService(repository)
    normalized = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            )
        ],
        constraints=NormalizedRequestConstraints(),
        normalization_summary="v13 test",
    )
    inventory = context.build_inventory_context(order_ids=["ORD-001"])
    robots = context.build_robot_context(required_capacity=1)
    map_bundle = context.build_map_context(inventory=inventory)
    versions = repository.versions
    snapshot = ContextSnapshot(
        snapshot_id="SNAP-V13-TEST",
        captured_at=datetime.now(timezone.utc).isoformat(),
        graph_version=versions["graph_version"],
        inventory_version=versions["inventory_version"],
        runtime_version=versions["runtime_version"],
    )
    graph = WarehouseSituationGraphBuilder(repository).build(
        normalized_request=normalized,
        snapshot=snapshot,
        inventory=inventory,
        robots=robots,
        map_context=map_bundle.context,
        graph_arcs=map_bundle.graph_arcs,
    )
    return normalized, graph, map_bundle


def test_status_references_are_separated_from_canonical_filters() -> None:
    request = NormalizedWarehouseRequest(
        source="natural_language",
        operations=[NormalizedOperation(operation_id="bearing", operation_type="OUTBOUND_ORDER")],
        constraints=NormalizedRequestConstraints(
            excluded_robot_statuses=["충전 중인 로봇", "working"],
            excluded_robot_references=["작업 중인 로봇"],
        ),
        raw_user_command="충전 중이거나 작업 중인 로봇은 제외해.",
        normalization_summary="test",
    )
    result = _canonicalize_normalized_request(request)
    assert result.constraints.excluded_robot_statuses == ["charging", "working"]
    assert set(result.constraints.excluded_robot_status_references) == {
        "충전 중인 로봇",
        "작업 중인 로봇",
    }
    assert result.constraints.excluded_robot_references == []


def test_evidence_enricher_fixes_only_missing_provenance() -> None:
    normalized, graph, _map_bundle = _graph_bundle()
    rule = RuleCuOptFormulator().formulate(
        normalized_request=normalized,
        graph=graph,
        time_limit_seconds=5,
    )
    task = rule.tasks[0]
    llm_like = rule.model_copy(
        update={
            "formulation_source": "llm",
            "tasks": [task.model_copy(update={"evidence_ids": []})],
            "fleet": rule.fleet.model_copy(update={"evidence_ids": []}),
            "map_constraints": rule.map_constraints.model_copy(update={"evidence_ids": []}),
        }
    )
    before_business = llm_like.model_dump(exclude={"tasks": {"__all__": {"evidence_ids"}}, "fleet": {"evidence_ids"}, "map_constraints": {"evidence_ids"}})
    enriched, audit = CuOptDraftEvidenceEnricher().enrich(draft=llm_like, graph=graph)
    after_business = enriched.model_dump(exclude={"tasks": {"__all__": {"evidence_ids"}}, "fleet": {"evidence_ids"}, "map_constraints": {"evidence_ids"}})
    assert before_business == after_business
    assert audit.applied
    assert enriched.tasks[0].evidence_ids
    validation = CuOptDynamicInputValidator().validate(
        draft=enriched,
        normalized_request=normalized,
        graph=graph,
        expected_source="llm",
    )
    assert validation.valid, validation.errors


def test_native_cuopt_builder_and_response_parser() -> None:
    request, payload, _map_context, _node_types, _metadata = build_problem(
        ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    )
    assert CuOptPayloadValidator().validate(payload).valid
    native = CuOptNativeRequestBuilder().build(payload)
    graph = native["cost_waypoint_graph_data"]["waypoint_graph"]["0"]
    time_graph = native["travel_time_waypoint_graph_data"]["waypoint_graph"]["0"]
    assert len(graph["offsets"]) == 221
    assert len(graph["edges"]) == 356
    assert len(graph["weights"]) == 356
    assert len(time_graph["offsets"]) == 221
    assert native["task_data"]["task_ids"] == payload.task_data.task_ids
    assert len(native["task_data"]["pickup_and_delivery_pairs"]) == 7
    assert native["fleet_data"]["vehicle_ids"] == payload.fleet_data.vehicle_ids
    assert native["fleet_data"]["vehicle_types"] == [0] * len(payload.fleet_data.vehicle_ids)
    raw = {
        "response": {
            "solver_response": {
                "status": 0,
                "solution_cost": 123.0,
                "vehicle_data": _complete_vehicle_data(payload),
                "dropped_tasks": {"task_id": []},
            }
        }
    }
    parsed = CuOptNativeResponseParser().parse(raw, payload)
    assert parsed.status == "success"
    assert parsed.routes[0].vehicle_id == payload.fleet_data.vehicle_ids[0]
    assert parsed.routes[0].task_sequence == payload.task_data.task_ids


def test_mixed_batch_has_four_outbound_three_inbound_and_five_robots() -> None:
    _request, payload, _map_context, _node_types, metadata = build_problem(
        ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    )
    operations = metadata["operations"]
    assert sum(value["operation_type"] == "OUTBOUND" for value in operations) == 4
    assert sum(value["operation_type"] == "INBOUND" for value in operations) == 3
    assert len(payload.task_data.pickup_and_delivery_pairs) == 7
    assert len(payload.task_data.task_ids) == 14
    assert len(payload.fleet_data.vehicle_ids) == 5
    fixture = ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    fixture_validation = validate_fixture_operations(fixture, operations)
    assert fixture_validation["valid"], fixture_validation["errors"]
    assert CuOptPayloadValidator().validate(payload).valid


def test_frontend_summary_is_fact_based_and_marks_payload_only() -> None:
    state = {
        "simulation_id": "SIM-FRONT",
        "workflow_status": "ready_for_cuopt",
        "node_execution_log": [],
        "llm_node_summaries": [],
        "retrieval_agent_step_count": 2,
        "formulation_retry_count": 0,
    }
    summary = _deterministic_summary(state)  # type: ignore[arg-type]
    assert summary.status_label == "최적화 입력 준비 완료"
    assert "cuOpt" in summary.next_action
    assert "실행 계획 검증 완료" not in summary.summary_text


def test_settings_normalize_documented_cuopt_transport_aliases() -> None:
    from app.core.config import Settings

    assert Settings(CUOPT_TRANSPORT="managed_thin_client").cuopt_transport == "managed"
    assert Settings(CUOPT_TRANSPORT="self-hosted").cuopt_transport == "http"
    assert Settings(CUOPT_TRANSPORT="build_api").cuopt_transport == "nvidia_api"


def test_frontend_timeline_uses_actual_node_records() -> None:
    from app.domain.schemas import NodeExecutionRecord

    state = {
        "simulation_id": "SIM-TIMELINE",
        "workflow_status": "ready_for_cuopt",
        "node_execution_log": [
            NodeExecutionRecord(
                node_name="llm_cuopt_formulator",
                purpose="상황 그래프 근거로 cuOpt 입력 작성",
                status="success",
                started_at="2026-07-24T00:00:00Z",
                ended_at="2026-07-24T00:00:01Z",
                duration_ms=1000.0,
                llm_used=True,
            )
        ],
        "llm_node_summaries": [],
    }
    summary = _deterministic_summary(state)  # type: ignore[arg-type]
    assert len(summary.timeline) == 1
    assert summary.timeline[0].phase == "LLM"
    assert summary.timeline[0].llm_used is True


def test_dashboard_event_carries_frontend_prose() -> None:
    from app.domain.schemas import FrontendExecutionSummary
    from app.graph.terminal import dashboard_event_node

    frontend = FrontendExecutionSummary(
        generation_source="deterministic",
        headline="cuOpt 입력 검증 완료",
        status_label="최적화 입력 준비 완료",
        summary_text="작업 7건의 입력을 검증했습니다.",
        next_action="cuOpt를 실행하세요.",
        debug_note="test",
    )
    update = dashboard_event_node(
        {
            "simulation_id": "SIM-DASH",
            "workflow_status": "ready_for_cuopt",
            "frontend_summary": frontend,
        }  # type: ignore[arg-type]
    )
    event = update["dashboard_event"]
    assert event.headline == frontend.headline
    assert event.summary_text == frontend.summary_text
    assert event.next_action == frontend.next_action


def test_legitimate_cuopt_http_key_suffix_is_not_treated_as_placeholder() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None, CUOPT_HTTP_API_KEY="tenant-prod-key").cuopt_http_api_key == "tenant-prod-key"


def test_cuopt_http_auth_headers_do_not_drop_legitimate_key() -> None:
    from app.core.config import Settings
    from app.services.optimization_service import ExternalCuOptGateway

    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        CUOPT_HTTP_AUTH_MODE="x-api-key",
        CUOPT_HTTP_API_KEY="tenant-prod-key",
        CUOPT_HTTP_API_KEY_HEADER="X-API-Key",
    )
    headers = gateway._headers()
    assert headers["X-API-Key"] == "tenant-prod-key"
    assert "tenant-prod-key" not in repr({"transport": gateway.settings.cuopt_transport})


def test_managed_sak_configuration_and_polling(monkeypatch) -> None:
    from app.core.config import Settings
    from app.services import optimization_service as module
    from app.services.optimization_service import ExternalCuOptGateway

    _request, payload, _map_context, _node_types, _metadata = build_problem(
        ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    )
    vehicle_id = payload.fleet_data.vehicle_ids[0]
    solver_body = {
        "response": {
            "solver_response": {
                "status": 0,
                "solution_cost": 3.0,
                "vehicle_data": _complete_vehicle_data(payload, vehicle_id),
                "dropped_tasks": {"task_id": []},
            }
        },
        "reqId": "M-1",
    }

    class FakeManagedClient:
        instances: list["FakeManagedClient"] = []

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.repoll_calls: list[tuple[str, str]] = []
            self.__class__.instances.append(self)

        def get_optimized_routes(self, _data: dict) -> dict:
            return {"reqId": "M-1"}

        def repoll(self, req_id: str, *, response_type: str) -> dict:
            self.repoll_calls.append((req_id, response_type))
            return solver_body

    monkeypatch.setitem(
        sys.modules,
        "cuopt_thin_client",
        types.SimpleNamespace(CuOptServiceClient=FakeManagedClient),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _value: None)
    settings = Settings(
        CUOPT_TRANSPORT="managed",
        CUOPT_CLIENT_SAK="sak-value",
        CUOPT_FUNCTION_ID="function-id",
        CUOPT_POLL_INTERVAL_SECONDS=0.001,
        CUOPT_MAX_POLL_ATTEMPTS=2,
    )
    assert settings.cuopt_managed_credentials_configured
    gateway = ExternalCuOptGateway()
    gateway.settings = settings
    result = gateway.solve(payload)
    assert result.status == "success"
    instance = FakeManagedClient.instances[-1]
    assert instance.kwargs == {
        "sak": "sak-value",
        "function_id": "function-id",
        "timeout_exception": False,
    }
    assert instance.repoll_calls == [("M-1", "dict")]


def test_managed_sak_accepts_nvidia_standard_env_alias() -> None:
    from app.core.config import Settings

    settings = Settings(
        NVIDIA_IDENTITY_FEDERATION_API_KEY="identity-key",
        CUOPT_FUNCTION_ID="function-id",
    )
    assert settings.effective_cuopt_client_sak == "identity-key"
    assert settings.cuopt_managed_credentials_configured


class _FakeHTTPResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.is_success = 200 <= status_code < 300

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, post_payload: dict, get_payloads: list[dict] | None = None) -> None:
        self.post_payload = post_payload
        self.get_payloads = list(get_payloads or [])
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeHTTPResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeHTTPResponse(self.post_payload)

    def get(self, url: str, *, headers: dict) -> _FakeHTTPResponse:
        self.gets.append(url)
        if not self.get_payloads:
            raise AssertionError("Unexpected extra poll")
        return _FakeHTTPResponse(self.get_payloads.pop(0))


def test_external_cuopt_http_sync_and_poll_paths(monkeypatch) -> None:
    from app.core.config import Settings
    from app.services import optimization_service as module
    from app.services.optimization_service import ExternalCuOptGateway

    _request, payload, _map_context, _node_types, _metadata = build_problem(
        ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    )
    vehicle_id = payload.fleet_data.vehicle_ids[0]
    solver_body = {
        "response": {
            "solver_response": {
                "status": 0,
                "solution_cost": 9.0,
                "vehicle_data": _complete_vehicle_data(payload, vehicle_id),
                "dropped_tasks": {"task_id": []},
            }
        }
    }

    sync_client = _FakeHTTPClient(solver_body)
    monkeypatch.setattr(module.httpx, "Client", lambda **_kwargs: sync_client)
    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        CUOPT_TRANSPORT="http",
        CUOPT_API_URL="http://cuopt.local/cuopt/request",
        CUOPT_HTTP_AUTH_MODE="bearer",
        CUOPT_HTTP_API_KEY="secret-token",
    )
    sync_result = gateway.solve(payload)
    assert sync_result.status == "success"
    assert sync_result.routes[0].vehicle_id == vehicle_id
    assert sync_client.posts[0]["headers"]["Authorization"] == "Bearer secret-token"

    poll_client = _FakeHTTPClient({"reqId": "REQ-1"}, [solver_body])
    monkeypatch.setattr(module.httpx, "Client", lambda **_kwargs: poll_client)
    monkeypatch.setattr(module.time, "sleep", lambda _value: None)
    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        CUOPT_TRANSPORT="http",
        CUOPT_API_URL="http://cuopt.local/cuopt/request",
        CUOPT_SOLUTION_URL_TEMPLATE="http://cuopt.local/cuopt/solution/{req_id}",
        CUOPT_POLL_INTERVAL_SECONDS=0.001,
        CUOPT_MAX_POLL_ATTEMPTS=2,
    )
    poll_result = gateway.solve(payload)
    assert poll_result.status == "success"
    assert poll_client.gets == ["http://cuopt.local/cuopt/solution/REQ-1"]

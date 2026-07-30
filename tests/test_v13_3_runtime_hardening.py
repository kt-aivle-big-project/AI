"""Runtime hardening for Windows console output and solver preflight checks."""
from __future__ import annotations

import io
from pathlib import Path

from app.core.console import safe_console_print
from app.services.optimization_service import CuOptPayloadValidator
from scripts.run_v13_mixed_batch_scenario import build_problem

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"


def _payload():
    return build_problem(FIXTURE)[1]


def test_cp949_console_does_not_crash_on_em_dash() -> None:
    """Unsupported LLM punctuation must be escaped rather than aborting LangGraph."""

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp949", errors="strict")
    safe_console_print("오케스트레이션 실패 — 오류 확인 필요", stream=stream)
    stream.flush()
    text = raw.getvalue().decode("cp949")
    assert "오케스트레이션 실패" in text
    assert "\\u2014" in text


def test_open_route_payload_has_at_least_one_vehicle_for_each_mandatory_pair() -> None:
    payload = _payload()
    validation = CuOptPayloadValidator().validate(payload)
    assert validation.valid, validation.errors
    assert any("Open-route policy" in warning for warning in validation.warnings)


def test_closed_route_preflight_rejects_unreachable_return() -> None:
    payload = _payload()
    vehicle_count = len(payload.fleet_data.vehicle_ids)
    closed = payload.model_copy(
        update={
            "fleet_data": payload.fleet_data.model_copy(
                update={
                    "drop_return_trips": [False] * vehicle_count,
                    "vehicle_end_locations": list(payload.fleet_data.vehicle_start_locations),
                }
            )
        }
    )
    validation = CuOptPayloadValidator().validate(closed)
    assert not validation.valid
    assert any("configured vehicle end" in error for error in validation.errors)


def test_preflight_rejects_task_with_no_reachable_vehicle() -> None:
    payload = _payload()
    r3_0 = payload.location_index_map["R3_0"]
    graph = payload.waypoint_graph_data
    keep = [
        index
        for index, source in enumerate(graph.from_indices)
        if source != r3_0
    ]
    isolated_graph = graph.model_copy(
        update={
            "edge_ids": [graph.edge_ids[index] for index in keep],
            "from_indices": [graph.from_indices[index] for index in keep],
            "to_indices": [graph.to_indices[index] for index in keep],
            "costs": [graph.costs[index] for index in keep],
            "travel_times_ms": [graph.travel_times_ms[index] for index in keep],
        }
    )
    unreachable = payload.model_copy(
        update={
            "fleet_data": payload.fleet_data.model_copy(
                update={
                    "vehicle_start_locations": [r3_0] * len(payload.fleet_data.vehicle_ids),
                    "vehicle_end_locations": [r3_0] * len(payload.fleet_data.vehicle_ids),
                }
            ),
            "waypoint_graph_data": isolated_graph,
        }
    )
    validation = CuOptPayloadValidator().validate(unreachable)
    assert not validation.valid
    assert any("No eligible vehicle can execute mandatory pair" in error for error in validation.errors)


def test_optimizer_node_marks_external_unavailability_as_technical_failure(monkeypatch) -> None:
    """A provider failure must not be logged as a successful optimizer node."""

    from app.domain.schemas import OptimizerResult
    from app.graph import optimization as module

    payload = _payload()

    class FakeGateway:
        def solve(self, _payload):
            return OptimizerResult(
                backend="cuopt",
                status="unavailable",
                optimizer="nvidia-cuopt-api",
                reason="NVIDIA cuOpt HTTP 422: schema mismatch",
                errors=["cuopt_http_422"],
            )

    monkeypatch.setattr(module, "ExternalCuOptGateway", FakeGateway)
    update = module.optimizer_node(
        {
            "optimization_backend": "cuopt",
            "cuopt_payload": payload,
        }
    )
    assert update["failure_requested"] is True
    assert update["failure_stage"] == "optimizer"
    assert update["optimizer_result"].status == "unavailable"
    assert update["errors"][0].code == "cuopt_http_422"
    assert update["node_execution_log"][0].status == "failed"

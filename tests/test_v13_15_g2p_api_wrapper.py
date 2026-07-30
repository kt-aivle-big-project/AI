"""The deprecated G2P plan endpoint must delegate to the canonical orchestration graph."""
from __future__ import annotations

from types import SimpleNamespace

from app.domain.schemas import GoodsToPersonPlanRequest


def test_compatibility_plan_endpoint_builds_one_canonical_mission(monkeypatch):
    import app.api.routes as routes

    captured = {}

    class FakeService:
        def run(self, request, *, trusted_planning_mode=None):
            captured["request"] = request
            captured["trusted_planning_mode"] = trusted_planning_mode
            return SimpleNamespace(status="ready_for_cuopt")

    monkeypatch.setattr(routes, "OrchestrationService", FakeService)
    result = routes.plan_goods_to_person(
        GoodsToPersonPlanRequest(
            simulation_id="SIM-API-G2P",
            order_ids=["ORD-001", "ORD-002"],
            optimization_backend="cuopt_payload_only",
        )
    )

    request = captured["request"]
    assert result.status == "ready_for_cuopt"
    assert captured["trusted_planning_mode"] == "force_rule"
    assert request.request_mode == "event_driven"
    assert [value.order_id for value in request.events] == ["ORD-001", "ORD-002"]
    assert request.goods_to_person_options is not None

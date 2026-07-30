from __future__ import annotations

from datetime import datetime, timezone

from app.domain.schemas import HumanInteractionRequest
from app.graph.hitl import human_interaction_pause_node
from app.graph.terminal import persist_result_node


def test_shadow_comparison_does_not_create_hitl_checkpoint(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("shadow comparison must not persist HITL")

    monkeypatch.setattr(
        "app.graph.hitl.HumanInteractionService.create_pending",
        fail,
    )
    interaction = HumanInteractionRequest(
        interaction_id="HITL-SHADOW",
        kind="APPROVAL",
        stage="PRE_ROUTE",
        reason_code="SHADOW_TEST",
        headline="shadow",
        prompt="shadow",
        default_action="HOLD",
        route_locked=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    result = human_interaction_pause_node(
        {
            "simulation_id": "SIM-SHADOW",
            "pending_human_interaction": interaction,
            "evaluation_shadow_mode": True,
        }
    )
    assert result["workflow_status"] == "awaiting_human_approval"


def test_shadow_comparison_skips_general_runtime_persistence() -> None:
    result = persist_result_node(
        {
            "simulation_id": "SIM-SHADOW",
            "evaluation_shadow_mode": True,
        }
    )
    assert result["persistence"].status == "skipped"
    assert "read-only" in (result["persistence"].reason or "")

from pydantic import TypeAdapter

from app.domain.schemas import EventInput, ReplanReason
from app.graph.input_formulation import _structured_normalized_request
from app.services.be_centered_plan_service import _trusted_replan_planning_mode
from app.services.terminal_relocation_service import charge_service_duration_ms


def test_low_battery_is_a_supported_replan_reason() -> None:
    assert TypeAdapter(ReplanReason).validate_python("LOW_BATTERY") == "LOW_BATTERY"
    assert _trusted_replan_planning_mode("LOW_BATTERY") == "force_rule"
    assert _trusted_replan_planning_mode("NEW_ORDER") is None


def test_low_battery_signal_does_not_create_fake_business_work() -> None:
    normalized = _structured_normalized_request(
        {"events": [EventInput(type="low_battery")]}  # type: ignore[arg-type]
    )

    assert normalized.operations == []


def test_charge_service_reserves_time_to_reach_full_battery() -> None:
    assert charge_service_duration_ms(
        battery_pct=20,
        charge_rate_pct_per_minute=50,
        minimum_service_ms=500,
    ) == 96_000


def test_charge_service_keeps_minimum_duration_for_full_robot() -> None:
    assert charge_service_duration_ms(
        battery_pct=100,
        charge_rate_pct_per_minute=50,
        minimum_service_ms=500,
    ) == 500

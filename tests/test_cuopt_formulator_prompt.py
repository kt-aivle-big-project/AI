from app.prompts.cuopt_formulator import CUOPT_FORMULATOR_SYSTEM, PROMPT_VERSION


def test_agent_reassesses_implicit_cost_default_from_live_context() -> None:
    assert PROMPT_VERSION == "13.35-contextual-objective-selection"
    assert "implicit MIN_TOTAL_COST value is a neutral request default" in CUOPT_FORMULATOR_SYSTEM
    assert "large independent wave" in CUOPT_FORMULATOR_SYSTEM
    assert "BALANCED, not implicit MIN_TOTAL_COST" in CUOPT_FORMULATOR_SYSTEM


def test_agent_coordinates_parallelism_with_soft_route_balance() -> None:
    assert "Keep objective_profile and minimum_vehicle_count coherent" in CUOPT_FORMULATOR_SYSTEM
    assert "Do not use minimum_vehicle_count as a substitute for workload balance" in CUOPT_FORMULATOR_SYSTEM
    assert "Never output raw" in CUOPT_FORMULATOR_SYSTEM
    assert "solver weights" in CUOPT_FORMULATOR_SYSTEM

"""Guard all live LLM response models against OpenAI strict-schema regressions."""
from __future__ import annotations

from typing import Any

import pytest

from app.domain.schemas import (
    CuOptDynamicInputDraft,
    FormulationRecommendation,
    FrontendNarrativeText,
    NormalizedWarehouseRequest,
    RoutedNormalizedWarehouseRequest,
    RetrievalAgentStep,
)


LIVE_LLM_OUTPUT_MODELS = (
    NormalizedWarehouseRequest,
    RoutedNormalizedWarehouseRequest,
    RetrievalAgentStep,
    CuOptDynamicInputDraft,
    FormulationRecommendation,
    FrontendNarrativeText,
)


def _open_object_paths(schema: dict[str, Any]) -> list[str]:
    """Return object-schema paths that allow or omit additional properties."""

    failures: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object" and value.get("additionalProperties") is not False:
                failures.append(path)
            for key, child in value.items():
                visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    visit(schema, "$")
    return failures


@pytest.mark.parametrize("model_type", LIVE_LLM_OUTPUT_MODELS)
def test_live_llm_output_schema_is_closed_for_openai_strict_mode(model_type: type) -> None:
    """Every JSON object sent as a strict response format must be closed."""

    failures = _open_object_paths(model_type.model_json_schema())
    assert failures == [], (
        f"{model_type.__name__} contains object schemas without "
        f"additionalProperties=false: {failures}"
    )


def test_gateway_preflight_rejects_open_dict_schema() -> None:
    """The gateway should fail locally before issuing a provider request."""

    from pydantic import BaseModel

    from app.core.llm_gateway import LLMConfigurationError, validate_openai_strict_output_model

    class UnsafeOutput(BaseModel):
        attributes: dict[str, object]

    with pytest.raises(LLMConfigurationError, match="open object paths"):
        validate_openai_strict_output_model(UnsafeOutput)

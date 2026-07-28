from typing import Any

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.models import (
    CommandInterpretation,
    FinalReportOutput,
    HypotheticalEvent,
    ScopeDecision,
    SupervisorDecision,
    VerificationDecision,
)


STRUCTURED_OUTPUT_MODELS = (
    CommandInterpretation,
    SupervisorDecision,
    VerificationDecision,
    ScopeDecision,
    FinalReportOutput,
)


def _object_schema_failures(
    value: Any,
    *,
    path: str = "$",
) -> list[str]:
    failures: list[str] = []
    if isinstance(value, dict):
        if (
            value.get("type") == "object"
            and value.get("additionalProperties") is not False
        ):
            failures.append(path)
        for key, nested in value.items():
            failures.extend(
                _object_schema_failures(nested, path=f"{path}.{key}")
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            failures.extend(
                _object_schema_failures(nested, path=f"{path}[{index}]")
            )
    return failures


@pytest.mark.parametrize("model", STRUCTURED_OUTPUT_MODELS)
def test_openai_structured_output_schema_forbids_additional_properties(model):
    # This is the same strict-schema conversion used by the OpenAI Python SDK
    # before a Pydantic response_format is sent to the API.
    schema = to_strict_json_schema(model)

    assert _object_schema_failures(schema) == []


def test_command_interpretation_parameters_are_a_typed_closed_object():
    schema = CommandInterpretation.model_json_schema()
    parameter_schema = schema["$defs"]["HypotheticalEventParameters"]

    assert parameter_schema["type"] == "object"
    assert parameter_schema["additionalProperties"] is False
    assert set(parameter_schema["properties"]) == {
        "battery_percent",
        "delay_seconds",
        "inventory_quantity",
    }


def test_hypothetical_event_parameters_keep_the_existing_object_response_shape():
    empty = HypotheticalEvent(event_type="TASK_DELAY")
    delayed = HypotheticalEvent(
        event_type="TASK_DELAY",
        parameters={"delay_seconds": 30},
    )

    assert empty.model_dump(mode="json")["parameters"] == {}
    assert delayed.model_dump(mode="json")["parameters"] == {
        "delay_seconds": 30
    }


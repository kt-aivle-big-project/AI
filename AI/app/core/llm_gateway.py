"""Strict LangChain structured-output gateway with no runtime fallback."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from app.core.config import get_settings

TModel = TypeVar("TModel", bound=BaseModel)


def _strict_schema_open_object_paths(schema: dict[str, Any]) -> list[str]:
    """Return JSON-object paths incompatible with OpenAI strict schemas."""

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


def validate_openai_strict_output_model(output_model: type[BaseModel]) -> None:
    """Fail locally when a response model contains an open-ended object.

    OpenAI strict structured output requires ``additionalProperties=false`` for
    every object.  Pydantic ``dict[str, Any]`` fields violate that contract.
    Running this check before the network call avoids a slow provider-side 400.
    """

    failures = _strict_schema_open_object_paths(output_model.model_json_schema())
    if failures:
        raise LLMConfigurationError(
            f"{output_model.__name__} is not compatible with OpenAI strict JSON schema; "
            f"open object paths: {failures}"
        )


class LLMConfigurationError(RuntimeError):
    """Raised when a live LLM node lacks provider configuration."""


class LLMInvocationError(RuntimeError):
    """Raised when a provider call or structured-output validation fails."""


@runtime_checkable
class StructuredLLMGateway(Protocol):
    """Protocol used by live LLM graph nodes."""

    def invoke_structured(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        output_model: type[TModel],
        trace_name: str,
        tags: list[str],
        metadata: dict[str, Any],
    ) -> TModel:
        """Invoke a model and return a validated structured result."""


class LangChainOpenAIGateway:
    """OpenAI implementation built on LangChain ChatOpenAI."""

    def __init__(self) -> None:
        """Create the live model after validating the API key."""

        settings = get_settings()
        if not settings.openai_api_key:
            raise LLMConfigurationError(
                "OPENAI_API_KEY is required for the graph-grounded warehouse agent."
            )
        from langchain_openai import ChatOpenAI

        self.model_name = settings.openai_model
        self._model = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0,
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )

    def invoke_structured(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        output_model: type[TModel],
        trace_name: str,
        tags: list[str],
        metadata: dict[str, Any],
    ) -> TModel:
        """Invoke the model with strict JSON schema and LangSmith metadata."""

        from langchain_core.messages import HumanMessage, SystemMessage

        validate_openai_strict_output_model(output_model)

        try:
            runnable = self._model.with_structured_output(output_model, method="json_schema", strict=True)
            result = runnable.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=json.dumps(user_payload, ensure_ascii=False, default=str)),
                ],
                config={
                    "run_name": trace_name,
                    "tags": ["laro", "structured-output", *tags],
                    "metadata": {
                        "laro_output_schema": output_model.__name__,
                        "laro_model": self.model_name,
                        **metadata,
                    },
                },
            )
        except Exception as exc:
            raise LLMInvocationError(f"Structured LLM invocation failed: {exc}") from exc
        return result if isinstance(result, output_model) else output_model.model_validate(result)


@lru_cache
def get_default_llm_gateway() -> StructuredLLMGateway:
    """Return the cached live OpenAI gateway."""

    return LangChainOpenAIGateway()

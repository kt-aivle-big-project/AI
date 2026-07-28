"""Sanitize provider diagnostics for end-user API contracts.

Internal provider errors remain available through FULL/DEBUG/evidence views.
COMPACT and purpose-specific public views use stable operational wording.
"""

from __future__ import annotations

import re
from typing import Any


FALLBACK_OPTIMIZER_PUBLIC_MESSAGE = (
    "기본 최적화 엔진을 사용할 수 없어 "
    "대체 최적화 엔진으로 계획했습니다."
)

_PROVIDER_WARNING_MARKERS = (
    "cuOpt 호출 실패로 CPU optimizer를 사용했습니다",
    "CUOPT_SUBMIT_HTTP",
    "CuOptRestError",
    "pickup_and_delivery_pairs",
)


def sanitize_public_warning(value: Any) -> Any:
    """Replace provider-specific optimizer diagnostics with public wording."""

    if not isinstance(value, str):
        return value
    if any(marker in value for marker in _PROVIDER_WARNING_MARKERS):
        return FALLBACK_OPTIMIZER_PUBLIC_MESSAGE
    return value


def sanitize_public_warnings(values: Any) -> list[Any]:
    """Sanitize and deduplicate a public warning/error list."""

    rows = values if isinstance(values, list) else []
    deduplicated: list[Any] = []
    for value in rows:
        public = sanitize_public_warning(value)
        if public not in deduplicated:
            deduplicated.append(public)
    return deduplicated


def sanitize_public_verification(value: Any) -> dict[str, Any]:
    """Return a consumer-safe verification payload."""

    verification = dict(value) if isinstance(value, dict) else {}
    for key in (
        "warning_findings",
        "user_visible_warnings",
        "blocking_findings",
    ):
        if key in verification:
            verification[key] = sanitize_public_warnings(verification.get(key))
    return verification


def sanitize_public_answer(value: Any) -> str | None:
    """Remove provider diagnostics embedded in a user report."""

    if not isinstance(value, str):
        return None
    sanitized = re.sub(
        r"- cuOpt 호출 실패로 CPU optimizer를 사용했습니다:.*?(?=\n\n|\Z)",
        f"- {FALLBACK_OPTIMIZER_PUBLIC_MESSAGE}",
        value,
        flags=re.DOTALL,
    )
    # Handle provider messages with different prefixes while retaining the
    # surrounding report structure.
    sanitized = re.sub(
        r"(?m)^- [^\n]*(?:CUOPT_SUBMIT_HTTP|CuOptRestError|pickup_and_delivery_pairs)[^\n]*$",
        f"- {FALLBACK_OPTIMIZER_PUBLIC_MESSAGE}",
        sanitized,
    )
    return sanitized

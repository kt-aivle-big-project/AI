"""Cross-platform console output helpers.

Windows PowerShell commonly exposes a CP949 stdout stream.  LLM-authored text
may contain characters such as an em dash (U+2014), which raises
``UnicodeEncodeError`` when written directly.  These helpers prefer UTF-8 and
always fall back to an encoding-safe representation instead of crashing the
workflow after the actual planning work has completed.
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def configure_console_utf8() -> None:
    """Best-effort UTF-8 configuration for stdout and stderr.

    ``TextIOWrapper.reconfigure`` is unavailable for some captured or embedded
    streams, so every operation is intentionally defensive.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (AttributeError, OSError, ValueError):
                pass


def _encoding_safe_text(message: str, stream: TextIO) -> str:
    """Return text representable by the stream's current encoding."""

    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        message.encode(encoding)
        return message
    except UnicodeEncodeError:
        return message.encode(encoding, errors="backslashreplace").decode(
            encoding,
            errors="strict",
        )


def safe_console_print(message: object, *, stream: TextIO | None = None) -> None:
    """Print without allowing a terminal encoding mismatch to abort execution."""

    target = stream or sys.stdout
    text = str(message)
    try:
        print(text, file=target, flush=True)
    except UnicodeEncodeError:
        target.write(_encoding_safe_text(text, target) + "\n")
        target.flush()


def safe_json_print(value: Any, *, indent: int = 2) -> None:
    """Serialize JSON for the console while preserving Unicode when possible."""

    safe_console_print(json.dumps(value, ensure_ascii=False, indent=indent, default=str))


configure_console_utf8()

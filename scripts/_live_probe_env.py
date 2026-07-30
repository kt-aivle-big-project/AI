"""Backward-compatible wrapper around the centralized Settings-file selector."""
from __future__ import annotations

from pathlib import Path

from scripts._runtime_env import selected_env_path, select_settings_environment


def preload_live_probe_environment(
    root: Path,
    *,
    default_filename: str = ".env.docker",
    argv: list[str] | None = None,
) -> Path | None:
    del default_filename  # retained for older callers
    return select_settings_environment(selected_env_path(root, argv, prefer_docker=True))

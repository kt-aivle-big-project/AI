from __future__ import annotations

import os
from pathlib import Path

from app.core.config import create_settings
from scripts._live_probe_env import preload_live_probe_environment


def test_live_probe_selects_explicit_settings_file_without_manual_parsing(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env.docker"
    env_file.write_text(
        "\n".join(
            [
                "WAREHOUSE_REPOSITORY_BACKEND=live",
                "POSTGRES_DSN=postgresql://laro:laro@localhost:5432/laro",
                "MAP_REPOSITORY_BACKEND=neo4j",
                "NVIDIA_API_KEY=",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WAREHOUSE_REPOSITORY_BACKEND", "json")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-preserve-me")

    loaded = preload_live_probe_environment(
        tmp_path,
        argv=["--env-file", str(env_file)],
    )
    settings = create_settings()

    assert loaded == env_file.resolve()
    assert os.environ["LARO_ENV_FILE"] == str(env_file.resolve())
    assert settings.warehouse_repository_backend == "live"
    assert settings.map_repository_backend == "neo4j"
    # Empty sample secrets do not erase an exported credential.
    assert settings.nvidia_api_key == "nvapi-preserve-me"


def test_live_probe_prefers_docker_env(tmp_path: Path, monkeypatch) -> None:
    from scripts._runtime_env import bootstrap_live_probe_environment

    (tmp_path / ".env").write_text(
        "WAREHOUSE_REPOSITORY_BACKEND=json\nMAP_REPOSITORY_BACKEND=json\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.docker").write_text(
        "WAREHOUSE_REPOSITORY_BACKEND=live\nMAP_REPOSITORY_BACKEND=neo4j\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("WAREHOUSE_REPOSITORY_BACKEND", raising=False)
    monkeypatch.delenv("MAP_REPOSITORY_BACKEND", raising=False)

    loaded = bootstrap_live_probe_environment(tmp_path, argv=[])
    settings = create_settings()

    assert loaded == (tmp_path / ".env.docker").resolve()
    assert settings.warehouse_repository_backend == "live"
    assert settings.map_repository_backend == "neo4j"

from __future__ import annotations

from pathlib import Path

from app.core.config import create_settings


def test_explicit_settings_file_overrides_stale_process_infrastructure_values(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env.docker"
    env_file.write_text(
        "\n".join(
            [
                "WAREHOUSE_REPOSITORY_BACKEND=live",
                "MAP_REPOSITORY_BACKEND=neo4j",
                "POSTGRES_DSN=postgresql://laro:laro@localhost:5432/laro",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WAREHOUSE_REPOSITORY_BACKEND", "json")
    monkeypatch.setenv("MAP_REPOSITORY_BACKEND", "json")

    settings = create_settings(env_file)

    assert settings.warehouse_repository_backend == "live"
    assert settings.map_repository_backend == "neo4j"
    assert settings.postgres_dsn.endswith("localhost:5432/laro")


def test_blank_secret_in_explicit_file_does_not_erase_exported_secret(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env.docker"
    env_file.write_text("NVIDIA_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-existing")

    settings = create_settings(env_file)

    assert settings.nvidia_api_key == "nvapi-existing"

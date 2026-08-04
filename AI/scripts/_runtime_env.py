"""Select one Pydantic settings file for standalone scripts.

The previous hotfix parsed ``.env.docker`` manually into ``os.environ``.  This
module now performs only file selection and sets ``LARO_ENV_FILE``; the normal
Pydantic Settings factory remains the single dotenv parser and validator.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _argument_value(argv: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for index, value in enumerate(argv):
        if value.startswith(prefix):
            return value[len(prefix) :]
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


def selected_env_path(
    root: Path,
    argv: list[str] | None = None,
    *,
    prefer_docker: bool = False,
) -> Path | None:
    """Resolve ``--env-file`` or one project default without reading the file."""

    values = list(sys.argv[1:] if argv is None else argv)
    requested = _argument_value(values, "--env-file")
    if requested:
        path = Path(requested)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"Environment file not found: {path}")
        return path
    names = [".env.docker", ".env"] if prefer_docker else [".env", ".env.docker"]
    for name in names:
        path = (root / name).resolve()
        if path.exists():
            return path
    return None


def select_settings_environment(path: Path | None) -> Path | None:
    """Make the selected file visible to :func:`app.core.config.get_settings`."""

    if path is None:
        os.environ.pop("LARO_ENV_FILE", None)
        return None
    os.environ["LARO_ENV_FILE"] = str(path)
    return path


def bootstrap_script_environment(root: Path, argv: list[str] | None = None) -> Path | None:
    return select_settings_environment(selected_env_path(root, argv, prefer_docker=False))


def bootstrap_live_probe_environment(root: Path, argv: list[str] | None = None) -> Path | None:
    """Select ``.env.docker`` by default for scripts that must use live stores."""

    return select_settings_environment(selected_env_path(root, argv, prefer_docker=True))


def safe_postgres_endpoint(dsn: str) -> dict[str, object]:
    parsed = urlsplit(dsn)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/") or None,
        "username": parsed.username,
        "password_present": bool(parsed.password),
    }

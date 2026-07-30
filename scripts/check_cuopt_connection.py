"""Check cuOpt environment configuration without printing secrets."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.core.console import safe_json_print
from app.services.optimization_service import ExternalCuOptGateway


def main() -> int:
    settings = get_settings()
    summary = {
        "version": "13.17.0",
        "project_root": str(PROJECT_ROOT),
        "process_cwd": str(Path.cwd()),
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_exists": (PROJECT_ROOT / ".env").exists(),
        "expected_public_key_variable": "NVIDIA_API_KEY",
        "process_environment_has_nvidia_key": bool(os.environ.get("NVIDIA_API_KEY", "").strip()),
        "transport": settings.cuopt_transport,
        "payload_format": settings.cuopt_payload_format,
        "api_url": settings.cuopt_api_url if settings.cuopt_transport != "managed" else None,
        "auth_mode": settings.cuopt_http_auth_mode,
        "nvidia_build_api_key_configured": bool(settings.nvidia_build_api_key),
        "private_gateway_api_key_configured": bool(settings.cuopt_http_api_key),
        "managed_credentials_configured": settings.cuopt_managed_credentials_configured,
        "managed_sak_configured": bool(settings.effective_cuopt_client_sak),
        "managed_function_id_configured": bool(settings.cuopt_function_id),
        "legacy_managed_credentials_configured": bool(settings.cuopt_client_id and settings.cuopt_client_secret),
    }
    try:
        diagnostic = ExternalCuOptGateway().health_check()
        summary["diagnostic"] = diagnostic
        ok = bool(diagnostic.get("configured", diagnostic.get("ok", False)))
    except Exception as exc:
        summary["diagnostic"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = False
    safe_json_print(summary)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

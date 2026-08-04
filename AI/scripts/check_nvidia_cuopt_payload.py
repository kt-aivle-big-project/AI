"""Submit the mixed fixture to NVIDIA's routing validator without running the solver.

This diagnostic deliberately uses the same native payload builder as the normal
pipeline but sends ``cuOpt_RoutingValidator``.  It never prints the API key.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.core.console import safe_json_print
from app.services.optimization_service import CuOptNativeRequestBuilder, CuOptPayloadValidator
from scripts.run_v13_mixed_batch_scenario import build_problem


def _body(response: httpx.Response) -> object:
    try:
        return response.json()
    except Exception:
        return response.text[:4000]


def main() -> int:
    settings = get_settings()
    key = settings.nvidia_build_api_key
    if not key:
        safe_json_print({
            "version": "13.17.0",
            "status": "NOT_CONFIGURED",
            "error": "NVIDIA_API_KEY is missing.",
            "project_root": str(PROJECT_ROOT),
            "process_cwd": str(Path.cwd()),
            "env_file": str(PROJECT_ROOT / ".env"),
            "env_file_exists": (PROJECT_ROOT / ".env").exists(),
            "expected_variable": "NVIDIA_API_KEY",
            "transport": settings.cuopt_transport,
            "payload_format": settings.cuopt_payload_format,
            "next_check": (
                "Run: python -c \"from app.core.config import get_settings; "
                "s=get_settings(); print(bool(s.nvidia_build_api_key), s.cuopt_transport, "
                "s.cuopt_payload_format)\""
            ),
        })
        return 2

    fixture = PROJECT_ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    _request, payload, _map_context, _node_types, _metadata = build_problem(fixture)
    validation = CuOptPayloadValidator().validate(payload)
    if not validation.valid:
        safe_json_print({"status": "LOCAL_VALIDATION_FAILED", "errors": validation.errors})
        return 1

    native = CuOptNativeRequestBuilder().build(payload)
    token = key if key.lower().startswith("bearer ") else f"Bearer {key}"
    headers = {
        "Authorization": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    envelope = {
        "action": "cuOpt_RoutingValidator",
        "data": native,
        "client_version": settings.cuopt_client_version,
    }
    timeout = float(max(payload.time_limit_seconds + 30, 60))
    with httpx.Client(verify=settings.cuopt_verify_ssl, timeout=timeout) as client:
        response = client.post(settings.cuopt_api_url, json=envelope, headers=headers)
        raw = _body(response)
        if response.status_code == 202 and isinstance(raw, dict):
            request_id = raw.get("requestId") or raw.get("reqId")
            if request_id:
                url = settings.cuopt_solution_url_template.format(
                    req_id=request_id,
                    reqId=request_id,
                    requestId=request_id,
                ) if settings.cuopt_solution_url_template else f"https://optimize.api.nvidia.com/v1/status/{request_id}"
                for _ in range(settings.cuopt_max_poll_attempts):
                    time.sleep(settings.cuopt_poll_interval_seconds)
                    poll = client.get(url, headers=headers)
                    if poll.status_code == 202:
                        continue
                    response = poll
                    raw = _body(poll)
                    break

    result = {
        "version": "13.17.0",
        "project_root": str(PROJECT_ROOT),
        "env_file": str(PROJECT_ROOT / ".env"),
        "status": "PASS" if response.status_code in {200, 202} else "FAIL",
        "http_status": response.status_code,
        "request_body_keys": list(envelope),
        "parameters_included": "parameters" in envelope,
        "task_priorities_included": "priorities" in native["task_data"],
        "native_problem": {
            "locations": len(payload.location_index_map),
            "edges": len(payload.waypoint_graph_data.edge_ids),
            "task_rows": len(payload.task_data.task_ids),
            "vehicles": len(payload.fleet_data.vehicle_ids),
            "drop_return_trips": native["fleet_data"]["drop_return_trips"],
            "service_times_ms": native["task_data"]["service_times"],
        },
        "response": raw,
    }
    output = PROJECT_ROOT / "runtime_outputs" / "nvidia_cuopt_routing_validator.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    safe_json_print(result)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

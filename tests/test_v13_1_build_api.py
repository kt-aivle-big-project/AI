"""Direct NVIDIA Build/API Catalog cuOpt transport tests."""
from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.services.optimization_service import CuOptNativeRequestBuilder, ExternalCuOptGateway
from scripts.run_v13_mixed_batch_scenario import build_problem

ROOT = Path(__file__).resolve().parents[1]


def _payload():
    return build_problem(
        ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
    )[1]


def _solver_body(payload) -> dict:
    task_rows = [str(index) for index in range(len(payload.task_data.task_ids))]
    return {
        "response": {
            "solver_response": {
                "status": 0,
                "solution_cost": 12.0,
                "vehicle_data": {
                    "0": {"task_id": ["Depot", *task_rows, "Depot"]},
                },
                "dropped_tasks": {"task_id": []},
            }
        }
    }


class FakeResponse:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200, content: bytes | None = None):
        self._payload = payload or {}
        self.status_code = status_code
        self.content = content if content is not None else json.dumps(self._payload).encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")
        self.is_success = 200 <= status_code < 300

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self) -> dict:
        return self._payload


class FakeNvidiaClient:
    def __init__(self, *, solver_payload: dict, async_mode: bool = False, asset_mode: bool = False):
        self.solver_payload = solver_payload
        self.async_mode = async_mode
        self.asset_mode = asset_mode
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.puts: list[dict] = []
        self.deletes: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url: str, *, json: dict, headers: dict) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        if url.endswith("/assets"):
            assert self.asset_mode
            return FakeResponse({"assetId": "ASSET-1", "uploadUrl": "https://upload.example/asset"})
        if self.async_mode:
            self.async_mode = False
            return FakeResponse({"requestId": "REQ-1"}, status_code=202)
        return FakeResponse(self.solver_payload)

    def get(self, url: str, *, headers: dict | None = None) -> FakeResponse:
        self.gets.append({"url": url, "headers": headers})
        return FakeResponse(self.solver_payload)

    def put(self, url: str, *, content: bytes, headers: dict) -> FakeResponse:
        self.puts.append({"url": url, "content": content, "headers": headers})
        return FakeResponse({}, status_code=200)

    def delete(self, url: str, *, headers: dict) -> FakeResponse:
        self.deletes.append({"url": url, "headers": headers})
        return FakeResponse({}, status_code=204)


def test_build_api_key_needs_no_function_id() -> None:
    settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="nvapi-test-key",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
    )
    assert settings.cuopt_transport == "nvidia_api"
    assert settings.nvidia_build_api_key == "nvapi-test-key"
    assert settings.cuopt_nvidia_api_configured
    assert settings.cuopt_function_id is None


def test_sparse_native_request_is_compact_and_complete() -> None:
    payload = _payload()
    native = CuOptNativeRequestBuilder().build(payload)
    graph = native["cost_waypoint_graph_data"]["waypoint_graph"]["0"]
    encoded = json.dumps(native, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(graph["offsets"]) == len(payload.location_index_map) + 1
    assert len(graph["edges"]) == len(payload.waypoint_graph_data.edge_ids)
    assert len(native["task_data"]["pickup_and_delivery_pairs"]) == 7
    assert "priorities" not in native["task_data"]
    assert native["task_data"]["service_times"] == payload.task_data.service_times_ms
    assert len(native["fleet_data"]["vehicle_ids"]) == 5
    assert native["fleet_data"]["skip_first_trips"] == [False] * 5
    assert native["fleet_data"]["drop_return_trips"] == [True] * 5
    assert len(encoded) < 200_000


def test_nvidia_build_api_inline_envelope_and_polling(monkeypatch) -> None:
    from app.services import optimization_service as module

    payload = _payload()
    client = FakeNvidiaClient(solver_payload=_solver_body(payload), async_mode=True)
    monkeypatch.setattr(module.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(module.time, "sleep", lambda _value: None)

    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="nvapi-test-key",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
        CUOPT_SOLUTION_URL_TEMPLATE="https://optimize.api.nvidia.com/v1/status/{req_id}",
        CUOPT_INLINE_LIMIT_BYTES=200000,
        CUOPT_POLL_INTERVAL_SECONDS=0.001,
        CUOPT_MAX_POLL_ATTEMPTS=2,
    )
    result = gateway.solve(payload)
    assert result.status == "success"
    invocation = client.posts[0]
    assert invocation["url"] == "https://optimize.api.nvidia.com/v1/nvidia/cuopt"
    assert invocation["headers"]["Authorization"] == "Bearer nvapi-test-key"
    assert invocation["json"]["action"] == "cuOpt_OptimizedRouting"
    assert invocation["json"]["client_version"] == "custom"
    assert "parameters" not in invocation["json"]
    assert invocation["json"]["data"]["cost_waypoint_graph_data"]
    assert client.gets[0]["url"] == "https://optimize.api.nvidia.com/v1/status/REQ-1"


def test_nvidia_build_api_large_asset_path_and_cleanup(monkeypatch) -> None:
    from app.services import optimization_service as module

    payload = _payload()
    client = FakeNvidiaClient(solver_payload=_solver_body(payload), asset_mode=True)
    monkeypatch.setattr(module.httpx, "Client", lambda **_kwargs: client)

    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="nvapi-test-key",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
        CUOPT_SOLUTION_URL_TEMPLATE="https://optimize.api.nvidia.com/v1/status/{req_id}",
        CUOPT_INLINE_LIMIT_BYTES=1000,
        CUOPT_DELETE_ASSET_AFTER_SOLVE=True,
    )
    result = gateway.solve(payload)
    assert result.status == "success"
    assert client.posts[0]["url"].endswith("/assets")
    assert client.puts and client.puts[0]["url"] == "https://upload.example/asset"
    invocation = client.posts[1]
    assert invocation["json"]["data"] is None
    assert invocation["headers"]["NVCF-INPUT-ASSET-REFERENCES"] == "ASSET-1"
    assert client.deletes[0]["url"].endswith("/assets/ASSET-1")


def test_nvidia_build_api_missing_key_is_explicitly_unavailable() -> None:
    payload = _payload()
    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
    )
    result = gateway.solve(payload)
    assert result.status == "unavailable"
    assert "NVIDIA_API_KEY" in (result.reason or "")


def test_private_http_key_never_authorizes_nvidia_build_transport() -> None:
    """A private-gateway credential must not leak into the public Build path."""

    payload = _payload()
    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="",
        CUOPT_HTTP_API_KEY="private-gateway-only",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
    )
    result = gateway.solve(payload)
    assert result.status == "unavailable"
    assert "NVIDIA_API_KEY" in (result.reason or "")


def test_legacy_cuopt_api_key_name_is_ignored_for_build_transport() -> None:
    """The removed generic key name cannot silently authenticate Build API calls."""

    settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="",
        CUOPT_API_KEY="legacy-value-that-must-be-ignored",
    )
    assert settings.nvidia_build_api_key is None


def test_public_api_envelope_omits_runtime_rejected_parameters(monkeypatch) -> None:
    """The live Build endpoint rejects the legacy top-level parameters field."""

    from app.services import optimization_service as module

    payload = _payload()
    client = FakeNvidiaClient(solver_payload=_solver_body(payload))
    monkeypatch.setattr(module.httpx, "Client", lambda **_kwargs: client)

    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="nvapi-test-key",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
    )
    result = gateway.solve(payload)
    assert result.status == "success"
    envelope = client.posts[0]["json"]
    assert set(envelope) == {"action", "data", "client_version"}
    assert "parameters" not in envelope


def test_native_request_uses_open_routes_and_cuopt_priority_direction() -> None:
    """Warehouse batches end at the last task and lower values mean higher priority."""

    payload = _payload()
    native = CuOptNativeRequestBuilder().build(payload)
    assert native["fleet_data"]["drop_return_trips"] == [True] * len(payload.fleet_data.vehicle_ids)
    assert native["fleet_data"]["skip_first_trips"] == [False] * len(payload.fleet_data.vehicle_ids)
    priorities_by_task = dict(zip(payload.task_data.task_ids, payload.task_data.priorities, strict=True))
    assert priorities_by_task["ORD-001_PICK"] == 0
    assert priorities_by_task["ORD-004_PICK"] == 2


def test_nvidia_error_body_is_preserved(monkeypatch) -> None:
    """A provider 4xx should expose its structured body to the workflow result."""

    from app.services import optimization_service as module

    payload = _payload()
    error_body = {
        "detail": [
            {
                "type": "extra_forbidden",
                "loc": ["parameters"],
                "msg": "Extra inputs are not permitted",
                "input": {},
            }
        ]
    }
    client = FakeNvidiaClient(solver_payload=error_body)

    def error_post(url: str, *, json: dict, headers: dict) -> FakeResponse:
        client.posts.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(error_body, status_code=422)

    client.post = error_post  # type: ignore[method-assign]
    monkeypatch.setattr(module.httpx, "Client", lambda **_kwargs: client)

    gateway = ExternalCuOptGateway()
    gateway.settings = Settings(
        _env_file=None,
        CUOPT_TRANSPORT="nvidia_api",
        NVIDIA_API_KEY="nvapi-test-key",
        CUOPT_API_URL="https://optimize.api.nvidia.com/v1/nvidia/cuopt",
    )
    result = gateway.solve(payload)
    assert result.status == "unavailable"
    assert result.errors == ["cuopt_http_422"]
    assert "extra_forbidden" in (result.reason or "")
    assert "parameters" in (result.reason or "")

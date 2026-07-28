from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx


class RobotGateway:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.2,
    ):
        if not base_url:
            raise RuntimeError("EXECUTE에는 ROBOT_GATEWAY_URL이 필요합니다.")
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/dispatch"
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    @staticmethod
    def _identity(plan_version: str, batches: list[dict[str, Any]]) -> tuple[str, str]:
        canonical = json.dumps(
            {"plan_version": plan_version, "batches": batches},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        dispatch_id = str(
            uuid5(NAMESPACE_URL, f"robot-gateway:{plan_version}:{fingerprint}")
        )
        return dispatch_id, fingerprint

    def dispatch_identity(
        self, plan_version: str, batches: list[dict[str, Any]]
    ) -> dict[str, Any]:
        dispatch_id, fingerprint = self._identity(plan_version, batches)
        return {
            "dispatch_id": dispatch_id,
            "idempotency_key": dispatch_id,
            "payload_fingerprint": fingerprint,
            "identity_source": "DETERMINISTIC_PRECOMPUTED",
        }

    def dispatch(self, plan_version: str, batches: list[dict[str, Any]]) -> dict[str, Any]:
        identity = self.dispatch_identity(plan_version, batches)
        dispatch_id = str(identity["dispatch_id"])
        fingerprint = str(identity["payload_fingerprint"])
        payload = {
            "dispatch_id": dispatch_id,
            "idempotency_key": dispatch_id,
            "payload_fingerprint": fingerprint,
            "plan_version": plan_version,
            "batches": batches,
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = httpx.post(
                    self.url,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                result = response.json()
                return {
                    **result,
                    "dispatch_id": result.get("dispatch_id") or dispatch_id,
                    "idempotency_key": dispatch_id,
                    "payload_fingerprint": fingerprint,
                    "gateway_attempt_count": attempt,
                }
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    break
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * attempt)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code
                if status_code < 500 or attempt >= self.max_attempts:
                    break
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * attempt)
        raise RuntimeError(
            f"ROBOT_GATEWAY_DISPATCH_FAILED:{self.max_attempts}:{last_error}"
        ) from last_error

    def cancel(
        self,
        dispatch_id: str,
        plan_version: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/dispatches/{dispatch_id}/cancel",
            json={"plan_version": plan_version, "reason": reason},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

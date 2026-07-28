"""Execution-event ordering, payload identity, and recovery helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from app.models import RobotEvent
from app.time_utils import as_utc_datetime


# Higher values win when two distinct events have the exact same timestamp.
EVENT_PRECEDENCE: dict[str, int] = {
    "POSITION_UPDATED": 10,
    "ROBOT_DELAYED": 20,
    "PATH_BLOCKED": 25,
    "PATH_DEVIATED": 30,
    "LOW_BATTERY": 35,
    "TASK_STARTED": 40,
    "TASK_FAILED": 80,
    "ROBOT_FAILED": 90,
    "TASK_COMPLETED": 100,
    "INBOUND_AVAILABLE": 100,
}


class StaleExecutionEventError(RuntimeError):
    def __init__(self, evidence: dict[str, Any]):
        super().__init__("STALE_EXECUTION_EVENT")
        self.code = "STALE_EXECUTION_EVENT"
        self.evidence = evidence


@dataclass(frozen=True)
class EventWatermark:
    event_id: str
    event_type: str
    occurred_at: datetime
    precedence: int

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "EventWatermark | None":
        if not value or not value.get("occurred_at"):
            return None
        return cls(
            event_id=str(value.get("event_id") or ""),
            event_type=str(value.get("event_type") or ""),
            occurred_at=as_utc_datetime(
                value["occurred_at"], field_name="event_watermark.occurred_at"
            ),
            precedence=int(
                value.get("precedence")
                or EVENT_PRECEDENCE.get(str(value.get("event_type") or ""), 0)
            ),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "occurred_at_us": str(int(self.occurred_at.timestamp() * 1_000_000)),
            "precedence": str(self.precedence),
        }


def event_watermark(event: RobotEvent) -> EventWatermark:
    return EventWatermark(
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        precedence=EVENT_PRECEDENCE.get(event.event_type, 0),
    )


def ordering_evidence(
    event: RobotEvent,
    current: EventWatermark | None,
    *,
    decision: str,
) -> dict[str, Any]:
    incoming = event_watermark(event)
    return {
        "policy": "EVENT_TIME_PRECEDENCE_EVENT_ID",
        "decision": decision,
        "incoming": incoming.as_mapping(),
        "current": current.as_mapping() if current else None,
    }


def compare_event_order(
    event: RobotEvent,
    current: EventWatermark | None,
) -> tuple[bool, str]:
    """Return whether the event may mutate state and the deterministic reason."""

    if current is None:
        return True, "FIRST_EVENT"
    incoming = event_watermark(event)
    if incoming.event_id == current.event_id:
        return False, "IDEMPOTENT_REPLAY"
    if incoming.occurred_at > current.occurred_at:
        return True, "NEWER_EVENT_TIME"
    if incoming.occurred_at < current.occurred_at:
        return False, "OLDER_EVENT_TIME"
    if incoming.precedence > current.precedence:
        return True, "SAME_TIME_HIGHER_PRECEDENCE"
    if incoming.precedence < current.precedence:
        return False, "SAME_TIME_LOWER_PRECEDENCE"
    if incoming.event_id > current.event_id:
        return True, "SAME_TIME_EVENT_ID_TIE_BREAK"
    return False, "SAME_TIME_EVENT_ID_TIE_BREAK"


def client_event_payload(event: RobotEvent) -> dict[str, Any]:
    payload = dict(event.payload)
    payload.pop("_server_runtime", None)
    payload.pop("server_runtime", None)
    return {
        **event.model_dump(mode="json"),
        "payload": payload,
    }


def canonical_event_fingerprint(value: RobotEvent | dict[str, Any]) -> str:
    if isinstance(value, RobotEvent):
        payload = client_event_payload(value)
    else:
        payload = dict(value or {})
        raw_payload = dict(payload.get("payload") or {})
        raw_payload.pop("_server_runtime", None)
        raw_payload.pop("server_runtime", None)
        payload["payload"] = raw_payload
    # Retry clients occasionally omit occurred_at and let the API apply a new
    # default timestamp. Identity therefore protects operational content while
    # event ordering remains responsible for time semantics.
    payload.pop("occurred_at", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def payload_identity_evidence(
    event: RobotEvent,
    stored_payload: dict[str, Any],
) -> dict[str, Any]:
    incoming = canonical_event_fingerprint(event)
    if not stored_payload:
        return {
            "policy": "EVENT_ID_IMMUTABLE_PAYLOAD",
            "event_id": event.event_id,
            "incoming_fingerprint": incoming,
            "stored_fingerprint": None,
            "match": True,
            "verification": "LEGACY_EVENT_PAYLOAD_UNAVAILABLE",
        }
    stored = canonical_event_fingerprint(stored_payload)
    return {
        "policy": "EVENT_ID_IMMUTABLE_PAYLOAD",
        "event_id": event.event_id,
        "incoming_fingerprint": incoming,
        "stored_fingerprint": stored,
        "match": incoming == stored,
        "verification": "VERIFIED",
    }

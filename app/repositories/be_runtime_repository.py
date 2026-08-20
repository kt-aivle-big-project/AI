"""Read the unmodified Spring BE Redis runtime contract.

Spring writes JSON strings at:

* ``simulation:run:{runId}:robots`` (Redis Set of numeric robot IDs)
* ``simulation:run:{runId}:robot:{robotId}:state`` (RobotState JSON)

LARO may additionally write/read optional, non-conflicting planning metadata at
``...:robot:{robotId}:laro`` and ``...:meta``.  The existing Spring code never
needs to deserialize these extension documents.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.config import Settings, get_settings
from app.domain.be_runtime import (
    BeRuntimeEdge,
    BeRuntimeRobot,
    BeRuntimeRunMeta,
    BeRuntimeSnapshot,
)
from app.infrastructure.manager import get_infrastructure_manager


class BeRuntimeDataError(ValueError):
    """Raised when a Spring Redis document is present but malformed."""


class BeSpringRuntimeRepository:
    RUN_PREFIX = "simulation:run:"

    def __init__(
        self,
        settings: Settings | None = None,
        manager: Any | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.manager = manager or get_infrastructure_manager()
        self._client = client
        self.last_source = "none"

    @property
    def client(self):
        return self._client or self.manager.redis.client

    @classmethod
    def robot_ids_key(cls, run_id: int) -> str:
        return f"{cls.RUN_PREFIX}{run_id}:robots"

    @classmethod
    def robot_state_key(cls, run_id: int, robot_id: int) -> str:
        return f"{cls.RUN_PREFIX}{run_id}:robot:{robot_id}:state"

    @classmethod
    def robot_extension_key(cls, run_id: int, robot_id: int) -> str:
        return f"{cls.RUN_PREFIX}{run_id}:robot:{robot_id}:laro"

    @classmethod
    def meta_key(cls, run_id: int) -> str:
        return f"{cls.RUN_PREFIX}{run_id}:meta"

    @classmethod
    def edge_ids_key(cls, run_id: int) -> str:
        return f"{cls.RUN_PREFIX}{run_id}:edges"

    @classmethod
    def edge_state_key(cls, run_id: int, edge_id: int) -> str:
        return f"{cls.RUN_PREFIX}{run_id}:edge:{edge_id}:state"

    @staticmethod
    def _parse_json(raw: Any, *, key: str) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, dict):
            return dict(raw)
        try:
            value = json.loads(str(raw))
        except Exception as exc:
            raise BeRuntimeDataError(f"Invalid JSON at Redis key {key}: {exc}") from exc
        if not isinstance(value, dict):
            raise BeRuntimeDataError(f"Redis key {key} must contain a JSON object.")
        return value

    def _robot_ids(self, run_id: int) -> list[int]:
        values = self.client.smembers(self.robot_ids_key(run_id)) or set()
        result: list[int] = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError) as exc:
                raise BeRuntimeDataError(
                    f"Robot ID {value!r} in {self.robot_ids_key(run_id)} is not numeric."
                ) from exc
        return sorted(set(result))

    def load_run_meta(self, run_id: int) -> BeRuntimeRunMeta | None:
        key = self.meta_key(run_id)
        data = self._parse_json(self.client.get(key), key=key)
        if not data:
            return None
        data.setdefault("simulationRunId", run_id)
        data["compatibilityMode"] = False
        return BeRuntimeRunMeta.model_validate(data)

    def load_robot_runtime(self, run_id: int) -> list[BeRuntimeRobot]:
        result: list[BeRuntimeRobot] = []
        for robot_id in self._robot_ids(run_id):
            state_key = self.robot_state_key(run_id, robot_id)
            state = self._parse_json(self.client.get(state_key), key=state_key)
            if not state:
                # The ID set can briefly lead the value write.  Treat it as
                # unavailable instead of inventing a robot state.
                continue
            state.setdefault("robotId", robot_id)
            extension_key = self.robot_extension_key(run_id, robot_id)
            extension = self._parse_json(self.client.get(extension_key), key=extension_key)
            state.update(extension)
            state["compatibilityMode"] = not bool(extension)
            result.append(BeRuntimeRobot.model_validate(state))
        return sorted(result, key=lambda value: value.robot_id)

    def _edge_ids(self, run_id: int) -> list[int]:
        values = self.client.smembers(self.edge_ids_key(run_id)) or set()
        if values:
            return sorted({int(value) for value in values})
        prefix = f"{self.RUN_PREFIX}{run_id}:edge:"
        suffix = ":state"
        result: set[int] = set()
        for key in self.client.scan_iter(match=f"{prefix}*{suffix}"):
            text = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            if text.startswith(prefix) and text.endswith(suffix):
                raw = text[len(prefix) : -len(suffix)]
                try:
                    result.add(int(raw))
                except ValueError:
                    continue
        return sorted(result)

    def load_edge_runtime(self, run_id: int) -> list[BeRuntimeEdge]:
        result: list[BeRuntimeEdge] = []
        for edge_id in self._edge_ids(run_id):
            key = self.edge_state_key(run_id, edge_id)
            data = self._parse_json(self.client.get(key), key=key)
            if not data:
                continue
            data.setdefault("edgeId", edge_id)
            data.setdefault("status", self.settings.runtime_default_edge_status)
            result.append(BeRuntimeEdge.model_validate(data))
        return sorted(result, key=lambda value: value.edge_id)

    def blocked_edge_ids(self, run_id: int) -> list[int]:
        blocked = {"BLOCKED", "CLOSED", "MAINTENANCE"}
        return [
            edge.edge_id
            for edge in self.load_edge_runtime(run_id)
            if edge.status.strip().upper() in blocked
        ]

    def snapshot(self, run_id: int) -> BeRuntimeSnapshot:
        meta = self.load_run_meta(run_id)
        robots = self.load_robot_runtime(run_id)
        blocked = self.blocked_edge_ids(run_id)
        warnings: list[str] = []
        if not robots:
            mode = "NOT_INITIALIZED"
            warnings.append(
                "Spring Redis has no robot runtime for this simulation run."
            )
        elif meta is None or any(robot.compatibility_mode for robot in robots):
            mode = "COMPATIBILITY"
            warnings.append(
                "Exact simTime/currentStep/load fields are unavailable; only node-based reoptimization is safe."
            )
        else:
            mode = "FULL"
        if meta is None:
            meta = BeRuntimeRunMeta(
                simulationRunId=run_id,
                compatibilityMode=True,
            )
        self.last_source = "spring_redis" if robots else "none"
        return BeRuntimeSnapshot(
            simulationRunId=run_id,
            mode=mode,
            meta=meta,
            robots=robots,
            blockedEdgeIds=blocked,
            warnings=warnings,
        )

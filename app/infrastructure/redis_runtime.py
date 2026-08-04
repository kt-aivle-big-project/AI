"""Redis runtime adapter with warehouse- and simulation-scoped namespaces."""
from __future__ import annotations

import json
import time
from typing import Any

from app.core.config import Settings, get_settings
from app.domain.schemas import normalize_warehouse_id


class RedisInfrastructureError(RuntimeError):
    pass


class RedisRuntimeAdapter:
    """Store latest runtime state and streams without cross-warehouse collisions."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    def open(self) -> None:
        if self._client is not None:
            return
        try:
            from redis import Redis
        except Exception as exc:  # pragma: no cover
            raise RedisInfrastructureError("Live Redis requires the redis Python package.") from exc
        self._client = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self._client.ping()

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
        self._client = None

    @property
    def client(self):
        self.open()
        return self._client

    def _key(self, *parts: str) -> str:
        return ":".join([self.settings.redis_key_prefix, *parts])

    def _scope(self, warehouse_or_simulation: str, simulation_id: str | None = None) -> tuple[str, str]:
        """Accept the v13.20 two-key API and legacy one-key calls."""

        if simulation_id is None:
            return normalize_warehouse_id(self.settings.default_warehouse_id), str(warehouse_or_simulation)
        return normalize_warehouse_id(warehouse_or_simulation), str(simulation_id)

    def _scope_key(self, warehouse_id: str, simulation_id: str, *parts: str) -> str:
        return self._key("warehouse", warehouse_id, "sim", simulation_id, *parts)

    def ping(self) -> dict[str, Any]:
        started = time.perf_counter()
        ok = bool(self.client.ping())
        return {"ok": ok, "latency_ms": round((time.perf_counter() - started) * 1000, 3)}

    def robot_key(self, warehouse_id: str, simulation_id: str, robot_id: str) -> str:
        return self._scope_key(warehouse_id, simulation_id, "robot", robot_id, "state")

    def edge_key(self, warehouse_id: str, simulation_id: str, edge_id: str) -> str:
        return self._scope_key(warehouse_id, simulation_id, "edge", edge_id, "state")

    def station_key(self, warehouse_id: str, simulation_id: str, station_id: str) -> str:
        return self._scope_key(warehouse_id, simulation_id, "station", station_id, "state")

    def reservation_key(self, warehouse_id: str, simulation_id: str, reservation_id: str) -> str:
        return self._scope_key(warehouse_id, simulation_id, "reservation", reservation_id)

    def seed_from_documents(
        self,
        *,
        scenario: dict[str, Any],
        facility: dict[str, Any],
        replace: bool = True,
        warehouse_id: str | None = None,
    ) -> dict[str, int]:
        warehouse = normalize_warehouse_id(
            warehouse_id or scenario.get("warehouse_id") or self.settings.default_warehouse_id
        )
        simulation = str(scenario.get("simulation_id", self.settings.runtime_simulation_id))
        if replace:
            pattern = self._scope_key(warehouse, simulation, "*")
            keys = list(self.client.scan_iter(match=pattern))
            if keys:
                self.client.delete(*keys)
        pipe = self.client.pipeline(transaction=True)
        for robot in scenario.get("robots", []):
            mapping = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                for key, value in robot.items() if value is not None
            }
            mapping.update({
                "warehouse_id": warehouse,
                "simulation_id": simulation,
                "state_version": str(mapping.get("state_version", "1")),
                "sim_time_ms": str(mapping.get("sim_time_ms", "0")),
            })
            pipe.hset(self.robot_key(warehouse, simulation, str(robot["robot_id"])), mapping=mapping)
        for edge in scenario.get("edge_runtime", []):
            mapping = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                for key, value in edge.items() if value is not None
            }
            mapping.update({"warehouse_id": warehouse, "simulation_id": simulation})
            pipe.hset(self.edge_key(warehouse, simulation, str(edge["edge_id"])), mapping=mapping)
        for station in facility.get("outbound_stations", []):
            pipe.hset(
                self.station_key(warehouse, simulation, str(station["station_id"])),
                mapping={
                    "warehouse_id": warehouse,
                    "simulation_id": simulation,
                    "station_id": str(station["station_id"]),
                    "station_robot_id": str(station["station_robot_id"]),
                    "status": str(station.get("status", "available")),
                    "active_handling_unit_id": "",
                    "queue_depth": "0",
                    "available_at_ms": "0",
                    "state_version": "1",
                },
            )
        for reservation in scenario.get("edge_reservations", []):
            reservation_id = str(
                reservation.get("reservation_id")
                or f"SEED-{reservation.get('edge_id')}-{reservation.get('robot_id')}-{reservation.get('start_at_ms', 0)}"
            )
            mapping = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                for key, value in reservation.items() if value is not None
            }
            mapping.update({
                "warehouse_id": warehouse,
                "simulation_id": simulation,
                "reservation_id": reservation_id,
            })
            pipe.hset(self.reservation_key(warehouse, simulation, reservation_id), mapping=mapping)
        pipe.set(self._scope_key(warehouse, simulation, "runtime_version"), "1")
        pipe.execute()
        return {
            "warehouse_id": warehouse,
            "simulation_id": simulation,
            "robots": len(scenario.get("robots", [])),
            "edges": len(scenario.get("edge_runtime", [])),
            "stations": len(facility.get("outbound_stations", [])),
            "reservations": len(scenario.get("edge_reservations", [])),
        }

    @staticmethod
    def _decode_hash(value: dict[str, str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, raw in value.items():
            if raw in {"True", "False"}:
                result[key] = raw == "True"
                continue
            try:
                if raw.startswith(("{", "[")):
                    result[key] = json.loads(raw)
                    continue
            except Exception:
                pass
            try:
                result[key] = int(raw)
                continue
            except Exception:
                pass
            try:
                result[key] = float(raw)
                continue
            except Exception:
                result[key] = raw
        return result

    def all_robots(self, warehouse_or_simulation: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse, simulation = self._scope(warehouse_or_simulation, simulation_id)
        pattern = self._scope_key(warehouse, simulation, "robot", "*", "state")
        return [
            record
            for key in sorted(self.client.scan_iter(match=pattern))
            if (record := self._decode_hash(self.client.hgetall(key)))
        ]

    def list_simulation_ids(self, warehouse_id: str) -> list[str]:
        """List simulation namespaces that contain at least one robot state."""

        warehouse = normalize_warehouse_id(warehouse_id)
        pattern = self._scope_key(warehouse, "*", "robot", "*", "state")
        values: set[str] = set()
        prefix = self._key("warehouse", warehouse, "sim") + ":"
        for key in self.client.scan_iter(match=pattern):
            text = str(key)
            if not text.startswith(prefix):
                continue
            remainder = text[len(prefix) :]
            simulation = remainder.split(":", 1)[0]
            if simulation:
                values.add(simulation)
        return sorted(values)

    def edge_runtime(self, warehouse_or_simulation: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse, simulation = self._scope(warehouse_or_simulation, simulation_id)
        pattern = self._scope_key(warehouse, simulation, "edge", "*", "state")
        return [
            record
            for key in sorted(self.client.scan_iter(match=pattern))
            if (record := self._decode_hash(self.client.hgetall(key)))
        ]

    def station_runtime(self, warehouse_or_simulation: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse, simulation = self._scope(warehouse_or_simulation, simulation_id)
        pattern = self._scope_key(warehouse, simulation, "station", "*", "state")
        return [self._decode_hash(self.client.hgetall(key)) for key in sorted(self.client.scan_iter(match=pattern))]

    def get_robot(
        self,
        warehouse_or_simulation: str,
        simulation_or_robot: str,
        robot_id: str | None = None,
    ) -> dict[str, Any] | None:
        if robot_id is None:
            warehouse, simulation = self._scope(warehouse_or_simulation)
            robot = simulation_or_robot
        else:
            warehouse, simulation = self._scope(warehouse_or_simulation, simulation_or_robot)
            robot = robot_id
        value = self._decode_hash(self.client.hgetall(self.robot_key(warehouse, simulation, robot)))
        return value or None

    def existing_reservations(self, warehouse_or_simulation: str, simulation_id: str | None = None) -> list[dict[str, Any]]:
        warehouse, simulation = self._scope(warehouse_or_simulation, simulation_id)
        pattern = self._scope_key(warehouse, simulation, "reservation", "*")
        return [
            self._decode_hash(self.client.hgetall(key))
            for key in sorted(self.client.scan_iter(match=pattern))
            if self.client.exists(key)
        ]

    def reserve_edges(
        self,
        *,
        simulation_id: str,
        reservations: list[dict[str, Any]],
        warehouse_id: str | None = None,
    ) -> int:
        warehouse = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        pipe = self.client.pipeline(transaction=True)
        for reservation in reservations:
            reservation_id = str(reservation["reservation_id"])
            mapping = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                for key, value in reservation.items() if value is not None
            }
            mapping.update({"warehouse_id": warehouse, "simulation_id": simulation_id})
            pipe.hset(self.reservation_key(warehouse, simulation_id, reservation_id), mapping=mapping)
        pipe.incr(self._scope_key(warehouse, simulation_id, "runtime_version"))
        pipe.execute()
        return len(reservations)

    def update_robot_state(
        self,
        *,
        simulation_id: str,
        robot_id: str,
        state: dict[str, Any],
        sequence: int,
        warehouse_id: str | None = None,
    ) -> bool:
        warehouse = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        key = self.robot_key(warehouse, simulation_id, robot_id)
        current = self.client.hget(key, "sequence")
        if current is not None and int(current) >= sequence:
            return False
        mapping = {
            field: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            for field, value in state.items() if value is not None
        }
        mapping.update({
            "warehouse_id": warehouse,
            "simulation_id": simulation_id,
            "robot_id": robot_id,
            "sequence": str(sequence),
        })
        stream = self._scope_key(warehouse, simulation_id, "telemetry")
        version_key = self._scope_key(warehouse, simulation_id, "runtime_version")
        pipe = self.client.pipeline(transaction=True)
        pipe.hset(key, mapping=mapping)
        pipe.xadd(
            stream,
            {"robot_id": robot_id, "sequence": str(sequence), "payload": json.dumps(state, ensure_ascii=False)},
            maxlen=100_000,
            approximate=True,
        )
        pipe.incr(version_key)
        pipe.execute()
        return True

    def publish_command(
        self,
        *,
        simulation_id: str,
        command: dict[str, Any],
        warehouse_id: str | None = None,
    ) -> str:
        warehouse = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        stream = self._scope_key(warehouse, simulation_id, "commands")
        envelope = {**command, "warehouse_id": warehouse, "simulation_id": simulation_id}
        return str(
            self.client.xadd(
                stream,
                {"payload": json.dumps(envelope, ensure_ascii=False)},
                maxlen=100_000,
                approximate=True,
            )
        )

    def runtime_version(self, warehouse_or_simulation: str, simulation_id: str | None = None) -> str:
        warehouse, simulation = self._scope(warehouse_or_simulation, simulation_id)
        return str(self.client.get(self._scope_key(warehouse, simulation, "runtime_version")) or "0")

    def clone_simulation_runtime(
        self,
        *,
        warehouse_id: str,
        source_simulation_id: str,
        target_simulation_id: str,
        reset: bool = True,
        copy_robot_runtime: bool = True,
        copy_edge_runtime: bool = True,
        copy_station_runtime: bool = True,
        copy_reservations: bool = False,
    ) -> dict[str, Any]:
        """Clone one simulation-scoped runtime namespace for debug scenarios.

        Business data and the Neo4j route graph remain warehouse-scoped and are
        not duplicated.  Only fast-changing Redis state is copied.  The method
        is intentionally explicit and is exposed only through the guarded debug
        scenario API.
        """

        warehouse = normalize_warehouse_id(warehouse_id)
        source = str(source_simulation_id)
        target = str(target_simulation_id)
        source_version = self.runtime_version(warehouse, source)
        if source == target:
            return {
                "status": "NOOP",
                "warehouse_id": warehouse,
                "source_simulation_id": source,
                "target_simulation_id": target,
                "robots": len(self.all_robots(warehouse, source)),
                "edges": len(self.edge_runtime(warehouse, source)),
                "stations": len(self.station_runtime(warehouse, source)),
                "reservations": len(self.existing_reservations(warehouse, source)),
                "source_runtime_version": source_version,
                "target_runtime_version": source_version,
            }

        robots = self.all_robots(warehouse, source) if copy_robot_runtime else []
        edges = self.edge_runtime(warehouse, source) if copy_edge_runtime else []
        stations = self.station_runtime(warehouse, source) if copy_station_runtime else []
        reservations = (
            self.existing_reservations(warehouse, source)
            if copy_reservations
            else []
        )
        if copy_robot_runtime and not robots:
            raise RedisInfrastructureError(
                f"Source simulation {source} has no robot runtime for warehouse {warehouse}."
            )

        if reset:
            keys = list(
                self.client.scan_iter(match=self._scope_key(warehouse, target, "*"))
            )
            if keys:
                self.client.delete(*keys)

        pipe = self.client.pipeline(transaction=True)

        def mapping_for(value: dict[str, Any]) -> dict[str, str]:
            payload = {
                **value,
                "warehouse_id": warehouse,
                "simulation_id": target,
            }
            return {
                key: (
                    json.dumps(item, ensure_ascii=False)
                    if isinstance(item, (dict, list))
                    else str(item)
                )
                for key, item in payload.items()
                if item is not None
            }

        for robot in robots:
            pipe.hset(
                self.robot_key(warehouse, target, str(robot["robot_id"])),
                mapping=mapping_for(robot),
            )
        for edge in edges:
            pipe.hset(
                self.edge_key(warehouse, target, str(edge["edge_id"])),
                mapping=mapping_for(edge),
            )
        for station in stations:
            pipe.hset(
                self.station_key(warehouse, target, str(station["station_id"])),
                mapping=mapping_for(station),
            )
        for reservation in reservations:
            reservation_id = str(
                reservation.get("reservation_id")
                or f"CLONE-{reservation.get('edge_id')}-{reservation.get('robot_id')}"
            )
            pipe.hset(
                self.reservation_key(warehouse, target, reservation_id),
                mapping=mapping_for(
                    {**reservation, "reservation_id": reservation_id}
                ),
            )
        pipe.set(self._scope_key(warehouse, target, "runtime_version"), "1")
        pipe.execute()
        return {
            "status": "BOOTSTRAPPED",
            "warehouse_id": warehouse,
            "source_simulation_id": source,
            "target_simulation_id": target,
            "robots": len(robots),
            "edges": len(edges),
            "stations": len(stations),
            "reservations": len(reservations),
            "source_runtime_version": source_version,
            "target_runtime_version": self.runtime_version(warehouse, target),
        }

    def roundtrip(self, probe_id: str, warehouse_id: str | None = None) -> dict[str, Any]:
        warehouse = normalize_warehouse_id(warehouse_id or self.settings.default_warehouse_id)
        key = self._key("warehouse", warehouse, "roundtrip", probe_id)
        payload = {"warehouse_id": warehouse, "probe": probe_id, "component": "redis"}
        self.client.set(key, json.dumps(payload), ex=60)
        raw = self.client.get(key)
        self.client.delete(key)
        return json.loads(raw)

import json
from datetime import UTC, datetime
from typing import Any

from redis import Redis
from redis.exceptions import WatchError

from app.models import RobotEvent
from app.services.event_safety import (
    EventWatermark,
    StaleExecutionEventError,
    compare_event_order,
    event_watermark,
    ordering_evidence,
)
from app.services.inventory_transition import calculate_inventory_transition


class RedisRepository:
    """실시간 로봇 상태, 임시 폐쇄, 미래 경로와 활성 계획 버전을 담당합니다."""

    ACTIVATE_PLAN_LUA = """
    local current = redis.call('GET', KEYS[2])
    if ARGV[4] == '__NONE__' then
        if current then
            return redis.error_reply('STALE_PLAN_VERSION')
        end
    elseif current ~= ARGV[4] then
        return redis.error_reply('STALE_PLAN_VERSION')
    end
    redis.call('SET', KEYS[1], ARGV[1])
    redis.call('SET', KEYS[2], ARGV[2])
    redis.call('XADD', KEYS[3], '*',
        'event_type', 'PLAN_ACTIVATED',
        'plan_version', ARGV[2],
        'occurred_at', ARGV[3])
    return ARGV[2]
    """

    ROLLBACK_PLAN_LUA = """
    local current = redis.call('GET', KEYS[2])
    if current ~= ARGV[1] then
        return 0
    end
    redis.call('DEL', KEYS[1])
    if ARGV[2] == '__NONE__' then
        redis.call('DEL', KEYS[2])
    else
        redis.call('SET', KEYS[2], ARGV[2])
    end
    redis.call('XADD', KEYS[3], '*',
        'event_type', 'PLAN_ACTIVATION_ROLLED_BACK',
        'plan_version', ARGV[1],
        'occurred_at', ARGV[3])
    return 1
    """

    RELEASE_LOCK_LUA = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, redis_url: str):
        if not redis_url:
            raise RuntimeError("REDIS_URL이 설정되지 않았습니다.")
        self.client = Redis.from_url(redis_url, decode_responses=True)
        self._activate_script = self.client.register_script(self.ACTIVATE_PLAN_LUA)
        self._rollback_script = self.client.register_script(self.ROLLBACK_PLAN_LUA)
        self._release_lock_script = self.client.register_script(
            self.RELEASE_LOCK_LUA
        )

    @staticmethod
    def _prefix(warehouse_id: int) -> str:
        return f"wh:{warehouse_id}"

    @staticmethod
    def _event_ordering_required(event: RobotEvent) -> bool:
        # Simulation replay events build a complete virtual timeline and must not
        # become the external-event watermark. API events are bound to a
        # server-owned runtime context before reaching this repository.
        return isinstance(event.payload.get("_server_runtime"), dict)

    def _event_watermark_key(self, event: RobotEvent) -> str:
        if event.execution_context == "SIMULATION":
            return f"sim:{event.simulation_id}:event_watermark:{event.robot_id}"
        return f"{self._prefix(event.warehouse_id)}:event_watermark:{event.robot_id}"

    def get_event_watermark(self, event: RobotEvent) -> dict[str, Any] | None:
        if not self._event_ordering_required(event):
            return None
        value = self.client.hgetall(self._event_watermark_key(event))
        return dict(value) if value else None

    def validate_event_order(self, event: RobotEvent) -> dict[str, Any]:
        if not self._event_ordering_required(event):
            return ordering_evidence(event, None, decision="ORDERING_NOT_REQUIRED")
        current = EventWatermark.from_mapping(self.get_event_watermark(event))
        accepted, reason = compare_event_order(event, current)
        evidence = ordering_evidence(event, current, decision=reason)
        if not accepted and reason != "IDEMPOTENT_REPLAY":
            raise StaleExecutionEventError(evidence)
        return evidence

    def restore_simulation_snapshot(
        self,
        simulation_id: str,
        snapshot: dict[str, Any],
        *,
        event: RobotEvent | None = None,
        previous_watermark: dict[str, Any] | None = None,
        reason: str = "EVENT_CHECKPOINT_FAILED",
    ) -> dict[str, Any]:
        keys = self._simulation_keys(simulation_id)
        pipeline = self.client.pipeline(transaction=True)
        pipeline.set(
            keys["inventory"],
            json.dumps(snapshot.get("inventory") or [], ensure_ascii=False, default=str),
        )
        pipeline.set(
            keys["robots"],
            json.dumps(snapshot.get("robots") or [], ensure_ascii=False, default=str),
        )
        pipeline.set(
            keys["works"],
            json.dumps(snapshot.get("works") or [], ensure_ascii=False, default=str),
        )
        active_plan = snapshot.get("active_plan")
        if active_plan:
            pipeline.set(
                keys["plan"],
                json.dumps(active_plan, ensure_ascii=False, default=str),
            )
        else:
            pipeline.delete(keys["plan"])
        if event is not None and self._event_ordering_required(event):
            watermark_key = self._event_watermark_key(event)
            if previous_watermark:
                pipeline.hset(watermark_key, mapping=previous_watermark)
            else:
                pipeline.delete(watermark_key)
        pipeline.xadd(
            keys["events"],
            {
                "event_type": "SIMULATION_EVENT_STATE_ROLLED_BACK",
                "simulation_id": simulation_id,
                "trigger_event_id": event.event_id if event else "",
                "reason": reason,
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        results = pipeline.execute()
        restored = self.simulation_snapshot(simulation_id)
        return {
            "restored": True,
            "simulation_id": simulation_id,
            "active_plan_version": restored.get("active_plan_version"),
            "checkpoint": str(results[-1]),
            "reason": reason,
        }

    def healthcheck(self) -> dict[str, Any]:
        return {"ok": bool(self.client.ping())}

    def live_snapshot(self, warehouse_id: int) -> dict[str, Any]:
        prefix = self._prefix(warehouse_id)
        robot_ids = sorted(self.client.smembers(f"{prefix}:robots"))
        executing_ids = sorted(self.client.smembers(f"{prefix}:tasks:executing"))
        planned_ids = sorted(self.client.smembers(f"{prefix}:tasks:planned"))
        active_version = self.client.get(f"{prefix}:active_plan_version")

        pipeline = self.client.pipeline(transaction=False)
        for robot_id in robot_ids:
            pipeline.hgetall(f"{prefix}:robot:{robot_id}")
        for task_id in executing_ids + planned_ids:
            pipeline.hgetall(f"{prefix}:task:{task_id}")
        rows = pipeline.execute()

        robot_count = len(robot_ids)
        robots = [dict(row) for row in rows[:robot_count]]
        tasks = [dict(row) for row in rows[robot_count:]]
        active_plan_raw = (
            self.client.get(f"{prefix}:plan:{active_version}")
            if active_version
            else None
        )
        closures = []
        for closure_id, payload in self.client.hgetall(
            f"{prefix}:temporary_closures"
        ).items():
            value = json.loads(payload)
            value["closure_id"] = closure_id
            closures.append(value)

        return {
            "robots": robots,
            "tasks": tasks,
            "executing_task_ids": executing_ids,
            "planned_task_ids": planned_ids,
            "active_plan_version": active_version,
            "active_plan": json.loads(active_plan_raw) if active_plan_raw else None,
            "temporary_closures": closures,
            "inventory_reservations": self.list_inventory_reservations(
                warehouse_id,
                scope="ACTIVE_PLAN",
                statuses={"RESERVED"},
            ),
        }

    def acquire_inventory_lock(
        self,
        warehouse_id: int,
        item_id: str,
        token: str,
        *,
        ttl_seconds: int = 15,
    ) -> bool:
        return bool(
            self.client.set(
                f"{self._prefix(warehouse_id)}:inventory:lock:{item_id}",
                token,
                nx=True,
                ex=max(1, int(ttl_seconds)),
            )
        )

    def release_inventory_lock(
        self, warehouse_id: int, item_id: str, token: str
    ) -> bool:
        result = self._release_lock_script(
            keys=[f"{self._prefix(warehouse_id)}:inventory:lock:{item_id}"],
            args=[token],
        )
        return bool(int(result))

    def list_inventory_reservations(
        self,
        warehouse_id: int,
        *,
        scope: str | None = None,
        statuses: set[str] | None = None,
        simulation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        key = f"{self._prefix(warehouse_id)}:inventory:reservations"
        rows: list[dict[str, Any]] = []
        for payload in self.client.hvals(key):
            row = json.loads(payload)
            if scope and str(row.get("scope")) != scope:
                continue
            if statuses and str(row.get("status")) not in statuses:
                continue
            if simulation_id is not None and row.get("simulation_id") != simulation_id:
                continue
            rows.append(row)
        return sorted(rows, key=lambda row: str(row.get("reservation_id")))

    def save_inventory_reservations(
        self, warehouse_id: int, reservations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not reservations:
            return []
        prefix = self._prefix(warehouse_id)
        reservation_key = f"{prefix}:inventory:reservations"
        idempotency_key = f"{prefix}:inventory:reservation_idempotency"
        stored: list[dict[str, Any]] = []
        for row in reservations:
            existing_id = self.client.hget(
                idempotency_key, str(row["idempotency_key"])
            )
            if existing_id:
                payload = self.client.hget(reservation_key, existing_id)
                if payload:
                    stored.append(json.loads(payload))
                    continue
            pipeline = self.client.pipeline(transaction=True)
            pipeline.hset(
                reservation_key,
                str(row["reservation_id"]),
                json.dumps(row, ensure_ascii=False, default=str),
            )
            pipeline.hset(
                idempotency_key,
                str(row["idempotency_key"]),
                str(row["reservation_id"]),
            )
            pipeline.xadd(
                f"{prefix}:events",
                {
                    "event_type": "INVENTORY_RESERVED",
                    "reservation_id": str(row["reservation_id"]),
                    "plan_version": str(row["plan_version"]),
                    "item_id": str(row["item_id"]),
                    "quantity_boxes": str(row["quantity_boxes"]),
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
            pipeline.execute()
            stored.append(dict(row))
        return stored

    def update_inventory_reservations(
        self,
        warehouse_id: int,
        *,
        plan_version: str | None = None,
        work_id: str | None = None,
        from_statuses: set[str] | None = None,
        status: str,
    ) -> list[dict[str, Any]]:
        key = f"{self._prefix(warehouse_id)}:inventory:reservations"
        updated: list[dict[str, Any]] = []
        for reservation_id, payload in self.client.hgetall(key).items():
            row = json.loads(payload)
            if plan_version and row.get("plan_version") != plan_version:
                continue
            if work_id and row.get("work_id") != work_id:
                continue
            if from_statuses and row.get("status") not in from_statuses:
                continue
            row["status"] = status
            row["updated_at"] = datetime.now(UTC).isoformat()
            self.client.hset(
                key,
                reservation_id,
                json.dumps(row, ensure_ascii=False, default=str),
            )
            updated.append(row)
        return updated

    def consume_inventory_reservations(
        self,
        warehouse_id: int,
        *,
        work_id: str,
        inventory_deltas: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Consume only the lot quantities proven by a committed REAL event.

        A work may be fulfilled from several lots and may emit more than one
        completion event.  Keeping the unconsumed remainder RESERVED prevents
        the first event from releasing the rest of the stock to another plan.
        """
        requested_by_lot: dict[str, int] = {}
        for delta in inventory_deltas:
            quantity_delta = int(delta.get("quantity_delta") or 0)
            if quantity_delta >= 0:
                continue
            lot_id = str(delta.get("warehouse_item_id") or "")
            if lot_id:
                requested_by_lot[lot_id] = (
                    requested_by_lot.get(lot_id, 0) + abs(quantity_delta)
                )
        if not requested_by_lot:
            return []

        key = f"{self._prefix(warehouse_id)}:inventory:reservations"
        remaining = dict(requested_by_lot)
        staged: list[tuple[Any, dict[str, Any]]] = []
        now = datetime.now(UTC).isoformat()
        for reservation_id, payload in self.client.hgetall(key).items():
            row = json.loads(payload)
            if (
                str(row.get("work_id")) != str(work_id)
                or row.get("scope") != "ACTIVE_PLAN"
                or row.get("status") != "RESERVED"
            ):
                continue
            consumed_now = 0
            new_allocations: list[dict[str, Any]] = []
            for allocation in row.get("lot_allocations", []):
                allocation = dict(allocation)
                lot_id = str(allocation.get("warehouse_item_id") or "")
                available = int(allocation.get("quantity_boxes") or 0)
                consumed = min(available, remaining.get(lot_id, 0))
                remaining[lot_id] = max(0, remaining.get(lot_id, 0) - consumed)
                consumed_now += consumed
                allocation["quantity_boxes"] = available - consumed
                if allocation["quantity_boxes"] > 0:
                    new_allocations.append(allocation)
            if consumed_now <= 0:
                continue
            row["lot_allocations"] = new_allocations
            row["consumed_quantity_boxes"] = int(
                row.get("consumed_quantity_boxes") or 0
            ) + consumed_now
            row["remaining_quantity_boxes"] = sum(
                int(value.get("quantity_boxes") or 0) for value in new_allocations
            )
            if row["remaining_quantity_boxes"] == 0:
                row["status"] = "CONSUMED"
            row["updated_at"] = now
            staged.append((reservation_id, row))

        unmatched = {lot_id: value for lot_id, value in remaining.items() if value > 0}
        if unmatched:
            raise RuntimeError(
                "Committed inventory deltas exceed ACTIVE_PLAN reservations: "
                f"{unmatched}"
            )
        pipeline = self.client.pipeline(transaction=True)
        for reservation_id, row in staged:
            pipeline.hset(
                key,
                reservation_id,
                json.dumps(row, ensure_ascii=False, default=str),
            )
        pipeline.execute()
        return [row for _, row in staged]

    def atomic_activate_plan(
        self,
        warehouse_id: int,
        plan_version: str,
        plan: dict[str, Any],
        expected_active_version: str | None = None,
    ) -> str:
        prefix = self._prefix(warehouse_id)
        result = self._activate_script(
            keys=[
                f"{prefix}:plan:{plan_version}",
                f"{prefix}:active_plan_version",
                f"{prefix}:events",
            ],
            args=[
                json.dumps(plan, ensure_ascii=False, default=str),
                plan_version,
                datetime.now(UTC).isoformat(),
                expected_active_version or "__NONE__",
            ],
        )
        return str(result)

    def rollback_plan_activation(
        self,
        warehouse_id: int,
        failed_plan_version: str,
        previous_active_version: str | None,
    ) -> bool:
        prefix = self._prefix(warehouse_id)
        result = self._rollback_script(
            keys=[
                f"{prefix}:plan:{failed_plan_version}",
                f"{prefix}:active_plan_version",
                f"{prefix}:events",
            ],
            args=[
                failed_plan_version,
                previous_active_version or "__NONE__",
                datetime.now(UTC).isoformat(),
            ],
        )
        return bool(int(result))

    def update_from_event(self, event: RobotEvent) -> dict[str, Any]:
        if event.execution_context != "REAL":
            raise RuntimeError("SIMULATION 이벤트는 실제 Redis robot state를 수정할 수 없습니다.")
        prefix = self._prefix(event.warehouse_id)
        ordering_required = self._event_ordering_required(event)
        watermark_key = self._event_watermark_key(event)

        for _ in range(5):
            with self.client.pipeline() as pipeline:
                try:
                    current = None
                    if ordering_required:
                        pipeline.watch(watermark_key)
                        current = EventWatermark.from_mapping(
                            pipeline.hgetall(watermark_key)
                        )
                        accepted, reason = compare_event_order(event, current)
                        evidence = ordering_evidence(event, current, decision=reason)
                        if not accepted:
                            pipeline.unwatch()
                            if reason == "IDEMPOTENT_REPLAY":
                                return {
                                    "accepted": False,
                                    "duplicate": True,
                                    "event_ordering": evidence,
                                }
                            raise StaleExecutionEventError(evidence)
                    else:
                        evidence = ordering_evidence(
                            event, None, decision="ORDERING_NOT_REQUIRED"
                        )

                    robot_key = f"{prefix}:robot:{event.robot_id}"
                    task_key = f"{prefix}:task:{event.task_id}" if event.task_id else None
                    mapping = {
                        "robot_id": event.robot_id,
                        "last_event": event.event_type,
                        "updated_at": event.occurred_at.isoformat(),
                    }
                    if event.node_id is not None:
                        mapping["node_id"] = str(event.node_id)
                    if event.battery is not None:
                        mapping["battery"] = str(event.battery)
                    if event.event_type == "TASK_STARTED":
                        mapping["status"] = "EXECUTING"
                    elif event.event_type in {"TASK_COMPLETED", "TASK_FAILED"}:
                        mapping["status"] = "IDLE"
                    elif event.event_type == "ROBOT_FAILED":
                        mapping["status"] = "FAILED"
                    elif event.event_type == "ROBOT_DELAYED":
                        mapping["status"] = "DELAYED"
                    elif event.event_type == "LOW_BATTERY":
                        mapping.setdefault("status", "IDLE")

                    pipeline.multi()
                    if event.event_type != "INBOUND_AVAILABLE":
                        pipeline.sadd(f"{prefix}:robots", event.robot_id)
                        pipeline.hset(robot_key, mapping=mapping)
                        if task_key:
                            pipeline.hset(
                                task_key,
                                mapping={
                                    "task_id": event.task_id or "",
                                    "work_id": event.work_id or "",
                                    "robot_id": event.robot_id,
                                    "status": event.event_type,
                                    "updated_at": event.occurred_at.isoformat(),
                                },
                            )
                            if event.event_type == "TASK_STARTED":
                                pipeline.srem(f"{prefix}:tasks:planned", event.task_id)
                                pipeline.sadd(f"{prefix}:tasks:executing", event.task_id)
                            elif event.event_type in {"TASK_COMPLETED", "TASK_FAILED"}:
                                pipeline.srem(f"{prefix}:tasks:planned", event.task_id)
                                pipeline.srem(f"{prefix}:tasks:executing", event.task_id)
                    if ordering_required:
                        pipeline.hset(
                            watermark_key, mapping=event_watermark(event).as_mapping()
                        )
                    pipeline.xadd(
                        f"{prefix}:events",
                        {
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "payload": event.model_dump_json(),
                        },
                    )
                    pipeline.execute()
                    return {
                        "accepted": True,
                        "duplicate": False,
                        "event_ordering": evidence,
                    }
                except WatchError:
                    continue
        raise RuntimeError(f"execution event 갱신 충돌: {event.event_id}")

    def emit_replan_required(self, event: RobotEvent) -> str:
        if event.execution_context != "REAL":
            raise RuntimeError("SIMULATION 이벤트는 실제 Redis replan stream을 수정할 수 없습니다.")
        prefix = self._prefix(event.warehouse_id)
        return str(
            self.client.xadd(
                f"{prefix}:events",
                {
                    "event_type": "REPLAN_REQUIRED",
                    "trigger_event_id": event.event_id,
                    "robot_id": event.robot_id,
                    "task_id": event.task_id or "",
                    "work_id": event.work_id or "",
                    "reason": event.event_type,
                    "occurred_at": event.occurred_at.isoformat(),
                },
            )
        )

    @staticmethod
    def _simulation_keys(simulation_id: str) -> dict[str, str]:
        prefix = f"sim:{simulation_id}"
        return {
            "inventory": f"{prefix}:inventory",
            "robots": f"{prefix}:robots",
            "works": f"{prefix}:works",
            "plan": f"{prefix}:active_plan",
            "events": f"{prefix}:events",
        }

    @staticmethod
    def _merged_simulation_robots(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        live_by_id = {
            str(row.get("robot_id")): row
            for row in snapshot.get("redis", {}).get("robots", [])
        }
        robots: list[dict[str, Any]] = []
        for raw in snapshot.get("sql", {}).get("robots", []):
            robot = dict(raw)
            live = live_by_id.get(str(robot.get("robot_id")), {})
            if live.get("node_id") not in (None, ""):
                robot["node_id"] = int(live["node_id"])
            if live.get("battery") not in (None, ""):
                robot["battery"] = float(live["battery"])
            if live.get("status"):
                robot["status"] = live["status"]
            if live.get("last_event"):
                robot["last_event"] = live["last_event"]
            robots.append(robot)
        return robots

    def initialize_simulation_session(
        self,
        simulation_id: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        keys = self._simulation_keys(simulation_id)
        inventory = [
            dict(row) for row in snapshot.get("sql", {}).get("inventory", [])
        ]
        robots = self._merged_simulation_robots(snapshot)
        works = [dict(row) for row in snapshot.get("sql", {}).get("works", [])]
        warehouse_id = snapshot.get("warehouse_id")
        index_key = (
            f"{self._prefix(int(warehouse_id))}:simulations"
            if warehouse_id is not None
            else None
        )

        for _ in range(3):
            with self.client.pipeline() as pipeline:
                try:
                    pipeline.watch(keys["inventory"], keys["robots"], keys["works"])
                    if pipeline.exists(keys["inventory"]):
                        pipeline.unwatch()
                        if index_key:
                            self.client.sadd(index_key, simulation_id)
                        return self.simulation_snapshot(simulation_id)
                    pipeline.multi()
                    pipeline.set(
                        keys["inventory"],
                        json.dumps(inventory, ensure_ascii=False, default=str),
                    )
                    pipeline.set(
                        keys["robots"],
                        json.dumps(robots, ensure_ascii=False, default=str),
                    )
                    pipeline.set(
                        keys["works"],
                        json.dumps(works, ensure_ascii=False, default=str),
                    )
                    pipeline.xadd(
                        keys["events"],
                        {
                            "event_type": "SIMULATION_INITIALIZED",
                            "simulation_id": simulation_id,
                            "occurred_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    if index_key:
                        pipeline.sadd(index_key, simulation_id)
                    pipeline.execute()
                    return self.simulation_snapshot(simulation_id)
                except WatchError:
                    continue
        raise RuntimeError(f"simulation session 초기화 충돌: {simulation_id}")

    def save_simulation_plan(
        self,
        simulation_id: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the verified candidate plan inside the simulation session."""

        keys = self._simulation_keys(simulation_id)
        if not self.client.exists(keys["inventory"]):
            raise RuntimeError(f"simulation session을 찾을 수 없습니다: {simulation_id}")
        payload = dict(plan)
        payload["simulation_id"] = simulation_id
        payload["base_plan_is_simulated"] = True
        payload["candidate_plan"] = True
        payload["execution_mode"] = "SIMULATE_ONLY"
        payload["stored_at"] = datetime.now(UTC).isoformat()
        pipeline = self.client.pipeline(transaction=True)
        pipeline.set(
            keys["plan"],
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        pipeline.xadd(
            keys["events"],
            {
                "event_type": "SIMULATION_PLAN_STORED",
                "simulation_id": simulation_id,
                "plan_version": str(payload.get("plan_version") or ""),
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        results = pipeline.execute()
        return {
            "saved": bool(results[0]),
            "simulation_id": simulation_id,
            "plan_version": payload.get("plan_version"),
            "checkpoint": str(results[-1]),
        }

    def simulation_snapshot(self, simulation_id: str) -> dict[str, Any]:
        keys = self._simulation_keys(simulation_id)
        values = self.client.mget(
            keys["inventory"],
            keys["robots"],
            keys["works"],
            keys["plan"],
        )
        if any(value is None for value in values[:3]):
            raise RuntimeError(f"simulation session을 찾을 수 없습니다: {simulation_id}")
        latest = self.client.xrevrange(keys["events"], count=1)
        active_plan = json.loads(values[3]) if values[3] else None
        return {
            "simulation_id": simulation_id,
            "inventory": json.loads(values[0]),
            "robots": json.loads(values[1]),
            "works": json.loads(values[2]),
            "active_plan_version": (
                active_plan.get("plan_version") if active_plan else None
            ),
            "active_plan": active_plan,
            "reference_time": (
                active_plan.get("reference_time") if active_plan else None
            ),
            "checkpoint": str(latest[0][0]) if latest else "0-0",
        }

    def update_simulation_from_event(self, event: RobotEvent) -> dict[str, Any]:
        if event.execution_context != "SIMULATION" or not event.simulation_id:
            raise RuntimeError("가상 상태 갱신에는 simulation_id가 있는 SIMULATION 이벤트가 필요합니다.")
        keys = self._simulation_keys(event.simulation_id)
        ordering_required = self._event_ordering_required(event)
        watermark_key = self._event_watermark_key(event)

        for _ in range(5):
            with self.client.pipeline() as pipeline:
                try:
                    watch_keys = [keys["inventory"], keys["robots"], keys["works"]]
                    if ordering_required:
                        watch_keys.append(watermark_key)
                    pipeline.watch(*watch_keys)
                    current = None
                    if ordering_required:
                        current = EventWatermark.from_mapping(
                            pipeline.hgetall(watermark_key)
                        )
                        accepted, reason = compare_event_order(event, current)
                        evidence = ordering_evidence(event, current, decision=reason)
                        if not accepted:
                            pipeline.unwatch()
                            if reason == "IDEMPOTENT_REPLAY":
                                snapshot = self.simulation_snapshot(event.simulation_id)
                                snapshot["event_ordering"] = evidence
                                snapshot["event_duplicate"] = True
                                return snapshot
                            raise StaleExecutionEventError(evidence)
                    else:
                        evidence = ordering_evidence(
                            event, None, decision="ORDERING_NOT_REQUIRED"
                        )

                    inventory_raw = pipeline.get(keys["inventory"])
                    robots_raw = pipeline.get(keys["robots"])
                    works_raw = pipeline.get(keys["works"])
                    if inventory_raw is None or robots_raw is None or works_raw is None:
                        raise RuntimeError(
                            f"simulation session을 찾을 수 없습니다: {event.simulation_id}"
                        )

                    inventory = json.loads(inventory_raw)
                    robots = json.loads(robots_raw)
                    works = json.loads(works_raw)

                    robots_by_id = {
                        str(row.get("robot_id")): dict(row) for row in robots
                    }
                    if event.event_type != "INBOUND_AVAILABLE":
                        robot = robots_by_id.setdefault(
                            event.robot_id, {"robot_id": event.robot_id}
                        )
                        if event.node_id is not None:
                            robot["node_id"] = event.node_id
                        if event.battery is not None:
                            robot["battery"] = event.battery
                        robot["last_event"] = event.event_type
                        robot["updated_at"] = event.occurred_at.isoformat()
                        if event.event_type == "TASK_STARTED":
                            robot["status"] = "EXECUTING"
                        elif event.event_type in {"TASK_COMPLETED", "TASK_FAILED"}:
                            robot["status"] = "IDLE"
                        elif event.event_type == "ROBOT_FAILED":
                            robot["status"] = "FAILED"
                        elif event.event_type == "ROBOT_DELAYED":
                            robot["status"] = "DELAYED"

                    virtual_work_id = event.work_id or event.task_id
                    works_by_id = {
                        str(row.get("work_id") or row.get("task_id")): dict(row)
                        for row in works
                    }
                    if virtual_work_id:
                        work = works_by_id.setdefault(
                            str(virtual_work_id),
                            {"work_id": event.work_id, "task_id": event.task_id},
                        )
                        if event.event_type != "INBOUND_AVAILABLE":
                            work["assigned_robot_id"] = event.robot_id
                        work["updated_at"] = event.occurred_at.isoformat()
                        if event.event_type == "TASK_STARTED":
                            work["status"] = "EXECUTING"
                        elif event.event_type == "TASK_COMPLETED":
                            work["status"] = "COMPLETED"
                        elif event.event_type == "TASK_FAILED":
                            work["status"] = "FAILED"
                        elif event.event_type == "INBOUND_AVAILABLE":
                            work["status"] = "AVAILABLE"

                    if event.event_type == "INBOUND_AVAILABLE":
                        item_id = str(event.payload.get("item_id") or "").strip()
                        quantity_boxes = int(event.payload.get("quantity_boxes") or 0)
                        if not item_id or quantity_boxes <= 0:
                            raise ValueError(
                                "INBOUND_AVAILABLE에는 item_id와 양의 quantity_boxes가 필요합니다."
                            )
                        warehouse_item_id = str(
                            event.payload.get("warehouse_item_id")
                            or f"SIM:{event.simulation_id}:{event.task_id or event.event_id}"
                        )
                        inventory_by_id = {
                            str(row["warehouse_item_id"]): dict(row) for row in inventory
                        }
                        row = inventory_by_id.setdefault(
                            warehouse_item_id,
                            {
                                "warehouse_item_id": warehouse_item_id,
                                "warehouse_id": event.warehouse_id,
                                "item_id": item_id,
                                "lot_id": event.payload.get("lot_id"),
                                "node_id": event.node_id,
                                "quantity": 0,
                                "reserved_quantity": 0,
                                "status": "AVAILABLE",
                                "source_type": event.payload.get("source_type"),
                                "inbound_source_id": event.payload.get("inbound_id"),
                            },
                        )
                        row["quantity"] = int(row.get("quantity") or 0) + quantity_boxes
                        row["available_quantity"] = row["quantity"] - int(
                            row.get("reserved_quantity") or 0
                        )
                        row["available_at"] = event.occurred_at.isoformat()
                        row["status"] = "AVAILABLE"
                        inventory = list(inventory_by_id.values())
                    elif event.event_type == "TASK_COMPLETED" and event.inventory_deltas:
                        inventory_by_id = {
                            str(row["warehouse_item_id"]): dict(row) for row in inventory
                        }
                        next_quantities = calculate_inventory_transition(
                            {
                                warehouse_item_id: int(row.get("quantity") or 0)
                                for warehouse_item_id, row in inventory_by_id.items()
                            },
                            event.inventory_deltas,
                        )
                        for warehouse_item_id, quantity in next_quantities.items():
                            row = inventory_by_id[warehouse_item_id]
                            row["quantity"] = quantity
                            reserved = int(row.get("reserved_quantity") or 0)
                            row["available_quantity"] = quantity - reserved
                        inventory = list(inventory_by_id.values())

                    robots = list(robots_by_id.values())
                    works = list(works_by_id.values())
                    pipeline.multi()
                    pipeline.set(
                        keys["inventory"],
                        json.dumps(inventory, ensure_ascii=False, default=str),
                    )
                    pipeline.set(
                        keys["robots"],
                        json.dumps(robots, ensure_ascii=False, default=str),
                    )
                    pipeline.set(
                        keys["works"],
                        json.dumps(works, ensure_ascii=False, default=str),
                    )
                    if ordering_required:
                        pipeline.hset(
                            watermark_key, mapping=event_watermark(event).as_mapping()
                        )
                    pipeline.xadd(
                        keys["events"],
                        {
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "payload": event.model_dump_json(),
                        },
                    )
                    pipeline.execute()
                    snapshot = self.simulation_snapshot(event.simulation_id)
                    snapshot["event_ordering"] = evidence
                    snapshot["event_duplicate"] = False
                    return snapshot
                except WatchError:
                    continue
        raise RuntimeError(f"simulation event 갱신 충돌: {event.simulation_id}")

    def remove_simulation_state(
        self,
        warehouse_id: int,
        simulation_id: str,
    ) -> dict[str, Any]:
        keys = self._simulation_keys(simulation_id)
        ordered_keys = [
            keys["inventory"],
            keys["robots"],
            keys["works"],
            keys["plan"],
            keys["events"],
        ]
        pipeline = self.client.pipeline(transaction=True)
        pipeline.delete(*ordered_keys)
        pipeline.srem(
            f"{self._prefix(warehouse_id)}:simulations",
            simulation_id,
        )
        deleted_count, _ = pipeline.execute()
        return {
            "affected_redis_keys": ordered_keys,
            "deleted_redis_key_count": int(deleted_count),
        }

    def simulation_state_exists(self, simulation_id: str) -> bool:
        keys = self._simulation_keys(simulation_id)
        return bool(self.client.exists(*keys.values()))

    def simulation_state_summary(self, simulation_id: str) -> dict[str, Any]:
        keys = self._simulation_keys(simulation_id)
        values = self.client.mget(
            keys["inventory"],
            keys["robots"],
            keys["works"],
            keys["plan"],
        )
        latest = self.client.xrevrange(keys["events"], count=1)
        active_plan = json.loads(values[3]) if values[3] else None
        return {
            "inventory_record_count": len(json.loads(values[0])) if values[0] else 0,
            "robot_count": len(json.loads(values[1])) if values[1] else 0,
            "work_count": len(json.loads(values[2])) if values[2] else 0,
            "active_plan_version": (
                active_plan.get("plan_version") if active_plan else None
            ),
            "plan_loaded": bool(active_plan),
            "event_count": int(self.client.xlen(keys["events"])),
            "checkpoint": str(latest[0][0]) if latest else None,
            "redis_state_exists": any(value is not None for value in values)
            or bool(latest),
        }

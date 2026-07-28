from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from app.models import SimulationResetRequest, SimulationResult
from app.repositories.postgres import PostgresRepository
from app.services.simulation_reset import SimulationResetService
from app.services.simulation_session import replay_simulation_session


def virtual_state(simulation_id: str) -> dict:
    return {
        "simulation_id": simulation_id,
        "inventory": [
            {
                "warehouse_item_id": "I-1",
                "quantity": 10,
                "reserved_quantity": 0,
                "available_quantity": 10,
            }
        ],
        "robots": [{"robot_id": "R-1", "node_id": 1, "status": "IDLE"}],
        "works": [{"work_id": "W-1", "status": "NEW"}],
        "checkpoint": "1-0",
    }


class ResetFakeRedis:
    def __init__(self) -> None:
        self.simulations: dict[str, dict] = {}
        self.indexes: dict[int, set[str]] = {}
        self.fail_ids: set[str] = set()
        self.removed_keys: list[str] = []
        self.real_state = {
            "robots": {"R-REAL": {"node_id": 99}},
            "executing": {"REAL-TASK"},
            "planned": {"REAL-PLANNED"},
            "active_plan_version": "REAL-PLAN",
            "plans": {"REAL-PLAN": {"safe": True}},
        }

    def add(self, simulation_id: str, warehouse_id: int) -> None:
        self.simulations[simulation_id] = virtual_state(simulation_id)
        self.indexes.setdefault(warehouse_id, set()).add(simulation_id)

    def initialize_simulation_session(self, simulation_id: str, snapshot: dict) -> dict:
        warehouse_id = int(snapshot["warehouse_id"])
        if simulation_id not in self.simulations:
            self.simulations[simulation_id] = {
                "simulation_id": simulation_id,
                "inventory": deepcopy(snapshot["sql"]["inventory"]),
                "robots": deepcopy(snapshot["sql"]["robots"]),
                "works": deepcopy(snapshot["sql"]["works"]),
                "checkpoint": "1-0",
            }
        self.indexes.setdefault(warehouse_id, set()).add(simulation_id)
        return deepcopy(self.simulations[simulation_id])

    def simulation_snapshot(self, simulation_id: str) -> dict:
        return deepcopy(self.simulations[simulation_id])

    def update_simulation_from_event(self, _event):
        raise AssertionError("이 테스트에는 replay 이벤트가 없어야 합니다.")

    @staticmethod
    def keys(simulation_id: str) -> list[str]:
        return [
            f"sim:{simulation_id}:inventory",
            f"sim:{simulation_id}:robots",
            f"sim:{simulation_id}:works",
            f"sim:{simulation_id}:events",
        ]

    def remove_simulation_state(self, warehouse_id: int, simulation_id: str) -> dict:
        if simulation_id in self.fail_ids:
            raise RuntimeError(f"redis unavailable token=raw-secret {simulation_id}")
        existed = simulation_id in self.simulations
        self.simulations.pop(simulation_id, None)
        self.indexes.setdefault(warehouse_id, set()).discard(simulation_id)
        keys = self.keys(simulation_id)
        self.removed_keys.extend(keys)
        return {
            "affected_redis_keys": keys,
            "deleted_redis_key_count": 4 if existed else 0,
        }

    def simulation_state_summary(self, simulation_id: str) -> dict:
        state = self.simulations.get(simulation_id)
        if state is None:
            return {"redis_state_exists": False}
        return {
            "inventory_record_count": len(state["inventory"]),
            "robot_count": len(state["robots"]),
            "work_count": len(state["works"]),
            "event_count": 16,
            "checkpoint": state["checkpoint"],
            "redis_state_exists": True,
        }


class ResetFakePostgres:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.runs: list[dict] = []
        self.command_history: dict[str, dict] = {}
        self.stage_logs: dict[str, list[dict]] = {}
        self.reset_audits: dict[str, dict] = {}
        self.operational = {
            "warehouse_items": {"I-1": 10},
            "works": {"W-1": "NEW"},
            "robot": {"R-REAL": {"node_id": 99}},
            "work_event": [],
            "inventory_reservation": {"I-1": 0},
        }

    def add_session(self, simulation_id: str, warehouse_id: int) -> None:
        now = datetime.now(UTC)
        state = virtual_state(simulation_id)
        self.sessions[simulation_id] = {
            "simulation_id": simulation_id,
            "warehouse_id": warehouse_id,
            "status": "ACTIVE",
            "generation": 1,
            "base_state": deepcopy(state),
            "current_state": deepcopy(state),
            "checkpoint": state["checkpoint"],
            "created_by_command_id": f"CREATE-{simulation_id}",
            "last_command_id": f"CREATE-{simulation_id}",
            "created_at": now,
            "updated_at": now,
            "reset_at": None,
            "reset_by": None,
            "reset_reason": None,
        }
        self.runs.append(
            {
                "run_id": f"RUN-{len(self.runs) + 1}",
                "simulation_id": simulation_id,
                "command_id": f"CREATE-{simulation_id}",
                "warehouse_id": warehouse_id,
                "plan_version": "P-1",
                "status": "SIMULATION_SUCCESS",
                "checkpoint": state["checkpoint"],
                "created_at": now,
            }
        )

    def record_simulation(self, state: dict) -> None:
        now = datetime.now(UTC)
        simulation_id = state["simulation_id"]
        self.runs.append(
            {
                "run_id": f"RUN-{len(self.runs) + 1}",
                "simulation_id": simulation_id,
                "command_id": state["command"]["command_id"],
                "warehouse_id": state["command"]["warehouse_id"],
                "plan_version": state.get("plan_version"),
                "status": state.get("final_status"),
                "checkpoint": state.get("simulation_checkpoint"),
                "created_at": now,
            }
        )
        if simulation_id not in self.sessions:
            self.sessions[simulation_id] = {
                "simulation_id": simulation_id,
                "warehouse_id": state["command"]["warehouse_id"],
                "status": "ACTIVE",
                "generation": 1,
                "base_state": deepcopy(state["simulation_base_state"]),
                "current_state": deepcopy(state["simulation_current_state"]),
                "checkpoint": state["simulation_checkpoint"],
                "created_by_command_id": state["command"]["command_id"],
                "last_command_id": state["command"]["command_id"],
                "created_at": now,
                "updated_at": now,
                "reset_at": None,
                "reset_by": None,
                "reset_reason": None,
            }

    def create_or_get_command_history(self, values: dict) -> dict:
        self.command_history.setdefault(values["command_id"], deepcopy(values))
        return deepcopy(self.command_history[values["command_id"]])

    def persist_stage_logs(self, command_id: str, stages: list[dict]) -> None:
        self.stage_logs.setdefault(command_id, []).extend(deepcopy(stages))

    def finalize_command_audit(self, history: dict, stages: list[dict]) -> None:
        self.command_history.setdefault(history["command_id"], {}).update(
            deepcopy(history)
        )
        existing = {
            (row["sequence"], row.get("attempt", 1))
            for row in self.stage_logs.get(history["command_id"], [])
        }
        self.stage_logs.setdefault(history["command_id"], []).extend(
            deepcopy(row)
            for row in stages
            if (row["sequence"], row.get("attempt", 1)) not in existing
        )

    def create_reset_audit(self, values: dict) -> None:
        self.reset_audits.setdefault(values["reset_id"], deepcopy(values))

    def finalize_reset_audit(self, reset_id: str, values: dict) -> None:
        self.reset_audits[reset_id].update(deepcopy(values))

    def get_simulation_session(self, simulation_id: str):
        value = self.sessions.get(simulation_id)
        return deepcopy(value) if value else None

    def list_resettable_simulation_sessions(self, warehouse_id: int) -> list[dict]:
        return [
            deepcopy(row)
            for row in self.sessions.values()
            if row["warehouse_id"] == warehouse_id and row["status"] != "RESET"
        ]

    def mark_simulation_reset_pending(self, simulation_id: str, command_id: str):
        session = self.sessions.get(simulation_id)
        if not session or session["status"] == "RESET":
            return None
        session["status"] = "RESET_PENDING"
        session["last_command_id"] = command_id
        return deepcopy(session)

    def complete_simulation_reset(self, **values) -> None:
        session = self.sessions[values["simulation_id"]]
        session.update(
            {
                "status": values["status"],
                "last_command_id": values["command_id"],
                "reset_at": values["reset_at"],
                "reset_by": values["actor_id"],
                "reset_reason": values["reason"],
                "updated_at": datetime.now(UTC),
            }
        )

    def list_simulation_sessions(self, **filters) -> list[dict]:
        rows = list(self.sessions.values())
        if filters.get("warehouse_id") is not None:
            rows = [r for r in rows if r["warehouse_id"] == filters["warehouse_id"]]
        if filters.get("status") is not None:
            rows = [r for r in rows if r["status"] == filters["status"]]
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        start = filters.get("offset", 0)
        end = start + filters.get("limit", 50)
        return [
            {k: deepcopy(v) for k, v in row.items() if k not in {"base_state", "current_state"}}
            for row in rows[start:end]
        ]

    def list_simulation_runs(self, simulation_id: str, *, limit=50, offset=0):
        rows = [row for row in self.runs if row["simulation_id"] == simulation_id]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return deepcopy(rows[offset : offset + limit])

    def get_latest_simulation_run(self, simulation_id: str):
        rows = self.list_simulation_runs(simulation_id, limit=1)
        return rows[0] if rows else None

    def list_simulation_reset_audits(
        self, *, warehouse_id=None, simulation_id=None, limit=50, offset=0
    ):
        rows = list(self.reset_audits.values())
        if warehouse_id is not None:
            rows = [row for row in rows if row["warehouse_id"] == warehouse_id]
        if simulation_id is not None:
            rows = [
                row for row in rows
                if row.get("target_simulation_id") == simulation_id
            ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return deepcopy(rows[offset : offset + limit])

    def list_simulation_logs(self, simulation_id: str):
        commands = [
            row for row in self.command_history.values()
            if row.get("simulation_id") == simulation_id
        ]
        command_ids = {row["command_id"] for row in commands}
        return {
            "commands": deepcopy(commands),
            "stages": [
                deepcopy(stage)
                for command_id in command_ids
                for stage in self.stage_logs.get(command_id, [])
            ],
            "reset_audits": self.list_simulation_reset_audits(
                simulation_id=simulation_id
            ),
        }


class ResetFakeNeo4j:
    def __init__(self) -> None:
        self.write_calls = 0


def services() -> SimpleNamespace:
    return SimpleNamespace(
        postgres=ResetFakePostgres(),
        redis=ResetFakeRedis(),
        neo4j=ResetFakeNeo4j(),
    )


def add_session(bundle, simulation_id: str, warehouse_id: int) -> None:
    bundle.postgres.add_session(simulation_id, warehouse_id)
    bundle.redis.add(simulation_id, warehouse_id)


def test_simulate_only_creates_session_base_current_checkpoint_and_redis_index() -> None:
    bundle = services()
    state = {
        "command": {"command_id": "C-SIM", "warehouse_id": 1},
        "snapshot": {
            "warehouse_id": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "sql": {
                "inventory": virtual_state("SIM-X")["inventory"],
                "robots": virtual_state("SIM-X")["robots"],
                "works": virtual_state("SIM-X")["works"],
            },
            "redis": {"robots": []},
        },
        "required_tasks": [],
        "cuopt_plan": {"scheduled_tasks": []},
        "collision_plan": {"routes": [], "time_step_seconds": 5},
        "plan_version": "P-SIM",
        "final_status": "SIMULATION_SUCCESS",
    }
    replayed = replay_simulation_session(
        state,
        SimulationResult(success=True, valid=True, status="SUCCESS"),
        bundle.redis,
    )
    state.update(
        {
            "simulation_id": replayed["simulation_id"],
            "simulation_base_state": replayed["base_state"],
            "simulation_current_state": replayed["current_state"],
            "simulation_checkpoint": replayed["checkpoint"],
        }
    )
    bundle.postgres.record_simulation(state)

    session = bundle.postgres.sessions[replayed["simulation_id"]]
    assert session["warehouse_id"] == 1
    assert session["base_state"]
    assert session["current_state"]
    assert session["checkpoint"]
    assert replayed["simulation_id"] in bundle.redis.indexes[1]


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params):
        self.calls.append((str(statement), deepcopy(params)))
        return SimpleNamespace(rowcount=1)


class RecordingBegin:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class RecordingEngine:
    def __init__(self) -> None:
        self.connection = RecordingConnection()

    def begin(self):
        return RecordingBegin(self.connection)


def test_record_simulation_is_append_only_for_same_simulation_id() -> None:
    repository = PostgresRepository.__new__(PostgresRepository)
    repository.engine = RecordingEngine()
    state = {
        "command": {"command_id": "C-1", "warehouse_id": 1},
        "simulation_id": "SIM-APPEND",
        "final_status": "SIMULATION_SUCCESS",
        "errors": [],
        "warnings": [],
        "trace": [],
    }

    repository.record_simulation(state)
    repository.record_simulation(state)

    calls = repository.engine.connection.calls
    run_inserts = [sql for sql, _ in calls if "INSERT INTO simulation_run" in sql]
    run_updates = [sql for sql, _ in calls if "UPDATE simulation_run" in sql]
    run_ids = [params["run_id"] for sql, params in calls if "INSERT INTO simulation_run" in sql]
    assert len(run_inserts) == 2
    assert run_updates == []
    assert len(set(run_ids)) == 2


def test_single_reset_deletes_only_target_and_preserves_logs_and_real_data() -> None:
    bundle = services()
    add_session(bundle, "SIM-A", 1)
    add_session(bundle, "SIM-B", 1)
    before_operational = deepcopy(bundle.postgres.operational)
    before_real_redis = deepcopy(bundle.redis.real_state)
    before_runs = deepcopy(bundle.postgres.runs)

    result = SimulationResetService(bundle).reset_simulation(
        "SIM-A",
        SimulationResetRequest(actor_id="user-01", reason="retry"),
    )

    assert result["status"] == "RESET_COMPLETED"
    assert result["deleted_redis_key_count"] == 4
    assert bundle.postgres.sessions["SIM-A"]["status"] == "RESET"
    assert bundle.postgres.sessions["SIM-A"]["reset_by"] == "user-01"
    assert bundle.postgres.sessions["SIM-A"]["reset_reason"] == "retry"
    assert bundle.postgres.sessions["SIM-A"]["reset_at"] is not None
    assert "SIM-A" not in bundle.redis.simulations
    assert "SIM-B" in bundle.redis.simulations
    assert bundle.postgres.runs == before_runs
    assert bundle.postgres.operational == before_operational
    assert bundle.redis.real_state == before_real_redis
    assert bundle.neo4j.write_calls == 0
    assert result["command_id"] in bundle.postgres.command_history
    assert bundle.postgres.stage_logs[result["command_id"]]
    assert result["reset_id"] in bundle.postgres.reset_audits


def test_single_reset_is_idempotent() -> None:
    bundle = services()
    add_session(bundle, "SIM-A", 1)
    service = SimulationResetService(bundle)
    service.reset_simulation("SIM-A", SimulationResetRequest(reason="first"))
    real_state = deepcopy(bundle.redis.real_state)

    result = service.reset_simulation(
        "SIM-A",
        SimulationResetRequest(reason="again"),
    )

    assert result["status"] == "ALREADY_RESET"
    assert result["deleted_redis_key_count"] == 0
    assert bundle.redis.real_state == real_state


def test_missing_session_returns_404_and_preserves_failed_command(monkeypatch) -> None:
    bundle = services()
    monkeypatch.setattr(api_module, "get_services", lambda: bundle)
    client = TestClient(app)

    response = client.post(
        "/v1/simulations/UNKNOWN/reset",
        json={"reason": "missing", "warehouse_id": 1},
    )

    assert response.status_code == 404
    command_id = response.json()["detail"]["command_id"]
    assert bundle.postgres.command_history[command_id]["status"] == "FAILED"
    names = [row["node_name"] for row in bundle.postgres.stage_logs[command_id]]
    assert "RESET_FAILED" in names
    assert "COMMAND_FAILED" in names


def test_reset_all_is_warehouse_scoped_and_reports_partial_failure() -> None:
    bundle = services()
    add_session(bundle, "SIM-A", 1)
    add_session(bundle, "SIM-B", 1)
    add_session(bundle, "SIM-C", 2)
    bundle.redis.fail_ids.add("SIM-B")

    result = SimulationResetService(bundle).reset_all_simulations(
        1,
        SimulationResetRequest(actor_id="user-01", reason="all retry"),
    )

    assert result["status"] == "RESET_ALL_PARTIAL"
    assert result["success_simulation_ids"] == ["SIM-A"]
    assert result["failed_simulations"][0]["simulation_id"] == "SIM-B"
    assert bundle.postgres.sessions["SIM-A"]["status"] == "RESET"
    assert bundle.postgres.sessions["SIM-B"]["status"] == "RESET_FAILED"
    assert bundle.postgres.sessions["SIM-C"]["status"] == "ACTIVE"
    assert "SIM-C" in bundle.redis.simulations
    assert all("SIM-C" not in key for key in bundle.redis.removed_keys)


def test_redis_failure_keeps_session_retryable_and_real_data_unchanged() -> None:
    bundle = services()
    add_session(bundle, "SIM-FAIL", 1)
    bundle.redis.fail_ids.add("SIM-FAIL")
    operational = deepcopy(bundle.postgres.operational)
    real_redis = deepcopy(bundle.redis.real_state)

    result = SimulationResetService(bundle).reset_simulation(
        "SIM-FAIL",
        SimulationResetRequest(reason="retry after redis failure"),
    )

    assert result["status"] == "RESET_FAILED"
    assert bundle.postgres.sessions["SIM-FAIL"]["status"] == "RESET_FAILED"
    assert "SIM-FAIL" in bundle.redis.simulations
    assert bundle.postgres.operational == operational
    assert bundle.redis.real_state == real_redis
    assert bundle.postgres.reset_audits[result["reset_id"]]["failure_summary"]


def test_simulation_query_apis_and_pagination(monkeypatch) -> None:
    bundle = services()
    add_session(bundle, "SIM-A", 1)
    add_session(bundle, "SIM-B", 2)
    reset = SimulationResetService(bundle).reset_simulation(
        "SIM-A",
        SimulationResetRequest(reason="api history"),
    )
    monkeypatch.setattr(api_module, "get_services", lambda: bundle)
    client = TestClient(app)

    listed = client.get("/v1/simulations?warehouse_id=1&limit=1&offset=0")
    detail = client.get("/v1/simulations/SIM-A")
    state = client.get("/v1/simulations/SIM-A/state")
    runs = client.get("/v1/simulations/SIM-A/runs?limit=1")
    logs = client.get("/v1/simulations/SIM-A/logs")
    reset_logs = client.get("/v1/warehouses/1/simulation-reset-logs")
    missing = client.get("/v1/simulations/UNKNOWN")

    assert listed.status_code == 200 and listed.json()["count"] == 1
    assert "base_state" not in detail.json()["simulation_session"]
    assert state.json()["base_state"]
    assert runs.json()["runs"]
    assert logs.json()["reset_audits"]
    assert reset_logs.json()["reset_logs"][0]["reset_id"] == reset["reset_id"]
    assert missing.status_code == 404


def test_reset_logs_redact_secrets_in_reason_and_failure() -> None:
    bundle = services()
    add_session(bundle, "SIM-SECRET", 1)

    result = SimulationResetService(bundle).reset_simulation(
        "SIM-SECRET",
        SimulationResetRequest(
            reason="password=visible token:raw-token API_KEY=raw-key"
        ),
    )

    persisted = repr(
        {
            "session": bundle.postgres.sessions["SIM-SECRET"],
            "command": bundle.postgres.command_history[result["command_id"]],
            "stages": bundle.postgres.stage_logs[result["command_id"]],
            "audit": bundle.postgres.reset_audits[result["reset_id"]],
        }
    )
    assert "visible" not in persisted
    assert "raw-token" not in persisted
    assert "raw-key" not in persisted

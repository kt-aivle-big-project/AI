from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.execution.graph as execution_module
from app.models import CommandInterpretation, NaturalLanguageCommand, RobotEvent
from app.planning import nodes as planning_nodes
from app.repositories.postgres import PostgresRepository
from app.services.inventory_transition import calculate_inventory_transition


class InMemoryRedis:
    def __init__(self) -> None:
        self.real_robots = {"R1": {"robot_id": "R1", "node_id": 1}}
        self.simulations = {
            simulation_id: {
                "simulation_id": simulation_id,
                "inventory": [
                    {
                        "warehouse_item_id": "ITEM-ROW-1",
                        "quantity": 10,
                        "reserved_quantity": 0,
                        "available_quantity": 10,
                    }
                ],
                "robots": [{"robot_id": "R1", "node_id": 1, "status": "IDLE"}],
                "works": [{"work_id": "W1", "status": "EXECUTING"}],
                "checkpoint": "0-0",
            }
            for simulation_id in ("SIM-A", "SIM-B")
        }
        self.simulation_event_count = 0

    def update_from_event(self, event: RobotEvent) -> None:
        assert event.execution_context == "REAL"
        robot = self.real_robots.setdefault(event.robot_id, {"robot_id": event.robot_id})
        if event.node_id is not None:
            robot["node_id"] = event.node_id
        robot["last_event"] = event.event_type

    def update_simulation_from_event(self, event: RobotEvent) -> dict:
        assert event.execution_context == "SIMULATION"
        session = self.simulations[event.simulation_id]
        self.simulation_event_count += 1
        session["checkpoint"] = f"{self.simulation_event_count}-0"
        for robot in session["robots"]:
            if robot["robot_id"] == event.robot_id:
                if event.node_id is not None:
                    robot["node_id"] = event.node_id
                robot["last_event"] = event.event_type
                robot["status"] = (
                    "IDLE" if event.event_type == "TASK_COMPLETED" else "EXECUTING"
                )
        if event.event_type == "TASK_COMPLETED":
            current = {
                row["warehouse_item_id"]: row["quantity"]
                for row in session["inventory"]
            }
            transitioned = calculate_inventory_transition(
                current,
                event.inventory_deltas,
            )
            for row in session["inventory"]:
                row["quantity"] = transitioned[row["warehouse_item_id"]]
                row["available_quantity"] = row["quantity"] - row["reserved_quantity"]
            for work in session["works"]:
                if work["work_id"] == event.work_id:
                    work["status"] = "COMPLETED"
        return deepcopy(session)

    def simulation_snapshot(self, simulation_id: str) -> dict:
        return deepcopy(self.simulations[simulation_id])

    def emit_replan_required(self, _event: RobotEvent) -> str:
        return "REAL-REPLAN-1"


class InMemoryPostgres:
    def __init__(self) -> None:
        self.inventory = {"ITEM-ROW-1": 10}
        self.works = {"W1": "EXECUTING"}
        self.real_completion_count = 0
        self.simulation_checkpoint_count = 0

    def commit_completion(self, event: RobotEvent) -> dict:
        assert event.execution_context == "REAL"
        self.inventory = calculate_inventory_transition(
            self.inventory,
            event.inventory_deltas,
        )
        self.works[event.work_id] = "COMPLETED"
        self.real_completion_count += 1
        return {"committed": True}

    def update_simulation_checkpoint(
        self,
        event: RobotEvent,
        current_state: dict,
        checkpoint: str,
    ) -> dict:
        assert event.execution_context == "SIMULATION"
        assert current_state["simulation_id"] == event.simulation_id
        assert checkpoint == current_state["checkpoint"]
        self.simulation_checkpoint_count += 1
        return {"saved": True, "simulation_id": event.simulation_id}


def install_services(monkeypatch) -> SimpleNamespace:
    services = SimpleNamespace(
        redis=InMemoryRedis(),
        postgres=InMemoryPostgres(),
    )
    monkeypatch.setattr(execution_module, "get_services", lambda: services)
    return services


def completion_event(
    *,
    execution_context: str,
    simulation_id: str | None = None,
    quantity_delta: int = -3,
) -> RobotEvent:
    return RobotEvent(
        event_id=f"EVENT-{execution_context}-{simulation_id or 'REAL'}",
        warehouse_id=1,
        robot_id="R1",
        work_id="W1",
        task_id="T1",
        event_type="TASK_COMPLETED",
        node_id=2,
        inventory_deltas=[
            {
                "warehouse_item_id": "ITEM-ROW-1",
                "quantity_delta": quantity_delta,
            }
        ],
        execution_context=execution_context,
        simulation_id=simulation_id,
    )


def test_simulation_completion_changes_only_virtual_inventory_and_work(monkeypatch) -> None:
    services = install_services(monkeypatch)

    result = execution_module.handle_robot_event(
        completion_event(execution_context="SIMULATION", simulation_id="SIM-A")
    )

    assert result["final_status"] == "SIMULATION_COMPLETED"
    assert services.postgres.inventory["ITEM-ROW-1"] == 10
    assert services.postgres.works["W1"] == "EXECUTING"
    assert services.postgres.real_completion_count == 0
    assert services.postgres.simulation_checkpoint_count == 1
    sim_a = services.redis.simulations["SIM-A"]
    assert sim_a["inventory"][0]["quantity"] == 7
    assert sim_a["works"][0]["status"] == "COMPLETED"


def test_real_completion_uses_same_rule_and_does_not_change_simulation(monkeypatch) -> None:
    services = install_services(monkeypatch)
    before_simulation = deepcopy(services.redis.simulations)

    result = execution_module.handle_robot_event(
        completion_event(execution_context="REAL")
    )

    assert result["final_status"] == "COMPLETED"
    assert services.postgres.inventory["ITEM-ROW-1"] == 7
    assert services.postgres.works["W1"] == "COMPLETED"
    assert services.redis.real_robots["R1"]["node_id"] == 2
    assert services.redis.simulations == before_simulation


def test_simulation_id_is_required() -> None:
    with pytest.raises(ValidationError):
        completion_event(execution_context="SIMULATION")


@pytest.mark.parametrize("battery", [-1, 101])
def test_real_robot_event_rejects_impossible_battery_before_processing(
    battery: int,
) -> None:
    with pytest.raises(ValidationError):
        RobotEvent(
            warehouse_id=1,
            robot_id="R1",
            work_id="W1",
            event_type="TASK_COMPLETED",
            battery=battery,
            execution_context="REAL",
        )


def test_simulation_sessions_are_isolated(monkeypatch) -> None:
    services = install_services(monkeypatch)

    execution_module.handle_robot_event(
        completion_event(
            execution_context="SIMULATION",
            simulation_id="SIM-A",
            quantity_delta=-3,
        )
    )
    execution_module.handle_robot_event(
        completion_event(
            execution_context="SIMULATION",
            simulation_id="SIM-B",
            quantity_delta=-2,
        )
    )

    assert services.redis.simulations["SIM-A"]["inventory"][0]["quantity"] == 7
    assert services.redis.simulations["SIM-B"]["inventory"][0]["quantity"] == 8


def test_postgres_rejects_simulation_event_before_transaction() -> None:
    repository = PostgresRepository.__new__(PostgresRepository)

    with pytest.raises(RuntimeError, match="실제 warehouse_items 또는 works"):
        repository.commit_completion(
            completion_event(
                execution_context="SIMULATION",
                simulation_id="SIM-A",
            )
        )


def test_existing_simulation_replan_uses_virtual_state_not_real_state(monkeypatch) -> None:
    class NoRealPostgres:
        def snapshot(self, *_args, **_kwargs):
            raise AssertionError("실제 PostgreSQL snapshot을 읽으면 안 됩니다.")

    class SimulationRedis:
        def simulation_snapshot(self, simulation_id: str) -> dict:
            assert simulation_id == "SIM-A"
            return {
                "simulation_id": simulation_id,
                "inventory": [],
                "robots": [
                    {
                        "robot_id": "R1",
                        "node_id": 2,
                        "battery": 80,
                        "status": "IDLE",
                    }
                ],
                "works": [
                    {
                        "work_id": "W-SIM",
                        "status": "NEW",
                        "source_node": 2,
                        "target_node": 3,
                    }
                ],
                "checkpoint": "3-0",
            }

        def live_snapshot(self, *_args, **_kwargs):
            raise AssertionError("실제 Redis live snapshot을 읽으면 안 됩니다.")

    class StaticNeo4j:
        def fetch_topology(self, _warehouse_id: int) -> dict:
            return {
                "nodes": [{"node_id": value} for value in (1, 2, 3)],
                "edges": [
                    {"from_node": 1, "to_node": 2},
                    {"from_node": 2, "to_node": 3},
                ],
            }

        def validate_node_ids(self, _warehouse_id: int, node_ids: list[int]) -> dict:
            return {
                "valid": sorted({value for value in node_ids if value is not None}),
                "missing": [],
            }

    monkeypatch.setattr(
        planning_nodes,
        "get_services",
        lambda: SimpleNamespace(
            postgres=NoRealPostgres(),
            redis=SimulationRedis(),
            neo4j=StaticNeo4j(),
        ),
    )
    command = NaturalLanguageCommand(
        warehouse_id=1,
        text="같은 시뮬레이션에 신규 작업을 추가해줘",
        requested_execution_mode="SIMULATE_ONLY",
        simulation_id="SIM-A",
    )
    interpretation = CommandInterpretation(
        command_kind="PLAN",
        intent="INSERT_TASK",
        objective="가상 상태에 작업 추가",
        execution_mode="SIMULATE_ONLY",
        summary="simulation replan",
    )

    update = planning_nodes.build_snapshot_node(
        {
            "command": command.model_dump(mode="json"),
            "interpretation": interpretation.model_dump(mode="json"),
        }
    )

    assert update["validation"]["valid"] is True
    assert update["snapshot"]["sql"]["robots"][0]["node_id"] == 2
    assert update["snapshot"]["sql"]["works"][0]["work_id"] == "W-SIM"
    assert update["trace"][0]["state_source"] == "SIMULATION"

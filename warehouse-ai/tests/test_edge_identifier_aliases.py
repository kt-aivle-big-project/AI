from types import SimpleNamespace

from app.models import CommandInterpretation, NaturalLanguageCommand
from app.planning import nodes as planning_nodes


def test_edge_identifier_aliases_include_edge_id_and_endpoint_aliases():
    aliases = planning_nodes._edge_identifier_aliases(
        {
            "edge_id": "H1_1",
            "from_node": 2013,
            "to_node": 2014,
            "direction": "BOTH",
        }
    )

    assert aliases == {"H1_1", "2013->2014", "2014->2013"}


def test_build_snapshot_accepts_endpoint_alias_when_neo4j_has_stable_edge_id(monkeypatch):
    class Postgres:
        def snapshot(self, _warehouse_id, _item_ids):
            return {
                "inventory": [],
                "robots": [
                    {
                        "robot_id": "R2-03",
                        "node_id": 2013,
                        "battery": 90,
                        "status": "IDLE",
                    }
                ],
                "works": [],
            }

    class Redis:
        def live_snapshot(self, _warehouse_id):
            return {
                "robots": [],
                "tasks": [],
                "executing_task_ids": [],
                "planned_task_ids": [],
                "active_plan_version": None,
                "active_plan": None,
                "temporary_closures": [],
            }

    class Neo4j:
        def fetch_topology(self, _warehouse_id):
            return {
                "nodes": [{"node_id": 2013}, {"node_id": 2014}],
                "edges": [
                    {
                        "edge_id": "H1_1",
                        "from_node": 2013,
                        "to_node": 2014,
                        "direction": "BOTH",
                    }
                ],
            }

        def validate_node_ids(self, _warehouse_id, node_ids):
            values = sorted({int(value) for value in node_ids if value is not None})
            return {"valid": values, "missing": []}

    monkeypatch.setattr(
        planning_nodes,
        "get_services",
        lambda: SimpleNamespace(postgres=Postgres(), redis=Redis(), neo4j=Neo4j()),
    )

    command = NaturalLanguageCommand(
        warehouse_id=2,
        text=(
            "R2-03의 상태를 조회하고, 2013번 노드와 2014번 노드 사이 "
            "통로는 폐쇄된 것으로 가정해줘."
        ),
    )
    interpretation = CommandInterpretation(
        command_kind="QUERY",
        intent="ROBOT_QUERY",
        objective=command.text,
        query_target="ROBOT",
        query_action="DETAIL",
        execution_mode="PLAN_ONLY",
        target_robot_ids=["R2-03"],
        extracted_robot_ids=["R2-03"],
        excluded_edge_ids=["2013->2014"],
        assumed_closed_edges=[
            {"from_node": 2013, "to_node": 2014, "bidirectional": False}
        ],
        summary="ROBOT DETAIL 조회",
    )

    update = planning_nodes.build_snapshot_node(
        {
            "command": command.model_dump(mode="json"),
            "interpretation": interpretation.model_dump(mode="json"),
        }
    )

    assert update["validation"]["valid"] is True
    assert update["errors"] == []
    assert update["interpretation"]["excluded_edge_ids"] == ["2013->2014"]

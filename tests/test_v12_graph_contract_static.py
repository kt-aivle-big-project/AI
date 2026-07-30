"""Static topology validation for the v12 direct Rule / stepwise Agent graph."""
from __future__ import annotations

import importlib
import sys
import types


def test_all_declared_graph_sources_and_targets_exist(monkeypatch) -> None:
    START = "__START__"
    END = "__END__"

    class RecordingStateGraph:
        def __init__(self, *args, **kwargs):
            self.nodes = set()
            self.conditional = []
            self.edges = []

        def add_node(self, name, node):
            assert name not in self.nodes
            self.nodes.add(name)

        def add_conditional_edges(self, source, router, mapping):
            self.conditional.append((source, set(mapping.values())))

        def add_edge(self, source, target):
            self.edges.append((source, target))

        def compile(self):
            valid_sources = self.nodes | {START}
            valid_targets = self.nodes | {END}
            for source, targets in self.conditional:
                assert source in valid_sources, f"unknown conditional source {source}"
                assert targets <= valid_targets, f"unknown conditional targets {targets - valid_targets}"
            for source, target in self.edges:
                assert source in valid_sources, f"unknown edge source {source}"
                assert target in valid_targets, f"unknown edge target {target}"
            return self

    package = types.ModuleType("langgraph")
    graph_module = types.ModuleType("langgraph.graph")
    graph_module.START = START
    graph_module.END = END
    graph_module.StateGraph = RecordingStateGraph
    package.graph = graph_module
    monkeypatch.setitem(sys.modules, "langgraph", package)
    monkeypatch.setitem(sys.modules, "langgraph.graph", graph_module)
    sys.modules.pop("app.graph.build_graph", None)
    module = importlib.import_module("app.graph.build_graph")
    compiled = module.build_laro_graph()

    assert "structured_key_validator" in compiled.nodes
    assert "canonical_retrieval_key_builder" in compiled.nodes
    assert "llm_retrieval_agent" in compiled.nodes
    assert "retrieval_tool_call_validator" in compiled.nodes
    assert "query_key_resolver" in compiled.nodes
    assert "retrieval_tool_executor" in compiled.nodes
    assert "retrieval_context_sufficiency_guard" in compiled.nodes
    assert "agent_context_materializer" in compiled.nodes
    assert "warehouse_situation_graph_builder" in compiled.nodes
    assert "llm_cuopt_formulator" in compiled.nodes
    assert "prioritized_mapf_planner" in compiled.nodes

    # The Rule path is intentionally direct and must not contain query-planning nodes.
    assert "rule_query_planner" not in compiled.nodes
    assert "query_plan_validator" not in compiled.nodes
    assert "retrieval_tool_dispatcher" not in compiled.nodes
    assert "event_detector" not in compiled.nodes

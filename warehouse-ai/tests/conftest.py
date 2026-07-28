"""Conditional test-only stubs for optional runtime integrations.

The release gate exercises deterministic planning logic in environments where
LangChain, Neo4j, Redis, or SQLAlchemy clients may not be installed. Real
installations are never shadowed.
"""

from __future__ import annotations

import importlib.util
import sys
import types


def _missing(name: str) -> bool:
    return importlib.util.find_spec(name) is None


if _missing("langchain_core"):
    core = types.ModuleType("langchain_core")
    messages = types.ModuleType("langchain_core.messages")

    class _Message:
        def __init__(self, content=None, **kwargs):
            self.content = content
            self.additional_kwargs = kwargs

    messages.HumanMessage = _Message
    messages.SystemMessage = _Message
    sys.modules["langchain_core"] = core
    sys.modules["langchain_core.messages"] = messages


if _missing("langchain_openai"):
    module = types.ModuleType("langchain_openai")

    class ChatOpenAI:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, *args, **kwargs):
            raise RuntimeError("TEST_STUB_OPENAI_DISABLED")

    module.ChatOpenAI = ChatOpenAI
    sys.modules["langchain_openai"] = module


if _missing("neo4j"):
    module = types.ModuleType("neo4j")

    class _Driver:
        def verify_connectivity(self):
            return None

        def close(self):
            return None

        def execute_query(self, *args, **kwargs):
            return [], None, None

    class GraphDatabase:
        @staticmethod
        def driver(*args, **kwargs):
            return _Driver()

    class RoutingControl:
        READ = "READ"

    module.GraphDatabase = GraphDatabase
    module.RoutingControl = RoutingControl
    sys.modules["neo4j"] = module


if _missing("redis"):
    module = types.ModuleType("redis")
    exceptions = types.ModuleType("redis.exceptions")

    class WatchError(Exception):
        pass

    class _Script:
        def __call__(self, *args, **kwargs):
            return None

    class Redis:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

        def register_script(self, *args, **kwargs):
            return _Script()

    module.Redis = Redis
    exceptions.WatchError = WatchError
    sys.modules["redis"] = module
    sys.modules["redis.exceptions"] = exceptions


if _missing("sqlalchemy"):
    module = types.ModuleType("sqlalchemy")
    exc = types.ModuleType("sqlalchemy.exc")

    class Engine:
        pass

    class SQLAlchemyError(Exception):
        pass

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            return self

        def scalar_one(self):
            return 1

    class _Engine(Engine):
        def connect(self):
            return _Connection()

        def begin(self):
            return _Connection()

    def create_engine(*args, **kwargs):
        return _Engine()

    def text(value):
        return value

    module.Engine = Engine
    module.create_engine = create_engine
    module.text = text
    exc.SQLAlchemyError = SQLAlchemyError
    sys.modules["sqlalchemy"] = module
    sys.modules["sqlalchemy.exc"] = exc

if _missing("langgraph"):
    langgraph = types.ModuleType("langgraph")
    graph_module = types.ModuleType("langgraph.graph")
    START = "__start__"
    END = "__end__"

    class _CompiledGraph:
        def __init__(self, nodes, edges, conditionals):
            self.nodes = dict(nodes)
            self.edges = {key: list(value) for key, value in edges.items()}
            self.conditionals = dict(conditionals)

        @staticmethod
        def _merge(state, update):
            additive = {
                "supervisor_warnings",
                "verification_warnings",
                "audit_warnings",
                "errors",
                "warnings",
                "trace",
            }
            for key, value in (update or {}).items():
                if key in additive:
                    state[key] = list(state.get(key, [])) + list(value or [])
                else:
                    state[key] = value

        def invoke(self, initial, config=None):
            state = dict(initial)
            current = self.edges[START][0]
            limit = int((config or {}).get("recursion_limit", 100))
            steps = 0
            while current != END:
                steps += 1
                if steps > limit:
                    raise RuntimeError("GRAPH_RECURSION_LIMIT")
                update = self.nodes[current](state)
                self._merge(state, update)
                if current in self.conditionals:
                    router, mapping = self.conditionals[current]
                    branch = router(state)
                    current = mapping[branch]
                else:
                    targets = self.edges.get(current, [])
                    if not targets:
                        raise RuntimeError(f"GRAPH_EDGE_MISSING:{current}")
                    current = targets[0]
            return state

    class StateGraph:
        def __init__(self, state_type=None):
            self.nodes = {}
            self.edges = {}
            self.conditionals = {}

        def add_node(self, name, fn):
            self.nodes[name] = fn

        def add_edge(self, source, target):
            self.edges.setdefault(source, []).append(target)

        def add_conditional_edges(self, source, router, mapping):
            self.conditionals[source] = (router, dict(mapping))

        def compile(self):
            return _CompiledGraph(self.nodes, self.edges, self.conditionals)

    graph_module.START = START
    graph_module.END = END
    graph_module.StateGraph = StateGraph
    sys.modules["langgraph"] = langgraph
    sys.modules["langgraph.graph"] = graph_module


if _missing("openai"):
    openai = types.ModuleType("openai")
    lib = types.ModuleType("openai.lib")
    pydantic_module = types.ModuleType("openai.lib._pydantic")

    def to_strict_json_schema(model):
        return model.model_json_schema()

    pydantic_module.to_strict_json_schema = to_strict_json_schema
    sys.modules["openai"] = openai
    sys.modules["openai.lib"] = lib
    sys.modules["openai.lib._pydantic"] = pydantic_module

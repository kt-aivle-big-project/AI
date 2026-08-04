"""Project the existing Spring BE map tables into LARO Neo4j RouteNode/TRAVERSES."""
from __future__ import annotations

import argparse
import json

from app.infrastructure.be_centered_postgres import BeCenteredPostgresAdapter
from app.infrastructure.manager import get_infrastructure_manager
from app.services.be_route_projection import build_projection


def sync(warehouse_id: int, *, replace: bool = True) -> dict[str, Any]:
    manager = get_infrastructure_manager()
    manager.start()
    adapter = BeCenteredPostgresAdapter(manager=manager)
    adapter.require_views()
    warehouse = next(
        (value for value in adapter.list_warehouses() if int(value["warehouse_id"]) == warehouse_id),
        None,
    )
    if warehouse is None:
        raise ValueError(f"warehouse_id={warehouse_id} does not exist")
    node_rows = adapter.route_nodes(warehouse_id)
    edge_rows = adapter.route_edges(warehouse_id)
    nodes, edges = build_projection(node_rows, edge_rows)
    snapshot = manager.neo4j.load_route_graph(
        warehouse_id=str(warehouse["warehouse_code"]),
        nodes=nodes,
        edges=edges,
        replace=replace,
    )
    return {
        "status": "PASS",
        "warehouse_id": warehouse_id,
        "warehouse_code": warehouse["warehouse_code"],
        "node_count": len(snapshot.nodes),
        "edge_count": len(snapshot.edges),
        "graph_version": snapshot.version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse-id", type=int, required=True)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()
    print(json.dumps(sync(args.warehouse_id, replace=not args.append), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

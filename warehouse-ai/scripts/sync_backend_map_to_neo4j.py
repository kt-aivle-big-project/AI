"""Upsert the backend_laro map into the AI Neo4j graph contract."""

from __future__ import annotations

import argparse
from collections import Counter

from app.config import get_settings
from app.repositories import BackendLaroPostgresAdapter, Neo4jRepository


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "warehouse_id=1 backend_laro 지도를 AI Neo4j 계약으로 "
            "upsert합니다. 기본 동작은 dry-run입니다."
        )
    )
    parser.add_argument("--warehouse-id", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="명시적으로 지정한 경우에만 Neo4j에 upsert합니다.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="변경 예정 내역만 확인합니다(기본값).",
    )
    args = parser.parse_args()

    if args.warehouse_id != 1:
        print("Refusing to sync: backend_laro target must be warehouse_id=1.")
        return 2

    settings = get_settings()
    if not settings.database_url:
        print("Map sync unavailable. Missing setting: DATABASE_URL")
        return 2

    try:
        postgres = BackendLaroPostgresAdapter(settings.database_url)
        backend_map = postgres.fetch_map(args.warehouse_id)
    except Exception:
        print(
            "Map sync failed while reading backend_laro map data. "
            "No Neo4j data was modified."
        )
        return 2

    nodes = backend_map["nodes"]
    edges = backend_map["edges"]
    node_types = Counter(str(row["node_type"]) for row in nodes)
    print("Mode: " + ("APPLY" if args.apply else "DRY_RUN"))
    print(f"Warehouse: {args.warehouse_id}")
    print(f"Nodes to upsert: {len(nodes)}")
    print(f"Edges to upsert: {len(edges)}")
    print(
        "Node types: "
        + ", ".join(
            f"{node_type}={count}"
            for node_type, count in sorted(node_types.items())
        )
    )
    if not args.apply:
        return 0

    missing = [
        name
        for name, value in {
            "NEO4J_URI": settings.neo4j_uri,
            "NEO4J_PASSWORD": settings.neo4j_password,
        }.items()
        if not value
    ]
    if missing:
        print("Map sync unavailable. Missing settings: " + ", ".join(missing))
        return 2

    neo4j = Neo4jRepository(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        settings.neo4j_database,
    )
    try:
        result = neo4j.upsert_map(
            args.warehouse_id,
            f"BACKEND_LARO_{args.warehouse_id}",
            nodes,
            edges,
        )
        print(f"Neo4j nodes upserted: {result['nodes']}")
        print(f"Neo4j edges upserted: {result['edges']}")
        return 0
    except Exception:
        print("Neo4j map upsert failed. No delete operation was used.")
        return 2
    finally:
        neo4j.close()


if __name__ == "__main__":
    raise SystemExit(main())

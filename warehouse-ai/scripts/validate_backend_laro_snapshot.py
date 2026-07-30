"""Read-only consistency checks for the backend_laro planning snapshot."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.config import get_settings
from app.repositories import (
    BackendLaroPostgresAdapter,
    Neo4jRepository,
    RedisRepository,
)


def build_consistency_report(
    *,
    warehouse_id: int,
    sql_snapshot: dict[str, Any],
    backend_map_nodes: list[dict[str, Any]],
    graph_snapshot: dict[str, Any],
    chargers: list[dict[str, Any]],
    redis_snapshot: dict[str, Any],
) -> dict[str, Any]:
    graph_node_ids = {
        int(row["node_id"])
        for row in graph_snapshot.get("nodes", [])
        if row.get("node_id") is not None
    }
    postgres_robot_ids = {
        str(row["robot_id"])
        for row in sql_snapshot.get("robots", [])
        if row.get("robot_id") is not None
    }
    redis_robot_ids = {
        str(row["robot_id"])
        for row in redis_snapshot.get("robots", [])
        if row.get("robot_id") is not None
    }
    robot_node_ids = {
        int(row["node_id"])
        for row in sql_snapshot.get("robots", [])
        if row.get("node_id") is not None
    }
    inventory_node_ids = {
        int(row["node_id"])
        for row in sql_snapshot.get("inventory", [])
        if row.get("node_id") is not None
    }
    charger_node_ids = {
        int(row["node_id"])
        for row in chargers
        if row.get("node_id") is not None
    }
    postgres_map_node_ids = {
        int(row["node_id"])
        for row in backend_map_nodes
        if row.get("node_id") is not None
    }
    outbound_node_ids = {
        int(row["node_id"])
        for row in backend_map_nodes
        if str(row.get("backend_node_type") or "").upper() == "OUTBOUND"
    }
    charging_slot_node_ids = {
        int(row["node_id"])
        for row in backend_map_nodes
        if str(row.get("backend_node_type") or "").upper()
        == "CHARGING_SLOT"
    }

    mismatches = {
        "postgres_map_node_ids_missing_in_neo4j": sorted(
            postgres_map_node_ids - graph_node_ids
        ),
        "robot_node_ids_missing_in_neo4j": sorted(
            robot_node_ids - graph_node_ids
        ),
        "inventory_node_ids_missing_in_neo4j": sorted(
            inventory_node_ids - graph_node_ids
        ),
        "outbound_node_ids_missing_in_neo4j": sorted(
            outbound_node_ids - graph_node_ids
        ),
        "charging_slot_node_ids_missing_in_neo4j": sorted(
            charging_slot_node_ids - graph_node_ids
        ),
        "charging_slot_node_ids_missing_charger_metadata": sorted(
            charging_slot_node_ids - charger_node_ids
        ),
        "postgres_robot_ids_missing_in_redis": sorted(
            postgres_robot_ids - redis_robot_ids
        ),
        "redis_robot_ids_missing_in_postgres": sorted(
            redis_robot_ids - postgres_robot_ids
        ),
    }
    return {
        "warehouse_id": warehouse_id,
        "counts": {
            "postgres_robots": len(sql_snapshot.get("robots", [])),
            "postgres_inventory_rows": len(sql_snapshot.get("inventory", [])),
            "postgres_map_nodes": len(backend_map_nodes),
            "neo4j_nodes": len(graph_snapshot.get("nodes", [])),
            "neo4j_edges": len(graph_snapshot.get("edges", [])),
            "neo4j_chargers": len(chargers),
            "redis_robots": len(redis_snapshot.get("robots", [])),
        },
        "mismatches": mismatches,
        "id_consistency": (
            "PASS"
            if all(not values for values in mismatches.values())
            else "FAIL"
        ),
    }


def _print_report(report: dict[str, Any]) -> None:
    counts = report["counts"]
    print(f"PostgreSQL robots: {counts['postgres_robots']}")
    print(f"PostgreSQL inventory rows: {counts['postgres_inventory_rows']}")
    print(f"PostgreSQL map nodes: {counts['postgres_map_nodes']}")
    print(f"Neo4j nodes: {counts['neo4j_nodes']}")
    print(f"Neo4j edges: {counts['neo4j_edges']}")
    print(f"Neo4j chargers: {counts['neo4j_chargers']}")
    print(f"Redis robots: {counts['redis_robots']}")
    print(f"ID consistency: {report['id_consistency']}")
    if report["id_consistency"] == "FAIL":
        print(
            json.dumps(
                {
                    "warehouse_id": report["warehouse_id"],
                    "mismatches": report["mismatches"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "backend_laro PostgreSQL, Neo4j, Redis ID를 변경 없이 검사합니다."
        )
    )
    parser.add_argument("--warehouse-id", type=int, default=1)
    args = parser.parse_args()

    settings = get_settings()
    missing = settings.missing_for_connections()
    if missing:
        print("Validation unavailable. Missing settings: " + ", ".join(missing))
        return 2

    neo4j: Neo4jRepository | None = None
    redis: RedisRepository | None = None
    try:
        postgres = BackendLaroPostgresAdapter(settings.database_url)
        neo4j = Neo4jRepository(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
            settings.neo4j_database,
        )
        redis = RedisRepository(settings.redis_url)
        sql_snapshot = postgres.snapshot(args.warehouse_id, [])
        backend_map_nodes = postgres.fetch_map_nodes(args.warehouse_id)
        graph_snapshot = neo4j.fetch_topology(args.warehouse_id)
        chargers = neo4j.list_chargers(args.warehouse_id)
        redis_snapshot = redis.live_snapshot(args.warehouse_id)
        report = build_consistency_report(
            warehouse_id=args.warehouse_id,
            sql_snapshot=sql_snapshot,
            backend_map_nodes=backend_map_nodes,
            graph_snapshot=graph_snapshot,
            chargers=chargers,
            redis_snapshot=redis_snapshot,
        )
        _print_report(report)
        return 0 if report["id_consistency"] == "PASS" else 1
    except Exception:
        # Connection strings and database exception details can contain
        # infrastructure metadata, so this validation CLI emits a stable error.
        print(
            "Validation failed while reading backend_laro data sources. "
            "No data was modified."
        )
        return 2
    finally:
        if neo4j is not None:
            neo4j.close()
        if redis is not None:
            redis.client.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Seed P16.5.9 shared resource capacities on existing Neo4j map nodes.

Run once after the P16.5.8 waiting-area seed:
    python -m scripts.seed_p16_5_9_resource_capacities --warehouse-id 2
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.repositories.neo4j import Neo4jRepository


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse-id", type=int, default=2)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.neo4j_uri or not settings.neo4j_password:
        raise RuntimeError(".env의 NEO4J_URI와 NEO4J_PASSWORD가 필요합니다.")

    resources = json.loads(
        (ROOT / "examples/p16_5_9_resource_capacities.json").read_text(
            encoding="utf-8"
        )
    )
    repository = Neo4jRepository(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        settings.neo4j_database,
    )
    try:
        result = repository.upsert_resource_capacities(
            args.warehouse_id,
            resources,
        )
        topology = repository.fetch_topology(args.warehouse_id)
        requested_ids = {int(row["node_id"]) for row in resources}
        seeded = [
            row
            for row in topology["nodes"]
            if int(row["node_id"]) in requested_ids
        ]
        found_ids = {int(row["node_id"]) for row in seeded}
        missing_ids = sorted(requested_ids - found_ids)
        print(
            json.dumps(
                {
                    "warehouse_id": args.warehouse_id,
                    "updated": result,
                    "shared_resources": seeded,
                    "missing_node_ids": missing_ids,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if missing_ids:
            raise SystemExit(2)
    finally:
        repository.close()


if __name__ == "__main__":
    main()

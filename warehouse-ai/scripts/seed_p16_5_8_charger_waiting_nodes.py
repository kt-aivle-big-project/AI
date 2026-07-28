"""Upgrade P16.5.7 parking bays to P16.5.8 charger waiting areas.

Run once before the Swagger P16.5.8 test:
    python -m scripts.seed_p16_5_8_charger_waiting_nodes --warehouse-id 2
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

    nodes = json.loads(
        (ROOT / "examples/p16_5_8_charger_waiting_nodes.json").read_text(
            encoding="utf-8"
        )
    )
    edges = json.loads(
        (ROOT / "examples/p16_5_8_charger_waiting_edges.json").read_text(
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
        result = repository.upsert_idle_nodes(args.warehouse_id, nodes, edges)
        topology = repository.fetch_topology(args.warehouse_id)
        seeded = [
            row
            for row in topology["nodes"]
            if int(row["node_id"]) in {2160, 2161, 2162}
        ]
        print(
            json.dumps(
                {
                    "warehouse_id": args.warehouse_id,
                    "upserted": result,
                    "charger_waiting_areas": seeded,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()

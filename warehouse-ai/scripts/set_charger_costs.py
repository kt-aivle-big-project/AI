from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.repositories.neo4j import Neo4jRepository


def parse_cost(value: str) -> tuple[int, float]:
    try:
        node_text, cost_text = value.split("=", 1)
        node_id = int(node_text)
        cost = float(cost_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "--cost 값은 NODE_ID=COST 형식이어야 합니다. 예: 2152=1.5"
        ) from exc
    if cost < 0:
        raise argparse.ArgumentTypeError("충전 비용은 0 이상이어야 합니다.")
    return node_id, cost


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Neo4j active CHARGER 노드에 명시적으로 charging_cost를 설정합니다. "
            "실제 단위와 기준은 프로젝트 정책에서 동일하게 적용해야 합니다."
        )
    )
    parser.add_argument("--warehouse-id", type=int, required=True)
    parser.add_argument(
        "--cost",
        action="append",
        type=parse_cost,
        required=True,
        help="NODE_ID=COST. 여러 노드는 --cost를 반복 입력합니다.",
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.neo4j_uri or not settings.neo4j_password:
        raise RuntimeError(".env에 NEO4J_URI와 NEO4J_PASSWORD가 필요합니다.")

    repository = Neo4jRepository(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        settings.neo4j_database,
    )
    try:
        requested = dict(args.cost)
        updated = repository.set_charger_costs(args.warehouse_id, requested)
        updated_ids = {int(row["node_id"]) for row in updated}
        missing_ids = sorted(set(requested) - updated_ids)
        result = {
            "warehouse_id": args.warehouse_id,
            "updated": updated,
            "missing_or_inactive_charger_node_ids": missing_ids,
            "chargers": repository.list_chargers(args.warehouse_id),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if missing_ids:
            raise SystemExit(2)
    finally:
        repository.close()


if __name__ == "__main__":
    main()

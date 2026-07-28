import json
from pathlib import Path
from typing import Any

from app.services.container import get_services


def load_json_list(path: str | Path) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"JSON 최상위 값은 배열이어야 합니다: {path}")
    return value


def upload_map(
    warehouse_id: int,
    warehouse_name: str,
    node_file: str | Path,
    edge_file: str | Path,
) -> dict[str, int]:
    nodes = load_json_list(node_file)
    edges = load_json_list(edge_file)
    if not nodes or not edges:
        raise ValueError("노드와 간선 JSON에 실제 지도 데이터를 넣으세요.")
    return get_services().neo4j.upsert_map(
        warehouse_id,
        warehouse_name,
        nodes,
        edges,
    )


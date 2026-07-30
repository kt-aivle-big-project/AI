"""Validate supplied graph, populated rack inventory, and scenario references."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.repositories.json_repository import JsonWarehouseRepository


def main() -> None:
    """Load all repository contracts and print a compact summary."""

    repository = JsonWarehouseRepository()
    print(
        {
            "nodes": len(repository.nodes),
            "edges": len(repository.edges),
            "robots": len(repository.robots),
            "orders": len(repository.orders),
            "occupied_rack_levels": repository.inventory["summary"]["occupied_level_count"],
            "versions": repository.versions,
        }
    )


if __name__ == "__main__":
    main()

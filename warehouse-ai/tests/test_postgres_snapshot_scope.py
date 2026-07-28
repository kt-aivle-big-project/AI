from app.repositories.postgres import PostgresRepository


class FakePostgresRepository:
    def __init__(self) -> None:
        self.item_scopes: dict[str, list[str]] = {}

    def fetch_open_works(self, warehouse_id: int):
        assert warehouse_id == 2
        return [
            {
                "work_id": "DEMO-W-OUT-2-F",
                "item_id": "F",
                "operation_type": "OUTBOUND",
            }
        ]

    def fetch_inventory(self, warehouse_id: int, item_ids: list[str]):
        assert warehouse_id == 2
        self.item_scopes["inventory"] = item_ids
        return []

    def fetch_inventory_items(self, item_ids: list[str]):
        self.item_scopes["inventory_items"] = item_ids
        return []

    def fetch_inbound_orders(self, warehouse_id: int, item_ids: list[str]):
        assert warehouse_id == 2
        self.item_scopes["inbound_orders"] = item_ids
        return []

    def fetch_outbound_orders(self, warehouse_id: int, item_ids: list[str]):
        assert warehouse_id == 2
        self.item_scopes["outbound_orders"] = item_ids
        return []

    def fetch_storage_capacity(self, warehouse_id: int):
        return None

    def fetch_robots(self, warehouse_id: int):
        return []

    def fetch_work_dependencies(self, warehouse_id: int):
        return []

    def fetch_work_schedule_constraints(self, warehouse_id: int):
        return []


def test_snapshot_expands_item_scope_with_open_work_items() -> None:
    repository = FakePostgresRepository()

    snapshot = PostgresRepository.snapshot(repository, 2, ["E"])

    assert snapshot["works"][0]["item_id"] == "F"
    assert repository.item_scopes == {
        "inventory": ["E", "F"],
        "inventory_items": ["E", "F"],
        "inbound_orders": ["E", "F"],
        "outbound_orders": ["E", "F"],
    }

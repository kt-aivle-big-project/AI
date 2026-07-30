"""Offline contracts for v13.14 logical-destination goods-to-person cycles."""
from __future__ import annotations

from pathlib import Path

from app.domain.schemas import GoodsToPersonPlanRequest
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.goods_to_person_service import GoodsToPersonPlanningService

ROOT = Path(__file__).resolve().parents[1]
RETURN_FIXTURE = ROOT / "scenarios" / "fixtures" / "V13_goods_to_person_bearing_wave_return"
DEPLETED_FIXTURE = ROOT / "scenarios" / "fixtures" / "V13_goods_to_person_bearing_wave_depleted"
MULTI_HU_FIXTURE = ROOT / "scenarios" / "fixtures" / "V14_goods_to_person_multi_hu"
ORDER_IDS = [f"ORD-G2P-{index:03d}" for index in range(1, 6)]


def _plan(fixture: Path):
    return GoodsToPersonPlanningService(JsonWarehouseRepository(fixture)).plan(
        GoodsToPersonPlanRequest(
            simulation_id="SIM-G2P-TEST",
            order_ids=ORDER_IDS,
            optimization_backend="cuopt_payload_only",
        )
    )


def test_five_same_item_orders_create_one_cycle_and_preserve_logical_destinations() -> None:
    result = _plan(RETURN_FIXTURE)
    assert result.status == "ready_for_optimizer", result.errors
    assert len(result.batches) == 1
    assert len(result.optimizer_payloads) == 1

    batch = result.batches[0]
    assert batch.item_id == "ITEM_BEARING"
    assert batch.order_ids == ORDER_IDS
    assert batch.requested_quantity == 8
    assert batch.quantity_before == 12
    assert batch.quantity_after == 4
    assert batch.return_required
    assert batch.disposition == "RETURN_TO_HOME"
    assert batch.post_station_action == "RETURN_TO_SOURCE"
    assert batch.post_station_node == batch.source_access_node
    assert batch.mobile_robot_id is None  # payload-only mode has not solved the vehicle assignment
    assert set(batch.logical_destination_ids) == {"O_A", "O_B", "O_C", "O_D"}
    assert {value.logical_destination_id for value in batch.allocations} == {
        "O_A", "O_B", "O_C", "O_D"
    }
    assert batch.station_processing_ticks == batch.requested_quantity
    assert batch.station_sort_time_ms == batch.requested_quantity * 100
    assert {value.action for value in result.station_actions} == {
        "RECEIVE_HANDLING_UNIT",
        "SORT_TO_DESTINATIONS",
        "RELEASE_REMAINDER",
    }
    assert result.inventory_mutation_previews[0].next_status == "returning"

    payload = result.optimizer_payloads[0]
    end_nodes = {
        node_id
        for node_id, node_index in payload.location_index_map.items()
        if node_index in payload.fleet_data.vehicle_end_locations
    }
    assert end_nodes == {batch.source_access_node}
    assert payload.fleet_data.drop_return_trips == [False] * len(
        payload.fleet_data.vehicle_ids
    )


def test_depleted_handling_unit_moves_to_empty_tote_buffer() -> None:
    result = _plan(DEPLETED_FIXTURE)
    assert result.status == "ready_for_optimizer", result.errors
    assert len(result.batches) == 1
    batch = result.batches[0]
    assert batch.requested_quantity == 12
    assert batch.quantity_after == 0
    assert not batch.return_required
    assert batch.disposition == "MOVE_TO_EMPTY_TOTE_BUFFER"
    assert batch.post_station_action == "MOVE_TO_EMPTY_TOTE_BUFFER"
    assert batch.empty_tote_buffer_id == "EMPTY_TOTE_BUFFER_1"
    assert batch.post_station_node == "EMPTY_TOTE_BUFFER_1_ACCESS"
    assert {value.action for value in result.station_actions} == {
        "RECEIVE_HANDLING_UNIT",
        "SORT_TO_DESTINATIONS",
        "RELEASE_EMPTY_TOTE",
    }
    assert result.inventory_mutation_previews[0].next_status == "empty_in_transit"

    payload = result.optimizer_payloads[0]
    end_nodes = {
        node_id
        for node_id, node_index in payload.location_index_map.items()
        if node_index in payload.fleet_data.vehicle_end_locations
    }
    assert end_nodes == {"EMPTY_TOTE_BUFFER_1_ACCESS"}


def test_inventory_split_across_two_handling_units_creates_two_cycles() -> None:
    result = _plan(MULTI_HU_FIXTURE)
    assert result.status == "ready_for_optimizer", result.errors
    assert len(result.batches) == 2
    assert len(result.optimizer_payloads) == 2
    assert {value.handling_unit_id for value in result.batches} == {
        "HU-K1_7-L1-ITEM_BEARING",
        "HU-K2_7-L2-ITEM_BEARING",
    }
    assert sum(value.requested_quantity for value in result.batches) == 15
    assert all(value.quantity_after == 0 for value in result.batches)
    assert all(value.post_station_node == "EMPTY_TOTE_BUFFER_1_ACCESS" for value in result.batches)

    # One order can be split across physical handling-unit cycles, but the
    # committed sum must still equal its original required quantity.
    allocated_by_order: dict[str, int] = {}
    for batch in result.batches:
        for allocation in batch.allocations:
            allocated_by_order[allocation.order_id] = (
                allocated_by_order.get(allocation.order_id, 0) + allocation.quantity
            )
    assert allocated_by_order == {order_id: 3 for order_id in ORDER_IDS}


def test_both_stations_can_serve_every_logical_outbound_destination() -> None:
    repository = JsonWarehouseRepository(RETURN_FIXTURE)
    destinations = set(repository.outbound_chutes)
    assert destinations == {f"O_{value}" for value in "ABCDEFG"}
    stations = repository.outbound_station_candidates(sorted(destinations))
    assert {value["station_id"] for value in stations} == {
        "OUT_STATION_1",
        "OUT_STATION_2",
    }
    assert all(set(value["served_chute_ids"]) == destinations for value in stations)

"""Docker-free local persistence contracts for v13.14."""
from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.domain.schemas import GoodsToPersonPlanRequest
from app.infrastructure.manager import get_infrastructure_manager
from app.repositories.json_repository import get_repository
from app.services.goods_to_person_service import GoodsToPersonPlanningService

ROOT = Path(__file__).resolve().parents[1]
RETURN_FIXTURE = ROOT / "scenarios" / "fixtures" / "V13_goods_to_person_bearing_wave_return"
ORDER_IDS = [f"ORD-G2P-{index:03d}" for index in range(1, 6)]


def _reset_caches() -> None:
    get_repository.cache_clear()
    get_infrastructure_manager.cache_clear()
    get_settings.cache_clear()


def test_embedded_three_database_contract_bootstrap_roundtrip_and_g2p(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WAREHOUSE_REPOSITORY_BACKEND", "embedded")
    monkeypatch.setenv("LOCAL_DB_DIR", str(tmp_path / "local-db"))
    monkeypatch.setenv("INFRASTRUCTURE_STRICT_STARTUP", "true")
    monkeypatch.setenv("RUNTIME_SIMULATION_ID", "SIM-G2P-RETURN")
    monkeypatch.setenv("FRONTEND_EXPLANATION_MODE", "deterministic")
    _reset_caches()

    try:
        manager = get_infrastructure_manager()
        startup = manager.start()
        assert startup["status"] == "ok"
        assert {
            value["result"]["engine"]
            for value in startup["components"].values()
        } == {
            "sqlite-embedded-postgres",
            "sqlite-embedded-redis",
            "sqlite-embedded-neo4j",
        }

        seeded = manager.bootstrap_from_json(RETURN_FIXTURE)
        assert seeded["status"] == "seeded"
        assert seeded["postgres"]["racks"] == 48
        assert seeded["postgres"]["orders"] == 5
        assert seeded["postgres"]["empty_tote_buffers"] == 1
        assert seeded["neo4j"]["node_count"] == 220
        assert seeded["neo4j"]["edge_count"] == 356
        assert seeded["neo4j"]["empty_tote_buffer_access_nodes"] == 1

        roundtrip = manager.roundtrip("RT-EMBEDDED-V14")
        assert roundtrip["status"] == "pass"
        assert all(value["ok"] for value in roundtrip["components"].values())

        get_repository.cache_clear()
        repository = get_repository()
        assert len(repository.nodes) == 220
        assert len(repository.edges) == 356
        assert repository.get_order("ORD-G2P-001")["logical_destination_id"] == "O_A"
        assert repository.station_runtime("SIM-G2P-RETURN")

        result = GoodsToPersonPlanningService(repository).plan(
            GoodsToPersonPlanRequest(
                simulation_id="SIM-G2P-RETURN",
                order_ids=ORDER_IDS,
                optimization_backend="cuopt_payload_only",
            )
        )
        assert result.status == "ready_for_optimizer", result.errors
        assert len(result.batches) == 1
        planned = result.batches[0].model_copy(update={"mobile_robot_id": "R002"})

        reservation_id = manager.postgres.create_batch_reservation(
            batch={**planned.model_dump(mode="json"), "simulation_id": "SIM-G2P-RETURN"},
            allocations=[value.model_dump(mode="json") for value in planned.allocations],
            expected_version=planned.handling_unit_version,
        )
        assert reservation_id == f"RES-{planned.batch_id}"

        committed = manager.postgres.commit_station_pick(batch_id=planned.batch_id)
        assert committed["handling_unit_status"] == "returning"
        assert committed["batch_status"] == "returning"
        assert all(manager.postgres.get_order(order_id)["status"] == "fulfilled" for order_id in ORDER_IDS)

        completed = manager.postgres.complete_post_station_move(
            batch_id=planned.batch_id,
            robot_id="R002",
        )
        assert completed["handling_unit_status"] == "stored"
        assert completed["batch_status"] == "completed"
    finally:
        try:
            get_infrastructure_manager().close()
        except Exception:
            pass
        _reset_caches()

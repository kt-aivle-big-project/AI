from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import api as api_module


class FakeCommandRepository:
    def __init__(self) -> None:
        self.filters = None
        self.history = {
            "command_id": "C-1",
            "warehouse_id": 1,
            "status": "SUCCESS",
            "simulation_id": "SIM-1",
            "plan_version": "PLAN-1",
            "result_summary": {"status": "SIMULATION_SUCCESS"},
            "error_summary": None,
        }
        self.stages = [
            {
                "sequence": 1,
                "attempt": 1,
                "node_name": "COMMAND_RECEIVED",
                "status": "SUCCESS",
            }
        ]
        self.plan_evidence = {
            "plan_version": "PLAN-1",
            "output_payload": {
                "optimization_evidence": [
                    {
                        "task_id": "T-1",
                        "candidate_count": 1,
                        "selected_robot_id": "R-1",
                        "candidates": [{"robot_id": "R-1", "selected": True}],
                    }
                ],
                "objective_breakdown": {"total": 3.0},
                "routing_evidence": {"route_segment_count": 2},
                "reservation_evidence": {"vertex_reservation_count": 3},
                "distance_comparison": {"difference": 0.0},
                "verification_evidence": [{"evidence_id": "E-1"}],
            },
        }

    def list_command_history(self, **filters):
        self.filters = filters
        return [self.history]

    def get_command_history(self, command_id: str):
        return self.history if command_id == "C-1" else None

    def list_planning_stage_logs(self, command_id: str):
        assert command_id == "C-1"
        return self.stages

    def get_latest_command_plan_evidence(self, command_id: str):
        assert command_id == "C-1"
        return self.plan_evidence


def install_repository(monkeypatch) -> FakeCommandRepository:
    repository = FakeCommandRepository()
    monkeypatch.setattr(
        api_module,
        "get_services",
        lambda: SimpleNamespace(postgres=repository),
    )
    return repository


def test_list_commands_uses_filters_and_pagination(monkeypatch) -> None:
    repository = install_repository(monkeypatch)
    client = TestClient(api_module.app)

    response = client.get(
        "/v1/commands",
        params={
            "warehouse_id": 1,
            "status": "SUCCESS",
            "requested_execution_mode": "SIMULATE_ONLY",
            "limit": 20,
            "offset": 5,
        },
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert repository.filters["warehouse_id"] == 1
    assert repository.filters["status"] == "SUCCESS"
    assert repository.filters["limit"] == 20
    assert repository.filters["offset"] == 5


def test_get_command_and_stages(monkeypatch) -> None:
    install_repository(monkeypatch)
    client = TestClient(api_module.app)

    command = client.get("/v1/commands/C-1")
    stages = client.get("/v1/commands/C-1/stages")

    assert command.status_code == 200
    assert command.json()["simulation_id"] == "SIM-1"
    assert command.json()["plan_version"] == "PLAN-1"
    assert stages.status_code == 200
    assert stages.json()["stages"][0]["node_name"] == "COMMAND_RECEIVED"


def test_get_missing_command_returns_404(monkeypatch) -> None:
    install_repository(monkeypatch)
    client = TestClient(api_module.app)

    response = client.get("/v1/commands/missing")

    assert response.status_code == 404


def test_list_commands_rejects_limit_over_200(monkeypatch) -> None:
    install_repository(monkeypatch)
    client = TestClient(api_module.app)

    response = client.get("/v1/commands", params={"limit": 201})

    assert response.status_code == 422


def test_get_plan_evidence_defaults_to_compact_candidates(monkeypatch) -> None:
    install_repository(monkeypatch)
    client = TestClient(api_module.app)

    response = client.get("/v1/commands/C-1/plan-evidence")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "AVAILABLE"
    assert "candidates" not in payload["optimization_evidence"][0]
    assert payload["routing_evidence"]["route_segment_count"] == 2
    assert payload["reservation_evidence"]["vertex_reservation_count"] == 3


def test_get_plan_evidence_include_options(monkeypatch) -> None:
    install_repository(monkeypatch)
    client = TestClient(api_module.app)

    response = client.get(
        "/v1/commands/C-1/plan-evidence",
        params={
            "include_candidates": "true",
            "include_routes": "false",
            "include_reservations": "false",
        },
    )

    payload = response.json()
    assert payload["optimization_evidence"][0]["candidates"][0]["robot_id"] == "R-1"
    assert payload["routing_evidence"] == {}
    assert payload["reservation_evidence"] == {}


def test_get_plan_evidence_returns_no_evidence_for_query_or_reset(monkeypatch) -> None:
    repository = install_repository(monkeypatch)
    repository.plan_evidence = None
    client = TestClient(api_module.app)

    response = client.get("/v1/commands/C-1/plan-evidence")

    assert response.status_code == 200
    assert response.json() == {
        "status": "NO_PLAN_EVIDENCE",
        "command_id": "C-1",
    }


def test_get_plan_evidence_missing_command_returns_404(monkeypatch) -> None:
    install_repository(monkeypatch)
    client = TestClient(api_module.app)

    response = client.get("/v1/commands/missing/plan-evidence")

    assert response.status_code == 404

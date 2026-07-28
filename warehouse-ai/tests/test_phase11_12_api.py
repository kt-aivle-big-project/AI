from fastapi.testclient import TestClient

import app.api as api_module


class Postgres:
    def get_scenario_comparison(self, comparison_id):
        return {"comparison_id": comparison_id, "status": "COMPLETED"}

    def get_scenario_comparison_run(self, comparison_id, scenario_id):
        return {
            "comparison_id": comparison_id,
            "scenario_id": scenario_id,
            "result_summary": {"valid": True},
        }

    def list_scenario_comparisons(self, **_filters):
        return [{"comparison_id": "CMP-1", "status": "COMPLETED"}]

    def get_execution_event_processing(self, event_id):
        return {"event_id": event_id, "status": "APPROVAL_REQUIRED"}

    def get_automatic_replan_request(self, request_id):
        return {"request_id": request_id, "status": "APPROVAL_REQUIRED"}

    def list_automatic_replan_requests(self, warehouse_id, **_filters):
        return [{"request_id": "REQ-1", "warehouse_id": warehouse_id}]


class Services:
    postgres = Postgres()


client = TestClient(api_module.app)


def test_scenario_comparison_query_apis(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "get_services", lambda: Services())
    listed = client.get("/v1/scenario-comparisons?warehouse_id=1")
    detail = client.get("/v1/scenario-comparisons/CMP-1")
    scenario = client.get("/v1/scenario-comparisons/CMP-1/scenarios/scenario-1")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert detail.json()["comparison_id"] == "CMP-1"
    assert scenario.json()["scenario_id"] == "scenario-1"


def test_event_and_replan_query_apis(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "get_services", lambda: Services())
    event = client.get("/v1/execution/events/EVENT-1")
    replan = client.get("/v1/event-replans/REQ-1")
    listed = client.get("/v1/warehouses/1/event-replans")
    assert event.json()["event_id"] == "EVENT-1"
    assert replan.json()["request_id"] == "REQ-1"
    assert listed.json()["count"] == 1


def test_create_comparison_uses_simulate_only_service(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "get_services", lambda: Services())

    def execute(_self, request):
        assert request.warehouse_id == 1
        return {
            "comparison_id": "CMP-1",
            "status": "COMPLETED",
            "scenarios": [],
        }

    monkeypatch.setattr(api_module.ScenarioComparisonService, "execute", execute)
    response = client.post(
        "/v1/scenario-comparisons",
        json={"warehouse_id": 1, "text": "로봇 2대와 3대를 비교해줘"},
    )
    assert response.status_code == 200
    assert response.json()["comparison_id"] == "CMP-1"


def test_approve_and_reject_apis(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "get_services", lambda: Services())
    monkeypatch.setattr(
        api_module.EventReplanService,
        "approve",
        lambda _self, request_id, _decision: {
            "request_id": request_id,
            "status": "EXECUTED",
        },
    )
    monkeypatch.setattr(
        api_module.EventReplanService,
        "reject",
        lambda _self, request_id, _decision: {
            "request_id": request_id,
            "status": "REJECTED",
        },
    )
    approved = client.post(
        "/v1/event-replans/REQ-1/approve",
        json={"reason": "운영자 확인"},
    )
    rejected = client.post(
        "/v1/event-replans/REQ-2/reject",
        json={"reason": "수동 처리"},
    )
    assert approved.json()["status"] == "EXECUTED"
    assert rejected.json()["status"] == "REJECTED"

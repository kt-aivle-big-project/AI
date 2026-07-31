from __future__ import annotations

from types import SimpleNamespace

from app.services import native_plan_diagnostics_service as module


class _FakePostgres:
    def count_summary(self, warehouse_id: str):
        return {
            "racks": 48,
            "handling_units": 8,
            "orders": 5,
            "inbound_receipts": 2,
            "outbound_stations": 2,
            "empty_tote_buffers": 1,
        }

    def load_orders(self, warehouse_id: str):
        return [{"order_id": "ORD-001"}]

    def load_inbound_receipts(self, warehouse_id: str):
        return [{"inbound_id": "IN-001"}]

    def versions(self, warehouse_id: str):
        return {"inventory_version": "INV-1"}


class _FakeRedis:
    def all_robots(self, warehouse_id: str, simulation_id: str):
        return [{"robot_id": "R002"}, {"robot_id": "R003"}]

    def edge_runtime(self, warehouse_id: str, simulation_id: str):
        return []

    def station_runtime(self, warehouse_id: str, simulation_id: str):
        return [{"station_id": "OUT_STATION_1"}]

    def existing_reservations(self, warehouse_id: str, simulation_id: str):
        return []

    def runtime_version(self, warehouse_id: str, simulation_id: str):
        return "7"


class _FakeNeo4j:
    def fetch_route_graph(self, warehouse_id: str):
        return SimpleNamespace(
            summary={"node_count": 220, "edge_count": 356},
            nodes=[{"id": "R0_0"}],
            edges=[{"id": "H0"}],
            version="MAP-1",
        )


def test_native_plan_preflight_reports_all_three_stores(monkeypatch) -> None:
    manager = SimpleNamespace(
        postgres=_FakePostgres(), redis=_FakeRedis(), neo4j=_FakeNeo4j()
    )
    monkeypatch.setattr(module, "get_infrastructure_manager", lambda: manager)
    service = module.NativePlanDiagnosticsService()

    value = service.preflight("WH-001", "SIM-V18-MIXED")

    assert value["ready"] is True
    assert value["status"] == "READY"
    assert value["postgres"]["counts"]["orders"] == 5
    assert value["redis"]["robot_count"] == 2
    assert value["neo4j"]["node_count"] == 220
    assert value["neo4j"]["edge_count"] == 356
    assert value["problems"] == []


def test_native_plan_trace_compacts_node_and_validation_evidence(monkeypatch) -> None:
    plan = SimpleNamespace(
        plan_id="PLAN-1",
        plan_version=1,
        warehouse_id="WH-001",
        simulation_id="SIM-V18-MIXED",
        status="READY",
        robots=[SimpleNamespace(steps=[1, 2, 3])],
        logical_operations=[1, 2],
        station_reservations=[1],
        makespan_ms=1000,
        absolute_finish_at_ms=1000,
    )
    node = SimpleNamespace(
        node_name="optimizer",
        status="success",
        duration_ms=12.5,
        llm_used=False,
        error_code=None,
    )
    valid = SimpleNamespace(valid=True)
    optimizer = SimpleNamespace(
        backend="ortools",
        status="success",
        optimizer="ortools-routing",
        routes=[1, 2],
        unassigned_task_ids=[],
        estimated_makespan_ms=900.0,
    )
    result = SimpleNamespace(
        status="plan_validated",
        workflow_trace=["optimizer", "prioritized_mapf_planner"],
        node_execution_log=[node],
        structured_key_validation=valid,
        cuopt_dynamic_input_validation=valid,
        payload_validation=valid,
        candidate_space_validation=valid,
        optimizer_assignment_validation=valid,
        route_validation=valid,
        mapf_validation=valid,
        logical_operation_coverage_validation=valid,
        context_snapshot=SimpleNamespace(
            repository_type="LiveWarehouseRepository",
            source_manifest={
                "route_nodes": "neo4j_snapshot",
                "racks": "postgres_snapshot",
                "robots": "redis_live",
            },
            graph_version="MAP-1",
            inventory_version="INV-1",
            runtime_version="RT-1",
        ),
        optimizer_result=optimizer,
        orchestration_plan=SimpleNamespace(formulation_route="RULE_FORMULATION"),
        optimization_backend="ortools",
        errors=[],
    )

    monkeypatch.setattr(
        module.SimulationPlanStore,
        "load",
        lambda self, plan_id: (plan, result),
    )
    service = module.NativePlanDiagnosticsService()
    value = service.trace("PLAN-1")

    assert value["workflow_status"] == "plan_validated"
    assert value["checks"]["mapf_valid"] is True
    assert value["checks"]["logical_operation_coverage_valid"] is True
    assert value["repository"]["repository_type"] == "LiveWarehouseRepository"
    assert value["repository"]["source_manifest"]["route_nodes"] == "neo4j_snapshot"
    assert value["optimizer"]["route_count"] == 2
    assert value["plan_summary"]["step_count"] == 3

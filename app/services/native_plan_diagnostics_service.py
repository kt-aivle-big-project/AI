"""Diagnostics for the native LARO plan API.

The Spring compatibility endpoints (``/optimize`` and ``/reoptimize``) use a
numeric-ID contract.  The native plan API uses LARO's warehouse-scoped
PostgreSQL/Redis/Neo4j contracts.  This module makes the distinction visible and
provides a small, read-only preflight/trace surface for integration checks.
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.domain.schemas import normalize_warehouse_id
from app.infrastructure.manager import get_infrastructure_manager
from app.services.simulation_plan_service import SimulationPlanStore


class NativePlanDiagnosticsService:
    """Read-only health and trace summaries for the native mission planner."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.manager = get_infrastructure_manager()

    @staticmethod
    def _error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"

    def preflight(self, warehouse_id: str, simulation_id: str) -> dict[str, Any]:
        """Verify all data required by ``POST .../missions/plan``.

        This check never creates a plan and never mutates business/runtime data.
        It verifies the native LARO PostgreSQL records, the selected Redis
        simulation namespace, and the Neo4j ``RouteNode/TRAVERSES`` projection.
        """

        wid = normalize_warehouse_id(warehouse_id)
        sid = str(simulation_id).strip()
        if not sid:
            raise ValueError("simulation_id must not be blank")

        problems: list[str] = []

        postgres: dict[str, Any] = {"ok": False}
        try:
            counts = self.manager.postgres.count_summary(wid)
            orders = self.manager.postgres.load_orders(wid)
            receipts = self.manager.postgres.load_inbound_receipts(wid)
            versions = self.manager.postgres.versions(wid)
            postgres = {
                "ok": True,
                "counts": counts,
                "order_ids": [str(value["order_id"]) for value in orders],
                "inbound_ids": [str(value["inbound_id"]) for value in receipts],
                "versions": versions,
            }
            if counts.get("racks", 0) <= 0:
                problems.append("PostgreSQL native rack inventory is empty.")
            if counts.get("handling_units", 0) <= 0:
                problems.append("PostgreSQL native handling-unit inventory is empty.")
            if counts.get("outbound_stations", 0) <= 0:
                problems.append("PostgreSQL native outbound-station resources are empty.")
            if not orders and not receipts:
                problems.append("PostgreSQL contains no native outbound order or inbound receipt.")
        except Exception as exc:
            postgres = {"ok": False, "error": self._error(exc)}
            problems.append("PostgreSQL native planning contract is unavailable.")

        redis: dict[str, Any] = {"ok": False}
        try:
            robots = self.manager.redis.all_robots(wid, sid)
            edges = self.manager.redis.edge_runtime(wid, sid)
            stations = self.manager.redis.station_runtime(wid, sid)
            reservations = self.manager.redis.existing_reservations(wid, sid)
            runtime_version = self.manager.redis.runtime_version(wid, sid)
            redis = {
                "ok": True,
                "robot_count": len(robots),
                "robot_ids": [str(value.get("robot_id")) for value in robots],
                "edge_runtime_count": len(edges),
                "station_runtime_count": len(stations),
                "reservation_count": len(reservations),
                "runtime_version": str(runtime_version),
            }
            if not robots:
                problems.append(
                    f"Redis contains no native robot runtime for {wid}/{sid}."
                )
        except Exception as exc:
            redis = {"ok": False, "error": self._error(exc)}
            problems.append("Redis native planning runtime is unavailable.")

        neo4j: dict[str, Any] = {"ok": False}
        try:
            snapshot = self.manager.neo4j.fetch_route_graph(wid)
            node_count = int(snapshot.summary.get("node_count", len(snapshot.nodes)))
            edge_count = int(snapshot.summary.get("edge_count", len(snapshot.edges)))
            neo4j = {
                "ok": True,
                "node_count": node_count,
                "edge_count": edge_count,
                "graph_version": snapshot.version,
                "node_label": "RouteNode",
                "relationship_type": "TRAVERSES",
            }
            if node_count <= 0 or edge_count <= 0:
                problems.append("Neo4j native RouteNode/TRAVERSES projection is empty.")
        except Exception as exc:
            neo4j = {"ok": False, "error": self._error(exc)}
            problems.append("Neo4j native RouteNode/TRAVERSES projection is unavailable.")

        ready = not problems
        return {
            "status": "READY" if ready else "NOT_READY",
            "ready": ready,
            "warehouse_id": wid,
            "simulation_id": sid,
            "repository_backend": self.settings.warehouse_repository_backend,
            "map_repository_backend": self.settings.map_repository_backend,
            "default_planning_mode": self.settings.default_planning_mode,
            "default_optimization_backend": self.settings.optimization_backend,
            "plan_endpoint": f"/api/v1/warehouses/{wid}/missions/plan",
            "postgres": postgres,
            "redis": redis,
            "neo4j": neo4j,
            "problems": problems,
        }

    def trace(self, plan_id: str) -> dict[str, Any]:
        """Return a compact plan-stage summary without returning the full payload."""

        plan, result = SimulationPlanStore().load(plan_id)
        if result is None:
            return {
                "plan_id": plan.plan_id,
                "plan_version": plan.plan_version,
                "warehouse_id": plan.warehouse_id,
                "simulation_id": plan.simulation_id,
                "plan_status": plan.status,
                "workflow_status": None,
                "nodes": [],
                "checks": {},
            }

        nodes = [
            {
                "node_name": value.node_name,
                "status": value.status,
                "duration_ms": value.duration_ms,
                "llm_used": value.llm_used,
                "error_code": value.error_code,
            }
            for value in result.node_execution_log
        ]

        checks = {
            "structured_keys_valid": (
                result.structured_key_validation.valid
                if result.structured_key_validation is not None
                else None
            ),
            "dynamic_input_valid": (
                result.cuopt_dynamic_input_validation.valid
                if result.cuopt_dynamic_input_validation is not None
                else None
            ),
            "payload_valid": (
                result.payload_validation.valid
                if result.payload_validation is not None
                else None
            ),
            "candidate_space_valid": (
                result.candidate_space_validation.valid
                if result.candidate_space_validation is not None
                else None
            ),
            "assignment_valid": (
                result.optimizer_assignment_validation.valid
                if result.optimizer_assignment_validation is not None
                else None
            ),
            "route_valid": (
                result.route_validation.valid
                if result.route_validation is not None
                else None
            ),
            "mapf_valid": (
                result.mapf_validation.valid
                if result.mapf_validation is not None
                else None
            ),
        }

        optimizer = None
        if result.optimizer_result is not None:
            optimizer = {
                "backend": result.optimizer_result.backend,
                "status": result.optimizer_result.status,
                "optimizer": result.optimizer_result.optimizer,
                "route_count": len(result.optimizer_result.routes),
                "unassigned_task_ids": list(result.optimizer_result.unassigned_task_ids),
                "estimated_makespan_ms": result.optimizer_result.estimated_makespan_ms,
            }

        return {
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "warehouse_id": plan.warehouse_id,
            "simulation_id": plan.simulation_id,
            "plan_status": plan.status,
            "workflow_status": result.status,
            "final_route": (
                result.orchestration_plan.formulation_route
                if result.orchestration_plan is not None
                else None
            ),
            "optimization_backend": result.optimization_backend,
            "optimizer": optimizer,
            "checks": checks,
            "workflow_trace": list(result.workflow_trace),
            "nodes": nodes,
            "plan_summary": {
                "robot_count": len(plan.robots),
                "step_count": sum(len(value.steps) for value in plan.robots),
                "logical_operation_count": len(plan.logical_operations),
                "station_reservation_count": len(plan.station_reservations),
                "makespan_ms": plan.makespan_ms,
                "absolute_finish_at_ms": plan.absolute_finish_at_ms,
            },
            "errors": [value.model_dump(mode="json") for value in result.errors],
        }

"""Build a compact, execution-safe context for LLM cuOpt formulation.

The validated :class:`WarehouseSituationGraph` remains the internal authority
for evidence validation and route compilation.  The LLM does not need the raw
graph topology or every node/edge sequence.  This projection exposes the
business candidates once, plus deduplicated route costs and reachability
summaries required to author a ``CuOptDynamicInputDraft``.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.domain.schemas import (
    ContextSnapshot,
    InventoryContext,
    MapContext,
    NormalizedWarehouseRequest,
    RobotRuntimeContext,
    WarehouseSituationGraph,
)


class CuOptLlmPlanningContextBuilder:
    """Project full planning state into a compact LLM-only read model."""

    SCHEMA_VERSION = "cuopt_llm_planning_context_v1"

    def build(
        self,
        *,
        request: NormalizedWarehouseRequest,
        snapshot: ContextSnapshot,
        inventory: InventoryContext,
        robots: RobotRuntimeContext,
        map_context: MapContext,
        graph: WarehouseSituationGraph,
    ) -> dict[str, Any]:
        """Return facts needed for a draft without raw topology or path arrays."""

        inventory_payload = inventory.model_dump(mode="json")
        for inbound_need in inventory_payload.get("inbound_needs", []):
            ea_quantity = int(inbound_need["quantity"])
            box_count = int(inbound_need.get("transport_unit_count") or ea_quantity)
            inbound_need.update(
                {
                    "quantity_unit": "EA",
                    "transport_unit": "BOX",
                    "solver_demand": box_count,
                    "solver_demand_unit": "BOX",
                    "units_per_box": (
                        ea_quantity // box_count
                        if box_count > 0 and ea_quantity % box_count == 0
                        else None
                    ),
                }
            )

        return {
            "schema_version": self.SCHEMA_VERSION,
            "snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "captured_at": snapshot.captured_at,
                "graph_version": snapshot.graph_version,
                "inventory_version": snapshot.inventory_version,
                "runtime_version": snapshot.runtime_version,
            },
            "fulfillment": {
                "mode": graph.fulfillment_mode,
                "g2p_order_ids": list(graph.g2p_order_ids),
                "required_operation_ids": [
                    operation.operation_id
                    for operation in request.operations
                    if operation.operation_type
                    in {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}
                ],
            },
            # InventoryContext is already a scoped read model.  In particular,
            # candidate_putaway_slots is emitted once instead of once per inbound
            # operation, which removes the operation x slot Cartesian duplicate.
            "inventory": inventory_payload,
            "fleet": self._fleet_summary(robots),
            "active_operations": self._active_operation_summary(graph),
            "map": self._map_summary(map_context, graph),
            "pickup_reachability": self._pickup_reachability(graph),
            "task_route_options": self._task_route_options(graph),
            "completeness": graph.completeness.model_dump(mode="json"),
            "instructions": {
                "inbound_transport_contract": (
                    "For INBOUND_ITEM, quantity is physical EA and is not solver "
                    "demand. Copy task.demand from solver_demand, whose unit is BOX. "
                    "Copy item_id and handling_unit_id from the same inbound_need."
                ),
                "evidence_handling": (
                    "Leave task, fleet, and map evidence_ids empty. The deterministic "
                    "evidence compiler fills them from the full validated graph."
                ),
                "route_handling": (
                    "Choose pickup_node and delivery_node only from a matching "
                    "task_route_options entry. Never invent a map identifier."
                ),
            },
        }

    @staticmethod
    def _active_operation_summary(
        graph: WarehouseSituationGraph,
    ) -> list[dict[str, Any]]:
        """Preserve replan/recovery commitments without exposing route history."""

        robot_by_task: dict[str, str] = {}
        for relation in graph.relations:
            if (
                relation.relation_type == "HAS_ACTIVE_TASK"
                and relation.source_node_id.startswith("robot:")
                and relation.target_node_id.startswith("active_task:")
            ):
                robot_by_task[relation.target_node_id] = (
                    relation.source_node_id.removeprefix("robot:")
                )
        return [
            {
                "task_id": str(
                    node.attributes.get("task_id")
                    or node.node_id.removeprefix("active_task:")
                ),
                "assigned_robot_id": robot_by_task.get(node.node_id),
                "load_state": node.attributes.get("load_state"),
            }
            for node in graph.nodes
            if node.node_type == "active_task"
        ]

    @staticmethod
    def _fleet_summary(robots: RobotRuntimeContext) -> dict[str, Any]:
        """Keep solver-relevant robot facts while dropping pose/detail noise."""

        return {
            "candidate_robot_ids": list(robots.candidate_robot_ids),
            "excluded_by_reason": dict(robots.excluded_by_reason),
            "minimum_battery_pct": robots.min_battery_pct,
            "minimum_capacity_units": robots.min_capacity_units,
            "robots": [
                {
                    "robot_id": robot.robot_id,
                    "status": robot.status,
                    "battery_pct": robot.battery_pct,
                    "capacity_units": robot.capacity_units,
                    "current_node": robot.current_node,
                    "active_task_id": robot.active_task_id,
                    "load_state": robot.load_state,
                    "current_load_units": robot.current_load_units,
                    "available_at_ms": robot.sim_time_ms,
                    "baseline_eligible": robot.robot_id
                    in robots.candidate_robot_ids,
                }
                for robot in robots.robots
            ],
            "summary": robots.summary,
            "missing_info": list(robots.missing_info),
        }

    @staticmethod
    def _map_summary(
        map_context: MapContext,
        graph: WarehouseSituationGraph,
    ) -> dict[str, Any]:
        """Expose semantic facilities and active overlays, never raw topology."""

        facility_types = {
            "inbound_handoff_access",
            "outbound_station",
            "outbound_station_access",
            "empty_tote_buffer",
            "empty_tote_buffer_access",
            "charging_slot",
        }
        facilities = [
            {
                "node_id": node.node_id.removeprefix("map:"),
                "node_type": node.node_type,
            }
            for node in graph.nodes
            if node.node_type in facility_types
        ]
        constraints = map_context.map_constraints
        return {
            "graph_version": map_context.graph_version,
            "node_count": map_context.node_count,
            "edge_count": map_context.edge_count,
            "facilities": facilities,
            "blocked_edge_ids": list(constraints.blocked_edge_ids),
            "blocked_node_ids": list(constraints.blocked_node_ids),
            "edge_penalties": [
                penalty.model_dump(mode="json")
                for penalty in constraints.edge_penalties
            ],
            # Occupancy/reservation rows can grow with a replan horizon.  They
            # remain in the full MapContext used by the compiler and MAPF; the
            # LLM only needs workload pressure summaries.
            "edge_occupancy_count": len(constraints.edge_occupancies),
            "edge_reservation_count": len(constraints.edge_reservations),
            "summary": map_context.summary,
            "missing_info": list(map_context.missing_info),
        }

    @staticmethod
    def _task_route_options(graph: WarehouseSituationGraph) -> list[dict[str, Any]]:
        """Return unique direct-task path costs without node/edge sequences.

        G2P station/return paths are deliberately absent: the deterministic G2P
        compiler owns those physical cycles.  Direct inbound, recovery, and
        legacy outbound work only needs pickup-to-delivery feasibility here.
        """

        best_by_pair: dict[tuple[str, str, str], Any] = {}
        for path in graph.path_evidence:
            if path.purpose != "PICKUP_TO_DELIVERY":
                continue
            key = (path.purpose, path.source_node_id, path.target_node_id)
            current = best_by_pair.get(key)
            if current is None or (
                path.travel_time_ms,
                path.cost,
                path.path_id,
            ) < (
                current.travel_time_ms,
                current.cost,
                current.path_id,
            ):
                best_by_pair[key] = path
        return [
            {
                "purpose": path.purpose,
                "source_node_id": path.source_node_id,
                "target_node_id": path.target_node_id,
                "cost": path.cost,
                "travel_time_ms": path.travel_time_ms,
                "affected_constraint_ids": list(path.affected_constraint_ids),
            }
            for path in sorted(
                best_by_pair.values(),
                key=lambda value: (
                    value.source_node_id,
                    value.target_node_id,
                    value.travel_time_ms,
                    value.cost,
                ),
            )
        ]

    @staticmethod
    def _pickup_reachability(graph: WarehouseSituationGraph) -> list[dict[str, Any]]:
        """Aggregate robot-to-pickup paths by physical pickup node."""

        robot_by_path_id: dict[str, str] = {}
        for relation in graph.relations:
            if relation.relation_type != "CAN_REACH":
                continue
            path_id = relation.attributes.get("path_id")
            if path_id and relation.source_node_id.startswith("robot:"):
                robot_by_path_id[str(path_id)] = relation.source_node_id.removeprefix(
                    "robot:"
                )

        records: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
        for path in graph.path_evidence:
            if path.purpose != "ROBOT_TO_PICKUP":
                continue
            robot_id = robot_by_path_id.get(path.path_id)
            if robot_id is None:
                parts = path.path_id.split(":")
                if len(parts) >= 3 and parts[0:2] == ["path", "robot"]:
                    robot_id = parts[2]
            if robot_id is not None:
                records[path.target_node_id].append(
                    (robot_id, path.travel_time_ms, path.cost)
                )

        result: list[dict[str, Any]] = []
        for pickup_node, values in sorted(records.items()):
            best_by_robot: dict[str, tuple[int, float]] = {}
            for robot_id, travel_time_ms, cost in values:
                current = best_by_robot.get(robot_id)
                candidate = (travel_time_ms, cost)
                if current is None or candidate < current:
                    best_by_robot[robot_id] = candidate
            result.append(
                {
                    "pickup_node_id": pickup_node,
                    "reachable_robot_ids": sorted(best_by_robot),
                    "minimum_travel_time_ms": min(
                        value[0] for value in best_by_robot.values()
                    ),
                    "minimum_cost": min(value[1] for value in best_by_robot.values()),
                }
            )
        return result

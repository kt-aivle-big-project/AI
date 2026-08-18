"""Reusable read-only warehouse context builders.

The same functions power deterministic LangGraph context nodes and the bounded
LLM agent tools.  They never mutate inventory, robot state, map state, or
reservations.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from app.core.config import get_settings
from app.domain.schemas import (
    CandidateStock,
    EdgeOccupancy,
    EdgePenalty,
    EdgeReservation,
    InventoryContext,
    InventoryOverview,
    InventoryQueryScope,
    InventoryTaskNeed,
    InboundTaskNeed,
    CandidatePutawaySlot,
    MapConstraints,
    MapContext,
    MissionSpec,
    RelevantMapNode,
    RobotRuntime,
    RobotRuntimeContext,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository


@dataclass(frozen=True)
class MapContextBundle:
    """Map context plus the complete adjusted graph used by downstream solvers."""

    context: MapContext
    graph_nodes: list[str]
    graph_node_types: dict[str, str]
    graph_arcs: list[dict]


def apply_runtime_overrides(
    context: RobotRuntimeContext,
    overrides: object | None,
) -> RobotRuntimeContext:
    """Apply one trusted runtime snapshot before eligibility is evaluated.

    ``OVERLAY`` keeps Redis robots and replaces only matching fields.
    ``COMPLETE`` treats the request snapshot as the entire authoritative fleet.
    Override-only robots are materialized into :class:`RobotRuntime`, which lets
    a complete simulator snapshot drive planning even when the target Redis
    namespace has not been seeded.  Every robot is clamped to the global rolling
    horizon so a replan can never create steps in the past.
    """

    robot_states = list(getattr(overrides, "robot_states", []) or [])
    snapshot_mode = str(
        getattr(overrides, "runtime_snapshot_mode", "OVERLAY") or "OVERLAY"
    ).upper()
    horizon_start_ms = int(
        getattr(overrides, "planning_horizon_start_ms", 0) or 0
    )
    allowed_task_robot_ids = getattr(overrides, "allowed_task_robot_ids", None)
    allowed_task_robot_set = (
        set(allowed_task_robot_ids)
        if allowed_task_robot_ids is not None
        else None
    )
    if (
        not robot_states
        and horizon_start_ms <= 0
        and allowed_task_robot_set is None
    ):
        return context

    base_by_id = {value.robot_id: value for value in context.robots}
    override_by_id = {value.robot_id: value for value in robot_states}
    if snapshot_mode == "COMPLETE":
        robot_ids = list(override_by_id)
    else:
        robot_ids = list(base_by_id)
        robot_ids.extend(
            robot_id for robot_id in override_by_id if robot_id not in base_by_id
        )

    robots: list[RobotRuntime] = []
    for robot_id in robot_ids:
        robot = base_by_id.get(robot_id)
        value = override_by_id.get(robot_id)
        if robot is None:
            if value is None:
                continue
            robot = RobotRuntime(
                warehouse_id=context.warehouse_id,
                simulation_id=context.simulation_id,
                robot_id=robot_id,
                robot_code=robot_id,
                status=value.status,
                battery_pct=(
                    float(value.battery_pct)
                    if value.battery_pct is not None
                    else 0.0
                ),
                capacity_units=(
                    int(value.capacity_units)
                    if value.capacity_units is not None
                    else context.min_capacity_units
                ),
                current_node=value.current_node,
                current_edge=value.current_edge,
                active_task_id=value.active_task_id,
                load_state=(
                    "LOADED"
                    if int(value.current_load_units or 0) > 0
                    else "EMPTY"
                ),
                current_load_units=int(value.current_load_units or 0),
                sim_time_ms=max(int(value.sim_time_ms), horizon_start_ms),
                from_node=value.from_node,
                to_node=value.to_node,
                edge_progress=value.edge_progress,
            )

        update: dict[str, object] = {
            "sim_time_ms": max(
                int(robot.sim_time_ms),
                int(value.sim_time_ms) if value is not None else 0,
                horizon_start_ms,
            )
        }
        if value is not None:
            update.update(
                {
                    "status": value.status,
                    "current_node": value.current_node,
                    "current_edge": value.current_edge,
                    "from_node": value.from_node,
                    "to_node": value.to_node,
                    "edge_progress": value.edge_progress,
                    "active_task_id": (
                        None
                        if value.clear_active_work
                        else (
                            value.active_task_id
                            if value.active_task_id is not None
                            else robot.active_task_id
                        )
                    ),
                }
            )
            if value.clear_active_work:
                update["active_mission_id"] = None
            if value.current_edge is None and value.current_node is None:
                update["current_node"] = robot.current_node
            if value.battery_pct is not None:
                update["battery_pct"] = value.battery_pct
            if value.capacity_units is not None:
                update["capacity_units"] = value.capacity_units
            if value.current_load_units is not None:
                update["current_load_units"] = value.current_load_units
                update["load_state"] = (
                    "LOADED" if value.current_load_units > 0 else "EMPTY"
                )
        robots.append(robot.model_copy(update=update))

    candidates: list[str] = []
    excluded: dict[str, list[str]] = defaultdict(list)
    for robot in robots:
        reasons: list[str] = []
        if robot.status != "idle":
            reasons.append(f"status:{robot.status}")
        if robot.current_node is None:
            reasons.append("not_at_plannable_node")
        if robot.active_task_id is not None or robot.active_mission_id is not None:
            reasons.append("active_work")
        if robot.current_load_units > 0 or robot.load_state == "LOADED":
            reasons.append("already_loaded")
        if robot.battery_pct < context.min_battery_pct:
            reasons.append("low_battery")
        if robot.capacity_units - robot.current_load_units < context.min_capacity_units:
            reasons.append("insufficient_capacity")
        if (
            allowed_task_robot_set is not None
            and robot.robot_id not in allowed_task_robot_set
        ):
            reasons.append("transition_fleet_restriction")
        if reasons:
            for reason in reasons:
                excluded[reason].append(robot.robot_id)
        else:
            candidates.append(robot.robot_id)

    return context.model_copy(
        update={
            "robots": robots,
            "candidate_robot_ids": candidates,
            "excluded_by_reason": dict(excluded),
            "missing_info": (
                []
                if robots
                else [
                    f"No robot runtime is available for {context.warehouse_id}/"
                    f"{context.simulation_id}."
                ]
            ),
            "summary": (
                f"{len(candidates)} of {len(robots)} robot(s) are eligible after "
                f"the request-scoped {snapshot_mode} runtime snapshot; battery "
                f"threshold={context.min_battery_pct}%; planning horizon="
                f"{horizon_start_ms}ms."
            ),
        }
    )


def apply_runtime_map_overrides(
    context: MapContext,
    overrides: object | None,
) -> MapContext:
    """Merge old-plan edge reservations into the request-scoped map snapshot.

    Rolling-horizon replanning keeps already-started MOVE/handling cycles on the
    old plan.  Their future corridor reservations must remain visible to both
    cuOpt/OR-Tools feasibility checks and the new MAPF solve.
    """

    preserved = list(
        getattr(overrides, "preserved_edge_reservations", []) or []
    )
    if not preserved:
        return context
    existing = {
        value.reservation_id: value
        for value in context.map_constraints.edge_reservations
    }
    for value in preserved:
        existing[value.reservation_id] = value
    constraints = context.map_constraints.model_copy(
        update={"edge_reservations": list(existing.values())}
    )
    return context.model_copy(
        update={
            "map_constraints": constraints,
            "summary": (
                f"{context.summary} Rolling-horizon overlay preserves "
                f"{len(preserved)} committed edge reservation(s)."
            ),
        }
    )


class WarehouseContextService:
    """Build bounded inventory, map, and robot views from one repository snapshot."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        """Use the process repository unless an explicit read-only repository is supplied."""

        self.repository = repository or get_repository()

    def build_inventory_context(
        self,
        *,
        order_ids: Iterable[str] = (),
        inbound_ids: Iterable[str] = (),
        item_ids: Iterable[str] = (),
    ) -> InventoryContext:
        """Build an order-scoped, item-scoped, or aggregate inventory context."""

        repository = self.repository
        unique_order_ids = list(dict.fromkeys(str(value) for value in order_ids if value))
        unique_inbound_ids = list(dict.fromkeys(str(value) for value in inbound_ids if value))
        unique_item_ids = list(dict.fromkeys(str(value) for value in item_ids if value))
        task_needs: list[InventoryTaskNeed] = []
        inbound_needs: list[InboundTaskNeed] = []
        candidate_stocks: list[CandidateStock] = []
        missing: list[str] = []


        slots: list[CandidatePutawaySlot] = []
        if unique_inbound_ids:
            for inbound_id in unique_inbound_ids:
                receipt = repository.get_inbound_receipt(inbound_id)
                if receipt is None:
                    missing.append(f"Inbound receipt {inbound_id} was not found.")
                    continue
                inbound_needs.append(
                    InboundTaskNeed(
                        inbound_id=inbound_id,
                        handling_unit_id=str(receipt.get("handling_unit_id") or f"HU-{inbound_id}"),
                        item_id=str(receipt["item_id"]),
                        quantity=int(receipt["quantity"]),
                        transport_unit_count=(
                            int(receipt["transport_unit_count"])
                            if receipt.get("transport_unit_count") is not None
                            else None
                        ),
                        source_port_id=str(receipt["source_port_id"]),
                        priority=str(receipt.get("priority", "medium")),
                        target_rack_id=receipt.get("target_rack_id"),
                        target_rack_level=receipt.get("target_rack_level"),
                        status=str(receipt.get("status", "arrived")),
                    )
                )
            slots = [
                CandidatePutawaySlot.model_validate(value)
                for value in repository.empty_putaway_slots()
            ]

        if unique_order_ids:
            seen_stock_ids: set[str] = set()
            for order_id in unique_order_ids:
                order = repository.get_order(order_id)
                if order is None:
                    missing.append(f"Order {order_id} was not found.")
                    continue
                task_needs.append(
                    InventoryTaskNeed(
                        order_id=order_id,
                        item_id=str(order["item_id"]),
                        required_qty=int(order["required_qty"]),
                        delivery_node=str(order["delivery_node"]),
                        priority=str(order.get("priority", "medium")),
                        order_status=str(order.get("status", "unknown")),
                    )
                )
                for stock in repository.item_stocks(str(order["item_id"])):
                    if stock["stock_id"] in seen_stock_ids:
                        continue
                    seen_stock_ids.add(str(stock["stock_id"]))
                    candidate_stocks.append(self._candidate_stock(stock))

        if unique_order_ids and unique_inbound_ids:
            return InventoryContext(
                query_scope=InventoryQueryScope(
                    mode="mixed_operations",
                    warehouse_id=repository.warehouse_id,
                    order_ids=unique_order_ids,
                    item_ids=list(dict.fromkeys(
                        [value.item_id for value in task_needs] +
                        [value.item_id for value in inbound_needs]
                    )),
                    reason="Read outbound order stock and inbound receipt putaway facts in one snapshot.",
                ),
                inventory_summary=(
                    f"Loaded {len(task_needs)} outbound order(s), {len(candidate_stocks)} stock candidate(s), "
                    f"{len(inbound_needs)} inbound receipt(s), and {len(slots)} putaway slot(s)."
                ),
                task_needs=task_needs,
                inbound_needs=inbound_needs,
                candidate_stocks=candidate_stocks,
                candidate_putaway_slots=slots,
                missing_info=missing,
            )

        if unique_inbound_ids:
            return InventoryContext(
                query_scope=InventoryQueryScope(
                    mode="inbound_putaway",
                    warehouse_id=repository.warehouse_id,
                    item_ids=[value.item_id for value in inbound_needs],
                    reason="Read inbound receipt facts and empty putaway slots.",
                ),
                inventory_summary=(
                    f"Loaded {len(inbound_needs)} inbound receipt(s) and "
                    f"{len(slots)} empty putaway slot(s)."
                ),
                inbound_needs=inbound_needs,
                candidate_putaway_slots=slots,
                missing_info=missing,
            )

        if unique_order_ids:
            return InventoryContext(
                query_scope=InventoryQueryScope(
                    mode="order_fulfillment",
                    warehouse_id=repository.warehouse_id,
                    order_ids=unique_order_ids,
                    reason="Read only the orders and rack levels needed for mission grounding.",
                ),
                inventory_summary=(
                    f"Loaded {len(task_needs)} order requirement(s) and "
                    f"{len(candidate_stocks)} candidate rack level(s)."
                ),
                task_needs=task_needs,
                candidate_stocks=candidate_stocks,
                missing_info=missing,
            )

        if unique_item_ids:
            seen_stock_ids: set[str] = set()
            for item_id in unique_item_ids:
                stocks = repository.item_stocks(item_id)
                if not stocks:
                    missing.append(f"Item {item_id} has no positive-quantity rack record.")
                for stock in stocks:
                    if stock["stock_id"] in seen_stock_ids:
                        continue
                    seen_stock_ids.add(str(stock["stock_id"]))
                    candidate_stocks.append(self._candidate_stock(stock))
            return InventoryContext(
                query_scope=InventoryQueryScope(
                    mode="item_detail",
                    warehouse_id=repository.warehouse_id,
                    item_ids=unique_item_ids,
                    reason="Read only the requested item locations and quantities.",
                ),
                inventory_summary=f"Loaded {len(candidate_stocks)} rack level(s) for {len(unique_item_ids)} item(s).",
                candidate_stocks=candidate_stocks,
                missing_info=missing,
            )

        overview = InventoryOverview.model_validate(repository.inventory_overview())
        return InventoryContext(
            query_scope=InventoryQueryScope(
                mode="warehouse_overview",
                warehouse_id=repository.warehouse_id,
                reason="Return aggregates instead of serializing all rack levels.",
            ),
            inventory_summary=(
                f"{overview.distinct_item_count} item(s) occupy "
                f"{overview.occupied_level_count} rack level(s)."
            ),
            overview=overview,
        )

    def build_robot_context(self, *, required_capacity: int = 1) -> RobotRuntimeContext:
        """Return all current robots and deterministic eligibility classifications."""

        settings = get_settings()
        robots = [
            RobotRuntime.model_validate(
                {
                    **value,
                    "warehouse_id": self.repository.warehouse_id,
                    "simulation_id": self.repository.simulation_id,
                }
            )
            for value in self.repository.all_robots()
        ]
        candidates: list[str] = []
        excluded: dict[str, list[str]] = defaultdict(list)
        known_graph_nodes = set(getattr(self.repository, "nodes", {}))
        for robot in robots:
            reasons: list[str] = []
            if robot.status != "idle":
                reasons.append(f"status:{robot.status}")
            if robot.current_node is None:
                reasons.append("not_at_plannable_node")
            elif known_graph_nodes and robot.current_node not in known_graph_nodes:
                reasons.append("node_not_in_active_graph")
            if robot.active_task_id is not None or robot.active_mission_id is not None:
                reasons.append("active_work")
            if robot.current_load_units > 0 or robot.load_state == "LOADED":
                reasons.append("already_loaded")
            if robot.battery_pct < settings.robot_min_battery_pct:
                reasons.append("low_battery")
            if robot.capacity_units - robot.current_load_units < required_capacity:
                reasons.append("insufficient_capacity")
            if reasons:
                for reason in reasons:
                    excluded[reason].append(robot.robot_id)
            else:
                candidates.append(robot.robot_id)
        return RobotRuntimeContext(
            warehouse_id=self.repository.warehouse_id,
            simulation_id=self.repository.simulation_id,
            robots=robots,
            candidate_robot_ids=candidates,
            excluded_by_reason=dict(excluded),
            min_battery_pct=settings.robot_min_battery_pct,
            min_capacity_units=max(1, required_capacity),
            summary=f"{len(candidates)} of {len(robots)} robot(s) are eligible for demand {required_capacity}.",
        )

    def build_map_context(
        self,
        *,
        inventory: InventoryContext | None = None,
        mission: MissionSpec | None = None,
        edge_ids: Iterable[str] = (),
        node_ids: Iterable[str] = (),
    ) -> MapContextBundle:
        """Build a scoped semantic map view and the full runtime-adjusted graph."""

        repository = self.repository
        relevant_ids: set[str] = {str(value) for value in node_ids if value}
        requested_edges = {str(value) for value in edge_ids if value}
        if inventory is not None:
            relevant_ids.update(
                access_node_id
                for stock in inventory.candidate_stocks
                for access_node_id in stock.access_node_ids
            )
            relevant_ids.update(need.delivery_node for need in inventory.task_needs)
            relevant_ids.update(
                access_node_id
                for slot in inventory.candidate_putaway_slots
                for access_node_id in slot.access_node_ids
            )
            for need in inventory.inbound_needs:
                handoff = repository.inbound_handoff_for_port(need.source_port_id)
                if handoff:
                    relevant_ids.update(
                        repository.mobile_handoff_node_for_inbound_access(str(value))
                        for value in handoff.get("access_node_ids", [])
                    )
        if mission is not None:
            relevant_ids.update(task.delivery_node for task in mission.task_requests)

        penalties: list[EdgePenalty] = []
        occupancies: list[EdgeOccupancy] = []
        reservations: list[EdgeReservation] = []
        blocked_edges: list[str] = []
        blocked_nodes: list[str] = []
        for runtime in repository.runtime_edge_records():
            edge_id = str(runtime["edge_id"])
            edge = repository.edges[edge_id]
            relevant_ids.update([str(edge["source"]), str(edge["target"])])
            status = runtime.get("status")
            if status == "congested":
                penalties.append(
                    EdgePenalty(
                        edge_id=edge_id,
                        cost_multiplier=float(runtime.get("cost_multiplier", 1.0)),
                        travel_time_multiplier=float(runtime.get("travel_time_multiplier", 1.0)),
                        reason=str(runtime.get("reason", "Runtime congestion.")),
                    )
                )
            elif status == "occupied":
                occupancies.append(
                    EdgeOccupancy(
                        edge_id=edge_id,
                        robot_id=str(runtime["occupying_robot_id"]),
                        direction=str(runtime.get("direction", "UNKNOWN")),
                        occupied_from_ms=int(runtime.get("occupied_from_ms", 0)),
                        occupied_until_ms=int(runtime["occupied_until_ms"]),
                        capacity=int(runtime.get("capacity", 1)),
                        reason=str(runtime.get("reason", "Runtime occupancy.")),
                    )
                )
            elif status == "reserved":
                reservations.append(EdgeReservation.model_validate(runtime))
            elif status == "blocked":
                blocked_edges.append(edge_id)

        for edge_id in requested_edges:
            edge = repository.edge(edge_id)
            if edge:
                relevant_ids.update([str(edge["source"]), str(edge["target"])])
        reservations.extend(EdgeReservation.model_validate(value) for value in repository.existing_reservations())
        constraints = MapConstraints(
            blocked_edge_ids=sorted(set(blocked_edges)),
            blocked_node_ids=sorted(set(blocked_nodes)),
            edge_penalties=penalties,
            edge_occupancies=occupancies,
            edge_reservations=reservations,
        )
        relevant_nodes = [
            RelevantMapNode(
                node_id=node_id,
                node_type=str(repository.nodes[node_id]["type"]),
                x=float(repository.nodes[node_id]["x"]),
                y=float(repository.nodes[node_id]["y"]),
            )
            for node_id in sorted(relevant_ids)
            if node_id in repository.nodes
        ]
        arcs = repository.adjusted_arcs(
            blocked_edge_ids=set(constraints.blocked_edge_ids),
            blocked_node_ids=set(constraints.blocked_node_ids),
        )
        context = MapContext(
            warehouse_id=repository.warehouse_id,
            graph_version=repository.versions["graph_version"],
            node_count=len(repository.nodes),
            edge_count=len(repository.edges),
            relevant_nodes=relevant_nodes,
            map_constraints=constraints,
            summary=(
                f"Graph has {len(repository.nodes)} nodes and {len(repository.edges)} directed edges; "
                f"{len(penalties)} congested, {len(occupancies)} occupied, "
                f"and {len(blocked_edges)} blocked edge(s)."
            ),
        )
        return MapContextBundle(
            context=context,
            graph_nodes=list(repository.nodes),
            graph_node_types={node_id: str(value["type"]) for node_id, value in repository.nodes.items()},
            graph_arcs=arcs,
        )

    @staticmethod
    def _candidate_stock(stock: dict) -> CandidateStock:
        """Convert one repository rack record into the public candidate contract."""

        return CandidateStock(
            stock_id=str(stock["stock_id"]),
            item_id=str(stock["item_id"]),
            item_name=str(stock["item_name"]),
            rack_id=str(stock["rack_id"]),
            rack_level=int(stock["rack_level"]),
            access_node_ids=[str(value) for value in stock["access_node_ids"]],
            available_qty=int(stock["quantity"]),
            unit=str(stock["unit"]),
        )

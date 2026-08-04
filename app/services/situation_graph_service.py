"""Build and validate a request-scoped Warehouse Situation Graph.

The graph is a read-only materialized view over the authoritative order and
inventory store, robot/traffic runtime, and the static warehouse graph.  It is
created per orchestration snapshot and is never treated as a source of truth.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

from app.core.config import get_settings
from app.domain.schemas import (
    ContextSnapshot,
    InventoryContext,
    MapContext,
    NormalizedWarehouseRequest,
    RobotRuntimeContext,
    RetrievalObservation,
    SituationEvidence,
    SituationGraphCompleteness,
    SituationGraphValidationResult,
    SituationNode,
    SituationPathEvidence,
    SituationRelation,
    WarehouseSituationGraph,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository
from app.services.graph_service import DirectedGraphService


class WarehouseSituationGraphBuilder:
    """Join scoped source contexts into one evidence-backed situation graph."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        """Initialize this service with its validated dependencies."""
        self.repository = repository or get_repository()

    def build(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        snapshot: ContextSnapshot,
        inventory: InventoryContext,
        robots: RobotRuntimeContext,
        map_context: MapContext,
        graph_arcs: Iterable[dict[str, Any]],
        retrieval_observations: Iterable[RetrievalObservation] = (),
    ) -> WarehouseSituationGraph:
        """Create a complete scoped graph for rule or LLM cuOpt formulation."""

        evidence: dict[str, SituationEvidence] = {}
        nodes: dict[str, SituationNode] = {}
        observations = list(retrieval_observations)
        observations_by_entity: dict[str, list[str]] = defaultdict(list)
        observations_by_tool: dict[str, list[str]] = defaultdict(list)
        for observation in observations:
            observations_by_tool[observation.tool_name].append(observation.observation_id)
            for entity_id in observation.canonical_entity_ids:
                observations_by_entity[entity_id].append(observation.observation_id)
        relations: dict[str, SituationRelation] = {}
        paths: dict[str, SituationPathEvidence] = {}
        graph = DirectedGraphService(graph_arcs)
        requested_order_ids = [
            operation.operation_id
            for operation in normalized_request.operations
            if operation.operation_type == "OUTBOUND_ORDER"
        ]
        requested_inbound_ids = [
            operation.operation_id
            for operation in normalized_request.operations
            if operation.operation_type == "INBOUND_ITEM"
        ]
        g2p_mode = bool(
            requested_order_ids
            and get_settings().outbound_fulfillment_mode == "goods_to_person"
        )

        def add_evidence(source: str, record_id: str, payload: Any) -> str:
            """Add one validated entity or relation to the accumulating result."""
            key = f"{source}:{record_id}"
            digest = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:10]
            evidence_id = f"EVID-{source.upper()}-{record_id}-{digest}"
            source_tool_map = {
                "inventory_store": [
                    "get_order_facts",
                    "get_inbound_facts",
                    "get_inventory_candidates",
                    "find_orders",
                ],
                "facility_master": [
                    "get_inbound_facts",
                    "resolve_map_entities",
                    "get_connecting_subgraph",
                ],
                "robot_runtime": ["get_robot_candidates", "get_active_operations"],
                "traffic_runtime": ["get_runtime_constraints"],
                "warehouse_graph": ["get_connecting_subgraph", "resolve_map_entities"],
            }
            observation_candidates = list(observations_by_entity.get(record_id, []))
            if not observation_candidates:
                for tool_name in source_tool_map.get(source, []):
                    observation_candidates.extend(observations_by_tool.get(tool_name, []))
            observation_id = (
                observation_candidates[0]
                if observation_candidates
                else f"OBS-{source.upper()}-{digest}"
            )
            evidence.setdefault(
                evidence_id,
                SituationEvidence(
                    evidence_id=evidence_id,
                    source=source,
                    source_record_id=record_id,
                    observation_id=observation_id,
                    captured_at=snapshot.captured_at,
                ),
            )
            return evidence_id

        def add_node(
            node_id: str,
            node_type: str,
            attributes: dict[str, Any],
            evidence_ids: list[str],
        ) -> None:
            """Add one validated entity or relation to the accumulating result."""
            existing = nodes.get(node_id)
            if existing is None:
                nodes[node_id] = SituationNode(
                    node_id=node_id,
                    node_type=node_type,
                    attributes=attributes,
                    evidence_ids=list(dict.fromkeys(evidence_ids)),
                )
                return
            merged = dict(existing.attributes)
            merged.update(attributes)
            nodes[node_id] = existing.model_copy(
                update={
                    "attributes": merged,
                    "evidence_ids": list(dict.fromkeys([*existing.evidence_ids, *evidence_ids])),
                }
            )

        def add_relation(
            relation_id: str,
            source_id: str,
            target_id: str,
            relation_type: str,
            *,
            attributes: dict[str, Any] | None = None,
            evidence_ids: list[str] | None = None,
        ) -> None:
            """Add one validated entity or relation to the accumulating result."""
            relations[relation_id] = SituationRelation(
                relation_id=relation_id,
                source_node_id=source_id,
                target_node_id=target_id,
                relation_type=relation_type,
                attributes=attributes or {},
                evidence_ids=evidence_ids or [],
            )

        # Request anchors are evidence too: this makes it possible to prove that
        # every requested operation was represented in the final formulation.
        request_anchor_ids: list[str] = []
        request_constraint_evidence = add_evidence(
            "request",
            "constraints",
            normalized_request.constraints.model_dump(mode="json"),
        )
        for operation in normalized_request.operations:
            request_anchor_ids.append(operation.operation_id)
            evidence_id = add_evidence(
                "request",
                operation.operation_id,
                operation.model_dump(mode="json"),
            )
            if operation.operation_type == "OUTBOUND_ORDER":
                add_node(
                    f"order:{operation.operation_id}",
                    "order",
                    {"order_id": operation.operation_id, "requested_by_input": True},
                    [evidence_id],
                )
            elif operation.operation_type == "INBOUND_ITEM":
                add_node(
                    f"inbound:{operation.operation_id}",
                    "inbound",
                    {"inbound_id": operation.operation_id, "requested_by_input": True},
                    [evidence_id],
                )

        needs_by_order = {need.order_id: need for need in inventory.task_needs}
        stocks_by_item: dict[str, list] = defaultdict(list)
        for stock in inventory.candidate_stocks:
            stocks_by_item[stock.item_id].append(stock)

        # Authoritative order, item, stock, rack, and delivery facts.
        for need in inventory.task_needs:
            order_evidence = add_evidence(
                "inventory_store",
                need.order_id,
                need.model_dump(mode="json"),
            )
            order_node_id = f"order:{need.order_id}"
            item_node_id = f"item:{need.item_id}"
            destination_node_id = (
                f"destination:{need.delivery_node}"
                if g2p_mode
                else f"map:{need.delivery_node}"
            )
            add_node(
                order_node_id,
                "order",
                {
                    "order_id": need.order_id,
                    "required_qty": need.required_qty,
                    "priority": need.priority,
                    "status": need.order_status,
                    "delivery_node": need.delivery_node,
                    "logical_destination_id": need.delivery_node,
                    "fulfillment_mode": "goods_to_person" if g2p_mode else "legacy_order_tasks",
                },
                [order_evidence],
            )
            add_node(
                item_node_id,
                "item",
                {"item_id": need.item_id},
                [order_evidence],
            )
            if g2p_mode:
                destination_evidence = add_evidence(
                    "inventory_store",
                    f"logical_destination:{need.delivery_node}",
                    {"logical_destination_id": need.delivery_node},
                )
                add_node(
                    destination_node_id,
                    "logical_destination",
                    {"logical_destination_id": need.delivery_node},
                    [destination_evidence],
                )
            else:
                self._add_map_node(
                    add_node=add_node,
                    node_id=need.delivery_node,
                    evidence=add_evidence,
                    snapshot=snapshot,
                )
            add_relation(
                f"rel:{need.order_id}:requires:{need.item_id}",
                order_node_id,
                item_node_id,
                "REQUIRES_ITEM",
                evidence_ids=[order_evidence],
            )
            add_relation(
                f"rel:{need.order_id}:deliver:{need.delivery_node}",
                order_node_id,
                destination_node_id,
                "DELIVER_TO",
                evidence_ids=[order_evidence],
            )

        for stock in inventory.candidate_stocks:
            stock_evidence = add_evidence(
                "inventory_store",
                stock.stock_id,
                stock.model_dump(mode="json"),
            )
            stock_node_id = f"stock:{stock.stock_id}"
            item_node_id = f"item:{stock.item_id}"
            rack_node_id = f"rack:{stock.rack_id}"
            add_node(
                stock_node_id,
                "stock",
                {
                    "stock_id": stock.stock_id,
                    "item_id": stock.item_id,
                    "available_qty": stock.available_qty,
                    "unit": stock.unit,
                    "rack_level": stock.rack_level,
                    "rack_id": stock.rack_id,
                    "access_node_ids": list(stock.access_node_ids),
                },
                [stock_evidence],
            )
            add_node(
                item_node_id,
                "item",
                {"item_id": stock.item_id, "item_name": stock.item_name},
                [stock_evidence],
            )
            rack_evidence = add_evidence(
                "inventory_store",
                stock.rack_id,
                {"rack_id": stock.rack_id, "access_node_ids": stock.access_node_ids},
            )
            add_node(
                rack_node_id,
                "rack",
                {"rack_id": stock.rack_id, "access_node_ids": list(stock.access_node_ids)},
                [rack_evidence],
            )
            for access_node_id in stock.access_node_ids:
                self._add_map_node(
                    add_node=add_node,
                    node_id=access_node_id,
                    evidence=add_evidence,
                    snapshot=snapshot,
                )
                add_relation(
                    f"rel:{stock.rack_id}:access:{access_node_id}",
                    rack_node_id,
                    f"map:{access_node_id}",
                    "HAS_ACCESS_POINT",
                    evidence_ids=[rack_evidence],
                )
            add_relation(
                f"rel:{stock.stock_id}:item",
                stock_node_id,
                item_node_id,
                "OF_ITEM",
                evidence_ids=[stock_evidence],
            )
            add_relation(
                f"rel:{stock.stock_id}:rack",
                stock_node_id,
                rack_node_id,
                "STORED_AT",
                evidence_ids=[stock_evidence],
            )

        # Authoritative inbound receipt, handling-unit, handoff, and putaway
        # candidates.  Agent formulation must receive the same physical facts
        # that the direct Rule path uses; otherwise a mixed G2P+inbound request
        # cannot be represented without silently dropping the inbound work.
        inbound_needs_by_id = {
            need.inbound_id: need for need in inventory.inbound_needs
        }
        inbound_pickup_nodes: dict[str, list[str]] = defaultdict(list)
        inbound_delivery_nodes: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
        for need in inventory.inbound_needs:
            inbound_evidence = add_evidence(
                "inventory_store",
                need.inbound_id,
                need.model_dump(mode="json"),
            )
            inbound_node_id = f"inbound:{need.inbound_id}"
            item_node_id = f"item:{need.item_id}"
            handling_unit_node_id = f"handling_unit:{need.handling_unit_id}"
            add_node(
                inbound_node_id,
                "inbound",
                {
                    **need.model_dump(mode="json"),
                    "requested_by_input": need.inbound_id in request_anchor_ids,
                },
                [inbound_evidence],
            )
            add_node(
                item_node_id,
                "item",
                {"item_id": need.item_id},
                [inbound_evidence],
            )
            add_node(
                handling_unit_node_id,
                "handling_unit",
                {
                    "handling_unit_id": need.handling_unit_id,
                    "item_id": need.item_id,
                    "quantity": need.quantity,
                    "status": need.status,
                    "source_inbound_id": need.inbound_id,
                },
                [inbound_evidence],
            )
            add_relation(
                f"rel:{need.inbound_id}:requires:{need.item_id}",
                inbound_node_id,
                item_node_id,
                "REQUIRES_ITEM",
                evidence_ids=[inbound_evidence],
            )
            add_relation(
                f"rel:{need.inbound_id}:handling_unit:{need.handling_unit_id}",
                inbound_node_id,
                handling_unit_node_id,
                "USES_HANDLING_UNIT",
                evidence_ids=[inbound_evidence],
            )

            handoff = self.repository.inbound_handoff_for_port(need.source_port_id)
            if handoff:
                handoff_evidence = add_evidence(
                    "facility_master",
                    str(handoff.get("handoff_id") or need.source_port_id),
                    handoff,
                )
                mobile_pickups = {
                    self.repository.mobile_handoff_node_for_inbound_access(str(value)): str(value)
                    for value in handoff.get("access_node_ids", [])
                }
                for pickup_node, facility_access_node in mobile_pickups.items():
                    inbound_pickup_nodes[need.inbound_id].append(pickup_node)
                    self._add_map_node(
                        add_node=add_node,
                        node_id=pickup_node,
                        evidence=add_evidence,
                        snapshot=snapshot,
                    )
                    add_relation(
                        f"rel:{need.inbound_id}:pickup:{pickup_node}",
                        inbound_node_id,
                        f"map:{pickup_node}",
                        "PICKUP_FROM",
                        attributes={"facility_access_node": facility_access_node},
                        evidence_ids=[inbound_evidence, handoff_evidence],
                    )

            candidate_slots = list(inventory.candidate_putaway_slots)
            if need.target_rack_id:
                candidate_slots = [
                    slot for slot in candidate_slots
                    if slot.rack_id == need.target_rack_id
                ]
            if need.target_rack_level:
                candidate_slots = [
                    slot for slot in candidate_slots
                    if slot.rack_level == need.target_rack_level
                ]
            for slot in candidate_slots:
                slot_record_id = f"{slot.rack_id}:L{slot.rack_level}"
                slot_evidence = add_evidence(
                    "inventory_store",
                    f"rack_slot:{slot_record_id}",
                    slot.model_dump(mode="json"),
                )
                slot_node_id = f"rack_slot:{slot_record_id}"
                rack_node_id = f"rack:{slot.rack_id}"
                add_node(
                    slot_node_id,
                    "rack_slot",
                    slot.model_dump(mode="json"),
                    [slot_evidence],
                )
                add_node(
                    rack_node_id,
                    "rack",
                    {
                        "rack_id": slot.rack_id,
                        "access_node_ids": list(slot.access_node_ids),
                    },
                    [slot_evidence],
                )
                add_relation(
                    f"rel:{need.inbound_id}:putaway:{slot_record_id}",
                    inbound_node_id,
                    slot_node_id,
                    "PUTAWAY_TO",
                    evidence_ids=[inbound_evidence, slot_evidence],
                )
                add_relation(
                    f"rel:{slot_record_id}:rack:{slot.rack_id}",
                    slot_node_id,
                    rack_node_id,
                    "STORED_AT",
                    evidence_ids=[slot_evidence],
                )
                for delivery_node in slot.access_node_ids:
                    delivery_node = str(delivery_node)
                    inbound_delivery_nodes[need.inbound_id].append(
                        (slot.rack_id, slot.rack_level, delivery_node)
                    )
                    self._add_map_node(
                        add_node=add_node,
                        node_id=delivery_node,
                        evidence=add_evidence,
                        snapshot=snapshot,
                    )
                    add_relation(
                        f"rel:{slot_record_id}:access:{delivery_node}",
                        rack_node_id,
                        f"map:{delivery_node}",
                        "HAS_ACCESS_POINT",
                        evidence_ids=[slot_evidence],
                    )

        if g2p_mode:
            # Handling units are the physical outbound transport units.  Orders
            # remain logical demand records; AMR work is compiled later from
            # these units and the station-access paths below.
            relevant_items = {need.item_id for need in inventory.task_needs}
            stock_node_by_stock_id = {
                str(node.attributes.get("stock_id")): node
                for node in nodes.values()
                if node.node_type == "stock"
            }
            for item_id in sorted(relevant_items):
                for handling_unit in self.repository.handling_units(item_id):
                    hu_id = str(handling_unit["handling_unit_id"])
                    stock_id = str(handling_unit["stock_id"])
                    rack_id = str(handling_unit["rack_id"])
                    hu_evidence = add_evidence(
                        "inventory_store",
                        hu_id,
                        handling_unit,
                    )
                    add_node(
                        f"handling_unit:{hu_id}",
                        "handling_unit",
                        {
                            **handling_unit,
                            "available_qty": int(handling_unit.get("quantity", 0)),
                        },
                        [hu_evidence],
                    )
                    if stock_id in stock_node_by_stock_id:
                        add_relation(
                            f"rel:{hu_id}:stock:{stock_id}",
                            f"handling_unit:{hu_id}",
                            f"stock:{stock_id}",
                            "REPRESENTS_STOCK",
                            evidence_ids=[hu_evidence],
                        )
                    add_relation(
                        f"rel:{hu_id}:item:{item_id}",
                        f"handling_unit:{hu_id}",
                        f"item:{item_id}",
                        "OF_ITEM",
                        evidence_ids=[hu_evidence],
                    )
                    add_relation(
                        f"rel:{hu_id}:rack:{rack_id}",
                        f"handling_unit:{hu_id}",
                        f"rack:{rack_id}",
                        "STORED_AT",
                        evidence_ids=[hu_evidence],
                    )

            logical_destinations = sorted({need.delivery_node for need in inventory.task_needs})
            for station in self.repository.outbound_station_candidates(logical_destinations):
                station_id = str(station["station_id"])
                station_coverage = {
                    station_id,
                    *(str(value) for value in station.get("served_chute_ids", [])),
                }
                served_destinations = [
                    value for value in logical_destinations if value in station_coverage
                ]
                station_evidence = add_evidence("facility_master", station_id, station)
                add_node(
                    f"station:{station_id}",
                    "outbound_station",
                    {
                        **station,
                        "logical_destination_ids": served_destinations,
                    },
                    [station_evidence],
                )
                for access_node_id in [str(value) for value in station.get("access_node_ids", [])]:
                    mobile_handoff = self.repository.mobile_handoff_node_for_station_access(
                        access_node_id
                    )
                    self._add_map_node(
                        add_node=add_node,
                        node_id=access_node_id,
                        evidence=add_evidence,
                        snapshot=snapshot,
                    )
                    self._add_map_node(
                        add_node=add_node,
                        node_id=mobile_handoff,
                        evidence=add_evidence,
                        snapshot=snapshot,
                    )
                    add_relation(
                        f"rel:{station_id}:access:{access_node_id}",
                        f"station:{station_id}",
                        f"map:{access_node_id}",
                        "HAS_ACCESS_POINT",
                        evidence_ids=[station_evidence],
                    )
                for destination_id in served_destinations:
                    add_relation(
                        f"rel:{station_id}:serves:{destination_id}",
                        f"station:{station_id}",
                        f"destination:{destination_id}",
                        "SERVES_DESTINATION",
                        evidence_ids=[station_evidence],
                    )

            for buffer in self.repository.empty_tote_buffer_candidates():
                buffer_id = str(buffer["buffer_id"])
                buffer_evidence = add_evidence("facility_master", buffer_id, buffer)
                add_node(
                    f"empty_tote_buffer:{buffer_id}",
                    "empty_tote_buffer",
                    dict(buffer),
                    [buffer_evidence],
                )
                for access_node_id in [str(value) for value in buffer.get("access_node_ids", [])]:
                    self._add_map_node(
                        add_node=add_node,
                        node_id=access_node_id,
                        evidence=add_evidence,
                        snapshot=snapshot,
                    )
                    add_relation(
                        f"rel:{buffer_id}:access:{access_node_id}",
                        f"empty_tote_buffer:{buffer_id}",
                        f"map:{access_node_id}",
                        "HAS_ACCESS_POINT",
                        evidence_ids=[buffer_evidence],
                    )

        # Robot runtime facts.  Every robot is represented, while the candidate
        # flag records canonical baseline eligibility without hiding exclusions.
        robot_by_id = {robot.robot_id: robot for robot in robots.robots}
        for robot in robots.robots:
            robot_evidence = add_evidence(
                "robot_runtime",
                robot.robot_id,
                robot.model_dump(mode="json"),
            )
            robot_node_id = f"robot:{robot.robot_id}"
            current_node_id = f"map:{robot.current_node}"
            add_node(
                robot_node_id,
                "robot",
                {
                    **robot.model_dump(mode="json"),
                    "baseline_eligible": robot.robot_id in robots.candidate_robot_ids,
                },
                [robot_evidence],
            )
            self._add_map_node(
                add_node=add_node,
                node_id=robot.current_node,
                evidence=add_evidence,
                snapshot=snapshot,
            )
            add_relation(
                f"rel:{robot.robot_id}:located:{robot.current_node}",
                robot_node_id,
                current_node_id,
                "LOCATED_AT",
                evidence_ids=[robot_evidence],
            )
            if robot.active_task_id:
                active_id = f"active_task:{robot.active_task_id}"
                add_node(
                    active_id,
                    "active_task",
                    {
                        "task_id": robot.active_task_id,
                        "load_state": robot.load_state,
                    },
                    [robot_evidence],
                )
                add_relation(
                    f"rel:{robot.robot_id}:active:{robot.active_task_id}",
                    robot_node_id,
                    active_id,
                    "HAS_ACTIVE_TASK",
                    evidence_ids=[robot_evidence],
                )

        # Runtime and explicit request constraints are first-class evidence nodes.
        # Explicit edges are checked against the static graph here, before an LLM
        # can use them in a cuOpt draft.
        constraint_ids_by_edge: dict[str, list[str]] = defaultdict(list)
        unknown_requested_edges: list[str] = []
        explicit_constraints = [
            ("REQUESTED_HARD_BLOCK", edge_id, "CUOPT_AND_MAPF")
            for edge_id in normalized_request.constraints.hard_block_edge_ids
        ] + [
            ("REQUESTED_SOFT_AVOID", edge_id, "CUOPT")
            for edge_id in normalized_request.constraints.soft_avoid_edge_ids
        ] + [
            ("REQUESTED_CONDITIONAL_POLICY", value.edge_id, "CUOPT")
            for value in normalized_request.constraints.conditional_edge_policies
        ]
        for constraint_type, edge_id, consumer in explicit_constraints:
            if self.repository.edge(edge_id) is None:
                unknown_requested_edges.append(edge_id)
                continue
            constraint_id = f"constraint:request:{constraint_type.lower()}:{edge_id}"
            self._add_edge_node(add_node, edge_id, add_evidence, snapshot)
            edge_evidence = nodes[f"edge:{edge_id}"].evidence_ids
            combined_evidence = list(dict.fromkeys([request_constraint_evidence, *edge_evidence]))
            add_node(
                constraint_id,
                "runtime_constraint",
                {
                    "constraint_type": constraint_type,
                    "edge_id": edge_id,
                    "consumer": consumer,
                    "source": "normalized_request",
                },
                combined_evidence,
            )
            add_relation(
                f"rel:{constraint_id}:affects:{edge_id}",
                constraint_id,
                f"edge:{edge_id}",
                "AFFECTS",
                evidence_ids=combined_evidence,
            )
            constraint_ids_by_edge[edge_id].append(constraint_id)

        for penalty in map_context.map_constraints.edge_penalties:
            constraint_id = f"constraint:congested:{penalty.edge_id}"
            ev = add_evidence("traffic_runtime", constraint_id, penalty.model_dump(mode="json"))
            add_node(
                constraint_id,
                "runtime_constraint",
                {
                    "constraint_type": "CONGESTED",
                    **penalty.model_dump(mode="json"),
                    "consumer": "CUOPT",
                },
                [ev],
            )
            self._add_edge_node(add_node, penalty.edge_id, add_evidence, snapshot)
            add_relation(
                f"rel:{constraint_id}:affects:{penalty.edge_id}",
                constraint_id,
                f"edge:{penalty.edge_id}",
                "AFFECTS",
                evidence_ids=[ev],
            )
            constraint_ids_by_edge[penalty.edge_id].append(constraint_id)
        for occupancy in map_context.map_constraints.edge_occupancies:
            constraint_id = f"constraint:occupied:{occupancy.edge_id}:{occupancy.robot_id}"
            ev = add_evidence("traffic_runtime", constraint_id, occupancy.model_dump(mode="json"))
            add_node(
                constraint_id,
                "runtime_constraint",
                {
                    "constraint_type": "OCCUPIED",
                    **occupancy.model_dump(mode="json"),
                    "consumer": "MAPF",
                },
                [ev],
            )
            self._add_edge_node(add_node, occupancy.edge_id, add_evidence, snapshot)
            add_relation(
                f"rel:{constraint_id}:affects:{occupancy.edge_id}",
                constraint_id,
                f"edge:{occupancy.edge_id}",
                "AFFECTS",
                evidence_ids=[ev],
            )
            if occupancy.robot_id in robot_by_id:
                add_relation(
                    f"rel:{constraint_id}:occupied_by:{occupancy.robot_id}",
                    constraint_id,
                    f"robot:{occupancy.robot_id}",
                    "OCCUPIED_BY",
                    evidence_ids=[ev],
                )
            constraint_ids_by_edge[occupancy.edge_id].append(constraint_id)
        for edge_id in map_context.map_constraints.blocked_edge_ids:
            constraint_id = f"constraint:blocked:{edge_id}"
            ev = add_evidence("traffic_runtime", constraint_id, {"edge_id": edge_id, "status": "blocked"})
            add_node(
                constraint_id,
                "runtime_constraint",
                {"constraint_type": "BLOCKED", "edge_id": edge_id, "consumer": "CUOPT_AND_MAPF"},
                [ev],
            )
            self._add_edge_node(add_node, edge_id, add_evidence, snapshot)
            add_relation(
                f"rel:{constraint_id}:affects:{edge_id}",
                constraint_id,
                f"edge:{edge_id}",
                "AFFECTS",
                evidence_ids=[ev],
            )
            constraint_ids_by_edge[edge_id].append(constraint_id)

        # Graph-RAG path evidence uses service-only access nodes.  In legacy
        # mode the physical target is the outbound O_* node.  In G2P mode the
        # AMR target is a station access while O_* remains a logical destination.
        eligible_robots = [
            robot_by_id[rid]
            for rid in robots.candidate_robot_ids
            if rid in robot_by_id
        ]

        def register_path(path: SituationPathEvidence) -> None:
            paths[path.path_id] = path
            path_node_id = f"path_option:{path.path_id}"
            ev = add_evidence("warehouse_graph", path.path_id, path.model_dump(mode="json"))
            add_node(path_node_id, "path_option", path.model_dump(mode="json"), [ev])
            add_relation(
                f"rel:{path.path_id}:starts",
                path_node_id,
                f"map:{path.source_node_id}",
                "STARTS_AT",
                evidence_ids=[ev],
            )
            add_relation(
                f"rel:{path.path_id}:ends",
                path_node_id,
                f"map:{path.target_node_id}",
                "ENDS_AT",
                evidence_ids=[ev],
            )

        positive_stocks = [
            stock for stock in inventory.candidate_stocks if stock.available_qty > 0
        ]
        for stock in positive_stocks:
            for access_node_id in stock.access_node_ids:
                for robot in eligible_robots:
                    _, arcs = graph.shortest_path(
                        robot.current_node,
                        access_node_id,
                        metric="travel_time",
                    )
                    if not arcs and robot.current_node != access_node_id:
                        continue
                    path = self._path_evidence(
                        path_id=f"path:robot:{robot.robot_id}:to:{stock.stock_id}:{access_node_id}",
                        purpose="ROBOT_TO_PICKUP",
                        source=robot.current_node,
                        target=access_node_id,
                        arcs=arcs,
                        affected_constraints=constraint_ids_by_edge,
                    )
                    register_path(path)
                    ev = nodes[f"path_option:{path.path_id}"].evidence_ids
                    add_relation(
                        f"rel:{robot.robot_id}:can_reach:{stock.stock_id}:{access_node_id}",
                        f"robot:{robot.robot_id}",
                        f"stock:{stock.stock_id}",
                        "CAN_REACH",
                        attributes={
                            "path_id": path.path_id,
                            "access_node_id": access_node_id,
                            "travel_time_ms": path.travel_time_ms,
                            "cost": path.cost,
                        },
                        evidence_ids=ev,
                    )

        # Direct inbound work needs the same complete path evidence as legacy
        # outbound tasks.  The LLM may choose one authoritative handoff access
        # and one authoritative putaway access, but it may not invent either.
        for inbound_id in requested_inbound_ids:
            pickup_nodes = list(dict.fromkeys(inbound_pickup_nodes.get(inbound_id, [])))
            delivery_records = list(dict.fromkeys(inbound_delivery_nodes.get(inbound_id, [])))
            for pickup_node in pickup_nodes:
                for robot in eligible_robots:
                    _, arcs = graph.shortest_path(
                        robot.current_node, pickup_node, metric="travel_time"
                    )
                    if not arcs and robot.current_node != pickup_node:
                        continue
                    path = self._path_evidence(
                        path_id=f"path:robot:{robot.robot_id}:to:inbound:{inbound_id}:{pickup_node}",
                        purpose="ROBOT_TO_PICKUP",
                        source=robot.current_node,
                        target=pickup_node,
                        arcs=arcs,
                        affected_constraints=constraint_ids_by_edge,
                    )
                    register_path(path)
                    add_relation(
                        f"rel:{robot.robot_id}:can_reach:inbound:{inbound_id}:{pickup_node}",
                        f"robot:{robot.robot_id}",
                        f"inbound:{inbound_id}",
                        "CAN_REACH",
                        attributes={
                            "path_id": path.path_id,
                            "access_node_id": pickup_node,
                            "travel_time_ms": path.travel_time_ms,
                            "cost": path.cost,
                        },
                        evidence_ids=nodes[f"path_option:{path.path_id}"].evidence_ids,
                    )
                for rack_id, rack_level, delivery_node in delivery_records:
                    _, arcs = graph.shortest_path(
                        pickup_node, delivery_node, metric="travel_time"
                    )
                    if not arcs and pickup_node != delivery_node:
                        continue
                    register_path(
                        self._path_evidence(
                            path_id=(
                                f"path:inbound:{inbound_id}:{pickup_node}:to:"
                                f"{rack_id}:L{rack_level}:{delivery_node}"
                            ),
                            purpose="PICKUP_TO_DELIVERY",
                            source=pickup_node,
                            target=delivery_node,
                            arcs=arcs,
                            affected_constraints=constraint_ids_by_edge,
                        )
                    )

        if g2p_mode:
            logical_destinations = sorted({need.delivery_node for need in inventory.task_needs})
            station_records = self.repository.outbound_station_candidates(logical_destinations)
            station_accesses = {
                str(station["station_id"]): [
                    (
                        str(value),
                        self.repository.mobile_handoff_node_for_station_access(str(value)),
                    )
                    for value in station.get("access_node_ids", [])
                ]
                for station in station_records
            }
            empty_buffer_accesses = [
                str(access)
                for buffer in self.repository.empty_tote_buffer_candidates()
                for access in buffer.get("access_node_ids", [])
            ]
            for stock in positive_stocks:
                for source_access in stock.access_node_ids:
                    for station_id, access_ids in station_accesses.items():
                        for station_access, mobile_handoff in access_ids:
                            _, arcs = graph.shortest_path(
                                source_access,
                                mobile_handoff,
                                metric="travel_time",
                            )
                            if arcs or source_access == mobile_handoff:
                                path = self._path_evidence(
                                    path_id=(
                                        f"path:g2p:{stock.stock_id}:{source_access}:"
                                        f"to:{station_id}:{mobile_handoff}"
                                    ),
                                    purpose="PICKUP_TO_STATION",
                                    source=source_access,
                                    target=mobile_handoff,
                                    arcs=arcs,
                                    affected_constraints=constraint_ids_by_edge,
                                )
                                register_path(path)
                                add_relation(
                                    f"rel:{path.path_id}:station",
                                    f"path_option:{path.path_id}",
                                    f"station:{station_id}",
                                    "ROUTES_THROUGH",
                                    attributes={
                                        "station_access_node": station_access,
                                        "mobile_handoff_node": mobile_handoff,
                                    },
                                    evidence_ids=nodes[f"path_option:{path.path_id}"].evidence_ids,
                                )

                            # A positive remainder returns to the same source
                            # access; the path is execution evidence, not a new
                            # solver assignment.
                            _, return_arcs = graph.shortest_path(
                                mobile_handoff,
                                source_access,
                                metric="travel_time",
                            )
                            if return_arcs or mobile_handoff == source_access:
                                return_path = self._path_evidence(
                                    path_id=(
                                        f"path:g2p:{station_id}:{mobile_handoff}:"
                                        f"return:{stock.stock_id}:{source_access}"
                                    ),
                                    purpose="STATION_TO_POST_MOVE",
                                    source=mobile_handoff,
                                    target=source_access,
                                    arcs=return_arcs,
                                    affected_constraints=constraint_ids_by_edge,
                                )
                                register_path(return_path)
                    for station_id, access_ids in station_accesses.items():
                        for station_access, mobile_handoff in access_ids:
                            for buffer_access in empty_buffer_accesses:
                                path_id = (
                                    f"path:g2p:{station_id}:{mobile_handoff}:"
                                    f"empty:{buffer_access}"
                                )
                                if path_id in paths:
                                    continue
                                _, buffer_arcs = graph.shortest_path(
                                    mobile_handoff,
                                    buffer_access,
                                    metric="travel_time",
                                )
                                if buffer_arcs or mobile_handoff == buffer_access:
                                    register_path(
                                        self._path_evidence(
                                            path_id=path_id,
                                            purpose="STATION_TO_POST_MOVE",
                                            source=mobile_handoff,
                                            target=buffer_access,
                                            arcs=buffer_arcs,
                                            affected_constraints=constraint_ids_by_edge,
                                        )
                                    )
        else:
            for need in inventory.task_needs:
                for stock in stocks_by_item.get(need.item_id, []):
                    if stock.available_qty < need.required_qty:
                        continue
                    for access_node_id in stock.access_node_ids:
                        _, pickup_to_drop_arcs = graph.shortest_path(
                            access_node_id,
                            need.delivery_node,
                            metric="travel_time",
                        )
                        if pickup_to_drop_arcs or access_node_id == need.delivery_node:
                            register_path(
                                self._path_evidence(
                                    path_id=(
                                        f"path:stock:{stock.stock_id}:{access_node_id}:"
                                        f"to:{need.delivery_node}"
                                    ),
                                    purpose="PICKUP_TO_DELIVERY",
                                    source=access_node_id,
                                    target=need.delivery_node,
                                    arcs=pickup_to_drop_arcs,
                                    affected_constraints=constraint_ids_by_edge,
                                )
                            )

        missing: list[str] = []
        outbound_facts_complete = all(
            order_id in needs_by_order for order_id in requested_order_ids
        )
        inbound_facts_complete = all(
            inbound_id in inbound_needs_by_id for inbound_id in requested_inbound_ids
        )
        order_facts_complete = outbound_facts_complete and inbound_facts_complete
        if not outbound_facts_complete:
            missing.extend(
                f"Order {order_id} is missing from the inventory snapshot."
                for order_id in requested_order_ids
                if order_id not in needs_by_order
            )
        if not inbound_facts_complete:
            missing.extend(
                f"Inbound receipt {inbound_id} is missing from the inventory snapshot."
                for inbound_id in requested_inbound_ids
                if inbound_id not in inbound_needs_by_id
            )

        inventory_complete = True
        map_paths_complete = True
        if g2p_mode:
            required_by_item: dict[str, int] = defaultdict(int)
            destinations_by_item: dict[str, set[str]] = defaultdict(set)
            orders_by_item: dict[str, list[str]] = defaultdict(list)
            for order_id in requested_order_ids:
                need = needs_by_order.get(order_id)
                if need is None:
                    continue
                required_by_item[need.item_id] += need.required_qty
                destinations_by_item[need.item_id].add(need.delivery_node)
                orders_by_item[need.item_id].append(order_id)

            station_serves = {
                relation.target_node_id.removeprefix("destination:")
                for relation in relations.values()
                if relation.relation_type == "SERVES_DESTINATION"
            }
            empty_buffer_accesses = {
                str(value)
                for buffer in self.repository.empty_tote_buffer_candidates()
                for value in buffer.get("access_node_ids", [])
            }
            for item_id, required_qty in required_by_item.items():
                candidates = [
                    stock
                    for stock in stocks_by_item.get(item_id, [])
                    if stock.available_qty > 0
                ]
                total_available = sum(stock.available_qty for stock in candidates)
                if total_available < required_qty:
                    inventory_complete = False
                    missing.append(
                        f"Item {item_id} requires {required_qty} unit(s) but only "
                        f"{total_available} are available across handling units."
                    )
                access_nodes = {
                    access
                    for stock in candidates
                    for access in stock.access_node_ids
                }
                has_robot_path = any(
                    path.purpose == "ROBOT_TO_PICKUP"
                    and path.target_node_id in access_nodes
                    for path in paths.values()
                )
                has_station_path = any(
                    path.purpose == "PICKUP_TO_STATION"
                    and path.source_node_id in access_nodes
                    for path in paths.values()
                )
                has_return_path = any(
                    path.purpose == "STATION_TO_POST_MOVE"
                    and path.target_node_id in access_nodes
                    for path in paths.values()
                )
                has_empty_path = bool(empty_buffer_accesses) and any(
                    path.purpose == "STATION_TO_POST_MOVE"
                    and path.target_node_id in empty_buffer_accesses
                    for path in paths.values()
                )
                destinations_served = destinations_by_item[item_id].issubset(station_serves)
                if not (
                    has_robot_path
                    and has_station_path
                    and has_return_path
                    and has_empty_path
                    and destinations_served
                ):
                    map_paths_complete = False
                    missing.append(
                        f"G2P item {item_id} lacks complete robot-to-handling-unit, "
                        "handling-unit-to-station, station service, or post-station path evidence."
                    )
        else:
            for order_id in requested_order_ids:
                need = needs_by_order.get(order_id)
                if need is None:
                    continue
                candidates = [
                    stock
                    for stock in stocks_by_item.get(need.item_id, [])
                    if stock.available_qty > 0
                ]
                if not any(stock.available_qty >= need.required_qty for stock in candidates):
                    inventory_complete = False
                    missing.append(
                        f"Order {order_id} has no single rack level with sufficient inventory "
                        "for the legacy task model."
                    )
                has_path = any(
                    path.purpose == "PICKUP_TO_DELIVERY"
                    and path.target_node_id == need.delivery_node
                    and any(path.source_node_id in stock.access_node_ids for stock in candidates)
                    for path in paths.values()
                )
                has_robot_path = any(
                    path.purpose == "ROBOT_TO_PICKUP"
                    and any(path.target_node_id in stock.access_node_ids for stock in candidates)
                    for path in paths.values()
                )
                if not has_path or not has_robot_path:
                    map_paths_complete = False
                    missing.append(
                        f"Order {order_id} has no complete robot-pickup-delivery path evidence."
                    )

        for inbound_id in requested_inbound_ids:
            need = inbound_needs_by_id.get(inbound_id)
            if need is None:
                continue
            pickups = set(inbound_pickup_nodes.get(inbound_id, []))
            deliveries = {value[2] for value in inbound_delivery_nodes.get(inbound_id, [])}
            if not pickups or not deliveries:
                inventory_complete = False
                missing.append(
                    f"Inbound receipt {inbound_id} has no authoritative handoff or putaway candidate."
                )
                continue
            has_robot_path = any(
                path.purpose == "ROBOT_TO_PICKUP"
                and path.target_node_id in pickups
                and f":inbound:{inbound_id}:" in path.path_id
                for path in paths.values()
            )
            has_delivery_path = any(
                path.purpose == "PICKUP_TO_DELIVERY"
                and path.source_node_id in pickups
                and path.target_node_id in deliveries
                and path.path_id.startswith(f"path:inbound:{inbound_id}:")
                for path in paths.values()
            )
            if not has_robot_path or not has_delivery_path:
                map_paths_complete = False
                missing.append(
                    f"Inbound receipt {inbound_id} lacks complete robot-to-handoff or "
                    "handoff-to-putaway path evidence."
                )

        robot_complete = bool(robots.candidate_robot_ids)
        if not robot_complete:
            missing.append("No baseline-eligible robot is available.")
        runtime_complete = not map_context.missing_info and not unknown_requested_edges
        if map_context.missing_info:
            missing.extend(map_context.missing_info)
        if unknown_requested_edges:
            missing.append(
                "Requested map constraints reference unknown edge(s): "
                + ", ".join(sorted(set(unknown_requested_edges)))
            )

        completeness = SituationGraphCompleteness(
            order_facts_complete=order_facts_complete,
            inventory_candidates_complete=inventory_complete,
            robot_candidates_complete=robot_complete,
            map_paths_complete=map_paths_complete,
            runtime_constraints_complete=runtime_complete,
            missing_information=list(
                dict.fromkeys([*inventory.missing_info, *robots.missing_info, *missing])
            ),
            truncated_sections=[],
            ready_for_formulation=(
                order_facts_complete
                and inventory_complete
                and robot_complete
                and map_paths_complete
                and runtime_complete
                and not inventory.missing_info
                and not robots.missing_info
            ),
        )
        return WarehouseSituationGraph(
            fulfillment_mode=("goods_to_person" if g2p_mode else "legacy_order_tasks"),
            g2p_order_ids=(requested_order_ids if g2p_mode else []),
            snapshot_id=snapshot.snapshot_id,
            captured_at=snapshot.captured_at,
            graph_version=snapshot.graph_version,
            inventory_version=snapshot.inventory_version,
            runtime_version=snapshot.runtime_version,
            request_anchor_ids=list(dict.fromkeys(request_anchor_ids)),
            nodes=sorted(nodes.values(), key=lambda value: value.node_id),
            relations=sorted(relations.values(), key=lambda value: value.relation_id),
            path_evidence=sorted(paths.values(), key=lambda value: value.path_id),
            evidence_index=sorted(evidence.values(), key=lambda value: value.evidence_id),
            completeness=completeness,
            summary=(
                f"Situation graph mode={'goods_to_person' if g2p_mode else 'legacy_order_tasks'} contains "
                f"{len(nodes)} entity node(s), {len(relations)} relation(s), "
                f"{len(paths)} path evidence record(s), and {len(evidence)} source evidence record(s)."
            ),
        )

    def _add_map_node(self, *, add_node, node_id: str, evidence, snapshot: ContextSnapshot) -> None:
        """Add one validated entity or relation to the accumulating result."""
        record = self.repository.node(node_id)
        if record is None:
            return
        ev = evidence("warehouse_graph", node_id, record)
        node_type = str(record.get("type", "route"))
        situation_type = {
            "rack_access": "rack_access",
            "outbound": "outbound",
            "outbound_station_access": "outbound_station_access",
            "empty_tote_buffer_access": "empty_tote_buffer_access",
            "inbound": "inbound",
            "inbound_handoff_access": "inbound_handoff_access",
            "charging_slot": "charging_slot",
        }.get(node_type, "route_node")
        add_node(
            f"map:{node_id}",
            situation_type,
            {**record, "graph_version": snapshot.graph_version},
            [ev],
        )

    def _add_edge_node(self, add_node, edge_id: str, evidence, snapshot: ContextSnapshot) -> None:
        """Add one validated entity or relation to the accumulating result."""
        record = self.repository.edge(edge_id)
        if record is None:
            return
        ev = evidence("warehouse_graph", edge_id, record)
        add_node(
            f"edge:{edge_id}",
            "edge",
            {**record, "graph_version": snapshot.graph_version},
            [ev],
        )

    @staticmethod
    def _path_evidence(
        *,
        path_id: str,
        purpose: str,
        source: str,
        target: str,
        arcs,
        affected_constraints: dict[str, list[str]],
    ) -> SituationPathEvidence:
        """Return evidence identifiers associated with the requested path relation."""
        edges = [arc.edge_id for arc in arcs]
        nodes = [source, *[arc.target for arc in arcs]]
        return SituationPathEvidence(
            path_id=path_id,
            purpose=purpose,
            source_node_id=source,
            target_node_id=target,
            node_sequence=nodes,
            edge_sequence=edges,
            cost=round(sum(arc.cost for arc in arcs), 6),
            travel_time_ms=sum(arc.travel_time_ms for arc in arcs),
            affected_constraint_ids=list(
                dict.fromkeys(
                    constraint_id
                    for edge_id in edges
                    for constraint_id in affected_constraints.get(edge_id, [])
                )
            ),
        )


class WarehouseSituationGraphValidator:
    """Validate graph structure, evidence integrity, and scoped completeness."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        """Initialize this service with its validated dependencies."""
        self.repository = repository or get_repository()

    def validate(self, graph: WarehouseSituationGraph) -> SituationGraphValidationResult:
        """Validate the supplied contract and return structured findings."""
        errors: list[str] = []
        warnings: list[str] = []
        node_ids = [node.node_id for node in graph.nodes]
        relation_ids = [relation.relation_id for relation in graph.relations]
        path_ids = [path.path_id for path in graph.path_evidence]
        evidence_ids = [value.evidence_id for value in graph.evidence_index]
        for name, values in {
            "node": node_ids,
            "relation": relation_ids,
            "path": path_ids,
            "evidence": evidence_ids,
        }.items():
            if len(values) != len(set(values)):
                errors.append(f"Duplicate {name} identifiers were found.")
        node_set = set(node_ids)
        evidence_set = set(evidence_ids)
        for relation in graph.relations:
            if relation.source_node_id not in node_set:
                errors.append(f"Relation {relation.relation_id} has unknown source {relation.source_node_id}.")
            if relation.target_node_id not in node_set:
                errors.append(f"Relation {relation.relation_id} has unknown target {relation.target_node_id}.")
            unknown = set(relation.evidence_ids) - evidence_set
            if unknown:
                errors.append(f"Relation {relation.relation_id} references unknown evidence {sorted(unknown)}.")
        for node in graph.nodes:
            unknown = set(node.evidence_ids) - evidence_set
            if unknown:
                errors.append(f"Node {node.node_id} references unknown evidence {sorted(unknown)}.")
        for path in graph.path_evidence:
            if path.node_sequence[0] != path.source_node_id or path.node_sequence[-1] != path.target_node_id:
                errors.append(f"Path {path.path_id} endpoints do not match the node sequence.")
            if len(path.edge_sequence) != max(0, len(path.node_sequence) - 1):
                errors.append(f"Path {path.path_id} edge and node sequence lengths do not match.")
                continue
            for index, edge_id in enumerate(path.edge_sequence):
                edge = self.repository.edge(edge_id)
                if edge is None:
                    errors.append(f"Path {path.path_id} references unknown edge {edge_id}.")
                    continue
                if edge["source"] != path.node_sequence[index] or edge["target"] != path.node_sequence[index + 1]:
                    errors.append(f"Path {path.path_id} is discontinuous at edge {edge_id}.")
        versions = self.repository.versions
        if graph.graph_version != versions["graph_version"]:
            errors.append("Situation graph graph_version does not match the repository snapshot.")
        if graph.inventory_version != versions["inventory_version"]:
            errors.append("Situation graph inventory_version does not match the repository snapshot.")
        if graph.runtime_version != versions["runtime_version"]:
            errors.append("Situation graph runtime_version does not match the repository snapshot.")
        if not graph.completeness.ready_for_formulation:
            errors.extend(graph.completeness.missing_information)
        if graph.completeness.truncated_sections:
            errors.append(
                "Situation graph contains truncated sections: "
                + ", ".join(graph.completeness.truncated_sections)
            )
        if not graph.request_anchor_ids:
            warnings.append("Situation graph has no explicit request anchors.")
        return SituationGraphValidationResult(valid=not errors, errors=list(dict.fromkeys(errors)), warnings=warnings)

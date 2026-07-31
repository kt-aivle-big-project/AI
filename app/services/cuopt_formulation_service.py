"""Rule and LLM-facing cuOpt dynamic-input formulation and validation.

The LLM or rule formulator chooses the dynamic business content: tasks, stock
locations, fleet inclusion, objective, and runtime constraints.  A later
mechanical payload builder only adds integer indices and the fixed graph arrays.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.core.config import get_settings
from app.services.graph_service import DirectedGraphService
from app.services.rack_access_service import choose_best_access_node
from app.repositories.json_repository import get_repository
from app.domain.schemas import (
    CuOptDynamicInputDraft,
    CuOptDynamicInputValidationResult,
    CuOptEvidenceEnrichmentResult,
    CuOptFleetDraft,
    CuOptMapConstraintDraft,
    CuOptTaskDraft,
    ContextSnapshot,
    InventoryContext,
    RobotRuntimeContext,
    EdgePenalty,
    MapConstraints,
    MapContext,
    NormalizedWarehouseRequest,
    OptimizationRequest,
    OptimizationTask,
    OptimizationVehicle,
    WarehouseSituationGraph,
)


def _conditional_edge_policy_ids(request: NormalizedWarehouseRequest) -> set[str]:
    """Return edges whose final hard/soft state depends on runtime evidence.

    Canonical normalization records these edges as soft candidates so the
    retrieval plan includes them.  That provisional representation must not
    force the final cuOpt draft to remain soft: the Agent may select one of the
    explicitly allowed actions after reading runtime context.
    """

    return {value.edge_id for value in request.constraints.conditional_edge_policies}


def _objective_terms(request: NormalizedWarehouseRequest) -> list[str]:
    """Return an auditable objective-term list for the dynamic draft."""

    explicit = list(dict.fromkeys(request.constraints.objective_terms))
    if explicit:
        return explicit
    mapped = {
        "MIN_COMPLETION_TIME": "MIN_COMPLETION_TIME",
        "THROUGHPUT": "MAX_THROUGHPUT",
        "BALANCED": "MIN_COMPLETION_TIME",
        "URGENT_FIRST": "MIN_COMPLETION_TIME",
        "MIN_REHANDLE": "MIN_TRAVEL_DISTANCE",
    }
    return [mapped[request.constraints.objective_profile]]


def _apply_emergency_reserve(
    *,
    candidate_robot_ids: list[str],
    battery_by_robot: dict[str, float],
    request: NormalizedWarehouseRequest,
) -> tuple[list[str], list[str]]:
    """Deterministically split eligible robots into active and reserve fleets.

    The highest-battery eligible robots are kept as reserve.  At least one
    robot remains active; if the requested reserve is infeasible the validator
    reports the exact mismatch rather than silently weakening the constraint.
    """

    candidates = list(dict.fromkeys(candidate_robot_ids))
    requested = int(request.constraints.reserve_robot_count or 0)
    if requested <= 0 or len(candidates) <= 1:
        return sorted(candidates), []
    threshold = request.constraints.reserve_robot_min_battery_pct
    pool = [
        robot_id
        for robot_id in candidates
        if threshold is None or float(battery_by_robot.get(robot_id, -1.0)) >= float(threshold)
    ]
    ranked = sorted(
        pool,
        key=lambda robot_id: (-float(battery_by_robot.get(robot_id, -1.0)), robot_id),
    )
    reserve_count = min(requested, max(0, len(candidates) - 1), len(ranked))
    reserved = ranked[:reserve_count]
    included = [robot_id for robot_id in candidates if robot_id not in set(reserved)]
    return sorted(included), sorted(reserved)




def _compare_runtime_value(value: int, operator: str, threshold: int) -> bool:
    """Evaluate one typed integer predicate without LLM involvement."""

    return {
        "GT": value > threshold,
        "GTE": value >= threshold,
        "LT": value < threshold,
        "LTE": value <= threshold,
    }[operator]


def _selected_conditional_action(policy, expected_wait_ms: int) -> str:
    """Return the declared branch selected by current runtime evidence."""

    return policy.when_true if _compare_runtime_value(
        expected_wait_ms, policy.operator, policy.threshold_ms
    ) else policy.when_false


def _expected_wait_from_map_context(map_context: MapContext, edge_id: str) -> int:
    """Conservatively estimate the next safe wait for one edge."""

    intervals = [
        int(value.occupied_until_ms)
        for value in map_context.map_constraints.edge_occupancies
        if value.edge_id == edge_id
    ] + [
        int(value.end_at_ms)
        for value in map_context.map_constraints.edge_reservations
        if value.edge_id == edge_id
    ]
    return max(intervals, default=0)


def _apply_conditional_policies(
    *,
    request: NormalizedWarehouseRequest,
    expected_wait_by_edge: dict[str, int],
    blocked: set[str],
    soft: set[str],
) -> None:
    """Resolve simple typed conditional policies deterministically."""

    for policy in request.constraints.conditional_edge_policies:
        edge_id = policy.edge_id
        blocked.discard(edge_id)
        soft.discard(edge_id)
        selected = _selected_conditional_action(
            policy, expected_wait_by_edge.get(edge_id, 0)
        )
        if selected == "HARD_AVOID":
            blocked.add(edge_id)
        elif selected == "SOFT_AVOID":
            soft.add(edge_id)

def _validate_conditional_edge_policies(
    *,
    request: NormalizedWarehouseRequest,
    blocked_edge_ids: set[str],
    soft_edge_ids: set[str],
    errors: list[str],
) -> None:
    """Validate the resolved action for each typed conditional policy.

    Rule evaluates a single typed predicate directly; Agent may compose several
    interacting policies. The validator verifies the common contract boundary:
    an edge is represented by exactly one action and that
    action is one of ``when_true``/``when_false`` declared in the normalized
    request.  Runtime evidence and the LLM explanation are validated elsewhere.
    """

    for policy in request.constraints.conditional_edge_policies:
        edge_id = policy.edge_id
        is_blocked = edge_id in blocked_edge_ids
        is_soft = edge_id in soft_edge_ids
        if is_blocked and is_soft:
            errors.append(f"CONDITIONAL_EDGE_MULTIPLE_ACTIONS:{edge_id}")
            continue
        selected_action = "HARD_AVOID" if is_blocked else "SOFT_AVOID" if is_soft else "ALLOW"
        allowed_actions = {policy.when_true, policy.when_false}
        if selected_action not in allowed_actions:
            errors.append(
                f"CONDITIONAL_EDGE_POLICY_MISMATCH:{edge_id}:"
                f"selected={selected_action};allowed={','.join(sorted(allowed_actions))}"
            )


class RuleCuOptFormulator:
    """Create the dynamic cuOpt request deterministically from situation evidence."""

    def formulate(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        graph: WarehouseSituationGraph,
        time_limit_seconds: int,
    ) -> CuOptDynamicInputDraft:
        """Formulate a grounded optimization input from the situation graph."""
        if graph.fulfillment_mode == "goods_to_person":
            return self._formulate_g2p_wave(
                normalized_request=normalized_request,
                graph=graph,
                time_limit_seconds=time_limit_seconds,
            )

        node_by_id = {node.node_id: node for node in graph.nodes}
        relations = graph.relations
        paths = graph.path_evidence
        stock_by_item: dict[str, list] = defaultdict(list)
        item_by_order: dict[str, str] = {}
        for relation in relations:
            if relation.relation_type == "REQUIRES_ITEM":
                item_by_order[relation.source_node_id.removeprefix("order:")] = relation.target_node_id.removeprefix("item:")
            elif relation.relation_type == "OF_ITEM":
                stock_id = relation.source_node_id.removeprefix("stock:")
                item_id = relation.target_node_id.removeprefix("item:")
                stock_node = node_by_id.get(f"stock:{stock_id}")
                if stock_node is not None:
                    stock_by_item[item_id].append(stock_node)

        path_by_pair = {(path.source_node_id, path.target_node_id): path for path in paths}
        robot_paths_by_pickup: dict[str, list] = defaultdict(list)
        station_paths_by_pickup: dict[str, list] = defaultdict(list)
        for path in paths:
            if path.purpose == "ROBOT_TO_PICKUP":
                robot_paths_by_pickup[path.target_node_id].append(path)
            elif path.purpose == "PICKUP_TO_STATION":
                station_paths_by_pickup[path.source_node_id].append(path)
        g2p_mode = graph.fulfillment_mode == "goods_to_person"
        station_by_access: dict[str, str] = {}
        destinations_by_station: dict[str, set[str]] = defaultdict(set)
        for relation in relations:
            if relation.relation_type == "HAS_ACCESS_POINT" and relation.source_node_id.startswith("station:"):
                station_by_access[relation.target_node_id.removeprefix("map:")] = relation.source_node_id.removeprefix("station:")
            elif relation.relation_type == "SERVES_DESTINATION" and relation.source_node_id.startswith("station:"):
                destinations_by_station[relation.source_node_id.removeprefix("station:")].add(
                    relation.target_node_id.removeprefix("destination:")
                )

        remaining_by_stock = {
            node.attributes["stock_id"]: int(node.attributes["available_qty"])
            for node in graph.nodes
            if node.node_type == "stock"
        }
        tasks: list[CuOptTaskDraft] = []
        deferred: list[str] = []
        requested_orders = [
            operation.operation_id
            for operation in normalized_request.operations
            if operation.operation_type == "OUTBOUND_ORDER"
        ]
        for index, order_id in enumerate(requested_orders, start=1):
            order_node = node_by_id.get(f"order:{order_id}")
            if order_node is None:
                deferred.append(order_id)
                continue
            item_id = item_by_order.get(order_id, str(order_node.attributes.get("item_id", "")))
            demand = int(order_node.attributes["required_qty"])
            destination = str(order_node.attributes["delivery_node"])
            candidates: list[tuple[int, float, Any, str, Any, Any]] = []
            for stock_node in stock_by_item.get(item_id, []):
                stock_id = str(stock_node.attributes["stock_id"])
                if remaining_by_stock.get(stock_id, 0) < demand:
                    continue
                for pickup in [str(value) for value in stock_node.attributes.get("access_node_ids", [])]:
                    robot_paths = robot_paths_by_pickup.get(pickup, [])
                    if not robot_paths:
                        continue
                    best_robot_path = min(
                        robot_paths,
                        key=lambda value: (value.travel_time_ms, value.cost, value.path_id),
                    )
                    if g2p_mode:
                        delivery_paths = [
                            path
                            for path in station_paths_by_pickup.get(pickup, [])
                            if destination in destinations_by_station.get(
                                station_by_access.get(path.target_node_id, ""), set()
                            )
                        ]
                    else:
                        direct_path = path_by_pair.get((pickup, destination))
                        delivery_paths = [direct_path] if direct_path is not None else []
                    for delivery_path in delivery_paths:
                        score = best_robot_path.travel_time_ms + delivery_path.travel_time_ms
                        candidates.append((
                            score,
                            delivery_path.cost + best_robot_path.cost,
                            stock_node,
                            pickup,
                            best_robot_path,
                            delivery_path,
                        ))
            if not candidates:
                deferred.append(order_id)
                continue
            _, _, selected, pickup, robot_path, delivery_path = min(
                candidates,
                key=lambda value: (value[0], value[1], value[2].node_id, value[3]),
            )
            stock_id = str(selected.attributes["stock_id"])
            remaining_by_stock[stock_id] -= demand
            evidence_ids = list(
                dict.fromkeys(
                    [
                        *order_node.evidence_ids,
                        *selected.evidence_ids,
                        *self._path_evidence_ids(graph, robot_path.path_id),
                        *self._path_evidence_ids(graph, delivery_path.path_id),
                    ]
                )
            )
            tasks.append(
                CuOptTaskDraft(
                    task_id=f"TASK-{index:03d}",
                    order_id=order_id,
                    item_id=item_id,
                    stock_id=stock_id,
                    rack_id=str(selected.attributes["rack_id"]),
                    rack_level=int(selected.attributes["rack_level"]),
                    pickup_node=pickup,
                    delivery_node=(delivery_path.target_node_id if g2p_mode else destination),
                    demand=demand,
                    priority=str(order_node.attributes["priority"]),
                    mandatory=True,
                    fixed_vehicle_id=None,
                    evidence_ids=evidence_ids,
                )
            )

        eligible_robot_nodes = [
            node
            for node in graph.nodes
            if node.node_type == "robot" and bool(node.attributes.get("baseline_eligible"))
        ]
        explicit_exclusions = set(normalized_request.constraints.excluded_robot_ids)
        candidate_robot_ids = sorted(
            str(node.attributes["robot_id"])
            for node in eligible_robot_nodes
            if str(node.attributes["robot_id"]) not in explicit_exclusions
        )
        battery_by_robot = {
            str(node.attributes["robot_id"]): float(node.attributes.get("battery_pct") or 0.0)
            for node in eligible_robot_nodes
        }
        included_robot_ids, reserved_robot_ids = _apply_emergency_reserve(
            candidate_robot_ids=candidate_robot_ids,
            battery_by_robot=battery_by_robot,
            request=normalized_request,
        )
        relevant_robot_ids = set(included_robot_ids) | set(reserved_robot_ids) | explicit_exclusions
        robot_evidence = list(
            dict.fromkeys(
                evidence_id
                for node in graph.nodes
                if node.node_type == "robot"
                and str(node.attributes.get("robot_id")) in relevant_robot_ids
                for evidence_id in node.evidence_ids
            )
        )
        blocked = sorted(
            {
                str(node.attributes["edge_id"])
                for node in graph.nodes
                if node.node_type == "runtime_constraint"
                and node.attributes.get("constraint_type") == "BLOCKED"
            }
            | set(normalized_request.constraints.hard_block_edge_ids)
        )
        runtime_penalties = {
            str(node.attributes["edge_id"])
            for node in graph.nodes
            if node.node_type == "runtime_constraint"
            and node.attributes.get("constraint_type") == "CONGESTED"
        }
        blocked_set = set(blocked)
        soft_set = runtime_penalties | set(normalized_request.constraints.soft_avoid_edge_ids)
        expected_wait_by_edge: dict[str, int] = {}
        for node in graph.nodes:
            if node.node_type != "runtime_constraint":
                continue
            edge_id = node.attributes.get("edge_id")
            if not edge_id:
                continue
            expected_wait_by_edge[str(edge_id)] = max(
                expected_wait_by_edge.get(str(edge_id), 0),
                int(node.attributes.get("occupied_until_ms") or node.attributes.get("end_at_ms") or 0),
            )
        _apply_conditional_policies(
            request=normalized_request,
            expected_wait_by_edge=expected_wait_by_edge,
            blocked=blocked_set,
            soft=soft_set,
        )
        blocked = sorted(blocked_set)
        soft_penalties = sorted(soft_set - blocked_set)
        constraint_evidence = list(
            dict.fromkeys(
                evidence_id
                for node in graph.nodes
                if node.node_type == "runtime_constraint"
                and node.attributes.get("constraint_type") in {
                    "BLOCKED",
                    "CONGESTED",
                    "REQUESTED_HARD_BLOCK",
                    "REQUESTED_SOFT_AVOID",
                }
                for evidence_id in node.evidence_ids
            )
        )
        if graph.fulfillment_mode == "goods_to_person":
            tasks = []
            deferred = []
        return CuOptDynamicInputDraft(
            formulation_mode=(
                "GOODS_TO_PERSON"
                if graph.fulfillment_mode == "goods_to_person"
                else "ORDER_TASKS"
            ),
            g2p_order_ids=(requested_orders if graph.fulfillment_mode == "goods_to_person" else []),
            snapshot_id=graph.snapshot_id,
            graph_version=graph.graph_version,
            formulation_source="rule",
            objective_profile=normalized_request.constraints.objective_profile,
            objective_terms=_objective_terms(normalized_request),
            tasks=tasks,
            deferred_order_ids=sorted(set(deferred)),
            fleet=CuOptFleetDraft(
                included_robot_ids=included_robot_ids,
                excluded_robot_ids=sorted(explicit_exclusions),
                reserved_robot_ids=reserved_robot_ids,
                evidence_ids=robot_evidence,
            ),
            map_constraints=CuOptMapConstraintDraft(
                blocked_edge_ids=blocked,
                soft_penalty_edge_ids=soft_penalties,
                max_edge_wait_ms=normalized_request.constraints.max_edge_wait_ms,
                evidence_ids=constraint_evidence,
            ),
            time_limit_seconds=time_limit_seconds,
            formulation_summary=(
                f"Rule formulation created {len(tasks)} task(s), deferred {len(deferred)} order(s), "
                f"included {len(included_robot_ids)} robot(s), and reserved "
                f"{len(reserved_robot_ids)} robot(s)."
            ),
        )

    def _formulate_g2p_wave(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        graph: WarehouseSituationGraph,
        time_limit_seconds: int,
    ) -> CuOptDynamicInputDraft:
        """Preserve one canonical outbound wave for deterministic HU compilation.

        The Agent/Rule formulation boundary decides fleet, objective, and runtime
        constraints.  It deliberately does *not* assign one stock to each order:
        the downstream G2P compiler owns handling-unit aggregation and physical
        rack/station task creation.
        """

        requested_orders = [
            operation.operation_id
            for operation in normalized_request.operations
            if operation.operation_type == "OUTBOUND_ORDER"
        ]
        eligible_robot_nodes = [
            node
            for node in graph.nodes
            if node.node_type == "robot" and bool(node.attributes.get("baseline_eligible"))
        ]
        explicit_exclusions = set(normalized_request.constraints.excluded_robot_ids)
        candidate_robot_ids = sorted(
            str(node.attributes["robot_id"])
            for node in eligible_robot_nodes
            if str(node.attributes["robot_id"]) not in explicit_exclusions
        )
        battery_by_robot = {
            str(node.attributes["robot_id"]): float(node.attributes.get("battery_pct") or 0.0)
            for node in eligible_robot_nodes
        }
        included_robot_ids, reserved_robot_ids = _apply_emergency_reserve(
            candidate_robot_ids=candidate_robot_ids,
            battery_by_robot=battery_by_robot,
            request=normalized_request,
        )
        relevant_robot_ids = set(included_robot_ids) | set(reserved_robot_ids) | explicit_exclusions
        robot_evidence = list(
            dict.fromkeys(
                evidence_id
                for node in graph.nodes
                if node.node_type == "robot"
                and str(node.attributes.get("robot_id")) in relevant_robot_ids
                for evidence_id in node.evidence_ids
            )
        )

        blocked_set = {
            str(node.attributes["edge_id"])
            for node in graph.nodes
            if node.node_type == "runtime_constraint"
            and node.attributes.get("constraint_type") == "BLOCKED"
        } | set(normalized_request.constraints.hard_block_edge_ids)
        soft_set = {
            str(node.attributes["edge_id"])
            for node in graph.nodes
            if node.node_type == "runtime_constraint"
            and node.attributes.get("constraint_type") == "CONGESTED"
        } | set(normalized_request.constraints.soft_avoid_edge_ids)
        expected_wait_by_edge: dict[str, int] = {}
        for node in graph.nodes:
            if node.node_type != "runtime_constraint":
                continue
            edge_id = node.attributes.get("edge_id")
            if not edge_id:
                continue
            expected_wait_by_edge[str(edge_id)] = max(
                expected_wait_by_edge.get(str(edge_id), 0),
                int(node.attributes.get("occupied_until_ms") or node.attributes.get("end_at_ms") or 0),
            )
        _apply_conditional_policies(
            request=normalized_request,
            expected_wait_by_edge=expected_wait_by_edge,
            blocked=blocked_set,
            soft=soft_set,
        )
        soft_set -= blocked_set
        constrained_edges = blocked_set | soft_set
        constraint_evidence = list(
            dict.fromkeys(
                evidence_id
                for node in graph.nodes
                if (
                    node.node_type in {"runtime_constraint", "edge"}
                    and str(node.attributes.get("edge_id") or node.attributes.get("id"))
                    in constrained_edges
                )
                for evidence_id in node.evidence_ids
            )
        )
        return CuOptDynamicInputDraft(
            formulation_mode="GOODS_TO_PERSON",
            g2p_order_ids=list(dict.fromkeys(requested_orders)),
            snapshot_id=graph.snapshot_id,
            graph_version=graph.graph_version,
            formulation_source="rule",
            objective_profile=normalized_request.constraints.objective_profile,
            objective_terms=_objective_terms(normalized_request),
            tasks=[],
            deferred_order_ids=[],
            fleet=CuOptFleetDraft(
                included_robot_ids=included_robot_ids,
                excluded_robot_ids=sorted(explicit_exclusions),
                reserved_robot_ids=reserved_robot_ids,
                evidence_ids=robot_evidence,
            ),
            map_constraints=CuOptMapConstraintDraft(
                blocked_edge_ids=sorted(blocked_set),
                soft_penalty_edge_ids=sorted(soft_set),
                max_edge_wait_ms=normalized_request.constraints.max_edge_wait_ms,
                evidence_ids=constraint_evidence,
            ),
            time_limit_seconds=time_limit_seconds,
            formulation_summary=(
                f"G2P wave formulation preserved {len(requested_orders)} order(s), "
                f"included {len(included_robot_ids)} robot(s), reserved "
                f"{len(reserved_robot_ids)} robot(s), and deferred physical "
                "handling-unit task creation to the deterministic compiler."
            ),
        )

    def formulate_from_contexts(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        snapshot: ContextSnapshot,
        inventory: InventoryContext,
        robots: RobotRuntimeContext,
        map_context: MapContext,
        graph_arcs: list[dict[str, Any]],
        time_limit_seconds: int,
    ) -> CuOptDynamicInputDraft:
        """Create the direct Rule-path draft from typed authoritative contexts.

        This method intentionally avoids the semantic retrieval program and the
        Warehouse Situation Graph.  It selects one feasible stock location per
        order while preserving every baseline-eligible robot for the solver.
        """

        directed = DirectedGraphService(graph_arcs)
        need_by_order = {value.order_id: value for value in inventory.task_needs}
        stocks_by_item: dict[str, list] = defaultdict(list)
        for stock in inventory.candidate_stocks:
            stocks_by_item[stock.item_id].append(stock)
        remaining_by_stock = {value.stock_id: value.available_qty for value in inventory.candidate_stocks}
        robot_by_id = {value.robot_id: value for value in robots.robots}
        explicit_exclusions = set(normalized_request.constraints.excluded_robot_ids)
        candidate_robot_ids = sorted(set(robots.candidate_robot_ids) - explicit_exclusions)
        included_robot_ids, reserved_robot_ids = _apply_emergency_reserve(
            candidate_robot_ids=candidate_robot_ids,
            battery_by_robot={
                value.robot_id: float(value.battery_pct)
                for value in robots.robots
            },
            request=normalized_request,
        )

        tasks: list[CuOptTaskDraft] = []
        deferred: list[str] = []
        outbound_operations = [
            value for value in normalized_request.operations
            if value.operation_type == "OUTBOUND_ORDER"
        ]
        g2p_order_ids = [value.operation_id for value in outbound_operations]
        g2p_mode = bool(
            g2p_order_ids
            and get_settings().outbound_fulfillment_mode == "goods_to_person"
        )
        operations_to_formulate = [] if g2p_mode else outbound_operations
        for index, operation in enumerate(operations_to_formulate, start=1):
            need = need_by_order.get(operation.operation_id)
            if need is None:
                deferred.append(operation.operation_id)
                continue

            candidates: list[tuple[int, float, str, Any, str]] = []
            robot_start_nodes = {
                robot_id: robot_by_id[robot_id].current_node
                for robot_id in included_robot_ids
                if robot_id in robot_by_id and robot_by_id[robot_id].capacity_units >= need.required_qty
            }
            for stock in stocks_by_item.get(need.item_id, []):
                if remaining_by_stock.get(stock.stock_id, 0) < need.required_qty:
                    continue
                access_choice = choose_best_access_node(
                    directed,
                    rack_id=stock.rack_id,
                    access_node_ids=stock.access_node_ids,
                    robot_start_nodes=robot_start_nodes,
                    delivery_node=need.delivery_node,
                )
                if access_choice is None:
                    continue
                candidates.append((
                    access_choice.total_time_ms,
                    access_choice.total_cost,
                    stock.stock_id,
                    stock,
                    access_choice.access_node_id,
                ))

            if not candidates:
                deferred.append(need.order_id)
                continue

            _, _, _, selected, pickup_node = min(
                candidates,
                key=lambda value: (value[0], value[1], value[2], value[4]),
            )
            remaining_by_stock[selected.stock_id] -= need.required_qty
            tasks.append(
                CuOptTaskDraft(
                    task_id=f"TASK-{index:03d}",
                    operation_type="OUTBOUND_ORDER",
                    order_id=need.order_id,
                    item_id=need.item_id,
                    stock_id=selected.stock_id,
                    rack_id=selected.rack_id,
                    rack_level=selected.rack_level,
                    pickup_node=pickup_node,
                    delivery_node=need.delivery_node,
                    demand=need.required_qty,
                    priority=need.priority,
                    mandatory=True,
                    fixed_vehicle_id=None,
                    evidence_ids=[],
                )
            )

        inbound_operations = [
            value for value in normalized_request.operations
            if value.operation_type == "INBOUND_ITEM"
        ]
        inbound_by_id = {value.inbound_id: value for value in inventory.inbound_needs}
        repository = get_repository()
        next_task_index = len(tasks) + 1
        for operation in inbound_operations:
            need = inbound_by_id.get(operation.operation_id)
            if need is None:
                deferred.append(operation.operation_id)
                continue
            handoff = repository.inbound_handoff_for_port(need.source_port_id)
            if not handoff:
                deferred.append(operation.operation_id)
                continue
            slots = list(inventory.candidate_putaway_slots)
            if need.target_rack_id:
                slots = [value for value in slots if value.rack_id == need.target_rack_id]
            if need.target_rack_level:
                slots = [value for value in slots if value.rack_level == need.target_rack_level]
            robot_start_nodes = {
                robot_id: robot_by_id[robot_id].current_node
                for robot_id in included_robot_ids
                if robot_id in robot_by_id and robot_by_id[robot_id].capacity_units >= need.quantity
            }
            choices: list[tuple[int, float, str, int, str, str]] = []
            for pickup_node in [str(value) for value in handoff.get("access_node_ids", [])]:
                robot_options: list[tuple[int, float]] = []
                for start_node in robot_start_nodes.values():
                    robot_time, robot_path = directed.shortest_path(start_node, pickup_node, metric="travel_time")
                    if start_node == pickup_node or robot_path:
                        robot_options.append((int(robot_time), sum(float(arc.cost) for arc in robot_path)))
                if not robot_options:
                    continue
                robot_time, robot_cost = min(robot_options)
                for slot in slots:
                    for delivery_node in slot.access_node_ids:
                        travel_time, path = directed.shortest_path(pickup_node, delivery_node, metric="travel_time")
                        if pickup_node != delivery_node and not path:
                            continue
                        choices.append((
                            robot_time + int(travel_time),
                            robot_cost + sum(float(arc.cost) for arc in path),
                            slot.rack_id,
                            slot.rack_level,
                            pickup_node,
                            delivery_node,
                        ))
            if not choices:
                deferred.append(operation.operation_id)
                continue
            _, _, rack_id, rack_level, pickup_node, delivery_node = min(
                choices, key=lambda value: (value[0], value[1], value[2], value[3], value[4], value[5])
            )
            tasks.append(
                CuOptTaskDraft(
                    task_id=f"TASK-{next_task_index:03d}",
                    operation_type="INBOUND_ITEM",
                    order_id=need.inbound_id,
                    item_id=need.item_id,
                    stock_id=need.handling_unit_id,
                    rack_id=rack_id,
                    rack_level=rack_level,
                    pickup_node=pickup_node,
                    delivery_node=delivery_node,
                    demand=need.quantity,
                    priority=need.priority,
                    mandatory=True,
                    fixed_vehicle_id=None,
                    evidence_ids=[],
                )
            )
            next_task_index += 1

        blocked_set = (
            set(map_context.map_constraints.blocked_edge_ids)
            | set(normalized_request.constraints.hard_block_edge_ids)
        )
        soft_set = (
            {value.edge_id for value in map_context.map_constraints.edge_penalties}
            | set(normalized_request.constraints.soft_avoid_edge_ids)
        )
        _apply_conditional_policies(
            request=normalized_request,
            expected_wait_by_edge={
                value.edge_id: _expected_wait_from_map_context(map_context, value.edge_id)
                for value in normalized_request.constraints.conditional_edge_policies
            },
            blocked=blocked_set,
            soft=soft_set,
        )
        blocked = sorted(blocked_set)
        soft_penalties = sorted(soft_set - blocked_set)
        return CuOptDynamicInputDraft(
            formulation_mode=("GOODS_TO_PERSON" if g2p_mode else "ORDER_TASKS"),
            g2p_order_ids=(g2p_order_ids if g2p_mode else []),
            snapshot_id=snapshot.snapshot_id,
            graph_version=snapshot.graph_version,
            formulation_source="rule",
            objective_profile=normalized_request.constraints.objective_profile,
            objective_terms=_objective_terms(normalized_request),
            tasks=tasks,
            deferred_order_ids=sorted(set(deferred)),
            fleet=CuOptFleetDraft(
                included_robot_ids=included_robot_ids,
                excluded_robot_ids=sorted(explicit_exclusions),
                reserved_robot_ids=reserved_robot_ids,
                evidence_ids=[],
            ),
            map_constraints=CuOptMapConstraintDraft(
                blocked_edge_ids=blocked,
                soft_penalty_edge_ids=soft_penalties,
                max_edge_wait_ms=normalized_request.constraints.max_edge_wait_ms,
                evidence_ids=[],
            ),
            time_limit_seconds=time_limit_seconds,
            formulation_summary=(
                (
                    f"Direct Rule G2P formulation preserved {len(g2p_order_ids)} canonical "
                    f"order(s) and {len(included_robot_ids)} robot candidate(s) for the compiler."
                )
                if g2p_mode
                else (
                    f"Direct Rule formulation created {len(tasks)} task(s), deferred "
                    f"{len(deferred)} order(s), and preserved {len(included_robot_ids)} robot candidate(s)."
                )
            ),
        )

    @staticmethod
    def _path_evidence_ids(graph: WarehouseSituationGraph, path_id: str) -> list[str]:
        """Return evidence identifiers associated with the requested path relation."""
        node = next((value for value in graph.nodes if value.node_id == f"path_option:{path_id}"), None)
        return list(node.evidence_ids) if node is not None else []


class CuOptDraftEvidenceEnricher:
    """Complete factual evidence references without changing business choices.

    The LLM still chooses the task, stock location, included fleet, objective,
    and map constraints.  This service only attaches evidence identifiers that
    are mechanically implied by those already-selected canonical IDs.  It is
    therefore closer to a compiler source-map pass than a planner.
    """

    def enrich(
        self,
        *,
        draft: CuOptDynamicInputDraft,
        graph: WarehouseSituationGraph,
    ) -> tuple[CuOptDynamicInputDraft, CuOptEvidenceEnrichmentResult]:
        """Return an evidence-complete copy and an auditable change record."""

        node_by_id = {node.node_id: node for node in graph.nodes}
        path_node_by_id = {
            node.node_id.removeprefix("path_option:"): node
            for node in graph.nodes
            if node.node_type == "path_option"
        }
        evidence_index = {value.evidence_id for value in graph.evidence_index}
        added_by_task: dict[str, list[str]] = {}
        warnings: list[str] = []
        enriched_tasks: list[CuOptTaskDraft] = []

        for task in draft.tasks:
            additions: list[str] = []
            if task.operation_type == "OUTBOUND_ORDER":
                source_node = node_by_id.get(f"order:{task.order_id}")
                resource_node = node_by_id.get(f"stock:{task.stock_id}")
                source_label = "order"
                resource_label = "stock"
            elif task.operation_type == "INBOUND_ITEM":
                source_node = node_by_id.get(f"inbound:{task.order_id}")
                resource_node = node_by_id.get(f"handling_unit:{task.stock_id}")
                source_label = "inbound"
                resource_label = "handling unit"
            else:
                source_node = node_by_id.get(f"active_task:{task.order_id}")
                resource_node = None
                source_label = "recovery operation"
                resource_label = "resource"

            if source_node is not None:
                additions.extend(source_node.evidence_ids)
            else:
                warnings.append(f"No {source_label} node was available for {task.order_id}.")
            if resource_node is not None:
                additions.extend(resource_node.evidence_ids)
            elif task.operation_type != "RECOVERY":
                warnings.append(f"No {resource_label} node was available for {task.stock_id}.")

            if task.rack_id and task.rack_level:
                slot_node = node_by_id.get(f"rack_slot:{task.rack_id}:L{task.rack_level}")
                if slot_node is not None:
                    additions.extend(slot_node.evidence_ids)

            for path in graph.path_evidence:
                include = False
                if (
                    path.purpose in {"PICKUP_TO_DELIVERY", "PICKUP_TO_STATION"}
                    and path.source_node_id == task.pickup_node
                    and path.target_node_id == task.delivery_node
                ):
                    include = True
                elif path.purpose == "ROBOT_TO_PICKUP" and path.target_node_id == task.pickup_node:
                    include = any(
                        path.path_id.startswith(f"path:robot:{robot_id}:")
                        for robot_id in draft.fleet.included_robot_ids
                    )
                if include:
                    node = path_node_by_id.get(path.path_id)
                    if node is not None:
                        additions.extend(node.evidence_ids)

            additions = [value for value in dict.fromkeys(additions) if value in evidence_index]
            existing = list(dict.fromkeys(task.evidence_ids))
            new_values = [value for value in additions if value not in existing]
            if new_values:
                added_by_task[task.task_id] = new_values
            enriched_tasks.append(
                task.model_copy(update={"evidence_ids": [*existing, *new_values]})
            )

        fleet_additions: list[str] = []
        for robot_id in dict.fromkeys(
            [
                *draft.fleet.included_robot_ids,
                *draft.fleet.excluded_robot_ids,
                *draft.fleet.reserved_robot_ids,
            ]
        ):
            node = node_by_id.get(f"robot:{robot_id}")
            if node is not None:
                fleet_additions.extend(node.evidence_ids)
        fleet_existing = list(dict.fromkeys(draft.fleet.evidence_ids))
        fleet_new = [
            value
            for value in dict.fromkeys(fleet_additions)
            if value in evidence_index and value not in fleet_existing
        ]

        constrained_edges = set(draft.map_constraints.blocked_edge_ids) | set(
            draft.map_constraints.soft_penalty_edge_ids
        )
        map_additions: list[str] = []
        for node in graph.nodes:
            if node.node_type == "runtime_constraint" and str(node.attributes.get("edge_id")) in constrained_edges:
                map_additions.extend(node.evidence_ids)
            if node.node_type == "edge" and str(node.attributes.get("edge_id")) in constrained_edges:
                map_additions.extend(node.evidence_ids)
        map_existing = list(dict.fromkeys(draft.map_constraints.evidence_ids))
        map_new = [
            value
            for value in dict.fromkeys(map_additions)
            if value in evidence_index and value not in map_existing
        ]

        enriched = draft.model_copy(
            update={
                "tasks": enriched_tasks,
                "fleet": draft.fleet.model_copy(
                    update={"evidence_ids": [*fleet_existing, *fleet_new]}
                ),
                "map_constraints": draft.map_constraints.model_copy(
                    update={"evidence_ids": [*map_existing, *map_new]}
                ),
            }
        )
        result = CuOptEvidenceEnrichmentResult(
            applied=bool(added_by_task or fleet_new or map_new),
            added_task_evidence=added_by_task,
            added_fleet_evidence=fleet_new,
            added_map_evidence=map_new,
            warnings=list(dict.fromkeys(warnings)),
        )
        return enriched, result


def _operation_coverage_errors(
    *,
    draft: CuOptDynamicInputDraft,
    normalized_request: NormalizedWarehouseRequest,
) -> list[str]:
    """Return exact-once coverage errors for every actionable operation."""

    actionable_types = {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}
    requested_type_by_id = {
        value.operation_id: value.operation_type
        for value in normalized_request.operations
        if value.operation_type in actionable_types
    }
    requested_ids = set(requested_type_by_id)
    errors: list[str] = []
    representations: dict[str, list[str]] = defaultdict(list)

    for operation_id in draft.g2p_order_ids:
        representations[operation_id].append("g2p_order_ids")
        expected_type = requested_type_by_id.get(operation_id)
        if expected_type is not None and expected_type != "OUTBOUND_ORDER":
            errors.append(f"G2P_NON_OUTBOUND_OPERATION:{operation_id}")
    for task in draft.tasks:
        representations[task.order_id].append(f"task:{task.task_id}")
        expected_type = requested_type_by_id.get(task.order_id)
        if expected_type is not None and expected_type != task.operation_type:
            errors.append(
                f"OPERATION_TYPE_MISMATCH:{task.order_id}:"
                f"expected={expected_type};actual={task.operation_type}"
            )
    for operation_id in draft.deferred_order_ids:
        representations[operation_id].append("deferred_order_ids")

    represented_ids = set(representations)
    missing = requested_ids - represented_ids
    unexpected = represented_ids - requested_ids
    if missing:
        errors.append("OPERATION_COVERAGE_MISMATCH:" + ",".join(sorted(missing)))
    if unexpected:
        errors.append("UNKNOWN_OPERATION_COVERAGE:" + ",".join(sorted(unexpected)))
    for operation_id, locations in sorted(representations.items()):
        if len(locations) != 1:
            errors.append(
                f"OPERATION_MULTIPLE_REPRESENTATIONS:{operation_id}:"
                + ",".join(locations)
            )
    task_ids = [value.task_id for value in draft.tasks]
    if len(task_ids) != len(set(task_ids)):
        errors.append("DUPLICATE_TASK_ID")
    return errors


class CuOptDynamicInputValidator:
    """Validate that a dynamic draft is complete, factual, grounded, and feasible."""

    def validate(
        self,
        *,
        draft: CuOptDynamicInputDraft,
        normalized_request: NormalizedWarehouseRequest,
        graph: WarehouseSituationGraph,
        expected_source: str | None = None,
    ) -> CuOptDynamicInputValidationResult:
        """Validate one Agent/graph-grounded draft against its physical mode."""

        errors: list[str] = []
        warnings: list[str] = []
        node_by_id = {node.node_id: node for node in graph.nodes}
        evidence_ids = {value.evidence_id for value in graph.evidence_index}
        relations = graph.relations
        if draft.snapshot_id != graph.snapshot_id:
            errors.append("DRAFT_SNAPSHOT_MISMATCH")
        if draft.graph_version != graph.graph_version:
            errors.append("DRAFT_GRAPH_VERSION_MISMATCH")
        if draft.formulation_source not in {"rule", "llm"}:
            errors.append("INVALID_FORMULATION_SOURCE")
        if expected_source is not None and draft.formulation_source != expected_source:
            errors.append(f"FORMULATION_SOURCE_MISMATCH:expected={expected_source}")

        requested_orders = {
            operation.operation_id
            for operation in normalized_request.operations
            if operation.operation_type == "OUTBOUND_ORDER"
        }
        requested_inbounds = {
            operation.operation_id
            for operation in normalized_request.operations
            if operation.operation_type == "INBOUND_ITEM"
        }
        outbound_tasks = [
            task for task in draft.tasks if task.operation_type == "OUTBOUND_ORDER"
        ]
        inbound_tasks = [
            task for task in draft.tasks if task.operation_type == "INBOUND_ITEM"
        ]
        errors.extend(
            _operation_coverage_errors(
                draft=draft, normalized_request=normalized_request
            )
        )
        g2p_mode = graph.fulfillment_mode == "goods_to_person"

        if g2p_mode:
            if draft.formulation_mode != "GOODS_TO_PERSON":
                errors.append("G2P_FORMULATION_MODE_REQUIRED")
            if outbound_tasks:
                errors.append("G2P_ORDER_LEVEL_TASKS_FORBIDDEN")
            actual_g2p_orders = list(draft.g2p_order_ids)
            if len(actual_g2p_orders) != len(set(actual_g2p_orders)):
                errors.append("DUPLICATE_G2P_ORDER_ID")
            if set(actual_g2p_orders) != requested_orders:
                errors.append(
                    "G2P_ORDER_WAVE_MISMATCH:expected="
                    + ",".join(sorted(requested_orders))
                    + ";actual="
                    + ",".join(sorted(set(actual_g2p_orders)))
                )
            if set(graph.g2p_order_ids) != requested_orders:
                errors.append("SITUATION_GRAPH_G2P_ORDER_WAVE_MISMATCH")
            if not requested_orders:
                errors.append("G2P_ORDER_WAVE_EMPTY")
        else:
            if draft.formulation_mode != "ORDER_TASKS":
                errors.append("LEGACY_FORMULATION_MODE_REQUIRED")
            if draft.g2p_order_ids:
                errors.append("LEGACY_G2P_ORDER_IDS_FORBIDDEN")
            task_order_ids = [task.order_id for task in outbound_tasks]
            deferred = set(draft.deferred_order_ids)
            if len(task_order_ids) != len(set(task_order_ids)):
                errors.append("DUPLICATE_ORDER_TASK")
            if set(task_order_ids) & deferred:
                errors.append("EXECUTE_DEFER_OVERLAP")
            covered = set(task_order_ids) | (deferred & requested_orders)
            missing = requested_orders - covered
            unknown = set(task_order_ids) - requested_orders
            if missing:
                errors.append("ORDER_COVERAGE_MISSING:" + ",".join(sorted(missing)))
            if unknown:
                errors.append("UNKNOWN_ORDER_COVERAGE:" + ",".join(sorted(unknown)))

            item_by_order = {
                relation.source_node_id.removeprefix("order:"): relation.target_node_id.removeprefix("item:")
                for relation in relations
                if relation.relation_type == "REQUIRES_ITEM"
            }
            stock_to_item = {
                relation.source_node_id.removeprefix("stock:"): relation.target_node_id.removeprefix("item:")
                for relation in relations
                if relation.relation_type == "OF_ITEM"
            }
            stock_used: dict[str, int] = defaultdict(int)
            for task in outbound_tasks:
                order_node = node_by_id.get(f"order:{task.order_id}")
                stock_node = node_by_id.get(f"stock:{task.stock_id}")
                if order_node is None:
                    errors.append(f"UNKNOWN_TASK_ORDER:{task.order_id}")
                    continue
                if stock_node is None:
                    errors.append(f"UNKNOWN_TASK_STOCK:{task.stock_id}")
                    continue
                expected_item = item_by_order.get(task.order_id)
                expected_demand = int(order_node.attributes["required_qty"])
                expected_delivery = str(order_node.attributes["delivery_node"])
                expected_priority = str(order_node.attributes["priority"])
                if task.item_id != expected_item:
                    errors.append(f"TASK_ITEM_MISMATCH:{task.order_id}")
                if stock_to_item.get(task.stock_id) != expected_item:
                    errors.append(f"STOCK_ITEM_MISMATCH:{task.order_id}")
                if task.demand != expected_demand:
                    errors.append(f"TASK_DEMAND_MISMATCH:{task.order_id}")
                if task.delivery_node != expected_delivery:
                    errors.append(f"TASK_DELIVERY_MISMATCH:{task.order_id}")
                if task.priority != expected_priority:
                    errors.append(f"TASK_PRIORITY_MISMATCH:{task.order_id}")
                expected_access_nodes = {
                    str(value) for value in stock_node.attributes.get("access_node_ids", [])
                }
                if task.pickup_node not in expected_access_nodes:
                    errors.append(f"TASK_PICKUP_MISMATCH:{task.order_id}")
                if task.rack_id != str(stock_node.attributes.get("rack_id")):
                    errors.append(f"TASK_RACK_MISMATCH:{task.order_id}")
                if task.rack_level != int(stock_node.attributes.get("rack_level", 0)):
                    errors.append(f"TASK_RACK_LEVEL_MISMATCH:{task.order_id}")
                if task.fixed_vehicle_id is not None:
                    errors.append(f"NEW_TASK_FIXED_VEHICLE_FORBIDDEN:{task.order_id}")
                unknown_evidence = set(task.evidence_ids) - evidence_ids
                if unknown_evidence:
                    errors.append(f"UNKNOWN_TASK_EVIDENCE:{task.order_id}:{sorted(unknown_evidence)}")
                if not set(order_node.evidence_ids).intersection(task.evidence_ids):
                    errors.append(f"ORDER_EVIDENCE_MISSING:{task.order_id}")
                if not set(stock_node.evidence_ids).intersection(task.evidence_ids):
                    errors.append(f"STOCK_EVIDENCE_MISSING:{task.order_id}")
                has_delivery_path = any(
                    path.source_node_id == task.pickup_node
                    and path.target_node_id == task.delivery_node
                    and path.purpose == "PICKUP_TO_DELIVERY"
                    for path in graph.path_evidence
                )
                has_robot_path = any(
                    path.target_node_id == task.pickup_node and path.purpose == "ROBOT_TO_PICKUP"
                    for path in graph.path_evidence
                )
                if not has_delivery_path or not has_robot_path:
                    errors.append(f"TASK_PATH_EVIDENCE_MISSING:{task.order_id}")
                matching_delivery_evidence = {
                    evidence_id
                    for path in graph.path_evidence
                    if path.source_node_id == task.pickup_node
                    and path.target_node_id == task.delivery_node
                    and path.purpose == "PICKUP_TO_DELIVERY"
                    for node in graph.nodes
                    if node.node_id == f"path_option:{path.path_id}"
                    for evidence_id in node.evidence_ids
                }
                matching_robot_evidence = {
                    evidence_id
                    for path in graph.path_evidence
                    if path.target_node_id == task.pickup_node
                    and path.purpose == "ROBOT_TO_PICKUP"
                    for node in graph.nodes
                    if node.node_id == f"path_option:{path.path_id}"
                    for evidence_id in node.evidence_ids
                }
                if matching_delivery_evidence and not matching_delivery_evidence.intersection(task.evidence_ids):
                    errors.append(f"DELIVERY_PATH_EVIDENCE_MISSING:{task.order_id}")
                if matching_robot_evidence and not matching_robot_evidence.intersection(task.evidence_ids):
                    errors.append(f"ROBOT_PATH_EVIDENCE_MISSING:{task.order_id}")
                stock_used[task.stock_id] += task.demand
            for stock_id, used in stock_used.items():
                stock_node = node_by_id.get(f"stock:{stock_id}")
                if stock_node and used > int(stock_node.attributes["available_qty"]):
                    errors.append(f"STOCK_OVERALLOCATED:{stock_id}:{used}")

        # Graph-grounded direct inbound validation.  The typed-context
        # validator runs as an independent second pass in the graph node.
        for task in inbound_tasks:
            inbound_node = node_by_id.get(f"inbound:{task.order_id}")
            hu_node = node_by_id.get(f"handling_unit:{task.stock_id}")
            if inbound_node is None:
                errors.append(f"UNKNOWN_INBOUND_TASK:{task.order_id}")
                continue
            expected_item = str(inbound_node.attributes.get("item_id", ""))
            expected_quantity = int(inbound_node.attributes.get("quantity", 0))
            expected_hu = str(inbound_node.attributes.get("handling_unit_id", ""))
            if task.item_id != expected_item or task.demand != expected_quantity:
                errors.append(f"INBOUND_ITEM_OR_QTY_MISMATCH:{task.order_id}")
            if task.stock_id != expected_hu or hu_node is None:
                errors.append(f"INBOUND_HANDLING_UNIT_MISMATCH:{task.order_id}")

            pickup_nodes = {
                relation.target_node_id.removeprefix("map:")
                for relation in relations
                if relation.source_node_id == f"inbound:{task.order_id}"
                and relation.relation_type == "PICKUP_FROM"
            }
            slot_nodes = {
                relation.target_node_id
                for relation in relations
                if relation.source_node_id == f"inbound:{task.order_id}"
                and relation.relation_type == "PUTAWAY_TO"
            }
            selected_slot = (
                f"rack_slot:{task.rack_id}:L{task.rack_level}"
                if task.rack_id and task.rack_level
                else None
            )
            delivery_nodes = {
                relation.target_node_id.removeprefix("map:")
                for relation in relations
                if selected_slot is not None
                and relation.relation_type == "HAS_ACCESS_POINT"
                and relation.target_node_id.startswith("map:")
                and (
                    relation.source_node_id == selected_slot
                    or relation.source_node_id == f"rack:{task.rack_id}"
                )
            }
            if task.pickup_node not in pickup_nodes:
                errors.append(f"INBOUND_PICKUP_MISMATCH:{task.order_id}")
            if selected_slot not in slot_nodes:
                errors.append(f"UNKNOWN_PUTAWAY_SLOT:{task.order_id}")
            if task.delivery_node not in delivery_nodes:
                errors.append(f"INBOUND_DELIVERY_MISMATCH:{task.order_id}")

            has_delivery_path = any(
                path.purpose == "PICKUP_TO_DELIVERY"
                and path.source_node_id == task.pickup_node
                and path.target_node_id == task.delivery_node
                for path in graph.path_evidence
            )
            has_robot_path = any(
                path.purpose == "ROBOT_TO_PICKUP"
                and path.target_node_id == task.pickup_node
                for path in graph.path_evidence
            )
            if not has_delivery_path or not has_robot_path:
                errors.append(f"INBOUND_PATH_EVIDENCE_MISSING:{task.order_id}")
            unknown_evidence = set(task.evidence_ids) - evidence_ids
            if unknown_evidence:
                errors.append(
                    f"UNKNOWN_TASK_EVIDENCE:{task.order_id}:{sorted(unknown_evidence)}"
                )
            if not set(inbound_node.evidence_ids).intersection(task.evidence_ids):
                errors.append(f"INBOUND_EVIDENCE_MISSING:{task.order_id}")
            if hu_node is not None and not set(hu_node.evidence_ids).intersection(task.evidence_ids):
                errors.append(f"HANDLING_UNIT_EVIDENCE_MISSING:{task.order_id}")

        robot_nodes = {
            str(node.attributes["robot_id"]): node
            for node in graph.nodes
            if node.node_type == "robot"
        }
        eligible = {
            robot_id
            for robot_id, node in robot_nodes.items()
            if bool(node.attributes.get("baseline_eligible"))
        }
        explicit_exclusions = set(normalized_request.constraints.excluded_robot_ids)
        expected_included_list, expected_reserved_list = _apply_emergency_reserve(
            candidate_robot_ids=sorted(eligible - explicit_exclusions),
            battery_by_robot={
                robot_id: float(node.attributes.get("battery_pct") or 0.0)
                for robot_id, node in robot_nodes.items()
            },
            request=normalized_request,
        )
        expected_included = set(expected_included_list)
        expected_reserved = set(expected_reserved_list)
        requested_reserve_count = int(normalized_request.constraints.reserve_robot_count or 0)
        if len(expected_reserved) != requested_reserve_count:
            errors.append(
                "FLEET_RESERVE_REQUIREMENT_UNSATISFIED:requested="
                + str(requested_reserve_count)
                + ";available="
                + str(len(expected_reserved))
            )
        included = set(draft.fleet.included_robot_ids)
        excluded = set(draft.fleet.excluded_robot_ids)
        reserved = set(draft.fleet.reserved_robot_ids)
        if included != expected_included:
            errors.append(
                "FLEET_CANDIDATE_SPACE_MISMATCH:expected="
                + ",".join(sorted(expected_included))
                + ";actual="
                + ",".join(sorted(included))
            )
        if excluded != explicit_exclusions:
            errors.append("FLEET_EXCLUSION_MISMATCH")
        if reserved != expected_reserved:
            errors.append(
                "FLEET_RESERVE_MISMATCH:expected="
                + ",".join(sorted(expected_reserved))
                + ";actual="
                + ",".join(sorted(reserved))
            )
        if included & (excluded | reserved) or excluded & reserved:
            errors.append("FLEET_PARTITION_OVERLAP")
        if (included | excluded | reserved) - set(robot_nodes):
            errors.append("UNKNOWN_FLEET_IDENTIFIER")
        if set(draft.fleet.evidence_ids) - evidence_ids:
            errors.append("UNKNOWN_FLEET_EVIDENCE")
        for robot_id in sorted(included | excluded | reserved):
            node = robot_nodes.get(robot_id)
            if node is not None and not set(node.evidence_ids).intersection(draft.fleet.evidence_ids):
                errors.append(f"ROBOT_EVIDENCE_MISSING:{robot_id}")
        if not included and (draft.tasks or (g2p_mode and requested_orders)):
            errors.append("NO_INCLUDED_ROBOT")

        current_blocked = {
            str(node.attributes["edge_id"])
            for node in graph.nodes
            if node.node_type == "runtime_constraint"
            and node.attributes.get("constraint_type") == "BLOCKED"
        }
        current_penalties = {
            str(node.attributes["edge_id"])
            for node in graph.nodes
            if node.node_type == "runtime_constraint"
            and node.attributes.get("constraint_type") == "CONGESTED"
        }
        conditional_edges = _conditional_edge_policy_ids(normalized_request)
        expected_blocked = (
            current_blocked | set(normalized_request.constraints.hard_block_edge_ids)
        ) - conditional_edges
        expected_soft = (
            current_penalties | set(normalized_request.constraints.soft_avoid_edge_ids)
        ) - conditional_edges
        actual_blocked = set(draft.map_constraints.blocked_edge_ids)
        actual_soft = set(draft.map_constraints.soft_penalty_edge_ids)
        if actual_blocked - conditional_edges != expected_blocked:
            errors.append("BLOCKED_EDGE_SET_MISMATCH")
        if actual_soft - conditional_edges != expected_soft:
            errors.append("SOFT_PENALTY_EDGE_SET_MISMATCH")
        if actual_blocked & actual_soft:
            errors.append("BLOCKED_EDGE_CANNOT_BE_SOFT")
        _validate_conditional_edge_policies(
            request=normalized_request,
            blocked_edge_ids=actual_blocked,
            soft_edge_ids=actual_soft,
            errors=errors,
        )
        if set(draft.map_constraints.evidence_ids) - evidence_ids:
            errors.append("UNKNOWN_MAP_CONSTRAINT_EVIDENCE")
        edge_nodes = {
            str(node.attributes.get("id")): node
            for node in graph.nodes
            if node.node_type == "edge"
        }
        constraint_evidence_by_edge: dict[str, set[str]] = defaultdict(set)
        for node in graph.nodes:
            if node.node_type != "runtime_constraint":
                continue
            edge_id = node.attributes.get("edge_id")
            if edge_id:
                constraint_evidence_by_edge[str(edge_id)].update(node.evidence_ids)
        for edge_id in sorted(expected_blocked | expected_soft | conditional_edges):
            if edge_id not in edge_nodes:
                errors.append(f"UNKNOWN_MAP_EDGE:{edge_id}")
                continue
            supported = constraint_evidence_by_edge.get(edge_id, set()) | set(edge_nodes[edge_id].evidence_ids)
            if supported and not supported.intersection(draft.map_constraints.evidence_ids):
                errors.append(f"MAP_CONSTRAINT_EVIDENCE_MISSING:{edge_id}")
        if draft.objective_profile != normalized_request.constraints.objective_profile:
            errors.append("OBJECTIVE_PROFILE_MISMATCH")
        if normalized_request.constraints.objective_terms:
            if set(draft.objective_terms) != set(_objective_terms(normalized_request)):
                errors.append("OBJECTIVE_TERMS_MISMATCH")
        if draft.map_constraints.max_edge_wait_ms != normalized_request.constraints.max_edge_wait_ms:
            errors.append("MAX_WAIT_MISMATCH")
        if not graph.completeness.ready_for_formulation:
            errors.append("SITUATION_GRAPH_NOT_READY")
        repairable = bool(errors) and draft.formulation_source == "llm"
        return CuOptDynamicInputValidationResult(
            valid=not errors,
            repairable=repairable,
            errors=list(dict.fromkeys(errors)),
            warnings=warnings,
        )


    def validate_from_contexts(
        self,
        *,
        draft: CuOptDynamicInputDraft,
        normalized_request: NormalizedWarehouseRequest,
        snapshot: ContextSnapshot,
        inventory: InventoryContext,
        robots: RobotRuntimeContext,
        map_context: MapContext,
        graph_arcs: list[dict[str, Any]],
        expected_source: str = "rule",
    ) -> CuOptDynamicInputValidationResult:
        """Validate the deterministic Rule draft against typed authoritative contexts."""

        errors: list[str] = []
        warnings: list[str] = []
        if draft.snapshot_id != snapshot.snapshot_id:
            errors.append("DRAFT_SNAPSHOT_MISMATCH")
        if draft.graph_version != snapshot.graph_version:
            errors.append("DRAFT_GRAPH_VERSION_MISMATCH")
        if draft.formulation_source != expected_source:
            errors.append(f"FORMULATION_SOURCE_MISMATCH:expected={expected_source}")
        errors.extend(
            _operation_coverage_errors(
                draft=draft, normalized_request=normalized_request
            )
        )

        requested_orders = {
            value.operation_id
            for value in normalized_request.operations
            if value.operation_type == "OUTBOUND_ORDER"
        }
        g2p_mode = bool(
            requested_orders
            and get_settings().outbound_fulfillment_mode == "goods_to_person"
        )
        need_by_order = {value.order_id: value for value in inventory.task_needs}
        stock_by_id = {value.stock_id: value for value in inventory.candidate_stocks}

        requested_inbounds = {
            value.operation_id
            for value in normalized_request.operations
            if value.operation_type == "INBOUND_ITEM"
        }
        inbound_by_id = {value.inbound_id: value for value in inventory.inbound_needs}
        slot_by_key = {
            (value.rack_id, value.rack_level): value
            for value in inventory.candidate_putaway_slots
        }
        outbound_tasks = [value for value in draft.tasks if value.operation_type == "OUTBOUND_ORDER"]
        inbound_tasks = [value for value in draft.tasks if value.operation_type == "INBOUND_ITEM"]
        unknown_typed_tasks = [
            value.task_id for value in draft.tasks
            if value.operation_type not in {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}
        ]
        if unknown_typed_tasks:
            errors.append("UNKNOWN_TYPED_TASKS:" + ",".join(sorted(unknown_typed_tasks)))

        if g2p_mode:
            if draft.formulation_mode != "GOODS_TO_PERSON":
                errors.append("G2P_FORMULATION_MODE_REQUIRED")
            if outbound_tasks:
                errors.append("G2P_ORDER_TASKS_FORBIDDEN")
            if len(draft.g2p_order_ids) != len(set(draft.g2p_order_ids)):
                errors.append("DUPLICATE_G2P_ORDER")
            missing = requested_orders - set(draft.g2p_order_ids)
            unknown = set(draft.g2p_order_ids) - requested_orders
            if missing:
                errors.append("G2P_ORDER_COVERAGE_MISSING:" + ",".join(sorted(missing)))
            if unknown:
                errors.append("UNKNOWN_G2P_ORDER_COVERAGE:" + ",".join(sorted(unknown)))
            missing_facts = requested_orders - set(need_by_order)
            if missing_facts:
                errors.append("G2P_ORDER_FACTS_MISSING:" + ",".join(sorted(missing_facts)))
            required_by_item: dict[str, int] = defaultdict(int)
            for order_id in requested_orders:
                need = need_by_order.get(order_id)
                if need is not None:
                    required_by_item[need.item_id] += need.required_qty
            available_by_item: dict[str, int] = defaultdict(int)
            for stock in inventory.candidate_stocks:
                available_by_item[stock.item_id] += max(0, stock.available_qty)
            for item_id, required_qty in required_by_item.items():
                available_qty = available_by_item.get(item_id, 0)
                if available_qty < required_qty:
                    errors.append(
                        f"G2P_AGGREGATE_STOCK_SHORTAGE:{item_id}:"
                        f"required={required_qty};available={available_qty}"
                    )
        else:
            if draft.formulation_mode != "ORDER_TASKS":
                errors.append("LEGACY_ORDER_TASK_FORMULATION_REQUIRED")
            if draft.g2p_order_ids:
                errors.append("LEGACY_G2P_ORDER_IDS_FORBIDDEN")
            outbound_ids = [value.order_id for value in outbound_tasks]
            deferred = set(draft.deferred_order_ids)
            if len(outbound_ids) != len(set(outbound_ids)):
                errors.append("DUPLICATE_ORDER_TASK")
            covered = set(outbound_ids) | (deferred & requested_orders)
            missing = requested_orders - covered
            unknown = set(outbound_ids) - requested_orders
            if missing:
                errors.append("ORDER_COVERAGE_MISSING:" + ",".join(sorted(missing)))
            if unknown:
                errors.append("UNKNOWN_ORDER_COVERAGE:" + ",".join(sorted(unknown)))

        stock_used: dict[str, int] = defaultdict(int)
        directed = DirectedGraphService(graph_arcs)
        for task in outbound_tasks:
            need = need_by_order.get(task.order_id)
            stock = stock_by_id.get(task.stock_id)
            if need is None:
                errors.append(f"UNKNOWN_TASK_ORDER:{task.order_id}")
                continue
            if stock is None:
                errors.append(f"UNKNOWN_TASK_STOCK:{task.stock_id}")
                continue
            if task.item_id != need.item_id:
                errors.append(f"TASK_ITEM_MISMATCH:{task.order_id}")
            if stock.item_id != need.item_id:
                errors.append(f"STOCK_ITEM_MISMATCH:{task.order_id}")
            if task.demand != need.required_qty:
                errors.append(f"TASK_DEMAND_MISMATCH:{task.order_id}")
            if task.delivery_node != need.delivery_node:
                errors.append(f"TASK_DELIVERY_MISMATCH:{task.order_id}")
            if task.pickup_node not in set(stock.access_node_ids):
                errors.append(f"TASK_PICKUP_MISMATCH:{task.order_id}")
            stock_used[task.stock_id] += task.demand

        for stock_id, used in stock_used.items():
            stock = stock_by_id.get(stock_id)
            if stock is not None and used > stock.available_qty:
                errors.append(f"STOCK_OVERALLOCATED:{stock_id}:{used}")

        inbound_ids = [value.order_id for value in inbound_tasks]
        if len(inbound_ids) != len(set(inbound_ids)):
            errors.append("DUPLICATE_INBOUND_TASK")
        missing_inbound = requested_inbounds - set(inbound_ids) - set(draft.deferred_order_ids)
        unknown_inbound = set(inbound_ids) - requested_inbounds
        if missing_inbound:
            errors.append("INBOUND_COVERAGE_MISSING:" + ",".join(sorted(missing_inbound)))
        if unknown_inbound:
            errors.append("UNKNOWN_INBOUND_COVERAGE:" + ",".join(sorted(unknown_inbound)))
        for task in inbound_tasks:
            need = inbound_by_id.get(task.order_id)
            if need is None:
                errors.append(f"UNKNOWN_INBOUND_TASK:{task.order_id}")
                continue
            slot = slot_by_key.get((str(task.rack_id), int(task.rack_level or 0)))
            if slot is None:
                errors.append(f"UNKNOWN_PUTAWAY_SLOT:{task.order_id}")
                continue
            handoff = get_repository().inbound_handoff_for_port(need.source_port_id)
            if not handoff or task.pickup_node not in set(handoff.get("access_node_ids", [])):
                errors.append(f"INBOUND_PICKUP_MISMATCH:{task.order_id}")
            if task.delivery_node not in set(slot.access_node_ids):
                errors.append(f"INBOUND_DELIVERY_MISMATCH:{task.order_id}")
            if task.item_id != need.item_id or task.demand != need.quantity:
                errors.append(f"INBOUND_ITEM_OR_QTY_MISMATCH:{task.order_id}")
            if task.stock_id != need.handling_unit_id:
                errors.append(f"INBOUND_HANDLING_UNIT_MISMATCH:{task.order_id}")
            _, delivery_arcs = directed.shortest_path(task.pickup_node, task.delivery_node, metric="travel_time")
            if not delivery_arcs and task.pickup_node != task.delivery_node:
                errors.append(f"INBOUND_DELIVERY_PATH_MISSING:{task.order_id}")

        all_robot_ids = {value.robot_id for value in robots.robots}
        eligible = set(robots.candidate_robot_ids)
        explicit_exclusions = set(normalized_request.constraints.excluded_robot_ids)
        expected_included_list, expected_reserved_list = _apply_emergency_reserve(
            candidate_robot_ids=sorted(eligible - explicit_exclusions),
            battery_by_robot={
                value.robot_id: float(value.battery_pct)
                for value in robots.robots
            },
            request=normalized_request,
        )
        expected_included = set(expected_included_list)
        expected_reserved = set(expected_reserved_list)
        requested_reserve_count = int(normalized_request.constraints.reserve_robot_count or 0)
        if len(expected_reserved) != requested_reserve_count:
            errors.append(
                "FLEET_RESERVE_REQUIREMENT_UNSATISFIED:requested="
                + str(requested_reserve_count)
                + ";available="
                + str(len(expected_reserved))
            )
        included = set(draft.fleet.included_robot_ids)
        excluded = set(draft.fleet.excluded_robot_ids)
        reserved = set(draft.fleet.reserved_robot_ids)
        if included != expected_included:
            errors.append(
                "FLEET_CANDIDATE_SPACE_MISMATCH:expected="
                + ",".join(sorted(expected_included))
                + ";actual="
                + ",".join(sorted(included))
            )
        if excluded != explicit_exclusions:
            errors.append("FLEET_EXCLUSION_MISMATCH")
        if reserved != expected_reserved:
            errors.append(
                "FLEET_RESERVE_MISMATCH:expected="
                + ",".join(sorted(expected_reserved))
                + ";actual="
                + ",".join(sorted(reserved))
            )
        if included & (excluded | reserved) or excluded & reserved:
            errors.append("FLEET_PARTITION_OVERLAP")
        if (included | excluded | reserved) - all_robot_ids:
            errors.append("UNKNOWN_FLEET_IDENTIFIER")
        if (draft.tasks or (g2p_mode and requested_orders)) and not included:
            errors.append("NO_INCLUDED_ROBOT")

        conditional_edges = _conditional_edge_policy_ids(normalized_request)
        expected_blocked = (
            set(map_context.map_constraints.blocked_edge_ids)
            | set(normalized_request.constraints.hard_block_edge_ids)
        ) - conditional_edges
        expected_soft = (
            {value.edge_id for value in map_context.map_constraints.edge_penalties}
            | set(normalized_request.constraints.soft_avoid_edge_ids)
        ) - conditional_edges
        expected_soft -= expected_blocked
        actual_blocked = set(draft.map_constraints.blocked_edge_ids)
        actual_soft = set(draft.map_constraints.soft_penalty_edge_ids)
        if actual_blocked - conditional_edges != expected_blocked:
            errors.append("BLOCKED_EDGE_SET_MISMATCH")
        if actual_soft - conditional_edges != expected_soft:
            errors.append("SOFT_PENALTY_EDGE_SET_MISMATCH")
        if actual_blocked & actual_soft:
            errors.append("BLOCKED_EDGE_CANNOT_BE_SOFT")
        _validate_conditional_edge_policies(
            request=normalized_request,
            blocked_edge_ids=actual_blocked,
            soft_edge_ids=actual_soft,
            errors=errors,
        )
        if draft.objective_profile != normalized_request.constraints.objective_profile:
            errors.append("OBJECTIVE_PROFILE_MISMATCH")
        if normalized_request.constraints.objective_terms:
            if set(draft.objective_terms) != set(_objective_terms(normalized_request)):
                errors.append("OBJECTIVE_TERMS_MISMATCH")
        if draft.map_constraints.max_edge_wait_ms != normalized_request.constraints.max_edge_wait_ms:
            errors.append("MAX_WAIT_MISMATCH")

        return CuOptDynamicInputValidationResult(
            valid=not errors,
            repairable=False,
            errors=list(dict.fromkeys(errors)),
            warnings=warnings,
        )


class DynamicInputOptimizationRequestAdapter:
    """Mechanically map a validated draft to the existing solver-neutral request."""

    def build(
        self,
        *,
        draft: CuOptDynamicInputDraft,
        graph: WarehouseSituationGraph,
        map_context: MapContext,
    ) -> OptimizationRequest:
        """Build the typed output from validated source data."""
        robot_nodes = {
            str(node.attributes["robot_id"]): node
            for node in graph.nodes
            if node.node_type == "robot"
        }
        priority_penalties = {"high": 1_000_000_000, "medium": 100_000_000, "low": 10_000_000}
        tasks = [
            OptimizationTask(
                task_id=task.task_id,
                pickup_node=task.pickup_node,
                delivery_node=task.delivery_node,
                demand=task.demand,
                priority=task.priority,
                operation_type=task.operation_type,
                order_id=task.order_id,
                order_ids=([task.order_id] if task.operation_type == "OUTBOUND_ORDER" else []),
                item_id=task.item_id,
                stock_id=task.stock_id,
                logical_destination_ids=([task.delivery_node] if task.operation_type == "OUTBOUND_ORDER" else []),
                handling_unit_id=(task.stock_id if task.operation_type == "INBOUND_ITEM" else None),
                rack_id=task.rack_id,
                rack_level=task.rack_level,
                optional=not task.mandatory,
                unassigned_penalty=None if task.mandatory else priority_penalties[task.priority],
                fixed_robot_id=task.fixed_vehicle_id,
            )
            for task in draft.tasks
        ]
        vehicles = []
        for robot_id in draft.fleet.included_robot_ids:
            node = robot_nodes[robot_id]
            vehicles.append(
                OptimizationVehicle(
                    robot_id=robot_id,
                    start_node=str(node.attributes["current_node"]),
                    capacity_units=int(node.attributes["capacity_units"]),
                    battery_pct=float(node.attributes["battery_pct"]),
                    available_at_ms=int(node.attributes.get("sim_time_ms", 0)),
                )
            )
        canonical_penalties = {
            value.edge_id: value for value in map_context.map_constraints.edge_penalties
        }
        penalties: list[EdgePenalty] = []
        for edge_id in draft.map_constraints.soft_penalty_edge_ids:
            penalties.append(
                canonical_penalties.get(edge_id)
                or EdgePenalty(
                    edge_id=edge_id,
                    cost_multiplier=1.25,
                    travel_time_multiplier=1.25,
                    reason="Explicit soft-avoid constraint from the normalized request.",
                )
            )
        constraints = MapConstraints(
            blocked_edge_ids=list(draft.map_constraints.blocked_edge_ids),
            blocked_node_ids=list(map_context.map_constraints.blocked_node_ids),
            edge_penalties=penalties,
            edge_occupancies=list(map_context.map_constraints.edge_occupancies),
            edge_reservations=list(map_context.map_constraints.edge_reservations),
        )
        return OptimizationRequest(
            snapshot_id=draft.snapshot_id,
            tasks=tasks,
            vehicles=vehicles,
            map_constraints=constraints,
            objective_profile=draft.objective_profile,
            max_edge_wait_ms=draft.map_constraints.max_edge_wait_ms,
        )

    def build_from_contexts(
        self,
        *,
        draft: CuOptDynamicInputDraft,
        robots: RobotRuntimeContext,
        map_context: MapContext,
    ) -> OptimizationRequest:
        """Build an OptimizationRequest for the Rule path without a Situation Graph."""

        robot_by_id = {value.robot_id: value for value in robots.robots}
        priority_penalties = {"high": 1_000_000_000, "medium": 100_000_000, "low": 10_000_000}
        tasks = [
            OptimizationTask(
                task_id=task.task_id,
                pickup_node=task.pickup_node,
                delivery_node=task.delivery_node,
                demand=task.demand,
                priority=task.priority,
                operation_type=task.operation_type,
                order_id=task.order_id,
                order_ids=([task.order_id] if task.operation_type == "OUTBOUND_ORDER" else []),
                item_id=task.item_id,
                stock_id=task.stock_id,
                logical_destination_ids=([task.delivery_node] if task.operation_type == "OUTBOUND_ORDER" else []),
                handling_unit_id=(task.stock_id if task.operation_type == "INBOUND_ITEM" else None),
                rack_id=task.rack_id,
                rack_level=task.rack_level,
                optional=not task.mandatory,
                unassigned_penalty=None if task.mandatory else priority_penalties[task.priority],
                fixed_robot_id=task.fixed_vehicle_id,
            )
            for task in draft.tasks
        ]
        vehicles = [
            OptimizationVehicle(
                robot_id=robot_id,
                start_node=robot_by_id[robot_id].current_node,
                capacity_units=robot_by_id[robot_id].capacity_units,
                battery_pct=robot_by_id[robot_id].battery_pct,
                available_at_ms=robot_by_id[robot_id].sim_time_ms,
            )
            for robot_id in draft.fleet.included_robot_ids
        ]
        canonical_penalties = {value.edge_id: value for value in map_context.map_constraints.edge_penalties}
        penalties = [
            canonical_penalties.get(edge_id)
            or EdgePenalty(
                edge_id=edge_id,
                cost_multiplier=1.25,
                travel_time_multiplier=1.25,
                reason="Explicit soft-avoid constraint from the normalized request.",
            )
            for edge_id in draft.map_constraints.soft_penalty_edge_ids
        ]
        constraints = MapConstraints(
            blocked_edge_ids=list(draft.map_constraints.blocked_edge_ids),
            blocked_node_ids=list(map_context.map_constraints.blocked_node_ids),
            edge_penalties=penalties,
            edge_occupancies=list(map_context.map_constraints.edge_occupancies),
            edge_reservations=list(map_context.map_constraints.edge_reservations),
        )
        return OptimizationRequest(
            snapshot_id=draft.snapshot_id,
            tasks=tasks,
            vehicles=vehicles,
            map_constraints=constraints,
            objective_profile=draft.objective_profile,
            max_edge_wait_ms=draft.map_constraints.max_edge_wait_ms,
        )

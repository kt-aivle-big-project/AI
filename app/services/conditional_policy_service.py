"""Deterministic evaluation of typed runtime policies.

The input router may translate natural language such as "if H3_7 wait exceeds
8 seconds, hard avoid; otherwise soft avoid" into a closed typed contract.  At
that point no additional semantic Agent judgment is required: this service reads
the runtime snapshot and deterministically selects one of the declared actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.domain.schemas import ConditionalEdgePolicy, MapContext, WarehouseSituationGraph


@dataclass(frozen=True)
class ConditionalEdgeEvaluation:
    """Auditable result for one typed edge condition."""

    edge_id: str
    metric: str
    observed_value: int
    threshold_ms: int
    predicate_result: bool
    selected_action: str


def _compare(value: int, operator: str, threshold: int) -> bool:
    if operator == "GT":
        return value > threshold
    if operator == "GTE":
        return value >= threshold
    if operator == "LT":
        return value < threshold
    if operator == "LTE":
        return value <= threshold
    if operator == "EQ":
        return value == threshold
    raise ValueError(f"Unsupported conditional operator {operator!r}.")


def expected_wait_from_map_context(map_context: MapContext, edge_id: str) -> int:
    """Estimate immediate safe-entry wait from occupancy and reservation intervals."""

    values = [
        int(value.occupied_until_ms)
        for value in map_context.map_constraints.edge_occupancies
        if value.edge_id == edge_id
    ]
    values.extend(
        int(value.end_at_ms)
        for value in map_context.map_constraints.edge_reservations
        if value.edge_id == edge_id
    )
    return max(values, default=0)


def expected_wait_from_situation_graph(graph: WarehouseSituationGraph, edge_id: str) -> int:
    """Read immediate wait evidence materialized in a situation graph."""

    values: list[int] = []
    for node in graph.nodes:
        if node.node_type != "runtime_constraint":
            continue
        if str(node.attributes.get("edge_id", "")) != edge_id:
            continue
        for key in ("occupied_until_ms", "end_at_ms", "expected_wait_ms"):
            raw = node.attributes.get(key)
            if isinstance(raw, (int, float)):
                values.append(int(raw))
    return max(values, default=0)


def evaluate_policies(
    policies: Iterable[ConditionalEdgePolicy],
    *,
    wait_by_edge: dict[str, int],
) -> list[ConditionalEdgeEvaluation]:
    """Evaluate every policy without inventing an action outside its contract."""

    results: list[ConditionalEdgeEvaluation] = []
    for policy in policies:
        if policy.metric != "EXPECTED_WAIT_MS":
            raise ValueError(f"Unsupported conditional metric {policy.metric!r}.")
        observed = int(wait_by_edge.get(policy.edge_id, 0))
        result = _compare(observed, policy.operator, int(policy.threshold_ms))
        results.append(
            ConditionalEdgeEvaluation(
                edge_id=policy.edge_id,
                metric=policy.metric,
                observed_value=observed,
                threshold_ms=int(policy.threshold_ms),
                predicate_result=result,
                selected_action=policy.when_true if result else policy.when_false,
            )
        )
    return results


def apply_evaluations(
    *,
    blocked_edge_ids: set[str],
    soft_edge_ids: set[str],
    evaluations: Iterable[ConditionalEdgeEvaluation],
) -> tuple[set[str], set[str]]:
    """Apply one and only one declared action per conditional edge."""

    blocked = set(blocked_edge_ids)
    soft = set(soft_edge_ids)
    for value in evaluations:
        blocked.discard(value.edge_id)
        soft.discard(value.edge_id)
        if value.selected_action == "HARD_AVOID":
            blocked.add(value.edge_id)
        elif value.selected_action == "SOFT_AVOID":
            soft.add(value.edge_id)
        elif value.selected_action != "ALLOW":
            raise ValueError(f"Unsupported conditional action {value.selected_action!r}.")
    soft -= blocked
    return blocked, soft

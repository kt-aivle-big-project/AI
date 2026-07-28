"""Shared deterministic charger selection policy.

P16.5.8.1 uses this module from both the opportunity-charge planner and the
verification layer.  The verifier therefore checks the exact policy evidence
that the planner used instead of applying a second, incompatible cost rule.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


OPPORTUNITY_COST_POLICY = "MIN_OPPORTUNITY_TOTAL_COST"
OPPORTUNITY_DISTANCE_FALLBACK_POLICY = (
    "MIN_OPPORTUNITY_TRAVEL_DISTANCE_FALLBACK"
)


def is_opportunity_policy(value: object) -> bool:
    return str(value or "").upper() in {
        OPPORTUNITY_COST_POLICY,
        OPPORTUNITY_DISTANCE_FALLBACK_POLICY,
        "OPPORTUNITY_CHARGE_WITH_LINKED_WAITING_AREA",  # P16.5.8 compatibility
    }


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _feasible(row: dict[str, Any]) -> bool:
    return (
        row.get("safe_reachable") is not False
        and row.get("rejection_reason") in (None, "")
    )


def rank_opportunity_charger_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str, str]:
    """Rank opportunity-charge candidates using one uniform rule.

    Configured charger cost is used only when *every* feasible candidate has a
    comparable cost.  If even one feasible candidate lacks cost data, all
    configured costs are ignored for this decision and a distance fallback is
    applied uniformly.  This prevents a missing cost from silently behaving as
    zero and keeps planning and verification consistent.
    """

    ranked = [deepcopy(row) for row in candidates]
    feasible = [row for row in ranked if _feasible(row)]
    if not feasible:
        return ranked, None, OPPORTUNITY_DISTANCE_FALLBACK_POLICY, "NO_FEASIBLE_CANDIDATE"

    complete_cost_data = all(row.get("charger_cost") is not None for row in feasible)
    policy = (
        OPPORTUNITY_COST_POLICY
        if complete_cost_data
        else OPPORTUNITY_DISTANCE_FALLBACK_POLICY
    )
    cost_mode = (
        "ALL_FEASIBLE_CANDIDATES_HAVE_CONFIGURED_COST"
        if complete_cost_data
        else "UNIFORM_DISTANCE_FALLBACK_INCOMPLETE_COST_DATA"
    )

    keyed: list[tuple[tuple[float, float, float, int], dict[str, Any]]] = []
    for row in feasible:
        has_waiting_area = bool(row.get("linked_waiting_area_node_ids"))
        waiting_penalty = 0.0 if has_waiting_area else 100000.0
        travel_distance = _as_float(row.get("to_charger_distance")) + _as_float(
            row.get("charger_to_next_source_distance")
        )
        configured_component = 0.0
        if complete_cost_data:
            configured_component = _as_float(row.get("charger_cost")) * _as_float(
                row.get("charged_percent")
            )
        total_cost = travel_distance + configured_component
        duration = _as_float(row.get("charge_duration_seconds"))
        charger_node = int(row.get("charger_node"))
        key = (waiting_penalty, total_cost, duration, charger_node)
        row.update(
            {
                "cost_mode": cost_mode,
                "travel_distance_total": round(travel_distance, 6),
                "configured_cost_component": round(configured_component, 6),
                "total_selection_cost": round(total_cost, 6),
                "selection_key": [
                    round(waiting_penalty, 6),
                    round(total_cost, 6),
                    round(duration, 6),
                    charger_node,
                ],
                "selected": False,
            }
        )
        keyed.append((key, row))

    keyed.sort(key=lambda value: value[0])
    for rank, (_, row) in enumerate(keyed, start=1):
        row["selection_rank"] = rank
    selected = keyed[0][1]
    selected["selected"] = True

    # Replace the feasible rows in the original order so evidence remains easy
    # to compare with the source node list.
    by_node = {int(row["charger_node"]): row for _, row in keyed}
    output: list[dict[str, Any]] = []
    for row in ranked:
        node = row.get("charger_node")
        if node is not None and int(node) in by_node and _feasible(row):
            output.append(deepcopy(by_node[int(node)]))
        else:
            output.append(row)

    reason = (
        "모든 안전 후보에 충전 비용이 있어 이동거리와 설정 비용을 합산한 "
        "총비용이 가장 낮은 충전소를 선택했습니다."
        if complete_cost_data
        else "안전 후보 중 일부에 충전 비용이 없어 비용을 0으로 간주하지 않고 "
        "모든 후보에 동일하게 이동거리 기준 fallback을 적용했습니다."
    )
    return output, deepcopy(selected), policy, reason


def _recorded_selection_key(row: dict[str, Any]) -> tuple[float, float, float, int] | None:
    raw = row.get("selection_key")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        return (
            float(raw[0]),
            float(raw[1]),
            float(raw[2]),
            int(raw[3]),
        )
    except (TypeError, ValueError):
        return None


def expected_opportunity_candidate(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, str]:
    """Return the candidate selected by the planner's immutable evidence.

    Routing/energy reconciliation may legitimately adjust operational values
    such as the selected charger's battery-at-arrival or charge duration. Those
    post-route values must not cause verification to solve a second charger
    selection problem. When every feasible candidate has a recorded
    ``selection_key``, verification replays that exact planning evidence.
    Legacy evidence without keys falls back to the shared ranking function.
    """

    feasible = [deepcopy(row) for row in candidates if _feasible(row)]
    keyed = [
        (_recorded_selection_key(row), row)
        for row in feasible
        if _recorded_selection_key(row) is not None
    ]
    if feasible and len(keyed) == len(feasible):
        keyed.sort(key=lambda value: value[0])
        selected = deepcopy(keyed[0][1])
        cost_mode = str(selected.get("cost_mode") or "")
        if cost_mode == "ALL_FEASIBLE_CANDIDATES_HAVE_CONFIGURED_COST":
            policy = OPPORTUNITY_COST_POLICY
            reason = (
                "계획 시점에 기록된 후보 selection_key를 사용해 설정 비용과 "
                "이동거리 기반 선택 근거를 재현했습니다."
            )
        else:
            policy = OPPORTUNITY_DISTANCE_FALLBACK_POLICY
            reason = (
                "계획 시점에 기록된 후보 selection_key를 사용해 불완전 비용 "
                "데이터의 통일 거리 fallback 선택 근거를 재현했습니다."
            )
        return selected, policy, reason

    _, selected, policy, reason = rank_opportunity_charger_candidates(candidates)
    return selected, policy, reason

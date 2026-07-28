import heapq
import math
from datetime import UTC, datetime
from typing import Any, Protocol
from app.models import (
    CollisionFreePlan,
    CuOptPlan,
    ScheduledTask,
    TimedRoute,
    TimedWaypoint,
)
from app.services.scheduling import rebase_time_step
from app.services.task_ordering import dependency_aware_robot_task_ids
from app.services.mapf_replan import order_robot_ids, start_delay_steps


IDLE_ALLOWED_NODE_TYPES = {
    "PARKING",
    "STAGING",
    "HOLDING",
    "CHARGER_WAITING_AREA",
    "ROBOT_PARKING",
}

ROUTING_TASK_ORDER_POLICY = "START_TIME_DEPENDENCY_AWARE_PRIORITY_TIEBREAK"

FAILED_ROUTE_STATUSES = {"FAILED", "ROBOT_FAILED", "OFFLINE", "MAINTENANCE"}


IDLE_PROHIBITED_NODE_TYPES = {
    "ROUTE",
    "INTERSECTION",
    "STORAGE",
    "INBOUND",
    "OUTBOUND",
    "CHARGER",
    "DESTINATION",
}


class RoutingSettings(Protocol):
    routing_backend: str
    mapf_url: str
    mapf_fallback_to_internal: bool
    request_timeout_seconds: float
    time_step_seconds: int
    max_mapf_time_steps: int


def closed_resources(
    problem: dict[str, Any],
) -> tuple[set[int], set[tuple[int, int]]]:
    nodes: set[int] = set()
    edges: set[tuple[int, int]] = set()
    for row in problem.get("temporary_closures", []):
        if row.get("node_id") is not None:
            nodes.add(int(row["node_id"]))
        if row.get("from_node") is not None and row.get("to_node") is not None:
            edge = (int(row["from_node"]), int(row["to_node"]))
            edges.add(edge)
            if bool(row.get("bidirectional")) or str(
                row.get("direction", "")
            ).upper() in {"BOTH", "BIDIRECTIONAL"}:
                edges.add((edge[1], edge[0]))
    return nodes, edges


def active_node_ids(problem: dict[str, Any]) -> set[int]:
    closed_nodes, _ = closed_resources(problem)
    declared = {
        int(row["node_id"])
        for row in problem.get("nodes", [])
        if row.get("active", True) and int(row["node_id"]) not in closed_nodes
    }
    if "nodes" in problem:
        return declared
    return {
        int(value)
        for edge in problem.get("edges", [])
        for value in (edge["from_node"], edge["to_node"])
        if int(value) not in closed_nodes
    }


def active_edges(problem: dict[str, Any]) -> list[dict[str, Any]]:
    closed_nodes, closures = closed_resources(problem)
    return [
        edge
        for edge in problem.get("edges", [])
        if edge.get("active", True)
        and int(edge["from_node"]) not in closed_nodes
        and int(edge["to_node"]) not in closed_nodes
        and (int(edge["from_node"]), int(edge["to_node"])) not in closures
    ]


class PrioritizedTimeExpandedPlanner:
    """시간-노드·시간-간선 예약을 사용하는 결정적 MAPF 기반 경로기입니다."""

    def __init__(
        self,
        problem: dict[str, Any],
        time_step_seconds: int,
        max_time_steps: int,
    ):
        self.problem = problem
        self.time_step_seconds = time_step_seconds
        self.max_time_steps = max_time_steps
        self.stale_route_eviction_evidence: dict[str, Any] = {
            "version": "p16.5.14.1",
            "policy": "EVICT_EXCLUDED_OR_FAILED_ACTIVE_ROUTES",
            "changed_robot_ids": [],
            "evicted_robot_ids": [],
            "preserved_robot_ids": [],
        }
        self.congestion_node_ids = {
            int(value) for value in problem.get("congestion_node_ids", [])
        }
        self.congestion_penalty_steps = max(
            0, int(problem.get("congestion_penalty_steps") or 0)
        )
        configured_idle_types = problem.get("idle_allowed_node_types") or []
        self.idle_allowed_node_types = {
            str(value).upper() for value in configured_idle_types
        } or set(IDLE_ALLOWED_NODE_TYPES)
        self.explicit_idle_node_ids = {
            int(value) for value in problem.get("idle_allowed_node_ids", [])
        }
        self.idle_relocation_min_gap_steps = max(
            2,
            int(problem.get("idle_relocation_min_gap_steps") or 12),
        )
        hard_constraints = {
            str(value).upper() for value in problem.get("hard_constraints", [])
        }
        self.strict_idle_whitelist = bool(
            problem.get("idle_whitelist_strict")
            or "IDLE_ONLY_ON_WHITELISTED_NODE" in hard_constraints
        )
        self.valid_nodes = active_node_ids(problem)
        _, self.closed_edges = closed_resources(problem)
        self.adjacency: dict[int, list[tuple[int, int, float]]] = {}
        self.edge_distance: dict[tuple[int, int], float] = {}

        for edge in active_edges(problem):
            start = int(edge["from_node"])
            target = int(edge["to_node"])
            seconds = float(
                edge.get("travel_seconds")
                or edge.get("distance")
                or time_step_seconds
            )
            duration = max(1, math.ceil(seconds / time_step_seconds))
            distance = float(edge.get("distance") or 0.0)
            self.adjacency.setdefault(start, []).append((target, duration, distance))
            self.edge_distance[(start, target)] = distance
            if str(edge.get("direction", "ONE_WAY")).upper() in {
                "BOTH",
                "BIDIRECTIONAL",
            } and (target, start) not in self.closed_edges:
                self.adjacency.setdefault(target, []).append((start, duration, distance))
                self.edge_distance[(target, start)] = distance
        for neighbors in self.adjacency.values():
            neighbors.sort(key=lambda row: (row[0], row[1], row[2]))
        self.articulation_node_ids = self._compute_articulation_nodes()

    def _compute_articulation_nodes(self) -> set[int]:
        """Find cut vertices in the active map viewed as an undirected graph.

        A holding robot must never park on a gateway such as node 2044, which
        is the only connection to OUTBOUND 2146 in the demo map.  Occupying a
        cut vertex for a long idle interval would disconnect otherwise valid
        routes even though the holding node itself is not a service node.
        """

        neighbors: dict[int, set[int]] = {node: set() for node in self.valid_nodes}
        for start, rows in self.adjacency.items():
            for target, _duration, _distance in rows:
                neighbors.setdefault(start, set()).add(target)
                neighbors.setdefault(target, set()).add(start)

        discovery: dict[int, int] = {}
        low: dict[int, int] = {}
        parent: dict[int, int] = {}
        articulation: set[int] = set()
        clock = 0

        def visit(node: int) -> None:
            nonlocal clock
            clock += 1
            discovery[node] = clock
            low[node] = clock
            child_count = 0
            for neighbor in sorted(neighbors.get(node, set())):
                if neighbor not in discovery:
                    parent[neighbor] = node
                    child_count += 1
                    visit(neighbor)
                    low[node] = min(low[node], low[neighbor])
                    if node not in parent and child_count > 1:
                        articulation.add(node)
                    if node in parent and low[neighbor] >= discovery[node]:
                        articulation.add(node)
                elif parent.get(node) != neighbor:
                    low[node] = min(low[node], discovery[neighbor])

        for node in sorted(neighbors):
            if node not in discovery:
                visit(node)
        return articulation

    @staticmethod
    def _is_move_free(
        start: int,
        target: int,
        start_time: int,
        end_time: int,
        vertex_reservations: set[tuple[int, int]],
        edge_reservations: set[tuple[int, int, int]],
        *,
        robot_id: str | None = None,
        vertex_owners: dict[tuple[int, int], dict[str, Any]] | None = None,
        edge_owners: dict[tuple[int, int, int], dict[str, Any]] | None = None,
    ) -> bool:
        def owned_by_other(owner: dict[str, Any] | None) -> bool:
            if robot_id is None:
                return True
            if owner is None:
                # Legacy reservations without ownership remain conservative.
                return True
            return str(owner.get("robot_id")) != str(robot_id)

        vertex_key = (target, end_time)
        if vertex_key in vertex_reservations:
            owner = vertex_owners.get(vertex_key) if vertex_owners else None
            if owned_by_other(owner):
                return False
        for time_step in range(start_time, end_time):
            # Treat an aisle edge as a capacity-one resource. Reservations made
            # by the same robot are route continuity, not a collision.
            for edge_key in (
                (start, target, time_step),
                (target, start, time_step),
            ):
                if edge_key not in edge_reservations:
                    continue
                owner = edge_owners.get(edge_key) if edge_owners else None
                if owned_by_other(owner):
                    return False
        return True

    def shortest_time_path(
        self,
        start: int,
        goal: int,
        start_time: int,
        vertex_reservations: set[tuple[int, int]],
        edge_reservations: set[tuple[int, int, int]],
        *,
        robot_id: str | None = None,
        vertex_owners: dict[tuple[int, int], dict[str, Any]] | None = None,
        edge_owners: dict[tuple[int, int, int], dict[str, Any]] | None = None,
        goal_hold_steps: int = 0,
    ) -> list[TimedWaypoint]:
        if start not in self.valid_nodes or goal not in self.valid_nodes:
            return []

        def goal_hold_is_free(arrival_step: int) -> bool:
            for offset in range(1, max(0, goal_hold_steps) + 1):
                key = (goal, arrival_step + offset)
                if key not in vertex_reservations:
                    continue
                owner = vertex_owners.get(key) if vertex_owners else None
                if (
                    robot_id is None
                    or owner is None
                    or str(owner.get("robot_id")) != str(robot_id)
                ):
                    return False
            return True

        # Queue order is generalized route cost, then actual time. Entering a
        # command-specified congestion node receives a soft penalty, allowing a
        # slightly longer but less crowded aisle to win without changing the
        # real waypoint timestamps.
        queue: list[tuple[int, int, int, int]] = [(0, start_time, 0, start)]
        search_deadline_step = start_time + self.max_time_steps
        previous: dict[tuple[int, int], tuple[int, int] | None] = {
            (start, start_time): None
        }
        best_score: dict[tuple[int, int], tuple[int, int]] = {
            (start, start_time): (0, 0)
        }
        goal_state: tuple[int, int] | None = None

        while queue:
            score, time_step, hotspot_visits, node = heapq.heappop(queue)
            state_key = (node, time_step)
            if best_score.get(state_key) != (score, hotspot_visits):
                continue
            if node == goal and goal_hold_is_free(time_step):
                goal_state = state_key
                break
            if time_step >= search_deadline_step:
                continue

            candidates = [(node, 1, 0.0)] + self.adjacency.get(node, [])
            for neighbor, duration, _ in candidates:
                arrival = time_step + duration
                next_key = (neighbor, arrival)
                if arrival > search_deadline_step:
                    continue
                if not self._is_move_free(
                    node,
                    neighbor,
                    time_step,
                    arrival,
                    vertex_reservations,
                    edge_reservations,
                    robot_id=robot_id,
                    vertex_owners=vertex_owners,
                    edge_owners=edge_owners,
                ):
                    continue
                enters_hotspot = bool(
                    neighbor != node
                    and neighbor in self.congestion_node_ids
                    and neighbor not in {start, goal}
                )
                next_hotspot_visits = hotspot_visits + int(enters_hotspot)
                next_score = (
                    score
                    + duration
                    + (self.congestion_penalty_steps if enters_hotspot else 0)
                )
                candidate_score = (next_score, next_hotspot_visits)
                if candidate_score >= best_score.get(
                    next_key, (math.inf, math.inf)
                ):
                    continue
                best_score[next_key] = candidate_score
                previous[next_key] = state_key
                heapq.heappush(
                    queue,
                    (next_score, arrival, next_hotspot_visits, neighbor),
                )

        if goal_state is None:
            return []

        chain: list[tuple[int, int]] = []
        cursor: tuple[int, int] | None = goal_state
        while cursor is not None:
            chain.append(cursor)
            cursor = previous[cursor]
        chain.reverse()
        return [
            TimedWaypoint(
                node_id=node,
                time_step=time_step,
                action=(
                    "WAIT"
                    if index and node == chain[index - 1][0]
                    else "MOVE"
                ),
            )
            for index, (node, time_step) in enumerate(chain)
        ]

    def wait_path(
        self,
        node: int,
        start_time: int,
        end_time: int,
        vertex_reservations: set[tuple[int, int]],
        *,
        robot_id: str | None = None,
        vertex_owners: dict[tuple[int, int], dict[str, Any]] | None = None,
    ) -> list[TimedWaypoint]:
        if end_time <= start_time:
            return [TimedWaypoint(node_id=node, time_step=start_time)]
        result = [TimedWaypoint(node_id=node, time_step=start_time)]
        for time_step in range(start_time + 1, end_time + 1):
            key = (node, time_step)
            if key in vertex_reservations:
                owner = vertex_owners.get(key) if vertex_owners else None
                if robot_id is None or owner is None or str(owner.get("robot_id")) != str(robot_id):
                    return []
            result.append(
                TimedWaypoint(node_id=node, time_step=time_step, action="WAIT")
            )
        return result

    @staticmethod
    def reserve(
        waypoints: list[TimedWaypoint],
        vertex_reservations: set[tuple[int, int]],
        edge_reservations: set[tuple[int, int, int]],
        *,
        robot_id: str | None = None,
        task_id: str | None = None,
        vertex_owners: dict[tuple[int, int], dict[str, Any]] | None = None,
        edge_owners: dict[tuple[int, int, int], dict[str, Any]] | None = None,
    ) -> None:
        for waypoint in waypoints:
            vertex_reservations.add((waypoint.node_id, waypoint.time_step))
            if vertex_owners is not None and robot_id is not None:
                vertex_owners.setdefault(
                    (waypoint.node_id, waypoint.time_step),
                    {"robot_id": robot_id, "task_id": task_id},
                )
        for left, right in zip(waypoints, waypoints[1:]):
            for time_step in range(left.time_step, right.time_step):
                edge_reservations.add((left.node_id, right.node_id, time_step))
                if edge_owners is not None and robot_id is not None:
                    edge_owners.setdefault(
                        (left.node_id, right.node_id, time_step),
                        {"robot_id": robot_id, "task_id": task_id},
                    )

    def _node_type(self, node_id: int) -> str | None:
        for row in self.problem.get("nodes", []):
            if int(row.get("node_id")) == int(node_id):
                value = row.get("node_type") or row.get("type")
                return str(value).upper() if value is not None else None
        return None

    def _node_row(self, node_id: int) -> dict[str, Any] | None:
        for row in self.problem.get("nodes", []):
            if int(row.get("node_id")) == int(node_id):
                return row
        return None

    def _idle_allowed(self, node_id: int) -> bool:
        if int(node_id) in self.explicit_idle_node_ids:
            return True
        row = self._node_row(node_id) or {}
        if bool(row.get("idle_allowed")):
            return True
        node_type = self._node_type(node_id)
        return bool(node_type and node_type in self.idle_allowed_node_types)

    def _idle_node_priority(self, node_id: int) -> int:
        row = self._node_row(node_id) or {}
        try:
            return int(row.get("parking_priority") or 100)
        except (TypeError, ValueError):
            return 100

    def _idle_policy_violation_reason(self, node_id: int) -> str | None:
        if self._idle_allowed(node_id):
            return None
        node_type = self._node_type(node_id) or "UNKNOWN"
        if int(node_id) in self.articulation_node_ids:
            return "NO_IDLE_ON_ARTICULATION_NODE"
        if int(node_id) in self.congestion_node_ids:
            return "NO_IDLE_ON_CONGESTION_NODE"
        if node_type == "INTERSECTION":
            return "NO_IDLE_ON_INTERSECTION"
        if node_type in {"STORAGE", "INBOUND", "OUTBOUND", "DESTINATION"}:
            return "NO_IDLE_ON_SERVICE_NODE"
        if node_type == "CHARGER":
            return "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE"
        return "NO_IDLE_ON_TRANSIT_NODE"

    def _movement_duration(self, start: int, target: int) -> int:
        for neighbor, duration, _distance in self.adjacency.get(start, []):
            if neighbor == target:
                return duration
        return 1

    def _blocking_owner_for_wait(
        self,
        *,
        start: int,
        intended_target: int,
        depart_step: int,
        vertex_owners: dict[tuple[int, int], dict[str, Any]],
        edge_owners: dict[tuple[int, int, int], dict[str, Any]],
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        duration = self._movement_duration(start, intended_target)
        arrival_step = depart_step + duration
        vertex_owner = vertex_owners.get((intended_target, arrival_step))
        if vertex_owner:
            conflict_type = (
                "CHARGER_OCCUPANCY"
                if self._node_type(intended_target) == "CHARGER"
                else "VERTEX_OCCUPANCY"
            )
            return (
                conflict_type,
                f"NODE:{intended_target}@{arrival_step}",
                vertex_owner,
            )
        for time_step in range(depart_step, arrival_step):
            same_owner = edge_owners.get((start, intended_target, time_step))
            if same_owner:
                return (
                    "EDGE_CAPACITY",
                    f"EDGE:{start}->{intended_target}@{time_step}",
                    same_owner,
                )
            reverse_owner = edge_owners.get((intended_target, start, time_step))
            if reverse_owner:
                return (
                    "EDGE_SWAP",
                    f"EDGE:{start}<->{intended_target}@{time_step}",
                    reverse_owner,
                )
        return None, None, None

    @staticmethod
    def _next_distinct_node(
        waypoints: list[TimedWaypoint], index: int
    ) -> int | None:
        current = waypoints[index].node_id
        for waypoint in waypoints[index + 1 :]:
            if waypoint.node_id != current:
                return waypoint.node_id
        return None

    def path_distance(self, waypoints: list[TimedWaypoint]) -> float:
        return sum(
            self.edge_distance.get((left.node_id, right.node_id), 0.0)
            for left, right in zip(waypoints, waypoints[1:])
            if left.node_id != right.node_id
        )

    def _static_time_distances(self, start: int) -> dict[int, int]:
        """Return reservation-free shortest travel steps from ``start``.

        This small Dijkstra pass is used only to rank candidate holding nodes.
        The final relocation path is still produced by the time-expanded
        planner with all vertex/edge reservations applied.
        """

        distances: dict[int, int] = {int(start): 0}
        queue: list[tuple[int, int]] = [(0, int(start))]
        while queue:
            elapsed, node = heapq.heappop(queue)
            if elapsed != distances.get(node):
                continue
            for neighbor, duration, _distance in self.adjacency.get(node, []):
                candidate = elapsed + int(duration)
                if candidate >= distances.get(neighbor, math.inf):
                    continue
                distances[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
        return distances

    def _holding_node_candidates(
        self,
        *,
        current_node: int,
        next_source_node: int,
    ) -> list[int]:
        """Rank non-service nodes that can safely absorb a long idle gap.

        P16.5.7 no longer treats an arbitrary low-degree ROUTE node as a safe
        waiting place. Long idle is allowed only on explicitly designated
        PARKING/STAGING/HOLDING/CHARGER_WAITING_AREA nodes (or nodes carrying
        ``idle_allowed=true``). This is a hard safety policy, not a soft cost.
        """

        from_current = self._static_time_distances(current_node)
        from_next_source = self._static_time_distances(next_source_node)
        active_chargers = {
            int(row["node_id"])
            for row in self.problem.get("nodes", [])
            if row.get("active", True)
            and str(row.get("node_type") or "").upper() == "CHARGER"
        }
        current_type = self._node_type(current_node)
        preferred_charger = int(current_node) if current_type == "CHARGER" else None
        charger_area_first = str(
            self.problem.get("idle_return_policy") or "CHARGER_AREA_FIRST"
        ).upper() == "CHARGER_AREA_FIRST"
        ranked: list[tuple[int, int, int, int, int, int, int]] = []
        for node_id in sorted(self.valid_nodes):
            if node_id in {int(current_node), int(next_source_node)}:
                continue
            if not self._idle_allowed(node_id):
                continue
            if node_id in self.congestion_node_ids:
                continue
            if node_id in self.articulation_node_ids:
                continue
            if node_id not in from_current or node_id not in from_next_source:
                continue
            row = self._node_row(node_id) or {}
            linked_raw = row.get("linked_charger_node_id")
            linked_charger = int(linked_raw) if linked_raw is not None else None
            node_type = self._node_type(node_id)
            is_charger_area = bool(
                node_type == "CHARGER_WAITING_AREA"
                or linked_charger in active_chargers
            )
            exact_post_charge_area = bool(
                preferred_charger is not None
                and linked_charger == preferred_charger
            )
            degree = len(self.adjacency.get(node_id, []))
            ranked.append(
                (
                    0 if exact_post_charge_area else 1,
                    0 if (charger_area_first and is_charger_area) else 1,
                    self._idle_node_priority(node_id),
                    int(from_current[node_id] + from_next_source[node_id]),
                    int(from_current[node_id]),
                    int(degree),
                    int(node_id),
                )
            )
        candidate_limit = max(
            1,
            int(self.problem.get("idle_holding_candidate_limit") or 24),
        )
        return [row[6] for row in sorted(ranked)[:candidate_limit]]

    def _plan_idle_holding(
        self,
        *,
        robot_id: str,
        current_node: int,
        current_time: int,
        requested_start: int,
        next_source_node: int,
        vertex_reservations: set[tuple[int, int]],
        edge_reservations: set[tuple[int, int, int]],
        vertex_owners: dict[tuple[int, int], dict[str, Any]],
        edge_owners: dict[tuple[int, int, int], dict[str, Any]],
    ) -> tuple[int, list[TimedWaypoint], list[TimedWaypoint]] | None:
        """Move an idle robot off a shared service node before a long gap.

        Returns ``(holding_node, relocation_path, holding_wait)``.  The caller
        owns reservation/metadata updates so this helper can evaluate multiple
        candidates without mutating planner state.
        """

        gap_steps = int(requested_start) - int(current_time)
        minimum_gap = self.idle_relocation_min_gap_steps
        if gap_steps <= minimum_gap:
            return None
        if self._idle_allowed(current_node):
            return None

        for holding_node in self._holding_node_candidates(
            current_node=current_node,
            next_source_node=next_source_node,
        ):
            relocation = self.shortest_time_path(
                current_node,
                holding_node,
                current_time,
                vertex_reservations,
                edge_reservations,
                robot_id=robot_id,
                vertex_owners=vertex_owners,
                edge_owners=edge_owners,
            )
            if not relocation:
                continue
            arrival_step = int(relocation[-1].time_step)
            if arrival_step >= int(requested_start):
                continue
            holding_wait = self.wait_path(
                holding_node,
                arrival_step,
                requested_start,
                vertex_reservations,
                robot_id=robot_id,
                vertex_owners=vertex_owners,
            )
            if not holding_wait:
                continue
            # Verify that the robot can re-enter the task network when the next
            # window opens.  This avoids selecting a free but topologically or
            # temporally trapped holding point.
            return_path = self.shortest_time_path(
                holding_node,
                next_source_node,
                requested_start,
                vertex_reservations,
                edge_reservations,
                robot_id=robot_id,
                vertex_owners=vertex_owners,
                edge_owners=edge_owners,
            )
            if not return_path:
                continue
            return holding_node, relocation, holding_wait
        return None

    def seed_existing_reservations(
        self,
        cuopt_plan: CuOptPlan,
        vertex_reservations: set[tuple[int, int]],
        edge_reservations: set[tuple[int, int, int]],
    ) -> tuple[set[str], dict[str, TimedRoute]]:
        active_plan = self.problem.get("active_plan") or {}
        collision_plan = active_plan.get("collision_plan") or {}
        routes = collision_plan.get("routes") or []
        if not routes:
            return set(), {}

        current_step = 0
        activated_at_raw = active_plan.get("activated_at")
        candidate_reference_time = active_plan.get("reference_time") or activated_at_raw
        child_reference_time = self.problem.get("reference_time") or self.problem.get(
            "captured_at"
        )
        candidate_plan = bool(active_plan.get("candidate_plan"))
        if activated_at_raw and not candidate_plan:
            try:
                activated_at = datetime.fromisoformat(
                    str(activated_at_raw).replace("Z", "+00:00")
                )
                current_step = max(
                    0,
                    int(
                        (datetime.now(UTC) - activated_at).total_seconds()
                        // self.time_step_seconds
                    ),
                )
            except ValueError:
                current_step = 0

        # 재계획 중인 후보 계획 자체는 아직 실행되지 않았으므로 미래 구간을
        # freeze horizon으로 고정하지 않습니다. 실제 활성 계획의 보호 대상은
        # prepare_replan_node가 fixed/frozen task로 전달합니다.
        freeze_steps = (
            0
            if active_plan.get("candidate_plan")
            else math.ceil(
                int(self.problem.get("freeze_horizon_seconds") or 0)
                / self.time_step_seconds
            )
        )
        affected_robot_ids = {
            str(robot_id)
            for robot_id in self.problem.get("affected_robot_ids", [])
            if robot_id not in (None, "")
        }
        excluded_robot_ids = {
            str(robot_id)
            for robot_id in self.problem.get("excluded_robot_ids", [])
            if robot_id not in (None, "")
        }
        failed_robot_ids: set[str] = set(excluded_robot_ids)

        def collect_failed_overrides(raw: Any) -> None:
            if isinstance(raw, dict):
                rows = [
                    {"robot_id": robot_id, **(value if isinstance(value, dict) else {})}
                    for robot_id, value in raw.items()
                ]
            elif isinstance(raw, list):
                rows = [value for value in raw if isinstance(value, dict)]
            else:
                rows = []
            for row in rows:
                status = str(row.get("status") or "").upper()
                robot_id = row.get("robot_id")
                if robot_id not in (None, "") and status in FAILED_ROUTE_STATUSES:
                    failed_robot_ids.add(str(robot_id))

        collect_failed_overrides(self.problem.get("robot_state_overrides"))
        collect_failed_overrides(
            (self.problem.get("runtime_partial_replan") or {}).get(
                "robot_state_overrides"
            )
        )
        for robot in self.problem.get("robots", []):
            if not isinstance(robot, dict):
                continue
            status = str(robot.get("status") or "").upper()
            robot_id = robot.get("robot_id")
            if robot_id not in (None, "") and status in FAILED_ROUTE_STATUSES:
                failed_robot_ids.add(str(robot_id))

        # A robot can be changed for more than one independent reason.  The old
        # `set(cuopt_changed) or set(affected)` expression discarded the event
        # impact set whenever cuOpt changed at least one replacement robot.  In
        # a carried-load handover that left the failed robot's old active route
        # reserved even though the failed robot had been removed from the
        # optimization snapshot.
        changed = (
            {str(robot_id) for robot_id in cuopt_plan.changed_robot_ids}
            | affected_robot_ids
            | failed_robot_ids
        )
        scheduled_task_ids_by_robot: dict[str, set[str]] = {}
        for scheduled in cuopt_plan.scheduled_tasks:
            scheduled_task_ids_by_robot.setdefault(scheduled.robot_id, set()).add(
                scheduled.task_id
            )
        existing_routes: dict[str, TimedRoute] = {}

        evicted_robot_ids: set[str] = set()
        for raw_route in routes:
            robot_id = str(raw_route.get("robot_id"))
            if robot_id in failed_robot_ids:
                # Failed/excluded robots must contribute neither future task
                # ownership nor a preserved motion prefix.  Their physical stop
                # position is represented by the server-authoritative robot
                # state override and the synthetic handover task instead.
                evicted_robot_ids.add(robot_id)
                continue
            normalized: list[TimedWaypoint] = []
            for raw_waypoint in raw_route.get("waypoints", []):
                old_step = int(raw_waypoint["time_step"])
                if (
                    candidate_plan
                    and candidate_reference_time
                    and child_reference_time
                ):
                    relative_step = rebase_time_step(
                        old_step,
                        parent_reference_time=candidate_reference_time,
                        child_reference_time=child_reference_time,
                        time_step_seconds=self.time_step_seconds,
                    )
                else:
                    if old_step < current_step:
                        continue
                    relative_step = old_step - current_step
                if robot_id in changed and relative_step > freeze_steps:
                    continue
                normalized.append(
                    TimedWaypoint(
                        node_id=int(raw_waypoint["node_id"]),
                        time_step=relative_step,
                        action=raw_waypoint.get("action", "MOVE"),
                    )
                )
            if not normalized:
                continue
            route_task_id_list = [
                str(task_id) for task_id in raw_route.get("task_ids", [])
            ]
            if candidate_plan and robot_id in changed:
                # A rejected candidate plan may contribute the robot's current
                # position prefix, but none of its task ownership.  Carrying
                # the old task IDs into the newly routed candidate duplicated
                # A/B/C and stale CHARGE IDs in the final route evidence.
                route_task_id_list = []
            route_task_ids = set(route_task_id_list)
            scheduled_task_ids = scheduled_task_ids_by_robot.get(robot_id, set())
            if (
                robot_id not in changed
                and scheduled_task_ids
                and not scheduled_task_ids.issubset(route_task_ids)
            ):
                continue
            self.reserve(normalized, vertex_reservations, edge_reservations)
            existing_routes[robot_id] = TimedRoute(
                robot_id=robot_id,
                task_ids=route_task_id_list,
                waypoints=normalized,
                distance=self.path_distance(normalized),
            )
        self.stale_route_eviction_evidence = {
            "version": "p16.5.14.1",
            "policy": "EVICT_EXCLUDED_OR_FAILED_ACTIVE_ROUTES",
            "changed_robot_ids": sorted(changed),
            "evicted_robot_ids": sorted(evicted_robot_ids),
            "preserved_robot_ids": sorted(existing_routes),
        }
        return changed, existing_routes

    def solve(self, cuopt_plan: CuOptPlan) -> CollisionFreePlan:
        robots = {
            str(row["robot_id"]): row
            for row in self.problem["robots"]
        }
        task_by_id = {
            str(task.task_id): task for task in cuopt_plan.scheduled_tasks
        }
        dependency_rows = list(
            (cuopt_plan.metadata or {}).get("execution_task_dependencies", [])
            or []
        )
        ordered_task_ids, ordering_errors = dependency_aware_robot_task_ids(
            cuopt_plan.scheduled_tasks,
            dependency_rows,
        )
        if ordering_errors:
            raise RuntimeError("; ".join(ordering_errors))
        grouped: dict[str, list[ScheduledTask]] = {
            robot_id: [task_by_id[task_id] for task_id in task_ids]
            for robot_id, task_ids in ordered_task_ids.items()
        }
        mapf_replan_policy = self.problem.get("mapf_replan_policy") or {}
        # Reserve shared nodes in operational priority order.  Per-robot task
        # ordering is dependency-aware; the cross-robot baseline must use the
        # earliest executable task's start/priority rather than dictionary or
        # robot-id order, otherwise a normal task can reserve a bottleneck
        # before an emergency task listed later in the input.
        baseline_robot_order = sorted(
            grouped,
            key=lambda robot_id: min(
                (
                    int(task.start_time_step),
                    int(task.priority),
                    int(task.end_time_step),
                    str(task.task_id),
                )
                for task in grouped[robot_id]
            )
            + (str(robot_id),),
        )
        robot_processing_order = order_robot_ids(
            baseline_robot_order,
            mapf_replan_policy,
        )

        vertex_reservations: set[tuple[int, int]] = set()
        edge_reservations: set[tuple[int, int, int]] = set()
        vertex_owners: dict[tuple[int, int], dict[str, Any]] = {}
        edge_owners: dict[tuple[int, int, int], dict[str, Any]] = {}
        wait_evidence: list[dict[str, Any]] = []
        resolution_events: list[dict[str, Any]] = []
        idle_relocations: list[dict[str, Any]] = []
        idle_action_tasks: list[dict[str, Any]] = []
        idle_policy_violations: list[dict[str, Any]] = []
        route_sources: dict[str, str] = {}
        preserved_prefix_end_steps: dict[str, int] = {}
        # Keep the completion of each task separate from the route-wide
        # makespan.  A robot can execute more than one task in one route, so
        # the route's final waypoint alone is not enough to reconcile the
        # operating schedule after time-expanded routing adds waits/detours.
        task_completion_steps: dict[str, int] = {}
        task_start_steps: dict[str, int] = {}
        changed, existing_routes = self.seed_existing_reservations(
            cuopt_plan,
            vertex_reservations,
            edge_reservations,
        )
        # Reconstruct ownership for preserved reservations so newly routed
        # robots can report exactly which existing robot/task blocked them.
        for robot_id, route in existing_routes.items():
            owner_task_id = route.task_ids[0] if len(route.task_ids) == 1 else None
            self.reserve(
                route.waypoints,
                vertex_reservations,
                edge_reservations,
                robot_id=robot_id,
                task_id=owner_task_id,
                vertex_owners=vertex_owners,
                edge_owners=edge_owners,
            )
        routes: list[TimedRoute] = [
            route
            for robot_id, route in existing_routes.items()
            if (robot_id not in changed or robot_id not in grouped)
            and route.waypoints
        ]
        for route in routes:
            route_sources[route.robot_id] = "PRESERVED_ACTIVE_PLAN"
            preserved_prefix_end_steps[route.robot_id] = route.waypoints[-1].time_step
            if len(route.task_ids) == 1:
                task_start_steps[route.task_ids[0]] = route.waypoints[0].time_step
                task_completion_steps[route.task_ids[0]] = route.waypoints[-1].time_step

        for robot_id in robot_processing_order:
            scheduled_tasks = grouped[robot_id]
            existing_route = existing_routes.get(robot_id)
            scheduled_task_ids = {task.task_id for task in scheduled_tasks}
            can_reuse_existing_route = bool(
                existing_route
                and existing_route.waypoints
                and scheduled_task_ids.issubset(set(existing_route.task_ids))
            )
            if can_reuse_existing_route and robot_id not in changed:
                continue
            if robot_id not in robots:
                raise RuntimeError(
                    f"cuOpt 결과의 robot_id가 Snapshot에 없습니다: {robot_id}"
                )

            frozen_prefix = existing_routes.get(robot_id)
            if frozen_prefix and frozen_prefix.waypoints:
                current_node = frozen_prefix.waypoints[-1].node_id
                current_time = frozen_prefix.waypoints[-1].time_step
                full_waypoints = list(frozen_prefix.waypoints)
                task_ids = list(frozen_prefix.task_ids)
                total_distance = frozen_prefix.distance
                preserved_prefix_end_steps[robot_id] = frozen_prefix.waypoints[-1].time_step
            else:
                current_node = int(robots[robot_id]["node_id"])
                current_time = start_delay_steps(
                    robot_id,
                    robot_processing_order,
                    mapf_replan_policy,
                )
                full_waypoints = []
                task_ids = []
                total_distance = 0.0
            route_sources[robot_id] = "INTERNAL_ROUTE_SEARCH"

            for scheduled in scheduled_tasks:
                requested_start = max(current_time, scheduled.start_time_step)
                task_start_steps[scheduled.task_id] = requested_start

                # A snapshot can contain multiple idle robots at the same
                # dispatch/service node. Before planning their initial parking
                # relocation, activate each route at the first unreserved
                # vertex step. This preserves the physical one-robot-per-node
                # constraint from the first waypoint instead of creating a
                # time-zero vertex collision.
                if not full_waypoints:
                    activation_step = int(current_time)
                    activation_limit = activation_step + self.max_time_steps
                    while activation_step <= activation_limit:
                        key = (int(current_node), activation_step)
                        owner = vertex_owners.get(key)
                        if key not in vertex_reservations or (
                            owner is not None
                            and str(owner.get("robot_id")) == str(robot_id)
                        ):
                            break
                        activation_step += 1
                    if activation_step > activation_limit:
                        raise RuntimeError(
                            f"{robot_id}가 초기 노드 {current_node}에서 활성화될 수 없습니다."
                        )
                    if activation_step > current_time:
                        resolution_events.append(
                            {
                                "resolution": "INITIAL_ACTIVATION_DELAY",
                                "robot_id": robot_id,
                                "task_id": scheduled.task_id,
                                "node_id": int(current_node),
                                "old_start_step": int(current_time),
                                "new_start_step": int(activation_step),
                                "added_delay_steps": int(
                                    activation_step - current_time
                                ),
                                "reason": "SHARED_INITIAL_NODE_SEQUENCING",
                            }
                        )
                    current_time = activation_step
                    # Immediate first tasks must inherit the serialized activation
                    # step. Without this rebase, requested_start remains zero and
                    # the route recreates an overlapping t=0 waypoint.
                    requested_start = max(int(requested_start), int(current_time))
                    task_start_steps[scheduled.task_id] = requested_start

                # P16.5.7 treats long idle as an explicit local planning task.
                # A robot may not remain on a service node, route aisle,
                # intersection, cut vertex, congestion node, or charger slot.
                # This applies both between tasks and before the robot's first
                # future task; otherwise a sparse route silently assumes that a
                # robot blocks its snapshot node for hours.
                gap_steps = int(requested_start) - int(current_time)
                # A route that begins beyond the bounded MAPF horizon is not
                # active before its scheduled activation.  Do not interpret the
                # pre-activation interval as an in-plan idle reservation.  Once
                # a route has started, long idle remains subject to the strict
                # whitelist policy and must relocate to a designated node.
                defer_setting = self.problem.get("defer_initial_pre_activation")
                # A standalone routing problem may not carry the higher-level
                # activation flag.  In that case, a first task beyond the
                # bounded MAPF horizon is sparse by default; materializing
                # every step from t=0 creates fake multi-hour WAIT routes.
                # Higher-level planning can explicitly set False for an
                # already-active same-day plan that must use idle holding.
                defer_initial_pre_activation = (
                    (not self.strict_idle_whitelist)
                    if defer_setting is None
                    else bool(defer_setting)
                )
                sparse_future_start = bool(
                    defer_initial_pre_activation
                    and not full_waypoints
                    and gap_steps > self.max_time_steps
                )
                requires_idle_relocation = bool(
                    not sparse_future_start
                    and gap_steps > self.idle_relocation_min_gap_steps
                    and not self._idle_allowed(current_node)
                )
                # Before a far-future plan is activated, the robot remains
                # outside this candidate plan's reservation domain.  Creating a
                # relocation/wait task here would reserve a holding node for
                # hours and can exceed its maximum idle duration.  Once the
                # route is inside the bounded activation horizon, the existing
                # strict idle-whitelist policy applies unchanged.
                idle_holding = None
                if not sparse_future_start:
                    idle_holding = self._plan_idle_holding(
                        robot_id=robot_id,
                        current_node=current_node,
                        current_time=current_time,
                        requested_start=requested_start,
                        next_source_node=scheduled.source_node,
                        vertex_reservations=vertex_reservations,
                        edge_reservations=edge_reservations,
                        vertex_owners=vertex_owners,
                        edge_owners=edge_owners,
                    )
                if idle_holding is not None:
                    initial_idle_relocation = not full_waypoints
                    holding_node, relocation, holding_wait = idle_holding
                    relocation_distance = self.path_distance(relocation)
                    idle_segment = relocation + holding_wait[1:]
                    self.reserve(
                        idle_segment,
                        vertex_reservations,
                        edge_reservations,
                        robot_id=robot_id,
                        task_id=scheduled.task_id,
                        vertex_owners=vertex_owners,
                        edge_owners=edge_owners,
                    )
                    if (
                        full_waypoints
                        and idle_segment
                        and full_waypoints[-1].node_id == idle_segment[0].node_id
                        and full_waypoints[-1].time_step == idle_segment[0].time_step
                    ):
                        idle_segment = idle_segment[1:]
                    full_waypoints.extend(idle_segment)
                    total_distance += relocation_distance
                    wait_evidence.extend(
                        {
                            "robot_id": robot_id,
                            "task_id": scheduled.task_id,
                            "node_id": waypoint.node_id,
                            "time_step": waypoint.time_step,
                            "reason": "IDLE_WHITELIST_WAIT",
                            "conflict_type": None,
                            "blocked_resource": None,
                            "blocked_by_robot_id": None,
                            "blocked_by_task_id": None,
                            "added_delay_steps": 1,
                            "holding_node_id": holding_node,
                        }
                        for waypoint in holding_wait[1:]
                    )
                    holding_row = self._node_row(holding_node) or {}
                    holding_type = self._node_type(holding_node)
                    linked_charger_node_id = holding_row.get(
                        "linked_charger_node_id"
                    )
                    idle_behavior = (
                        "LEAVE_CHARGER_SLOT_TO_WAITING_AREA"
                        if self._node_type(current_node) == "CHARGER"
                        and linked_charger_node_id is not None
                        and int(linked_charger_node_id) == int(current_node)
                        else "RETURN_TO_CHARGER_AREA_AND_WAIT"
                        if holding_type == "CHARGER_WAITING_AREA"
                        or linked_charger_node_id is not None
                        else "RETURN_TO_WHITELISTED_IDLE_NODE"
                    )
                    relocation_row = {
                        "resolution": "IDLE_RELOCATION",
                        "robot_id": robot_id,
                        "task_id": scheduled.task_id,
                        "from_node": current_node,
                        "holding_node_id": holding_node,
                        "depart_step": current_time,
                        "arrive_step": relocation[-1].time_step,
                        "resume_step": requested_start,
                        "distance": relocation_distance,
                        "holding_node_type": holding_type,
                        "linked_charger_node_id": (
                            int(linked_charger_node_id)
                            if linked_charger_node_id is not None
                            else None
                        ),
                        "idle_behavior": idle_behavior,
                        "idle_whitelist_valid": self._idle_allowed(holding_node),
                        "reason": (
                            "INITIAL_IDLE_RELOCATION_TO_WHITELIST"
                            if initial_idle_relocation
                            else "INTER_TASK_IDLE_RELOCATION_TO_WHITELIST"
                        ),
                    }
                    idle_relocations.append(relocation_row)
                    resolution_events.append(relocation_row)
                    action_sequence = len(idle_action_tasks) + 1
                    idle_action_tasks.extend(
                        [
                            {
                                "idle_task_id": (
                                    f"idle:{robot_id}:{action_sequence}:relocate"
                                ),
                                "robot_id": robot_id,
                                "action": "MOVE_TO_IDLE_NODE",
                                "source_node": int(relocation[0].node_id),
                                "target_node": int(holding_node),
                                "start_time_step": int(relocation[0].time_step),
                                "end_time_step": int(relocation[-1].time_step),
                                "distance": relocation_distance,
                                "next_task_id": scheduled.task_id,
                                "policy": "IDLE_ONLY_ON_WHITELISTED_NODE",
                                "behavior_action": (
                                    "MOVE_TO_CHARGER_WAITING_AREA"
                                    if idle_behavior in {
                                        "LEAVE_CHARGER_SLOT_TO_WAITING_AREA",
                                        "RETURN_TO_CHARGER_AREA_AND_WAIT",
                                    }
                                    else "MOVE_TO_WHITELISTED_IDLE_NODE"
                                ),
                                "linked_charger_node_id": (
                                    int(linked_charger_node_id)
                                    if linked_charger_node_id is not None
                                    else None
                                ),
                            },
                            {
                                "idle_task_id": (
                                    f"idle:{robot_id}:{action_sequence}:wait"
                                ),
                                "robot_id": robot_id,
                                "action": "WAIT_AT_IDLE_NODE",
                                "source_node": int(holding_node),
                                "target_node": int(holding_node),
                                "start_time_step": int(relocation[-1].time_step),
                                "end_time_step": int(requested_start),
                                "distance": 0.0,
                                "next_task_id": scheduled.task_id,
                                "policy": "IDLE_ONLY_ON_WHITELISTED_NODE",
                                "behavior_action": (
                                    "WAIT_AT_CHARGER_WAITING_AREA"
                                    if idle_behavior in {
                                        "LEAVE_CHARGER_SLOT_TO_WAITING_AREA",
                                        "RETURN_TO_CHARGER_AREA_AND_WAIT",
                                    }
                                    else "WAIT_AT_WHITELISTED_IDLE_NODE"
                                ),
                                "linked_charger_node_id": (
                                    int(linked_charger_node_id)
                                    if linked_charger_node_id is not None
                                    else None
                                ),
                            },
                        ]
                    )
                    current_node = holding_node
                    current_time = requested_start
                elif requires_idle_relocation:
                    violation = {
                        "robot_id": robot_id,
                        "task_id": scheduled.task_id,
                        "node_id": int(current_node),
                        "node_type": self._node_type(current_node),
                        "gap_start_step": int(current_time),
                        "gap_end_step": int(requested_start),
                        "gap_steps": int(gap_steps),
                        "reason": self._idle_policy_violation_reason(current_node),
                        "code": "IDLE_NODE_NOT_CONFIGURED",
                    }
                    idle_policy_violations.append(violation)
                    if self.strict_idle_whitelist:
                        raise RuntimeError(
                            "IDLE_NODE_NOT_CONFIGURED: "
                            f"{robot_id}가 노드 {current_node}에서 "
                            f"{gap_steps} step 동안 대기해야 하지만 "
                            "PARKING/STAGING/HOLDING/CHARGER_WAITING_AREA "
                            "노드가 없거나 예약할 수 없습니다."
                        )

                # A far-future first segment is represented from its absolute
                # scheduled start.  Materializing every idle step from zero
                # would create thousands of fake WAIT waypoints/reservations.
                if sparse_future_start:
                    # Multiple idle robots may be recorded at the same staging
                    # node in a snapshot.  Activate the later-priority route at
                    # the first free time step instead of emitting overlapping
                    # initial waypoints that simulation would flag as a vertex
                    # collision.
                    activation_step = int(requested_start)
                    activation_limit = activation_step + self.max_time_steps
                    while activation_step <= activation_limit:
                        key = (int(current_node), activation_step)
                        owner = vertex_owners.get(key)
                        if key not in vertex_reservations or (
                            owner is not None
                            and str(owner.get("robot_id")) == str(robot_id)
                        ):
                            break
                        activation_step += 1
                    if activation_step > activation_limit:
                        raise RuntimeError(
                            f"{robot_id}가 작업 {scheduled.task_id} 시작 노드에서 활성화될 수 없습니다."
                        )
                    requested_start = activation_step
                    task_start_steps[scheduled.task_id] = requested_start
                    current_time = requested_start
                    waiting = [
                        TimedWaypoint(
                            node_id=current_node,
                            time_step=requested_start,
                        )
                    ]
                else:
                    waiting = self.wait_path(
                        current_node,
                        current_time,
                        requested_start,
                        vertex_reservations,
                        robot_id=robot_id,
                        vertex_owners=vertex_owners,
                    )
                if not waiting:
                    raise RuntimeError(
                        f"{robot_id}가 작업 {scheduled.task_id} 시작까지 안전하게 대기할 수 없습니다."
                    )
                if len(waiting) > 1:
                    wait_evidence.extend(
                        {
                            "robot_id": robot_id,
                            "task_id": scheduled.task_id,
                            "node_id": waypoint.node_id,
                            "time_step": waypoint.time_step,
                            "reason": "SCHEDULED_START_WAIT",
                            "blocked_by_robot_id": None,
                            "blocked_by_task_id": None,
                            "added_delay_steps": 1,
                        }
                        for waypoint in waiting[1:]
                    )
                    self.reserve(
                        waiting,
                        vertex_reservations,
                        edge_reservations,
                        robot_id=robot_id,
                        task_id=scheduled.task_id,
                        vertex_owners=vertex_owners,
                        edge_owners=edge_owners,
                    )
                    if (
                        full_waypoints
                        and full_waypoints[-1].node_id == waiting[0].node_id
                        and full_waypoints[-1].time_step == waiting[0].time_step
                    ):
                        # wait_path() includes the current boundary waypoint.
                        # The existing route may store that same boundary as
                        # WAIT while wait_path creates it as MOVE, so comparing
                        # the complete Pydantic model leaves a duplicate time.
                        waiting = waiting[1:]
                    full_waypoints.extend(waiting)
                current_time = requested_start
                to_source = self.shortest_time_path(
                    current_node,
                    scheduled.source_node,
                    current_time,
                    vertex_reservations,
                    edge_reservations,
                    robot_id=robot_id,
                    vertex_owners=vertex_owners,
                    edge_owners=edge_owners,
                )
                if not to_source:
                    raise RuntimeError(
                        f"{robot_id} → 작업 {scheduled.task_id} 출발지 경로 없음"
                    )
                current_time = to_source[-1].time_step
                to_target = self.shortest_time_path(
                    scheduled.source_node,
                    scheduled.target_node,
                    current_time,
                    vertex_reservations,
                    edge_reservations,
                    robot_id=robot_id,
                    vertex_owners=vertex_owners,
                    edge_owners=edge_owners,
                    goal_hold_steps=(
                        1 if scheduled.action in {"PICK", "DROP"} else 0
                    ),
                )
                if not to_target:
                    raise RuntimeError(
                        f"작업 {scheduled.task_id} 목적지 경로 없음"
                    )

                combined = to_source + to_target[1:]
                baseline_source = self.shortest_time_path(
                    current_node,
                    scheduled.source_node,
                    requested_start,
                    set(),
                    set(),
                )
                baseline_target = self.shortest_time_path(
                    scheduled.source_node,
                    scheduled.target_node,
                    baseline_source[-1].time_step if baseline_source else requested_start,
                    set(),
                    set(),
                )
                baseline_combined = (
                    baseline_source + baseline_target[1:]
                    if baseline_source and baseline_target
                    else []
                )
                for index, (left, right) in enumerate(
                    zip(combined, combined[1:])
                ):
                    if right.action != "WAIT" and left.node_id != right.node_id:
                        continue
                    intended_target = self._next_distinct_node(combined, index)
                    conflict_type = None
                    blocked_resource = None
                    owner = None
                    if intended_target is not None:
                        conflict_type, blocked_resource, owner = (
                            self._blocking_owner_for_wait(
                                start=left.node_id,
                                intended_target=intended_target,
                                depart_step=left.time_step,
                                vertex_owners=vertex_owners,
                                edge_owners=edge_owners,
                            )
                        )
                    wait_row = {
                        "robot_id": robot_id,
                        "task_id": scheduled.task_id,
                        "node_id": right.node_id,
                        "time_step": right.time_step,
                        "reason": "RESERVATION_CONFLICT_WAIT",
                        "conflict_type": conflict_type or "RESERVATION_CONFLICT",
                        "blocked_resource": blocked_resource,
                        "blocked_by_robot_id": (
                            str(owner.get("robot_id")) if owner else None
                        ),
                        "blocked_by_task_id": (
                            str(owner.get("task_id"))
                            if owner and owner.get("task_id") is not None
                            else None
                        ),
                        "added_delay_steps": max(
                            1, right.time_step - left.time_step
                        ),
                    }
                    wait_evidence.append(wait_row)
                    resolution_events.append(
                        {
                            "resolution": "WAIT",
                            **wait_row,
                        }
                    )

                actual_nodes = [
                    waypoint.node_id
                    for waypoint in combined
                    if waypoint.action != "WAIT"
                ]
                baseline_nodes = [
                    waypoint.node_id
                    for waypoint in baseline_combined
                    if waypoint.action != "WAIT"
                ]
                if (
                    baseline_nodes
                    and actual_nodes != baseline_nodes
                    and not any(
                        waypoint.action == "WAIT" for waypoint in combined
                    )
                ):
                    conflict_type = None
                    blocked_resource = None
                    owner = None
                    for left, right in zip(
                        baseline_combined, baseline_combined[1:]
                    ):
                        if left.node_id == right.node_id:
                            continue
                        conflict_type, blocked_resource, owner = (
                            self._blocking_owner_for_wait(
                                start=left.node_id,
                                intended_target=right.node_id,
                                depart_step=left.time_step,
                                vertex_owners=vertex_owners,
                                edge_owners=edge_owners,
                            )
                        )
                        if owner:
                            break
                    resolution_events.append(
                        {
                            "resolution": "REROUTE",
                            "robot_id": robot_id,
                            "task_id": scheduled.task_id,
                            "baseline_nodes": baseline_nodes,
                            "actual_nodes": actual_nodes,
                            "reason": "RESERVATION_CONFLICT_REROUTE",
                            "conflict_type": conflict_type,
                            "blocked_resource": blocked_resource,
                            "blocked_by_robot_id": (
                                str(owner.get("robot_id")) if owner else None
                            ),
                            "blocked_by_task_id": (
                                str(owner.get("task_id"))
                                if owner and owner.get("task_id") is not None
                                else None
                            ),
                        }
                    )
                # PICK/DROP include at least one processing step in the
                # optimizer schedule.  Pure same-node operations otherwise
                # finish at their start step because the route has no travel.
                # Materialize the remaining operation duration at the target
                # so routing metadata, commands and simulation share one clock.
                if scheduled.action in {"PICK", "DROP"}:
                    scheduled_duration = max(
                        1,
                        int(scheduled.end_time_step)
                        - int(scheduled.start_time_step),
                    )
                    route_elapsed = max(
                        0,
                        int(to_target[-1].time_step) - int(requested_start),
                    )
                    remaining_operation_steps = max(
                        0,
                        scheduled_duration - route_elapsed,
                    )
                    if remaining_operation_steps:
                        operation_end_step = (
                            int(to_target[-1].time_step)
                            + remaining_operation_steps
                        )
                        operation_dwell = self.wait_path(
                            scheduled.target_node,
                            int(to_target[-1].time_step),
                            operation_end_step,
                            vertex_reservations,
                            robot_id=robot_id,
                            vertex_owners=vertex_owners,
                        )
                        if not operation_dwell:
                            raise RuntimeError(
                                f"{robot_id} 작업 {scheduled.task_id} 처리 시간 예약 충돌"
                            )
                        for waypoint in operation_dwell[1:]:
                            waypoint.action = scheduled.action
                        combined.extend(operation_dwell[1:])

                if not combined:
                    raise RuntimeError(
                        "EMPTY_ROUTE_SEGMENT: "
                        f"{robot_id} 작업 {scheduled.task_id}의 경로 구간이 비어 있습니다."
                    )
                segment_end_time = int(combined[-1].time_step)
                segment_distance = self.path_distance(combined)
                self.reserve(
                    combined,
                    vertex_reservations,
                    edge_reservations,
                    robot_id=robot_id,
                    task_id=scheduled.task_id,
                    vertex_owners=vertex_owners,
                    edge_owners=edge_owners,
                )
                if (
                    full_waypoints
                    and combined
                    and full_waypoints[-1].node_id == combined[0].node_id
                    and full_waypoints[-1].time_step == combined[0].time_step
                ):
                    # A replanned candidate can begin its first synthetic CHARGE
                    # segment exactly at the preserved prefix endpoint.  Removing
                    # that duplicate can legitimately leave no new waypoint when
                    # source == target.  Keep the already captured segment end
                    # time instead of indexing the emptied list.
                    combined = combined[1:]
                full_waypoints.extend(combined)
                total_distance += segment_distance
                task_ids.append(scheduled.task_id)
                current_node = scheduled.target_node
                current_time = segment_end_time
                if scheduled.action == "CHARGE":
                    if scheduled.charge_duration_seconds is not None:
                        charge_steps = math.ceil(
                            max(0, scheduled.charge_duration_seconds)
                            / self.time_step_seconds
                        )
                        charge_end_step = current_time + charge_steps
                    else:
                        # Backward compatibility for older persisted plans that
                        # encoded the dwell only in end_time_step.
                        charge_end_step = max(current_time, scheduled.end_time_step)
                    if charge_end_step > current_time:
                        charge_dwell = self.wait_path(
                            current_node,
                            current_time,
                            charge_end_step,
                            vertex_reservations,
                            robot_id=robot_id,
                            vertex_owners=vertex_owners,
                        )
                        if not charge_dwell:
                            raise RuntimeError(
                                f"{robot_id} 충전소 {current_node} 충전 예약 충돌"
                            )
                        for waypoint in charge_dwell[1:]:
                            waypoint.action = "CHARGE"
                        wait_evidence.extend(
                            {
                                "robot_id": robot_id,
                                "task_id": scheduled.task_id,
                                "node_id": waypoint.node_id,
                                "time_step": waypoint.time_step,
                                "reason": "CHARGING",
                                "blocked_by_robot_id": None,
                                "blocked_by_task_id": None,
                                "added_delay_steps": 1,
                            }
                            for waypoint in charge_dwell[1:]
                        )
                        self.reserve(
                            charge_dwell,
                            vertex_reservations,
                            edge_reservations,
                            robot_id=robot_id,
                            task_id=scheduled.task_id,
                            vertex_owners=vertex_owners,
                            edge_owners=edge_owners,
                        )
                        full_waypoints.extend(charge_dwell[1:])
                        current_time = charge_end_step
                task_completion_steps[scheduled.task_id] = current_time

            routes.append(
                TimedRoute(
                    robot_id=robot_id,
                    task_ids=task_ids,
                    waypoints=full_waypoints,
                    distance=total_distance,
                )
            )

        recorded_waits = {
            (
                str(row["robot_id"]),
                int(row["node_id"]),
                int(row["time_step"]),
            )
            for row in wait_evidence
        }
        for route in routes:
            task_id = route.task_ids[0] if len(route.task_ids) == 1 else None
            for left, right in zip(route.waypoints, route.waypoints[1:]):
                if left.node_id != right.node_id:
                    continue
                key = (route.robot_id, right.node_id, right.time_step)
                if key in recorded_waits:
                    continue
                owner = vertex_owners.get((right.node_id, right.time_step))
                if owner and str(owner.get("robot_id")) == str(route.robot_id):
                    # Same-robot service dwell and route continuity are not
                    # reservation conflicts and must not create null blockers.
                    continue
                if right.action in {"PICK", "DROP"}:
                    continue
                wait_evidence.append(
                    {
                        "robot_id": route.robot_id,
                        "task_id": task_id,
                        "node_id": right.node_id,
                        "time_step": right.time_step,
                        "reason": (
                            "CHARGING"
                            if right.action == "CHARGE"
                            else "RESERVATION_CONFLICT_WAIT"
                        ),
                        "conflict_type": (
                            None
                            if right.action == "CHARGE"
                            else "RESERVATION_CONFLICT"
                        ),
                        "blocked_resource": None,
                        "blocked_by_robot_id": None,
                        "blocked_by_task_id": None,
                        "added_delay_steps": max(
                            1, right.time_step - left.time_step
                        ),
                    }
                )
                recorded_waits.add(key)

        # Final safety gate: no long idle interval may remain on an aisle,
        # intersection, service node, cut vertex, congestion node, or charger
        # slot. Short conflict-resolution waits are allowed; long operational
        # idle must be represented by an explicit whitelist idle task.
        for route in routes:
            index = 0
            waypoints = route.waypoints
            while index < len(waypoints) - 1:
                left = waypoints[index]
                right = waypoints[index + 1]
                if left.node_id != right.node_id or right.action != "WAIT":
                    index += 1
                    continue
                node_id = int(left.node_id)
                start_step = int(left.time_step)
                end_step = int(right.time_step)
                cursor = index + 1
                while cursor < len(waypoints) - 1:
                    current = waypoints[cursor]
                    following = waypoints[cursor + 1]
                    if (
                        current.node_id != node_id
                        or following.node_id != node_id
                        or following.action != "WAIT"
                    ):
                        break
                    end_step = int(following.time_step)
                    cursor += 1
                duration_steps = end_step - start_step
                if (
                    duration_steps > self.idle_relocation_min_gap_steps
                    and not self._idle_allowed(node_id)
                ):
                    violation = {
                        "robot_id": route.robot_id,
                        "task_id": task_id,
                        "node_id": node_id,
                        "node_type": self._node_type(node_id),
                        "gap_start_step": start_step,
                        "gap_end_step": end_step,
                        "gap_steps": duration_steps,
                        "reason": self._idle_policy_violation_reason(node_id),
                        "code": "LONG_IDLE_ON_PROHIBITED_NODE",
                    }
                    idle_policy_violations.append(violation)
                index = max(index + 1, cursor)

        if self.strict_idle_whitelist and idle_policy_violations:
            first = idle_policy_violations[0]
            raise RuntimeError(
                "LONG_IDLE_ON_PROHIBITED_NODE: "
                f"{first['robot_id']}가 노드 {first['node_id']}에서 "
                f"{first['gap_steps']} step 동안 대기합니다."
            )

        return CollisionFreePlan(
            engine="PRIORITIZED_TIME_ASTAR",
            routes=routes,
            time_step_seconds=self.time_step_seconds,
            total_distance=sum(route.distance for route in routes),
            metadata={
                "routing_backend": "internal",
                "task_ordering_policy": ROUTING_TASK_ORDER_POLICY,
                "mapf_replan_policy": mapf_replan_policy,
                "robot_processing_order": robot_processing_order,
                "vertex_reservations": len(vertex_reservations),
                "edge_reservations": len(edge_reservations),
                "wait_evidence": wait_evidence,
                "route_sources": route_sources,
                "preserved_prefix_end_steps": preserved_prefix_end_steps,
                "stale_route_eviction": self.stale_route_eviction_evidence,
                "task_completion_steps": task_completion_steps,
                "task_start_steps": task_start_steps,
                "resolution_events": resolution_events,
                "idle_relocations": idle_relocations,
                "idle_relocation_count": len(idle_relocations),
                "idle_action_tasks": idle_action_tasks,
                "idle_action_task_count": len(idle_action_tasks),
                "idle_energy_policy": {
                    "idle_return_policy": str(
                        self.problem.get("idle_return_policy")
                        or "CHARGER_AREA_FIRST"
                    ),
                    "opportunity_charging_enabled": bool(
                        self.problem.get("opportunity_charging_enabled", False)
                    ),
                    "charger_slot_idle_allowed": False,
                    "post_charge_behavior": "LEAVE_SLOT_TO_LINKED_WAITING_AREA",
                },
                "idle_policy": {
                    "strict": self.strict_idle_whitelist,
                    "allowed_node_types": sorted(self.idle_allowed_node_types),
                    "explicit_allowed_node_ids": sorted(
                        self.explicit_idle_node_ids
                    ),
                    "minimum_gap_steps": self.idle_relocation_min_gap_steps,
                    "prohibited_node_types": sorted(IDLE_PROHIBITED_NODE_TYPES),
                    "violation_count": len(idle_policy_violations),
                    "violations": idle_policy_violations,
                },
                "reroute_count": sum(
                    1
                    for row in resolution_events
                    if row.get("resolution") == "REROUTE"
                ),
                "conflict_wait_count": sum(
                    1
                    for row in resolution_events
                    if row.get("resolution") == "WAIT"
                ),
                "congestion_avoidance": {
                    "node_ids": sorted(self.congestion_node_ids),
                    "penalty_steps": self.congestion_penalty_steps,
                    "traversal_count": sum(
                        1
                        for route in routes
                        for left, right in zip(route.waypoints, route.waypoints[1:])
                        if left.node_id != right.node_id
                        and right.node_id in self.congestion_node_ids
                    ),
                },
            },
        )


def build_collision_plan(
    problem: dict[str, Any],
    cuopt_plan: CuOptPlan,
    settings: RoutingSettings,
) -> CollisionFreePlan:
    if settings.routing_backend == "internal":
        planner = PrioritizedTimeExpandedPlanner(
            problem,
            settings.time_step_seconds,
            settings.max_mapf_time_steps,
        )
        return planner.solve(cuopt_plan)
    if settings.routing_backend != "mapf":
        raise RuntimeError(f"지원하지 않는 ROUTING_BACKEND: {settings.routing_backend}")
    if not settings.mapf_url:
        raise RuntimeError("ROUTING_BACKEND=mapf에는 MAPF_URL이 필요합니다.")
    try:
        import httpx

        response = httpx.post(
            settings.mapf_url.rstrip("/") + "/plan",
            json={
                "problem": problem,
                "cuopt_plan": cuopt_plan.model_dump(mode="json"),
            },
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        plan = CollisionFreePlan.model_validate(response.json())
        plan.metadata["routing_backend"] = "mapf"
        return plan
    except Exception as exc:
        if not settings.mapf_fallback_to_internal:
            raise
        planner = PrioritizedTimeExpandedPlanner(
            problem,
            settings.time_step_seconds,
            settings.max_mapf_time_steps,
        )
        plan = planner.solve(cuopt_plan)
        plan.metadata["fallback_warning"] = (
            f"외부 MAPF 실패로 internal routing을 사용했습니다: {exc}"
        )
        return plan

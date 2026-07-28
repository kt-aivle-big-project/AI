import heapq
import math
from typing import Any

from app.models import (
    AtomicTask,
    CuOptPlan,
    ObjectiveBreakdown,
    OptimizationCandidateEvidence,
    ScheduledTask,
    TaskOptimizationEvidence,
)
from app.time_utils import planning_reference_time, task_tardiness_steps
from app.services.scheduling import rebase_preserved_task, relative_time_step


UNAVAILABLE_STATUSES = {
    "FAILED",
    "ROBOT_FAILED",
    "OFFLINE",
    "MAINTENANCE",
    "CHARGING",
    "DISABLED",
}


def _closed_resources(
    problem: dict[str, Any],
) -> tuple[set[int], set[tuple[int, int]]]:
    closed_nodes: set[int] = set()
    closed_edges: set[tuple[int, int]] = set()
    for closure in problem.get("temporary_closures", []):
        if closure.get("node_id") is not None:
            closed_nodes.add(int(closure["node_id"]))
        if closure.get("from_node") is not None and closure.get("to_node") is not None:
            edge = (int(closure["from_node"]), int(closure["to_node"]))
            closed_edges.add(edge)
            if bool(closure.get("bidirectional")) or str(
                closure.get("direction", "")
            ).upper() in {"BOTH", "BIDIRECTIONAL"}:
                closed_edges.add((edge[1], edge[0]))
    return closed_nodes, closed_edges


class LocalOptimizer:
    """작은 창고 문제를 위한 결정론적 CPU greedy-insertion 최적화기입니다."""

    def __init__(
        self,
        *,
        time_step_seconds: int,
        min_robot_battery: float,
        energy_per_distance: float,
        charge_target_battery: float = 80.0,
        charge_rate_percent_per_minute: float = 5.0,
        battery_safety_margin_percent: float = 0.5,
    ):
        self.time_step_seconds = max(1, time_step_seconds)
        self.min_robot_battery = min_robot_battery
        self.energy_per_distance = energy_per_distance
        self.charge_target_battery = charge_target_battery
        self.charge_rate_percent_per_minute = max(0.001, charge_rate_percent_per_minute)
        self.battery_safety_margin_percent = max(
            0.0, battery_safety_margin_percent
        )
        self.last_optimization_evidence: list[TaskOptimizationEvidence] = []
        self.last_objective_breakdown: ObjectiveBreakdown | None = None

    def _graph(
        self,
        problem: dict[str, Any],
    ) -> dict[int, list[tuple[int, float, float]]]:
        closed_nodes, closed_edges = _closed_resources(problem)
        valid_nodes = {
            int(row["node_id"])
            for row in problem.get("nodes", [])
            if row.get("active", True) and int(row["node_id"]) not in closed_nodes
        }
        graph: dict[int, list[tuple[int, float, float]]] = {
            node_id: [] for node_id in valid_nodes
        }
        for edge in problem.get("edges", []):
            start = int(edge["from_node"])
            target = int(edge["to_node"])
            if (
                not edge.get("active", True)
                or start not in valid_nodes
                or target not in valid_nodes
                or (start, target) in closed_edges
            ):
                continue
            distance = float(edge.get("distance") or 1.0)
            seconds = float(edge.get("travel_seconds") or distance)
            graph[start].append((target, distance, seconds))
            if str(edge.get("direction", "ONE_WAY")).upper() in {
                "BOTH",
                "BIDIRECTIONAL",
            } and (target, start) not in closed_edges:
                graph[target].append((start, distance, seconds))
        for neighbors in graph.values():
            neighbors.sort(key=lambda row: (row[0], row[1], row[2]))
        return graph

    @staticmethod
    def _shortest(
        graph: dict[int, list[tuple[int, float, float]]],
        start: int,
        target: int,
    ) -> tuple[float, float] | None:
        if start == target and start in graph:
            return 0.0, 0.0
        if start not in graph or target not in graph:
            return None
        queue: list[tuple[float, float, int]] = [(0.0, 0.0, start)]
        best: dict[int, tuple[float, float]] = {start: (0.0, 0.0)}
        while queue:
            distance, seconds, node = heapq.heappop(queue)
            if best.get(node) != (distance, seconds):
                continue
            if node == target:
                return distance, seconds
            for neighbor, edge_distance, edge_seconds in graph.get(node, []):
                candidate = (distance + edge_distance, seconds + edge_seconds)
                if candidate < best.get(neighbor, (math.inf, math.inf)):
                    best[neighbor] = candidate
                    heapq.heappush(queue, (*candidate, neighbor))
        return None

    @staticmethod
    def _charger_cost(node: dict[str, Any]) -> float | None:
        """Return an optional comparable charger-cost value.

        P10 map data does not define one canonical cost property. P11 accepts
        common aliases without inventing a value. When no candidate has a
        configured cost, selection falls back to distance and records that
        fact in the plan evidence.
        """

        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        for name in (
            "charging_cost",
            "charge_cost",
            "charger_cost",
            "price_per_percent",
            "cost",
        ):
            value = node.get(name, properties.get(name))
            if value in (None, ""):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric >= 0:
                return numeric
        return None

    def _charge_option(
        self,
        graph: dict[int, list[tuple[int, float, float]]],
        problem: dict[str, Any],
        *,
        robot_node: int,
        battery: float,
        source: int,
        target: int,
        operation: tuple[float, float],
        downstream_distance: float = 0.0,
        downstream_seconds: float = 0.0,
        skip_source_after_charge: bool = False,
    ) -> dict[str, Any] | None:
        """Choose the cheapest *safely reachable* active charger.

        P16.3.3 separates two battery policies:

        * ``min_robot_battery`` is the path-wide reserve that must never be
          crossed while travelling to a charger or performing work.
        * ``charge_target_battery`` is the operation-ready battery target.
          Once charging is required, the robot is charged to this target
          (80% by default), or higher when the remaining mission requires it.

        Charger cost is evaluated only after unsafe candidates are filtered.
        When no safe configured-cost candidate exists, the closest safe
        candidate is used as an explicit fallback.
        """

        closed_nodes, _ = _closed_resources(problem)
        charger_rows = sorted(
            (
                row
                for row in problem.get("nodes", [])
                if row.get("active", True)
                and int(row["node_id"]) not in closed_nodes
                and str(row.get("node_type") or "").upper() == "CHARGER"
            ),
            key=lambda row: int(row["node_id"]),
        )
        safety_margin = max(
            0.0,
            float(
                problem.get("battery_safety_margin_percent")
                if problem.get("battery_safety_margin_percent") is not None
                else self.battery_safety_margin_percent
            ),
        )
        minimum_arrival_battery = self.min_robot_battery + safety_margin
        configured_target = float(
            problem.get("charge_target_battery") or self.charge_target_battery
        )

        options: list[dict[str, Any]] = []
        candidate_evidence: list[dict[str, Any]] = []
        for charger in charger_rows:
            charger_node = int(charger["node_id"])
            to_charger = self._shortest(graph, robot_node, charger_node)
            if to_charger is None:
                candidate_evidence.append(
                    {
                        "charger_node": charger_node,
                        "active": bool(charger.get("active", True)),
                        "charger_cost": self._charger_cost(charger),
                        "safe_reachable": False,
                        "minimum_arrival_battery": round(
                            minimum_arrival_battery, 6
                        ),
                        "rejection_reason": "CHARGER_UNREACHABLE",
                        "selected": False,
                    }
                )
                continue

            battery_at_charger = battery - to_charger[0] * self.energy_per_distance
            if battery_at_charger + 1e-9 < minimum_arrival_battery:
                candidate_evidence.append(
                    {
                        "charger_node": charger_node,
                        "active": bool(charger.get("active", True)),
                        "charger_cost": self._charger_cost(charger),
                        "to_charger_distance": round(float(to_charger[0]), 6),
                        "battery_at_charger": round(battery_at_charger, 6),
                        "minimum_arrival_battery": round(
                            minimum_arrival_battery, 6
                        ),
                        "safe_reachable": False,
                        "rejection_reason": "BATTERY_BELOW_SAFE_ARRIVAL_THRESHOLD",
                        "selected": False,
                    }
                )
                continue

            if skip_source_after_charge:
                # A DROP task whose paired PICK has already completed carries
                # the load on the robot. After charging it can continue directly
                # to the destination instead of revisiting the pickup node.
                from_charger = self._shortest(graph, charger_node, target)
                task_source_node = charger_node
                if from_charger is None:
                    candidate_evidence.append(
                        {
                            "charger_node": charger_node,
                            "active": bool(charger.get("active", True)),
                            "charger_cost": self._charger_cost(charger),
                            "to_charger_distance": round(float(to_charger[0]), 6),
                            "battery_at_charger": round(battery_at_charger, 6),
                            "minimum_arrival_battery": round(
                                minimum_arrival_battery, 6
                            ),
                            "safe_reachable": False,
                            "rejection_reason": "POST_CHARGE_TARGET_UNREACHABLE",
                            "selected": False,
                        }
                    )
                    continue
                task_distance = from_charger[0]
                task_seconds = from_charger[1]
            else:
                from_charger = self._shortest(graph, charger_node, source)
                task_source_node = source
                if from_charger is None:
                    candidate_evidence.append(
                        {
                            "charger_node": charger_node,
                            "active": bool(charger.get("active", True)),
                            "charger_cost": self._charger_cost(charger),
                            "to_charger_distance": round(float(to_charger[0]), 6),
                            "battery_at_charger": round(battery_at_charger, 6),
                            "minimum_arrival_battery": round(
                                minimum_arrival_battery, 6
                            ),
                            "safe_reachable": False,
                            "rejection_reason": "POST_CHARGE_SOURCE_UNREACHABLE",
                            "selected": False,
                        }
                    )
                    continue
                task_distance = from_charger[0] + operation[0]
                task_seconds = from_charger[1] + operation[1]

            mission_distance = task_distance + max(0.0, downstream_distance)
            mission_seconds = task_seconds + max(0.0, downstream_seconds)
            mission_energy = mission_distance * self.energy_per_distance
            minimum_target = self.min_robot_battery + mission_energy
            target_battery = min(
                100.0, max(configured_target, minimum_target)
            )
            if target_battery - mission_energy + 1e-9 < self.min_robot_battery:
                candidate_evidence.append(
                    {
                        "charger_node": charger_node,
                        "active": bool(charger.get("active", True)),
                        "charger_cost": self._charger_cost(charger),
                        "to_charger_distance": round(float(to_charger[0]), 6),
                        "battery_at_charger": round(battery_at_charger, 6),
                        "minimum_arrival_battery": round(
                            minimum_arrival_battery, 6
                        ),
                        "safe_reachable": False,
                        "rejection_reason": "MISSION_EXCEEDS_BATTERY_CAPACITY",
                        "selected": False,
                    }
                )
                continue

            charged_percent = max(0.0, target_battery - battery_at_charger)
            charge_steps = math.ceil(
                (
                    charged_percent
                    / float(
                        problem.get("charge_rate_percent_per_minute")
                        or self.charge_rate_percent_per_minute
                    )
                )
                * 60
                / self.time_step_seconds
            )
            option = {
                "charger_node": charger_node,
                "active": bool(charger.get("active", True)),
                "charger_cost": self._charger_cost(charger),
                "to_charger_distance": to_charger[0],
                "to_charger_seconds": to_charger[1],
                "battery_at_charger": battery_at_charger,
                "minimum_arrival_battery": minimum_arrival_battery,
                "battery_safety_margin_percent": safety_margin,
                "safe_reachable": True,
                "task_source_node": task_source_node,
                "task_distance": task_distance,
                "task_seconds": task_seconds,
                "task_energy": task_distance * self.energy_per_distance,
                "mission_distance": mission_distance,
                "mission_seconds": mission_seconds,
                "mission_energy": mission_energy,
                "charge_steps": max(0, charge_steps),
                "charge_duration_seconds": max(0, charge_steps)
                * self.time_step_seconds,
                "target_battery": target_battery,
                "charged_percent": charged_percent,
                "projected_final_battery": target_battery - mission_energy,
                "total_distance": to_charger[0] + mission_distance,
            }
            options.append(option)
            candidate_evidence.append(
                {
                    "charger_node": charger_node,
                    "active": bool(charger.get("active", True)),
                    "charger_cost": option["charger_cost"],
                    "total_distance": round(float(option["total_distance"]), 6),
                    "to_charger_distance": round(
                        float(option["to_charger_distance"]), 6
                    ),
                    "battery_at_charger": round(battery_at_charger, 6),
                    "minimum_arrival_battery": round(
                        minimum_arrival_battery, 6
                    ),
                    "safe_reachable": True,
                    "charged_percent": round(float(charged_percent), 6),
                    "target_battery": round(float(target_battery), 6),
                    "charge_duration_seconds": int(
                        option["charge_duration_seconds"]
                    ),
                    "rejection_reason": None,
                    "selected": False,
                }
            )

        if not options:
            return None

        cost_options = [row for row in options if row["charger_cost"] is not None]
        if cost_options:
            selected = min(
                cost_options,
                key=lambda row: (
                    float(row["charger_cost"]),
                    row["total_distance"],
                    row["charge_steps"],
                    row["charger_node"],
                ),
            )
            policy = "MIN_SAFE_CONFIGURED_CHARGER_COST"
            reason = (
                "안전 도달 기준을 충족한 active 충전소 중 설정 비용이 "
                f"가장 낮은 노드 {selected['charger_node']}를 선택했습니다."
            )
        else:
            selected = min(
                options,
                key=lambda row: (
                    row["to_charger_distance"],
                    row["total_distance"],
                    row["charge_steps"],
                    row["charger_node"],
                ),
            )
            policy = "SAFE_DISTANCE_FALLBACK_NO_COST_DATA"
            reason = (
                "안전 도달 가능한 active 충전소에 비교 가능한 비용 속성이 없어 "
                f"충전소 도달거리 기준으로 노드 {selected['charger_node']}를 "
                "선택했습니다."
            )

        for row in candidate_evidence:
            if int(row.get("charger_node") or -1) == int(selected["charger_node"]):
                row["selected"] = True

        return {
            **selected,
            "selection_policy": policy,
            "selection_reason": reason,
            "candidates": candidate_evidence,
        }

    def _remaining_group_route(
        self,
        graph: dict[int, list[tuple[int, float, float]]],
        ordered_tasks: list[AtomicTask],
        current_index: int,
        *,
        current_target: int,
    ) -> tuple[float, float] | None:
        """Estimate the remaining route for the current same-robot mission.

        PICK and DROP tasks generated for one outbound trip share a
        ``same_robot_group``.  Looking only at the current atomic task can delay
        charging until after PICK, when the robot may no longer be able to reach
        a charger while preserving the minimum reserve.  This deterministic
        look-ahead estimates the shortest remaining chain for that group so the
        optimizer can charge at the earliest safe point.
        """

        task = ordered_tasks[current_index]
        if not task.same_robot_group:
            return 0.0, 0.0
        node = current_target
        total_distance = 0.0
        total_seconds = 0.0
        for future in ordered_tasks[current_index + 1 :]:
            if future.same_robot_group != task.same_robot_group:
                continue
            best: tuple[float, float, int] | None = None
            for source in sorted(set(future.source_candidates)):
                approach = self._shortest(graph, node, source)
                if approach is None:
                    continue
                for target in sorted(set(future.target_candidates or [source])):
                    operation = self._shortest(graph, source, target)
                    if operation is None:
                        continue
                    candidate = (
                        approach[0] + operation[0],
                        approach[1] + operation[1],
                        target,
                    )
                    if best is None or candidate < best:
                        best = candidate
            if best is None:
                return None
            total_distance += best[0]
            total_seconds += best[1]
            node = best[2]
        return total_distance, total_seconds

    @staticmethod
    def _task_order(tasks: list[AtomicTask]) -> list[AtomicTask]:
        by_id = {task.task_id: task for task in tasks}
        remaining = set(by_id)
        completed: set[str] = set()
        ordered: list[AtomicTask] = []
        while remaining:
            ready = [
                by_id[task_id]
                for task_id in remaining
                if all(
                    predecessor in completed or predecessor not in by_id
                    for predecessor in by_id[task_id].predecessors
                )
            ]
            if not ready:
                raise RuntimeError(
                    "작업 선후관계에 순환이 있습니다: " + ", ".join(sorted(remaining))
                )
            ready.sort(
                key=lambda task: (
                    task.priority,
                    task.deadline.isoformat() if task.deadline else "9999-12-31",
                    task.task_id,
                )
            )
            selected = ready[0]
            ordered.append(selected)
            completed.add(selected.task_id)
            remaining.remove(selected.task_id)
        return ordered

    def _existing_tasks(self, problem: dict[str, Any]) -> dict[str, ScheduledTask]:
        active_plan = problem.get("active_plan") or {}
        raw_plan = active_plan.get("cuopt_plan") or {}
        parent_reference_time = active_plan.get("reference_time") or active_plan.get(
            "activated_at"
        ) or raw_plan.get("metadata", {}).get("reference_time")
        child_reference_time = problem.get("reference_time") or problem.get(
            "captured_at"
        )
        result: dict[str, ScheduledTask] = {}
        for raw in raw_plan.get("scheduled_tasks", []):
            task = ScheduledTask.model_validate(raw)
            if (
                active_plan.get("candidate_plan")
                and parent_reference_time
                and child_reference_time
            ):
                task = rebase_preserved_task(
                    task,
                    parent_reference_time=parent_reference_time,
                    child_reference_time=child_reference_time,
                    time_step_seconds=self.time_step_seconds,
                )
            result[task.task_id] = task
        return result

    @staticmethod
    def _should_preserve(
        task: AtomicTask,
        existing: ScheduledTask | None,
        problem: dict[str, Any],
    ) -> bool:
        if existing is None:
            return False
        mode = str(problem.get("plan_mode") or "INITIAL_PLAN")
        fixed = {str(value) for value in problem.get("fixed_task_ids", [])}
        changeable = {str(value) for value in problem.get("changeable_task_ids", [])}
        if task.frozen or task.task_id in fixed or str(task.work_id) in fixed:
            return True
        if mode == "INSERT_TASK":
            return task.task_id not in changeable and str(task.work_id) not in changeable
        if mode == "LOCAL_REPLAN" and changeable:
            return task.task_id not in changeable and str(task.work_id) not in changeable
        return False

    @staticmethod
    def _robot_available(robot: dict[str, Any], minimum_battery: float) -> bool:
        status = str(robot.get("status") or "").upper()
        live_status = str(robot.get("live_status") or "").upper()
        battery = float(robot.get("battery") or 0.0)
        return (
            status not in UNAVAILABLE_STATUSES
            and live_status not in UNAVAILABLE_STATUSES
            and battery >= 0
            and robot.get("node_id") is not None
        )

    @staticmethod
    def _robot_unavailable_reason(
        robot: dict[str, Any], minimum_battery: float
    ) -> str | None:
        status = str(robot.get("status") or "").upper()
        live_status = str(robot.get("live_status") or "").upper()
        if status in UNAVAILABLE_STATUSES:
            return "ROBOT_STATUS_UNAVAILABLE"
        if live_status in UNAVAILABLE_STATUSES:
            return "ROBOT_LIVE_STATUS_UNAVAILABLE"
        if float(robot.get("battery") or 0.0) < 0:
            return "BATTERY_BELOW_ZERO"
        if robot.get("node_id") is None:
            return "ROBOT_START_NODE_MISSING"
        return None

    @staticmethod
    def _candidate_components(
        *,
        distance: float,
        end_step: int,
        tardiness_steps: int,
        energy: float,
        activation: int,
        change: int,
        weights: dict[str, Any],
    ) -> dict[str, float]:
        return {
            "distance": distance * float(weights["total_distance"]),
            "makespan": end_step * float(weights["makespan"]),
            "tardiness": tardiness_steps * float(weights["tardiness"]),
            "energy": energy * float(weights["energy"]),
            "robot_activation": activation
            * float(weights["robot_activation"]),
            "plan_change": change * float(weights["plan_change"]),
        }

    def optimize(self, problem: dict[str, Any]) -> CuOptPlan:
        self.last_optimization_evidence = []
        self.last_objective_breakdown = None
        graph = self._graph(problem)
        tasks = [AtomicTask.model_validate(row) for row in problem.get("tasks", [])]
        existing = self._existing_tasks(problem)
        weights = {
            "total_distance": 1.0,
            "makespan": 1.0,
            "tardiness": 5.0,
            "energy": 1.0,
            "robot_activation": 0.5,
            "plan_change": 2.0,
            "charging_time": 0.2,
            "charger_wait": 0.5,
            "charger_visit": 1.0,
            "congestion": 1.0,
            "shared_resource_occupancy": 0.05,
            "unnecessary_charger_roundtrip": 1.0,
            **problem.get("weights", {}),
        }
        explicit_charge_specs = problem.get("explicit_charge_task_specs") or {}
        reference_time = planning_reference_time(problem)
        raw_robots = sorted(
            problem.get("robots", []), key=lambda row: str(row["robot_id"])
        )

        robot_state: dict[str, dict[str, Any]] = {}
        for raw in raw_robots:
            if not self._robot_available(raw, self.min_robot_battery):
                continue
            robot_id = str(raw["robot_id"])
            robot_state[robot_id] = {
                "node_id": int(raw["node_id"]),
                "time_step": 0,
                "battery": float(raw.get("battery") or 0.0),
                "max_load": float(raw.get("max_load") or math.inf),
                "current_load": float(raw.get("current_load") or 0.0),
            }

        scheduled: list[ScheduledTask] = []
        preserved_ids: set[str] = set()
        active_robot_ids: set[str] = set()
        changed_robot_ids: set[str] = set()
        task_end: dict[str, int] = {}
        same_robot_assignments: dict[str, str] = {}
        parallel_rebalance = bool(problem.get("allow_local_robot_rebalance"))
        parallel_group_penalty = max(
            0.0, float(problem.get("parallel_robot_group_penalty") or 20.0)
        )
        robot_group_counts: dict[str, int] = {
            str(raw["robot_id"]): 0 for raw in raw_robots
        }
        counted_groups: set[str] = set()
        plan_changes = 0
        evidence_rows: list[TaskOptimizationEvidence] = []
        charger_selections: list[dict[str, Any]] = []
        execution_task_dependencies: list[dict[str, Any]] = []
        dependency_keys: set[tuple[str, str]] = set()

        def add_execution_dependency(
            predecessor_task_id: str,
            successor_task_id: str,
            source: str,
        ) -> None:
            key = (str(predecessor_task_id), str(successor_task_id))
            if not key[0] or not key[1] or key in dependency_keys:
                return
            dependency_keys.add(key)
            execution_task_dependencies.append(
                {
                    "predecessor_task_id": key[0],
                    "successor_task_id": key[1],
                    "dependency_type": "FINISH_TO_START",
                    "lag_seconds": 0,
                    "source": source,
                }
            )

        for task in tasks:
            for predecessor_task_id in task.predecessors:
                add_execution_dependency(
                    predecessor_task_id, task.task_id, "PLANNER_PREDECESSOR"
                )
        evidence_order = 0

        for task in tasks:
            old = existing.get(task.task_id)
            if not self._should_preserve(task, old, problem):
                continue
            if old.robot_id not in robot_state:
                continue
            state = robot_state[old.robot_id]
            activation = 0 if old.robot_id in active_robot_ids else 1
            tardiness = task_tardiness_steps(
                deadline=task.deadline,
                reference_time=reference_time,
                task_end_time_step=old.end_time_step,
                time_step_seconds=self.time_step_seconds,
            )
            components = self._candidate_components(
                distance=old.estimated_distance,
                end_step=old.end_time_step,
                tardiness_steps=tardiness,
                energy=old.estimated_energy,
                activation=activation,
                change=0,
                weights=weights,
            )
            evidence_order += 1
            evidence_rows.append(
                TaskOptimizationEvidence(
                    task_id=task.task_id,
                    task_order=evidence_order,
                    priority=task.priority,
                    selection_mode="PRESERVED_ASSIGNMENT",
                    tie_break_rule=[],
                    candidate_count=1,
                    selected_robot_id=old.robot_id,
                    selected_source_node=old.source_node,
                    selected_target_node=old.target_node,
                    candidates=[
                        OptimizationCandidateEvidence(
                            task_id=task.task_id,
                            robot_id=old.robot_id,
                            feasible=True,
                            selected=True,
                            robot_start_node=int(state["node_id"]),
                            source_node=old.source_node,
                            target_node=old.target_node,
                            distance=old.estimated_distance,
                            duration_time_steps=max(
                                0, old.end_time_step - old.start_time_step
                            ),
                            end_time_step=old.end_time_step,
                            energy=old.estimated_energy,
                            tardiness_time_steps=tardiness,
                            activation_indicator=activation,
                            plan_change_indicator=0,
                            robot_activation_cost=round(
                                components["robot_activation"], 6
                            ),
                            plan_change_cost=round(components["plan_change"], 6),
                            incremental_objective=round(sum(components.values()), 6),
                            objective_components={
                                key: round(value, 6)
                                for key, value in components.items()
                            },
                            rejection_reason=None,
                        )
                    ],
                )
            )
            scheduled.append(old)
            preserved_ids.add(task.task_id)
            active_robot_ids.add(old.robot_id)
            if old.end_time_step >= state["time_step"]:
                state["node_id"] = old.target_node
                state["time_step"] = old.end_time_step
                state["battery"] -= old.estimated_energy
            task_end[old.task_id] = old.end_time_step
            if task.same_robot_group:
                same_robot_assignments[task.same_robot_group] = old.robot_id
                if task.same_robot_group not in counted_groups:
                    counted_groups.add(task.same_robot_group)
                    robot_group_counts[old.robot_id] = (
                        robot_group_counts.get(old.robot_id, 0) + 1
                    )

        unassigned: list[str] = []
        total_distance = sum(task.estimated_distance for task in scheduled)
        total_energy = sum(task.estimated_energy for task in scheduled)
        tasks_by_id = {task.task_id: task for task in tasks}
        total_tardiness_steps = sum(
            task_tardiness_steps(
                deadline=tasks_by_id[scheduled_task.task_id].deadline,
                reference_time=reference_time,
                task_end_time_step=scheduled_task.end_time_step,
                time_step_seconds=self.time_step_seconds,
            )
            for scheduled_task in scheduled
            if scheduled_task.task_id in tasks_by_id
        )

        ordered_tasks = self._task_order(
            [row for row in tasks if row.task_id not in preserved_ids]
        )
        for task_index, task in enumerate(ordered_tasks):
            explicit_charge_spec = (
                explicit_charge_specs.get(task.task_id)
                if task.action == "CHARGE"
                else None
            )
            missing_predecessors = [
                predecessor
                for predecessor in task.predecessors
                if predecessor in tasks_by_id and predecessor not in task_end
            ]
            if missing_predecessors:
                unassigned.append(task.task_id)
                candidates = [
                    OptimizationCandidateEvidence(
                        task_id=task.task_id,
                        robot_id=str(raw_robot["robot_id"]),
                        feasible=False,
                        robot_start_node=(
                            int(raw_robot["node_id"])
                            if raw_robot.get("node_id") is not None
                            else None
                        ),
                        rejection_reason="PREDECESSOR_UNASSIGNED",
                    )
                    for raw_robot in raw_robots
                ]
                evidence_order += 1
                evidence_rows.append(
                    TaskOptimizationEvidence(
                        task_id=task.task_id,
                        task_order=evidence_order,
                        priority=task.priority,
                        selection_mode="DETERMINISTIC_GREEDY_INSERTION",
                        tie_break_rule=[],
                        candidate_count=len(candidates),
                        candidates=candidates,
                    )
                )
                continue

            dependency_lags = {
                (
                    str(row.predecessor_work_id),
                    str(row.successor_work_id),
                ): math.ceil(row.lag_seconds / self.time_step_seconds)
                for row in task.dependencies
            }
            predecessor_end = max(
                (
                    task_end.get(predecessor, 0)
                    + dependency_lags.get(
                        (
                            str(
                                tasks_by_id.get(predecessor).work_id
                                if tasks_by_id.get(predecessor)
                                else predecessor.split(":", 1)[0]
                            ),
                            str(task.work_id),
                        ),
                        0,
                    )
                    for predecessor in task.predecessors
                ),
                default=0,
            )
            earliest_step = relative_time_step(
                task.earliest_start,
                reference_time,
                self.time_step_seconds,
                round_up=True,
            )
            latest_step = (
                relative_time_step(
                    task.latest_finish,
                    reference_time,
                    self.time_step_seconds,
                    round_up=False,
                )
                if task.latest_finish is not None
                else None
            )
            preferred_robot = task.assigned_robot_id
            group_robot = (
                same_robot_assignments.get(task.same_robot_group)
                if task.same_robot_group
                else None
            )
            old = existing.get(task.task_id)
            choices: list[tuple[Any, ...]] = []
            candidates: list[OptimizationCandidateEvidence] = []

            for raw_robot in raw_robots:
                robot_id = str(raw_robot["robot_id"])
                unavailable_reason = self._robot_unavailable_reason(
                    raw_robot, self.min_robot_battery
                )
                if unavailable_reason:
                    candidates.append(
                        OptimizationCandidateEvidence(
                            task_id=task.task_id,
                            robot_id=robot_id,
                            feasible=False,
                            robot_start_node=(
                                int(raw_robot["node_id"])
                                if raw_robot.get("node_id") is not None
                                else None
                            ),
                            rejection_reason=unavailable_reason,
                        )
                    )
                    continue

                state = robot_state[robot_id]
                if group_robot and robot_id != group_robot:
                    candidates.append(
                        OptimizationCandidateEvidence(
                            task_id=task.task_id,
                            robot_id=robot_id,
                            feasible=False,
                            robot_start_node=int(state["node_id"]),
                            rejection_reason="SAME_ROBOT_GROUP_MISMATCH",
                        )
                    )
                    continue
                if preferred_robot and robot_id != preferred_robot:
                    candidates.append(
                        OptimizationCandidateEvidence(
                            task_id=task.task_id,
                            robot_id=robot_id,
                            feasible=False,
                            robot_start_node=int(state["node_id"]),
                            # Keep the historical reason code for API/test
                            # compatibility.  The constraint now also applies
                            # to changeable LOCAL_REPLAN tasks without freezing
                            # their previous times.
                            rejection_reason="FROZEN_ASSIGNMENT_MISMATCH",
                        )
                    )
                    continue
                if task.action == "PICK" and task.quantity > (
                    state["max_load"] - state["current_load"]
                ):
                    candidates.append(
                        OptimizationCandidateEvidence(
                            task_id=task.task_id,
                            robot_id=robot_id,
                            feasible=False,
                            robot_start_node=int(state["node_id"]),
                            rejection_reason="LOAD_CAPACITY_EXCEEDED",
                        )
                    )
                    continue
                robot_choices: list[tuple[Any, ...]] = []
                approach_found = False
                operation_found = False
                battery_feasible = False
                hard_window_feasible = False
                for source in sorted(set(task.source_candidates)):
                    approach = self._shortest(graph, state["node_id"], source)
                    if approach is None:
                        continue
                    approach_found = True
                    for target in sorted(set(task.target_candidates or [source])):
                        operation = self._shortest(graph, source, target)
                        if operation is None:
                            continue
                        operation_found = True
                        distance = approach[0] + operation[0]
                        seconds = approach[1] + operation[1]
                        if explicit_charge_spec is not None:
                            service_steps = max(
                                1,
                                math.ceil(
                                    float(
                                        explicit_charge_spec.get(
                                            "charge_duration_seconds", 0
                                        )
                                    )
                                    / self.time_step_seconds
                                ),
                            )
                        else:
                            service_steps = (
                                1 if task.action in {"PICK", "DROP", "CHARGE"} else 0
                            )
                        duration_steps = math.ceil(seconds / self.time_step_seconds) + service_steps
                        start_step = max(
                            state["time_step"], predecessor_end, earliest_step
                        )
                        end_step = start_step + max(0, duration_steps)
                        if (
                            task.time_constraint_type == "HARD_WINDOW"
                            and latest_step is not None
                            and end_step > latest_step
                        ):
                            continue
                        hard_window_feasible = True
                        energy = distance * self.energy_per_distance
                        remaining_route = self._remaining_group_route(
                            graph,
                            ordered_tasks,
                            task_index,
                            current_target=target,
                        )
                        if remaining_route is None:
                            continue
                        downstream_distance, downstream_seconds = remaining_route
                        mission_energy = (
                            distance + downstream_distance
                        ) * self.energy_per_distance
                        charge: dict[str, Any] | None = None
                        if explicit_charge_spec is not None:
                            # This visit was selected before the second optimizer
                            # pass. It may not be replaced by another automatic
                            # charger visit. Safe arrival remains a hard gate.
                            if state["battery"] - energy < self.min_robot_battery:
                                continue
                        elif state["battery"] - mission_energy < self.min_robot_battery:
                            skip_source_after_charge = bool(
                                task.action == "DROP"
                                and task.predecessors
                                and float(state.get("current_load") or 0.0)
                                >= float(task.quantity)
                            )
                            charge = self._charge_option(
                                graph,
                                problem,
                                robot_node=int(state["node_id"]),
                                battery=float(state["battery"]),
                                source=source,
                                target=target,
                                operation=operation,
                                downstream_distance=downstream_distance,
                                downstream_seconds=downstream_seconds,
                                skip_source_after_charge=skip_source_after_charge,
                            )
                            if charge is None:
                                continue
                            source = int(charge["task_source_node"])
                            distance = float(charge["task_distance"])
                            seconds = float(charge["task_seconds"])
                            energy = float(charge["task_energy"])
                            charge_travel_steps = math.ceil(
                                float(charge["to_charger_seconds"])
                                / self.time_step_seconds
                            )
                            duration_steps = (
                                charge_travel_steps
                                + int(charge["charge_steps"])
                                + math.ceil(seconds / self.time_step_seconds)
                                + service_steps
                            )
                            end_step = start_step + max(0, duration_steps)
                        battery_feasible = True
                        tardiness_steps = task_tardiness_steps(
                            deadline=task.deadline,
                            reference_time=reference_time,
                            task_end_time_step=end_step,
                            time_step_seconds=self.time_step_seconds,
                        )
                        activation = 0 if robot_id in active_robot_ids else 1
                        change = 1 if old and old.robot_id != robot_id else 0
                        is_new_mission_group = bool(
                            task.same_robot_group
                            and task.same_robot_group not in same_robot_assignments
                        )
                        parallel_load_penalty = (
                            robot_group_counts.get(robot_id, 0)
                            * parallel_group_penalty
                            if parallel_rebalance and is_new_mission_group
                            else 0.0
                        )
                        score = (
                            distance * float(weights["total_distance"])
                            + end_step * float(weights["makespan"])
                            + tardiness_steps * float(weights["tardiness"])
                            + energy * float(weights["energy"])
                            + activation * float(weights["robot_activation"])
                            + change * float(weights["plan_change"])
                            + parallel_load_penalty
                        )
                        choice = (
                            score,
                            robot_id,
                            source,
                            target,
                            distance,
                            energy,
                            end_step,
                            float(tardiness_steps),
                            charge,
                        )
                        choices.append(choice)
                        robot_choices.append(choice)

                if not robot_choices:
                    if not task.source_candidates:
                        reason = "SOURCE_CANDIDATES_EMPTY"
                    elif not approach_found:
                        reason = "SOURCE_UNREACHABLE"
                    elif not operation_found:
                        reason = "TARGET_UNREACHABLE"
                    elif (
                        task.time_constraint_type == "HARD_WINDOW"
                        and latest_step is not None
                        and not hard_window_feasible
                    ):
                        reason = "HARD_WINDOW_VIOLATION"
                    elif not battery_feasible:
                        reason = "BATTERY_UNREACHABLE"
                    else:
                        reason = "NO_FEASIBLE_SOURCE_TARGET_PAIR"
                    candidates.append(
                        OptimizationCandidateEvidence(
                            task_id=task.task_id,
                            robot_id=robot_id,
                            feasible=False,
                            robot_start_node=int(state["node_id"]),
                            rejection_reason=reason,
                        )
                    )
                    continue

                robot_choices.sort(key=lambda row: (row[0], row[2], row[3]))
                best = robot_choices[0]
                (
                    score,
                    _,
                    source,
                    target,
                    distance,
                    energy,
                    end_step,
                    tardiness,
                    charge,
                ) = best
                start_step = max(
                    state["time_step"], predecessor_end, earliest_step
                )
                activation = 0 if robot_id in active_robot_ids else 1
                change = 1 if old and old.robot_id != robot_id else 0
                components = self._candidate_components(
                    distance=distance,
                    end_step=end_step,
                    tardiness_steps=int(tardiness),
                    energy=energy,
                    activation=activation,
                    change=change,
                    weights=weights,
                )
                if parallel_rebalance and task.same_robot_group and not group_robot:
                    components["parallel_group_load"] = (
                        robot_group_counts.get(robot_id, 0)
                        * parallel_group_penalty
                    )
                candidates.append(
                    OptimizationCandidateEvidence(
                        task_id=task.task_id,
                        robot_id=robot_id,
                        feasible=True,
                        robot_start_node=int(state["node_id"]),
                        source_node=source,
                        target_node=target,
                        distance=round(distance, 6),
                        duration_time_steps=max(0, end_step - start_step),
                        end_time_step=end_step,
                        energy=round(energy, 6),
                        tardiness_time_steps=int(tardiness),
                        activation_indicator=activation,
                        plan_change_indicator=change,
                        robot_activation_cost=round(
                            components["robot_activation"], 6
                        ),
                        plan_change_cost=round(components["plan_change"], 6),
                        incremental_objective=round(score, 6),
                        objective_components={
                            key: round(value, 6)
                            for key, value in components.items()
                        },
                        rejection_reason="HIGHER_OBJECTIVE_OR_TIE_BREAK_KEY",
                    )
                )

            if not choices:
                unassigned.append(task.task_id)
                evidence_order += 1
                evidence_rows.append(
                    TaskOptimizationEvidence(
                        task_id=task.task_id,
                        task_order=evidence_order,
                        priority=task.priority,
                        selection_mode="DETERMINISTIC_GREEDY_INSERTION",
                        tie_break_rule=[
                            "incremental_objective",
                            "robot_id",
                            "source_node",
                            "target_node",
                        ],
                        candidate_count=len(candidates),
                        candidates=candidates,
                    )
                )
                continue
            choices.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
            _, robot_id, source, target, distance, energy, end_step, tardiness, charge = choices[0]
            for candidate in candidates:
                if (
                    candidate.robot_id == robot_id
                    and candidate.source_node == source
                    and candidate.target_node == target
                    and candidate.feasible
                ):
                    candidate.selected = True
                    candidate.rejection_reason = None
                    break
            evidence_order += 1
            evidence_rows.append(
                TaskOptimizationEvidence(
                    task_id=task.task_id,
                    task_order=evidence_order,
                    priority=task.priority,
                    selection_mode="DETERMINISTIC_GREEDY_INSERTION",
                    tie_break_rule=[
                        "incremental_objective",
                        "robot_id",
                        "source_node",
                        "target_node",
                    ],
                    candidate_count=len(candidates),
                    selected_robot_id=robot_id,
                    selected_source_node=source,
                    selected_target_node=target,
                    candidates=candidates,
                )
            )
            state = robot_state[robot_id]
            start_step = max(state["time_step"], predecessor_end, earliest_step)
            if charge is not None:
                charge_task_id = f"{task.task_id}:charge:{charge['charger_node']}"
                for predecessor_task_id in task.predecessors:
                    add_execution_dependency(
                        predecessor_task_id, charge_task_id, "AUTO_CHARGING"
                    )
                add_execution_dependency(
                    charge_task_id, task.task_id, "AUTO_CHARGING"
                )
                charge_travel_steps = math.ceil(
                    float(charge["to_charger_seconds"]) / self.time_step_seconds
                )
                charge_end_step = start_step + charge_travel_steps + int(charge["charge_steps"])
                scheduled.append(
                    ScheduledTask(
                        task_id=charge_task_id,
                        work_id=task.work_id,
                        action="CHARGE",
                        robot_id=robot_id,
                        source_node=int(state["node_id"]),
                        target_node=int(charge["charger_node"]),
                        start_time_step=start_step,
                        end_time_step=charge_end_step,
                        priority=task.priority,
                        estimated_distance=float(charge["to_charger_distance"]),
                        estimated_energy=float(charge["to_charger_distance"]) * self.energy_per_distance,
                        charge_target_battery=float(charge["target_battery"]),
                        charged_percent=float(charge["charged_percent"]),
                        charge_duration_seconds=int(
                            charge["charge_duration_seconds"]
                        ),
                        charger_cost=charge["charger_cost"],
                        charger_selection_policy=str(
                            charge["selection_policy"]
                        ),
                        charger_selection_reason=str(
                            charge["selection_reason"]
                        ),
                        charger_candidates=list(charge["candidates"]),
                        schedule_status="READY",
                    )
                )
                charger_selections.append(
                    {
                        "task_id": charge_task_id,
                        "robot_id": robot_id,
                        "selected_charger_node": int(charge["charger_node"]),
                        "selection_policy": str(charge["selection_policy"]),
                        "selection_reason": str(charge["selection_reason"]),
                        "charger_cost": charge["charger_cost"],
                        "battery_before_travel": round(
                            float(state["battery"]), 6
                        ),
                        "battery_at_charger": round(
                            float(charge["battery_at_charger"]), 6
                        ),
                        "charged_percent": round(
                            float(charge["charged_percent"]), 6
                        ),
                        "target_battery": round(
                            float(charge["target_battery"]), 6
                        ),
                        "projected_final_battery": round(
                            float(charge["projected_final_battery"]), 6
                        ),
                        "charge_duration_seconds": int(
                            charge["charge_duration_seconds"]
                        ),
                        "candidates": list(charge["candidates"]),
                    }
                )
                state["battery"] = float(charge["target_battery"])
                state["node_id"] = int(charge["charger_node"])
                state["time_step"] = charge_end_step
                start_step = charge_end_step
                total_distance += float(charge["to_charger_distance"])
                total_energy += float(charge["to_charger_distance"]) * self.energy_per_distance
            scheduled_task = ScheduledTask(
                task_id=task.task_id,
                work_id=task.work_id,
                action=task.action,
                robot_id=robot_id,
                source_node=source,
                target_node=target,
                start_time_step=start_step,
                end_time_step=end_step,
                priority=task.priority,
                estimated_distance=distance,
                estimated_energy=energy,
                charge_target_battery=(
                    explicit_charge_spec.get("target_battery")
                    if explicit_charge_spec is not None
                    else None
                ),
                charged_percent=(
                    float(explicit_charge_spec.get("charged_percent") or 0.0)
                    if explicit_charge_spec is not None
                    else 0.0
                ),
                charge_duration_seconds=(
                    int(explicit_charge_spec.get("charge_duration_seconds") or 0)
                    if explicit_charge_spec is not None
                    else None
                ),
                charger_cost=(
                    explicit_charge_spec.get("charger_cost")
                    if explicit_charge_spec is not None
                    else None
                ),
                charger_selection_policy=(
                    explicit_charge_spec.get("selection_policy")
                    if explicit_charge_spec is not None
                    else None
                ),
                charger_selection_reason=(
                    explicit_charge_spec.get("selection_reason")
                    if explicit_charge_spec is not None
                    else None
                ),
                charger_candidates=(
                    list(explicit_charge_spec.get("candidates") or [])
                    if explicit_charge_spec is not None
                    else []
                ),
                schedule_status=(
                    "WAITING_FOR_PREDECESSOR"
                    if task.predecessors
                    else "SCHEDULED"
                    if earliest_step > 0
                    else "READY"
                ),
            )
            scheduled.append(scheduled_task)
            task_end[task.task_id] = end_step
            state["node_id"] = target
            state["time_step"] = end_step
            state["battery"] -= energy
            if explicit_charge_spec is not None:
                state["battery"] = min(
                    100.0,
                    state["battery"]
                    + float(explicit_charge_spec.get("charged_percent") or 0.0),
                )
                charger_selections.append(
                    {
                        "task_id": task.task_id,
                        "robot_id": robot_id,
                        "selected_charger_node": int(target),
                        "selection_policy": explicit_charge_spec.get(
                            "selection_policy"
                        ),
                        "selection_reason": explicit_charge_spec.get(
                            "selection_reason"
                        ),
                        "charger_cost": explicit_charge_spec.get("charger_cost"),
                        "charged_percent": float(
                            explicit_charge_spec.get("charged_percent") or 0.0
                        ),
                        "target_battery": explicit_charge_spec.get(
                            "target_battery"
                        ),
                        "charge_duration_seconds": int(
                            explicit_charge_spec.get("charge_duration_seconds") or 0
                        ),
                        "candidates": list(
                            explicit_charge_spec.get("candidates") or []
                        ),
                        "source": "CUOPT_EXPLICIT_CHARGE_VISIT",
                    }
                )
            if task.action == "PICK":
                state["current_load"] += task.quantity
            elif task.action == "DROP":
                state["current_load"] = max(0.0, state["current_load"] - task.quantity)
            active_robot_ids.add(robot_id)
            if task.same_robot_group:
                if task.same_robot_group not in counted_groups:
                    counted_groups.add(task.same_robot_group)
                    robot_group_counts[robot_id] = (
                        robot_group_counts.get(robot_id, 0) + 1
                    )
                same_robot_assignments[task.same_robot_group] = robot_id
            changed_robot_ids.add(robot_id)
            if old and old.robot_id != robot_id:
                plan_changes += 1
            total_distance += distance
            total_energy += energy
            total_tardiness_steps += int(tardiness)

        scheduled.sort(key=lambda row: (row.start_time_step, row.robot_id, row.task_id))
        makespan = max((task.end_time_step for task in scheduled), default=0)
        objective_value = (
            total_distance * float(weights["total_distance"])
            + makespan * float(weights["makespan"])
            + total_tardiness_steps * float(weights["tardiness"])
            + total_energy * float(weights["energy"])
            + len(active_robot_ids) * float(weights["robot_activation"])
            + plan_changes * float(weights["plan_change"])
        )
        breakdown = ObjectiveBreakdown(
            total_distance=round(total_distance, 6),
            makespan_time_steps=makespan,
            tardiness_time_steps=total_tardiness_steps,
            total_energy=round(total_energy, 6),
            active_robot_count=len(active_robot_ids),
            plan_changes=plan_changes,
            distance_component=round(
                total_distance * float(weights["total_distance"]), 6
            ),
            makespan_component=round(
                makespan * float(weights["makespan"]), 6
            ),
            tardiness_component=round(
                total_tardiness_steps * float(weights["tardiness"]), 6
            ),
            energy_component=round(total_energy * float(weights["energy"]), 6),
            robot_activation_component=round(
                len(active_robot_ids) * float(weights["robot_activation"]), 6
            ),
            plan_change_component=round(
                plan_changes * float(weights["plan_change"]), 6
            ),
            total=round(objective_value, 6),
            weights={key: float(value) for key, value in weights.items()},
        )
        self.last_optimization_evidence = evidence_rows
        self.last_objective_breakdown = breakdown
        return CuOptPlan(
            scheduled_tasks=scheduled,
            unassigned_task_ids=sorted(unassigned),
            changed_robot_ids=sorted(changed_robot_ids),
            objective_value=round(objective_value, 6),
            metadata={
                "backend": "local",
                "algorithm": "deterministic_greedy_insertion",
                "total_distance": round(total_distance, 6),
                "makespan_time_steps": makespan,
                "tardiness_time_steps": total_tardiness_steps,
                "energy": round(total_energy, 6),
                "active_robot_count": len(active_robot_ids),
                "plan_changes": plan_changes,
                "preserved_task_ids": sorted(preserved_ids),
                "reference_time": reference_time.isoformat(),
                "time_step_seconds": self.time_step_seconds,
                "same_robot_assignments": same_robot_assignments,
                "charger_selections": charger_selections,
                "execution_task_dependencies": execution_task_dependencies,
                "charge_visit_optimization_contract": problem.get(
                    "charge_visit_optimization_contract", {}
                ),
                "explicit_charge_task_ids": sorted(explicit_charge_specs),
                "parallel_robot_rebalance": {
                    "enabled": parallel_rebalance,
                    "group_penalty": parallel_group_penalty,
                    "group_counts_by_robot": dict(sorted(robot_group_counts.items())),
                },
                "cuopt_assignment_application": problem.get(
                    "cuopt_assignment_application", {}
                ),
                "charging_policy": (
                    "TARGET_BATTERY_WITH_PATH_RESERVE"
                    if "MINIMUM_BATTERY_AT_ALL_TIMES"
                    in set(problem.get("hard_constraints", []))
                    else "TARGET_BATTERY"
                ),
                "minimum_battery_percent": round(self.min_robot_battery, 6),
                "battery_safety_margin_percent": round(
                    float(
                        problem.get("battery_safety_margin_percent")
                        if problem.get("battery_safety_margin_percent") is not None
                        else self.battery_safety_margin_percent
                    ),
                    6,
                ),
                "charge_target_battery_percent": round(
                    float(
                        problem.get("charge_target_battery")
                        or self.charge_target_battery
                    ),
                    6,
                ),
            },
        )

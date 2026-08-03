"""Hybrid live repository backed by PostgreSQL, Redis, and Neo4j.

PostgreSQL is authoritative for orders, racks, handling units, and station
master data.  Redis is authoritative for fast-changing robot/edge/reservation
runtime.  Neo4j is authoritative for the traversable route projection.  The
repository keeps the public read contract used by the LangGraph nodes while
allowing independent domains to be queried concurrently.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from app.core.config import get_settings
from app.domain.schemas import normalize_warehouse_id
from app.infrastructure.manager import get_infrastructure_manager
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository
from app.repositories.json_repository import DataContractError, JsonWarehouseRepository


class LiveWarehouseRepository(JsonWarehouseRepository):
    """Request-scoped facade whose authoritative records come from live stores.

    The class inherits the stable read methods from ``JsonWarehouseRepository``
    but deliberately does not call its file-loading constructor.  Live mode is
    therefore usable without ``data/*.json`` and cannot silently fall back to a
    local fixture when PostgreSQL, Redis, or Neo4j is incomplete.
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        warehouse_id: str | None = None,
        simulation_id: str | None = None,
        simulation_run_id: int | None = None,
    ) -> None:
        cfg = get_settings()
        if data_dir is not None:
            raise DataContractError("LiveWarehouseRepository does not support fixture overrides.")
        self.warehouse_id = normalize_warehouse_id(
            warehouse_id or cfg.default_warehouse_id
        )
        self.simulation_id = str(simulation_id or cfg.runtime_simulation_id)
        self.simulation_run_id = simulation_run_id
        # Keep path attributes only for compatibility with inherited diagnostics;
        # no live code reads these files.
        self.data_root = cfg.data_dir
        self.graph_path = self.data_root / "warehouse_graph.json"
        self.inventory_path = self.data_root / "rack_inventory.json"
        self.scenario_path = self.data_root / "scenario_state.json"
        self.facility_path = self.data_root / "facility_resources.json"
        self.inventory: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "racks": [],
        }
        self.scenario: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "orders": [],
            "inbound_receipts": [],
            "robots": [],
            "edge_runtime": [],
            "edge_reservations": [],
            "spring_tasks": [],
            "buffer_nodes": [],
        }
        self.facility: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "inbound_ports": [],
            "inbound_handoffs": [],
            "outbound_chutes": [],
            "outbound_stations": [],
            "station_robots": [],
            "empty_tote_buffers": [],
        }
        self.graph: dict[str, Any] = {
            "title": "Uninitialized live route projection",
            "summary": {"node_count": 0, "edge_count": 0},
            "nodes": [],
            "edges": [],
        }
        self._graph_version: str | None = None
        self.settings = cfg
        self.infrastructure = get_infrastructure_manager()
        self.infrastructure.start()
        self.spring_runtime = BeSpringRuntimeRepository(
            settings=cfg,
            manager=self.infrastructure,
        )
        self.live_component_status: dict[str, dict[str, Any]] = {}
        self._live_versions: dict[str, str] = {}
        self._load_live_snapshot()

    def _parallel(self, calls: dict[str, Callable[[], Any]]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="laro-live-load") as pool:
            futures = {pool.submit(call): name for name, call in calls.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    values[name] = future.result()
                    self.live_component_status[name] = {"ok": True}
                except Exception as exc:
                    self.live_component_status[name] = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    if self.settings.infrastructure_strict_startup:
                        raise
        return values

    def _load_live_snapshot(self) -> None:
        """Fetch independent data domains concurrently, then rebuild indexes."""

        calls: dict[str, Callable[[], Any]] = {
            "postgres_inventory": lambda: self.infrastructure.postgres.load_inventory_document(self.warehouse_id),
            "postgres_orders": lambda: self.infrastructure.postgres.load_orders(self.warehouse_id),
            "postgres_inbound_receipts": lambda: self.infrastructure.postgres.load_inbound_receipts(self.warehouse_id),
            "postgres_facility": lambda: self.infrastructure.postgres.load_facility_document(self.warehouse_id),
            "postgres_versions": lambda: self.infrastructure.postgres.versions(self.warehouse_id),
            "neo4j_graph": lambda: self.infrastructure.neo4j.fetch_route_graph(self.warehouse_id),
        }
        if self.simulation_run_id is not None:
            calls.update(
                {
                    "spring_runtime": lambda: self.spring_runtime.snapshot(
                        self.simulation_run_id
                    ),
                    "spring_edges": lambda: self.spring_runtime.load_edge_runtime(
                        self.simulation_run_id
                    ),
                    "spring_tasks": lambda: self.infrastructure.postgres.load_spring_tasks(
                        self.simulation_run_id
                    ),
                }
            )
        else:
            calls.update(
                {
                    "redis_robots": lambda: self.infrastructure.redis.all_robots(
                        self.warehouse_id, self.simulation_id
                    ),
                    "redis_edges": lambda: self.infrastructure.redis.edge_runtime(
                        self.warehouse_id, self.simulation_id
                    ),
                    "redis_reservations": lambda: self.infrastructure.redis.existing_reservations(
                        self.warehouse_id, self.simulation_id
                    ),
                    "redis_version": lambda: self.infrastructure.redis.runtime_version(
                        self.warehouse_id, self.simulation_id
                    ),
                }
            )
        values = self._parallel(calls)
        inventory = values.get("postgres_inventory")
        orders = values.get("postgres_orders")
        inbound_receipts = values.get("postgres_inbound_receipts")
        facility = values.get("postgres_facility")
        spring_snapshot = values.get("spring_runtime")
        spring_tasks = values.get("spring_tasks") or []
        if spring_snapshot is not None:
            robots = self._spring_robot_records(
                spring_snapshot.robots,
                spring_tasks,
                run_sim_time_ms=int(spring_snapshot.meta.sim_time_ms or 0),
            )
            edges = self._spring_edge_records(values.get("spring_edges") or [])
            reservations: list[dict[str, Any]] = []
            runtime_version = str(spring_snapshot.meta.runtime_version or 0)
        else:
            robots = values.get("redis_robots")
            edges = values.get("redis_edges")
            reservations = values.get("redis_reservations")
            runtime_version = str(values.get("redis_version", "0"))
        snapshot = values.get("neo4j_graph")

        if inventory and inventory.get("racks"):
            self.inventory = inventory
        else:
            raise DataContractError(
                f"PostgreSQL contains no rack inventory for warehouse {self.warehouse_id}; "
                "run the warehouse-scoped bootstrap script."
            )
        if isinstance(orders, list):
            self.scenario = {**self.scenario, "orders": orders}
        if isinstance(inbound_receipts, list):
            self.scenario = {**self.scenario, "inbound_receipts": inbound_receipts}
        if facility and facility.get("outbound_stations"):
            self.facility = facility
        else:
            raise DataContractError(
                f"PostgreSQL contains no facility resources for warehouse {self.warehouse_id}; "
                "run the warehouse-scoped bootstrap script."
            )
        # Robot runtime belongs to the request/session domain, not the static
        # warehouse contract.  Keep the repository usable when a scenario
        # namespace has not been seeded yet: the robot_runtime graph node will
        # either apply a COMPLETE/OVERLAY request snapshot or return the precise
        # MISSING_SIMULATION_RUNTIME workflow error.  Previously the repository
        # constructor failed before structured IDs or request snapshots could be
        # evaluated.
        self.scenario = {
            **self.scenario,
            "robots": robots if isinstance(robots, list) else [],
        }
        if not self.scenario["robots"]:
            self.live_component_status.setdefault("redis_robots", {})[
                "warning"
            ] = (
                f"No robot runtime exists for {self.warehouse_id}/{self.simulation_id}; "
                "a scenario bootstrap or COMPLETE runtime_snapshot is required."
            )
        if isinstance(edges, list):
            self.scenario = {**self.scenario, "edge_runtime": edges}
        if isinstance(reservations, list):
            self.scenario = {**self.scenario, "edge_reservations": reservations}
        if isinstance(spring_tasks, list):
            self.scenario = {**self.scenario, "spring_tasks": spring_tasks}
        if snapshot is not None and snapshot.nodes:
            self.graph = {
                "title": "Live Neo4j route-only warehouse projection",
                "coordinate_unit": "display_unit",
                "physical_distance_unit": "meters",
                "fallback_meters_per_coordinate_unit": self.settings.map_meters_per_coordinate_unit,
                "default_robot_speed_mps": self.settings.robot_nominal_speed_mps,
                "routing_model": {
                    "rack_entities_in_route_graph": False,
                    "rack_through_travel_allowed": False,
                    "goods_to_person_stations": True,
                    "distributed_inbound_handoffs": True,
                },
                "summary": snapshot.summary,
                "nodes": snapshot.nodes,
                "edges": snapshot.edges,
            }
            self._graph_version = snapshot.version
        else:
            raise DataContractError(
                f"Neo4j contains no route graph for warehouse {self.warehouse_id}; "
                "run the warehouse-scoped bootstrap script."
            )

        versions = values.get("postgres_versions") or {}
        self._live_versions = {
            "graph_version": self._graph_version or "missing-live-graph-version",
            "inventory_version": str(versions.get("inventory_version", "live")),
            "business_version": str(versions.get("business_version", "live")),
            "facility_version": str(versions.get("facility_version", "live")),
            "runtime_version": runtime_version,
        }
        self._rebuild_indexes()

    @staticmethod
    def _spring_robot_records(
        robots: list[Any],
        tasks: list[dict[str, Any]],
        *,
        run_sim_time_ms: int = 0,
    ) -> list[dict[str, Any]]:
        active_statuses = {"ASSIGNED", "IN_PROGRESS"}
        assigned_tasks = {
            str(task["robot_id"]): str(task["task_id"])
            for task in tasks
            if task.get("robot_id") is not None
            and str(task.get("status", "")).upper() in active_statuses
        }
        status_map = {
            "AVAILABLE": "idle",
            "IDLE": "idle",
            "WAITING": "waiting",
            "MOVING": "moving",
            "WORKING": "working",
            "BUSY": "working",
            "CHARGING": "charging",
            "FAULT": "fault",
            "FAILED": "fault",
            "OFFLINE": "offline",
        }
        result: list[dict[str, Any]] = []
        for robot in robots:
            robot_id = str(robot.robot_id)
            sim_time_ms = max(
                int(run_sim_time_ms),
                int(robot.sim_time_ms or 0),
            )
            available_at_ms = max(
                sim_time_ms,
                int(robot.step_end_at_ms or 0),
            )
            current_node = robot.current_node_code or (
                str(robot.current_node_id)
                if robot.current_node_id is not None
                else None
            )
            result.append(
                {
                    "robot_id": robot_id,
                    "robot_code": robot_id,
                    "status": status_map.get(
                        str(robot.status).upper(), str(robot.status).lower()
                    ),
                    "battery_pct": float(
                        robot.battery_level
                        if robot.battery_level is not None
                        else 100.0
                    ),
                    "capacity_units": int(robot.capacity_units or 1),
                    "current_node": current_node,
                    "current_edge": robot.current_edge_code,
                    "active_task_id": (
                        str(robot.current_task_id)
                        if robot.current_task_id is not None
                        else assigned_tasks.get(robot_id)
                    ),
                    "load_state": (
                        "LOADED"
                        if (robot.current_load_units or 0) > 0
                        or robot.handling_unit_code
                        else "EMPTY"
                    ),
                    "current_load_units": int(robot.current_load_units or 0),
                    "sim_time_ms": sim_time_ms,
                    "available_at_ms": available_at_ms,
                }
            )
        return result

    @staticmethod
    def _spring_edge_records(edges: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "edge_id": str(edge.edge_code or edge.edge_id),
                "status": str(edge.status).lower(),
                "cost_multiplier": float(edge.cost_multiplier),
                "travel_time_multiplier": float(edge.travel_time_multiplier),
                "occupied_by_robot_id": (
                    str(edge.occupied_by_robot_id)
                    if edge.occupied_by_robot_id is not None
                    else None
                ),
                "blocked_until_ms": edge.blocked_until_ms,
            }
            for edge in edges
        ]

    @property
    def versions(self) -> dict[str, str]:
        values = dict(self._live_versions)
        if self.simulation_run_id is not None:
            return values
        try:
            values["runtime_version"] = self.infrastructure.redis.runtime_version(self.warehouse_id, self.simulation_id)
        except Exception:
            pass
        return values

    @property
    def source_manifest(self) -> dict[str, str]:
        """Expose the live authority used by each plan data domain."""

        runtime_source = (
            "spring_redis"
            if self.simulation_run_id is not None
            else "redis_live"
        )
        task_source = (
            "public.task"
            if self.simulation_run_id is not None
            else "postgres_live"
        )
        return {
            "route_nodes": "neo4j_snapshot",
            "route_edges": "neo4j_snapshot",
            "racks": "postgres_snapshot",
            "handling_units": "postgres_live",
            "orders": "postgres_live",
            "tasks": task_source,
            "inbound_receipts": "postgres_live",
            "facilities": "postgres_snapshot",
            "robots": runtime_source,
            "edge_runtime": runtime_source,
            "reservations": (
                "not_available"
                if self.simulation_run_id is not None
                else "redis_live"
            ),
        }

    def buffer_nodes(self) -> list[str]:
        """Return live recovery buffers without consulting local JSON files.

        The current native contract has no dedicated recovery-buffer table.  An
        empty list is therefore explicit and fail-closed; a future facility table
        can populate this method without reintroducing fixture fallback.
        """

        return []

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        try:
            return self.infrastructure.postgres.get_order(self.warehouse_id, order_id)
        except Exception as exc:
            raise DataContractError(
                f"Live order lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def find_orders(
        self,
        *,
        order_ids: list[str] | None = None,
        item_ids: list[str] | None = None,
        item_text: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # Code-first execution never resolves an order by item-name text.  The
        # optional text remains available only through the JSON compatibility
        # path used by legacy evaluation scenarios.
        if item_text:
            raise DataContractError(
                "Live code-first order lookup does not accept item-name text; use canonical order IDs."
            )
        try:
            return self.infrastructure.postgres.find_orders(
                self.warehouse_id,
                order_ids=order_ids,
                item_ids=item_ids,
                statuses=statuses,
            )
        except Exception as exc:
            raise DataContractError(
                f"Live order search failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def get_inbound_receipt(self, inbound_id: str) -> dict[str, Any] | None:
        try:
            return self.infrastructure.postgres.get_inbound_receipt(self.warehouse_id, inbound_id)
        except Exception as exc:
            raise DataContractError(
                f"Live get_inbound_receipt lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def all_inbound_receipts(self) -> list[dict[str, Any]]:
        try:
            return self.infrastructure.postgres.load_inbound_receipts(self.warehouse_id)
        except Exception as exc:
            raise DataContractError(
                f"Live all_inbound_receipts lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def all_robots(self) -> list[dict[str, Any]]:
        if self.simulation_run_id is not None:
            return [dict(value) for value in self.scenario.get("robots", [])]
        try:
            return self.infrastructure.redis.all_robots(self.warehouse_id, self.simulation_id)
        except Exception as exc:
            raise DataContractError(
                f"Live all_robots lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def find_robots(self, references: list[str]) -> list[dict[str, Any]]:
        normalized = {self.normalize_search_text(value) for value in references if value}
        robots = self.all_robots()
        if not normalized:
            return robots
        return sorted(
            [
                value
                for value in robots
                if {
                    self.normalize_search_text(str(value.get("robot_id", ""))),
                    self.normalize_search_text(str(value.get("robot_code", ""))),
                }
                & normalized
            ],
            key=lambda value: str(value["robot_id"]),
        )

    def handling_units(self, item_id: str | None = None) -> list[dict[str, Any]]:
        try:
            return self.infrastructure.postgres.handling_units(self.warehouse_id, item_id)
        except Exception as exc:
            raise DataContractError(
                f"Live handling_units lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def item_stocks(self, item_id: str) -> list[dict[str, Any]]:
        try:
            return self.infrastructure.postgres.item_stocks(self.warehouse_id, item_id)
        except Exception as exc:
            raise DataContractError(
                f"Live item_stocks lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def runtime_edge_records(self) -> list[dict[str, Any]]:
        if self.simulation_run_id is not None:
            return [
                dict(value) for value in self.scenario.get("edge_runtime", [])
            ]
        try:
            return self.infrastructure.redis.edge_runtime(self.warehouse_id, self.simulation_id)
        except Exception as exc:
            raise DataContractError(
                f"Live runtime_edge_records lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def existing_reservations(self) -> list[dict[str, Any]]:
        if self.simulation_run_id is not None:
            return []
        try:
            return self.infrastructure.redis.existing_reservations(self.warehouse_id, self.simulation_id)
        except Exception as exc:
            raise DataContractError(
                f"Live existing_reservations lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

    def active_operations(self) -> list[dict[str, Any]]:
        """Return Spring-owned active tasks for the selected simulation run."""

        active = {"PENDING", "ASSIGNED", "IN_PROGRESS"}
        return [
            dict(value)
            for value in self.scenario.get("spring_tasks", [])
            if str(value.get("status", "")).upper() in active
        ]

    def station_runtime(self, simulation_id: str | None = None) -> list[dict[str, Any]]:
        try:
            return self.infrastructure.redis.station_runtime(self.warehouse_id, simulation_id or self.simulation_id)
        except Exception as exc:
            raise DataContractError(
                f"Live station_runtime lookup failed for warehouse {self.warehouse_id}: {exc}"
            ) from exc

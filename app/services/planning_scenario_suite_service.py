"""Materialize the operational scenario pack into frozen captures and run it."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.planning_evaluation import PlanningScenarioSuiteRequest
from app.domain.schemas import (
    AutoMissionRequest,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    RoutingWorkloadContext,
    StructuredMissionInput,
    StructuredOperationInput,
)
from app.repositories.json_repository import JsonWarehouseRepository
from app.services.planning_evaluation_job_service import (
    PlanningEvaluationJobService,
    get_planning_evaluation_job_service,
)
from app.services.planning_evaluation_service import PlanningEvaluationStore
from app.services.planning_scenario_catalog import generated_scenario_definitions
from app.services.planning_dynamic_scenario_validator import (
    validate_dynamic_definition,
)
from app.services.request_gate_service import code_input_rejection


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = (
    ROOT / "scenarios" / "fixtures" / "V13_mixed_inbound_outbound_multirobot"
)
_SUITE_ID_PREFIX = "ESUITE-"
_TERMINAL_JOB_STATUSES = {"SUCCEEDED", "FAILED"}
_SUITE_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _content_version(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class PlanningScenarioMaterializer:
    """Turn declarative workload conditions into hermetic planning captures."""

    def __init__(
        self,
        *,
        store: PlanningEvaluationStore | None = None,
        fixture_dir: Path = DEFAULT_FIXTURE_DIR,
    ) -> None:
        self.store = store or PlanningEvaluationStore()
        self.fixture_dir = Path(fixture_dir)

    def definitions(
        self,
        scenario_ids: list[str] | None = None,
        scenario_groups: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        requested = set(scenario_ids or [])
        requested_groups = set(scenario_groups or [])
        generated = generated_scenario_definitions()
        generated_by_id = {str(value["scenario_id"]): value for value in generated}
        known = set(generated_by_id)
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(f"Unknown planning scenario IDs: {', '.join(unknown)}")
        values: list[dict[str, Any]] = []
        for definition in generated:
            scenario_id = str(definition["scenario_id"])
            if requested and scenario_id not in requested:
                continue
            if requested_groups and definition["scenario_group"] not in requested_groups:
                continue
            self._validate_definition(definition)
            values.append(copy.deepcopy(definition))
        if not values:
            raise ValueError("No planning scenarios were selected")
        return values

    @staticmethod
    def _validate_definition(definition: dict[str, Any]) -> None:
        scenario_id = str(definition.get("scenario_id") or "<unknown>")
        workload = dict(definition.get("workload") or {})
        mix = dict(workload.get("operation_mix") or {})
        operation_count = int(workload.get("operation_count") or 0)
        outbound_count = int(mix.get("OUTBOUND") or 0)
        inbound_count = int(mix.get("INBOUND") or 0)
        if operation_count < 1 or outbound_count + inbound_count != operation_count:
            raise ValueError(f"{scenario_id}: invalid operation count/mix")
        if workload.get("operation_unit") != "ONE_PHYSICAL_BOX_CYCLE":
            raise ValueError(f"{scenario_id}: unsupported operation unit")
        robots = dict(definition.get("robots") or {})
        total = int(robots.get("total_robot_count") or 0)
        eligible = int(robots.get("eligible_robot_count") or 0)
        low_battery = int(robots.get("low_battery_robot_count") or 0)
        if total < 1 or eligible < 1 or total - low_battery != eligible:
            raise ValueError(f"{scenario_id}: inconsistent robot eligibility counts")
        minimum_battery = float(robots.get("minimum_battery_pct") or 0)
        battery_percentages = robots.get("battery_percentages")
        if battery_percentages is not None:
            if not isinstance(battery_percentages, list) or len(battery_percentages) != total:
                raise ValueError(
                    f"{scenario_id}: battery_percentages must contain one value per robot"
                )
            values = [float(value) for value in battery_percentages]
            if any(value < 0 or value > 100 for value in values):
                raise ValueError(f"{scenario_id}: battery percentage must be within 0..100")
            actual_low = sum(value < minimum_battery for value in values)
            actual_eligible = sum(value >= minimum_battery for value in values)
            if actual_low != low_battery or actual_eligible != eligible:
                raise ValueError(
                    f"{scenario_id}: battery_percentages do not match eligibility counts"
                )
        inventory_layout = str(
            workload.get("inventory_layout") or "DISTRIBUTED_RACKS"
        )
        if inventory_layout not in {
            "DISTRIBUTED_RACKS",
            "CONCENTRATED_RACK_LEVELS",
        }:
            raise ValueError(f"{scenario_id}: unsupported inventory layout")
        if int(workload.get("minimum_distinct_source_node_count") or 0) > outbound_count:
            raise ValueError(f"{scenario_id}: source-node requirement exceeds outbound boxes")
        distinct_items = int(workload.get("minimum_distinct_item_count") or 0)
        if distinct_items < 0 or (outbound_count == 0 and distinct_items != 0):
            raise ValueError(
                f"{scenario_id}: pure inbound workload must require zero outbound items"
            )
        group = str(definition.get("scenario_group") or "INITIAL")
        if group not in {"INITIAL", "REPLAN", "HUMAN_REVIEW"}:
            raise ValueError(f"{scenario_id}: unsupported scenario group {group}")
        dynamic = dict(definition.get("dynamic_contract") or {})
        if group == "REPLAN":
            required = {
                "reason",
                "checkpoint",
                "expected_handover_policy",
                "requires_active_plan",
                "requires_plan_version",
                "assertions",
            }
            missing = sorted(required - set(dynamic))
            if missing:
                raise ValueError(
                    f"{scenario_id}: incomplete replan contract: {', '.join(missing)}"
                )
        if group == "HUMAN_REVIEW":
            required = {
                "user_command",
                "expected_reason_code",
                "expected_action",
                "requires_nonempty_prompt",
                "requires_options",
            }
            missing = sorted(required - set(dynamic))
            if missing:
                raise ValueError(
                    f"{scenario_id}: incomplete Human Review contract: {', '.join(missing)}"
                )

    def _base_documents(self) -> dict[str, dict[str, Any]]:
        return {
            name: _read_json(self.fixture_dir / name)
            for name in (
                "warehouse_graph.json",
                "rack_inventory.json",
                "scenario_state.json",
                "facility_resources.json",
            )
        }

    @staticmethod
    def _materialize_inventory(
        definition: dict[str, Any], inventory: dict[str, Any]
    ) -> list[dict[str, Any]]:
        workload = dict(definition["workload"])
        outbound_count = int(workload["operation_mix"]["OUTBOUND"])
        distinct_items = int(workload["minimum_distinct_item_count"])
        same_item = bool(workload.get("same_item_only"))

        racks = list(inventory.get("racks", []))
        for rack in racks:
            for level in rack.get("levels", []):
                level["status"] = "EMPTY"
                level["item"] = None

        slots: list[tuple[dict[str, Any], dict[str, Any]]] = []
        inventory_layout = str(
            workload.get("inventory_layout") or "DISTRIBUTED_RACKS"
        )
        if inventory_layout == "CONCENTRATED_RACK_LEVELS":
            # Fill every level of one rack before moving to the next rack. This
            # creates an explicit paired control for source concentration.
            for rack in racks:
                for level in rack.get("levels", []):
                    slots.append((rack, level))
        else:
            # Spread the first BOX over distinct racks before using another
            # level in a previously used rack.
            maximum_level_count = max(
                (len(rack.get("levels", [])) for rack in racks), default=0
            )
            for level_index in range(maximum_level_count):
                for rack in racks:
                    levels = rack.get("levels", [])
                    if level_index < len(levels):
                        slots.append((rack, levels[level_index]))
        if outbound_count > len(slots):
            raise ValueError("Base fixture has too few rack levels for the workload")

        stock: list[dict[str, Any]] = []
        for index, (rack, level) in enumerate(slots[:outbound_count], start=1):
            item_number = 1 if same_item else ((index - 1) % distinct_items) + 1
            item_id = f"EVAL-ITEM-{item_number:03d}"
            handling_unit_id = f"HU-{definition['scenario_id']}-{index:03d}"
            item = {
                "item_id": item_id,
                "item_name": f"Evaluation item {item_number}",
                "category": "EVALUATION",
                "quantity": 1,
                "capacity": 1,
                "unit": "BOX",
                "handling_unit_id": handling_unit_id,
                "handling_unit_status": "stored",
                "home_rack_id": str(rack["rack_id"]),
                "home_rack_level": int(level["level"]),
            }
            level["status"] = "FULL"
            level["item"] = item
            stock.append(
                {
                    **item,
                    "rack_id": str(rack["rack_id"]),
                    "rack_level": int(level["level"]),
                    "access_node_ids": [str(value) for value in rack["access_node_ids"]],
                }
            )

        rack_count = len(inventory.get("racks", []))
        level_count = sum(
            len(value.get("levels", [])) for value in inventory.get("racks", [])
        )
        inventory["summary"] = {
            "rack_count": rack_count,
            "level_count": level_count,
            "occupied_level_count": len(stock),
            "empty_level_count": level_count - len(stock),
            "partial_level_count": 0,
            "full_level_count": len(stock),
            "handling_unit_count": len(stock),
        }
        return stock

    @staticmethod
    def _materialize_robots(
        definition: dict[str, Any], scenario: dict[str, Any]
    ) -> list[dict[str, Any]]:
        robot_contract = dict(definition["robots"])
        total = int(robot_contract["total_robot_count"])
        base_robots = list(scenario.get("robots", []))
        if len(base_robots) < total:
            raise ValueError(f"Base fixture has {len(base_robots)} robots, requires {total}")
        robots: list[dict[str, Any]] = []
        battery_percentages = robot_contract.get("battery_percentages")
        for index, source in enumerate(base_robots[:total]):
            robot = copy.deepcopy(source)
            robot.update(
                {
                    "status": "idle",
                    "battery_pct": (
                        float(battery_percentages[index])
                        if isinstance(battery_percentages, list)
                        else 90.0 - index
                    ),
                    "capacity_units": max(1, int(robot.get("capacity_units") or 1)),
                    "current_edge": None,
                    "active_task_id": None,
                    "active_mission_id": None,
                    "current_load_units": 0,
                    "load_state": "EMPTY",
                }
            )
            robots.append(robot)
        if not isinstance(battery_percentages, list):
            low_count = int(robot_contract.get("low_battery_robot_count") or 0)
            low_pct = float(robot_contract.get("low_battery_pct") or 18)
            for robot in robots[-low_count:] if low_count else []:
                robot["battery_pct"] = low_pct
        scenario["robots"] = robots
        scenario["edge_runtime"] = []
        scenario["edge_reservations"] = []
        scenario["orders"] = []
        scenario["inbound_receipts"] = []
        scenario["mixed_operations"] = []
        scenario["events"] = []
        return robots

    @staticmethod
    def _operations(
        definition: dict[str, Any],
        stock: list[dict[str, Any]],
        facility: dict[str, Any],
    ) -> list[StructuredOperationInput]:
        mix = definition["workload"]["operation_mix"]
        outbound_count = int(mix["OUTBOUND"])
        inbound_count = int(mix["INBOUND"])
        chute_ids = [str(value["chute_id"]) for value in facility["outbound_chutes"]]
        port_ids = [str(value["port_id"]) for value in facility["inbound_ports"]]
        if outbound_count and not chute_ids:
            raise ValueError("Base fixture has no outbound chutes")
        if inbound_count and not port_ids:
            raise ValueError("Base fixture has no inbound ports")

        # Evaluation work must use the same canonical operation identities as
        # production traffic.  Scenario-local labels such as ``PC01-OUT-001``
        # are useful to humans, but the executable request gate intentionally
        # accepts only ``ORD-###`` and ``IN-###`` identifiers.
        scenario_match = re.fullmatch(
            r"(PC|RP|HR)(\d+)(?:_[A-Z0-9]+)+",
            str(definition["scenario_id"]),
        )
        if scenario_match is None:
            raise ValueError(
                f"Unsupported planning scenario ID: {definition['scenario_id']}"
            )
        prefix_offsets = {"PC": 0, "RP": 100, "HR": 200}
        scenario_number = prefix_offsets[scenario_match.group(1)] + int(
            scenario_match.group(2)
        )
        operation_number_base = scenario_number * 1000
        values: list[StructuredOperationInput] = []
        for index, item in enumerate(stock, start=1):
            values.append(
                StructuredOperationInput(
                    operation_id=f"ORD-{operation_number_base + index}",
                    operation_type="OUTBOUND",
                    product_code=str(item["item_id"]),
                    quantity=1,
                    priority=("high" if index % 4 == 0 else "medium"),
                    source_node_code=str(item["access_node_ids"][0]),
                    destination_node_code=chute_ids[(index - 1) % len(chute_ids)],
                    attributes=json.dumps(
                        {
                            "box_count": 1,
                            "evaluation_handling_unit_id": item["handling_unit_id"],
                            "source_rack_id": item["rack_id"],
                            "source_rack_level": item["rack_level"],
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        distinct_items = max(1, int(definition["workload"]["minimum_distinct_item_count"]))
        for index in range(1, inbound_count + 1):
            item_number = (outbound_count + index - 1) % distinct_items + 1
            values.append(
                StructuredOperationInput(
                    # The physical compiler derives task keys from the numeric
                    # operation suffix, so inbound and outbound IDs also need
                    # disjoint number ranges inside one mixed request.
                    operation_id=f"IN-{operation_number_base + 500 + index}",
                    operation_type="INBOUND",
                    product_code=f"EVAL-ITEM-{item_number:03d}",
                    quantity=1,
                    priority=("high" if index == 1 else "medium"),
                    source_facility_code=port_ids[(index - 1) % len(port_ids)],
                    attributes=json.dumps(
                        {"box_count": 1, "evaluation_inbound_box": index},
                        ensure_ascii=False,
                    ),
                )
            )
        return values

    @staticmethod
    def _build_requests(
        definition: dict[str, Any],
        operations: list[StructuredOperationInput],
    ) -> tuple[AutoMissionRequest, NormalizedWarehouseRequest]:
        robot_contract = definition["robots"]
        structured = StructuredMissionInput(
            request_id=f"REQ-{definition['scenario_id']}",
            operations=operations,
            constraints=NormalizedRequestConstraints(
                objective_profile="BALANCED",
                objective_profile_explicit=True,
            ),
            routing_context=RoutingWorkloadContext(
                new_operation_count=len(operations),
                unfinished_operation_count=0,
                eligible_robot_count=int(robot_contract["eligible_robot_count"]),
                total_robot_count=int(robot_contract["total_robot_count"]),
                low_battery_robot_count=int(robot_contract["low_battery_robot_count"]),
                active_robot_count=0,
                source="PLANNING_SCENARIO_MATERIALIZER",
            ),
        )
        normalized = NormalizedWarehouseRequest(
            source="structured_events",
            operations=[
                NormalizedOperation(
                    operation_id=value.operation_id,
                    operation_type=(
                        "OUTBOUND_ORDER"
                        if value.operation_type == "OUTBOUND"
                        else "INBOUND_ITEM"
                    ),
                    source_event_type=(
                        "new_order"
                        if value.operation_type == "OUTBOUND"
                        else "inbound_item_arrived"
                    ),
                    raw_reference=value.product_code,
                    attributes=value.attributes,
                )
                for value in operations
            ],
            constraints=structured.constraints or NormalizedRequestConstraints(),
            normalization_summary=(
                f"Materialized {len(operations)} one-physical-box operations for "
                f"{definition['scenario_id']}."
            ),
        )
        internal = AutoMissionRequest(
            warehouse_id="WH-001",
            simulation_id=f"SIM-{definition['scenario_id']}",
            request_mode="event_driven",
            events=structured.to_events(),
            structured_input=structured,
            normalized_request_override=normalized,
            evaluation_shadow_mode=True,
        )
        dynamic = dict(definition.get("dynamic_contract") or {})
        if definition.get("scenario_group") == "HUMAN_REVIEW":
            command = str(dynamic.get("user_command") or "").strip()
            normalized = normalized.model_copy(update={"raw_user_command": command})
            internal = internal.model_copy(
                update={
                    "request_mode": "mixed",
                    "user_command": command,
                    "normalized_request_override": normalized,
                }
            )
        return internal, normalized

    @staticmethod
    def _validate_materialization(
        definition: dict[str, Any],
        *,
        repository: JsonWarehouseRepository,
        operations: list[StructuredOperationInput],
        normalized_request: NormalizedWarehouseRequest,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, actual: object, expected: object) -> None:
            checks.append(
                {"name": name, "passed": bool(passed), "actual": actual, "expected": expected}
            )

        workload = definition["workload"]
        robot_contract = definition["robots"]
        stock = repository.handling_units()
        robots = repository.all_robots()
        minimum_battery = float(robot_contract["minimum_battery_pct"])
        eligible = [
            value
            for value in robots
            if str(value.get("status")) == "idle"
            and float(value.get("battery_pct") or 0) >= minimum_battery
            and not value.get("active_task_id")
            and int(value.get("current_load_units") or 0) == 0
        ]
        low_battery = [
            value
            for value in robots
            if float(value.get("battery_pct") or 0) < minimum_battery
        ]
        mix = Counter(value.operation_type for value in operations)
        outbound_items = [
            str(value.product_code)
            for value in operations
            if value.operation_type == "OUTBOUND"
        ]
        inventory_by_item = Counter(
            {
                item_id: sum(
                    int(value.get("quantity") or 0)
                    for value in stock
                    if str(value.get("item_id")) == item_id
                )
                for item_id in {str(value.get("item_id")) for value in stock}
            }
        )
        required_by_item = Counter(outbound_items)
        source_racks = {str(value.get("rack_id")) for value in stock}
        handling_units = {str(value.get("handling_unit_id")) for value in stock}
        input_rejection = code_input_rejection(normalized_request)

        check(
            "runtime_input_contract",
            input_rejection is None,
            None if input_rejection is None else input_rejection.model_dump(mode="json"),
            "accepted",
        )
        check("operation_count", len(operations) == int(workload["operation_count"]), len(operations), workload["operation_count"])
        check("outbound_count", mix["OUTBOUND"] == int(workload["operation_mix"]["OUTBOUND"]), mix["OUTBOUND"], workload["operation_mix"]["OUTBOUND"])
        check("inbound_count", mix["INBOUND"] == int(workload["operation_mix"]["INBOUND"]), mix["INBOUND"], workload["operation_mix"]["INBOUND"])
        check("distinct_items", len(set(outbound_items)) >= int(workload["minimum_distinct_item_count"]), len(set(outbound_items)), f">={workload['minimum_distinct_item_count']}")
        check("distinct_source_racks", len(source_racks) >= int(workload["minimum_distinct_source_node_count"]), len(source_racks), f">={workload['minimum_distinct_source_node_count']}")
        check("physical_box_identity", len(handling_units) == mix["OUTBOUND"], len(handling_units), mix["OUTBOUND"])
        check("inventory_sufficiency", all(inventory_by_item[key] >= quantity for key, quantity in required_by_item.items()), dict(inventory_by_item), dict(required_by_item))
        check("total_robot_count", len(robots) == int(robot_contract["total_robot_count"]), len(robots), robot_contract["total_robot_count"])
        check("eligible_robot_count", len(eligible) == int(robot_contract["eligible_robot_count"]), len(eligible), robot_contract["eligible_robot_count"])
        check("low_battery_robot_count", len(low_battery) == int(robot_contract["low_battery_robot_count"]), len(low_battery), robot_contract["low_battery_robot_count"])
        threshold_count = sum(
            float(value.get("battery_pct") or 0) == minimum_battery
            for value in robots
        )
        check(
            "threshold_battery_robot_count",
            threshold_count
            == int(robot_contract.get("threshold_battery_robot_count") or 0),
            threshold_count,
            robot_contract.get("threshold_battery_robot_count") or 0,
        )
        check("same_item_only", not workload.get("same_item_only") or len(set(outbound_items)) == 1, len(set(outbound_items)), 1)
        check("outbound_facilities", not mix["OUTBOUND"] or bool(repository.outbound_chutes), len(repository.outbound_chutes), ">=1")
        check("inbound_facilities", not mix["INBOUND"] or bool(repository.inbound_ports), len(repository.inbound_ports), ">=1")
        failures = [value for value in checks if not value["passed"]]
        return {
            "passed": not failures,
            "scenario_id": definition["scenario_id"],
            "checks": checks,
            "failure_count": len(failures),
            "failed_checks": [str(value["name"]) for value in failures],
            "snapshot": {
                "scenario_group": definition.get("scenario_group", "INITIAL"),
                "operation_count": len(operations),
                "operation_mix": dict(sorted(mix.items())),
                "inventory_box_count": len(stock),
                "operation_ids": [value.operation_id for value in operations],
                "handling_unit_ids": sorted(handling_units),
                "source_rack_ids": sorted(source_racks),
                "inventory_states": sorted(
                    (
                        {
                            "handling_unit_id": value.get("handling_unit_id"),
                            "item_id": value.get("item_id"),
                            "quantity": value.get("quantity"),
                            "rack_id": value.get("rack_id"),
                            "rack_level": value.get("rack_level"),
                            "access_node_ids": value.get("access_node_ids"),
                        }
                        for value in stock
                    ),
                    key=lambda value: str(value.get("handling_unit_id")),
                ),
                "eligible_robot_count": len(eligible),
                "low_battery_robot_count": len(low_battery),
                "robot_states": [
                    {
                        "robot_id": value.get("robot_id"),
                        "current_node": value.get("current_node"),
                        "status": value.get("status"),
                        "battery_pct": value.get("battery_pct"),
                        "capacity_units": value.get("capacity_units"),
                    }
                    for value in sorted(
                        robots, key=lambda item: str(item.get("robot_id"))
                    )
                ],
                "facility_states": {
                    "inbound_port_ids": sorted(repository.inbound_ports),
                    "outbound_chute_ids": sorted(repository.outbound_chutes),
                    "charging_slot_ids": sorted(
                        node_id
                        for node_id, node in repository.nodes.items()
                        if str(node.get("type") or "").lower()
                        == "charging_slot"
                    ),
                },
                "repository_versions": dict(sorted(repository.versions.items())),
            },
        }

    @staticmethod
    def _repository_documents(
        repository: JsonWarehouseRepository,
    ) -> dict[str, dict[str, Any]]:
        """Return exactly the four frozen documents consumed by a replay."""

        return {
            "warehouse_graph.json": dict(repository.graph),
            "rack_inventory.json": dict(repository.inventory),
            "scenario_state.json": dict(repository.scenario),
            "facility_resources.json": dict(repository.facility),
        }

    @staticmethod
    def _input_fingerprint(
        documents: dict[str, dict[str, Any]],
        operations: list[StructuredOperationInput],
    ) -> str:
        """Hash graph, stock, robot runtime, facilities, and requested work."""

        return _content_version(
            {
                "documents": documents,
                "operations": [
                    value.model_dump(mode="json") for value in operations
                ],
            }
        )

    @staticmethod
    def _append_fingerprint_check(
        report: dict[str, Any],
        *,
        name: str,
        expected: str,
        actual: str,
    ) -> None:
        passed = expected == actual
        report["checks"].append(
            {
                "name": name,
                "passed": passed,
                "actual": actual,
                "expected": expected,
            }
        )
        report["input_fingerprint"] = actual
        if not passed:
            report["passed"] = False
            report["failure_count"] = int(report["failure_count"]) + 1
            report["failed_checks"].append(name)

    def materialize(self, definition: dict[str, Any], *, suite_id: str) -> dict[str, Any]:
        scenario_id = str(definition["scenario_id"])
        documents = copy.deepcopy(self._base_documents())
        inventory = documents["rack_inventory.json"]
        scenario = documents["scenario_state.json"]
        facility = documents["facility_resources.json"]
        stock = self._materialize_inventory(definition, inventory)
        robots = self._materialize_robots(definition, scenario)
        operations = self._operations(definition, stock, facility)
        internal, normalized = self._build_requests(definition, operations)

        scenario["simulation_id"] = internal.simulation_id
        scenario["events"] = [value.model_dump(mode="json") for value in internal.events]
        scenario["evaluation_scenario_id"] = scenario_id
        scenario["evaluation_robot_count"] = len(robots)
        for document in documents.values():
            document["warehouse_id"] = internal.warehouse_id
        source_input_fingerprint = self._input_fingerprint(documents, operations)

        evaluation_id = f"EVAL-{scenario_id}-{uuid4().hex[:12].upper()}"
        root = self.store.capture_dir(evaluation_id)
        frozen = root / "frozen_repository"
        if root.exists():
            raise FileExistsError(f"Capture already exists: {evaluation_id}")
        frozen.mkdir(parents=True, exist_ok=False)
        for name, document in documents.items():
            _write_json(frozen / name, document)

        try:
            repository = JsonWarehouseRepository(
                frozen,
                warehouse_id=internal.warehouse_id,
                simulation_id=internal.simulation_id,
            )
            preflight = self._validate_materialization(
                definition,
                repository=repository,
                operations=operations,
                normalized_request=normalized,
            )
            self._append_fingerprint_check(
                preflight,
                name="source_to_first_load_fingerprint",
                expected=source_input_fingerprint,
                actual=self._input_fingerprint(
                    self._repository_documents(repository), operations
                ),
            )
            if not preflight["passed"]:
                raise ValueError(
                    f"{scenario_id} materialization failed: "
                    + ", ".join(preflight["failed_checks"])
                )

            versions = dict(repository.versions)
            _write_json(root / "raw_request.json", {"scenario_definition": definition})
            _write_json(root / "internal_request.json", internal.model_dump(mode="json"))
            _write_json(root / "normalized_request.json", normalized.model_dump(mode="json"))
            _write_json(
                root / "context_snapshot.json",
                {
                    "source": "PLANNING_SCENARIO_MATERIALIZER",
                    "scenario_id": scenario_id,
                    "repository_versions": versions,
                    "frozen_state": preflight["snapshot"],
                },
            )
            _write_json(root / "materialization_report.json", preflight)

            # Re-open the files that the comparison will consume. This catches
            # serialization/path mistakes rather than validating only memory.
            post_repository = JsonWarehouseRepository(
                frozen,
                warehouse_id=internal.warehouse_id,
                simulation_id=internal.simulation_id,
            )
            postflight = self._validate_materialization(
                definition,
                repository=post_repository,
                operations=operations,
                normalized_request=normalized,
            )
            self._append_fingerprint_check(
                postflight,
                name="source_to_reopened_capture_fingerprint",
                expected=source_input_fingerprint,
                actual=self._input_fingerprint(
                    self._repository_documents(post_repository), operations
                ),
            )
            if not postflight["passed"]:
                raise ValueError(
                    f"{scenario_id} post-write validation failed: "
                    + ", ".join(postflight["failed_checks"])
                )
            _write_json(root / "post_materialization_report.json", postflight)
            dynamic_report: dict[str, Any] | None = None
            if definition.get("scenario_group") in {"REPLAN", "HUMAN_REVIEW"}:
                dynamic_report = validate_dynamic_definition(
                    definition,
                    repository=post_repository,
                )
                _write_json(root / "dynamic_contract_report.json", dynamic_report)
                if not dynamic_report["passed"]:
                    raise ValueError(
                        f"{scenario_id} dynamic validation failed: "
                        + ", ".join(dynamic_report["failed_checks"])
                    )

            manifest = {
                "evaluation_id": evaluation_id,
                "suite_id": suite_id,
                "scenario_id": scenario_id,
                "scenario_pack": "planning-operational-smoke-v2",
                "status": "CAPTURED",
                "created_at": _utc_now(),
                "request_kind": "PLANNING_SCENARIO_PACK",
                "scenario_group": definition.get("scenario_group", "INITIAL"),
                "warehouse_id": internal.warehouse_id,
                "simulation_id": internal.simulation_id,
                "source_fixture": str(self.fixture_dir.relative_to(ROOT)).replace("\\", "/"),
                "primary_route": None,
                "primary_status": "NOT_EXECUTED",
                "repository_versions": versions,
                "capture_fingerprint": source_input_fingerprint,
                "materialization_status": "PASSED",
                "comparison_status": "NOT_STARTED",
                "comparison_backend": None,
                "comparison_depth": None,
                "dynamic_contract_status": (
                    "PASSED" if dynamic_report is not None else "NOT_APPLICABLE"
                ),
            }
            self.store.save_manifest(evaluation_id, manifest)
            return {
                "scenario_id": scenario_id,
                "title": definition.get("title"),
                "scenario_group": definition.get("scenario_group", "INITIAL"),
                "workload_band": definition.get("expected_routing", {}).get(
                    "workload_band"
                ),
                "evaluation_id": evaluation_id,
                "materialization_status": "PASSED",
                "detail_url": f"/api/v1/debug/evaluations/{evaluation_id}",
                "job_id": None,
            }
        except Exception:
            # An invalid partial directory must never appear as a valid capture.
            import shutil

            shutil.rmtree(root, ignore_errors=True)
            raise


class PlanningScenarioSuiteService:
    """Create one auditable suite and submit every comparison to the serial queue."""

    def __init__(
        self,
        *,
        store: PlanningEvaluationStore | None = None,
        job_service: PlanningEvaluationJobService | None = None,
        materializer: PlanningScenarioMaterializer | None = None,
    ) -> None:
        self.store = store or PlanningEvaluationStore()
        self.job_service = job_service or get_planning_evaluation_job_service()
        self.materializer = materializer or PlanningScenarioMaterializer(store=self.store)
        self.suites = self.store.root / "suites"
        self.suites.mkdir(parents=True, exist_ok=True)

    def _path(self, suite_id: str) -> Path:
        if not suite_id.startswith(_SUITE_ID_PREFIX) or any(
            value not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for value in suite_id
        ):
            raise FileNotFoundError(f"Unknown evaluation suite {suite_id}.")
        return self.suites / f"{suite_id}.json"

    def start(self, request: PlanningScenarioSuiteRequest) -> dict[str, Any]:
        suite_id = f"{_SUITE_ID_PREFIX}{uuid4().hex[:16].upper()}"
        definitions = self.materializer.definitions(
            request.scenario_ids,
            request.scenario_groups,
        )
        record: dict[str, Any] = {
            "suite_id": suite_id,
            "status": "MATERIALIZING",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "materialize_only": request.materialize_only,
            "comparison_request": request.comparison_request().model_dump(mode="json"),
            "scenario_count": len(definitions),
            "completed_count": 0,
            "failed_count": 0,
            "scenarios": [],
            "status_url": f"/api/v1/debug/evaluation-suites/{suite_id}",
        }
        _write_json(self._path(suite_id), record)
        try:
            for definition in definitions:
                item = self.materializer.materialize(definition, suite_id=suite_id)
                if not request.materialize_only:
                    job = self.job_service.submit(
                        str(item["evaluation_id"]),
                        request.job_request(
                            scenario_id=str(item["scenario_id"]), suite_id=suite_id
                        ),
                    )
                    item.update(
                        {
                            "job_id": job.job_id,
                            "job_status": job.status,
                            "job_status_url": job.status_url,
                            "result_url": job.result_url,
                        }
                    )
                record["scenarios"].append(item)
            record["status"] = "SUCCEEDED" if request.materialize_only else "QUEUED"
            if request.materialize_only:
                record["completed_count"] = len(record["scenarios"])
            record["updated_at"] = _utc_now()
            _write_json(self._path(suite_id), record)
            return self.get(suite_id)
        except Exception as exc:
            record["status"] = "FAILED"
            record["error_type"] = type(exc).__name__
            record["error_message"] = str(exc)
            record["updated_at"] = _utc_now()
            _write_json(self._path(suite_id), record)
            raise

    def get(self, suite_id: str) -> dict[str, Any]:
        path = self._path(suite_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown evaluation suite {suite_id}.")
        with _SUITE_LOCK:
            record = _read_json(path)
            if record.get("materialize_only"):
                return record
            jobs = []
            for item in record.get("scenarios", []):
                job_id = item.get("job_id") if isinstance(item, dict) else None
                if not job_id:
                    continue
                job = self.job_service.get(str(job_id))
                item["job_status"] = job.status
                item["current_stage"] = job.current_stage
                item["completed_runs"] = job.completed_runs
                item["total_runs"] = job.total_runs
                item["error_type"] = job.error_type
                item["error_message"] = job.error_message
                jobs.append(job)
            completed = sum(value.status == "SUCCEEDED" for value in jobs)
            failed = sum(value.status == "FAILED" for value in jobs)
            active = sum(value.status not in _TERMINAL_JOB_STATUSES for value in jobs)
            if jobs and active:
                status = "RUNNING"
            elif jobs and failed == len(jobs):
                status = "FAILED"
            elif failed:
                status = "PARTIAL_FAILURE"
            elif jobs:
                status = "SUCCEEDED"
            else:
                status = str(record.get("status") or "FAILED")
            record.update(
                {
                    "status": status,
                    "completed_count": completed,
                    "failed_count": failed,
                    "updated_at": _utc_now(),
                }
            )
            _write_json(path, record)
            return record

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for path in sorted(
            self.suites.glob(f"{_SUITE_ID_PREFIX}*.json"),
            key=lambda value: value.stat().st_mtime,
            reverse=True,
        )[:limit]:
            try:
                values.append(self.get(path.stem))
            except Exception:
                continue
        return values


_SUITE_SERVICE: PlanningScenarioSuiteService | None = None


def get_planning_scenario_suite_service() -> PlanningScenarioSuiteService:
    global _SUITE_SERVICE
    if _SUITE_SERVICE is None:
        _SUITE_SERVICE = PlanningScenarioSuiteService()
    return _SUITE_SERVICE


def shutdown_planning_scenario_suite_service() -> None:
    """Drop the singleton before its shared async job queue is shut down."""

    global _SUITE_SERVICE
    _SUITE_SERVICE = None

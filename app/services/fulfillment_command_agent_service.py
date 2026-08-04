"""Inventory-grounded Agent command generation and deterministic compilation."""
from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.core.llm_gateway import StructuredLLMGateway, get_default_llm_gateway
from app.domain.fulfillment_command import (
    AgentOperationSelection,
    CommandLocation,
    FrontCommand,
    FrontSummary,
    FrontView,
    FulfillmentCommandAgentDecision,
    FulfillmentCommandExpressionDecision,
    FulfillmentCommandGenerateRequest,
    FulfillmentCommandGenerateResponse,
    PlanRequest,
    PlanRoutingContext,
    StructuredInput,
    StructuredOperation,
)
from app.infrastructure.be_centered_postgres import (
    BeCenteredDataError,
    BeCenteredPostgresAdapter,
)
from app.prompts.fulfillment_command_agent import (
    FULFILLMENT_COMMAND_AGENT_PROMPT,
    FULFILLMENT_COMMAND_EXPRESSION_PROMPT,
)
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository


class FulfillmentCommandAgentError(BeCenteredDataError):
    """Raised when the Agent decision contradicts authoritative data."""


class FulfillmentCommandAgentService:
    DEFAULT_SERVICE_TIME_MS = 1_200
    MIN_ROUTING_BATTERY_PCT = 30.0
    OPERATIONAL_EXPRESSION_MARKERS = (
        "입고",
        "출고",
        "작업",
        "처리",
        "로봇",
        "배터리",
        "혼잡",
        "차단",
        "예약",
        "재고",
        "선반",
        "이동",
        "완료시간",
        "우선순위",
        "box",
    )
    UNSAFE_EXPRESSION_MARKERS = (
        "안전 검사 생략",
        "안전검사 생략",
        "안전 무시",
        "차단 무시",
        "차단을 무시",
        "재고 무시",
        "재고를 무시",
        "예약 무시",
        "예약을 무시",
        "충돌 무시",
        "충돌을 무시",
        "검증 생략",
        "검증을 생략",
        "순간이동",
        "텔레포트",
        "벽을 통과",
        "없는 로봇",
        "없는 선반",
        "데이터 삭제",
        "테이블 삭제",
        "drop table",
        "detach delete",
        "hgetall",
    )
    SAFE_POLICY_BY_PROFILE = {
        "BALANCED": "완료시간, 이동거리, 배터리 상태와 기존 작업 부하를 균형 있게 고려하세요.",
        "BATTERY_SAVING": "배터리가 낮거나 작업 중인 로봇은 새 작업에서 제외하고 불필요한 이동을 줄이세요.",
        "CONGESTION_AVOIDANCE": "현재 차단·혼잡 정보와 기존 예약을 준수하면서 우회 비용을 줄이세요.",
        "THROUGHPUT": "가용 로봇에 독립 작업을 적절히 분산하여 전체 BOX 처리시간을 줄이세요.",
    }

    def __init__(
        self,
        *,
        postgres: BeCenteredPostgresAdapter | None = None,
        runtime: BeSpringRuntimeRepository | None = None,
        gateway: StructuredLLMGateway | None = None,
    ) -> None:
        self.postgres = postgres or BeCenteredPostgresAdapter()
        self.runtime = runtime or BeSpringRuntimeRepository()
        self.gateway = gateway

    def generate(
        self,
        simulation_run_id: int,
        request: FulfillmentCommandGenerateRequest,
    ) -> FulfillmentCommandGenerateResponse:
        run = self.postgres.resolve_simulation_run(simulation_run_id)
        warehouse_id = int(run["warehouse_id"])
        products = self.postgres.product_catalog()
        product_by_code = {str(value["product_code"]): value for value in products}
        physical_inventory = self.postgres.inventory_units(
            warehouse_id, include_active_reservations=False
        )
        available_inventory = self.postgres.inventory_units(warehouse_id)
        empty_slots = self.postgres.empty_rack_slots(warehouse_id)
        slot_counts = self.postgres.rack_slot_counts(warehouse_id)
        active_tasks = self.postgres.active_task_summary(simulation_run_id)
        reserved_boxes = self.postgres.active_inventory_reservation_count(
            warehouse_id, simulation_run_id
        )
        runtime = self.runtime.snapshot(simulation_run_id)
        route_nodes = self.postgres.route_nodes(warehouse_id)
        inbound_facilities = self._service_facilities(route_nodes, "INBOUND_HANDOFF_ACCESS")
        # A generated order names the customer's logical destination (chute),
        # never a fixed robot or one of its AMR hand-off ports. The G2P
        # compiler resolves chute -> eligible fixed station -> boundary node.
        facility_rows = (
            self.postgres.facilities(warehouse_id)
            if hasattr(self.postgres, "facilities")
            else []
        )
        outbound_facilities = self._logical_outbound_facilities(facility_rows)
        if not outbound_facilities:
            # Compatibility for a DB that has not installed the shared
            # facility table yet.
            outbound_facilities = self._service_facilities(
                route_nodes, "OUTBOUND_STATION_ACCESS"
            )

        self._validate_request_candidates(
            request=request,
            product_by_code=product_by_code,
            available_inventory=available_inventory,
            empty_slots=empty_slots,
            inbound_facilities=inbound_facilities,
            outbound_facilities=outbound_facilities,
        )

        if request.preselected_operations:
            decision = self._decision_from_preselection(
                simulation_run_id=simulation_run_id,
                warehouse_id=warehouse_id,
                request=request,
                product_by_code=product_by_code,
                available_inventory=available_inventory,
                slot_counts=slot_counts,
                active_tasks=active_tasks,
                runtime=runtime,
                inbound_facilities=inbound_facilities,
                outbound_facilities=outbound_facilities,
            )
        else:
            decision = self._invoke_selection_agent(
                simulation_run_id=simulation_run_id,
                warehouse_id=warehouse_id,
                request=request,
                run=run,
                products=products,
                product_by_code=product_by_code,
                physical_inventory=physical_inventory,
                available_inventory=available_inventory,
                empty_slots=empty_slots,
                slot_counts=slot_counts,
                active_tasks=active_tasks,
                reserved_boxes=reserved_boxes,
                runtime=runtime,
                inbound_facilities=inbound_facilities,
                outbound_facilities=outbound_facilities,
            )
        return self._compile(
            simulation_run_id=simulation_run_id,
            warehouse_id=warehouse_id,
            warehouse_name=self.postgres.warehouse_name(warehouse_id),
            request=request,
            decision=decision,
            product_by_code=product_by_code,
            physical_inventory=physical_inventory,
            available_inventory=available_inventory,
            slot_counts=slot_counts,
            active_tasks=active_tasks,
            reserved_boxes=reserved_boxes,
            runtime=runtime,
            inbound_facilities=inbound_facilities,
            outbound_facilities=outbound_facilities,
        )

    def _invoke_selection_agent(
        self,
        *,
        simulation_run_id: int,
        warehouse_id: int,
        request: FulfillmentCommandGenerateRequest,
        run: dict[str, Any],
        products: list[dict[str, Any]],
        product_by_code: dict[str, dict[str, Any]],
        physical_inventory: list[dict[str, Any]],
        available_inventory: list[dict[str, Any]],
        empty_slots: list[dict[str, Any]],
        slot_counts: dict[str, int],
        active_tasks: dict[str, int],
        reserved_boxes: int,
        runtime: Any,
        inbound_facilities: dict[str, list[dict[str, Any]]],
        outbound_facilities: dict[str, list[dict[str, Any]]],
    ) -> FulfillmentCommandAgentDecision:
        """Backward-compatible path for direct AI Swagger requests."""

        payload = self._agent_payload(
            request=request,
            run=run,
            products=products,
            physical_inventory=physical_inventory,
            available_inventory=available_inventory,
            empty_slots=empty_slots,
            slot_counts=slot_counts,
            active_tasks=active_tasks,
            reserved_boxes=reserved_boxes,
            runtime=runtime,
            inbound_facilities=inbound_facilities,
            outbound_facilities=outbound_facilities,
        )
        gateway = self.gateway or get_default_llm_gateway()
        decision = gateway.invoke_structured(
            system_prompt=FULFILLMENT_COMMAND_AGENT_PROMPT,
            user_payload=payload,
            output_model=FulfillmentCommandAgentDecision,
            trace_name="fulfillment_command_agent",
            tags=["fulfillment-command", "be-shared"],
            metadata={
                "simulation_run_id": simulation_run_id,
                "warehouse_id": warehouse_id,
            },
        )
        try:
            self._validate_decision(
                request=request,
                decision=decision,
                product_by_code=product_by_code,
                available_inventory=available_inventory,
                empty_slots=empty_slots,
                inbound_facilities=inbound_facilities,
                outbound_facilities=outbound_facilities,
            )
        except FulfillmentCommandAgentError as first_error:
            payload["correction"] = {
                "validation_error": str(first_error),
                "previous_decision": decision.model_dump(mode="json"),
                "instruction": "Return a corrected decision using only listed candidates.",
            }
            decision = gateway.invoke_structured(
                system_prompt=FULFILLMENT_COMMAND_AGENT_PROMPT,
                user_payload=payload,
                output_model=FulfillmentCommandAgentDecision,
                trace_name="fulfillment_command_agent_correction",
                tags=["fulfillment-command", "be-shared", "semantic-correction"],
                metadata={
                    "simulation_run_id": simulation_run_id,
                    "warehouse_id": warehouse_id,
                },
            )
            self._validate_decision(
                request=request,
                decision=decision,
                product_by_code=product_by_code,
                available_inventory=available_inventory,
                empty_slots=empty_slots,
                inbound_facilities=inbound_facilities,
                outbound_facilities=outbound_facilities,
            )
        return decision

    def _decision_from_preselection(
        self,
        *,
        simulation_run_id: int,
        warehouse_id: int,
        request: FulfillmentCommandGenerateRequest,
        product_by_code: dict[str, dict[str, Any]],
        available_inventory: list[dict[str, Any]],
        slot_counts: dict[str, int],
        active_tasks: dict[str, int],
        runtime: Any,
        inbound_facilities: dict[str, list[dict[str, Any]]],
        outbound_facilities: dict[str, list[dict[str, Any]]],
    ) -> FulfillmentCommandAgentDecision:
        """Keep Java's freshly selected operation IDs immutable and call LLM only for expression."""

        seed = int(request.selection_seed or 0)
        chooser = random.Random(seed)
        inbound_codes = sorted(inbound_facilities)
        outbound_codes = sorted(outbound_facilities)
        chooser.shuffle(inbound_codes)
        chooser.shuffle(outbound_codes)
        facility_offsets: Counter[str] = Counter()
        operations: list[AgentOperationSelection] = []
        available_by_id = {
            int(value["warehouse_item_id"]): value for value in available_inventory
        }
        expression_operations: list[dict[str, Any]] = []

        selected_values = request.preselected_operations or []
        if any(value.operation_type == "INBOUND" for value in selected_values) and not inbound_codes:
            raise FulfillmentCommandAgentError("No inbound handoff access is available.")
        if any(value.operation_type == "OUTBOUND" for value in selected_values) and not outbound_codes:
            raise FulfillmentCommandAgentError("No outbound station access is available.")

        for value in selected_values:
            if value.operation_type == "INBOUND":
                facility_code = inbound_codes[
                    facility_offsets["INBOUND"] % len(inbound_codes)
                ]
                facility_offsets["INBOUND"] += 1
                product = product_by_code.get(str(value.product_code or ""))
                expression_operations.append(
                    {
                        "operation_type": "INBOUND",
                        "product_code": value.product_code,
                        "product_name": None if product is None else product["product_name"],
                        "box_count": 1,
                        "quantity_ea": None if product is None else int(product["units_per_box"]),
                        "facility_code": facility_code,
                    }
                )
            else:
                facility_code = outbound_codes[
                    facility_offsets["OUTBOUND"] % len(outbound_codes)
                ]
                facility_offsets["OUTBOUND"] += 1
                item = available_by_id.get(int(value.warehouse_item_id or 0))
                expression_operations.append(
                    {
                        "operation_type": "OUTBOUND",
                        "warehouse_item_id": value.warehouse_item_id,
                        "product_code": None if item is None else item["product_code"],
                        "product_name": None if item is None else item["product_name"],
                        "box_count": 1,
                        "quantity_ea": None if item is None else int(item["quantity"]),
                        "facility_code": facility_code,
                    }
                )
            operations.append(
                AgentOperationSelection(
                    operation_type=value.operation_type,
                    product_code=value.product_code,
                    warehouse_item_id=value.warehouse_item_id,
                    facility_code=facility_code,
                    reason=value.reason,
                )
            )

        inbound_count = sum(value.operation_type == "INBOUND" for value in operations)
        outbound_count = sum(value.operation_type == "OUTBOUND" for value in operations)
        mode = "BOTH" if inbound_count and outbound_count else (
            "INBOUND" if inbound_count else "OUTBOUND"
        )
        expression_mode = request.command_expression_mode or "STRUCTURED_ONLY"
        if expression_mode == "AUTO":
            expression_mode = "STRUCTURED_ONLY"
        requested_profile = request.policy_profile or "AUTO"
        profile = requested_profile if requested_profile != "AUTO" else "BALANCED"

        if expression_mode == "STRUCTURED_ONLY":
            decision = FulfillmentCommandAgentDecision(
                mode=mode,
                command_expression_mode="STRUCTURED_ONLY",
                policy_profile=profile,
                operations=operations,
                policy_instruction=None,
                natural_language_command=None,
                decision_summary="Java selected a fresh random feasible BOX batch.",
            )
        else:
            eligible, low_battery, active = self._robot_counts(runtime)
            payload = {
                "command_expression_mode": expression_mode,
                "requested_policy_profile": requested_profile,
                "immutable_operations": expression_operations,
                "warehouse_summary": {
                    "empty_rack_slots": int(slot_counts["empty_rack_slots"]),
                    "available_outbound_boxes": len(available_inventory),
                    "unfinished_tasks": int(active_tasks.get("unfinished_tasks", 0)),
                    "eligible_robot_count": eligible,
                    "low_battery_robot_count": low_battery,
                    "active_robot_count": active,
                },
            }
            expression = (self.gateway or get_default_llm_gateway()).invoke_structured(
                system_prompt=FULFILLMENT_COMMAND_EXPRESSION_PROMPT,
                user_payload=payload,
                output_model=FulfillmentCommandExpressionDecision,
                trace_name="fulfillment_command_expression",
                tags=["fulfillment-command", "expression-only", expression_mode.lower()],
                metadata={
                    "simulation_run_id": simulation_run_id,
                    "warehouse_id": warehouse_id,
                    "selection_seed": seed,
                },
            )
            expression = self._sanitize_expression(expression)
            if requested_profile != "AUTO" and expression.policy_profile != requested_profile:
                raise FulfillmentCommandAgentError(
                    f"Expression LLM did not honor policy profile {requested_profile}."
                )
            if expression_mode == "STRUCTURED_WITH_POLICY" and not expression.policy_instruction:
                raise FulfillmentCommandAgentError(
                    "Expression LLM omitted policy_instruction."
                )
            decision = FulfillmentCommandAgentDecision(
                mode=mode,
                command_expression_mode=expression_mode,
                policy_profile=expression.policy_profile,
                operations=operations,
                policy_instruction=expression.policy_instruction,
                natural_language_command=expression.natural_language_command,
                decision_summary=expression.decision_summary,
            )

        self._validate_decision(
            request=request,
            decision=decision,
            product_by_code=product_by_code,
            available_inventory=available_inventory,
            empty_slots=[{}] * int(slot_counts["empty_rack_slots"]),
            inbound_facilities=inbound_facilities,
            outbound_facilities=outbound_facilities,
        )
        return decision

    @classmethod
    def _sanitize_expression(
        cls,
        expression: FulfillmentCommandExpressionDecision,
    ) -> FulfillmentCommandExpressionDecision:
        """Replace unsafe or unrelated LLM prose with an executable warehouse policy."""

        fallback = cls.SAFE_POLICY_BY_PROFILE.get(
            expression.policy_profile,
            cls.SAFE_POLICY_BY_PROFILE["BALANCED"],
        )

        def acceptable(value: str | None) -> bool:
            if not value or not value.strip():
                return False
            normalized = " ".join(value.casefold().split())
            if any(marker in normalized for marker in cls.UNSAFE_EXPRESSION_MARKERS):
                return False
            return any(
                marker in normalized
                for marker in cls.OPERATIONAL_EXPRESSION_MARKERS
            )

        return expression.model_copy(
            update={
                "policy_instruction": (
                    expression.policy_instruction
                    if acceptable(expression.policy_instruction)
                    else fallback
                ),
                # A missing natural command is compiled later from the immutable
                # structured operations plus the safe policy fallback.
                "natural_language_command": (
                    expression.natural_language_command
                    if acceptable(expression.natural_language_command)
                    else None
                ),
            }
        )

    @staticmethod
    def _service_facilities(
        route_nodes: list[dict[str, Any]], node_type: str
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in route_nodes:
            if str(node.get("be_node_type") or node.get("semantic_type")) != node_type:
                continue
            code = str(node.get("resource_code") or node.get("node_code") or "")
            if code:
                grouped[code].append(node)
        return {
            code: sorted(values, key=lambda value: int(value["node_id"]))
            for code, values in sorted(grouped.items())
        }

    @staticmethod
    def _logical_outbound_facilities(
        facilities: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for facility in facilities:
            if str(facility.get("facility_type")) != "OUTBOUND_CHUTE":
                continue
            code = str(facility.get("facility_code") or "")
            node_id = facility.get("node_id")
            if not code or node_id is None:
                continue
            grouped[code].append(
                {
                    **facility,
                    "node_id": int(node_id),
                    "node_code": str(facility.get("node_code") or code),
                    "resource_code": code,
                    "semantic_type": "OUTBOUND",
                    "be_node_type": "OUTBOUND",
                }
            )
        return {code: values for code, values in sorted(grouped.items())}

    @staticmethod
    def _normalized_filter(values: list[str] | None) -> set[str]:
        return {str(value).strip().upper() for value in values or [] if str(value).strip()}

    def _validate_request_candidates(
        self,
        *,
        request: FulfillmentCommandGenerateRequest,
        product_by_code: dict[str, dict[str, Any]],
        available_inventory: list[dict[str, Any]],
        empty_slots: list[dict[str, Any]],
        inbound_facilities: dict[str, list[dict[str, Any]]],
        outbound_facilities: dict[str, list[dict[str, Any]]],
    ) -> None:
        inbound_filter = self._normalized_filter(request.inbound_product_codes)
        outbound_filter = self._normalized_filter(request.outbound_product_codes)
        unknown = sorted((inbound_filter | outbound_filter) - set(product_by_code))
        if unknown:
            raise FulfillmentCommandAgentError(
                f"Unknown product codes in generation request: {', '.join(unknown)}"
            )
        available_outbound = [
            value
            for value in available_inventory
            if not outbound_filter or str(value["product_code"]).upper() in outbound_filter
        ]
        if request.mode in {"INBOUND", "BOTH"}:
            if not inbound_facilities:
                raise FulfillmentCommandAgentError("No inbound handoff access is available.")
            if request.inbound_count is not None and request.inbound_count > len(empty_slots):
                raise FulfillmentCommandAgentError(
                    f"Requested {request.inbound_count} inbound BOXes but only "
                    f"{len(empty_slots)} empty rack slots are available."
                )
        if request.mode in {"OUTBOUND", "BOTH"}:
            if not outbound_facilities:
                raise FulfillmentCommandAgentError("No outbound station access is available.")
            if request.outbound_count is not None and request.outbound_count > len(available_outbound):
                raise FulfillmentCommandAgentError(
                    f"Requested {request.outbound_count} outbound BOXes but only "
                    f"{len(available_outbound)} unreserved BOXes are available."
                )

    def _agent_payload(
        self,
        *,
        request: FulfillmentCommandGenerateRequest,
        run: dict[str, Any],
        products: list[dict[str, Any]],
        physical_inventory: list[dict[str, Any]],
        available_inventory: list[dict[str, Any]],
        empty_slots: list[dict[str, Any]],
        slot_counts: dict[str, int],
        active_tasks: dict[str, int],
        reserved_boxes: int,
        runtime: Any,
        inbound_facilities: dict[str, list[dict[str, Any]]],
        outbound_facilities: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        stock_units: Counter[str] = Counter()
        stock_boxes: Counter[str] = Counter()
        for value in physical_inventory:
            code = str(value["product_code"])
            stock_boxes[code] += 1
            stock_units[code] += int(value.get("quantity") or 0)
        empty_by_rack = Counter(str(value["rack_code"]) for value in empty_slots)
        inbound_filter = self._normalized_filter(request.inbound_product_codes)
        outbound_filter = self._normalized_filter(request.outbound_product_codes)
        product_candidates = [
            {
                "product_code": str(value["product_code"]),
                "product_name": str(value["product_name"]),
                "category": value.get("category"),
                "units_per_box": int(value.get("units_per_box") or 1),
                "current_boxes": stock_boxes[str(value["product_code"])],
                "current_units": stock_units[str(value["product_code"])],
                "inbound_allowed": not inbound_filter
                or str(value["product_code"]).upper() in inbound_filter,
            }
            for value in products
            if not inbound_filter or str(value["product_code"]).upper() in inbound_filter
        ]
        outbound_candidates = [
            {
                "warehouse_item_id": int(value["warehouse_item_id"]),
                "product_code": str(value["product_code"]),
                "product_name": str(value["product_name"]),
                "quantity": int(value["quantity"]),
                "rack_code": str(value["rack_code"]),
                "rack_level": int(value["rack_level"]),
            }
            for value in available_inventory
            if not outbound_filter or str(value["product_code"]).upper() in outbound_filter
        ]
        robots = []
        for value in runtime.robots:
            battery = float(value.battery_level or 0)
            status = str(value.status or "UNKNOWN").upper()
            load = int(value.current_load_units or 0)
            eligible = status in {"AVAILABLE", "IDLE"} and battery >= self.MIN_ROUTING_BATTERY_PCT and load == 0
            robots.append(
                {
                    "robot_id": int(value.robot_id),
                    "status": status,
                    "battery_pct": battery,
                    "current_load_boxes": 1 if load > 0 else 0,
                    "active_task": value.active_task_code or value.current_task_id,
                    "eligible_for_new_work": eligible,
                }
            )
        return {
            "request": request.model_dump(by_alias=True, exclude_none=True),
            "warehouse": {
                "warehouse_id": int(run["warehouse_id"]),
                "warehouse_code": str(run["warehouse_code"]),
                "run_status": str(run["run_status"]),
            },
            "rack_capacity": {
                **slot_counts,
                "empty_slots_by_rack": dict(sorted(empty_by_rack.items())),
            },
            "inventory": {
                "physical_box_count": len(physical_inventory),
                "available_unreserved_box_count": len(available_inventory),
                "active_reserved_box_count": reserved_boxes,
                "product_candidates": product_candidates,
                "outbound_box_candidates": outbound_candidates,
            },
            "facilities": {
                "inbound": self._facility_summaries(inbound_facilities),
                "outbound": self._facility_summaries(outbound_facilities),
            },
            "runtime": {
                "mode": runtime.mode,
                "robots": robots,
                "eligible_robot_count": sum(1 for value in robots if value["eligible_for_new_work"]),
                "unfinished_tasks": active_tasks,
            },
            "expression_constraints": {
                "allowed_modes": sorted(self._allowed_expression_modes(request)),
                "allowed_policy_profiles": (
                    [request.policy_profile]
                    if request.policy_profile and request.policy_profile != "AUTO"
                    else ["BALANCED", "BATTERY_SAVING", "CONGESTION_AVOIDANCE", "THROUGHPUT"]
                ),
            },
        }

    @staticmethod
    def _facility_summaries(
        values: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "facility_code": code,
                "access_nodes": [str(node["node_code"]) for node in nodes],
            }
            for code, nodes in values.items()
        ]

    @staticmethod
    def _allowed_expression_modes(request: FulfillmentCommandGenerateRequest) -> set[str]:
        if request.mix_structured_with_policy is not None or request.mix_natural_language is not None:
            values = {"STRUCTURED_ONLY"}
            if request.mix_structured_with_policy:
                values.add("STRUCTURED_WITH_POLICY")
            if request.mix_natural_language:
                values.add("NATURAL_LANGUAGE")
            return values
        if request.command_expression_mode and request.command_expression_mode != "AUTO":
            return {request.command_expression_mode}
        return {"STRUCTURED_ONLY", "STRUCTURED_WITH_POLICY", "NATURAL_LANGUAGE"}

    def _validate_decision(
        self,
        *,
        request: FulfillmentCommandGenerateRequest,
        decision: FulfillmentCommandAgentDecision,
        product_by_code: dict[str, dict[str, Any]],
        available_inventory: list[dict[str, Any]],
        empty_slots: list[dict[str, Any]],
        inbound_facilities: dict[str, list[dict[str, Any]]],
        outbound_facilities: dict[str, list[dict[str, Any]]],
    ) -> None:
        errors: list[str] = []
        inbound = [value for value in decision.operations if value.operation_type == "INBOUND"]
        outbound = [value for value in decision.operations if value.operation_type == "OUTBOUND"]
        actual_mode = "BOTH" if inbound and outbound else "INBOUND" if inbound else "OUTBOUND"
        if decision.mode != actual_mode:
            errors.append(f"decision mode {decision.mode} does not match operations {actual_mode}")
        if request.mode != "AUTO" and request.mode != actual_mode:
            errors.append(f"explicit request mode {request.mode} was not honored")
        if request.inbound_count is not None and len(inbound) != request.inbound_count:
            errors.append(f"expected {request.inbound_count} inbound operations, got {len(inbound)}")
        if request.outbound_count is not None and len(outbound) != request.outbound_count:
            errors.append(f"expected {request.outbound_count} outbound operations, got {len(outbound)}")
        if len(inbound) > len(empty_slots):
            errors.append("inbound selections exceed empty rack slots")

        inbound_filter = self._normalized_filter(request.inbound_product_codes)
        outbound_filter = self._normalized_filter(request.outbound_product_codes)
        available_by_id = {int(value["warehouse_item_id"]): value for value in available_inventory}
        outbound_ids: list[int] = []
        for value in inbound:
            code = str(value.product_code or "").upper()
            if code not in product_by_code:
                errors.append(f"unknown inbound product_code {code}")
            if inbound_filter and code not in inbound_filter:
                errors.append(f"inbound product_code {code} violates the request filter")
            if value.facility_code not in inbound_facilities:
                errors.append(f"unknown inbound facility {value.facility_code}")
        for value in outbound:
            item_id = int(value.warehouse_item_id or 0)
            item = available_by_id.get(item_id)
            outbound_ids.append(item_id)
            if item is None:
                errors.append(f"warehouse_item_id {item_id} is not an available unreserved BOX")
            elif outbound_filter and str(item["product_code"]).upper() not in outbound_filter:
                errors.append(f"warehouse_item_id {item_id} violates the outbound product filter")
            if value.facility_code not in outbound_facilities:
                errors.append(f"unknown outbound facility {value.facility_code}")
        if len(outbound_ids) != len(set(outbound_ids)):
            errors.append("the same outbound warehouse_item_id was selected more than once")
        if decision.command_expression_mode not in self._allowed_expression_modes(request):
            errors.append("command_expression_mode violates the request toggle/configuration")
        if request.policy_profile and request.policy_profile != "AUTO" and decision.policy_profile != request.policy_profile:
            errors.append(f"explicit policy profile {request.policy_profile} was not honored")
        if errors:
            raise FulfillmentCommandAgentError("Invalid fulfillment Agent decision: " + "; ".join(errors))

    def _compile(
        self,
        *,
        simulation_run_id: int,
        warehouse_id: int,
        warehouse_name: str,
        request: FulfillmentCommandGenerateRequest,
        decision: FulfillmentCommandAgentDecision,
        product_by_code: dict[str, dict[str, Any]],
        physical_inventory: list[dict[str, Any]],
        available_inventory: list[dict[str, Any]],
        slot_counts: dict[str, int],
        active_tasks: dict[str, int],
        reserved_boxes: int,
        runtime: Any,
        inbound_facilities: dict[str, list[dict[str, Any]]],
        outbound_facilities: dict[str, list[dict[str, Any]]],
    ) -> FulfillmentCommandGenerateResponse:
        generated_at = datetime.now()
        numeric_seed = generated_at.strftime("%Y%m%d%H%M%S%f")
        request_id = f"REQ-AGENT-{simulation_run_id}-{numeric_seed}-{uuid4().hex[:8].upper()}"
        available_by_id = {int(value["warehouse_item_id"]): value for value in available_inventory}
        physical_units: Counter[int] = Counter()
        for value in physical_inventory:
            physical_units[int(value["item_id"])] += int(value.get("quantity") or 0)
        projected_units = Counter(physical_units)
        facility_offsets: Counter[str] = Counter()
        structured: list[StructuredOperation] = []
        front: list[FrontCommand] = []
        inbound_number = outbound_number = 0
        priority = request.priority or "medium"
        release_interval = int(request.release_interval_ms or 0)

        for sequence, selection in enumerate(decision.operations, start=1):
            release_at = (sequence - 1) * release_interval
            if selection.operation_type == "INBOUND":
                inbound_number += 1
                product = product_by_code[str(selection.product_code)]
                node = self._next_facility_node(
                    inbound_facilities, selection.facility_code, facility_offsets
                )
                quantity = int(product.get("units_per_box") or 1)
                before = projected_units[int(product["product_id"])]
                after = before + quantity
                projected_units[int(product["product_id"])] = after
                operation_id = f"IN-{numeric_seed}{inbound_number:03d}"
                source = self._service_location("INBOUND_HANDOFF", selection.facility_code, node)
                destination = CommandLocation(kind="EMPTY_STORAGE_SLOT", label="AI plan 자동 배정 예정")
                structured.append(
                    StructuredOperation(
                        operationId=operation_id,
                        operationType="INBOUND",
                        itemId=int(product["product_id"]),
                        productCode=str(product["product_code"]),
                        quantity=quantity,
                        priority=priority,
                        sourceNodeId=int(node["node_id"]),
                        sourceNodeCode=str(node["node_code"]),
                        sourceFacilityCode=selection.facility_code,
                        releaseAtMs=release_at,
                        pickupServiceTimeMs=self.DEFAULT_SERVICE_TIME_MS,
                        dropServiceTimeMs=self.DEFAULT_SERVICE_TIME_MS,
                        attributes=self._attributes(selection.reason),
                    )
                )
            else:
                outbound_number += 1
                item = available_by_id[int(selection.warehouse_item_id or 0)]
                product = product_by_code[str(item["product_code"])]
                node = self._next_facility_node(
                    outbound_facilities, selection.facility_code, facility_offsets
                )
                quantity = int(item["quantity"])
                before = projected_units[int(product["product_id"])]
                after = max(0, before - quantity)
                projected_units[int(product["product_id"])] = after
                operation_id = f"ORD-{numeric_seed}{outbound_number:03d}"
                source = CommandLocation(
                    kind="STORAGE_RACK",
                    label=f"선반 {item['rack_code']} · {item['rack_level']}층",
                    warehouseItemId=int(item["warehouse_item_id"]),
                    storageLocationId=int(item["storage_location_id"]),
                    rackLevel=int(item["rack_level"]),
                    nodeId=int(item["rack_node_id"]),
                    nodeCode=str(item["rack_code"]),
                )
                destination = self._service_location(
                    "OUTBOUND_STATION", selection.facility_code, node
                )
                structured.append(
                    StructuredOperation(
                        operationId=operation_id,
                        operationType="OUTBOUND",
                        itemId=int(product["product_id"]),
                        productCode=str(product["product_code"]),
                        quantity=quantity,
                        priority=priority,
                        sourceWarehouseItemId=int(item["warehouse_item_id"]),
                        sourceStorageLocationId=int(item["storage_location_id"]),
                        sourceNodeId=int(item["rack_node_id"]),
                        sourceNodeCode=str(item["rack_code"]),
                        # Business input owns only the logical chute.  The
                        # fixed station and one of its AMR boundary nodes are
                        # selected during physical G2P compilation.
                        destinationNodeId=None,
                        destinationNodeCode=None,
                        destinationFacilityCode=selection.facility_code,
                        releaseAtMs=release_at,
                        pickupServiceTimeMs=self.DEFAULT_SERVICE_TIME_MS,
                        dropServiceTimeMs=self.DEFAULT_SERVICE_TIME_MS,
                        attributes=self._attributes(selection.reason),
                    )
                )
            front.append(
                FrontCommand(
                    sequence=sequence,
                    operationId=operation_id,
                    operationType=selection.operation_type,
                    productId=int(product["product_id"]),
                    productCode=str(product["product_code"]),
                    productName=str(product["product_name"]),
                    category=product.get("category"),
                    quantity=quantity,
                    quantityUnit="EA",
                    unitsPerBox=int(product.get("units_per_box") or 1),
                    boxCount=1,
                    priority=priority,
                    releaseAtMs=release_at,
                    source=source,
                    destination=destination,
                    warehouseProductUnitsBefore=int(before),
                    warehouseProductUnitsAfter=int(after),
                    reason=selection.reason,
                )
            )

        eligible, low_battery, active = self._robot_counts(runtime)
        routing = PlanRoutingContext(
            newOperationCount=len(structured),
            unfinishedOperationCount=int(active_tasks.get("unfinished_tasks", 0)),
            eligibleRobotCount=eligible,
            totalRobotCount=len(runtime.robots),
            lowBatteryRobotCount=low_battery,
            activeRobotCount=active,
            source="AI_FULFILLMENT_COMMAND_AGENT",
        )
        plan_request = PlanRequest(
            structuredInput=StructuredInput(
                requestId=request_id,
                operations=structured,
                constraints={
                    "objective_profile": "MIN_TOTAL_COST",
                    # The BE-facing command contract transports constraint
                    # values as strings. Input normalization parses this flag
                    # back to bool while preserving that this is a default,
                    # not an operator-selected objective.
                    "objective_profile_explicit": "false",
                },
                routingContext=routing,
            ),
            userCommand=self._user_command(decision, front),
            optimizationBackend="cuopt",
        )
        summary = FrontSummary(
            requestedInboundCommands=request.inbound_count if request.inbound_count is not None else inbound_number,
            generatedInboundCommands=inbound_number,
            requestedOutboundCommands=request.outbound_count if request.outbound_count is not None else outbound_number,
            generatedOutboundCommands=outbound_number,
            totalStorageLocations=int(slot_counts["storage_locations"]),
            totalStorageSlots=int(slot_counts["rack_slots"]),
            occupiedStorageSlots=int(slot_counts["occupied_rack_slots"]),
            emptyStorageSlots=int(slot_counts["empty_rack_slots"]),
            availableOutboundBoxes=len(available_inventory),
            excludedReservedBoxes=reserved_boxes,
            totalInventoryUnits=sum(physical_units.values()),
            generatedInboundUnits=sum(value.quantity for value in front if value.operation_type == "INBOUND"),
            generatedOutboundUnits=sum(value.quantity for value in front if value.operation_type == "OUTBOUND"),
        )
        return FulfillmentCommandGenerateResponse(
            planRequest=plan_request,
            frontView=FrontView(
                requestId=request_id,
                simulationRunId=simulation_run_id,
                warehouseId=warehouse_id,
                warehouseName=warehouse_name,
                requestedMode=request.mode,
                mode=decision.mode,
                requestedCommandExpressionMode=request.command_expression_mode or "STRUCTURED_ONLY",
                commandExpressionMode=decision.command_expression_mode,
                policyProfile=decision.policy_profile,
                generatedAt=generated_at,
                summary=summary,
                commands=front,
                warnings=[],
            ),
        )

    @staticmethod
    def _next_facility_node(
        facilities: dict[str, list[dict[str, Any]]],
        facility_code: str,
        offsets: Counter[str],
    ) -> dict[str, Any]:
        nodes = facilities[facility_code]
        index = offsets[facility_code] % len(nodes)
        offsets[facility_code] += 1
        return nodes[index]

    @staticmethod
    def _service_location(
        kind: str, facility_code: str, node: dict[str, Any]
    ) -> CommandLocation:
        return CommandLocation(
            kind=kind,
            label=facility_code,
            nodeId=int(node["node_id"]),
            nodeCode=str(node["node_code"]),
            facilityCode=facility_code,
        )

    @staticmethod
    def _attributes(reason: str) -> str:
        return json.dumps(
            {
                "generated_by": "AI_FULFILLMENT_COMMAND_AGENT",
                "transport_unit": "BOX",
                "box_count": 1,
                "selection_reason": reason,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _user_command(
        decision: FulfillmentCommandAgentDecision, commands: list[FrontCommand]
    ) -> str | None:
        if decision.command_expression_mode == "STRUCTURED_ONLY":
            return None
        policy = decision.policy_instruction or decision.decision_summary
        if decision.command_expression_mode == "STRUCTURED_WITH_POLICY":
            return policy
        if decision.natural_language_command:
            return decision.natural_language_command
        lines = [
            (
                f"{value.operation_type} {value.product_name}({value.product_code}) "
                f"1 BOX({value.quantity} EA): {value.source.label}에서 {value.destination.label}로 처리"
            )
            for value in commands
        ]
        return "다음 입출고 작업을 수행하세요. " + " / ".join(lines) + f". 운영 정책: {policy}"

    def _robot_counts(self, runtime: Any) -> tuple[int, int, int]:
        eligible = low_battery = active = 0
        for value in runtime.robots:
            status = str(value.status or "").upper()
            battery = float(value.battery_level or 0)
            load = int(value.current_load_units or 0)
            if battery < self.MIN_ROUTING_BATTERY_PCT:
                low_battery += 1
            if status in {"MOVING", "WORKING", "ASSIGNED", "BUSY"} or value.current_task_id:
                active += 1
            if status in {"AVAILABLE", "IDLE"} and battery >= self.MIN_ROUTING_BATTERY_PCT and load == 0:
                eligible += 1
        return eligible, low_battery, active

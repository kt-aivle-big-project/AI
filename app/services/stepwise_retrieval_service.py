"""Stepwise semantic retrieval for the Agent formulation path.

The Rule path never uses this module.  The LLM selects one bounded read-only
Tool at a time.  This module validates that call, resolves only the identifiers
needed by that call, executes one deterministic adapter, and evaluates whether
more evidence is required.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from app.core.config import get_settings
from app.domain.schemas import (
    CandidatePutawaySlot,
    CandidateStock,
    EdgeOccupancy,
    EdgePenalty,
    EdgeReservation,
    EntityResolutionCandidate,
    EntityResolutionResult,
    InboundTaskNeed,
    InventoryContext,
    InventoryQueryScope,
    InventoryTaskNeed,
    MapConstraints,
    MapContext,
    NormalizedOperation,
    NormalizedWarehouseRequest,
    RelevantMapNode,
    ResolvedToolRequest,
    RetrievalContextSufficiencyResult,
    RetrievalObservation,
    RetrievalToolCallValidationResult,
    RetrievalToolName,
    RetrievalToolRequest,
    RobotRuntime,
    RobotRuntimeContext,
    WorkflowValidationIssue,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository
from app.services.graph_service import DirectedGraphService

ALLOWED_TOOLS: set[str] = {
    "find_orders",
    "get_order_facts",
    "get_inbound_facts",
    "get_inventory_candidates",
    "get_robot_candidates",
    "resolve_map_entities",
    "get_connecting_subgraph",
    "get_runtime_constraints",
    "get_active_operations",
}

_ROBOT_STATUS_ALIASES: dict[str, str] = {
    "idle": "idle",
    "available": "idle",
    "대기": "idle",
    "유휴": "idle",
    "charging": "charging",
    "charge": "charging",
    "충전": "charging",
    "working": "working",
    "busy": "working",
    "in_progress": "working",
    "작업": "working",
    "maintenance": "maintenance",
    "정비": "maintenance",
    "점검": "maintenance",
    "offline": "offline",
    "오프라인": "offline",
    "error": "error",
    "fault": "error",
    "고장": "error",
}


def normalize_robot_status(value: str) -> str | None:
    """Return one canonical robot-runtime status or ``None`` when unsupported."""

    normalized = re.sub(r"[\s_-]+", "", str(value).casefold())
    for alias, canonical in _ROBOT_STATUS_ALIASES.items():
        if re.sub(r"[\s_-]+", "", alias.casefold()) == normalized:
            return canonical
    return None


def status_filters_from_reference(value: str) -> list[str]:
    """Extract status classes from phrases that describe robot groups."""

    text = str(value).casefold()
    groups = {
        "charging": ("charging", "충전 중", "충전중", "충전 상태"),
        "working": ("working", "busy", "작업 중", "작업중", "업무 중", "임무 수행 중"),
        "maintenance": ("maintenance", "정비 중", "정비중", "점검 중", "점검중"),
        "offline": ("offline", "오프라인"),
        "error": ("fault", "error", "고장", "오류 상태"),
    }
    return [
        status
        for status, markers in groups.items()
        if any(marker.casefold() in text for marker in markers)
    ]

_RAW_STORAGE_PATTERNS = (
    # Block actual storage/query syntax, not ordinary prose such as
    # "Load order facts from the prior observation."
    re.compile(r"\bselect\b[\s\S]*\bfrom\b", re.I),
    re.compile(r"\bmatch\s*\(", re.I),
    re.compile(r"\breturn\s+(?:\*|[A-Za-z_])", re.I),
    re.compile(r"redis://", re.I),
    re.compile(r"(?:robot|edge|order):[A-Za-z0-9_-]+:", re.I),
)


class RetrievalToolCallValidator:
    """Validate one LLM-selected Tool call without forcing a full Tool list."""

    def validate(
        self,
        *,
        request: RetrievalToolRequest,
        observations: list[RetrievalObservation],
    ) -> RetrievalToolCallValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if request.tool_name not in ALLOWED_TOOLS:
            errors.append(f"UNSUPPORTED_RETRIEVAL_TOOL:{request.tool_name}")
        if not request.request_id.strip():
            errors.append("EMPTY_TOOL_REQUEST_ID")
        if any(value.request_id == request.request_id for value in observations):
            errors.append(f"DUPLICATE_TOOL_REQUEST_ID:{request.request_id}")

        all_strings = [
            request.request_id,
            request.item_text or "",
            request.purpose,
            *request.exact_ids,
            *request.item_ids,
            *request.statuses,
            *request.include_statuses,
            *request.exclude_statuses,
            *[value.raw_text for value in request.raw_references],
            *[value.exact_id_hint or "" for value in request.raw_references],
        ]
        for value in all_strings:
            if any(pattern.search(value) for pattern in _RAW_STORAGE_PATTERNS):
                errors.append("RAW_STORAGE_SYNTAX_FORBIDDEN")
                break

        completed = {value.tool_name for value in observations}
        if request.tool_name == "find_orders":
            if not (request.exact_ids or request.item_ids or request.item_text or request.raw_references):
                errors.append("FIND_ORDERS_REQUIRES_SEARCH_INPUT")
        elif request.tool_name == "get_order_facts":
            if not (request.exact_ids or request.raw_references or "find_orders" in completed):
                errors.append("GET_ORDER_FACTS_REQUIRES_ORDER_REFERENCE")
        elif request.tool_name == "get_inbound_facts":
            if not (request.exact_ids or request.raw_references):
                errors.append("GET_INBOUND_FACTS_REQUIRES_INBOUND_REFERENCE")
        elif request.tool_name == "get_inventory_candidates":
            if not (
                request.exact_ids
                or request.item_ids
                or "get_order_facts" in completed
                or "find_orders" in completed
            ):
                errors.append("INVENTORY_LOOKUP_REQUIRES_ORDER_FACTS")
        elif request.tool_name == "get_robot_candidates":
            if request.item_text:
                errors.append("ROBOT_LOOKUP_DOES_NOT_ACCEPT_ITEM_TEXT")
            for value in [*request.statuses, *request.include_statuses, *request.exclude_statuses]:
                if normalize_robot_status(value) is None:
                    errors.append(f"UNSUPPORTED_ROBOT_STATUS_FILTER:{value}")
        elif request.tool_name == "resolve_map_entities":
            if not (request.exact_ids or request.raw_references):
                errors.append("MAP_ENTITY_RESOLUTION_REQUIRES_REFERENCE")
        elif request.tool_name == "get_connecting_subgraph":
            has_robot = "get_robot_candidates" in completed
            has_order = "get_order_facts" in completed or "find_orders" in completed
            has_inventory = "get_inventory_candidates" in completed
            has_inbound = "get_inbound_facts" in completed
            if not has_robot:
                errors.append("SUBGRAPH_REQUIRES_ROBOT_OBSERVATION")
            if has_order != has_inventory:
                errors.append("SUBGRAPH_REQUIRES_COMPLETE_OUTBOUND_OBSERVATIONS")
            if not ((has_order and has_inventory) or has_inbound):
                errors.append("SUBGRAPH_REQUIRES_OPERATION_OBSERVATION")
        elif request.tool_name == "get_runtime_constraints":
            if "get_connecting_subgraph" not in completed and not request.exact_ids:
                errors.append("RUNTIME_CONSTRAINTS_REQUIRE_SUBGRAPH_OR_EXPLICIT_EDGE")

        fingerprint = self.fingerprint(request)
        for observation in observations:
            if observation.data.get("request_fingerprint") == fingerprint:
                errors.append("DUPLICATE_TOOL_CALL")
                break
        return RetrievalToolCallValidationResult(valid=not errors, errors=errors, warnings=warnings)

    @staticmethod
    def fingerprint(request: RetrievalToolRequest) -> str:
        payload = request.model_dump(mode="json", exclude={"request_id", "purpose"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class ResolvedRetrievalToolCallValidator:
    """Validate actual canonical keys after dependency materialization."""

    def validate(
        self,
        *,
        request: ResolvedToolRequest,
        observations: list[RetrievalObservation],
    ) -> RetrievalToolCallValidationResult:
        errors: list[str] = []
        completed = {value.tool_name for value in observations}
        if request.tool_name == "get_order_facts" and not request.order_ids:
            errors.append("RESOLVED_ORDER_FACTS_REQUIRES_ORDER_ID")
        elif request.tool_name == "get_inbound_facts" and not request.inbound_ids:
            errors.append("RESOLVED_INBOUND_FACTS_REQUIRES_INBOUND_ID")
        elif request.tool_name == "get_inventory_candidates" and not (
            request.order_ids or request.item_ids
        ):
            errors.append("RESOLVED_INVENTORY_REQUIRES_ORDER_OR_ITEM")
        elif request.tool_name == "resolve_map_entities" and not (
            request.rack_ids or request.node_ids or request.edge_ids
        ):
            errors.append("DERIVED_MAP_REFERENCE_EMPTY")
        elif request.tool_name == "get_connecting_subgraph":
            has_robot = "get_robot_candidates" in completed
            has_order = "get_order_facts" in completed or "find_orders" in completed
            has_inventory = "get_inventory_candidates" in completed
            has_inbound = "get_inbound_facts" in completed
            if (
                not has_robot
                or has_order != has_inventory
                or not ((has_order and has_inventory) or has_inbound)
            ):
                errors.append("RESOLVED_SUBGRAPH_DEPENDENCIES_INCOMPLETE")
        elif request.tool_name == "get_runtime_constraints" and not (
            request.edge_ids or "get_connecting_subgraph" in completed
        ):
            errors.append("DERIVED_RUNTIME_EDGE_SET_EMPTY")
        return RetrievalToolCallValidationResult(
            valid=not errors,
            errors=errors,
            warnings=[],
        )


@dataclass(frozen=True)
class ToolResolutionOutcome:
    """Internal resolution result for one proposed Tool call."""

    request: ResolvedToolRequest | None
    entity_resolutions: list[EntityResolutionResult]
    ambiguous_references: list[str]
    not_found_references: list[str]
    user_owned_not_found_references: list[str]


class StepwiseQueryKeyResolver:
    """Resolve only the current Tool call against authoritative indexes."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()

    def resolve(
        self,
        *,
        tool_request: RetrievalToolRequest,
        normalized_request: NormalizedWarehouseRequest,
        observations: list[RetrievalObservation],
        selected_entity_ids: list[str] | None = None,
    ) -> ToolResolutionOutcome:
        order_ids: list[str] = []
        inbound_ids: list[str] = []
        item_ids: list[str] = list(tool_request.item_ids)
        robot_ids: list[str] = []
        rack_ids: list[str] = []
        node_ids: list[str] = []
        edge_ids: list[str] = []
        resolutions: list[EntityResolutionResult] = []
        ambiguous: list[str] = []
        not_found: list[str] = []
        user_not_found: list[str] = []
        operator_selected_ids = set(selected_entity_ids or [])

        observed = ObservationIndex(observations)

        def record(
            *,
            reference_id: str,
            raw_text: str,
            candidates: list[EntityResolutionCandidate],
            allow_multiple: bool,
            required: bool = True,
        ) -> list[str]:
            # An in-route HITL response is authoritative for the ambiguous
            # candidate set that triggered the pause.  It narrows the current
            # resolver result but never invents an entity outside the
            # authoritative candidates returned by the repository.
            if operator_selected_ids:
                selected = [
                    candidate
                    for candidate in candidates
                    if candidate.entity_id in operator_selected_ids
                ]
                if selected:
                    candidates = selected
                    allow_multiple = False

            if not candidates:
                status = "NOT_FOUND"
                ids: list[str] = []
                if required:
                    not_found.append(raw_text)
                    if self._is_user_owned_reference(raw_text, normalized_request):
                        user_not_found.append(raw_text)
                    reason = "No authoritative entity matched the required reference."
                else:
                    reason = "No authoritative entity matched the optional reference; execution may continue."
            elif len(candidates) > 1 and not allow_multiple:
                status = "AMBIGUOUS"
                ids = []
                if required:
                    ambiguous.append(raw_text)
                    reason = "Multiple authoritative entities matched the required reference."
                else:
                    reason = "Multiple entities matched the optional reference; it is non-blocking."
            else:
                status = "RESOLVED"
                ids = [value.entity_id for value in candidates]
                reason = "Reference resolved against authoritative indexes."
            resolutions.append(
                EntityResolutionResult(
                    reference_id=reference_id,
                    raw_text=raw_text,
                    status=status,
                    resolved_entity_ids=ids,
                    candidates=candidates,
                    reason=reason,
                )
            )
            return ids

        tool = tool_request.tool_name
        if tool in {"find_orders", "get_order_facts"}:
            for index, exact in enumerate(tool_request.exact_ids, start=1):
                order = self.repository.get_order(exact)
                candidates = [] if order is None else [self._order_candidate(order, "EXACT_ID", 1.0)]
                order_ids.extend(record(
                    reference_id=f"order-exact:{index}",
                    raw_text=exact,
                    candidates=candidates,
                    allow_multiple=tool_request.allow_multiple_matches,
                ))
            for reference in tool_request.raw_references:
                exact = self.repository.get_order(reference.exact_id_hint) if reference.exact_id_hint else None
                if exact is not None:
                    candidates = [self._order_candidate(exact, "EXACT_ID", 1.0)]
                elif tool == "find_orders":
                    # Search text remains safe and may legitimately return multiple candidates.
                    matches = self.repository.find_orders(
                        item_ids=item_ids,
                        item_text=tool_request.item_text or reference.raw_text,
                        statuses=tool_request.statuses or ["pending"],
                    )
                    candidates = [self._order_candidate(value, "ATTRIBUTE", 0.9) for value in matches]
                else:
                    matches = self.repository.find_orders(
                        item_ids=item_ids,
                        item_text=tool_request.item_text or reference.raw_text,
                        statuses=tool_request.statuses or ["pending"],
                    )
                    candidates = [self._order_candidate(value, "ATTRIBUTE", 0.9) for value in matches]
                order_ids.extend(record(
                    reference_id=reference.reference_id,
                    raw_text=reference.raw_text,
                    candidates=candidates,
                    allow_multiple=tool_request.allow_multiple_matches,
                ))
            if not order_ids and tool == "get_order_facts":
                order_ids.extend(observed.order_ids)
        elif tool == "get_inbound_facts":
            for index, exact in enumerate(tool_request.exact_ids, start=1):
                receipt = self.repository.get_inbound_receipt(exact)
                candidates = (
                    []
                    if receipt is None
                    else [self._inbound_candidate(receipt, "EXACT_ID", 1.0)]
                )
                inbound_ids.extend(record(
                    reference_id=f"inbound-exact:{index}",
                    raw_text=exact,
                    candidates=candidates,
                    allow_multiple=False,
                ))
            for index, reference in enumerate(tool_request.raw_references, start=1):
                target = reference.exact_id_hint
                if not target:
                    embedded = re.findall(r"(?<![A-Za-z0-9])IN-[A-Za-z0-9_-]+", reference.raw_text, flags=re.I)
                    target = embedded[0].upper() if len(embedded) == 1 else reference.raw_text.strip()
                receipt = self.repository.get_inbound_receipt(target) if target else None
                candidates = (
                    []
                    if receipt is None
                    else [self._inbound_candidate(receipt, "EXACT_ID", 1.0)]
                )
                inbound_ids.extend(record(
                    reference_id=reference.reference_id or f"inbound-ref:{index}",
                    raw_text=reference.raw_text,
                    candidates=candidates,
                    allow_multiple=False,
                    required=reference.required,
                ))
            if not inbound_ids:
                inbound_ids.extend(observed.inbound_ids)
        elif tool == "get_inventory_candidates":
            for exact in tool_request.exact_ids:
                order = self.repository.get_order(exact)
                if order is None:
                    not_found.append(exact)
                    if self._is_user_owned_reference(exact, normalized_request):
                        user_not_found.append(exact)
                else:
                    order_ids.append(exact)
            if not order_ids:
                order_ids.extend(observed.order_ids)
            if not item_ids:
                item_ids.extend(observed.item_ids)
        elif tool in {"get_robot_candidates", "get_active_operations"}:
            # Operator-declared exclusions are authoritative input constraints.
            # Merge them even if the LLM omits them from the Tool call.
            exact_robot_refs = self._dedupe(
                [*normalized_request.constraints.excluded_robot_ids, *tool_request.exact_ids]
            )
            for index, ref in enumerate(exact_robot_refs, start=1):
                candidates = self._resolve_one_robot_reference(ref, exact_only=True)
                robot_ids.extend(record(
                    reference_id=f"robot-exact:{index}",
                    raw_text=ref,
                    candidates=candidates,
                    allow_multiple=False,
                    required=True,
                ))

            for index, reference in enumerate(tool_request.raw_references, start=1):
                # A phrase such as "충전 중인 로봇" is a status predicate, not an
                # entity name.  Keep it out of entity resolution even when the
                # model places it in raw_references.
                status_filters = status_filters_from_reference(reference.raw_text)
                if status_filters:
                    resolutions.append(
                        EntityResolutionResult(
                            reference_id=reference.reference_id,
                            raw_text=reference.raw_text,
                            status="RESOLVED",
                            resolved_entity_ids=[],
                            candidates=[],
                            reason=(
                                "Reference was interpreted as robot status filter(s): "
                                + ", ".join(status_filters)
                            ),
                        )
                    )
                    continue

                hinted = reference.exact_id_hint
                embedded = self._embedded_robot_ids(reference.raw_text)
                lookup_value = hinted or (embedded[0] if len(embedded) == 1 else reference.raw_text)
                candidates = self._resolve_one_robot_reference(
                    lookup_value,
                    exact_only=bool(hinted or embedded),
                )
                robot_ids.extend(record(
                    reference_id=reference.reference_id or f"robot-ref:{index}",
                    raw_text=reference.raw_text,
                    candidates=candidates,
                    allow_multiple=False,
                    required=reference.required,
                ))
        elif tool == "resolve_map_entities":
            exact_map_ids = self._dedupe([
                *normalized_request.constraints.soft_avoid_edge_ids,
                *normalized_request.constraints.hard_block_edge_ids,
                *[value.edge_id for value in normalized_request.constraints.conditional_edge_policies],
                *tool_request.exact_ids,
                *(
                    self._derived_map_ids(
                        observations=observations,
                        expected_types=tool_request.expected_entity_types,
                    )
                    if tool_request.derive_from_previous_results
                    else []
                ),
            ])
            for index, exact in enumerate(exact_map_ids, start=1):
                candidate = self._map_exact_candidate(
                    exact,
                    expected_types=tool_request.expected_entity_types,
                )
                ids = record(
                    reference_id=f"map-exact:{index}",
                    raw_text=exact,
                    candidates=[] if candidate is None else [candidate],
                    allow_multiple=tool_request.allow_multiple_matches,
                    required=True,
                )
                for value in ids:
                    if self.repository.edge(value):
                        edge_ids.append(value)
                    elif self.repository.rack(value):
                        rack_ids.append(value)
                    else:
                        node_ids.append(value)
            for reference in tool_request.raw_references:
                expected_types = list(reference.expected_entity_types or tool_request.expected_entity_types)
                allow_multiple = self._map_reference_allows_multiple(
                    raw_text=reference.raw_text,
                    explicitly_allowed=tool_request.allow_multiple_matches,
                )

                # An exact_id_hint is not a suggestion for another semantic
                # search.  Validate the hinted entity and use it directly.
                if reference.exact_id_hint:
                    exact_candidate = self._map_exact_candidate(
                        reference.exact_id_hint,
                        expected_types=expected_types,
                    )
                    candidates = [] if exact_candidate is None else [exact_candidate]
                else:
                    embedded = self._embedded_map_ids(reference.raw_text, expected_types)
                    if embedded:
                        candidates = [
                            candidate
                            for entity_id in embedded
                            if (candidate := self._map_exact_candidate(
                                entity_id,
                                expected_types=expected_types,
                            )) is not None
                        ]
                    else:
                        # Corridor/aisle mentions are entity-set requests. Ensure
                        # EDGE is included so "D 출고 통로" can resolve to O_D
                        # plus its authoritative incident corridor edges.
                        if allow_multiple and "EDGE" not in expected_types:
                            expected_types.append("EDGE")
                        matches = self.repository.search_map_entities(
                            raw_text=reference.raw_text,
                            expected_types=expected_types,
                        )
                        candidates = [EntityResolutionCandidate.model_validate(value) for value in matches]
                ids = record(
                    reference_id=reference.reference_id,
                    raw_text=reference.raw_text,
                    candidates=candidates,
                    allow_multiple=allow_multiple,
                    required=reference.required,
                )
                for value in ids:
                    if self.repository.edge(value):
                        edge_ids.append(value)
                    elif self.repository.rack(value):
                        rack_ids.append(value)
                    else:
                        node_ids.append(value)
        elif tool == "get_connecting_subgraph":
            node_ids.extend(observed.anchor_node_ids)
        elif tool == "get_runtime_constraints":
            edge_ids.extend(tool_request.exact_ids)
            edge_ids.extend(observed.relevant_edge_ids)

        resolved = None
        if not ambiguous and not not_found:
            resolved = ResolvedToolRequest(
                request_id=tool_request.request_id,
                tool_name=tool_request.tool_name,
                order_ids=self._dedupe(order_ids),
                inbound_ids=self._dedupe(inbound_ids),
                item_ids=self._dedupe(item_ids),
                robot_ids=self._dedupe(robot_ids),
                rack_ids=self._dedupe(rack_ids),
                node_ids=self._dedupe(node_ids),
                edge_ids=self._dedupe(edge_ids),
                statuses=self._canonical_statuses(tool_request.statuses),
                include_statuses=self._canonical_statuses(tool_request.include_statuses),
                exclude_statuses=self._canonical_statuses([
                    *normalized_request.constraints.excluded_robot_statuses,
                    *tool_request.exclude_statuses,
                    *[
                        status
                        for reference in tool_request.raw_references
                        for status in status_filters_from_reference(reference.raw_text)
                    ],
                ]),
                item_text=tool_request.item_text,
                allow_multiple_matches=tool_request.allow_multiple_matches,
                derive_from_previous_results=tool_request.derive_from_previous_results,
                include_runtime_constraints=tool_request.include_runtime_constraints,
                purpose=tool_request.purpose,
            )
        return ToolResolutionOutcome(
            request=resolved,
            entity_resolutions=resolutions,
            ambiguous_references=self._dedupe(ambiguous),
            not_found_references=self._dedupe(not_found),
            user_owned_not_found_references=self._dedupe(user_not_found),
        )

    def _derived_map_ids(
        self,
        *,
        observations: list[RetrievalObservation],
        expected_types: list[str],
    ) -> list[str]:
        """Materialize authoritative map/rack keys from completed dependency reads."""

        expected = {str(value).upper() for value in expected_types}
        allow_nodes = not expected or bool(
            expected
            & {
                "NODE",
                "RACK_ACCESS",
                "OUTBOUND",
                "INBOUND",
                "INBOUND_HANDOFF",
                "OUTBOUND_STATION",
                "EMPTY_TOTE_BUFFER",
                "CHARGING_SLOT",
            }
        )
        allow_edges = not expected or "EDGE" in expected
        allow_racks = not expected or "RACK" in expected
        values: list[str] = []
        for observation in observations:
            data = observation.data
            if allow_nodes:
                for order in data.get("orders", []):
                    destination = order.get("delivery_node") or order.get("logical_destination_id")
                    if destination:
                        values.append(str(destination))
                for handoff in data.get("inbound_handoffs", []):
                    values.extend(str(value) for value in handoff.get("access_node_ids", []) if value)
                for slot in data.get("putaway_slots", []):
                    values.extend(str(value) for value in slot.get("access_node_ids", []) if value)
                for stock in data.get("stocks", []):
                    values.extend(str(value) for value in stock.get("access_node_ids", []) if value)
                for robot in data.get("robots", []):
                    current_node = robot.get("current_node")
                    if current_node:
                        values.append(str(current_node))
                values.extend(str(value) for value in data.get("anchor_node_ids", []) if value)
            if allow_racks:
                values.extend(
                    str(stock.get("rack_id"))
                    for stock in data.get("stocks", [])
                    if stock.get("rack_id")
                )
                values.extend(
                    str(slot.get("rack_id"))
                    for slot in data.get("putaway_slots", [])
                    if slot.get("rack_id")
                )
            if allow_edges:
                values.extend(str(value) for value in data.get("relevant_edge_ids", []) if value)
                for summary in data.get("path_summaries", []):
                    values.extend(str(value) for value in summary.get("edge_ids", []) if value)
        return self._dedupe(values)

    def _resolve_one_robot_reference(
        self,
        value: str,
        *,
        exact_only: bool,
    ) -> list[EntityResolutionCandidate]:
        """Resolve one robot reference independently with strict type isolation."""

        text = str(value).strip()
        if not text:
            return []
        direct = self.repository.robots.get(text)
        if direct is not None:
            return [
                EntityResolutionCandidate(
                    entity_id=str(direct["robot_id"]),
                    entity_type="ROBOT",
                    display_name=str(direct.get("robot_code") or direct["robot_id"]),
                    match_method="EXACT_ID",
                    confidence=1.0,
                )
            ]
        if exact_only:
            # Exact hints may also use an authoritative robot code/alias.
            matches = self.repository.find_robots([text])
        else:
            # Resolve this reference alone.  Never reuse matches from another
            # reference; that caused ORD-001 to inherit R003 in the live v12 run.
            matches = self.repository.find_robots([text])
        return [
            EntityResolutionCandidate(
                entity_id=str(robot["robot_id"]),
                entity_type="ROBOT",
                display_name=str(robot.get("robot_code") or robot["robot_id"]),
                match_method="ALIAS",
                confidence=0.98,
            )
            for robot in matches
        ]

    def _embedded_robot_ids(self, raw_text: str) -> list[str]:
        """Return real robot IDs explicitly embedded in one prose reference."""

        text = str(raw_text).casefold()
        return [
            robot_id
            for robot_id in self.repository.robots
            if re.search(rf"(?<![a-z0-9]){re.escape(robot_id.casefold())}(?![a-z0-9])", text)
        ]

    def _map_exact_candidate(
        self,
        entity_id: str,
        *,
        expected_types: list[str] | None,
    ) -> EntityResolutionCandidate | None:
        """Validate one exact map identifier and its expected entity type."""

        matches = self.repository.search_map_entities(
            raw_text=entity_id,
            expected_types=expected_types or [],
        )
        for value in matches:
            if str(value.get("entity_id")) == entity_id:
                candidate = EntityResolutionCandidate.model_validate(value)
                return candidate.model_copy(update={"match_method": "EXACT_ID", "confidence": 1.0})
        return None

    def _embedded_map_ids(self, raw_text: str, expected_types: list[str]) -> list[str]:
        """Extract existing map IDs embedded in prose without fuzzy matching."""

        text = str(raw_text).casefold()
        values: list[str] = []
        for entity_id in [*self.repository.nodes, *self.repository.edges]:
            if not re.search(rf"(?<![a-z0-9]){re.escape(entity_id.casefold())}(?![a-z0-9])", text):
                continue
            if self._map_exact_candidate(entity_id, expected_types=expected_types) is not None:
                values.append(entity_id)
        return self._dedupe(values)

    @staticmethod
    def _map_reference_allows_multiple(*, raw_text: str, explicitly_allowed: bool) -> bool:
        """Return whether a map phrase semantically denotes an entity set."""

        if explicitly_allowed:
            return True
        text = str(raw_text).casefold()
        return any(
            marker in text
            for marker in (
                "통로",
                "corridor",
                "aisle",
                "구역",
                "zone",
                "주변",
                "접근 경로",
                "approach",
            )
        )

    @staticmethod
    def _canonical_statuses(values: Iterable[str]) -> list[str]:
        """Normalize supported status aliases and remove duplicates."""

        resolved = [normalize_robot_status(value) for value in values]
        return list(dict.fromkeys(value for value in resolved if value))

    def _order_candidate(self, order: dict[str, Any], method: str, confidence: float) -> EntityResolutionCandidate:
        return EntityResolutionCandidate(
            entity_id=str(order["order_id"]),
            entity_type="ORDER",
            display_name=f"{order['order_id']} / {order.get('item_id')}",
            match_method=method,
            confidence=confidence,
        )

    def _inbound_candidate(
        self,
        receipt: dict[str, Any],
        method: str,
        confidence: float,
    ) -> EntityResolutionCandidate:
        return EntityResolutionCandidate(
            entity_id=str(receipt["inbound_id"]),
            entity_type="INBOUND",
            display_name=(
                f"{receipt['inbound_id']} / "
                f"{receipt.get('handling_unit_id') or receipt.get('item_id')}"
            ),
            match_method=method,
            confidence=confidence,
        )

    def _node_entity_type(self, node_id: str) -> str:
        node = self.repository.node(node_id) or {}
        return {
            "outbound": "OUTBOUND",
            "inbound": "INBOUND",
            "rack_access": "RACK_ACCESS",
            "charging_slot": "CHARGING_SLOT",
        }.get(str(node.get("type", "")), "NODE")

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in values if value))

    @staticmethod
    def _is_user_owned_reference(value: str, request: NormalizedWarehouseRequest) -> bool:
        haystack = " ".join(
            [
                request.raw_user_command or "",
                *[operation.operation_id for operation in request.operations],
                *[operation.raw_reference or "" for operation in request.operations],
                *request.constraints.excluded_robot_ids,
                *request.constraints.excluded_robot_references,
                *request.constraints.excluded_robot_statuses,
                *request.constraints.soft_avoid_edge_ids,
                *request.constraints.soft_avoid_edge_references,
                *request.constraints.hard_block_edge_ids,
                *request.constraints.hard_block_edge_references,
                *[value.edge_id for value in request.constraints.conditional_edge_policies],
            ]
        ).casefold()
        return str(value).casefold() in haystack


class ObservationIndex:
    """Convenience index over accumulated Tool observations."""

    def __init__(self, observations: Iterable[RetrievalObservation]) -> None:
        self.observations = list(observations)

    def by_tool(self, tool_name: str) -> list[RetrievalObservation]:
        return [value for value in self.observations if value.tool_name == tool_name]

    @property
    def order_records(self) -> list[dict[str, Any]]:
        """Return deduplicated complete order facts from either order Tool.

        ``find_orders`` in this project returns the same authoritative order
        fields required by downstream retrieval.  Requiring a second
        ``get_order_facts`` call after a successful single-candidate search made
        the semantic live path needlessly incomplete and slow.
        """

        values: dict[str, dict[str, Any]] = {}
        for tool_name in ("find_orders", "get_order_facts"):
            for observation in self.by_tool(tool_name):
                for record in observation.data.get("orders", []):
                    order_id = record.get("order_id")
                    if order_id:
                        values[str(order_id)] = dict(record)
        return list(values.values())

    @property
    def order_ids(self) -> list[str]:
        values = [str(value["order_id"]) for value in self.order_records]
        for observation in self.observations:
            values.extend(str(value) for value in observation.data.get("candidate_order_ids", []))
        return list(dict.fromkeys(values))

    @property
    def inbound_records(self) -> list[dict[str, Any]]:
        """Return deduplicated authoritative inbound receipt facts."""

        values: dict[str, dict[str, Any]] = {}
        for observation in self.by_tool("get_inbound_facts"):
            for record in observation.data.get("inbound_receipts", []):
                inbound_id = record.get("inbound_id")
                if inbound_id:
                    values[str(inbound_id)] = dict(record)
        return list(values.values())

    @property
    def inbound_ids(self) -> list[str]:
        return [str(value["inbound_id"]) for value in self.inbound_records]

    @property
    def item_ids(self) -> list[str]:
        values: list[str] = []
        for observation in self.observations:
            values.extend(str(value.get("item_id")) for value in observation.data.get("orders", []) if value.get("item_id"))
            values.extend(str(value.get("item_id")) for value in observation.data.get("inbound_receipts", []) if value.get("item_id"))
            values.extend(str(value.get("item_id")) for value in observation.data.get("stocks", []) if value.get("item_id"))
        return list(dict.fromkeys(values))

    @property
    def anchor_node_ids(self) -> list[str]:
        values: list[str] = []
        for observation in self.observations:
            values.extend(str(value) for value in observation.data.get("anchor_node_ids", []))
        return list(dict.fromkeys(values))

    @property
    def relevant_edge_ids(self) -> list[str]:
        values: list[str] = []
        for observation in self.observations:
            values.extend(str(value) for value in observation.data.get("relevant_edge_ids", []))
            for summary in observation.data.get("path_summaries", []):
                values.extend(str(value) for value in summary.get("edge_ids", []))
        return list(dict.fromkeys(values))


class WarehouseReadToolExecutor:
    """Execute exactly one resolved read-only Tool call."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()

    def execute(
        self,
        *,
        request: ResolvedToolRequest,
        observations: list[RetrievalObservation],
        request_fingerprint: str,
    ) -> RetrievalObservation:
        index = ObservationIndex(observations)
        tool = request.tool_name
        data: dict[str, Any]
        canonical_ids: list[str]
        if tool == "find_orders":
            orders = self.repository.find_orders(
                order_ids=request.order_ids,
                item_ids=request.item_ids,
                item_text=request.item_text,
                statuses=request.statuses or ["pending"],
            )
            data = {"orders": orders, "candidate_order_ids": [str(value["order_id"]) for value in orders]}
            canonical_ids = list(data["candidate_order_ids"])
            summary = f"Found {len(orders)} authoritative order candidate(s)."
        elif tool == "get_order_facts":
            order_ids = request.order_ids or index.order_ids
            orders = [self.repository.get_order(value) for value in order_ids]
            orders = [value for value in orders if value is not None]
            data = {"orders": orders}
            canonical_ids = [str(value["order_id"]) for value in orders]
            summary = f"Loaded authoritative facts for {len(orders)} order(s)."
        elif tool == "get_inbound_facts":
            inbound_ids = request.inbound_ids or index.inbound_ids
            receipts = [self.repository.get_inbound_receipt(value) for value in inbound_ids]
            receipts = [value for value in receipts if value is not None]
            handoffs: list[dict[str, Any]] = []
            handoff_seen: set[str] = set()
            all_slots = self.repository.empty_putaway_slots()
            putaway_slots: list[dict[str, Any]] = []
            slot_seen: set[tuple[str, int]] = set()
            inbound_movements: list[dict[str, Any]] = []
            for receipt in receipts:
                handoff = self.repository.inbound_handoff_for_port(
                    str(receipt.get("source_port_id"))
                )
                if handoff is not None:
                    handoff_id = str(handoff.get("handoff_id"))
                    if handoff_id not in handoff_seen:
                        handoff_seen.add(handoff_id)
                        handoffs.append(handoff)
                target_rack_id = receipt.get("target_rack_id")
                target_rack_level = receipt.get("target_rack_level")
                matching = [
                    slot
                    for slot in all_slots
                    if (
                        not target_rack_id
                        or str(slot.get("rack_id")) == str(target_rack_id)
                    )
                    and (
                        target_rack_level is None
                        or int(slot.get("rack_level", 0)) == int(target_rack_level)
                    )
                ]
                for slot in matching:
                    key = (str(slot.get("rack_id")), int(slot.get("rack_level", 0)))
                    if key not in slot_seen:
                        slot_seen.add(key)
                        putaway_slots.append(slot)
                inbound_movements.append({
                    "inbound_id": str(receipt["inbound_id"]),
                    "handling_unit_id": str(receipt.get("handling_unit_id") or ""),
                    "item_id": str(receipt.get("item_id") or ""),
                    "quantity": int(receipt.get("quantity", 0)),
                    "source_port_id": str(receipt.get("source_port_id") or ""),
                    "handoff_id": str(handoff.get("handoff_id")) if handoff else None,
                    "pickup_access_node_ids": (
                        [str(value) for value in handoff.get("access_node_ids", [])]
                        if handoff else []
                    ),
                    "putaway_slots": [dict(value) for value in matching],
                    "priority": str(receipt.get("priority", "medium")),
                    "status": str(receipt.get("status", "pending")),
                })
            data = {
                "inbound_receipts": receipts,
                "inbound_handoffs": handoffs,
                "putaway_slots": putaway_slots,
                "inbound_movements": inbound_movements,
            }
            canonical_ids = [
                *[str(value["inbound_id"]) for value in receipts],
                *[str(value.get("handling_unit_id")) for value in receipts if value.get("handling_unit_id")],
                *[str(value.get("item_id")) for value in receipts if value.get("item_id")],
                *[str(value.get("source_port_id")) for value in receipts if value.get("source_port_id")],
                *[str(value.get("handoff_id")) for value in handoffs if value.get("handoff_id")],
                *[
                    str(access_node_id)
                    for value in handoffs
                    for access_node_id in value.get("access_node_ids", [])
                ],
                *[str(value.get("rack_id")) for value in putaway_slots if value.get("rack_id")],
                *[
                    str(access_node_id)
                    for value in putaway_slots
                    for access_node_id in value.get("access_node_ids", [])
                ],
            ]
            summary = (
                f"Loaded {len(receipts)} inbound receipt(s), {len(handoffs)} handoff(s), "
                f"and {len(putaway_slots)} putaway slot candidate(s)."
            )
        elif tool == "get_inventory_candidates":
            order_ids = request.order_ids or index.order_ids
            orders = [self.repository.get_order(value) for value in order_ids]
            orders = [value for value in orders if value is not None]
            item_ids = request.item_ids or [str(value["item_id"]) for value in orders]
            stocks: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item_id in item_ids:
                for stock in self.repository.item_stocks(item_id):
                    stock_id = str(stock["stock_id"])
                    if stock_id not in seen:
                        seen.add(stock_id)
                        stocks.append(stock)
            data = {"order_ids": order_ids, "item_ids": item_ids, "stocks": stocks}
            canonical_ids = [
                *order_ids,
                *[str(value["stock_id"]) for value in stocks],
                *[str(value["rack_id"]) for value in stocks],
                *[
                    str(access_node_id)
                    for value in stocks
                    for access_node_id in value.get("access_node_ids", [])
                ],
            ]
            summary = f"Loaded {len(stocks)} positive-quantity stock candidate(s)."
        elif tool == "get_robot_candidates":
            settings = get_settings()
            robots = self.repository.all_robots()
            candidates: list[str] = []
            excluded: dict[str, list[str]] = defaultdict(list)
            include_statuses = set(request.include_statuses or request.statuses or ["idle"])
            exclude_statuses = set(request.exclude_statuses)
            explicit_robot_exclusions = set(request.robot_ids)
            for robot in robots:
                reasons: list[str] = []
                robot_id = str(robot.get("robot_id"))
                status = str(robot.get("status", "")).casefold()
                if robot_id in explicit_robot_exclusions:
                    reasons.append("explicit_robot_exclusion")
                if include_statuses and status not in include_statuses:
                    reasons.append(f"status:{status or 'unknown'}")
                if status in exclude_statuses:
                    reasons.append(f"explicit_status_exclusion:{status}")
                if float(robot.get("battery_pct", 0)) < settings.robot_min_battery_pct:
                    reasons.append("low_battery")
                if reasons:
                    for reason in reasons:
                        excluded[reason].append(str(robot["robot_id"]))
                else:
                    candidates.append(str(robot["robot_id"]))
            data = {
                "robots": robots,
                "candidate_robot_ids": candidates,
                "excluded_by_reason": dict(excluded),
                "explicitly_referenced_robot_ids": request.robot_ids,
                "include_statuses": sorted(include_statuses),
                "exclude_statuses": sorted(exclude_statuses),
            }
            canonical_ids = [str(value["robot_id"]) for value in robots]
            summary = f"Loaded {len(robots)} robot runtime record(s); {len(candidates)} are baseline eligible."
        elif tool == "resolve_map_entities":
            racks = [self.repository.rack(value) for value in request.rack_ids]
            nodes = [self.repository.node(value) for value in request.node_ids]
            edges = [self.repository.edge(value) for value in request.edge_ids]
            data = {
                "racks": [value for value in racks if value is not None],
                "nodes": [value for value in nodes if value is not None],
                "edges": [value for value in edges if value is not None],
            }
            canonical_ids = [*request.rack_ids, *request.node_ids, *request.edge_ids]
            summary = (
                f"Resolved {len(data['racks'])} rack(s), {len(data['nodes'])} node(s), "
                f"and {len(data['edges'])} edge(s)."
            )
        elif tool == "get_connecting_subgraph":
            data = self._connecting_subgraph(index)
            canonical_ids = [*data["anchor_node_ids"], *data["relevant_edge_ids"]]
            summary = f"Built {len(data['path_summaries'])} directed path evidence record(s)."
        elif tool == "get_runtime_constraints":
            relevant = set(request.edge_ids or index.relevant_edge_ids)
            records = [
                value for value in self.repository.runtime_edge_records()
                if not relevant or str(value.get("edge_id")) in relevant
            ]
            reservations = [
                value for value in self.repository.existing_reservations()
                if not relevant or str(value.get("edge_id")) in relevant
            ]
            data = {"runtime_edge_records": records, "edge_reservations": reservations}
            canonical_ids = [str(value["edge_id"]) for value in records] + [str(value["edge_id"]) for value in reservations]
            summary = f"Loaded {len(records)} runtime edge state(s) and {len(reservations)} reservation(s)."
        elif tool == "get_active_operations":
            robots = [
                value for value in self.repository.all_robots()
                if value.get("status") != "idle" or value.get("load_state") == "LOADED"
            ]
            if request.robot_ids:
                robots = [value for value in robots if str(value.get("robot_id")) in request.robot_ids]
            tasks = self.repository.active_operations()
            data = {"active_robots": robots, "active_tasks": tasks}
            canonical_ids = [
                *[str(value["robot_id"]) for value in robots],
                *[str(value["task_id"]) for value in tasks],
            ]
            summary = (
                f"Loaded {len(robots)} active or loaded robot operation(s) and "
                f"{len(tasks)} Spring task(s)."
            )
        else:  # pragma: no cover
            raise ValueError(f"Unsupported retrieval tool {tool}")

        data["request_fingerprint"] = request_fingerprint
        observation_id = self._observation_id(request=request, data=data, sequence=len(observations) + 1)
        return RetrievalObservation(
            observation_id=observation_id,
            request_id=request.request_id,
            tool_name=tool,
            summary=summary,
            canonical_entity_ids=list(dict.fromkeys(canonical_ids)),
            data=data,
        )

    def _connecting_subgraph(self, index: ObservationIndex) -> dict[str, Any]:
        """Build one directed path-evidence subgraph for outbound and inbound work.

        The method consumes only completed retrieval observations plus the
        request-scoped repository snapshot.  Goods-to-person affects outbound
        operations only; inbound receipts remain direct handoff-to-putaway
        tasks in the same graph.
        """

        orders = index.order_records
        stocks = [
            value
            for obs in index.by_tool("get_inventory_candidates")
            for value in obs.data.get("stocks", [])
        ]
        inbound_movements = [
            value
            for obs in index.by_tool("get_inbound_facts")
            for value in obs.data.get("inbound_movements", [])
        ]
        robot_obs = index.by_tool("get_robot_candidates")
        robots = [value for obs in robot_obs for value in obs.data.get("robots", [])]
        candidate_robot_ids = {
            value
            for obs in robot_obs
            for value in obs.data.get("candidate_robot_ids", [])
        }
        eligible_robots = [
            value
            for value in robots
            if str(value.get("robot_id")) in candidate_robot_ids
        ]

        arcs = self.repository.adjusted_arcs(
            blocked_edge_ids=set(),
            blocked_node_ids=set(),
        )
        graph = DirectedGraphService(arcs)
        g2p_mode = bool(
            orders and get_settings().outbound_fulfillment_mode == "goods_to_person"
        )

        stock_accesses = {
            str(access_node_id)
            for stock in stocks
            if int(stock.get("quantity", stock.get("available_qty", 0))) > 0
            for access_node_id in stock.get("access_node_ids", [])
        }
        inbound_pickups = {
            str(access_node_id)
            for movement in inbound_movements
            for access_node_id in movement.get("pickup_access_node_ids", [])
        }
        inbound_deliveries = {
            str(access_node_id)
            for movement in inbound_movements
            for slot in movement.get("putaway_slots", [])
            for access_node_id in slot.get("access_node_ids", [])
        }
        anchors = {
            *[str(value["current_node"]) for value in robots],
            *stock_accesses,
            *inbound_pickups,
            *inbound_deliveries,
        }
        summaries: list[dict[str, Any]] = []
        relevant_edges: list[str] = []

        def append_path(*, purpose: str, source: str, target: str, **metadata: Any) -> None:
            value, path = graph.shortest_path(source, target, metric="travel_time")
            if not path and source != target:
                return
            edge_ids = [arc.edge_id for arc in path]
            relevant_edges.extend(edge_ids)
            summaries.append({
                "purpose": purpose,
                "source": source,
                "target": target,
                "edge_ids": edge_ids,
                "travel_time_ms": int(value),
                **metadata,
            })

        positive_stocks = [
            stock
            for stock in stocks
            if int(stock.get("quantity", stock.get("available_qty", 0))) > 0
        ]

        # Outbound robot-to-stock evidence.
        for stock in positive_stocks:
            for access_node_id in [str(value) for value in stock.get("access_node_ids", [])]:
                for robot in eligible_robots:
                    append_path(
                        purpose="ROBOT_TO_PICKUP",
                        source=str(robot["current_node"]),
                        target=access_node_id,
                        operation_type="OUTBOUND_ORDER",
                        robot_id=str(robot["robot_id"]),
                        stock_id=str(stock["stock_id"]),
                        rack_id=str(stock["rack_id"]),
                        access_node_id=access_node_id,
                    )

        logical_destinations: list[str] = []
        station_ids: list[str] = []
        if orders and g2p_mode:
            logical_destinations = sorted({str(value["delivery_node"]) for value in orders})
            stations = self.repository.outbound_station_candidates(logical_destinations)
            empty_buffers = self.repository.empty_tote_buffer_candidates()
            station_accesses = [
                (
                    str(station["station_id"]),
                    str(access_node_id),
                    list(station.get("served_chute_ids", [])),
                )
                for station in stations
                for access_node_id in station.get("access_node_ids", [])
            ]
            empty_accesses = [
                (str(buffer["buffer_id"]), str(access_node_id))
                for buffer in empty_buffers
                for access_node_id in buffer.get("access_node_ids", [])
            ]
            station_ids = [str(value["station_id"]) for value in stations]
            anchors.update(access for _, access, _ in station_accesses)
            anchors.update(access for _, access in empty_accesses)

            for stock in positive_stocks:
                for source_access in [str(value) for value in stock.get("access_node_ids", [])]:
                    for station_id, station_access, served_destinations in station_accesses:
                        append_path(
                            purpose="PICKUP_TO_STATION",
                            source=source_access,
                            target=station_access,
                            operation_type="OUTBOUND_ORDER",
                            stock_id=str(stock["stock_id"]),
                            rack_id=str(stock["rack_id"]),
                            access_node_id=source_access,
                            station_id=station_id,
                            station_access_node=station_access,
                            served_logical_destination_ids=served_destinations,
                        )
                        append_path(
                            purpose="STATION_TO_POST_MOVE",
                            source=station_access,
                            target=source_access,
                            operation_type="OUTBOUND_ORDER",
                            stock_id=str(stock["stock_id"]),
                            rack_id=str(stock["rack_id"]),
                            station_id=station_id,
                            station_access_node=station_access,
                            post_move_kind="RETURN_TO_SOURCE",
                        )

            seen_empty_paths: set[tuple[str, str, str]] = set()
            for station_id, station_access, _ in station_accesses:
                for buffer_id, buffer_access in empty_accesses:
                    key = (station_access, buffer_access, buffer_id)
                    if key in seen_empty_paths:
                        continue
                    seen_empty_paths.add(key)
                    append_path(
                        purpose="STATION_TO_POST_MOVE",
                        source=station_access,
                        target=buffer_access,
                        operation_type="OUTBOUND_ORDER",
                        station_id=station_id,
                        station_access_node=station_access,
                        empty_tote_buffer_id=buffer_id,
                        post_move_kind="MOVE_TO_EMPTY_TOTE_BUFFER",
                    )
        elif orders:
            stocks_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for stock in stocks:
                stocks_by_item[str(stock["item_id"])].append(stock)
            anchors.update(str(value["delivery_node"]) for value in orders)
            for order in orders:
                for stock in stocks_by_item.get(str(order["item_id"]), []):
                    if int(stock.get("quantity", stock.get("available_qty", 0))) < int(order["required_qty"]):
                        continue
                    for access_node_id in [str(value) for value in stock.get("access_node_ids", [])]:
                        append_path(
                            purpose="PICKUP_TO_DELIVERY",
                            source=access_node_id,
                            target=str(order["delivery_node"]),
                            operation_type="OUTBOUND_ORDER",
                            order_id=str(order["order_id"]),
                            stock_id=str(stock["stock_id"]),
                            rack_id=str(stock["rack_id"]),
                            access_node_id=access_node_id,
                        )

        # Inbound work always remains a direct task, even when outbound uses G2P.
        for movement in inbound_movements:
            inbound_id = str(movement.get("inbound_id"))
            handling_unit_id = str(movement.get("handling_unit_id") or "")
            pickup_accesses = [
                str(value) for value in movement.get("pickup_access_node_ids", [])
            ]
            putaway_slots = list(movement.get("putaway_slots", []))
            for pickup_access in pickup_accesses:
                for robot in eligible_robots:
                    append_path(
                        purpose="ROBOT_TO_PICKUP",
                        source=str(robot["current_node"]),
                        target=pickup_access,
                        operation_type="INBOUND_ITEM",
                        inbound_id=inbound_id,
                        handling_unit_id=handling_unit_id,
                        robot_id=str(robot["robot_id"]),
                        handoff_id=movement.get("handoff_id"),
                        access_node_id=pickup_access,
                    )
                for slot in putaway_slots:
                    for delivery_access in [
                        str(value) for value in slot.get("access_node_ids", [])
                    ]:
                        append_path(
                            purpose="PICKUP_TO_DELIVERY",
                            source=pickup_access,
                            target=delivery_access,
                            operation_type="INBOUND_ITEM",
                            inbound_id=inbound_id,
                            handling_unit_id=handling_unit_id,
                            handoff_id=movement.get("handoff_id"),
                            rack_id=str(slot.get("rack_id")),
                            rack_level=int(slot.get("rack_level", 0)),
                            pickup_access_node=pickup_access,
                            delivery_access_node=delivery_access,
                        )

        if orders and inbound_movements:
            fulfillment_mode = "mixed_operations"
        elif inbound_movements:
            fulfillment_mode = "inbound_putaway"
        elif g2p_mode:
            fulfillment_mode = "goods_to_person"
        else:
            fulfillment_mode = "legacy_order_tasks"

        return {
            "fulfillment_mode": fulfillment_mode,
            "logical_destination_ids": logical_destinations,
            "station_ids": station_ids,
            "inbound_ids": [
                str(value.get("inbound_id")) for value in inbound_movements
            ],
            "anchor_node_ids": sorted(anchors),
            "path_summaries": summaries,
            "relevant_edge_ids": list(dict.fromkeys(relevant_edges)),
        }

    @staticmethod
    def _observation_id(*, request: ResolvedToolRequest, data: dict[str, Any], sequence: int) -> str:
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
        return f"OBS-STEP-{sequence:02d}-{request.tool_name}-{digest}"


class StepwiseRetrievalSufficiencyValidator:
    """Evaluate accumulated observations and recommend the next Tool domain."""

    def validate(
        self,
        *,
        request: NormalizedWarehouseRequest,
        observations: list[RetrievalObservation],
    ) -> RetrievalContextSufficiencyResult:
        index = ObservationIndex(observations)
        tools = {value.tool_name for value in observations}
        issues: list[WorkflowValidationIssue] = []
        missing_domains: list[str] = []
        recommended: list[RetrievalToolName] = []
        ambiguous: list[str] = []
        not_found: list[str] = []

        outbound = [value for value in request.operations if value.operation_type == "OUTBOUND_ORDER"]
        if outbound:
            search_obs = index.by_tool("find_orders")
            for observation in search_obs:
                candidates = list(observation.data.get("candidate_order_ids", []))
                unresolved_operations = [
                    value for value in outbound if get_repository().get_order(value.operation_id) is None
                ]
                if unresolved_operations and len(candidates) > 1:
                    ambiguous.extend([value.raw_reference or value.operation_id for value in unresolved_operations])
                elif unresolved_operations and not candidates:
                    not_found.extend([value.raw_reference or value.operation_id for value in unresolved_operations])

            order_records = index.order_records
            required_order_fields = {
                "order_id",
                "item_id",
                "required_qty",
                "delivery_node",
                "priority",
                "status",
            }
            has_complete_order_facts = bool(order_records) and all(
                required_order_fields.issubset(record)
                for record in order_records
            )

            requirements: list[tuple[str, str, RetrievalToolName]] = [
                ("get_inventory_candidates", "inventory", "get_inventory_candidates"),
                ("get_robot_candidates", "robot_runtime", "get_robot_candidates"),
                ("get_connecting_subgraph", "map_graph", "get_connecting_subgraph"),
                ("get_runtime_constraints", "map_graph", "get_runtime_constraints"),
            ]
            if not has_complete_order_facts:
                requirements.insert(0, ("get_order_facts", "inventory", "get_order_facts"))
            if any(get_repository().get_order(value.operation_id) is None for value in outbound) and "find_orders" not in tools:
                requirements.insert(0, ("find_orders", "inventory", "find_orders"))

            explicit_map_ids = (
                set(request.constraints.soft_avoid_edge_ids)
                | set(request.constraints.hard_block_edge_ids)
                | {value.edge_id for value in request.constraints.conditional_edge_policies}
            )
            semantic_map_references = set(request.constraints.soft_avoid_edge_references) | set(
                request.constraints.hard_block_edge_references
            )
            if (explicit_map_ids or semantic_map_references) and "resolve_map_entities" not in tools:
                requirements.insert(-2, ("resolve_map_entities", "map_graph", "resolve_map_entities"))

            for tool_name, domain, recommendation in requirements:
                if tool_name == "get_order_facts" and has_complete_order_facts:
                    continue
                if tool_name not in tools:
                    missing_domains.append(domain)
                    recommended.append(recommendation)

            order_obs = [
                *index.by_tool("find_orders"),
                *index.by_tool("get_order_facts"),
            ]
            if order_obs and not index.order_records:
                not_found.extend([value.raw_reference or value.operation_id for value in outbound])
            inventory_obs = index.by_tool("get_inventory_candidates")
            if inventory_obs and not any(obs.data.get("stocks") for obs in inventory_obs):
                issues.append(WorkflowValidationIssue(
                    code="NO_INVENTORY_CANDIDATES",
                    node_name="retrieval_context_sufficiency_guard",
                    message="No positive-quantity stock candidates were found.",
                    requires_human_review=True,
                ))
            robot_obs = index.by_tool("get_robot_candidates")
            if robot_obs and not any(obs.data.get("candidate_robot_ids") for obs in robot_obs):
                issues.append(WorkflowValidationIssue(
                    code="NO_ELIGIBLE_ROBOTS",
                    node_name="retrieval_context_sufficiency_guard",
                    message="No baseline-eligible robot is available.",
                    requires_human_review=True,
                ))
            if robot_obs and request.constraints.excluded_robot_ids:
                observed_robot_ids = {
                    str(robot.get("robot_id"))
                    for obs in robot_obs
                    for robot in obs.data.get("robots", [])
                    if robot.get("robot_id")
                }
                for robot_id in request.constraints.excluded_robot_ids:
                    if robot_id not in observed_robot_ids:
                        not_found.append(robot_id)

            if explicit_map_ids and "resolve_map_entities" in tools:
                resolved_map_ids = {
                    entity_id
                    for obs in index.by_tool("resolve_map_entities")
                    for entity_id in obs.canonical_entity_ids
                }
                for edge_id in sorted(explicit_map_ids - resolved_map_ids):
                    not_found.append(edge_id)

            subgraph_obs = index.by_tool("get_connecting_subgraph")
            if subgraph_obs:
                path_summaries = [
                    value
                    for obs in subgraph_obs
                    for value in obs.data.get("path_summaries", [])
                ]
                if get_settings().outbound_fulfillment_mode == "goods_to_person":
                    observed_purposes = {
                        str(value.get("purpose")) for value in path_summaries
                    }
                    required_purposes = {
                        "ROBOT_TO_PICKUP",
                        "PICKUP_TO_STATION",
                        "STATION_TO_POST_MOVE",
                    }
                    missing_purposes = sorted(required_purposes - observed_purposes)
                    if missing_purposes:
                        issues.append(WorkflowValidationIssue(
                            code="NO_REACHABLE_G2P_PATH",
                            node_name="retrieval_context_sufficiency_guard",
                            message=(
                                "The G2P physical path evidence is incomplete: "
                                + ", ".join(missing_purposes)
                            ),
                            requires_human_review=False,
                            repair_target="SITUATION_GRAPH",
                        ))
                elif not path_summaries:
                    issues.append(WorkflowValidationIssue(
                        code="NO_REACHABLE_TASK_PATH",
                        node_name="retrieval_context_sufficiency_guard",
                        message="No directed robot-to-pickup or pickup-to-delivery path was found.",
                        requires_human_review=False,
                        repair_target="SITUATION_GRAPH",
                    ))

        inbound = [
            value for value in request.operations
            if value.operation_type == "INBOUND_ITEM"
        ]
        if inbound:
            requirements: list[tuple[str, str, RetrievalToolName]] = [
                ("get_inbound_facts", "inventory", "get_inbound_facts"),
                ("get_robot_candidates", "robot_runtime", "get_robot_candidates"),
                ("get_connecting_subgraph", "map_graph", "get_connecting_subgraph"),
                ("get_runtime_constraints", "map_graph", "get_runtime_constraints"),
            ]
            for tool_name, domain, recommendation in requirements:
                if tool_name not in tools:
                    missing_domains.append(domain)
                    recommended.append(recommendation)

            receipt_by_id = {
                str(value.get("inbound_id")): value
                for value in index.inbound_records
                if value.get("inbound_id")
            }
            for operation in inbound:
                if operation.operation_id not in receipt_by_id and "get_inbound_facts" in tools:
                    not_found.append(operation.raw_reference or operation.operation_id)

            inbound_observations = index.by_tool("get_inbound_facts")
            if inbound_observations:
                movements = [
                    value
                    for observation in inbound_observations
                    for value in observation.data.get("inbound_movements", [])
                ]
                movement_by_id = {
                    str(value.get("inbound_id")): value
                    for value in movements
                    if value.get("inbound_id")
                }
                for operation in inbound:
                    movement = movement_by_id.get(operation.operation_id)
                    if movement is None:
                        continue
                    if not movement.get("pickup_access_node_ids"):
                        issues.append(WorkflowValidationIssue(
                            code="INBOUND_HANDOFF_ACCESS_MISSING",
                            node_name="retrieval_context_sufficiency_guard",
                            message=(
                                f"Inbound {operation.operation_id} has no robot-accessible handoff node."
                            ),
                            entity_ids=[operation.operation_id],
                            repair_target="SITUATION_GRAPH",
                        ))
                    if not movement.get("putaway_slots"):
                        issues.append(WorkflowValidationIssue(
                            code="INBOUND_PUTAWAY_SLOT_MISSING",
                            node_name="retrieval_context_sufficiency_guard",
                            message=(
                                f"Inbound {operation.operation_id} has no eligible putaway slot."
                            ),
                            entity_ids=[operation.operation_id],
                            requires_human_review=True,
                        ))

            subgraph_obs = index.by_tool("get_connecting_subgraph")
            if subgraph_obs:
                path_summaries = [
                    value
                    for observation in subgraph_obs
                    for value in observation.data.get("path_summaries", [])
                ]
                for operation in inbound:
                    has_robot_to_pickup = any(
                        value.get("purpose") == "ROBOT_TO_PICKUP"
                        and value.get("operation_type") == "INBOUND_ITEM"
                        and str(value.get("inbound_id")) == operation.operation_id
                        for value in path_summaries
                    )
                    has_pickup_to_delivery = any(
                        value.get("purpose") == "PICKUP_TO_DELIVERY"
                        and value.get("operation_type") == "INBOUND_ITEM"
                        and str(value.get("inbound_id")) == operation.operation_id
                        for value in path_summaries
                    )
                    if not (has_robot_to_pickup and has_pickup_to_delivery):
                        issues.append(WorkflowValidationIssue(
                            code="NO_REACHABLE_INBOUND_PATH",
                            node_name="retrieval_context_sufficiency_guard",
                            message=(
                                f"Inbound {operation.operation_id} lacks complete robot-to-handoff "
                                "or handoff-to-putaway path evidence."
                            ),
                            entity_ids=[operation.operation_id],
                            repair_target="SITUATION_GRAPH",
                        ))

        recovery = [value for value in request.operations if value.operation_type == "RECOVERY"]
        if recovery and "get_active_operations" not in tools:
            missing_domains.append("active_operations")
            recommended.append("get_active_operations")

        if ambiguous:
            for value in ambiguous:
                issues.append(WorkflowValidationIssue(
                    code="ENTITY_REFERENCE_AMBIGUOUS",
                    node_name="retrieval_context_sufficiency_guard",
                    message=f"Multiple authoritative entities matched {value!r}.",
                    entity_ids=[value],
                    requires_user_clarification=True,
                ))
        if not_found:
            for value in not_found:
                issues.append(WorkflowValidationIssue(
                    code="ENTITY_REFERENCE_NOT_FOUND",
                    node_name="retrieval_context_sufficiency_guard",
                    message=f"No authoritative entity matched {value!r}.",
                    entity_ids=[value],
                    requires_user_clarification=True,
                ))

        hard_review = any(value.requires_human_review for value in issues)
        blocking_issue = bool(issues)
        ready = (
            not missing_domains
            and not ambiguous
            and not not_found
            and not blocking_issue
        )
        repair_target = "NONE"
        if missing_domains:
            repair_target = "RETRIEVAL_AGENT"
        elif any(value.repair_target == "SITUATION_GRAPH" for value in issues):
            repair_target = "SITUATION_GRAPH"
        return RetrievalContextSufficiencyResult(
            ready=ready,
            missing_domains=list(dict.fromkeys(missing_domains)),
            ambiguous_references=list(dict.fromkeys(ambiguous)),
            not_found_references=list(dict.fromkeys(not_found)),
            recommended_next_tools=list(dict.fromkeys(recommended)),
            retryable=(bool(missing_domains) or repair_target == "SITUATION_GRAPH") and not hard_review,
            repair_target=repair_target,
            errors=issues,
        )


class ObservationContextMaterializer:
    """Materialize typed contexts from actual Tool observations without re-querying data."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()

    def materialize(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        observations: list[RetrievalObservation],
        entity_resolutions: list[EntityResolutionResult] | None = None,
    ) -> tuple[NormalizedWarehouseRequest, InventoryContext, RobotRuntimeContext, MapContext, list[str], dict[str, str], list[dict[str, Any]]]:
        index = ObservationIndex(observations)
        orders = index.order_records
        inbound_receipts = index.inbound_records
        inbound_movements = [
            value
            for obs in index.by_tool("get_inbound_facts")
            for value in obs.data.get("inbound_movements", [])
        ]
        stocks = [value for obs in index.by_tool("get_inventory_candidates") for value in obs.data.get("stocks", [])]
        robot_observations = index.by_tool("get_robot_candidates")
        robots = [value for obs in robot_observations for value in obs.data.get("robots", [])]
        candidate_robot_ids = list(dict.fromkeys(
            value for obs in robot_observations for value in obs.data.get("candidate_robot_ids", [])
        ))
        excluded_by_reason: dict[str, list[str]] = defaultdict(list)
        for obs in robot_observations:
            for reason, ids in obs.data.get("excluded_by_reason", {}).items():
                excluded_by_reason[reason].extend(str(value) for value in ids)

        # Canonicalize natural-language references only from deterministic resolver results.
        canonical_orders = [str(value["order_id"]) for value in orders]
        operations: list[NormalizedOperation] = []
        for operation in normalized_request.operations:
            if operation.operation_type == "OUTBOUND_ORDER" and self.repository.get_order(operation.operation_id) is None:
                if len(canonical_orders) == 1:
                    operation = operation.model_copy(update={"operation_id": canonical_orders[0]})
            operations.append(operation)

        resolution_by_text: dict[str, list[str]] = defaultdict(list)
        for resolution in entity_resolutions or []:
            if resolution.status != "RESOLVED":
                continue
            resolution_by_text[resolution.raw_text].extend(resolution.resolved_entity_ids)

        def canonical_edges(exact_ids: list[str], references: list[str]) -> list[str]:
            values = [edge_id for edge_id in exact_ids if self.repository.edge(edge_id) is not None]
            for reference in references:
                values.extend(
                    entity_id
                    for entity_id in resolution_by_text.get(reference, [])
                    if self.repository.edge(entity_id) is not None
                )
            return list(dict.fromkeys(values))

        def canonical_robots(exact_ids: list[str], references: list[str]) -> list[str]:
            values = [robot_id for robot_id in exact_ids if robot_id in self.repository.robots]
            for reference in references:
                values.extend(
                    entity_id
                    for entity_id in resolution_by_text.get(reference, [])
                    if entity_id in self.repository.robots
                )
            return list(dict.fromkeys(values))

        canonical_constraints = normalized_request.constraints.model_copy(
            update={
                "excluded_robot_ids": canonical_robots(
                    normalized_request.constraints.excluded_robot_ids,
                    normalized_request.constraints.excluded_robot_references,
                ),
                "excluded_robot_references": [],
                "soft_avoid_edge_ids": canonical_edges(
                    normalized_request.constraints.soft_avoid_edge_ids,
                    normalized_request.constraints.soft_avoid_edge_references,
                ),
                "soft_avoid_edge_references": [],
                "hard_block_edge_ids": canonical_edges(
                    normalized_request.constraints.hard_block_edge_ids,
                    normalized_request.constraints.hard_block_edge_references,
                ),
                "hard_block_edge_references": [],
            }
        )
        normalized = normalized_request.model_copy(
            update={"operations": operations, "constraints": canonical_constraints}
        )

        task_needs = [
            InventoryTaskNeed(
                order_id=str(value["order_id"]),
                item_id=str(value["item_id"]),
                required_qty=int(value["required_qty"]),
                delivery_node=str(value["delivery_node"]),
                priority=str(value.get("priority", "medium")),
                order_status=str(value.get("status", "pending")),
            )
            for value in orders
        ]
        candidate_stocks = [
            CandidateStock(
                stock_id=str(value["stock_id"]),
                item_id=str(value["item_id"]),
                item_name=str(value.get("item_name", value["item_id"])),
                rack_id=str(value["rack_id"]),
                rack_level=int(value["rack_level"]),
                access_node_ids=[str(item) for item in value.get("access_node_ids", [])],
                available_qty=int(value.get("quantity", value.get("available_qty", 0))),
                unit=str(value.get("unit", "EA")),
            )
            for value in stocks
        ]
        inbound_needs = [
            InboundTaskNeed(
                inbound_id=str(value["inbound_id"]),
                handling_unit_id=str(value["handling_unit_id"]),
                item_id=str(value["item_id"]),
                quantity=int(value["quantity"]),
                source_port_id=str(value["source_port_id"]),
                priority=str(value.get("priority", "medium")),
                target_rack_id=(
                    str(value["target_rack_id"])
                    if value.get("target_rack_id") is not None
                    else None
                ),
                target_rack_level=(
                    int(value["target_rack_level"])
                    if value.get("target_rack_level") is not None
                    else None
                ),
                status=str(value.get("status", "arrived")),
            )
            for value in inbound_receipts
        ]
        putaway_values: dict[tuple[str, int], dict[str, Any]] = {}
        for movement in inbound_movements:
            for slot in movement.get("putaway_slots", []):
                access_node_ids = [
                    str(value) for value in slot.get("access_node_ids", []) if value
                ]
                if not access_node_ids:
                    continue
                key = (str(slot["rack_id"]), int(slot["rack_level"]))
                putaway_values[key] = dict(slot)
        candidate_putaway_slots = [
            CandidatePutawaySlot(
                rack_id=rack_id,
                rack_level=rack_level,
                access_node_ids=[str(value) for value in value.get("access_node_ids", [])],
                capacity=int(value.get("capacity", 0)),
            )
            for (rack_id, rack_level), value in sorted(putaway_values.items())
        ]
        if task_needs and inbound_needs:
            inventory_mode = "mixed_operations"
        elif inbound_needs:
            inventory_mode = "inbound_putaway"
        elif task_needs:
            inventory_mode = "order_fulfillment"
        else:
            inventory_mode = "warehouse_overview"

        inventory = InventoryContext(
            query_scope=InventoryQueryScope(
                mode=inventory_mode,
                warehouse_id=self.repository.warehouse_id,
                order_ids=canonical_orders,
                inbound_ids=[str(value["inbound_id"]) for value in inbound_receipts],
                item_ids=list(dict.fromkeys([
                    *[value.item_id for value in task_needs],
                    *[value.item_id for value in inbound_needs],
                ])),
                reason="Materialized from authoritative Agent Tool observations.",
            ),
            inventory_summary=(
                f"Materialized {len(task_needs)} outbound order(s), "
                f"{len(inbound_needs)} inbound receipt(s), "
                f"{len(candidate_stocks)} stock candidate(s), and "
                f"{len(candidate_putaway_slots)} putaway slot candidate(s)."
            ),
            task_needs=task_needs,
            inbound_needs=inbound_needs,
            candidate_putaway_slots=candidate_putaway_slots,
            candidate_stocks=candidate_stocks,
        )

        settings = get_settings()
        robot_models = [
            RobotRuntime.model_validate(
                {
                    **value,
                    "warehouse_id": self.repository.warehouse_id,
                    "simulation_id": self.repository.simulation_id,
                }
            )
            for value in robots
        ]
        robot_context = RobotRuntimeContext(
            warehouse_id=self.repository.warehouse_id,
            simulation_id=self.repository.simulation_id,
            robots=robot_models,
            candidate_robot_ids=candidate_robot_ids,
            excluded_by_reason={key: list(dict.fromkeys(values)) for key, values in excluded_by_reason.items()},
            min_battery_pct=settings.robot_min_battery_pct,
            min_capacity_units=1,
            summary=f"Materialized {len(robot_models)} robot(s); {len(candidate_robot_ids)} baseline candidate(s).",
        )

        runtime_records = [
            value for obs in index.by_tool("get_runtime_constraints")
            for value in obs.data.get("runtime_edge_records", [])
        ]
        reservation_records = [
            value for obs in index.by_tool("get_runtime_constraints")
            for value in obs.data.get("edge_reservations", [])
        ]
        penalties: list[EdgePenalty] = []
        occupancies: list[EdgeOccupancy] = []
        blocked_edges: list[str] = []
        for runtime in runtime_records:
            status = runtime.get("status")
            if status == "congested":
                penalties.append(EdgePenalty(
                    edge_id=str(runtime["edge_id"]),
                    cost_multiplier=float(runtime.get("cost_multiplier", 1.0)),
                    travel_time_multiplier=float(runtime.get("travel_time_multiplier", 1.0)),
                    reason=str(runtime.get("reason", "Runtime congestion.")),
                ))
            elif status == "occupied":
                occupancies.append(EdgeOccupancy(
                    edge_id=str(runtime["edge_id"]),
                    robot_id=str(runtime["occupying_robot_id"]),
                    direction=str(runtime.get("direction", "UNKNOWN")),
                    occupied_from_ms=int(runtime.get("occupied_from_ms", 0)),
                    occupied_until_ms=int(runtime["occupied_until_ms"]),
                    capacity=int(runtime.get("capacity", 1)),
                    reason=str(runtime.get("reason", "Runtime occupancy.")),
                ))
            elif status == "blocked":
                blocked_edges.append(str(runtime["edge_id"]))
        reservations = [EdgeReservation.model_validate(value) for value in reservation_records]
        constraints = MapConstraints(
            blocked_edge_ids=list(dict.fromkeys(blocked_edges)),
            edge_penalties=penalties,
            edge_occupancies=occupancies,
            edge_reservations=reservations,
        )
        path_observations = index.by_tool("get_connecting_subgraph")
        relevant_ids = {
            *[str(value) for obs in path_observations for value in obs.data.get("anchor_node_ids", [])],
            *[
                str(access_node_id)
                for value in stocks
                for access_node_id in value.get("access_node_ids", [])
            ],
            *[str(value["delivery_node"]) for value in orders],
            *[
                str(access_node_id)
                for movement in inbound_movements
                for access_node_id in movement.get("pickup_access_node_ids", [])
            ],
            *[
                str(access_node_id)
                for movement in inbound_movements
                for slot in movement.get("putaway_slots", [])
                for access_node_id in slot.get("access_node_ids", [])
            ],
            *[str(value["current_node"]) for value in robots],
        }
        for edge_id in [
            *constraints.blocked_edge_ids,
            *[value.edge_id for value in constraints.edge_penalties],
            *[value.edge_id for value in constraints.edge_occupancies],
            *[value.edge_id for value in constraints.edge_reservations],
        ]:
            edge = self.repository.edge(edge_id)
            if edge:
                relevant_ids.update([str(edge["source"]), str(edge["target"])])
        relevant_nodes = [
            RelevantMapNode(
                node_id=node_id,
                node_type=str(self.repository.nodes[node_id]["type"]),
                x=float(self.repository.nodes[node_id]["x"]),
                y=float(self.repository.nodes[node_id]["y"]),
            )
            for node_id in sorted(relevant_ids)
            if node_id in self.repository.nodes
        ]
        map_context = MapContext(
            warehouse_id=self.repository.warehouse_id,
            graph_version=self.repository.versions["graph_version"],
            node_count=len(self.repository.nodes),
            edge_count=len(self.repository.edges),
            relevant_nodes=relevant_nodes,
            map_constraints=constraints,
            summary=(
                f"Materialized graph with {len(self.repository.nodes)} nodes and {len(self.repository.edges)} edges; "
                f"{len(penalties)} penalty, {len(occupancies)} occupancy, and {len(blocked_edges)} blocked edge record(s)."
            ),
        )
        penalty_map = {value.edge_id: (value.cost_multiplier, value.travel_time_multiplier) for value in penalties}
        arcs = self.repository.adjusted_arcs(
            blocked_edge_ids=set(blocked_edges),
            blocked_node_ids=set(),
            edge_penalties=penalty_map,
        )
        return (
            normalized,
            inventory,
            robot_context,
            map_context,
            list(self.repository.nodes),
            {node_id: str(value["type"]) for node_id, value in self.repository.nodes.items()},
            arcs,
        )

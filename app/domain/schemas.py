"""Strict domain contracts for the warehouse orchestration workflow.

The contracts deliberately separate LLM interpretation, deterministic workflow
routing, warehouse snapshots, policy materialization, optimization, route
validation, and traffic scheduling.  Unknown fields are rejected so the final
API result is also a machine-checkable execution report.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)


RequestMode = Literal["event_driven", "human_command", "mixed"]

WAREHOUSE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")


def normalize_warehouse_id(value: object) -> str:
    """Normalize and validate a public warehouse identifier.

    The identifier is a tenant/facility key, never a database URI or Cypher
    fragment.  Backends use it only as a bound parameter / namespace.
    """

    text = str(value or "WH-001").strip().upper()
    if not WAREHOUSE_ID_PATTERN.fullmatch(text):
        raise ValueError(
            "warehouse_id must be 2-64 characters using letters, digits, '_' or '-'."
        )
    return text


WarehouseId = Annotated[str, BeforeValidator(normalize_warehouse_id)]


def infer_request_mode(*, events: list[object], user_command: str | None) -> RequestMode:
    """Infer the internal request mode from the actual public payload shape."""

    has_events = bool(events)
    has_command = bool((user_command or "").strip())
    if has_events and has_command:
        return "mixed"
    if has_events:
        return "event_driven"
    if has_command:
        return "human_command"
    raise ValueError("At least one event or user_command is required.")
ReplanReason = Literal[
    "NEW_ORDER",
    "URGENT_ORDER",
    "ROBOT_FAULT",
    "EDGE_BLOCKED",
    "POLICY_CHANGE",
    "LOW_BATTERY",
]
TerminalPolicy = Literal["STAY", "PARK", "CHARGE"]
HandoverPolicy = Literal[
    "CURRENT_NODE",
    "NEXT_NODE",
    "CURRENT_SERVICE_END",
    "CURRENT_OPERATION_END",
]
PlanningMode = Literal["llm_router", "force_agent", "force_rule"]
PlanningModeSource = Literal["environment", "request_override"]
AgentRetrievalMode = Literal["parallel_plan", "stepwise"]
HITLExecutionMode = Literal["terminal"]
HumanInteractionStage = Literal["PRE_ROUTE", "IN_ROUTE", "PRE_OPTIMIZATION"]
HumanInteractionResumeRoute = Literal["RULE_FORMULATION", "AGENT_FORMULATION", "INCIDENT_RESPONSE"]
RequestFinalRoute = Literal["RULE_FORMULATION", "AGENT_FORMULATION", "INCIDENT_RESPONSE"]
HumanInteractionKind = Literal["CLARIFICATION", "APPROVAL"]
HumanInteractionAction = Literal["SELECT", "APPROVE", "REJECT", "CANCEL"]
HumanInteractionStatus = Literal["PENDING", "RESOLVED", "REJECTED", "CANCELLED"]
RequestGateAction = Literal[
    "ROUTE_RULE",
    "ROUTE_AGENT",
    "HANDLE_INCIDENT",
    "REJECT_INPUT",
    "ASK_CLARIFICATION",
    "REQUIRE_HUMAN_APPROVAL",
    "HOLD_WORKFLOW",
]
OptimizationBackend = Literal["ortools", "cuopt", "cuopt_payload_only"]
OutboundFulfillmentMode = Literal["goods_to_person", "legacy_order_tasks"]
OptimizerResultBackend = Literal["rule", "ortools", "cuopt"]
ObjectiveProfile = Literal[
    "MIN_TOTAL_COST",
    "MIN_COMPLETION_TIME",
    "URGENT_FIRST",
    "THROUGHPUT",
    "MIN_REHANDLE",
    "BALANCED",
]
ObjectiveTerm = Literal[
    "MIN_COMPLETION_TIME",
    "MIN_BATTERY_RISK",
    "MIN_TRAVEL_DISTANCE",
    "MAX_THROUGHPUT",
]
PlanningRouteRecommendation = Literal["RULE", "GLOBAL_SOLVER"]
FormulationRoute = Literal[
    "RULE_FORMULATION",
    "AGENT_FORMULATION",
    "INCIDENT_RESPONSE",
    "ASK_CLARIFICATION",
    "HUMAN_REVIEW",
]
EntryRoute = Literal["NORMAL_FORMULATION", "QUERY_ONLY", "NO_ACTION", "PREBUILT_MISSION_PIPELINE", "EXECUTION_RECOVERY_PIPELINE", "HUMAN_REVIEW"]
NormalizationStrategy = Literal["STRUCTURED", "LLM", "LLM_ROUTER", "NONE"]
SupervisorStrategy = Literal["DETERMINISTIC", "UNIFIED_LLM", "NONE"]
OrchestrationRoute = Literal[
    "RULE_MISSION_PIPELINE",
    "AGENT_MISSION_PIPELINE",
    "INCIDENT_RESPONSE_PIPELINE",
    "QUERY_ONLY",
    "NO_ACTION",
    "PREBUILT_MISSION_PIPELINE",
    "EXECUTION_RECOVERY_PIPELINE",
    "CLARIFICATION_REQUIRED",
    "HUMAN_REVIEW",
]
ContextNodeName = Literal["inventory_context", "map_context", "robot_runtime"]
Priority = Literal["low", "medium", "high"]
TaskType = Literal["outbound_pick", "loaded_transfer"]
MissionType = Literal["order_fulfillment", "robot_recovery", "no_op"]
MissionSource = Literal["external", "rule_compiler", "llm_agent", "repair_agent", "recovery_policy"]
WorkflowStatus = Literal[
    "running",
    "plan_validated",
    "query_completed",
    "no_action",
    "clarification_required",
    "input_rejected",
    "human_review",
    "failed",
    "ready_for_cuopt",
    "awaiting_clarification",
    "awaiting_human_approval",
    "cancelled",
    "incident_handled",
    "held_for_human_action",
]
EdgeRuntimeStatus = Literal["open", "congested", "occupied", "reserved", "blocked"]
IncidentObservedEffect = Literal["TRAVERSABLE", "DEGRADED", "NOT_TRAVERSABLE", "UNKNOWN"]
IncidentScope = Literal["MAP_RESOURCE", "ROBOT", "INVENTORY", "MISSION", "UNKNOWN"]
IncidentRobotOperability = Literal["OPERABLE", "FAULTED", "UNKNOWN", "NOT_APPLICABLE"]
IncidentLoadState = Literal["EMPTY", "LOADED", "UNKNOWN", "NOT_APPLICABLE"]
IncidentHandlingMode = Literal[
    "AUTO_HANDLE",
    "AUTO_HANDLE_AND_NOTIFY_HUMAN",
    "REQUIRE_HUMAN_DECISION",
]
ImmediateSafetyAction = Literal[
    "NONE",
    "TEMPORARILY_BLOCK_RESOURCE",
    "HOLD_AFFECTED_ROBOT",
    "STOP_AFFECTED_MISSIONS",
]
OperatorNotificationType = Literal["INFO", "HUMAN_WORK_REQUIRED", "HUMAN_DECISION_REQUIRED"]


def canonicalize_planning_mode(value: object) -> PlanningMode:
    """Normalize legacy route-mode names to the v13.12 canonical contract.

    The aliases keep existing scripts compatible while the API and environment
    expose clearer names:

    * ``llm_router``: one input-level LLM call recommends Rule or Agent.
    * ``force_agent``: skip route judgment and force Agent formulation.
    * ``force_rule``: skip route judgment and force deterministic formulation.
    """

    text = str(value or "llm_router").strip().casefold().replace("-", "_")
    aliases = {
        "llm_router": "llm_router",
        "llm_first": "llm_router",
        "auto": "llm_router",
        "force_agent": "force_agent",
        "llm_agent": "force_agent",
        "force_rule": "force_rule",
        "rule_baseline": "force_rule",
    }
    if text not in aliases:
        raise ValueError(
            "planning mode must be llm_router, force_agent, or force_rule "
            "(legacy aliases: llm_first, llm_agent, rule_baseline, auto)."
        )
    return aliases[text]  # type: ignore[return-value]

RecoveryAction = Literal[
    "CONTINUE_TO_DESTINATION",
    "RETURN_TO_SOURCE",
    "EXIT_FORWARD_AND_WAIT",
    "EXIT_REVERSE_AND_WAIT",
    "DIVERT_TO_BUFFER",
    "EMERGENCY_STOP",
    "HUMAN_REVIEW",
]
HumanInteractionOptionOutcome = Literal["RESUME", "HOLD", "TERMINATE"]
HumanInteractionResumeOutcome = Literal[
    "RESUMED",
    "HELD",
    "TERMINATED",
    "PENDING_REVIEW",
    "FAILED",
]

ACTIONABLE_EVENT_TYPES = {
    "new_order",
    "inbound_item_arrived",
    "edge_congested",
    "edge_occupied",
    "edge_reserved",
    "edge_blocked",
    "robot_recovery_requested",
    "operational_incident",
}
QUERY_EVENT_TYPES = {"status_query", "explain_only"}
KNOWN_EVENT_TYPES = ACTIONABLE_EVENT_TYPES | QUERY_EVENT_TYPES


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and validates assignments."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class HumanInteractionOption(StrictModel):
    """One operator-selectable answer or approval alternative."""

    option_id: str
    label: str
    description: str = ""
    selected_entity_ids: list[str] = Field(default_factory=list)
    resolution_value: str | None = None
    impact_summary: str | None = None
    outcome: HumanInteractionOptionOutcome = "RESUME"
    resumable: bool | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def resolve_resume_contract(self) -> "HumanInteractionOption":
        expected = self.outcome == "RESUME"
        # resumable is a derived contract, not an LLM decision.  Normalize an
        # inconsistent structured response instead of failing the whole plan
        # before a useful Human Review can be shown to the operator.
        object.__setattr__(self, "resumable", expected)
        if not expected and not self.unavailable_reason:
            object.__setattr__(self, "unavailable_reason", (
                "이 선택은 현재 자동 계획을 즉시 재개하지 않습니다."
            ))
        return self


class OperationalIncidentImpact(StrictModel):
    """Generic operational incident normalized by impact, not by incident taxonomy.

    The system deliberately does not need separate BOX_SPILL, PERSON_IN_AISLE,
    FALLEN_PALLET, or FORKLIFT_INCIDENT event types.  It only needs to know the
    affected resources, observed operational effect, immediate safety action,
    and whether an operator decision is required.
    """

    incident_id: str
    description: str
    scope: IncidentScope = "UNKNOWN"
    affected_resource_ids: list[str] = Field(default_factory=list)
    affected_resource_references: list[str] = Field(default_factory=list)
    observed_effect: IncidentObservedEffect = "UNKNOWN"
    robot_operability: IncidentRobotOperability = "NOT_APPLICABLE"
    load_state: IncidentLoadState = "NOT_APPLICABLE"
    handling_mode: IncidentHandlingMode = "AUTO_HANDLE"
    immediate_safety_action: ImmediateSafetyAction = "NONE"
    physical_intervention_required: bool = False
    operator_decision_reason: str | None = None
    decision_prompt: str | None = None
    decision_options: list[HumanInteractionOption] = Field(default_factory=list)
    notification_title: str | None = None
    notification_message: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

class IncidentResponseAction(StrictModel):
    """One immediate, deterministic safety/runtime action for an incident."""

    incident_id: str
    action: ImmediateSafetyAction
    affected_resource_ids: list[str] = Field(default_factory=list)
    reason: str
    apply_before_human_response: bool = True
    execution_status: Literal["PLANNED", "APPLIED"] = "PLANNED"
    applied_immediately: bool = False


class OperatorNotification(StrictModel):
    """Front-end notification that may or may not require an operator response."""

    notification_id: str
    notification_type: OperatorNotificationType
    title: str
    message: str
    incident_id: str | None = None
    affected_resource_ids: list[str] = Field(default_factory=list)
    automatic_actions: list[str] = Field(default_factory=list)
    requires_response: bool = False
    created_at: str


class IncidentResponsePlan(StrictModel):
    """Aggregate incident handling plan created before Rule/Agent route locking."""

    incidents: list[OperationalIncidentImpact] = Field(default_factory=list)
    immediate_actions: list[IncidentResponseAction] = Field(default_factory=list)
    notifications: list[OperatorNotification] = Field(default_factory=list)
    pending_human_interaction: HumanInteractionRequest | None = None
    summary: str = ""


class HumanInteractionRequest(StrictModel):
    """Persistable HITL pause shown by the API and front end."""

    interaction_id: str
    kind: HumanInteractionKind
    stage: HumanInteractionStage
    reason_code: str
    headline: str
    prompt: str
    options: list[HumanInteractionOption] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    recommended_option_id: str | None = None
    default_action: Literal["HOLD", "REJECT", "CANCEL"] = "HOLD"
    route_locked: bool = False
    resume_route: HumanInteractionResumeRoute | None = None
    context_summary: str = ""
    created_at: str


class HumanInteractionResponse(StrictModel):
    """Operator response used to resume one paused workflow."""

    interaction_id: str
    action: HumanInteractionAction
    selected_option_id: str | None = None
    selected_entity_ids: list[str] = Field(default_factory=list)
    resolution_code: str | None = None
    resolution_value: str | None = None
    actor_id: str = "operator"
    comment: str | None = None
    responded_at: str | None = None


class HumanInteractionRecord(StrictModel):
    """File-backed checkpoint record for front-end HITL flows."""

    interaction: HumanInteractionRequest
    status: HumanInteractionStatus = "PENDING"
    original_request: dict[str, Any]
    response: HumanInteractionResponse | None = None
    result_path: str | None = None


class HumanInteractionResumeRequest(StrictModel):
    """API payload used to answer one pending HITL request."""

    action: HumanInteractionAction
    selected_option_id: str | None = None
    selected_entity_ids: list[str] = Field(default_factory=list)
    resolution_value: str | None = None
    actor_id: str = "operator"
    comment: str | None = None


class EventInput(StrictModel):
    """Normalized event entering the LangGraph workflow.

    ``type`` remains a string so an unknown upstream event can still reach
    request triage instead of failing before semantic interpretation.
    """

    type: str
    order_id: str | None = None
    inbound_id: str | None = None
    robot_id: str | None = None
    edge_id: str | None = None
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_known_event_identifiers(self) -> "EventInput":
        """Require entity identifiers for known entity-specific events."""

        if self.type == "new_order" and not self.order_id:
            raise ValueError("new_order requires order_id")
        if self.type == "inbound_item_arrived" and not (self.inbound_id or self.payload.get("inbound_id") or self.payload.get("handling_unit_id")):
            raise ValueError("inbound_item_arrived requires inbound_id")
        if self.type in {"edge_congested", "edge_occupied", "edge_reserved", "edge_blocked"} and not self.edge_id:
            raise ValueError(f"{self.type} requires edge_id")
        if self.type == "robot_recovery_requested" and not self.robot_id:
            raise ValueError("robot_recovery_requested requires robot_id")
        if self.type == "operational_incident":
            description = str(
                self.payload.get("description")
                or self.payload.get("incident_description")
                or self.payload.get("message")
                or ""
            ).strip()
            if not description and not any((self.robot_id, self.edge_id, self.node_id)):
                raise ValueError(
                    "operational_incident requires a description or an affected robot/edge/node identifier"
                )
        return self



StructuredOperationType = Literal["OUTBOUND", "INBOUND", "TRANSFER", "CHARGE", "PARK"]


class StructuredOperationInput(StrictModel):
    """Authoritative business operation supplied by the Spring BE.

    The request itself owns the business facts; LARO does not require a separate
    ``orders`` or ``handling_units`` table.  Existing BE identifiers may be
    supplied when available, while node codes keep the contract usable by the
    front-end simulator and Neo4j route projection.
    """

    operation_id: str = Field(min_length=1, max_length=128)
    operation_type: StructuredOperationType
    task_id: int | None = Field(default=None, ge=1)
    item_id: int | None = Field(default=None, ge=1)
    product_code: str | None = Field(default=None, min_length=1, max_length=64)
    quantity: PositiveInt = 1
    priority: Priority = "medium"

    source_warehouse_item_id: int | None = Field(default=None, ge=1)
    source_storage_location_id: int | None = Field(default=None, ge=1)
    source_node_id: int | None = Field(default=None, ge=1)
    source_node_code: str | None = Field(default=None, min_length=1, max_length=100)
    source_facility_code: str | None = Field(default=None, min_length=1, max_length=100)

    destination_storage_location_id: int | None = Field(default=None, ge=1)
    destination_node_id: int | None = Field(default=None, ge=1)
    destination_node_code: str | None = Field(default=None, min_length=1, max_length=100)
    destination_facility_code: str | None = Field(default=None, min_length=1, max_length=100)
    target_rack_level: int | None = Field(default=None, ge=1, le=20)

    release_at_ms: int = Field(default=0, ge=0)
    pickup_service_time_ms: int = Field(default=1200, ge=0)
    drop_service_time_ms: int = Field(default=1200, ge=0)
    attributes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "StructuredOperationInput":
        if self.operation_type in {"OUTBOUND", "INBOUND", "TRANSFER"}:
            if self.item_id is None and not self.product_code:
                raise ValueError(
                    f"{self.operation_type} requires item_id or product_code"
                )
        if self.operation_type == "OUTBOUND":
            if not any((
                self.destination_node_id,
                self.destination_node_code,
                self.destination_facility_code,
            )):
                raise ValueError(
                    "OUTBOUND requires destination_node_id, destination_node_code, "
                    "or destination_facility_code"
                )
        if self.operation_type in {"INBOUND", "TRANSFER"}:
            if not any((
                self.source_node_id,
                self.source_node_code,
                self.source_facility_code,
                self.source_storage_location_id,
            )):
                raise ValueError(
                    f"{self.operation_type} requires a source node, facility, or storage location"
                )
        if self.operation_type == "TRANSFER":
            if not any((
                self.destination_node_id,
                self.destination_node_code,
                self.destination_facility_code,
                self.destination_storage_location_id,
            )):
                raise ValueError(
                    "TRANSFER requires a destination node, facility, or storage location"
                )
        return self


class RoutingWorkloadContext(StrictModel):
    """Authoritative pre-route workload snapshot supplied by the Spring BE."""

    new_operation_count: int = Field(default=0, ge=0)
    unfinished_operation_count: int = Field(default=0, ge=0)
    eligible_robot_count: int = Field(default=0, ge=0)
    total_robot_count: int = Field(default=0, ge=0)
    low_battery_robot_count: int = Field(default=0, ge=0)
    active_robot_count: int = Field(default=0, ge=0)
    source: str = Field(default="UNKNOWN", min_length=1, max_length=128)

    @property
    def effective_operation_count(self) -> int:
        return self.new_operation_count + self.unfinished_operation_count


class StructuredMissionInput(StrictModel):
    """Request-native operation set sent by the BE for one simulation run."""

    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    operations: list[StructuredOperationInput] = Field(min_length=1)
    constraints: NormalizedRequestConstraints | None = None
    routing_context: RoutingWorkloadContext | None = None

    @model_validator(mode="after")
    def validate_unique_operation_ids(self) -> "StructuredMissionInput":
        ids = [value.operation_id for value in self.operations]
        if len(ids) != len(set(ids)):
            raise ValueError("structured_input operation_id values must be unique")
        return self

    def to_events(self) -> list[EventInput]:
        values: list[EventInput] = []
        for operation in self.operations:
            payload = operation.model_dump(mode="json", exclude_none=True)
            if operation.operation_type == "OUTBOUND":
                values.append(
                    EventInput(
                        type="new_order",
                        order_id=operation.operation_id,
                        payload=payload,
                    )
                )
            elif operation.operation_type == "INBOUND":
                values.append(
                    EventInput(
                        type="inbound_item_arrived",
                        inbound_id=operation.operation_id,
                        payload=payload,
                    )
                )
            elif operation.operation_type == "TRANSFER":
                values.append(
                    EventInput(
                        type="structured_transfer",
                        node_id=operation.source_node_code,
                        payload=payload,
                    )
                )
            elif operation.operation_type in {"CHARGE", "PARK"}:
                values.append(
                    EventInput(
                        type=f"structured_{operation.operation_type.lower()}",
                        payload=payload,
                    )
                )
        return values


class GoodsToPersonOptions(StrictModel):
    """Per-request G2P compiler options.

    These options do not choose whether the warehouse uses G2P; the server-level
    ``OUTBOUND_FULFILLMENT_MODE`` setting owns that policy. They only constrain
    station selection and handling-unit allocation for a trusted request.
    """

    preferred_station_id: str | None = None
    require_single_handling_unit: bool = False
    same_mobile_robot_round_trip: bool = True


class RobotRuntimeOverride(StrictModel):
    """Trusted test/replan override. Live operation should use telemetry instead."""

    robot_id: str
    current_node: str | None = None
    current_edge: str | None = None
    from_node: str | None = None
    to_node: str | None = None
    edge_progress: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str = "idle"
    battery_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    capacity_units: int | None = Field(default=None, ge=0)
    current_load_units: int | None = Field(default=None, ge=0)
    active_task_id: str | None = None
    # Rolling-horizon projection has reached a handover boundary where the
    # previous task/mission must be removed from an OVERLAY Redis snapshot.
    clear_active_work: bool = False
    sim_time_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_position(self) -> "RobotRuntimeOverride":
        on_edge = self.current_edge is not None
        if not on_edge and not self.current_node:
            raise ValueError("runtime robot override requires current_node or current_edge")
        if on_edge and not (self.from_node and self.to_node and self.edge_progress is not None):
            raise ValueError(
                "current_edge override requires from_node, to_node, and edge_progress"
            )
        if (self.current_load_units is not None and self.capacity_units is not None
                and self.current_load_units > self.capacity_units):
            raise ValueError("current_load_units cannot exceed capacity_units")
        return self


class RuntimePlanningOverrides(StrictModel):
    robot_states: list[RobotRuntimeOverride] = Field(default_factory=list)
    runtime_snapshot_mode: Literal["OVERLAY", "COMPLETE"] = "OVERLAY"
    preserved_edge_reservations: list[EdgeReservation] = Field(default_factory=list)
    preserved_node_reservations: list[NodeReservation] = Field(default_factory=list)
    preserved_station_reservations: list[StationServiceReservation] = Field(default_factory=list)
    source_plan_id: str | None = None
    planning_horizon_start_ms: int = Field(default=0, ge=0)
    relocate_idle_robot_ids: list[str] = Field(default_factory=list)
    # Trusted rolling-horizon fleet floor. It counts task-performing robots only;
    # charge/park relocation routes remain outside this lower bound.
    minimum_task_vehicle_count: int = Field(default=0, ge=0)


class PublicRuntimeSnapshot(StrictModel):
    """Optional deviation snapshot used only for this planning request.

    Normal browser playback does not stream telemetry: the server projects the
    current state from ``active_plan_id`` and ``replan_at_sim_time_ms``.  Supply
    this snapshot only when reality diverged from the stored plan, for example a
    robot fault, manual relocation, delayed service, or failed pickup.  Battery
    and capacity remain deterministic facts; the LLM cannot override them.
    """

    mode: Literal["OVERLAY", "COMPLETE"] = "OVERLAY"
    captured_at_sim_time_ms: int = Field(default=0, ge=0)
    robot_states: list[RobotRuntimeOverride] = Field(default_factory=list)
    # Test/replan-only committed intervals. They remain request-scoped and are
    # never written back to Redis by the planning API. Exposing them here lets
    # integration tests prove that MAPF inserts WAIT around a real reservation.
    preserved_edge_reservations: list[EdgeReservation] = Field(default_factory=list)
    preserved_node_reservations: list[NodeReservation] = Field(default_factory=list)
    preserved_station_reservations: list[StationServiceReservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_runtime_snapshot(self) -> "PublicRuntimeSnapshot":
        robot_ids = [value.robot_id for value in self.robot_states]
        if len(robot_ids) != len(set(robot_ids)):
            raise ValueError("runtime_snapshot robot_id values must be unique")
        if self.mode == "COMPLETE":
            if not self.robot_states:
                raise ValueError("COMPLETE runtime_snapshot requires robot_states")
            incomplete = [
                value.robot_id
                for value in self.robot_states
                if value.battery_pct is None or value.capacity_units is None
            ]
            if incomplete:
                raise ValueError(
                    "COMPLETE runtime_snapshot requires battery_pct and capacity_units "
                    f"for every robot: {', '.join(incomplete)}"
                )
        reservation_ids = [
            *[value.reservation_id for value in self.preserved_edge_reservations],
            *[value.reservation_id for value in self.preserved_node_reservations],
            *[value.reservation_id for value in self.preserved_station_reservations],
        ]
        if len(reservation_ids) != len(set(reservation_ids)):
            raise ValueError("runtime_snapshot reservation_id values must be unique")
        return self

    def to_internal(self) -> RuntimePlanningOverrides:
        states = [
            value.model_copy(
                update={
                    "sim_time_ms": (
                        value.sim_time_ms
                        if value.sim_time_ms > 0
                        else self.captured_at_sim_time_ms
                    )
                }
            )
            for value in self.robot_states
        ]
        return RuntimePlanningOverrides(
            robot_states=states,
            runtime_snapshot_mode=self.mode,
            preserved_edge_reservations=list(self.preserved_edge_reservations),
            preserved_node_reservations=list(self.preserved_node_reservations),
            preserved_station_reservations=list(self.preserved_station_reservations),
        )


class ScenarioRuntimeBootstrapRequest(StrictModel):
    """Debug-only request that clones one Redis simulation namespace.

    Complex API scenarios intentionally use isolated ``simulation_id`` values.
    The source runtime is copied into that namespace before the mission request
    so every scenario starts from the same robot, edge, and station baseline.
    """

    warehouse_id: WarehouseId
    target_simulation_id: str = Field(min_length=1, max_length=128)
    source_simulation_id: str | None = Field(default=None, min_length=1, max_length=128)
    reset: bool = True
    copy_robot_runtime: bool = True
    copy_edge_runtime: bool = True
    copy_station_runtime: bool = True
    copy_reservations: bool = False


class ScenarioRuntimeBootstrapResult(StrictModel):
    status: Literal["BOOTSTRAPPED", "NOOP"]
    warehouse_id: WarehouseId
    source_simulation_id: str
    target_simulation_id: str
    robots: int = Field(default=0, ge=0)
    edges: int = Field(default=0, ge=0)
    stations: int = Field(default=0, ge=0)
    reservations: int = Field(default=0, ge=0)
    source_runtime_version: str = "0"
    target_runtime_version: str = "0"


class AutoMissionRequest(StrictModel):
    """Internal request used by the LangGraph workflow.

    Public FastAPI callers use :class:`PublicMissionRequest`; the API infers
    ``request_mode`` and converts to this explicit internal contract.
    """

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str = "SIM001"
    request_mode: RequestMode = "event_driven"
    optimization_backend: OptimizationBackend | None = None
    events: list[EventInput] = Field(default_factory=list)
    structured_input: StructuredMissionInput | None = None
    user_command: str | None = None
    mission_spec: MissionSpec | None = None
    planning_mode: PlanningMode | None = None
    goods_to_person_options: GoodsToPersonOptions = Field(default_factory=GoodsToPersonOptions)
    runtime_overrides: RuntimePlanningOverrides = Field(default_factory=lambda: RuntimePlanningOverrides())
    normalized_request_override: NormalizedWarehouseRequest | None = None
    evaluation_shadow_mode: bool = False
    human_responses: list[HumanInteractionResponse] = Field(default_factory=list)
    parent_interaction_id: str | None = None
    max_agent_steps: int = Field(default=8, ge=1, le=16)
    max_planner_retries: int = Field(default=1, ge=0, le=3)

    @field_validator("warehouse_id", mode="before")
    @classmethod
    def validate_warehouse_id(cls, value: object) -> str:
        return normalize_warehouse_id(value)

    @field_validator("planning_mode", mode="before")
    @classmethod
    def normalize_planning_mode(cls, value: object) -> object:
        """Accept clear v13.12 names and legacy script aliases."""

        if value is None or str(value).strip() == "":
            return None
        return canonicalize_planning_mode(value)

    @model_validator(mode="after")
    def validate_entry_contract(self) -> "AutoMissionRequest":
        """Validate request-mode input requirements."""

        if self.request_mode == "event_driven" and not self.events:
            raise ValueError("event_driven mode requires at least one event")
        if self.request_mode == "human_command" and not (self.user_command or "").strip():
            raise ValueError("human_command mode requires user_command")
        if self.request_mode == "mixed" and (not self.events or not (self.user_command or "").strip()):
            raise ValueError("mixed mode requires both events and user_command")
        return self


class PublicMissionRequest(StrictModel):
    """Stable front-end mission input.

    The browser chooses neither ``request_mode`` nor Rule/Agent. The server
    infers the input shape from ``events`` and ``user_command``, then the router
    and deterministic guard lock one execution branch.
    """

    warehouse_id: WarehouseId
    simulation_id: str = Field(min_length=1, max_length=128)
    optimization_backend: OptimizationBackend | None = None
    events: list[EventInput] = Field(default_factory=list)
    structured_input: StructuredMissionInput | None = None
    user_command: str | None = None
    runtime_snapshot: PublicRuntimeSnapshot | None = None

    @model_validator(mode="after")
    def validate_payload_shape(self) -> "PublicMissionRequest":
        effective_events = [
            *self.events,
            *(self.structured_input.to_events() if self.structured_input is not None else []),
        ]
        infer_request_mode(events=effective_events, user_command=self.user_command)
        return self

    def to_internal(self) -> AutoMissionRequest:
        settings_snapshot = (
            self.runtime_snapshot.to_internal()
            if self.runtime_snapshot is not None
            else RuntimePlanningOverrides()
        )
        effective_events = [
            *self.events,
            *(self.structured_input.to_events() if self.structured_input is not None else []),
        ]
        return AutoMissionRequest(
            warehouse_id=self.warehouse_id,
            simulation_id=self.simulation_id,
            request_mode=infer_request_mode(
                events=effective_events, user_command=self.user_command
            ),
            optimization_backend=self.optimization_backend,
            events=effective_events,
            structured_input=self.structured_input,
            user_command=self.user_command,
            runtime_overrides=settings_snapshot,
        )


class EntryRouteDecision(StrictModel):
    """Classify only the outer request shape before semantic formulation.

    The entry classifier deliberately does not decide RULE versus AGENT
    formulation.  That decision is made after normalization and supervision.
    """

    route: EntryRoute
    normalization_strategy: NormalizationStrategy = "NONE"
    supervisor_strategy: SupervisorStrategy = "NONE"
    reasons: list[str] = Field(default_factory=list)


class OrchestrationPlan(StrictModel):
    """Authoritative workflow plan built after semantic supervision."""

    orchestration_goal: str
    route: OrchestrationRoute
    formulation_route: RequestFinalRoute | None = None
    retrieval_strategy: Literal["DIRECT_CONTEXT", "PARALLEL_TOOL_PLAN", "STEPWISE_TOOL_AGENT", "LEGACY_CONTEXT", "NONE"] = "NONE"
    selected_context_nodes: list[ContextNodeName] = Field(default_factory=list)
    selected_retrieval_tools: list[str] = Field(default_factory=list)
    routing_reason: list[str] = Field(default_factory=list)
    routing_source: Literal[
        "formulation_supervisor",
        "request_router_llm",
        "deterministic_event_mapping",
        "external_mission",
        "special_route",
        "incident_response_service",
    ]
    planning_mode: PlanningMode
    requested_planning_mode: PlanningMode | None = None
    planning_mode_source: PlanningModeSource = "environment"
    route_locked: bool = True
    route_switch_allowed: bool = False
    route_decision_stage: Literal["PRE_EXECUTION"] = "PRE_EXECUTION"
    needs_optimization: bool = False

    @model_validator(mode="after")
    def validate_route_lock(self) -> "OrchestrationPlan":
        """Enforce a one-time pre-execution Rule/Agent branch decision."""

        if not self.route_locked or self.route_switch_allowed:
            raise ValueError(
                "Rule/Agent route must be locked before branch execution and cannot switch later."
            )
        expected = {
            "RULE_MISSION_PIPELINE": "RULE_FORMULATION",
            "AGENT_MISSION_PIPELINE": "AGENT_FORMULATION",
            "INCIDENT_RESPONSE_PIPELINE": "INCIDENT_RESPONSE",
        }.get(self.route)
        if expected is not None and self.formulation_route != expected:
            raise ValueError(
                f"{self.route} requires formulation_route={expected}; "
                f"received {self.formulation_route}."
            )
        return self


class NormalizedOperation(StrictModel):
    """One business operation extracted from structured events or natural language.

    ``attributes`` is intentionally a human-readable note instead of an
    open-ended JSON object.  Authoritative constraints always live in typed
    fields such as ``operation_id`` and ``NormalizedRequestConstraints``.
    This keeps OpenAI strict structured-output schemas closed while retaining a
    compact debugging explanation for the front end and LangSmith.
    """

    operation_id: str
    operation_type: Literal["OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY", "QUERY", "INCIDENT", "UNKNOWN"]
    source_event_type: str | None = None
    raw_reference: str | None = None
    attributes: str = Field(default="", max_length=2000)


class ConditionalEdgePolicy(StrictModel):
    """Typed conditional policy for one canonical warehouse edge.

    The router may identify the policy from natural language, but the edge ID,
    threshold, operator, and two allowed actions remain machine-checkable. A single
    typed policy is evaluated deterministically from runtime evidence in the Rule path.
    """

    edge_id: str
    metric: Literal["EXPECTED_WAIT_MS"] = "EXPECTED_WAIT_MS"
    operator: Literal["GT", "GTE", "LT", "LTE"] = "GT"
    threshold_ms: int = Field(ge=0)
    when_true: Literal["HARD_AVOID", "SOFT_AVOID", "ALLOW"]
    when_false: Literal["HARD_AVOID", "SOFT_AVOID", "ALLOW"]
    source_text: str = ""


class NormalizedRequestConstraints(StrictModel):
    """Business constraints separated into canonical IDs and semantic references.

    Exact identifiers may flow directly to the Rule fast path. Natural-language
    references are resolved only by the Agent read-tool loop before formulation.
    """

    excluded_robot_ids: list[str] = Field(default_factory=list)
    excluded_robot_references: list[str] = Field(default_factory=list)
    excluded_robot_statuses: list[str] = Field(default_factory=list)
    excluded_robot_status_references: list[str] = Field(default_factory=list)
    soft_avoid_edge_ids: list[str] = Field(default_factory=list)
    soft_avoid_edge_references: list[str] = Field(default_factory=list)
    hard_block_edge_ids: list[str] = Field(default_factory=list)
    hard_block_edge_references: list[str] = Field(default_factory=list)
    conditional_edge_policies: list[ConditionalEdgePolicy] = Field(default_factory=list)
    objective_profile: ObjectiveProfile = "MIN_TOTAL_COST"
    # True only when the caller/operator explicitly selected the objective.
    # Agent formulation may choose a different profile from live warehouse
    # context when this remains false; Rule formulation keeps the cost default.
    objective_profile_explicit: bool = False
    # The profile is the single solver-facing mode; these terms preserve a
    # semantic multi-objective request for routing, validation, and audit.
    objective_terms: list[ObjectiveTerm] = Field(default_factory=list)
    # Keep this many eligible robots outside the optimization fleet as an
    # emergency reserve. The highest-battery robots are selected
    # deterministically and are distinct from explicit exclusions.
    reserve_robot_count: int = Field(default=0, ge=0, le=32)
    reserve_robot_min_battery_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    max_edge_wait_ms: int | None = Field(default=None, ge=0)


class SystemContextRequirement(StrictModel):
    """Warehouse fact that can be resolved by a read-only system context node.

    These requirements are not questions for the operator.  They describe facts
    that the workflow must fetch from inventory, robot runtime, or map storage
    before formulation.
    """

    code: str
    context_node: ContextNodeName
    description: str
    entity_ids: list[str] = Field(default_factory=list)


class PolicyDefaultRequirement(StrictModel):
    """Policy value that must come from approved configuration, not user guessing."""

    policy_key: str
    description: str


class NormalizedWarehouseRequest(StrictModel):
    """Common request schema used by the formulation supervisor.

    Missing information is deliberately split into three classes:

    * ``system_context_requirements`` are fetched from read-only warehouse tools.
    * ``policy_default_requirements`` are resolved from approved system policy.
    * ``user_clarification_questions`` contain only facts or intent that the
      warehouse system cannot determine without asking the operator.
    """

    source: Literal["structured_events", "natural_language", "mixed"]
    operations: list[NormalizedOperation] = Field(default_factory=list)
    incidents: list[OperationalIncidentImpact] = Field(default_factory=list)
    constraints: NormalizedRequestConstraints = Field(default_factory=NormalizedRequestConstraints)
    raw_user_command: str | None = None
    system_context_requirements: list[SystemContextRequirement] = Field(default_factory=list)
    policy_default_requirements: list[PolicyDefaultRequirement] = Field(default_factory=list)
    user_clarification_questions: list[str] = Field(default_factory=list)
    normalization_summary: str


class FormulationRecommendation(StrictModel):
    """Tool-free LLM recommendation before the route is locked."""

    route: Literal["RULE_FORMULATION", "AGENT_FORMULATION", "HUMAN_REVIEW"]
    gate_action: Literal["PROCEED", "ASK_CLARIFICATION", "REQUIRE_HUMAN_APPROVAL"] = "PROCEED"
    reason_code: str | None = None
    reasons: list[str] = Field(default_factory=list)
    prompt: str | None = None
    options: list[HumanInteractionOption] = Field(default_factory=list)
    recommended_option_id: str | None = None


class WorkflowHoldResult(StrictModel):
    """Auditable terminal hold created by an approved exception choice.

    A hold is neither an input error nor an optimizer failure.  It records that
    automation intentionally stopped until a human work item (for example a
    recount) is completed.
    """

    reason_code: str
    message: str
    selected_option_id: str | None = None
    required_actions: list[str] = Field(default_factory=list)


class RequestGateDecision(StrictModel):
    """Final deterministic pre-execution gate result."""

    action: RequestGateAction
    recommended_route: Literal["RULE_FORMULATION", "AGENT_FORMULATION"] | None = None
    final_route: RequestFinalRoute | None = None
    reasons: list[str] = Field(default_factory=list)
    route_locked: bool = False
    human_interaction: HumanInteractionRequest | None = None
    input_rejection: InputRejectionResult | None = None
    workflow_hold: WorkflowHoldResult | None = None

    @model_validator(mode="after")
    def validate_gate_contract(self) -> "RequestGateDecision":
        if self.action in {"ROUTE_RULE", "ROUTE_AGENT"}:
            expected = "RULE_FORMULATION" if self.action == "ROUTE_RULE" else "AGENT_FORMULATION"
            if self.final_route != expected or not self.route_locked:
                raise ValueError(f"{self.action} requires locked final_route={expected}.")
            if any((self.human_interaction, self.input_rejection, self.workflow_hold)):
                raise ValueError("Route actions must not carry a terminal gate payload.")
        elif self.action == "HANDLE_INCIDENT":
            if self.final_route != "INCIDENT_RESPONSE" or not self.route_locked:
                raise ValueError("HANDLE_INCIDENT requires the locked INCIDENT_RESPONSE route.")
            if any((self.human_interaction, self.input_rejection, self.workflow_hold)):
                raise ValueError("HANDLE_INCIDENT must not carry another terminal gate payload.")
        elif self.action == "REJECT_INPUT":
            if self.final_route is not None or self.route_locked:
                raise ValueError("REJECT_INPUT must finish before route locking.")
            if self.human_interaction is not None or self.input_rejection is None or self.workflow_hold is not None:
                raise ValueError("REJECT_INPUT requires input_rejection only.")
        elif self.action == "HOLD_WORKFLOW":
            if self.final_route is not None or self.route_locked:
                raise ValueError("HOLD_WORKFLOW is a terminal hold without a new execution route.")
            if self.workflow_hold is None or self.human_interaction is not None or self.input_rejection is not None:
                raise ValueError("HOLD_WORKFLOW requires workflow_hold only.")
        else:
            if self.human_interaction is None or self.input_rejection is not None or self.workflow_hold is not None:
                raise ValueError("HITL gate actions require only human_interaction.")
            if self.final_route is None:
                if self.route_locked or self.human_interaction.route_locked:
                    raise ValueError("Pre-route HITL without an execution route cannot be locked.")
            else:
                if self.final_route != "INCIDENT_RESPONSE":
                    raise ValueError("Only the special INCIDENT_RESPONSE route may be locked before incident HITL.")
                if not self.route_locked or not self.human_interaction.route_locked:
                    raise ValueError("Incident HITL requires matching locked route state.")
                if self.human_interaction.resume_route != "INCIDENT_RESPONSE":
                    raise ValueError("Incident HITL must resume the INCIDENT_RESPONSE route.")
        return self


class RoutedNormalizedWarehouseRequest(StrictModel):
    """One tool-free LLM result that normalizes and recommends Rule or Agent.

    This contract deliberately combines the former input-normalizer and formulation-
    supervisor calls.  It sees only the incoming request envelope; it cannot query
    PostgreSQL, Redis, Neo4j, or any retrieval Tool.  The graph resolves genuine
    clarification and applies the deterministic pre-route guard before locking the route.
    """

    normalized_request: NormalizedWarehouseRequest
    recommendation: FormulationRecommendation


class GeneratedCommandRoutingDecision(StrictModel):
    """Compact Router result for an already validated generated command batch.

    The Spring command generator owns the immutable operation/resource contract.
    The Router therefore returns only semantic policy/context additions and the
    Rule-vs-Agent recommendation instead of echoing every operation.
    """

    constraints: NormalizedRequestConstraints = Field(default_factory=NormalizedRequestConstraints)
    system_context_requirements: list[SystemContextRequirement] = Field(default_factory=list)
    policy_default_requirements: list[PolicyDefaultRequirement] = Field(default_factory=list)
    normalization_summary: str
    recommendation: FormulationRecommendation


class FormulationDecision(StrictModel):
    """Resolved graph route after deterministic context and clarification guards."""

    route: FormulationRoute
    reasons: list[str] = Field(default_factory=list)
    required_context_nodes: list[ContextNodeName] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


EvidenceSource = Literal["request", "inventory_store", "facility_master", "robot_runtime", "traffic_runtime", "warehouse_graph"]
SituationNodeType = Literal[
    "order", "item", "stock", "handling_unit", "rack", "rack_access", "robot",
    "route_node", "outbound", "logical_destination", "outbound_station",
    "outbound_station_access", "empty_tote_buffer", "empty_tote_buffer_access",
    "inbound", "inbound_handoff_access", "rack_slot", "charging_slot", "edge", "runtime_constraint", "active_task",
    "path_option"
]
SituationRelationType = Literal[
    "REQUIRES_ITEM", "DELIVER_TO", "OF_ITEM", "STORED_AT", "HAS_ACCESS_POINT", "LOCATED_AT",
    "AFFECTS", "OCCUPIED_BY", "STARTS_AT", "ENDS_AT", "CAN_REACH",
    "USES_EDGE", "HAS_ACTIVE_TASK", "REPRESENTS_STOCK", "SERVES_DESTINATION",
    "ROUTES_THROUGH", "POST_MOVE_TO", "USES_HANDLING_UNIT", "PICKUP_FROM", "PUTAWAY_TO"
]


class SituationEvidence(StrictModel):
    """Trace one situation fact to its read-only source record."""

    evidence_id: str
    source: EvidenceSource
    source_record_id: str
    observation_id: str
    captured_at: str


class SituationNode(StrictModel):
    """Entity in the request-scoped warehouse situation graph."""

    node_id: str
    node_type: SituationNodeType
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class SituationRelation(StrictModel):
    """Semantic relation between two situation entities."""

    relation_id: str
    source_node_id: str
    target_node_id: str
    relation_type: SituationRelationType
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class SituationPathEvidence(StrictModel):
    """Deterministic path summary used as graph-RAG evidence, not a final route."""

    path_id: str
    purpose: Literal[
        "ROBOT_TO_PICKUP",
        "PICKUP_TO_DELIVERY",
        "PICKUP_TO_STATION",
        "STATION_TO_POST_MOVE",
    ]
    source_node_id: str
    target_node_id: str
    node_sequence: list[str]
    edge_sequence: list[str]
    cost: float = Field(ge=0)
    travel_time_ms: int = Field(ge=0)
    affected_constraint_ids: list[str] = Field(default_factory=list)


class SituationGraphCompleteness(StrictModel):
    """Whether the graph contains enough complete evidence for cuOpt formulation."""

    order_facts_complete: bool
    inventory_candidates_complete: bool
    robot_candidates_complete: bool
    map_paths_complete: bool
    runtime_constraints_complete: bool
    missing_information: list[str] = Field(default_factory=list)
    truncated_sections: list[str] = Field(default_factory=list)
    ready_for_formulation: bool


class WarehouseSituationGraph(StrictModel):
    """Read-only request-scoped graph joining orders, stock, robots, map, and runtime state."""

    fulfillment_mode: Literal["legacy_order_tasks", "goods_to_person"] = "legacy_order_tasks"
    g2p_order_ids: list[str] = Field(default_factory=list)
    snapshot_id: str
    captured_at: str
    graph_version: str
    inventory_version: str
    runtime_version: str
    request_anchor_ids: list[str] = Field(default_factory=list)
    nodes: list[SituationNode] = Field(default_factory=list)
    relations: list[SituationRelation] = Field(default_factory=list)
    path_evidence: list[SituationPathEvidence] = Field(default_factory=list)
    evidence_index: list[SituationEvidence] = Field(default_factory=list)
    completeness: SituationGraphCompleteness
    summary: str


class SituationGraphValidationResult(StrictModel):
    """Independent structural and evidence validation of a situation graph."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


RetrievalToolName = Literal[
    "find_orders",
    "get_order_facts",
    "get_inbound_facts",
    "get_inventory_candidates",
    "get_robot_candidates",
    "resolve_map_entities",
    "get_connecting_subgraph",
    "get_runtime_constraints",
    "get_active_operations",
]
RetrievalEntityType = Literal[
    "ORDER", "ITEM", "ROBOT", "RACK", "RACK_ACCESS", "NODE", "EDGE",
    "OUTBOUND", "INBOUND", "INBOUND_HANDOFF", "OUTBOUND_STATION",
    "EMPTY_TOTE_BUFFER", "CHARGING_SLOT"
]
EntityResolutionStatus = Literal["RESOLVED", "AMBIGUOUS", "NOT_FOUND"]
RepairTarget = Literal[
    "RETRIEVAL_AGENT",
    "QUERY_KEY_RESOLVER",
    "TOOL_EXECUTOR",
    "SITUATION_GRAPH",
    "CUOPT_FORMULATOR",
    "NONE",
]


class SemanticEntityReference(StrictModel):
    """Free-form entity mention that still needs canonical ID resolution."""

    reference_id: str
    raw_text: str
    expected_entity_types: list[RetrievalEntityType] = Field(default_factory=list)
    exact_id_hint: str | None = None
    required: bool = True


class RetrievalToolRequest(StrictModel):
    """One allowed semantic read-tool request selected by Rule or the LLM."""

    request_id: str
    tool_name: RetrievalToolName
    exact_ids: list[str] = Field(default_factory=list)
    raw_references: list[SemanticEntityReference] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    item_text: str | None = None
    statuses: list[str] = Field(default_factory=list)
    include_statuses: list[str] = Field(default_factory=list)
    exclude_statuses: list[str] = Field(default_factory=list)
    expected_entity_types: list[RetrievalEntityType] = Field(default_factory=list)
    allow_multiple_matches: bool = False
    derive_from_previous_results: bool = False
    include_runtime_constraints: bool = True
    depends_on: list[str] = Field(default_factory=list)
    purpose: str


class ParallelRetrievalPlan(StrictModel):
    """One validated read-only retrieval DAG authored in a single LLM call.

    Requests with no dependencies may execute in the same wave.  Dependent
    requests execute only after every request named in ``depends_on`` has
    produced an observation.  The plan never contains raw SQL, Cypher, or Redis
    commands; it only references the bounded Tool vocabulary above.
    """

    requests: list[RetrievalToolRequest] = Field(default_factory=list)
    planning_summary: str


class ParallelRetrievalWaveRecord(StrictModel):
    """Observed execution metrics for one dependency wave."""

    wave_index: int = Field(ge=1)
    request_ids: list[str]
    tool_names: list[RetrievalToolName]
    data_sources: list[Literal["postgres", "redis", "neo4j"]] = Field(default_factory=list)
    started_at: str
    ended_at: str
    duration_ms: float = Field(ge=0)
    parallel_width: int = Field(ge=1)


class ParallelRetrievalExecutionResult(StrictModel):
    """Auditable result of the deterministic parallel retrieval executor."""

    valid: bool
    plan_request_count: int = Field(ge=0)
    completed_request_ids: list[str] = Field(default_factory=list)
    wave_records: list[ParallelRetrievalWaveRecord] = Field(default_factory=list)
    peak_parallel_width: int = Field(default=0, ge=0)
    llm_planning_call_count: int = Field(default=1, ge=0)
    errors: list[WorkflowValidationIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    total_duration_ms: float = Field(default=0.0, ge=0)


RetrievalAgentAction = Literal[
    "CALL_TOOL",
    "FINALIZE_RETRIEVAL",
    "ASK_CLARIFICATION",
    "HUMAN_REVIEW",
]


class RetrievalAgentStep(StrictModel):
    """One bounded decision in the stepwise read-only retrieval loop.

    The LLM chooses exactly one next action.  It never emits SQL, Redis keys,
    Cypher, or a complete multi-tool program.
    """

    action: RetrievalAgentAction
    tool_request: RetrievalToolRequest | None = None
    clarification_questions: list[str] = Field(default_factory=list)
    human_review_reason: str | None = None
    rationale_summary: str

    @model_validator(mode="after")
    def validate_action_payload(self) -> "RetrievalAgentStep":
        """Require exactly the payload needed by the selected action."""

        if self.action == "CALL_TOOL" and self.tool_request is None:
            raise ValueError("CALL_TOOL requires tool_request")
        if self.action != "CALL_TOOL" and self.tool_request is not None:
            raise ValueError(f"{self.action} must not include tool_request")
        if self.action == "ASK_CLARIFICATION" and not self.clarification_questions:
            raise ValueError("ASK_CLARIFICATION requires questions")
        if self.action == "HUMAN_REVIEW" and not self.human_review_reason:
            raise ValueError("HUMAN_REVIEW requires human_review_reason")
        return self


class RetrievalToolCallValidationResult(StrictModel):
    """Validation of one proposed retrieval Tool call."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StructuredKeyValidationResult(StrictModel):
    """Existence and type validation for direct Rule-path identifiers."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_user_clarification: bool = False

class EntityResolutionCandidate(StrictModel):
    """One canonical entity candidate returned by deterministic resolution."""

    entity_id: str
    entity_type: RetrievalEntityType
    display_name: str
    match_method: Literal["EXACT_ID", "ALIAS", "ATTRIBUTE", "SEMANTIC", "GRAPH_RELATION"]
    confidence: float = Field(ge=0.0, le=1.0)


class EntityResolutionResult(StrictModel):
    """Resolution of one free-form or hinted entity reference."""

    reference_id: str
    raw_text: str
    status: EntityResolutionStatus
    resolved_entity_ids: list[str] = Field(default_factory=list)
    candidates: list[EntityResolutionCandidate] = Field(default_factory=list)
    reason: str


class ResolvedToolRequest(StrictModel):
    """Tool request after canonical IDs and safe filters have been resolved."""

    request_id: str
    tool_name: RetrievalToolName
    order_ids: list[str] = Field(default_factory=list)
    inbound_ids: list[str] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    robot_ids: list[str] = Field(default_factory=list)
    rack_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    include_statuses: list[str] = Field(default_factory=list)
    exclude_statuses: list[str] = Field(default_factory=list)
    item_text: str | None = None
    allow_multiple_matches: bool = False
    derive_from_previous_results: bool = False
    include_runtime_constraints: bool = True
    purpose: str


class RetrievalObservation(StrictModel):
    """Evidence returned by one deterministic read adapter."""

    observation_id: str
    request_id: str
    tool_name: RetrievalToolName
    summary: str
    canonical_entity_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class WorkflowValidationIssue(StrictModel):
    """Typed validation issue that drives bounded repair or terminal routing."""

    code: str
    node_name: str
    message: str
    entity_ids: list[str] = Field(default_factory=list)
    retryable: bool = False
    repair_target: RepairTarget = "NONE"
    requires_user_clarification: bool = False
    requires_human_review: bool = False


class RetrievalContextSufficiencyResult(StrictModel):
    """Whether the accumulated stepwise observations are complete."""

    ready: bool
    missing_domains: list[Literal["inventory", "robot_runtime", "map_graph", "active_operations"]] = Field(default_factory=list)
    missing_entity_ids: list[str] = Field(default_factory=list)
    ambiguous_references: list[str] = Field(default_factory=list)
    not_found_references: list[str] = Field(default_factory=list)
    recommended_next_tools: list[RetrievalToolName] = Field(default_factory=list)
    retryable: bool = False
    repair_target: RepairTarget = "NONE"
    errors: list[WorkflowValidationIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CuOptTaskDraft(StrictModel):
    """Dynamic pickup-delivery task authored by a rule or the LLM formulator."""

    task_id: str
    operation_type: Literal["OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"] = "OUTBOUND_ORDER"
    # Historical field name retained for compatibility.  The value is the
    # canonical source operation ID and may therefore be ORD-###, IN-###, or a
    # supported REC-* identifier depending on ``operation_type``.
    order_id: str
    item_id: str
    stock_id: str
    rack_id: str | None = None
    rack_level: int | None = Field(default=None, ge=1, le=3)
    pickup_node: str
    delivery_node: str
    demand: PositiveInt
    priority: Priority
    mandatory: bool = True
    fixed_vehicle_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class CuOptFleetDraft(StrictModel):
    """Dynamic fleet inclusion authored before numeric payload assembly."""

    included_robot_ids: list[str]
    excluded_robot_ids: list[str] = Field(default_factory=list)
    reserved_robot_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CuOptMapConstraintDraft(StrictModel):
    """Runtime map constraints selected for the optimization problem."""

    blocked_edge_ids: list[str] = Field(default_factory=list)
    soft_penalty_edge_ids: list[str] = Field(default_factory=list)
    max_edge_wait_ms: int | None = Field(default=None, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)


class CuOptDynamicInputDraft(StrictModel):
    """Human-readable dynamic portion of a cuOpt request.

    ``formulation_mode=GOODS_TO_PERSON`` controls outbound fulfillment only.
    Canonical outbound orders are preserved in ``g2p_order_ids`` while the
    deterministic G2P compiler creates physical handling-unit cycles later.
    Direct non-outbound work such as INBOUND_ITEM or RECOVERY remains in
    ``tasks``.  This makes mixed outbound/inbound requests representable without
    turning one logical order into one AMR trip.
    """

    formulation_mode: Literal["ORDER_TASKS", "GOODS_TO_PERSON"] = "ORDER_TASKS"
    g2p_order_ids: list[str] = Field(default_factory=list)
    snapshot_id: str
    graph_version: str
    formulation_source: Literal["rule", "llm"]
    objective_profile: ObjectiveProfile
    objective_terms: list[ObjectiveTerm] = Field(default_factory=list)
    tasks: list[CuOptTaskDraft]
    # Agent may provide a validated lower bound for an explicit parallelism
    # policy. A trusted rolling-horizon Rule replan may also preserve the prior
    # task fleet after a robot becomes unavailable. cuOpt still chooses IDs and
    # assignments.
    minimum_vehicle_count: int = Field(default=0, ge=0)
    # Historical field name retained for compatibility.  Values are canonical
    # operation IDs of any supported type, not outbound orders only.
    deferred_order_ids: list[str] = Field(default_factory=list)
    fleet: CuOptFleetDraft
    map_constraints: CuOptMapConstraintDraft = Field(default_factory=CuOptMapConstraintDraft)
    time_limit_seconds: int = Field(default=5, ge=1, le=300)
    formulation_summary: str


class CuOptDynamicInputValidationResult(StrictModel):
    """Fact, coverage, inventory, fleet, map, and evidence validation result."""

    valid: bool
    repairable: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CuOptEvidenceEnrichmentResult(StrictModel):
    """Mechanical evidence completion that never changes LLM business choices."""

    applied: bool = False
    added_task_evidence: dict[str, list[str]] = Field(default_factory=dict)
    added_fleet_evidence: list[str] = Field(default_factory=list)
    added_map_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


AgentToolName = Literal[
    "get_warehouse_summary",
    "get_pending_orders",
    "get_inventory_context",
    "get_robot_context",
    "get_map_context",
    "get_active_operations",
]


class AgentToolCall(StrictModel):
    """One bounded read-only warehouse tool request selected by the LLM agent."""

    tool_name: AgentToolName
    order_ids: list[str] = Field(default_factory=list)
    inbound_ids: list[str] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    robot_ids: list[str] = Field(default_factory=list)
    required_capacity: int | None = Field(default=None, ge=1)


class AgentToolObservation(StrictModel):
    """Bounded observation returned from one read-only warehouse tool."""

    observation_id: str
    tool_name: AgentToolName
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


OperationIntentType = Literal[
    "FULFILL_OUTBOUND_ORDER",
    "PUTAWAY_INBOUND_ITEM",
    "DEFER_OPERATION",
    "CONTINUE_ACTIVE_TASK",
    "RETURN_LOAD_TO_SOURCE",
    "DIVERT_LOAD_TO_BUFFER",
    "KEEP_ROBOT_CHARGING",
    "REQUEST_HUMAN_REVIEW",
]


class OperationIntent(StrictModel):
    """High-level warehouse operation selected without physical resource assignment."""

    operation_type: OperationIntentType
    target_id: str
    priority: Priority = "medium"
    max_defer_ms: int | None = Field(default=None, ge=0)
    reason: str


class MissionIntent(StrictModel):
    """LLM-authored semantic mission grounded in read-only tool observations.

    ``planning_route`` is a recommendation only.  A deterministic physical
    profiler may override it when a one-to-one rule plan cannot cover the
    work or when the baseline wait exceeds the configured threshold.
    """

    intent_type: Literal["MISSION", "QUERY", "NO_ACTION", "HUMAN_REVIEW"]
    objective_profile: ObjectiveProfile = "MIN_TOTAL_COST"
    planning_route: PlanningRouteRecommendation = "RULE"
    mission_goal: str
    operations: list[OperationIntent] = Field(default_factory=list)
    optional_operation_ids: list[str] = Field(default_factory=list)
    excluded_robot_ids: list[str] = Field(default_factory=list)
    soft_avoid_edge_ids: list[str] = Field(default_factory=list)
    max_edge_wait_ms: int | None = Field(default=None, ge=0)
    evidence_observation_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


class WarehouseAgentStep(StrictModel):
    """Next bounded action selected by the warehouse situation agent."""

    action: Literal["CALL_TOOL", "FINALIZE", "ASK_CLARIFICATION"]
    tool_call: AgentToolCall | None = None
    mission_intent: MissionIntent | None = None
    clarification_questions: list[str] = Field(default_factory=list)
    rationale_summary: str

    @model_validator(mode="after")
    def validate_action_payload(self) -> "WarehouseAgentStep":
        """Require exactly the payload needed by the selected agent action."""

        if self.action == "CALL_TOOL" and self.tool_call is None:
            raise ValueError("CALL_TOOL requires tool_call")
        if self.action == "FINALIZE" and self.mission_intent is None:
            raise ValueError("FINALIZE requires mission_intent")
        if self.action == "ASK_CLARIFICATION" and not self.clarification_questions:
            raise ValueError("ASK_CLARIFICATION requires at least one question")
        return self


class NodeExecutionRecord(StrictModel):
    """Observable graph-node execution record."""

    node_name: str
    purpose: str
    status: Literal["success", "failed"]
    started_at: str
    ended_at: str
    duration_ms: float = Field(ge=0)
    output_keys: list[str] = Field(default_factory=list)
    llm_used: bool = False
    error_code: str | None = None


class LLMNodeSummary(StrictModel):
    """Compact per-LLM-node task, input, and output summary."""

    node_name: str
    prompt_version: str
    model_name: str
    task_summary: str
    input_summary: str
    output_summary: str
    retry_count: int = Field(default=0, ge=0)


class FrontendNarrativeText(StrictModel):
    """LLM-authored prose only; all factual cards are built deterministically."""

    headline: str
    summary_text: str
    next_action: str
    debug_note: str


class FrontendTimelineItem(StrictModel):
    """One front-end timeline row derived from actual node execution records."""

    phase: str
    label: str
    status: Literal["success", "failed"]
    duration_ms: float = Field(ge=0)
    detail: str
    llm_used: bool = False


class FrontendExecutionSummary(StrictModel):
    """Operator-facing and debugger-facing execution summary for the UI."""

    generation_source: Literal["llm", "deterministic", "deterministic_fallback", "off"]
    language: str = "ko"
    headline: str
    status_label: str
    summary_text: str
    completed_actions: list[str] = Field(default_factory=list)
    selected_resources: list[str] = Field(default_factory=list)
    applied_constraints: list[str] = Field(default_factory=list)
    validation_summary: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_action: str
    debug_note: str
    timeline: list[FrontendTimelineItem] = Field(default_factory=list)


class ContextSnapshot(StrictModel):
    """Immutable source versions used by one graph execution."""

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str = "SIM001"
    snapshot_id: str
    captured_at: str
    graph_version: str
    inventory_version: str
    runtime_version: str
    repository_type: str = "unknown"
    source_manifest: dict[str, str] = Field(default_factory=dict)


class InventoryQueryScope(StrictModel):
    """Bounded inventory query used by the inventory context node."""

    mode: Literal["warehouse_overview", "item_detail", "order_fulfillment", "inbound_putaway", "mixed_operations"]
    warehouse_id: str
    order_ids: list[str] = Field(default_factory=list)
    inbound_ids: list[str] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    reason: str


class InboundTaskNeed(StrictModel):
    """Authoritative inbound handling-unit movement requirement."""

    inbound_id: str
    handling_unit_id: str
    item_id: str
    quantity: PositiveInt
    transport_unit_count: PositiveInt | None = None
    source_port_id: str
    priority: Priority = "medium"
    target_rack_id: str | None = None
    target_rack_level: int | None = Field(default=None, ge=1, le=3)
    status: str = "arrived"


class CandidatePutawaySlot(StrictModel):
    """One empty rack level that can receive an inbound handling unit."""

    rack_id: str
    rack_level: int = Field(ge=1, le=3)
    access_node_ids: list[str] = Field(min_length=1)
    capacity: int = Field(default=0, ge=0)


class InventoryTaskNeed(StrictModel):
    """Order-backed material movement requirement."""

    order_id: str
    item_id: str
    required_qty: PositiveInt
    delivery_node: str
    priority: Priority = "medium"
    order_status: str = "pending"


class CandidateStock(StrictModel):
    """Available item quantity at one rack level.

    ``rack_id`` is an inventory identity, not a routing node.  Robots service
    the rack from one of the dead-end ``access_node_ids`` that exist in the map
    graph.  This prevents a rack from accidentally becoming a transit shortcut.
    """

    stock_id: str
    item_id: str
    item_name: str
    rack_id: str
    rack_level: int = Field(ge=1, le=3)
    access_node_ids: list[str] = Field(min_length=1)
    available_qty: int = Field(ge=0)
    unit: str


class InventoryOverview(StrictModel):
    """Small warehouse inventory aggregate suitable for query responses."""

    rack_count: int = Field(ge=0)
    occupied_level_count: int = Field(ge=0)
    empty_level_count: int = Field(ge=0)
    distinct_item_count: int = Field(ge=0)
    total_quantity: int = Field(ge=0)


class InventoryContext(StrictModel):
    """Scoped inventory snapshot; no full raw-row dump is placed in the prompt."""

    query_scope: InventoryQueryScope
    inventory_summary: str
    overview: InventoryOverview | None = None
    task_needs: list[InventoryTaskNeed] = Field(default_factory=list)
    inbound_needs: list[InboundTaskNeed] = Field(default_factory=list)
    candidate_putaway_slots: list[CandidatePutawaySlot] = Field(default_factory=list)
    candidate_stocks: list[CandidateStock] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)


class EdgePenalty(StrictModel):
    """Cost and travel-time multipliers for a traversable congested edge."""

    edge_id: str
    cost_multiplier: float = Field(ge=1.0)
    travel_time_multiplier: float = Field(ge=1.0)
    reason: str


class EdgeOccupancy(StrictModel):
    """Physical edge occupancy used by traffic scheduling, not by LLM safety logic."""

    edge_id: str
    robot_id: str
    direction: str
    occupied_from_ms: int = Field(ge=0)
    occupied_until_ms: int = Field(gt=0)
    capacity: int = Field(default=1, ge=1)
    reason: str


class EdgeReservation(StrictModel):
    """Committed or planned physical-corridor use interval."""

    reservation_id: str
    edge_id: str
    robot_id: str
    direction: str
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(gt=0)
    from_node: str | None = None
    to_node: str | None = None
    physical_resource_id: str | None = None


class NodeReservation(StrictModel):
    """Committed node-use interval preserved across rolling-horizon replans."""

    reservation_id: str
    node_id: str
    robot_id: str
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(gt=0)
    reason: str

    @model_validator(mode="after")
    def validate_interval(self) -> "NodeReservation":
        if self.end_at_ms <= self.start_at_ms:
            raise ValueError("node reservation end_at_ms must exceed start_at_ms")
        return self


class MapConstraints(StrictModel):
    """Runtime map overlays passed to optimization and route validation."""

    blocked_edge_ids: list[str] = Field(default_factory=list)
    blocked_node_ids: list[str] = Field(default_factory=list)
    edge_penalties: list[EdgePenalty] = Field(default_factory=list)
    edge_occupancies: list[EdgeOccupancy] = Field(default_factory=list)
    edge_reservations: list[EdgeReservation] = Field(default_factory=list)


class RelevantMapNode(StrictModel):
    """Warehouse node relevant to the current task or runtime event."""

    node_id: str
    node_type: str
    x: float
    y: float


class MapContext(StrictModel):
    """Scoped semantic map context plus runtime overlays."""

    warehouse_id: WarehouseId = "WH-001"
    graph_version: str
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    relevant_nodes: list[RelevantMapNode] = Field(default_factory=list)
    map_constraints: MapConstraints = Field(default_factory=MapConstraints)
    summary: str
    missing_info: list[str] = Field(default_factory=list)


class RobotRuntime(StrictModel):
    """Current runtime state of one robot.

    The planner primarily consumes the graph position, status, battery, and
    capacity fields.  Version/tick/pose fields are retained because Redis (or
    the embedded Redis contract) is the authoritative source for simulation
    telemetry and stale-update rejection.
    """

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str | None = None
    robot_id: str
    robot_code: str
    status: str
    battery_pct: float = Field(ge=0, le=100)
    capacity_units: PositiveInt
    current_node: str | None = None
    # Stable terminal node from the BE robot master. Live position continues
    # to come from Redis; this node is the robot's dedicated charging home.
    home_node: str | None = None
    current_edge: str | None = None
    active_task_id: str | None = None
    active_mission_id: str | None = None
    load_state: Literal["EMPTY", "LOADED"] = "EMPTY"
    current_load_units: int = Field(default=0, ge=0)
    sequence: int = Field(default=0, ge=0)
    state_version: int = Field(default=1, ge=0)
    sim_tick: int | None = Field(default=None, ge=0)
    sim_time_ms: int = Field(default=0, ge=0)
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    from_node: str | None = None
    to_node: str | None = None
    edge_progress: float | None = Field(default=None, ge=0, le=1)


class RobotRuntimeContext(StrictModel):
    """Warehouse/session-scoped robot snapshot and eligibility result."""

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str = "SIM001"
    robots: list[RobotRuntime]
    candidate_robot_ids: list[str] = Field(default_factory=list)
    excluded_by_reason: dict[str, list[str]] = Field(default_factory=dict)
    min_battery_pct: float = Field(default=30.0, ge=0, le=100)
    min_capacity_units: int = Field(default=1, ge=1)
    summary: str
    missing_info: list[str] = Field(default_factory=list)


class TaskRequest(StrictModel):
    """High-level task proposed by an LLM or submitted externally."""

    request_type: TaskType
    order_id: str | None = None
    item_id: str | None = None
    requested_qty: PositiveInt
    delivery_node: str
    priority: Priority = "medium"
    fixed_robot_id: str | None = None


class MissionSpec(StrictModel):
    """System mission passed to policy materialization."""

    mission_type: MissionType
    mission_priority: Priority = "medium"
    reason: list[str] = Field(default_factory=list)
    task_requests: list[TaskRequest] = Field(default_factory=list)
    map_constraints: MapConstraints = Field(default_factory=MapConstraints)
    excluded_robot_ids: list[str] = Field(default_factory=list)
    optional_order_ids: list[str] = Field(default_factory=list)
    objective_profile: ObjectiveProfile = "MIN_TOTAL_COST"
    max_edge_wait_ms: int | None = Field(default=None, ge=0)
    soft_avoid_edge_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    mission_source: MissionSource = "external"
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_type_matches_tasks(self) -> "MissionSpec":
        """Require mission type to match its task composition."""

        task_types = {task.request_type for task in self.task_requests}
        expected: MissionType
        if not task_types:
            expected = "no_op"
        elif task_types == {"outbound_pick"}:
            expected = "order_fulfillment"
        elif task_types == {"loaded_transfer"}:
            expected = "robot_recovery"
        else:
            raise ValueError(f"unsupported mixed task composition: {sorted(task_types)}")
        if self.mission_type != expected:
            raise ValueError(f"mission_type {self.mission_type} does not match {expected}")
        return self


class FulfillmentCandidate(StrictModel):
    """Policy-approved rack-level candidate for one outbound order."""

    order_id: str
    item_id: str
    required_qty: PositiveInt
    delivery_node: str
    priority: Priority
    stock_id: str
    rack_id: str
    access_node_ids: list[str] = Field(min_length=1)
    rack_level: int = Field(ge=1, le=3)
    available_qty: int = Field(ge=0)



class StockAllocation(StrictModel):
    """Quantity selected from one rack level using availability and route-cost semantics."""

    stock_id: str
    item_id: str
    rack_id: str
    service_node_id: str
    rack_level: int = Field(ge=1, le=3)
    quantity: PositiveInt
    selection_cost: float = Field(ge=0)


class ValidatedTask(StrictModel):
    """Executable task produced after deterministic policy validation."""

    task_id: str
    task_type: TaskType
    pickup_node: str
    delivery_node: str
    demand: PositiveInt
    priority: Priority
    item_id: str | None = None
    order_id: str | None = None
    stock_id: str | None = None
    rack_id: str | None = None
    rack_level: int | None = Field(default=None, ge=1, le=3)
    fixed_robot_id: str | None = None


class CandidateRobot(StrictModel):
    """Robot admitted to the optimization problem."""

    robot_id: str
    start_node: str
    home_node: str | None = None
    capacity_units: PositiveInt
    battery_pct: float = Field(ge=0, le=100)
    available_at_ms: int = Field(default=0, ge=0)


class PolicyCheck(StrictModel):
    """Audit record for one deterministic policy check."""

    check_type: str
    status: Literal["pass", "fail"]
    target: str
    detail: dict[str, Any] = Field(default_factory=dict)


class PolicyViolation(StrictModel):
    """Business-policy violation separated from technical workflow failure."""

    code: str
    message: str
    repairable: bool = False


class EventDisposition(StrictModel):
    """Explicit explanation of how one input event affected the plan."""

    event_type: str
    entity_id: str | None
    resolution: Literal["TASK_CREATED", "CONSTRAINT_APPLIED", "OBSERVATION_ONLY", "ALREADY_HANDLED"]
    reason: str


class PolicyValidationResult(StrictModel):
    """Pure-function result built from MissionSpec and one ContextSnapshot."""

    status: Literal["pass", "repairable", "fail"]
    snapshot_id: str
    map_constraints: MapConstraints
    validated_tasks: list[ValidatedTask] = Field(default_factory=list)
    fulfillment_candidates: list[FulfillmentCandidate] = Field(default_factory=list)
    candidate_robots: list[CandidateRobot] = Field(default_factory=list)
    stock_allocations: list[StockAllocation] = Field(default_factory=list)
    event_dispositions: list[EventDisposition] = Field(default_factory=list)
    checks: list[PolicyCheck] = Field(default_factory=list)
    violations: list[PolicyViolation] = Field(default_factory=list)


class OptimizationTask(StrictModel):
    """Solver-neutral pickup-delivery task.

    New work normally leaves ``fixed_robot_id`` empty so the routing solver
    can assign and sequence it.  Only already-started/loaded work is fixed.
    """

    task_id: str
    pickup_node: str
    delivery_node: str
    demand: PositiveInt
    priority: Priority
    operation_type: Literal["OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY", "G2P_HANDLING_UNIT"] | None = None
    order_id: str | None = None
    order_ids: list[str] = Field(default_factory=list)
    item_id: str | None = None
    stock_id: str | None = None
    logical_destination_ids: list[str] = Field(default_factory=list)
    handling_unit_id: str | None = None
    g2p_batch_id: str | None = None
    station_id: str | None = None
    station_access_node: str | None = None
    post_station_node: str | None = None
    # ``rack_id`` is business/master data and is deliberately not a route node.
    # ``pickup_node``/``delivery_node`` contain the executable rack access node.
    rack_id: str | None = None
    rack_level: int | None = Field(default=None, ge=1, le=3)
    optional: bool = False
    unassigned_penalty: int | None = Field(default=None, ge=0)
    fixed_robot_id: str | None = None
    pickup_service_time_ms: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional authoritative pickup handling time. When omitted, the "
            "payload builder derives it from configured base and per-unit times."
        ),
    )
    drop_service_time_ms: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional authoritative drop handling time. When omitted, the "
            "payload builder derives it from configured base and per-unit times."
        ),
    )


class OptimizationVehicle(StrictModel):
    """Solver-neutral vehicle with explicit rolling-horizon and terminal facts."""

    robot_id: str
    start_node: str
    capacity_units: PositiveInt
    battery_pct: float = Field(ge=0, le=100)
    available_at_ms: int = Field(default=0, ge=0)
    end_node: str | None = None
    terminal_policy: TerminalPolicy = "STAY"


class OptimizationRequest(StrictModel):
    """Solver-neutral multi-vehicle pickup-delivery problem.

    The fleet-limit fields remain readable for historical persisted plans.
    Normal Rule plans emit no hard minimum fleet size. Agent may emit a validated
    lower bound for explicit parallelism policy, while a trusted Rule replan may
    preserve prior task capacity; the routing solver still chooses assignment.
    """

    snapshot_id: str
    tasks: list[OptimizationTask]
    vehicles: list[OptimizationVehicle]
    map_constraints: MapConstraints
    objective_profile: ObjectiveProfile = "MIN_TOTAL_COST"
    max_edge_wait_ms: int | None = Field(default=None, ge=0)
    minimum_vehicle_count: int = Field(default=0, ge=0)
    max_g2p_cycles_per_vehicle: int | None = Field(default=None, ge=1)


class FleetData(StrictModel):
    """Vehicle arrays used by the cuOpt adapter and local solver.

    ``vehicle_end_locations`` defaults to each vehicle's start location.  The
    explicit ``drop_return_trips`` vector decides whether the final leg to that
    end location is part of the cuOpt model.  Warehouse batch planning uses an
    open-route policy by default, because MAPF and the next planning horizon
    continue from the robot's last serviced task rather than forcing a return
    to its original start node.
    """

    vehicle_ids: list[str]
    vehicle_start_locations: list[int]
    vehicle_end_locations: list[int] = Field(default_factory=list)
    capacities: list[int]
    vehicle_available_at_ms: list[int] = Field(default_factory=list)
    min_vehicles: int = Field(default=0, ge=0)
    max_g2p_cycles_per_vehicle: int | None = Field(default=None, ge=1)
    skip_first_trips: list[bool] = Field(default_factory=list)
    drop_return_trips: list[bool] = Field(default_factory=list)


class TaskData(StrictModel):
    """Task arrays used by the cuOpt adapter and routing backends."""

    task_ids: list[str]
    task_locations: list[int]
    pickup_and_delivery_pairs: list[list[int]]
    demand: list[int]
    priorities: list[int]
    service_times_ms: list[int] = Field(default_factory=list)
    fixed_vehicle_ids: list[str | None]
    optional_task_ids: list[str] = Field(default_factory=list)


class WaypointGraphData(StrictModel):
    """Directed graph arrays with separate cost and travel-time metrics."""

    edge_ids: list[str]
    from_indices: list[int]
    to_indices: list[int]
    costs: list[float]
    travel_times_ms: list[int]
    # Service endpoints (rack access, inbound handoff, fixed station port) may
    # be route starts or task destinations, but never aisle shortcuts.  Keep
    # this topology flag in the indexed payload so every local reconstruction
    # applies the same production graph rule.
    service_only_node_indices: list[int] = Field(default_factory=list)


class CuOptPayload(StrictModel):
    """cuOpt-oriented wire contract generated from the scenario graph."""

    snapshot_id: str
    # Preserve the solver-neutral business objective through the indexed cuOpt
    # wire contract.  Native/local adapters translate this profile into the
    # objective controls supported by their respective routing engines.
    objective_profile: ObjectiveProfile = "MIN_TOTAL_COST"
    location_index_map: dict[str, int]
    fleet_data: FleetData
    task_data: TaskData
    waypoint_graph_data: WaypointGraphData
    applied_map_constraints: MapConstraints
    time_limit_seconds: int = Field(default=5, ge=1, le=300)


class PayloadValidationResult(StrictModel):
    """Deterministic payload contract validation result."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CandidateSpaceValidation(StrictModel):
    """Ensure the solver payload preserves canonical tasks and vehicle candidates."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OptimizerObjectiveMetric(StrictModel):
    """One named component of the optimizer's global objective."""

    name: str
    value: float


class OptimizerRoute(StrictModel):
    """Task ordering and route-level timing assigned to one robot.

    ``route_cost`` is populated only when a backend exposes or can derive an
    actual per-route cost. NVIDIA cuOpt's public response currently exposes
    only a global ``solution_cost``; it therefore leaves ``route_cost`` empty
    and reports task arrival/completion timestamps instead.
    """

    vehicle_id: str
    task_sequence: list[str]
    route_cost: float | None = Field(default=None, ge=0)
    task_arrival_stamps_ms: list[float | None] = Field(default_factory=list)
    last_task_arrival_ms: float | None = Field(default=None, ge=0)
    completion_ms: float | None = Field(default=None, ge=0)

    # Backward-compatible input only. The old contract overloaded this field
    # with either route completion time or the global cuOpt solution cost.
    # Keep accepting old serialized fixtures but never emit it in new output.
    objective_cost: float | None = Field(default=None, ge=0, exclude=True)


class OptimizerResult(StrictModel):
    """Normalized result from the local solver or external cuOpt service."""

    backend: OptimizerResultBackend
    status: Literal["success", "infeasible", "unavailable", "failed"]
    optimizer: str
    global_objective_cost: float | None = None
    objective_values: list[OptimizerObjectiveMetric] = Field(default_factory=list)
    estimated_makespan_ms: float | None = Field(default=None, ge=0)
    routes: list[OptimizerRoute] = Field(default_factory=list)
    unassigned_task_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OptimizerAssignmentValidation(StrictModel):
    """Task coverage and precedence validation result."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RouteSegment(StrictModel):
    """One directed warehouse edge in an expanded route."""

    sequence: int = Field(ge=0)
    edge_id: str
    from_node: str
    to_node: str
    cost: float = Field(ge=0)
    travel_time_ms: int = Field(gt=0)


class ExpandedRobotRoute(StrictModel):
    """Waypoint route expanded from task order."""

    vehicle_id: str
    start_node: str
    task_sequence: list[str]
    node_sequence: list[str]
    segments: list[RouteSegment]
    total_cost: float = Field(ge=0)
    total_travel_time_ms: int = Field(ge=0)


class WaypointRouteExpansionResult(StrictModel):
    """All robot waypoint routes."""

    status: Literal["expanded", "failed"]
    routes: list[ExpandedRobotRoute] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RouteValidationResult(StrictModel):
    """Static graph validation of expanded routes."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TimedRouteStep(StrictModel):
    """Scheduled MOVE, WAIT, or task-linked PICKUP/DROP service step."""

    step_type: Literal["MOVE", "WAIT", "SERVICE"]
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(gt=0)
    node_id: str | None = None
    edge_id: str | None = None
    from_node: str | None = None
    to_node: str | None = None
    task_id: str | None = None
    service_kind: Literal["PICKUP", "DROP", "STATION", "RETURN", "EMPTY_TOTE_BUFFER", "PARK", "CHARGE"] | None = None
    reason: str | None = None


class TimedRobotRoute(StrictModel):
    """Traffic-safe time schedule for one robot."""

    robot_id: str
    steps: list[TimedRouteStep]
    finish_at_ms: int = Field(ge=0)


class StationServiceReservation(StrictModel):
    """Exclusive input-handoff window of one fixed outbound station.

    The later sort/release conveyor stage may overlap the next reservation.
    """

    reservation_id: str
    station_id: str
    station_robot_id: str
    handling_unit_id: str
    mobile_robot_id: str
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(gt=0)
    processed_quantity: PositiveInt
    processing_ticks: PositiveInt


class TrafficScheduleResult(StrictModel):
    """Timed, collision-free route plan generated by the MAPF layer."""

    valid: bool
    planner: str = "prioritized_sipp"
    routes: list[TimedRobotRoute] = Field(default_factory=list)
    reservations: list[EdgeReservation] = Field(default_factory=list)
    station_reservations: list[StationServiceReservation] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    total_wait_ms: int = Field(default=0, ge=0)
    total_service_ms: int = Field(default=0, ge=0)
    makespan_ms: int = Field(default=0, ge=0)


class MAPFValidationResult(StrictModel):
    """Independent validation of a prioritized multi-goal MAPF plan."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)



GoodsToPersonDisposition = Literal[
    "RETURN_TO_HOME",
    "CONSUMED_AT_STATION",
    # Backward-compatible read support for plans persisted before the fixed
    # outbound-station handoff model was introduced.
    "MOVE_TO_EMPTY_TOTE_BUFFER",
]
GoodsToPersonPostStationAction = Literal[
    "RETURN_TO_SOURCE",
    "COMPLETE_AT_STATION",
    "MOVE_TO_EMPTY_TOTE_BUFFER",
]
GoodsToPersonPlanStatus = Literal[
    "ready_for_optimizer",
    "planned",
    "input_rejected",
    "infeasible",
    "failed",
]


class GoodsToPersonPlanRequest(StrictModel):
    """Plan one code-first outbound wave using handling-unit retrieval.

    The same contract is used by the compatibility endpoint and by the integrated
    main LangGraph branch.  Route-level constraints are supplied by the already
    normalized and locked orchestration request; the compiler never changes the
    Rule/Agent decision.
    """

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str = "SIM001"
    order_ids: list[str] = Field(min_length=1)
    optimization_backend: OptimizationBackend | None = None
    preferred_station_id: str | None = None
    require_single_handling_unit: bool = False
    same_mobile_robot_round_trip: bool = True
    allowed_robot_ids: list[str] = Field(default_factory=list)
    excluded_robot_ids: list[str] = Field(default_factory=list)
    objective_profile: ObjectiveProfile = "MIN_TOTAL_COST"


class OutboundChuteAllocation(StrictModel):
    """Quantity removed by the station robot for one outbound order."""

    order_id: str
    chute_id: str
    logical_destination_id: str
    quantity: PositiveInt

    @model_validator(mode="after")
    def validate_destination(self) -> "OutboundChuteAllocation":
        if self.logical_destination_id != self.chute_id:
            raise ValueError("logical_destination_id must equal the configured outbound chute")
        return self


class HandlingUnitBatchPlan(StrictModel):
    """One mobile-robot cycle for one physical handling unit."""

    batch_id: str
    item_id: str
    order_ids: list[str] = Field(min_length=1)
    logical_destination_ids: list[str] = Field(min_length=1)
    handling_unit_id: str
    handling_unit_version: int = Field(default=0, ge=0)
    source_stock_id: str
    source_rack_id: str
    source_rack_level: int = Field(ge=1, le=3)
    source_access_node: str
    station_id: str
    station_robot_id: str
    # Fixed station-robot workspace (blue station node on the UI map).
    station_access_node: str
    station_access_node_ids: list[str] = Field(default_factory=list)
    # AMR-side stop on the warehouse route boundary (purple route node).
    # Optional keeps previously persisted plans readable.
    mobile_handoff_node: str | None = None
    station_selection_score_ms: int = Field(default=0, ge=0)
    station_available_at_ms: int = Field(default=0, ge=0)
    station_queue_wait_ms: int = Field(default=0, ge=0)
    allocations: list[OutboundChuteAllocation] = Field(min_length=1)
    requested_quantity: PositiveInt
    quantity_before: PositiveInt
    quantity_after: int = Field(ge=0)
    return_required: bool
    disposition: GoodsToPersonDisposition
    post_station_action: GoodsToPersonPostStationAction
    post_station_node: str
    empty_tote_buffer_id: str | None = None
    station_receive_time_ms: int = Field(default=0, ge=0)
    station_sort_time_ms: int = Field(default=0, ge=0)
    station_release_time_ms: int = Field(default=0, ge=0)
    station_service_time_ms: int = Field(ge=0)
    station_processing_ticks: PositiveInt
    mobile_robot_id: str | None = None

    @model_validator(mode="after")
    def validate_quantities(self) -> "HandlingUnitBatchPlan":
        if sum(value.quantity for value in self.allocations) != self.requested_quantity:
            raise ValueError("allocation quantities must equal requested_quantity")
        if sorted(set(value.order_id for value in self.allocations)) != sorted(set(self.order_ids)):
            raise ValueError("order_ids must match allocation order IDs")
        destinations = sorted(set(value.logical_destination_id for value in self.allocations))
        if destinations != sorted(set(self.logical_destination_ids)):
            raise ValueError("logical_destination_ids must match allocation destinations")
        if self.quantity_before - self.requested_quantity != self.quantity_after:
            raise ValueError("quantity_after must equal quantity_before-requested_quantity")
        if self.return_required != (self.quantity_after > 0):
            raise ValueError("return_required must be true only for a positive remainder")
        if self.return_required:
            if self.disposition != "RETURN_TO_HOME":
                raise ValueError("positive remainders must use RETURN_TO_HOME")
            if self.post_station_action != "RETURN_TO_SOURCE":
                raise ValueError("positive remainders must use RETURN_TO_SOURCE")
            if self.post_station_node != self.source_access_node:
                raise ValueError("positive remainders must return to the source access node")
        elif self.disposition == "CONSUMED_AT_STATION":
            if self.post_station_action != "COMPLETE_AT_STATION":
                raise ValueError("consumed handling units must complete at the station")
            if self.empty_tote_buffer_id is not None:
                raise ValueError("consumed handling units must not reference an empty-tote buffer")
            if self.mobile_handoff_node and self.post_station_node != self.mobile_handoff_node:
                raise ValueError("consumed handling units must end at the AMR handoff node")
        else:
            # Older persisted plans may still contain the removed empty-tote
            # workflow. New compilers never emit this pair.
            if self.disposition != "MOVE_TO_EMPTY_TOTE_BUFFER":
                raise ValueError("depleted handling units must be consumed at the station")
            if self.post_station_action != "MOVE_TO_EMPTY_TOTE_BUFFER":
                raise ValueError("legacy empty-tote plans require MOVE_TO_EMPTY_TOTE_BUFFER")
            if not self.empty_tote_buffer_id:
                raise ValueError("legacy empty-tote plans require an empty-tote buffer")
        expected_station = (
            self.station_receive_time_ms
            + self.station_sort_time_ms
            + self.station_release_time_ms
        )
        if self.station_service_time_ms != expected_station:
            raise ValueError("station_service_time_ms must equal receive+sort+release")
        return self


class StationRobotAction(StrictModel):
    """Fixed outbound-station work that splits one handling unit across destinations."""

    station_robot_id: str
    station_id: str
    handling_unit_id: str
    action: Literal[
        "RECEIVE_HANDLING_UNIT",
        "SORT_TO_DESTINATIONS",
        "RELEASE_REMAINDER",
        "COMPLETE_OUTBOUND",
        "RELEASE_EMPTY_TOTE",
    ]
    order_ids: list[str] = Field(default_factory=list)
    logical_destination_ids: list[str] = Field(default_factory=list)
    start_after_mobile_drop: bool = True
    duration_ms: int = Field(ge=0)
    processing_ticks: int = Field(default=0, ge=0)


class InventoryMutationPreview(StrictModel):
    """Business-store mutation preview committed only after station confirmation."""

    handling_unit_id: str
    expected_version: int = Field(ge=0)
    quantity_before: int = Field(ge=0)
    reserved_quantity: int = Field(ge=0)
    quantity_after: int = Field(ge=0)
    next_status: Literal[
        "stored", "at_station", "returning", "consumed", "empty_in_transit", "empty_buffered"
    ]
    home_rack_id: str
    home_rack_level: int = Field(ge=1, le=3)
    post_station_node: str
    order_ids: list[str] = Field(default_factory=list)


class GoodsToPersonCompilationResult(StrictModel):
    """Result of compiling outbound order tasks into handling-unit cycles.

    The compiler stops before payload serialization and solver execution. The
    shared LangGraph cuOpt/MAPF nodes consume ``optimization_request``.
    """

    version: str = "13.21.1"
    applied: bool = False
    source_order_ids: list[str] = Field(default_factory=list)
    original_task_ids: list[str] = Field(default_factory=list)
    compiled_task_ids: list[str] = Field(default_factory=list)
    preserved_task_ids: list[str] = Field(default_factory=list)
    batches: list[HandlingUnitBatchPlan] = Field(default_factory=list)
    station_actions: list[StationRobotAction] = Field(default_factory=list)
    inventory_mutation_previews: list[InventoryMutationPreview] = Field(default_factory=list)
    optimization_request: OptimizationRequest | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    summary: str = ""


class GoodsToPersonRouteEnrichmentResult(StrictModel):
    """Same-AMR post-station execution goals added after solver assignment."""

    applied: bool = False
    valid: bool = True
    appended_task_ids: list[str] = Field(default_factory=list)
    batch_robot_assignments: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GoodsToPersonPlanResult(StrictModel):
    """End-to-end goods-to-person planning result for one or more item groups."""

    version: str = "13.21.1"
    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str
    status: GoodsToPersonPlanStatus
    batches: list[HandlingUnitBatchPlan] = Field(default_factory=list)
    station_actions: list[StationRobotAction] = Field(default_factory=list)
    inventory_mutation_previews: list[InventoryMutationPreview] = Field(default_factory=list)
    optimizer_payloads: list[CuOptPayload] = Field(default_factory=list)
    optimizer_results: list[OptimizerResult] = Field(default_factory=list)
    traffic_schedules: list[TrafficScheduleResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""


class GoodsToPersonBatchReservationRequest(StrictModel):
    """Persist one planned batch reservation in the business database."""

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str
    batch: HandlingUnitBatchPlan


class GoodsToPersonStationCommitRequest(StrictModel):
    """Commit quantities after the station robot confirms sorting."""

    warehouse_id: WarehouseId = "WH-001"
    batch_id: str


class GoodsToPersonPostMoveCommitRequest(StrictModel):
    """Confirm that the same AMR completed the return or empty-tote move."""

    warehouse_id: WarehouseId = "WH-001"
    batch_id: str
    robot_id: str


class RobotTelemetryUpdateRequest(StrictModel):
    """Code-first robot telemetry written to warehouse-scoped runtime state."""

    warehouse_id: WarehouseId | None = None
    simulation_id: str | None = None
    sequence: int = Field(ge=0)
    sim_tick: int | None = Field(default=None, ge=0)
    sim_time_ms: int = Field(ge=0)
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    current_node: str | None = None
    current_edge: str | None = None
    from_node: str | None = None
    to_node: str | None = None
    edge_progress: float | None = Field(default=None, ge=0, le=1)
    status: Literal["idle", "moving", "working", "waiting", "charging", "fault", "offline"]
    battery_pct: float = Field(ge=0, le=100)
    active_task_id: str | None = None
    active_mission_id: str | None = None
    current_load_units: int = Field(default=0, ge=0)
    capacity_units: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_location(self) -> "RobotTelemetryUpdateRequest":
        on_node = self.current_node is not None
        on_edge = self.current_edge is not None
        if on_node == on_edge:
            raise ValueError("exactly one of current_node or current_edge must be set")
        if on_edge and (not self.from_node or not self.to_node or self.edge_progress is None):
            raise ValueError("edge telemetry requires from_node, to_node, and edge_progress")
        if on_node and any(value is not None for value in (self.from_node, self.to_node)):
            raise ValueError("node telemetry must not include from_node/to_node")
        if self.current_load_units > self.capacity_units:
            raise ValueError("current_load_units cannot exceed capacity_units")
        return self


class RuntimeCommandPublishRequest(StrictModel):
    """Command envelope appended to a warehouse-scoped WCS command stream."""

    warehouse_id: WarehouseId | None = None
    simulation_id: str | None = None
    command_id: str
    robot_id: str
    command_type: Literal["MOVE", "WAIT", "SERVICE", "HOLD", "RELEASE"]
    payload: dict[str, Any] = Field(default_factory=dict)



class RobotExecutionContext(StrictModel):
    """Execution state used by the standalone recovery policy service."""

    robot_id: str
    task_phase: Literal["EN_ROUTE_TO_PICKUP", "LOADED_EN_ROUTE", "WAITING", "RETURNING"]
    load_state: Literal["EMPTY", "LOADED"]
    quantity: int = Field(ge=0)
    source_node: str | None = None
    destination_node: str | None = None
    current_node: str | None = None
    current_edge: str | None = None
    previous_safe_node: str | None = None
    next_safe_node: str | None = None


class RecoveryDecision(StrictModel):
    """Deterministic robot recovery recommendation."""

    action: RecoveryAction
    target_node: str | None = None
    reason: str


class QueryResponse(StrictModel):
    """Structured query-only response."""

    summary: str
    details: dict[str, Any] = Field(default_factory=dict)




class InputRejectionResult(StrictModel):
    """Deterministic rejection for malformed or unsupported mission input.

    This is deliberately not HITL.  The caller must submit a corrected request
    that uses canonical warehouse identifiers.
    """

    reason_code: str
    message: str
    required_identifier_types: list[str] = Field(default_factory=list)
    invalid_references: list[str] = Field(default_factory=list)

class ClarificationResult(StrictModel):
    """Normal conversational pause for information only the operator can provide."""

    reason: str
    questions: list[str] = Field(default_factory=list)


class HumanReviewResult(StrictModel):
    """Business, safety, or authority issue requiring an operator decision."""

    reason: str
    details: list[str] = Field(default_factory=list)


class WorkflowError(StrictModel):
    """Technical error propagated to the failure terminal."""

    stage: str
    code: str
    message: str
    retryable: bool = False


class WorkflowFailureResult(StrictModel):
    """Terminal technical failure."""

    stage: str
    errors: list[WorkflowError]


class PersistenceResult(StrictModel):
    """Persistence adapter result; JSON mode writes an execution artifact."""

    status: Literal["stored", "failed", "skipped"]
    path: str | None = None
    reason: str | None = None


class DashboardEvent(StrictModel):
    """Dashboard/stream envelope prepared after terminal persistence."""

    status: Literal["prepared"] = "prepared"
    event_type: str
    workflow_status: WorkflowStatus
    headline: str | None = None
    summary_text: str | None = None
    next_action: str | None = None


class PhysicalProblemProfile(StrictModel):
    """Deterministic estimate of whether one-to-one planning is sufficient."""

    task_count: int = Field(ge=0)
    eligible_robot_count: int = Field(ge=0)
    pickup_delivery_pair_count: int = Field(ge=0)
    baseline_deferred_count: int = Field(ge=0)
    baseline_total_wait_ms: int = Field(ge=0)
    baseline_max_wait_ms: int = Field(ge=0)
    force_global_solver: bool
    force_reasons: list[str] = Field(default_factory=list)


class PlanningRouteResolution(StrictModel):
    """LLM recommendation resolved against deterministic physical guards."""

    llm_recommended_route: PlanningRouteRecommendation
    resolved_route: PlanningRouteRecommendation
    override_reasons: list[str] = Field(default_factory=list)


class MultiTaskComparisonResult(StrictModel):
    """Comparable one-to-one and multi-task planning metrics for v9 probes."""

    baseline_assigned_count: int = Field(ge=0)
    baseline_deferred_count: int = Field(ge=0)
    baseline_total_wait_ms: int = Field(ge=0)
    baseline_max_wait_ms: int = Field(ge=0)
    baseline_makespan_ms: int = Field(ge=0)
    solver_assigned_count: int = Field(ge=0)
    solver_unassigned_count: int = Field(ge=0)
    solver_total_wait_ms: int = Field(ge=0)
    solver_max_wait_ms: int = Field(ge=0)
    solver_makespan_ms: int = Field(ge=0)
    wait_reduction_ms: int
    makespan_reduction_ms: int


class TerminalRelocationRecord(StrictModel):
    robot_id: str
    policy: TerminalPolicy
    from_node: str
    to_node: str
    task_id: str
    solver_end_cost_included: bool = False
    execution_only: bool = True
    reason: str


class TerminalRelocationResult(StrictModel):
    applied: bool = False
    valid: bool = True
    relocations: list[TerminalRelocationRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SimulationPlanStep(StrictModel):
    """One executable front-end step copied from the validated MAPF schedule."""

    step_id: str
    sequence: int = Field(ge=0)
    step_type: Literal["MOVE", "WAIT", "SERVICE"]
    start_at_ms: int = Field(ge=0)
    end_at_ms: int = Field(gt=0)
    node_id: str | None = None
    edge_id: str | None = None
    from_node: str | None = None
    to_node: str | None = None
    task_id: str | None = None
    service_kind: Literal["PICKUP", "DROP", "STATION", "RETURN", "EMPTY_TOTE_BUFFER", "PARK", "CHARGE"] | None = None
    reason: str | None = None
    distance_m: float | None = Field(default=None, ge=0.0)
    nominal_speed_mps: float | None = Field(default=None, gt=0.0)
    nominal_travel_time_ms: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_step_shape(self) -> "SimulationPlanStep":
        if self.end_at_ms <= self.start_at_ms:
            raise ValueError("simulation step end_at_ms must be greater than start_at_ms")
        if self.step_type == "MOVE" and not (self.edge_id and self.from_node and self.to_node):
            raise ValueError("MOVE requires edge_id, from_node, and to_node")
        if self.step_type in {"WAIT", "SERVICE"} and not self.node_id:
            raise ValueError(f"{self.step_type} requires node_id")
        if self.step_type == "SERVICE" and not self.service_kind:
            raise ValueError("SERVICE requires service_kind")
        return self


class SimulationRobotPlan(StrictModel):
    robot_id: str
    initial_node: str
    available_at_ms: int = Field(default=0, ge=0)
    finish_at_ms: int = Field(default=0, ge=0)
    steps: list[SimulationPlanStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timeline(self) -> "SimulationRobotPlan":
        ordered = sorted(self.steps, key=lambda value: value.sequence)
        if ordered != self.steps:
            raise ValueError("simulation steps must be ordered by sequence")
        if len({value.sequence for value in self.steps}) != len(self.steps):
            raise ValueError("simulation step sequence values must be unique")
        for previous, current in zip(self.steps, self.steps[1:]):
            if current.start_at_ms < previous.end_at_ms:
                raise ValueError("simulation steps must not overlap")
        if self.finish_at_ms < self.steps[-1].end_at_ms:
            raise ValueError("finish_at_ms cannot precede the final step")
        return self


class SimulationLogicalOperation(StrictModel):
    operation_id: str
    operation_type: Literal["OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"]
    item_id: str | None = None
    quantity: int | None = Field(default=None, ge=0)
    # Physical storage resource selected by formulation.  Route nodes used by
    # MOVE/SERVICE steps stay in the robot timeline; they are not the business
    # source/destination of the warehouse operation.
    rack_id: str | None = None
    rack_level: int | None = Field(default=None, ge=1, le=3)
    logical_destination_id: str | None = None
    source_port_id: str | None = None
    handling_unit_id: str | None = None
    assigned_robot_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)


class LogicalOperationCoverageValidationResult(StrictModel):
    """Independent final-plan coverage check for every actionable operation.

    The dynamic-input validators run before the solver.  This final guard runs
    after SimulationPlan materialization so an operation cannot silently lose
    its task/robot mapping in any downstream compiler, enrichment, MAPF, or
    projection step.
    """

    valid: bool
    requested_operation_ids: list[str] = Field(default_factory=list)
    executable_operation_ids: list[str] = Field(default_factory=list)
    deferred_operation_ids: list[str] = Field(default_factory=list)
    planned_operation_ids: list[str] = Field(default_factory=list)
    missing_operation_ids: list[str] = Field(default_factory=list)
    duplicate_operation_ids: list[str] = Field(default_factory=list)
    unexpected_operation_ids: list[str] = Field(default_factory=list)
    operations_without_tasks: list[str] = Field(default_factory=list)
    operations_without_robots: list[str] = Field(default_factory=list)
    task_ids_missing_from_plan: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PlanHandoverPoint(StrictModel):
    robot_id: str
    node_id: str
    handover_at_ms: int = Field(ge=0)
    reason: str
    handover_policy: HandoverPolicy = "CURRENT_NODE"
    current_step_id: str | None = None
    locked_task_ids: list[str] = Field(default_factory=list)
    carrying_load: bool = False


class ReplanExecutionSnapshot(StrictModel):
    """Plan-derived runtime state used to build a rolling-horizon problem.

    Each robot keeps an independent handover time.  The snapshot also carries
    old-plan reservations that must remain visible to the new MAPF solve while
    already-started physical work finishes.
    """

    source_plan_id: str
    replan_at_sim_time_ms: int = Field(ge=0)
    earliest_handover_at_ms: int = Field(ge=0)
    latest_handover_at_ms: int = Field(ge=0)
    handover_points: list[PlanHandoverPoint] = Field(default_factory=list)
    robot_overrides: list[RobotRuntimeOverride] = Field(default_factory=list)
    preserved_edge_reservations: list[EdgeReservation] = Field(default_factory=list)
    preserved_node_reservations: list[NodeReservation] = Field(default_factory=list)
    preserved_station_reservations: list[StationServiceReservation] = Field(default_factory=list)
    completed_task_bases: list[str] = Field(default_factory=list)
    locked_task_bases: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_handover_range(self) -> "ReplanExecutionSnapshot":
        if self.latest_handover_at_ms < self.earliest_handover_at_ms:
            raise ValueError("latest_handover_at_ms cannot precede earliest_handover_at_ms")
        return self


class SimulationPlan(StrictModel):
    plan_id: str
    plan_version: int = Field(ge=1)
    base_plan_id: str | None = None
    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str
    status: Literal["READY", "SUPERSEDED", "COMPLETED"] = "READY"
    plan_kind: Literal["INITIAL", "REPLAN"] = "INITIAL"
    replan_reason: ReplanReason | None = None
    replan_requested_at_ms: int | None = Field(default=None, ge=0)
    map_version: str
    coordinate_system: Literal["METERS"] = "METERS"
    source_snapshot_id: str | None = None
    plan_start_sim_time_ms: int = Field(default=0, ge=0)
    effective_from_sim_time_ms: int = Field(default=0, ge=0)
    sim_tick_ms: int = Field(default=100, ge=1)
    makespan_ms: int = Field(default=0, ge=0)
    absolute_finish_at_ms: int = Field(default=0, ge=0)
    robots: list[SimulationRobotPlan] = Field(min_length=1)
    station_reservations: list[StationServiceReservation] = Field(default_factory=list)
    logical_operations: list[SimulationLogicalOperation] = Field(default_factory=list)
    handover_points: list[PlanHandoverPoint] = Field(default_factory=list)
    supersedes_plan_id: str | None = None


class SimulationPlanResponse(StrictModel):
    """Compact API response consumed by the front-end simulator."""

    api_version: Literal["v1"] = "v1"
    status: WorkflowStatus
    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str
    request_mode: RequestMode | None = None
    final_route: RequestFinalRoute | None = None
    effective_planning_mode: PlanningMode | None = None
    planning_mode_source: PlanningModeSource | None = None
    router_llm_executed: bool = False
    plan: SimulationPlan | None = None
    evaluation_id: str | None = None
    frontend_summary: FrontendExecutionSummary | None = None
    pending_human_interaction: HumanInteractionRequest | None = None
    input_rejection: InputRejectionResult | None = None
    workflow_hold: WorkflowHoldResult | None = None
    errors: list[WorkflowError] = Field(default_factory=list)


class ReplanMissionRequest(StrictModel):
    active_plan_id: str
    active_plan_version: int | None = Field(default=None, ge=1)
    replan_at_sim_time_ms: int = Field(
        ge=0,
        validation_alias=AliasChoices("replan_at_sim_time_ms", "sim_time_ms"),
    )
    mission: AutoMissionRequest
    reason: ReplanReason = "NEW_ORDER"
    activation_policy: Literal["PER_ROBOT_HANDOVER", "ALL_ROBOTS_READY"] = (
        "PER_ROBOT_HANDOVER"
    )

    @property
    def sim_time_ms(self) -> int:
        """Backward-compatible internal alias used by older probes."""

        return self.replan_at_sim_time_ms


class PublicReplanMissionRequest(StrictModel):
    active_plan_id: str
    active_plan_version: int | None = Field(default=None, ge=1)
    replan_at_sim_time_ms: int = Field(
        ge=0,
        validation_alias=AliasChoices("replan_at_sim_time_ms", "sim_time_ms"),
    )
    mission: PublicMissionRequest
    reason: ReplanReason = "NEW_ORDER"
    activation_policy: Literal["PER_ROBOT_HANDOVER", "ALL_ROBOTS_READY"] = (
        "PER_ROBOT_HANDOVER"
    )

    @property
    def sim_time_ms(self) -> int:
        return self.replan_at_sim_time_ms

    def to_internal(self) -> ReplanMissionRequest:
        return ReplanMissionRequest(
            active_plan_id=self.active_plan_id,
            active_plan_version=self.active_plan_version,
            replan_at_sim_time_ms=self.replan_at_sim_time_ms,
            mission=self.mission.to_internal(),
            reason=self.reason,
            activation_policy=self.activation_policy,
        )


class OrchestrationResult(StrictModel):
    """Typed final result for every terminal route."""

    warehouse_id: WarehouseId = "WH-001"
    simulation_id: str
    request_mode: RequestMode
    optimization_backend: OptimizationBackend
    planning_mode: PlanningMode
    effective_planning_mode: PlanningMode
    requested_planning_mode: PlanningMode | None = None
    planning_mode_source: PlanningModeSource = "environment"
    status: WorkflowStatus
    workflow_trace: list[str]
    node_execution_log: list[NodeExecutionRecord]
    llm_node_summaries: list[LLMNodeSummary]
    errors: list[WorkflowError]
    events: list[EventInput]
    entry_route_decision: EntryRouteDecision | None = None
    orchestration_plan: OrchestrationPlan | None = None
    normalized_request: NormalizedWarehouseRequest | None = None
    request_gate_decision: RequestGateDecision | None = None
    incident_response_plan: IncidentResponsePlan | None = None
    operator_notifications: list[OperatorNotification] = Field(default_factory=list)
    pending_human_interaction: HumanInteractionRequest | None = None
    human_responses: list[HumanInteractionResponse] = Field(default_factory=list)
    formulation_decision: FormulationDecision | None = None
    structured_key_validation: StructuredKeyValidationResult | None = None
    retrieval_agent_step: RetrievalAgentStep | None = None
    parallel_retrieval_plan: ParallelRetrievalPlan | None = None
    parallel_retrieval_execution: ParallelRetrievalExecutionResult | None = None
    retrieval_agent_step_count: int = 0
    retrieval_agent_retry_count: int = 0
    retrieval_tool_retry_count: int = 0
    retrieval_tool_call_validation: RetrievalToolCallValidationResult | None = None
    resolved_retrieval_tool_request: ResolvedToolRequest | None = None
    current_entity_resolutions: list[EntityResolutionResult] = Field(default_factory=list)
    entity_resolution_history: list[EntityResolutionResult] = Field(default_factory=list)
    resolved_tool_requests: list[ResolvedToolRequest] = Field(default_factory=list)
    retrieval_observations: list[RetrievalObservation] = Field(default_factory=list)
    retrieval_context_sufficiency: RetrievalContextSufficiencyResult | None = None
    validation_issues: list[WorkflowValidationIssue] = Field(default_factory=list)
    validation_issue_history: list[WorkflowValidationIssue] = Field(default_factory=list)
    warehouse_situation_graph: WarehouseSituationGraph | None = None
    situation_graph_validation: SituationGraphValidationResult | None = None
    cuopt_dynamic_input_draft: CuOptDynamicInputDraft | None = None
    cuopt_evidence_enrichment: CuOptEvidenceEnrichmentResult | None = None
    cuopt_dynamic_input_validation: CuOptDynamicInputValidationResult | None = None
    cuopt_dynamic_input_validation_history: list[CuOptDynamicInputValidationResult] = Field(default_factory=list)
    mission_intent: MissionIntent | None = None
    context_snapshot: ContextSnapshot | None = None
    inventory_context: InventoryContext | None = None
    map_context: MapContext | None = None
    robot_context: RobotRuntimeContext | None = None
    effective_mission_spec: MissionSpec | None = None
    policy_validation: PolicyValidationResult | None = None
    goods_to_person_compilation: GoodsToPersonCompilationResult | None = None
    goods_to_person_route_enrichment: GoodsToPersonRouteEnrichmentResult | None = None
    terminal_relocation: TerminalRelocationResult | None = None
    physical_problem_profile: PhysicalProblemProfile | None = None
    planning_route_resolution: PlanningRouteResolution | None = None
    optimization_request: OptimizationRequest | None = None
    execution_payload: CuOptPayload | None = None
    execution_optimizer_result: OptimizerResult | None = None
    cuopt_payload: CuOptPayload | None = None
    payload_validation: PayloadValidationResult | None = None
    candidate_space_validation: CandidateSpaceValidation | None = None
    optimizer_result: OptimizerResult | None = None
    optimizer_assignment_validation: OptimizerAssignmentValidation | None = None
    waypoint_route_expansion: WaypointRouteExpansionResult | None = None
    route_validation: RouteValidationResult | None = None
    traffic_schedule: TrafficScheduleResult | None = None
    mapf_validation: MAPFValidationResult | None = None
    logical_operation_coverage_validation: LogicalOperationCoverageValidationResult | None = None
    goods_to_person_plan: GoodsToPersonPlanResult | None = None
    query_response: QueryResponse | None = None
    clarification: ClarificationResult | None = None
    input_rejection: InputRejectionResult | None = None
    workflow_hold: WorkflowHoldResult | None = None
    human_review: HumanReviewResult | None = None
    failure: WorkflowFailureResult | None = None
    frontend_summary: FrontendExecutionSummary | None = None
    persistence: PersistenceResult | None = None
    dashboard_event: DashboardEvent | None = None
    simulation_plan: SimulationPlan | None = None


class HumanInteractionResumeResult(StrictModel):
    """Result of resolving or rejecting one interaction checkpoint.

    ``terminal_status`` is used when an operator choice intentionally ends the
    current automation run without starting another Rule/Agent/incident graph,
    for example ``HOLD_AND_RECOUNT``.
    """

    interaction_id: str
    interaction_status: HumanInteractionStatus
    resume_outcome: HumanInteractionResumeOutcome
    orchestration_result: OrchestrationResult | None = None
    terminal_status: WorkflowStatus | None = None
    terminal_reason_code: str | None = None
    workflow_hold: WorkflowHoldResult | None = None
    message: str

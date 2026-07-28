from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.time_utils import as_utc_datetime


ExecutionMode = Literal["AUTO", "PLAN_ONLY", "SIMULATE_ONLY", "EXECUTE"]
ExecutionContext = Literal["REAL", "SIMULATION"]
QueryTarget = Literal[
    "ROBOT",
    "INVENTORY",
    "WORK",
    "MAP",
    "PLAN",
    "SIMULATION",
    "REPLAN",
    "VERIFICATION",
    "RESET",
    "EVIDENCE",
    "SYSTEM",
    "NONE",
]
QueryAction = Literal["COUNT", "STATUS", "LIST", "DETAIL", "HISTORY", "NONE"]
PlanMode = Literal[
    "INITIAL_PLAN",
    "INSERT_TASK",
    "LOCAL_REPLAN",
    "GLOBAL_REPLAN",
    "NO_REPLAN",
]
SupervisorTool = Literal[
    "SNAPSHOT",
    "OPTIMIZER",
    "ROUTING",
    "SIMULATION",
    "VERIFICATION",
    "EXECUTION",
]


class ResponseView(str, Enum):
    AUTO = "AUTO"
    COMPACT = "COMPACT"
    FULL = "FULL"


class ReportDetailLevel(str, Enum):
    SUMMARY = "SUMMARY"
    STANDARD = "STANDARD"
    DEBUG = "DEBUG"


class NaturalLanguageCommand(BaseModel):
    command_id: str = Field(default_factory=lambda: str(uuid4()))
    warehouse_id: int
    text: str = Field(min_length=1)
    requested_execution_mode: ExecutionMode = "AUTO"
    simulation_id: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["USER", "SYSTEM_EVENT"] = "USER"
    conversation_id: str | None = None
    parent_command_id: str | None = None
    clarification_id: str | None = None
    report_detail_level: ReportDetailLevel | None = None
    response_view: ResponseView = ResponseView.AUTO
    # What-if 실행기가 구조화된 제약을 기존 Planning Graph에 안전하게
    # 전달하기 위한 내부 필드입니다. 일반 명령에서는 항상 None입니다.
    scenario_definition: dict[str, Any] | None = None

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return as_utc_datetime(value, field_name="received_at")


class SimulationResetRequest(BaseModel):
    actor_id: str | None = None
    reason: str = Field(min_length=1)
    warehouse_id: int | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason은 비어 있을 수 없습니다.")
        return normalized


class StrictStructuredOutputModel(BaseModel):
    """Base model for schemas passed to OpenAI structured output."""

    model_config = ConfigDict(extra="forbid")


class OptimizationWeights(StrictStructuredOutputModel):
    total_distance: float = 1.0
    makespan: float = 1.0
    tardiness: float = 5.0
    energy: float = 1.0
    robot_activation: float = 0.5
    plan_change: float = 2.0
    charging_time: float = 0.2
    charger_wait: float = 0.5
    charger_visit: float = 1.0
    congestion: float = 1.0
    shared_resource_occupancy: float = 0.05
    unnecessary_charger_roundtrip: float = 1.0


InventoryOperationType = Literal["INBOUND", "OUTBOUND"]
InventoryUnit = Literal["BOX"]
InventoryPriority = Literal["NORMAL", "HIGH", "EMERGENCY"]


class InventoryOperationRequest(StrictStructuredOutputModel):
    """One inventory-aware operation extracted from a command or SQL order."""

    operation_id: str = Field(default_factory=lambda: str(uuid4()))
    work_id: str | None = None
    order_id: str | None = None
    operation_type: InventoryOperationType
    item_id: str = Field(min_length=1)
    quantity_boxes: int = Field(gt=0)
    unit: InventoryUnit = "BOX"
    required_at: datetime | None = None
    required_by: datetime | None = None
    expected_arrival_at: datetime | None = None
    expected_available_at: datetime | None = None
    actual_arrival_at: datetime | None = None
    actual_available_at: datetime | None = None
    storage_node_id: int | None = None
    lot_id: str | None = None
    warehouse_item_id: str | None = None
    priority: InventoryPriority = "NORMAL"
    allow_partial_fulfillment: bool = False
    source: Literal["COMMAND", "SQL_ORDER", "WORK"] = "COMMAND"

    @field_validator(
        "required_at",
        "required_by",
        "expected_arrival_at",
        "expected_available_at",
        "actual_arrival_at",
        "actual_available_at",
    )
    @classmethod
    def normalize_inventory_time(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="inventory_operation_time")

    @model_validator(mode="after")
    def synchronize_required_time(self) -> "InventoryOperationRequest":
        # `required_at` and `required_by` are deadline aliases in the execution
        # contract.  LLM structured output may express a time window by placing
        # the start in required_at and the end in required_by.  Rejecting that
        # shape forces an unnecessary rule fallback, so normalize both aliases
        # to the later instant (the business completion deadline).  The actual
        # start/end window is carried by scheduled_task_constraints.
        values = [value for value in (self.required_at, self.required_by) if value]
        resolved = max(values) if values else None
        self.required_at = resolved
        self.required_by = resolved
        return self


class InventoryProjectionPoint(StrictStructuredOutputModel):
    at: datetime
    item_id: str
    quantity_boxes: int
    quantity_delta_boxes: int = 0
    event_type: str
    source_id: str | None = None
    precedence: int

    @field_validator("at")
    @classmethod
    def normalize_projection_time(cls, value: datetime) -> datetime:
        return as_utc_datetime(value, field_name="projection_at")


class LotAllocation(StrictStructuredOutputModel):
    warehouse_item_id: str
    item_id: str | None = None
    lot_id: str | None = None
    quantity_boxes: int = Field(gt=0)
    storage_node_id: int | None = None
    available_at: datetime | None = None
    source_type: Literal["CURRENT_LOT", "FUTURE_INBOUND"] = "CURRENT_LOT"
    inbound_source_id: str | None = None

    @field_validator("available_at")
    @classmethod
    def normalize_lot_available_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="lot_available_at")


class ItemInventoryResult(StrictStructuredOutputModel):
    operation_id: str
    work_id: str | None = None
    order_id: str | None = None
    operation_type: InventoryOperationType
    item_id: str
    requested_quantity_boxes: int = Field(ge=0)
    planned_quantity_boxes: int = Field(ge=0)
    available_quantity_boxes: int = Field(ge=0)
    shortage_quantity_boxes: int = Field(ge=0)
    required_at: datetime | None = None
    earliest_full_fulfillment_at: datetime | None = None
    status: Literal[
        "PASS",
        "PARTIAL_FULFILLMENT_APPROVED",
        "INVENTORY_SHORTAGE",
        "EMERGENCY_REVIEW_REQUIRED",
        "NOT_APPLICABLE",
    ]
    allow_partial_fulfillment: bool = False
    projection: list[InventoryProjectionPoint] = Field(default_factory=list)
    lot_allocations: list[LotAllocation] = Field(default_factory=list)


class InventoryFeasibilityResult(StrictStructuredOutputModel):
    status: Literal[
        "PASS",
        "FAILED",
        "PARTIAL_SUCCESS",
        "NOT_APPLICABLE",
    ]
    valid: bool
    partial_success: bool = False
    item_results: list[ItemInventoryResult] = Field(default_factory=list)
    shortage_work_ids: list[str] = Field(default_factory=list)
    blocked_work_ids: list[str] = Field(default_factory=list)
    independent_work_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class InventoryReservationSummary(StrictStructuredOutputModel):
    reservation_id: str
    warehouse_id: int
    item_id: str
    quantity_boxes: int = Field(gt=0)
    consumed_quantity_boxes: int = Field(default=0, ge=0)
    remaining_quantity_boxes: int | None = Field(default=None, ge=0)
    work_id: str
    order_id: str | None = None
    plan_version: str
    scope: Literal["SIMULATION", "ACTIVE_PLAN"]
    status: Literal["RESERVED", "CONSUMED", "RELEASED", "EXPIRED", "CANCELLED"]
    reserved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    required_at: datetime | None = None
    expires_at: datetime | None = None
    simulation_id: str | None = None
    idempotency_key: str
    lot_allocations: list[LotAllocation] = Field(default_factory=list)

    @field_validator("reserved_at", "required_at", "expires_at")
    @classmethod
    def normalize_reservation_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="reservation_time")


class CapacityFeasibilityResult(StrictStructuredOutputModel):
    status: Literal["PASS", "FAILED", "NOT_CONFIGURED", "NOT_APPLICABLE"]
    capacity_value: float | None = None
    capacity_unit: str | None = None
    capacity_type: str | None = None
    usable_capacity_value: float | None = None
    warnings: list[str] = Field(default_factory=list)


class EmergencyReviewItem(StrictStructuredOutputModel):
    item_id: str
    work_id: str | None = None
    requested_quantity_boxes: int
    available_quantity_boxes: int
    shortage_quantity_boxes: int
    required_at: datetime | None = None
    earliest_full_fulfillment_at: datetime | None = None
    blocked_work_ids: list[str] = Field(default_factory=list)
    independent_work_ids: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class ClosedEdgeAssumption(StrictStructuredOutputModel):
    from_node: int
    to_node: int
    bidirectional: bool = False


class HypotheticalEventParameters(StrictStructuredOutputModel):
    """Supported numeric inputs for a hypothetical operating event.

    Event identity and affected resources remain in ``event_type`` and
    ``target_ids``.  These optional values replace the previous unrestricted
    ``dict[str, Any]`` while keeping the public ``parameters`` object.
    """

    battery_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    delay_seconds: int | None = Field(default=None, ge=0)
    inventory_quantity: int | None = Field(default=None, ge=0)

    @model_serializer(mode="plain")
    def serialize_non_null_parameters(self) -> dict[str, float | int]:
        return {
            name: value
            for name in (
                "battery_percent",
                "delay_seconds",
                "inventory_quantity",
            )
            if (value := getattr(self, name)) is not None
        }


class HypotheticalEvent(StrictStructuredOutputModel):
    event_type: Literal[
        "ROBOT_FAILURE",
        "LOW_BATTERY",
        "NODE_CLOSURE",
        "EDGE_CLOSURE",
        "CHARGER_UNAVAILABLE",
        "URGENT_ORDER",
        "TASK_DELAY",
        "INVENTORY_SHORTAGE",
    ]
    target_ids: list[str] = Field(default_factory=list)
    parameters: HypotheticalEventParameters = Field(
        default_factory=HypotheticalEventParameters
    )


class ScenarioDefinition(BaseModel):
    scenario_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    description: str | None = None
    robot_limit: int | None = Field(default=None, ge=1)
    excluded_robot_ids: list[str] = Field(default_factory=list)
    excluded_node_ids: list[int] = Field(default_factory=list)
    excluded_edge_ids: list[str] = Field(default_factory=list)
    fixed_robot_assignments: dict[str, str] = Field(default_factory=dict)
    optimization_priority: str | None = None
    optimization_weights: dict[str, float] = Field(default_factory=dict)
    hypothetical_events: list[dict[str, Any]] = Field(default_factory=list)
    source_plan_version: str | None = None
    source_plan_snapshot: dict[str, Any] | None = None
    affected_robot_ids: list[str] = Field(default_factory=list)
    affected_task_ids: list[str] = Field(default_factory=list)
    protected_task_ids: list[str] = Field(default_factory=list)
    changeable_task_ids: list[str] = Field(default_factory=list)
    freeze_horizon_seconds: int | None = Field(default=None, ge=0)
    robot_state_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    robot_failure_recovery: dict[str, Any] = Field(default_factory=dict)
    recovery_tasks: list[dict[str, Any]] = Field(default_factory=list)
    recovery_replace_task_ids: list[str] = Field(default_factory=list)


class ScenarioComparisonRequest(BaseModel):
    comparison_id: str | None = None
    idempotency_key: str | None = None
    warehouse_id: int
    conversation_id: str | None = None
    text: str | None = None
    scenarios: list[ScenarioDefinition] = Field(default_factory=list)
    optimization_priority: str | None = None
    max_scenarios: int = Field(default=4, ge=2, le=6)

    @model_validator(mode="after")
    def require_text_or_scenarios(self) -> "ScenarioComparisonRequest":
        if not (self.text and self.text.strip()) and not self.scenarios:
            raise ValueError("text 또는 scenarios가 필요합니다.")
        return self


class ScenarioResult(BaseModel):
    scenario_id: str
    simulation_id: str | None = None
    command_id: str
    valid: bool
    verification_decision: str
    robot_count: int = 0
    assigned_task_count: int = 0
    unassigned_task_count: int = 0
    total_distance: float | None = None
    makespan_seconds: int | None = None
    tardiness_seconds: int | None = None
    energy: float | None = None
    conflict_count: int | None = None
    wait_count: int | None = None
    plan_change_count: int | None = None
    replan_attempts: int = 0
    warnings: list[str] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)


class ScenarioComparisonResult(BaseModel):
    comparison_id: str
    conversation_id: str | None = None
    warehouse_id: int
    scenarios: list[ScenarioResult] = Field(default_factory=list)
    comparison_metrics: list[dict[str, Any]] = Field(default_factory=list)
    recommended_scenario_id: str | None = None
    recommendation_evidence: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    status: Literal[
        "PROCESSING",
        "COMPLETED",
        "PARTIAL_SUCCESS",
        "FAILED",
        "CLARIFICATION_REQUIRED",
    ]


class EventImpactAnalysis(BaseModel):
    event_id: str
    trigger_type: str
    trigger_source: Literal["REAL", "SIMULATION", "HYPOTHETICAL"]
    affected_robot_ids: list[str] = Field(default_factory=list)
    affected_task_ids: list[str] = Field(default_factory=list)
    affected_node_ids: list[int] = Field(default_factory=list)
    affected_edge_ids: list[str] = Field(default_factory=list)
    recommended_scope: Literal[
        "NO_REPLAN",
        "LOCAL_REPLAN",
        "GLOBAL_REPLAN",
    ]
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    approval_required: bool
    evidence: list[str] = Field(default_factory=list)
    active_plan_version: str | None = None
    completed_task_ids: list[str] = Field(default_factory=list)
    frozen_task_ids: list[str] = Field(default_factory=list)
    changeable_task_ids: list[str] = Field(default_factory=list)
    freeze_horizon_seconds: int = Field(default=15, ge=0)
    partial_replan_policy: str = "FREEZE_COMPLETED_EXECUTING_AND_NEAR_TERM"
    robot_state_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    robot_failure_recovery: dict[str, Any] = Field(default_factory=dict)
    failure_signature: str


class EventReplanDecisionRequest(BaseModel):
    actor_id: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("reason은 비어 있을 수 없습니다.")
        return result


class PlanExecutionApprovalRequest(BaseModel):
    warehouse_id: int
    actor_id: str = Field(default="SYSTEM_VERIFICATION", min_length=1)
    reason: str = Field(min_length=1)
    expected_active_plan_version: str | None = None

    @field_validator("actor_id", "reason")
    @classmethod
    def normalize_execution_approval_text(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("값은 비어 있을 수 없습니다.")
        return result


class PlanExecutionDispatchRequest(BaseModel):
    warehouse_id: int
    max_attempts: int = Field(default=3, ge=1, le=10)
    dispatch_all_ready: bool = True


class RobotCommandAckRequest(BaseModel):
    ack_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    status: Literal["ACKED", "FAILED"]
    error_code: str | None = None
    error_message: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("ack_id", "plan_version", "robot_id", "command_id")
    @classmethod
    def normalize_ack_identity(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("ACK 식별값은 비어 있을 수 없습니다.")
        return result


class ExecutionDispatchCancelRequest(BaseModel):
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("actor_id", "reason")
    @classmethod
    def normalize_cancel_text(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("값은 비어 있을 수 없습니다.")
        return result


class ExecutionDispatchRetryRequest(BaseModel):
    actor_id: str = Field(default="SYSTEM_RETRY", min_length=1)
    reason: str = Field(default="gateway timeout retry", min_length=1)


class FixedRobotAssignment(StrictStructuredOutputModel):
    task_id: str
    robot_id: str


DependencyType = Literal["FINISH_TO_START"]
TimeConstraintType = Literal["HARD_WINDOW", "DEADLINE", "SOFT_WINDOW", "ASAP"]
PreemptionPolicy = Literal[
    "NON_PREEMPTIVE",
    "REQUIRE_SAFE_STOP_CONFIRMATION",
]
InsertionPolicy = Literal["NORMAL", "ASAP", "URGENT"]


class TaskScheduleConstraint(StrictStructuredOutputModel):
    work_id: str
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    time_constraint_type: TimeConstraintType = "ASAP"
    fixed_robot_id: str | None = None
    same_robot_group: str | None = None
    sequence_group: str | None = None
    sequence_order: int | None = Field(default=None, ge=1)

    @field_validator("earliest_start", "latest_finish")
    @classmethod
    def normalize_schedule_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="schedule_time")

    @model_validator(mode="after")
    def validate_window(self) -> "TaskScheduleConstraint":
        if (
            self.earliest_start is not None
            and self.latest_finish is not None
            and self.earliest_start > self.latest_finish
        ):
            raise ValueError("earliest_start는 latest_finish보다 늦을 수 없습니다.")
        return self


class TaskDependency(StrictStructuredOutputModel):
    predecessor_work_id: str
    successor_work_id: str
    dependency_type: DependencyType = "FINISH_TO_START"
    lag_seconds: int = Field(default=0, ge=0)


class SameRobotGroup(StrictStructuredOutputModel):
    group_id: str
    work_ids: list[str] = Field(min_length=2)


class ScheduleParseResult(StrictStructuredOutputModel):
    constraints: list[TaskScheduleConstraint] = Field(default_factory=list)
    dependencies: list[TaskDependency] = Field(default_factory=list)
    same_robot_groups: list[SameRobotGroup] = Field(default_factory=list)
    insertion_policy: InsertionPolicy = "NORMAL"
    preemption_policy: PreemptionPolicy = "NON_PREEMPTIVE"
    daily_schedule_requested: bool = False
    timezone_name: str
    timezone_defaulted: bool = False
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class PlanningReference(StrictStructuredOutputModel):
    """A user-visible planning clock, normalized for internal comparison."""

    original_text: str
    local_at: datetime
    utc_at: datetime
    timezone: str
    source: Literal[
        "USER_COMMAND",
        "INHERITED",
        "SNAPSHOT_CAPTURED_AT",
        "SYSTEM_CURRENT_TIME",
    ]

    @field_validator("utc_at")
    @classmethod
    def normalize_reference_time(cls, value: datetime) -> datetime:
        return as_utc_datetime(value, field_name="planning_reference_at")


class InventoryQuantityFilter(StrictStructuredOutputModel):
    """A deterministic quantity predicate for an inventory query."""

    field: Literal["available_quantity_boxes"]
    operator: Literal["LT", "LTE", "GT", "GTE"]
    value: int = Field(ge=0)
    unit: Literal["BOX"] = "BOX"


class CommandInterpretation(StrictStructuredOutputModel):
    command_kind: Literal["QUERY", "PLAN", "EXECUTE"]
    intent: Literal[
        "DAILY_PLAN",
        "INSERT_TASK",
        "LOCAL_REPLAN",
        "GLOBAL_REPLAN",
        "EXECUTE",
        "OUTBOUND",
        "INBOUND",
        "RELOCATION",
        "ROBOT_QUERY",
        "INVENTORY_QUERY",
        "WORK_QUERY",
        "MAP_QUERY",
        "SYSTEM_QUERY",
        "ROUTE_QUERY",
        "PLAN_QUERY",
        "SIMULATION_QUERY",
        "REPLAN_QUERY",
        "VERIFICATION_QUERY",
        "RESET_QUERY",
        "EVIDENCE_QUERY",
        "HYPOTHETICAL_SCENARIO",
        "SCENARIO_COMPARISON",
        "OTHER",
    ]
    objective: str
    query_target: QueryTarget = "NONE"
    query_action: QueryAction = "NONE"
    item_ids: list[str] = Field(default_factory=list)
    quantity: int | None = Field(default=None, ge=1)
    inventory_operations: list[InventoryOperationRequest] = Field(
        default_factory=list
    )
    load_open_inventory_orders: bool = False
    source_node_ids: list[int] = Field(default_factory=list)
    target_node_ids: list[int] = Field(default_factory=list)
    target_node_type: str | None = None
    deadline: datetime | None = None
    planning_reference: PlanningReference | None = None
    priority: Literal["NORMAL", "HIGH", "EMERGENCY"] = "NORMAL"
    required_sql_reads: list[
        Literal["INVENTORY", "ROBOTS", "WORKS"]
    ] = Field(default_factory=list)
    required_graph_reads: list[
        Literal["TOPOLOGY", "SPECIAL_NODES", "NODE_VALIDATION", "STORAGE_NODES"]
    ] = Field(default_factory=list)
    execution_mode: Literal["PLAN_ONLY", "SIMULATE_ONLY", "EXECUTE"]
    optimization_weights: OptimizationWeights = Field(default_factory=OptimizationWeights)
    hard_constraints: list[str] = Field(default_factory=list)
    assumed_closed_node_ids: list[int] = Field(default_factory=list)
    assumed_closed_edges: list[ClosedEdgeAssumption] = Field(default_factory=list)
    target_warehouse_id: int | None = None
    target_robot_ids: list[str] = Field(default_factory=list)
    target_task_ids: list[str] = Field(default_factory=list)
    target_simulation_ids: list[str] = Field(default_factory=list)
    target_plan_versions: list[str] = Field(default_factory=list)
    extracted_robot_ids: list[str] = Field(default_factory=list)
    extracted_task_ids: list[str] = Field(default_factory=list)
    verified_robot_ids: list[str] = Field(default_factory=list)
    verified_task_ids: list[str] = Field(default_factory=list)
    invalid_robot_ids: list[str] = Field(default_factory=list)
    invalid_task_ids: list[str] = Field(default_factory=list)
    excluded_robot_ids: list[str] = Field(default_factory=list)
    included_robot_ids: list[str] = Field(default_factory=list)
    excluded_node_ids: list[int] = Field(default_factory=list)
    excluded_edge_ids: list[str] = Field(default_factory=list)
    fixed_robot_assignments: list[FixedRobotAssignment] = Field(default_factory=list)
    scheduled_task_constraints: list[TaskScheduleConstraint] = Field(
        default_factory=list
    )
    task_dependencies: list[TaskDependency] = Field(default_factory=list)
    insertion_policy: InsertionPolicy = "NORMAL"
    preemption_policy: PreemptionPolicy = "NON_PREEMPTIVE"
    same_robot_groups: list[SameRobotGroup] = Field(default_factory=list)
    daily_schedule_requested: bool = False
    robot_limit: int | None = Field(default=None, ge=1)
    optimization_priority: str | None = None
    hypothetical_events: list[HypotheticalEvent] = Field(default_factory=list)
    query_filters: list[str | InventoryQuantityFilter] = Field(default_factory=list)
    comparison_requested: bool = False
    comparison_dimensions: list[str] = Field(default_factory=list)
    requires_future_feature: bool = False
    ambiguous_terms: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    missing_information: list[str] = Field(default_factory=list)
    summary: str

    @field_validator("deadline")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="deadline")


class SupervisorDecision(StrictStructuredOutputModel):
    """자연어 해석을 실행 흐름으로 바꾸는 상위 판단 결과입니다.

    이 모델은 실행 도구와 계획 범위만 표현하며 로봇 배정, 경로 또는
    계산 수치를 포함하지 않습니다.
    """

    intent: str
    command_kind: Literal["QUERY", "PLAN", "EXECUTE"]
    execution_mode: Literal["PLAN_ONLY", "SIMULATE_ONLY", "EXECUTE"]
    required_tools: list[SupervisorTool] = Field(default_factory=list)
    plan_mode: PlanMode
    requires_clarification: bool = False
    clarification_reason: str | None = None
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    allow_replan: bool = True
    max_replan_attempts: int = Field(default=2, ge=0, le=3)
    next_node: Literal["SNAPSHOT", "REPORT"] = "SNAPSHOT"
    reasoning_summary: str


class VerificationDecision(StrictStructuredOutputModel):
    """결정론적 검증 evidence를 종합한 독립 검증 판단입니다."""

    decision: Literal[
        "PASS",
        "PASS_WITH_WARNING",
        "REPLAN_LOCAL",
        "REPLAN_GLOBAL",
        "CLARIFICATION_REQUIRED",
        "FAIL",
    ]
    requires_replan: bool
    replan_scope: Literal["NO_REPLAN", "LOCAL_REPLAN", "GLOBAL_REPLAN"]
    affected_robot_ids: list[str] = Field(default_factory=list)
    affected_task_ids: list[str] = Field(default_factory=list)
    blocking_findings: list[str] = Field(default_factory=list)
    warning_findings: list[str] = Field(default_factory=list)
    user_visible_warnings: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str


class ReplanHistoryEntry(BaseModel):
    """한 command 안에서 수행된 재계획 시도의 변경 이력입니다."""

    attempt: int = Field(ge=1, le=3)
    scope: Literal["LOCAL_REPLAN", "GLOBAL_REPLAN"]
    reason: str
    affected_robot_ids: list[str] = Field(default_factory=list)
    affected_task_ids: list[str] = Field(default_factory=list)
    protected_task_ids: list[str] = Field(default_factory=list)
    previous_plan_version: str
    new_plan_version: str
    verification_before: Literal["REPLAN_LOCAL", "REPLAN_GLOBAL"]
    verification_after: Literal[
        "PASS",
        "PASS_WITH_WARNING",
        "REPLAN_LOCAL",
        "REPLAN_GLOBAL",
        "CLARIFICATION_REQUIRED",
        "FAIL",
    ] | None = None
    failure_signature: str
    status: Literal["STARTED", "COMPLETED", "FAILED"] = "STARTED"


class FinalReportOutput(StrictStructuredOutputModel):
    answer: str


class AssignmentSummary(StrictStructuredOutputModel):
    work_id: str
    robot_id: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    status_code: str
    status_label: str
    dependency_label: str | None = None
    is_inserted: bool = False
    is_preserved: bool = False
    is_shifted: bool = False


class DependencySummary(StrictStructuredOutputModel):
    predecessor_work_id: str
    successor_work_id: str
    relationship_label: str


class ScheduleChangeSummary(StrictStructuredOutputModel):
    inserted_work_ids: list[str] = Field(default_factory=list)
    preserved_work_ids: list[str] = Field(default_factory=list)
    shifted_work_ids: list[str] = Field(default_factory=list)
    blocked_work_ids: list[str] = Field(default_factory=list)
    previous_plan_version: str | None = None
    new_plan_version: str | None = None
    hard_window_violation: bool = False
    deadline_violation: bool = False


class UserVisibleWarning(StrictStructuredOutputModel):
    code: str
    message: str


class UserVisibleIssue(StrictStructuredOutputModel):
    code: str
    message: str
    action: str | None = None


class UserReportSummary(StrictStructuredOutputModel):
    report_level: ReportDetailLevel
    outcome: Literal[
        "SUCCESS",
        "SUCCESS_WITH_WARNING",
        "PARTIAL_SUCCESS_WITH_EMERGENCY",
        "FAILED",
        "CLARIFICATION_REQUIRED",
    ]
    title: str
    primary_message: str
    execution_mode_label: str | None = None
    plan_mode_label: str | None = None
    assignment_summaries: list[AssignmentSummary] = Field(default_factory=list)
    dependency_summaries: list[DependencySummary] = Field(default_factory=list)
    schedule_change_summary: ScheduleChangeSummary | None = None
    total_distance: float | None = None
    distance_unit: str | None = None
    schedule_completion_at: datetime | None = None
    active_work_duration_seconds: int | None = None
    elapsed_until_completion_seconds: int | None = None
    tardiness_seconds: int | float | None = None
    conflict_count: int | None = None
    warnings: list[UserVisibleWarning] = Field(default_factory=list)
    issues: list[UserVisibleIssue] = Field(default_factory=list)
    inventory_feasibility: InventoryFeasibilityResult | None = None
    emergency_review_items: list[EmergencyReviewItem] = Field(default_factory=list)
    recommended_action: str | None = None


class ClarificationOption(BaseModel):
    value: str
    label: str
    description: str | None = None


class ClarificationRequest(BaseModel):
    clarification_id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str | None = None
    command_id: str
    status: Literal[
        "CLARIFICATION_REQUIRED",
        "RESOLVED",
        "EXPIRED",
    ] = "CLARIFICATION_REQUIRED"
    reason_code: str
    question: str
    missing_fields: list[str] = Field(default_factory=list)
    ambiguous_fields: list[str] = Field(default_factory=list)
    options: list[ClarificationOption] = Field(default_factory=list)
    original_text: str
    expires_at: datetime | None = None


class ClarificationResponse(BaseModel):
    selected_value: str | None = None
    text: str | None = None
    conversation_id: str | None = None

    @model_validator(mode="after")
    def require_response_value(self) -> "ClarificationResponse":
        if not (self.selected_value or (self.text and self.text.strip())):
            raise ValueError("selected_value 또는 text 중 하나가 필요합니다.")
        return self


class ConversationContext(BaseModel):
    conversation_id: str
    warehouse_id: int
    status: Literal["ACTIVE", "CLOSED"] = "ACTIVE"
    active_command_id: str | None = None
    previous_command_id: str | None = None
    active_plan_version: str | None = None
    active_simulation_id: str | None = None
    active_clarification_id: str | None = None
    resolved_constraints: dict[str, Any] = Field(default_factory=dict)
    inherited_constraints: dict[str, Any] = Field(default_factory=dict)
    latest_result_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScopeDecision(StrictStructuredOutputModel):
    plan_mode: PlanMode
    affected_task_ids: list[str] = Field(default_factory=list)
    affected_robot_ids: list[str] = Field(default_factory=list)
    fixed_task_ids: list[str] = Field(default_factory=list)
    changeable_task_ids: list[str] = Field(default_factory=list)
    freeze_horizon_seconds: int = Field(default=15, ge=0)
    include_new_command: bool = True
    optimization_goal: str
    reason_summary: str


class AtomicTask(BaseModel):
    task_id: str
    work_id: str | None = None
    action: Literal["MOVE", "PICK", "DROP", "CHARGE"]
    item_id: str | None = None
    quantity: int = Field(default=0, ge=0)
    source_candidates: list[int] = Field(default_factory=list)
    target_candidates: list[int] = Field(default_factory=list)
    priority: int = Field(default=5, ge=1, le=100)
    deadline: datetime | None = None
    predecessors: list[str] = Field(default_factory=list)
    dependencies: list[TaskDependency] = Field(default_factory=list)
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    time_constraint_type: TimeConstraintType = "ASAP"
    same_robot_group: str | None = None
    frozen: bool = False
    assigned_robot_id: str | None = None
    inventory_allocations: list[dict[str, Any]] = Field(default_factory=list)
    inventory_transition_policy: Literal[
        "STANDARD",
        "NO_STOCK_DELTA",
    ] = "STANDARD"

    @field_validator("deadline", "earliest_start", "latest_finish")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="deadline")


class ScheduledTask(BaseModel):
    task_id: str
    work_id: str | None = None
    action: Literal["MOVE", "PICK", "DROP", "CHARGE"] = "MOVE"
    robot_id: str
    source_node: int
    target_node: int
    start_time_step: int = Field(ge=0)
    end_time_step: int = Field(ge=0)
    priority: int = 5
    estimated_distance: float = Field(default=0.0, ge=0)
    estimated_energy: float = Field(default=0.0, ge=0)
    charge_target_battery: float | None = Field(default=None, ge=0, le=100)
    charged_percent: float = Field(default=0.0, ge=0, le=100)
    charge_duration_seconds: int | None = Field(default=None, ge=0)
    charger_cost: float | None = Field(default=None, ge=0)
    charger_selection_policy: str | None = None
    charger_selection_reason: str | None = None
    charger_candidates: list[dict[str, Any]] = Field(default_factory=list)
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    schedule_status: Literal[
        "SCHEDULED",
        "WAITING_FOR_PREDECESSOR",
        "READY",
        "DISPATCHED",
        "EXECUTING",
        "COMPLETED",
        "FAILED",
        "BLOCKED",
    ] = "SCHEDULED"

    @field_validator("planned_start_at", "planned_end_at")
    @classmethod
    def normalize_planned_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return as_utc_datetime(value, field_name="planned_time")


class CuOptPlan(BaseModel):
    scheduled_tasks: list[ScheduledTask]
    unassigned_task_ids: list[str] = Field(default_factory=list)
    changed_robot_ids: list[str] = Field(default_factory=list)
    objective_value: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class OptimizationCandidateEvidence(BaseModel):
    task_id: str
    robot_id: str
    feasible: bool
    selected: bool = False
    robot_start_node: int | None = None
    source_node: int | None = None
    target_node: int | None = None
    distance: float | None = None
    duration_time_steps: int | None = None
    end_time_step: int | None = None
    energy: float | None = None
    tardiness_time_steps: int | None = None
    activation_indicator: int | None = None
    plan_change_indicator: int | None = None
    robot_activation_cost: float | None = None
    plan_change_cost: float | None = None
    incremental_objective: float | None = None
    objective_components: dict[str, float] = Field(default_factory=dict)
    rejection_reason: str | None = None


class TaskOptimizationEvidence(BaseModel):
    task_id: str
    task_order: int = Field(ge=1)
    priority: int
    selection_mode: str
    tie_break_rule: list[str] = Field(default_factory=list)
    candidate_count: int = Field(ge=0)
    selected_robot_id: str | None = None
    selected_source_node: int | None = None
    selected_target_node: int | None = None
    candidates: list[OptimizationCandidateEvidence] = Field(default_factory=list)


class ObjectiveBreakdown(BaseModel):
    total_distance: float
    makespan_time_steps: int
    tardiness_time_steps: int
    total_energy: float
    active_robot_count: int
    plan_changes: int
    distance_component: float
    makespan_component: float
    tardiness_component: float
    energy_component: float
    robot_activation_component: float
    plan_change_component: float
    total: float
    weights: dict[str, float] = Field(default_factory=dict)


class TimedWaypoint(BaseModel):
    node_id: int
    time_step: int = Field(ge=0)
    action: Literal["MOVE", "WAIT", "PICK", "DROP", "CHARGE"] = "MOVE"


class RobotCommand(BaseModel):
    command_id: str
    sequence: int = Field(ge=1)
    plan_version: str
    warehouse_id: int
    robot_id: str
    task_id: str | None = None
    work_id: str | None = None
    action: Literal["START", "MOVE", "WAIT", "CHARGE", "PICKUP", "DROPOFF", "STOP"]
    node_id: int
    time_step: int = Field(ge=0)
    time_step_seconds: int = Field(ge=1)
    execute_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RobotCommandBatch(BaseModel):
    plan_version: str
    warehouse_id: int
    robot_id: str
    commands: list[RobotCommand] = Field(min_length=1)
    command_count: int = Field(ge=1)

    @model_validator(mode="after")
    def command_count_matches(self) -> "RobotCommandBatch":
        if self.command_count != len(self.commands):
            raise ValueError("command_count must match commands")
        return self


class TimedRoute(BaseModel):
    robot_id: str
    task_ids: list[str]
    waypoints: list[TimedWaypoint]
    distance: float = 0.0


class CollisionFreePlan(BaseModel):
    engine: Literal["EXTERNAL_CBS", "PRIORITIZED_TIME_ASTAR"]
    routes: list[TimedRoute]
    time_step_seconds: int
    total_distance: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class RouteSegmentEvidence(BaseModel):
    from_node: int
    to_node: int
    depart_step: int = Field(ge=0)
    arrive_step: int = Field(ge=0)
    action: Literal["MOVE", "WAIT"]
    distance: float | None = Field(default=None, ge=0)
    travel_steps: int = Field(ge=0)
    edge_identifier: str | None = None
    source: Literal[
        "NEO4J_SNAPSHOT",
        "PRESERVED_ACTIVE_PLAN",
        "INTERNAL_ROUTE_SEARCH",
    ]


class RobotRouteEvidence(BaseModel):
    robot_id: str
    task_ids: list[str] = Field(default_factory=list)
    segments: list[RouteSegmentEvidence] = Field(default_factory=list)
    segment_distance: float
    route_distance: float
    distance_consistent: bool


class RoutingEvidence(BaseModel):
    engine: str
    route_segment_count: int = Field(ge=0)
    complete: bool
    issues: list[str] = Field(default_factory=list)
    routes: list[RobotRouteEvidence] = Field(default_factory=list)


class WaitEvidence(BaseModel):
    robot_id: str
    task_id: str | None = None
    node_id: int
    time_step: int = Field(ge=0)
    reason: str
    conflict_type: str | None = None
    blocked_resource: str | None = None
    blocked_by_robot_id: str | None = None
    blocked_by_task_id: str | None = None
    added_delay_steps: int = Field(default=1, ge=0)


class ReservationEvidence(BaseModel):
    vertex_reservation_count: int = Field(ge=0)
    edge_reservation_count: int = Field(ge=0)
    wait_count: int = Field(ge=0)
    reroute_count: int | None = Field(default=None, ge=0)
    final_conflict_count: int | None = Field(default=None, ge=0)
    waits: list[WaitEvidence] = Field(default_factory=list)
    resolution_events: list[dict[str, Any]] = Field(default_factory=list)
    idle_action_task_count: int = Field(default=0, ge=0)
    idle_action_tasks: list[dict[str, Any]] = Field(default_factory=list)
    idle_policy: dict[str, Any] = Field(default_factory=dict)


class RobotDistanceDifference(BaseModel):
    robot_id: str
    estimated_distance: float
    final_distance: float
    difference: float
    reason_code: Literal[
        "RESERVATION_WAIT",
        "CONFLICT_AVOIDANCE_DETOUR",
        "START_POSITION_CONNECTION",
        "MULTI_TASK_TRANSITION",
        "ESTIMATED_DISTANCE_APPROXIMATION",
        "TIME_OPTIMAL_ROUTE_DISTANCE_VARIANCE",
        "PRESERVED_ROUTE",
        "UNKNOWN",
    ] = "UNKNOWN"


class DistanceComparison(BaseModel):
    optimizer_estimated_distance: float
    routing_final_distance: float
    difference: float
    difference_percent: float | None = None
    robot_differences: list[RobotDistanceDifference] = Field(default_factory=list)


class SimulationIssue(BaseModel):
    code: str
    message: str
    robot_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    node_ids: list[int] = Field(default_factory=list)
    time_steps: list[int] = Field(default_factory=list)


class SimulationResult(BaseModel):
    success: bool
    valid: bool
    status: Literal["SUCCESS", "FAILED"]
    issues: list[SimulationIssue] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    total_distance: float = 0.0
    makespan: int = 0
    tardiness: float = 0.0
    robot_routes: list[dict[str, Any]] = Field(default_factory=list)
    task_assignments: list[dict[str, Any]] = Field(default_factory=list)
    conflict_count: int = 0
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class InventoryDelta(BaseModel):
    warehouse_item_id: str
    quantity_delta: int


class RobotEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    warehouse_id: int
    robot_id: str
    work_id: str | None = None
    task_id: str | None = None
    event_type: Literal[
        "POSITION_UPDATED",
        "TASK_STARTED",
        "TASK_COMPLETED",
        "TASK_FAILED",
        "ROBOT_DELAYED",
        "ROBOT_FAILED",
        "LOW_BATTERY",
        "PATH_BLOCKED",
        "PATH_DEVIATED",
        "INBOUND_AVAILABLE",
    ]
    node_id: int | None = None
    # Robot telemetry is operational input.  Reject impossible values before
    # an event can reach either Redis or PostgreSQL.
    battery: float | None = Field(default=None, ge=0, le=100)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    inventory_deltas: list[InventoryDelta] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    execution_context: ExecutionContext = "REAL"
    simulation_id: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return as_utc_datetime(value, field_name="occurred_at")

    @model_validator(mode="after")
    def validate_event_context(self) -> "RobotEvent":
        if self.execution_context == "SIMULATION" and not self.simulation_id:
            raise ValueError("SIMULATION 이벤트에는 simulation_id가 필요합니다.")
        if self.execution_context == "REAL" and self.simulation_id is not None:
            raise ValueError("REAL 이벤트의 simulation_id는 null이어야 합니다.")
        if self.event_type == "TASK_COMPLETED":
            if self.execution_context == "REAL" and not self.work_id:
                raise ValueError("REAL TASK_COMPLETED에는 work_id가 필요합니다.")
            if (
                self.execution_context == "SIMULATION"
                and not self.work_id
                and not self.task_id
            ):
                raise ValueError(
                    "SIMULATION TASK_COMPLETED에는 work_id 또는 task_id가 필요합니다."
                )
        if (
            self.event_type == "TASK_FAILED"
            and self.execution_context == "REAL"
            and not self.work_id
        ):
            raise ValueError("REAL TASK_FAILED에는 work_id가 필요합니다.")
        if self.event_type == "LOW_BATTERY":
            payload_battery = self.payload.get("battery")
            if self.battery is None and payload_battery is None:
                raise ValueError("LOW_BATTERY 이벤트에는 battery가 필요합니다.")
        if self.event_type in {"POSITION_UPDATED", "PATH_DEVIATED"} and self.node_id is None:
            raise ValueError(f"{self.event_type} 이벤트에는 node_id가 필요합니다.")
        return self

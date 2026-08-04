"""Contracts for Agent-generated BE fulfillment command batches."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.schemas import StrictModel


FulfillmentMode = Literal["AUTO", "INBOUND", "OUTBOUND", "BOTH"]
ResolvedFulfillmentMode = Literal["INBOUND", "OUTBOUND", "BOTH"]
ExpressionMode = Literal[
    "AUTO", "STRUCTURED_ONLY", "STRUCTURED_WITH_POLICY", "NATURAL_LANGUAGE"
]
ResolvedExpressionMode = Literal[
    "STRUCTURED_ONLY", "STRUCTURED_WITH_POLICY", "NATURAL_LANGUAGE"
]
PolicyProfile = Literal[
    "AUTO", "BALANCED", "BATTERY_SAVING", "CONGESTION_AVOIDANCE", "THROUGHPUT"
]
ResolvedPolicyProfile = Literal[
    "BALANCED", "BATTERY_SAVING", "CONGESTION_AVOIDANCE", "THROUGHPUT"
]
Priority = Literal["low", "medium", "high"]


class CamelModel(BaseModel):
    """Strict model matching the Spring record JSON contract."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class FulfillmentCommandGenerateRequest(CamelModel):
    mode: FulfillmentMode = "AUTO"
    inbound_count: int | None = Field(default=None, alias="inboundCount", ge=0, le=50)
    outbound_count: int | None = Field(default=None, alias="outboundCount", ge=0, le=50)
    inbound_product_codes: list[str] | None = Field(
        default=None, alias="inboundProductCodes", max_length=60
    )
    outbound_product_codes: list[str] | None = Field(
        default=None, alias="outboundProductCodes", max_length=60
    )
    priority: Priority | None = "medium"
    release_interval_ms: int | None = Field(
        default=0, alias="releaseIntervalMs", ge=0, le=3_600_000
    )
    command_expression_mode: ExpressionMode | None = Field(
        default="AUTO", alias="commandExpressionMode"
    )
    policy_profile: PolicyProfile | None = Field(default="AUTO", alias="policyProfile")
    mix_structured_with_policy: bool | None = Field(
        default=None, alias="mixStructuredWithPolicy"
    )
    mix_natural_language: bool | None = Field(
        default=None, alias="mixNaturalLanguage"
    )
    selection_seed: int | None = Field(default=None, alias="selectionSeed")
    preselected_operations: list["PreselectedOperation"] | None = Field(
        default=None, alias="preselectedOperations", min_length=1, max_length=100
    )


class PreselectedOperation(CamelModel):
    """Authoritative Java selection; the AI service may not replace these IDs."""

    operation_type: Literal["INBOUND", "OUTBOUND"] = Field(alias="operationType")
    product_code: str | None = Field(default=None, alias="productCode")
    warehouse_item_id: int | None = Field(
        default=None, alias="warehouseItemId", ge=1
    )
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_reference(self) -> "PreselectedOperation":
        if self.operation_type == "INBOUND" and not self.product_code:
            raise ValueError("INBOUND preselection requires productCode")
        if self.operation_type == "OUTBOUND" and self.warehouse_item_id is None:
            raise ValueError("OUTBOUND preselection requires warehouseItemId")
        return self


class AgentOperationSelection(StrictModel):
    operation_type: Literal["INBOUND", "OUTBOUND"]
    product_code: str | None = None
    warehouse_item_id: int | None = Field(default=None, ge=1)
    facility_code: str
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_reference(self) -> "AgentOperationSelection":
        if self.operation_type == "INBOUND" and not self.product_code:
            raise ValueError("INBOUND selection requires product_code")
        if self.operation_type == "OUTBOUND" and self.warehouse_item_id is None:
            raise ValueError("OUTBOUND selection requires warehouse_item_id")
        return self


class FulfillmentCommandAgentDecision(StrictModel):
    mode: ResolvedFulfillmentMode
    command_expression_mode: ResolvedExpressionMode
    policy_profile: ResolvedPolicyProfile
    operations: list[AgentOperationSelection] = Field(min_length=1, max_length=100)
    policy_instruction: str | None = Field(default=None, max_length=2000)
    natural_language_command: str | None = Field(default=None, max_length=4000)
    decision_summary: str = Field(min_length=1, max_length=1000)


class FulfillmentCommandExpressionDecision(StrictModel):
    """LLM output for expression only; operation selection is already immutable."""

    policy_profile: ResolvedPolicyProfile
    policy_instruction: str | None = Field(default=None, max_length=2000)
    natural_language_command: str | None = Field(default=None, max_length=4000)
    decision_summary: str = Field(min_length=1, max_length=1000)


class PlanRoutingContext(CamelModel):
    new_operation_count: int = Field(alias="newOperationCount", ge=0)
    unfinished_operation_count: int = Field(alias="unfinishedOperationCount", ge=0)
    eligible_robot_count: int = Field(alias="eligibleRobotCount", ge=0)
    total_robot_count: int = Field(alias="totalRobotCount", ge=0)
    low_battery_robot_count: int = Field(alias="lowBatteryRobotCount", ge=0)
    active_robot_count: int = Field(alias="activeRobotCount", ge=0)
    source: str


class StructuredOperation(CamelModel):
    operation_id: str = Field(alias="operationId")
    operation_type: Literal["INBOUND", "OUTBOUND"] = Field(alias="operationType")
    task_id: int | None = Field(default=None, alias="taskId")
    item_id: int = Field(alias="itemId", ge=1)
    product_code: str = Field(alias="productCode")
    quantity: int = Field(ge=1)
    priority: Priority
    source_warehouse_item_id: int | None = Field(default=None, alias="sourceWarehouseItemId")
    source_storage_location_id: int | None = Field(default=None, alias="sourceStorageLocationId")
    source_node_id: int | None = Field(default=None, alias="sourceNodeId")
    source_node_code: str | None = Field(default=None, alias="sourceNodeCode")
    source_facility_code: str | None = Field(default=None, alias="sourceFacilityCode")
    destination_storage_location_id: int | None = Field(
        default=None, alias="destinationStorageLocationId"
    )
    destination_node_id: int | None = Field(default=None, alias="destinationNodeId")
    destination_node_code: str | None = Field(default=None, alias="destinationNodeCode")
    destination_facility_code: str | None = Field(
        default=None, alias="destinationFacilityCode"
    )
    target_rack_level: int | None = Field(default=None, alias="targetRackLevel")
    release_at_ms: int = Field(alias="releaseAtMs", ge=0)
    pickup_service_time_ms: int = Field(alias="pickupServiceTimeMs", ge=0)
    drop_service_time_ms: int = Field(alias="dropServiceTimeMs", ge=0)
    attributes: str | None = None


class StructuredInput(CamelModel):
    request_id: str = Field(alias="requestId")
    operations: list[StructuredOperation]
    constraints: dict[str, str]
    routing_context: PlanRoutingContext = Field(alias="routingContext")


class PlanRequest(CamelModel):
    structured_input: StructuredInput = Field(alias="structuredInput")
    user_command: str | None = Field(default=None, alias="userCommand")
    optimization_backend: Literal["cuopt"] = Field(alias="optimizationBackend")
    runtime_snapshot: None = Field(default=None, alias="runtimeSnapshot")


class CommandLocation(CamelModel):
    kind: str
    label: str
    warehouse_item_id: int | None = Field(default=None, alias="warehouseItemId")
    storage_location_id: int | None = Field(default=None, alias="storageLocationId")
    rack_level: int | None = Field(default=None, alias="rackLevel")
    node_id: int | None = Field(default=None, alias="nodeId")
    node_code: str | None = Field(default=None, alias="nodeCode")
    facility_code: str | None = Field(default=None, alias="facilityCode")


class FrontCommand(CamelModel):
    sequence: int
    operation_id: str = Field(alias="operationId")
    operation_type: Literal["INBOUND", "OUTBOUND"] = Field(alias="operationType")
    product_id: int = Field(alias="productId")
    product_code: str = Field(alias="productCode")
    product_name: str = Field(alias="productName")
    category: str | None = None
    quantity: int
    quantity_unit: str = Field(alias="quantityUnit")
    units_per_box: int = Field(alias="unitsPerBox")
    box_count: int = Field(alias="boxCount")
    priority: Priority
    release_at_ms: int = Field(alias="releaseAtMs")
    source: CommandLocation
    destination: CommandLocation
    warehouse_product_units_before: int = Field(alias="warehouseProductUnitsBefore")
    warehouse_product_units_after: int = Field(alias="warehouseProductUnitsAfter")
    reason: str


class FrontSummary(CamelModel):
    requested_inbound_commands: int = Field(alias="requestedInboundCommands")
    generated_inbound_commands: int = Field(alias="generatedInboundCommands")
    requested_outbound_commands: int = Field(alias="requestedOutboundCommands")
    generated_outbound_commands: int = Field(alias="generatedOutboundCommands")
    total_storage_locations: int = Field(alias="totalStorageLocations")
    total_storage_slots: int = Field(alias="totalStorageSlots")
    occupied_storage_slots: int = Field(alias="occupiedStorageSlots")
    empty_storage_slots: int = Field(alias="emptyStorageSlots")
    available_outbound_boxes: int = Field(alias="availableOutboundBoxes")
    excluded_reserved_boxes: int = Field(alias="excludedReservedBoxes")
    total_inventory_units: int = Field(alias="totalInventoryUnits")
    generated_inbound_units: int = Field(alias="generatedInboundUnits")
    generated_outbound_units: int = Field(alias="generatedOutboundUnits")


class FrontView(CamelModel):
    request_id: str = Field(alias="requestId")
    simulation_run_id: int = Field(alias="simulationRunId")
    warehouse_id: int = Field(alias="warehouseId")
    warehouse_name: str = Field(alias="warehouseName")
    requested_mode: FulfillmentMode = Field(alias="requestedMode")
    mode: ResolvedFulfillmentMode
    requested_command_expression_mode: ExpressionMode = Field(
        alias="requestedCommandExpressionMode"
    )
    command_expression_mode: ResolvedExpressionMode = Field(alias="commandExpressionMode")
    policy_profile: ResolvedPolicyProfile = Field(alias="policyProfile")
    generated_at: datetime = Field(alias="generatedAt")
    summary: FrontSummary
    commands: list[FrontCommand]
    warnings: list[str]


class FulfillmentCommandGenerateResponse(CamelModel):
    plan_request: PlanRequest = Field(alias="planRequest")
    front_view: FrontView = Field(alias="frontView")

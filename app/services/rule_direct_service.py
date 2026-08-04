"""Deterministic Rule fast-path validation and cuOpt formulation.

The Rule path deliberately avoids the LLM retrieval program. Exact structured
identifiers are validated once, canonical inventory/robot/map contexts are read
in a fixed order, and a cuOpt dynamic draft is created directly from those typed
contexts. No semantic tool loop or Warehouse Situation Graph is required.
"""
from __future__ import annotations

from app.domain.schemas import (
    NormalizedWarehouseRequest,
    StructuredKeyValidationResult,
)
from app.repositories.json_repository import JsonWarehouseRepository, get_repository


class StructuredKeyValidator:
    """Validate exact identifiers accepted by the deterministic Rule path."""

    def __init__(self, repository: JsonWarehouseRepository | None = None) -> None:
        self.repository = repository or get_repository()

    def validate(
        self,
        request: NormalizedWarehouseRequest,
        runtime_overrides: object | None = None,
    ) -> StructuredKeyValidationResult:
        """Check exact operation, robot, and map identifiers without semantic guessing."""

        errors: list[str] = []
        warnings: list[str] = []
        clarification = False

        snapshot_robot_ids = {
            str(value.robot_id)
            for value in list(getattr(runtime_overrides, "robot_states", []) or [])
        }
        known_robot_ids = set(self.repository.robots) | snapshot_robot_ids

        for operation in request.operations:
            if operation.operation_type == "OUTBOUND_ORDER":
                if self.repository.get_order(operation.operation_id) is None:
                    errors.append(f"UNKNOWN_ORDER_ID:{operation.operation_id}")
                    clarification = request.source != "structured_events"
            elif operation.operation_type == "INBOUND_ITEM":
                if self.repository.get_inbound_receipt(operation.operation_id) is None:
                    errors.append(f"UNKNOWN_INBOUND_ID:{operation.operation_id}")
                    clarification = request.source != "structured_events"
            elif operation.operation_type == "RECOVERY":
                if operation.operation_id not in known_robot_ids:
                    errors.append(f"UNKNOWN_ROBOT_ID:{operation.operation_id}")
                    clarification = request.source != "structured_events"
            elif operation.operation_type == "UNKNOWN":
                errors.append(f"UNSUPPORTED_OPERATION:{operation.operation_id}")

        if request.constraints.excluded_robot_references:
            errors.append("SEMANTIC_ROBOT_REFERENCE_REQUIRES_AGENT")
            clarification = request.source != "structured_events"

        for robot_id in request.constraints.excluded_robot_ids:
            if robot_id not in known_robot_ids:
                errors.append(f"UNKNOWN_EXCLUDED_ROBOT_ID:{robot_id}")
                clarification = request.source != "structured_events"

        if request.constraints.soft_avoid_edge_references or request.constraints.hard_block_edge_references:
            errors.append("SEMANTIC_MAP_REFERENCE_REQUIRES_AGENT")
            clarification = request.source != "structured_events"

        for edge_id in [
            *request.constraints.soft_avoid_edge_ids,
            *request.constraints.hard_block_edge_ids,
        ]:
            if self.repository.edge(edge_id) is None:
                errors.append(f"UNKNOWN_EDGE_ID:{edge_id}")
                clarification = request.source != "structured_events"

        if not request.operations:
            warnings.append("RULE_REQUEST_HAS_NO_OPERATIONS")

        return StructuredKeyValidationResult(
            valid=not errors,
            errors=list(dict.fromkeys(errors)),
            warnings=warnings,
            requires_user_clarification=clarification,
        )

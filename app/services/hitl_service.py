"""File-backed Human-in-the-Loop checkpoint and resume service."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.config import get_settings
from app.domain.schemas import (
    AutoMissionRequest,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanInteractionResponse,
    HumanInteractionResumeRequest,
    HumanInteractionResumeResult,
    OrchestrationResult,
    StructuredMissionInput,
    WorkflowHoldResult,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_ORDER_ID = re.compile(r"(?<![A-Z0-9])ORD-\d{3,}(?![A-Z0-9])")
_DESTINATION_CODE = re.compile(r"O_[A-Z]")


def _destination_override_evidence(
    record: HumanInteractionRecord,
) -> tuple[str, str, str]:
    """Return authoritative order/current/requested destination identifiers."""

    evidence = [str(value).upper() for value in record.interaction.evidence_ids]
    order_ids = [value for value in evidence if _ORDER_ID.fullmatch(value)]
    destination_ids = [
        value for value in evidence if _DESTINATION_CODE.fullmatch(value)
    ]
    if len(order_ids) == 1 and len(destination_ids) >= 2:
        return order_ids[0], destination_ids[0], destination_ids[1]

    # Backward-compatible recovery for pending cards created before evidence_ids
    # were persisted. Preserve the command's appearance order.
    command = str(record.original_request.get("user_command") or "").upper()
    command_orders = list(dict.fromkeys(_ORDER_ID.findall(command)))
    command_destinations = list(
        dict.fromkeys(
            re.findall(r"(?<![A-Z0-9])O_[A-Z](?![A-Z0-9])", command)
        )
    )
    if len(command_orders) != 1 or len(command_destinations) < 2:
        raise ValueError(
            "DESTINATION_OVERRIDE_APPROVAL requires one canonical order ID and "
            "both current and replacement destination codes."
        )
    return command_orders[0], command_destinations[0], command_destinations[1]


def _apply_approved_destination_override(
    *,
    record: HumanInteractionRecord,
    response: HumanInteractionResponse,
    request_payload: dict[str, Any],
) -> None:
    """Mutate the copied structured request after explicit destination approval.

    The operator may select only the destination already embedded in the review
    option. The current destination is rechecked against the frozen structured
    order so stale or cross-order approvals fail before any optimizer call.
    """

    if response.resolution_code != "DESTINATION_OVERRIDE_APPROVAL":
        return
    if response.selected_option_id != "APPROVE_ALTERNATIVE_DESTINATION":
        raise ValueError(
            "Destination override resume requires "
            "APPROVE_ALTERNATIVE_DESTINATION."
        )

    order_id, expected_current, expected_replacement = (
        _destination_override_evidence(record)
    )
    selected_destinations = [
        str(value).upper()
        for value in [*response.selected_entity_ids, response.resolution_value]
        if value and _DESTINATION_CODE.fullmatch(str(value).upper())
    ]
    if set(selected_destinations) != {expected_replacement}:
        raise ValueError(
            "Approved destination does not match the reviewed replacement: "
            f"expected={expected_replacement};selected={selected_destinations}."
        )

    raw_structured = request_payload.get("structured_input")
    if not isinstance(raw_structured, dict):
        raise ValueError(
            "Destination override requires authoritative structured_input."
        )
    raw_operations = raw_structured.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError(
            "Destination override requires structured_input.operations."
        )
    matches = [
        value
        for value in raw_operations
        if isinstance(value, dict)
        and str(value.get("operation_id") or "").upper() == order_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "Destination override order must resolve exactly once: "
            f"order_id={order_id};matches={len(matches)}."
        )
    operation = matches[0]
    if str(operation.get("operation_type") or "").upper() != "OUTBOUND":
        raise ValueError(
            f"Destination override is valid only for OUTBOUND work: {order_id}."
        )
    destination_field = next(
        (
            field
            for field in (
                "destination_node_code",
                "destination_facility_code",
            )
            if operation.get(field)
        ),
        None,
    )
    if destination_field is None:
        raise ValueError(
            f"{order_id} has no canonical destination code to override."
        )
    current = str(operation[destination_field]).upper()
    if current != expected_current:
        raise ValueError(
            "Destination override is stale or targets the wrong order: "
            f"order_id={order_id};expected_current={expected_current};actual={current}."
        )
    operation[destination_field] = expected_replacement

    # Revalidate the complete authoritative contract and refresh matching event
    # payloads so graph state and the request-scoped repository see one value.
    structured = StructuredMissionInput.model_validate(raw_structured)
    request_payload["structured_input"] = structured.model_dump(mode="json")
    replacement_events = {
        (value.type, value.order_id or value.inbound_id): value.model_dump(mode="json")
        for value in structured.to_events()
    }
    refreshed_events: list[dict[str, Any]] = []
    for raw_event in list(request_payload.get("events") or []):
        event = (
            raw_event.model_dump(mode="json")
            if hasattr(raw_event, "model_dump")
            else dict(raw_event)
        )
        key = (str(event.get("type") or ""), event.get("order_id") or event.get("inbound_id"))
        refreshed_events.append(replacement_events.get(key, event))
    request_payload["events"] = refreshed_events


class HumanInteractionStore:
    """JSON checkpoint store for the current single-process simulator PoC."""

    def __init__(self, root: Path | None = None) -> None:
        settings = get_settings()
        configured = root or settings.hitl_store_dir
        self.root = Path(configured) if configured else Path(settings.output_dir) / "hitl"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, interaction_id: str) -> Path:
        safe = "".join(char for char in interaction_id if char.isalnum() or char in {"-", "_"})
        if not safe:
            raise ValueError("interaction_id is empty or unsafe")
        return self.root / f"{safe}.json"

    def save(self, record: HumanInteractionRecord) -> Path:
        path = self._path(record.interaction.interaction_id)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
        return path

    def load(self, interaction_id: str) -> HumanInteractionRecord:
        path = self._path(interaction_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown HITL interaction: {interaction_id}")
        return HumanInteractionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_pending(self) -> list[HumanInteractionRecord]:
        result: list[HumanInteractionRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                record = HumanInteractionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if record.status == "PENDING":
                result.append(record)
        return result


class HumanInteractionService:
    """Create, inspect, and resolve front-end HITL interactions."""

    def __init__(self, store: HumanInteractionStore | None = None) -> None:
        self.store = store or HumanInteractionStore()

    @staticmethod
    def original_request_from_state(state: dict[str, Any]) -> dict[str, Any]:
        frozen_normalized = (
            state.get("normalized_request_override")
            or state.get("normalized_request")
        )
        return {
            "warehouse_id": state.get("warehouse_id", get_settings().default_warehouse_id),
            "simulation_id": state["simulation_id"],
            "request_mode": state["request_mode"],
            "optimization_backend": state.get("optimization_backend"),
            "events": [
                value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                for value in state.get("events", [])
            ],
            "user_command": state.get("user_command"),
            "structured_input": (
                state["structured_input"].model_dump(mode="json")
                if hasattr(state.get("structured_input"), "model_dump")
                else state.get("structured_input")
            ),
            "normalized_request_override": (
                frozen_normalized.model_dump(mode="json")
                if hasattr(frozen_normalized, "model_dump")
                else frozen_normalized
            ),
            "mission_spec": (
                state["mission_spec"].model_dump(mode="json")
                if hasattr(state.get("mission_spec"), "model_dump")
                else state.get("mission_spec")
            ),
            "planning_mode": state.get("requested_planning_mode"),
            "goods_to_person_options": (
                state["goods_to_person_options"].model_dump(mode="json")
                if hasattr(state.get("goods_to_person_options"), "model_dump")
                else state.get("goods_to_person_options") or {}
            ),
            "runtime_overrides": (
                state["runtime_overrides"].model_dump(mode="json")
                if hasattr(state.get("runtime_overrides"), "model_dump")
                else state.get("runtime_overrides") or {}
            ),
            "max_agent_steps": state.get("max_agent_steps", 8),
            "max_planner_retries": state.get("max_planner_retries", 1),
            "human_responses": [
                value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                for value in state.get("human_responses", [])
            ],
            "parent_interaction_id": state.get("parent_interaction_id"),
        }

    def create_pending(self, *, interaction: HumanInteractionRequest, state: dict[str, Any]) -> HumanInteractionRecord:
        record = HumanInteractionRecord(
            interaction=interaction,
            status="PENDING",
            original_request=self.original_request_from_state(state),
        )
        self.store.save(record)
        return record

    def get(self, interaction_id: str) -> HumanInteractionRecord:
        return self.store.load(interaction_id)

    def list_pending(self) -> list[HumanInteractionRecord]:
        return self.store.list_pending()

    def respond(
        self,
        interaction_id: str,
        payload: HumanInteractionResumeRequest,
        *,
        runner: Callable[[AutoMissionRequest, str | None], OrchestrationResult]
        | None = None,
    ) -> HumanInteractionResumeResult:
        record = self.store.load(interaction_id)
        if record.status != "PENDING":
            raise ValueError(f"Interaction {interaction_id} is already {record.status}.")

        option = None
        if payload.selected_option_id:
            option = next(
                (value for value in record.interaction.options if value.option_id == payload.selected_option_id),
                None,
            )
            if option is None:
                available = [value.option_id for value in record.interaction.options]
                raise ValueError(
                    f"Unknown option {payload.selected_option_id!r}; "
                    f"available options are {available}."
                )

        response = HumanInteractionResponse(
            interaction_id=interaction_id,
            action=payload.action,
            selected_option_id=payload.selected_option_id,
            selected_entity_ids=(
                list(option.selected_entity_ids) if option else list(payload.selected_entity_ids)
            ),
            resolution_code=record.interaction.reason_code,
            resolution_value=(option.resolution_value if option else payload.resolution_value),
            actor_id=payload.actor_id,
            comment=payload.comment,
            responded_at=_utc_now(),
        )
        record.response = response

        if payload.action == "REJECT":
            record.status = "REJECTED"
            self.store.save(record)
            return HumanInteractionResumeResult(
                interaction_id=interaction_id,
                interaction_status="REJECTED",
                resume_outcome="TERMINATED",
                message="The operator rejected the proposed action; the workflow remains held.",
            )
        if payload.action == "CANCEL":
            record.status = "CANCELLED"
            self.store.save(record)
            return HumanInteractionResumeResult(
                interaction_id=interaction_id,
                interaction_status="CANCELLED",
                resume_outcome="TERMINATED",
                message="The operator cancelled the workflow.",
            )
        if record.interaction.kind == "CLARIFICATION" and payload.action != "SELECT":
            raise ValueError("Clarification interactions require SELECT, REJECT, or CANCEL.")
        if record.interaction.kind == "APPROVAL" and payload.action not in {"APPROVE", "SELECT"}:
            raise ValueError("Approval interactions require APPROVE, SELECT, REJECT, or CANCEL.")
        if record.interaction.options and option is None:
            raise ValueError("selected_option_id is required for this interaction.")
        if (
            record.interaction.kind == "CLARIFICATION"
            and not record.interaction.options
            and not response.selected_entity_ids
            and not response.resolution_value
        ):
            raise ValueError("Free-form clarification requires selected_entity_ids or resolution_value.")

        # HOLD choices intentionally stop automation instead of launching another
        # Rule/Agent run. Continuing with stale or physically unverified facts is
        # unsafe, so the front end receives an explicit non-resumable outcome.
        if option is not None and option.outcome == "HOLD":
            record.status = "RESOLVED"
            self.store.save(record)
            recount = (
                response.resolution_code == "AUTHORITATIVE_DATA_CONFLICT"
                and response.resolution_value == "HOLD_AND_RECOUNT"
            )
            hold = WorkflowHoldResult(
                reason_code=response.resolution_code or "HUMAN_SELECTED_HOLD",
                message=(
                    "Automation remains on hold while inventory is recounted; "
                    "no Rule, Agent, optimizer, or MAPF run was restarted."
                    if recount
                    else (
                        option.unavailable_reason
                        or "Automation remains on hold until the required human work is complete."
                    )
                ),
                selected_option_id=option.option_id,
                required_actions=(
                    [
                        "HOLD_AFFECTED_ORDER",
                        "EXCLUDE_STOCK_FROM_ALLOCATION",
                        "CREATE_RECOUNT_WORK_ITEM",
                    ]
                    if recount
                    else ["COMPLETE_REQUIRED_HUMAN_WORK", "RETRY_WITH_FRESH_FACTS"]
                ),
            )
            return HumanInteractionResumeResult(
                interaction_id=interaction_id,
                interaction_status="RESOLVED",
                resume_outcome="HELD",
                terminal_status="held_for_human_action",
                terminal_reason_code=hold.reason_code,
                workflow_hold=hold,
                message=hold.message,
            )
        if option is not None and option.outcome == "TERMINATE":
            record.status = "RESOLVED"
            self.store.save(record)
            return HumanInteractionResumeResult(
                interaction_id=interaction_id,
                interaction_status="RESOLVED",
                resume_outcome="TERMINATED",
                terminal_status="cancelled",
                terminal_reason_code=response.resolution_code,
                message=(
                    option.unavailable_reason
                    or "The selected option terminated the current automation run."
                ),
            )

        # Keep the persisted checkpoint immutable. Destination approval edits a
        # private resume payload, never the original facts shown to the reviewer.
        request_payload = deepcopy(record.original_request)
        responses = list(request_payload.get("human_responses") or [])
        responses.append(response.model_dump(mode="json"))
        request_payload["human_responses"] = responses
        request_payload["parent_interaction_id"] = interaction_id
        _apply_approved_destination_override(
            record=record,
            response=response,
            request_payload=request_payload,
        )
        request = AutoMissionRequest.model_validate(request_payload)

        trusted_mode = None
        if record.interaction.route_locked:
            if record.interaction.resume_route == "RULE_FORMULATION":
                trusted_mode = "force_rule"
            elif record.interaction.resume_route == "AGENT_FORMULATION":
                trusted_mode = "force_agent"
            elif record.interaction.resume_route == "INCIDENT_RESPONSE":
                # Incident continuation uses deterministic structured normalization;
                # the gate then restores the dedicated locked INCIDENT_RESPONSE route.
                trusted_mode = "force_rule"

        if runner is None:
            from app.services.orchestration_service import OrchestrationService

            result = OrchestrationService().run(
                request, trusted_planning_mode=trusted_mode
            )
        else:
            result = runner(request, trusted_mode)

        if record.interaction.route_locked and record.interaction.resume_route is not None:
            expected_route = record.interaction.resume_route
            plan = result.orchestration_plan
            gate = result.request_gate_decision
            observed_route = (
                plan.formulation_route
                if plan is not None
                else gate.final_route if gate is not None else None
            )
            observed_locked = bool(
                (plan is not None and plan.route_locked and not plan.route_switch_allowed)
                or (gate is not None and gate.route_locked)
            )
            if not observed_locked or observed_route != expected_route:
                raise RuntimeError(
                    "HITL resume violated the immutable route contract: "
                    f"expected locked {expected_route}, observed route={observed_route}, "
                    f"locked={observed_locked}."
                )

        record.status = "RESOLVED"
        record.result_path = result.persistence.path if result.persistence else None
        self.store.save(record)
        if result.pending_human_interaction is not None:
            outcome = "PENDING_REVIEW"
        elif result.workflow_hold is not None or result.status == "held_for_human_action":
            outcome = "HELD"
        elif result.status == "failed":
            outcome = "FAILED"
        elif result.status in {"cancelled", "input_rejected"}:
            outcome = "TERMINATED"
        else:
            outcome = "RESUMED"
        return HumanInteractionResumeResult(
            interaction_id=interaction_id,
            interaction_status="RESOLVED",
            resume_outcome=outcome,
            orchestration_result=result,
            message="The operator response was merged and the workflow resumed.",
        )

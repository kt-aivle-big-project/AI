"""File-backed Human-in-the-Loop checkpoint and resume service."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.domain.schemas import (
    AutoMissionRequest,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanInteractionResponse,
    HumanInteractionResumeRequest,
    HumanInteractionResumeResult,
    WorkflowHoldResult,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def respond(self, interaction_id: str, payload: HumanInteractionResumeRequest) -> HumanInteractionResumeResult:
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
                message="The operator rejected the proposed action; the workflow remains held.",
            )
        if payload.action == "CANCEL":
            record.status = "CANCELLED"
            self.store.save(record)
            return HumanInteractionResumeResult(
                interaction_id=interaction_id,
                interaction_status="CANCELLED",
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

        # Some accountable choices intentionally stop automation instead of
        # launching another Rule/Agent run.  Recounting conflicting inventory is
        # the canonical example: continuing with stale facts would be unsafe and
        # rerunning the Agent cannot resolve the physical discrepancy.
        if (
            response.resolution_code == "AUTHORITATIVE_DATA_CONFLICT"
            and response.resolution_value == "HOLD_AND_RECOUNT"
        ):
            record.status = "RESOLVED"
            self.store.save(record)
            hold = WorkflowHoldResult(
                reason_code="AUTHORITATIVE_DATA_CONFLICT",
                message=(
                    "Automation remains on hold while inventory is recounted; "
                    "no Rule, Agent, optimizer, or MAPF run was restarted."
                ),
                selected_option_id="HOLD_AND_RECOUNT",
                required_actions=[
                    "HOLD_AFFECTED_ORDER",
                    "EXCLUDE_STOCK_FROM_ALLOCATION",
                    "CREATE_RECOUNT_WORK_ITEM",
                ],
            )
            return HumanInteractionResumeResult(
                interaction_id=interaction_id,
                interaction_status="RESOLVED",
                terminal_status="held_for_human_action",
                terminal_reason_code="AUTHORITATIVE_DATA_CONFLICT",
                workflow_hold=hold,
                message=hold.message,
            )

        request_payload = dict(record.original_request)
        responses = list(request_payload.get("human_responses") or [])
        responses.append(response.model_dump(mode="json"))
        request_payload["human_responses"] = responses
        request_payload["parent_interaction_id"] = interaction_id
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

        from app.services.orchestration_service import OrchestrationService

        result = OrchestrationService().run(request, trusted_planning_mode=trusted_mode)

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
        return HumanInteractionResumeResult(
            interaction_id=interaction_id,
            interaction_status="RESOLVED",
            orchestration_result=result,
            message="The operator response was merged and the workflow resumed.",
        )

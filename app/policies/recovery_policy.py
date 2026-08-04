"""Deterministic policy for a robot already executing a task."""
from __future__ import annotations

from app.domain.schemas import MapContext, RecoveryDecision, RobotExecutionContext


class RecoveryPolicyService:
    """Choose continue, return, buffer, wait, or human review from runtime facts."""

    def decide(
        self,
        *,
        execution: RobotExecutionContext,
        map_context: MapContext,
        buffer_nodes: list[str],
    ) -> RecoveryDecision:
        """Return a bounded recovery action without using an LLM for safety control."""

        blocked_nodes = set(map_context.map_constraints.blocked_node_ids)
        if execution.current_edge and execution.next_safe_node:
            if execution.destination_node and execution.destination_node not in blocked_nodes:
                return RecoveryDecision(
                    action="EXIT_FORWARD_AND_WAIT",
                    target_node=execution.next_safe_node,
                    reason="The robot is inside an edge; exit forward to a safe node before replanning.",
                )
        if execution.load_state == "LOADED" and execution.destination_node:
            if execution.destination_node not in blocked_nodes:
                return RecoveryDecision(
                    action="CONTINUE_TO_DESTINATION",
                    target_node=execution.destination_node,
                    reason="The loaded destination is reachable in the current map snapshot.",
                )
            if execution.source_node and execution.source_node not in blocked_nodes:
                return RecoveryDecision(
                    action="RETURN_TO_SOURCE",
                    target_node=execution.source_node,
                    reason="The destination is blocked, but the source rack remains available.",
                )
            if buffer_nodes:
                return RecoveryDecision(
                    action="DIVERT_TO_BUFFER",
                    target_node=buffer_nodes[0],
                    reason="Both normal endpoints are unavailable; use the configured buffer.",
                )
        return RecoveryDecision(
            action="HUMAN_REVIEW",
            reason="No safe deterministic recovery target is available.",
        )

import operator
from typing import Annotated, Any, TypedDict


class PlanningState(TypedDict, total=False):
    command: dict[str, Any]
    comparison_id: str
    scenario_id: str
    conversation_id: str
    parent_command_id: str
    previous_command_id: str
    active_plan_version: str
    active_simulation_id: str
    base_plan_source: str
    base_plan_version: str
    base_plan_is_simulated: bool
    resolved_constraints: dict[str, Any]
    inherited_constraints: dict[str, Any]
    overridden_constraints: dict[str, Any]
    conversation_summary: dict[str, Any]
    clarification: dict[str, Any]
    interpretation: dict[str, Any]
    supervisor_decision: dict[str, Any]
    supervisor_source: str
    supervisor_prompt_version: str
    supervisor_warnings: Annotated[list[str], operator.add]
    snapshot: dict[str, Any]
    validation: dict[str, Any]
    inventory_operations: list[dict[str, Any]]
    inventory_feasibility: dict[str, Any]
    inventory_timeline_validation: dict[str, Any]
    inventory_projection: list[dict[str, Any]]
    inventory_reservations: list[dict[str, Any]]
    capacity_feasibility: dict[str, Any]
    emergency_review_items: list[dict[str, Any]]
    inventory_blocked_work_ids: list[str]
    inventory_unknown_item_ids: list[str]
    inventory_item_candidates: dict[str, list[str]]
    scope: dict[str, Any]
    required_tasks: list[dict[str, Any]]
    schedule_validation: dict[str, Any]
    schedule_impact: dict[str, Any]
    ready_task_ids: list[str]
    waiting_task_ids: list[str]
    blocked_task_ids: list[str]
    optimization_problem: dict[str, Any]
    cuopt_plan: dict[str, Any]
    optimization_evidence: list[dict[str, Any]]
    objective_breakdown: dict[str, Any]
    operational_objective: dict[str, Any]
    optimizer_execution: dict[str, Any]
    collision_plan: dict[str, Any]
    route_failure: dict[str, Any]
    mapf_replan_policy: dict[str, Any]
    idle_energy_planning: dict[str, Any]
    resource_reservation_plan: dict[str, Any]
    routing_evidence: dict[str, Any]
    reservation_evidence: dict[str, Any]
    distance_comparison: dict[str, Any]
    plan_validation: dict[str, Any]
    simulation: dict[str, Any]
    verification_decision: dict[str, Any]
    verification_evidence: list[dict[str, Any]]
    verification_source: str
    verification_prompt_version: str
    verification_warnings: Annotated[list[str], operator.add]
    replan_attempt: int
    max_replan_attempts: int
    replan_history: list[dict[str, Any]]
    last_verification_decision: dict[str, Any]
    repeated_failure_signatures: dict[str, int]
    replan_reason: str
    original_plan_version: str
    current_plan_version: str
    replan_base_plan: dict[str, Any]
    previous_successful_candidate: dict[str, Any]
    replan_ready: bool
    impact: dict[str, Any]
    plan_version: str
    simulation_id: str
    simulation_base_state: dict[str, Any]
    simulation_current_state: dict[str, Any]
    simulation_checkpoint: str
    robot_command_batches: list[dict[str, Any]]
    adapter_validation: dict[str, Any]
    dispatched_robot_count: int
    dispatched_command_count: int
    gateway_dispatched: bool
    dispatch_result: dict[str, Any]
    execution_ready: bool
    execution_approval: dict[str, Any]
    replan_count: int
    final_status: str
    response: dict[str, Any]
    answer: str
    report_data: dict[str, Any]
    report_evidence: dict[str, Any]
    report_detail_level: str
    user_report_summary: dict[str, Any]
    report_source: str
    report_prompt_version: str
    report_generation_warnings: list[str]
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    audit_warnings: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]


class ExecutionState(TypedDict, total=False):
    event: dict[str, Any]
    analyze_impact: bool
    impact_analysis: dict[str, Any]
    duplicate: bool
    auto_replan_requested: bool
    replan_request_id: str
    redis_updated: bool
    redis_reconciled: bool
    live_update_deferred: bool
    sql_committed: bool
    stale_event_ignored: bool
    event_ordering: dict[str, Any]
    simulation_state_rollback: dict[str, Any]
    recovery_required: bool
    replan_command: dict[str, Any] | None
    commit_result: dict[str, Any]
    schedule_transition: dict[str, Any]
    successor_dispatch_result: dict[str, Any]
    stream_id: str
    simulation_current_state: dict[str, Any]
    final_status: str
    errors: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]

"""One-shot retrieval planning and dependency-aware parallel warehouse reads."""
from __future__ import annotations

import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from app.core.config import get_settings
from app.repositories.json_repository import get_repository
from app.domain.schemas import (
    EntityResolutionResult,
    NormalizedWarehouseRequest,
    ParallelRetrievalExecutionResult,
    ParallelRetrievalPlan,
    ParallelRetrievalWaveRecord,
    ResolvedToolRequest,
    RetrievalContextSufficiencyResult,
    RetrievalObservation,
    RetrievalToolCallValidationResult,
    RetrievalToolRequest,
    WorkflowValidationIssue,
)
from app.services.stepwise_retrieval_service import (
    RetrievalToolCallValidator,
    ResolvedRetrievalToolCallValidator,
    StepwiseQueryKeyResolver,
    StepwiseRetrievalSufficiencyValidator,
    WarehouseReadToolExecutor,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TOOL_DATA_SOURCE: dict[str, str] = {
    "find_orders": "postgres",
    "get_order_facts": "postgres",
    "get_inbound_facts": "postgres",
    "get_inventory_candidates": "postgres",
    "get_robot_candidates": "redis",
    "get_active_operations": "redis",
    "get_runtime_constraints": "redis",
    "resolve_map_entities": "neo4j",
    "get_connecting_subgraph": "neo4j",
}


@dataclass(frozen=True)
class ParallelRetrievalOutcome:
    """Full parallel retrieval outcome consumed by one LangGraph node."""

    observations: list[RetrievalObservation]
    resolved_requests: list[ResolvedToolRequest]
    entity_resolutions: list[EntityResolutionResult]
    ambiguous_references: list[str]
    not_found_references: list[str]
    user_not_found_references: list[str]
    sufficiency: RetrievalContextSufficiencyResult
    execution: ParallelRetrievalExecutionResult


class ParallelRetrievalPlanCompiler:
    """Build a canonical key/DAG plan, then merge safe optional LLM reads."""

    def build_canonical_plan(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
    ) -> ParallelRetrievalPlan:
        """Create the deterministic base DAG from canonical mission identifiers.

        Direct keys are materialized here.  Requests whose keys depend on an
        earlier observation carry ``derive_from_previous_results=True`` and are
        materialized immediately before their dependency wave executes.
        """

        outbound_ids = list(dict.fromkeys(
            value.operation_id
            for value in normalized_request.operations
            if value.operation_type == "OUTBOUND_ORDER"
        ))
        inbound_ids = list(dict.fromkeys(
            value.operation_id
            for value in normalized_request.operations
            if value.operation_type == "INBOUND_ITEM"
        ))
        recovery_ids = list(dict.fromkeys(
            value.operation_id
            for value in normalized_request.operations
            if value.operation_type == "RECOVERY"
        ))
        explicit_edges = list(dict.fromkeys([
            *normalized_request.constraints.soft_avoid_edge_ids,
            *normalized_request.constraints.hard_block_edge_ids,
            *[
                value.edge_id
                for value in normalized_request.constraints.conditional_edge_policies
            ],
        ]))

        requests: list[RetrievalToolRequest] = []
        if outbound_ids:
            requests.append(
                RetrievalToolRequest(
                    request_id="ORDER_FACTS",
                    tool_name="get_order_facts",
                    exact_ids=outbound_ids,
                    purpose="Load authoritative order lines, quantities, priorities, and destinations.",
                )
            )
        if inbound_ids:
            requests.append(
                RetrievalToolRequest(
                    request_id="INBOUND_FACTS",
                    tool_name="get_inbound_facts",
                    exact_ids=inbound_ids,
                    purpose=(
                        "Load authoritative inbound receipts, handling units, source handoffs, "
                        "and putaway constraints."
                    ),
                )
            )

        if outbound_ids or inbound_ids or recovery_ids:
            requests.append(
                RetrievalToolRequest(
                    request_id="ROBOT_RUNTIME",
                    tool_name="get_robot_candidates",
                    include_statuses=["idle"],
                    exclude_statuses=list(
                        normalized_request.constraints.excluded_robot_statuses
                    ),
                    exact_ids=list(normalized_request.constraints.excluded_robot_ids),
                    purpose="Load the complete runtime fleet and deterministic eligibility facts.",
                )
            )

        if explicit_edges:
            requests.extend([
                RetrievalToolRequest(
                    request_id="EXPLICIT_MAP_ENTITIES",
                    tool_name="resolve_map_entities",
                    exact_ids=explicit_edges,
                    expected_entity_types=["EDGE"],
                    allow_multiple_matches=True,
                    purpose="Validate canonical edge identifiers used by request policies.",
                ),
                RetrievalToolRequest(
                    request_id="EXPLICIT_EDGE_RUNTIME",
                    tool_name="get_runtime_constraints",
                    exact_ids=explicit_edges,
                    purpose="Read current runtime evidence for explicitly named edges.",
                ),
            ])

        if outbound_ids:
            requests.append(
                RetrievalToolRequest(
                    request_id="INVENTORY_CANDIDATES",
                    tool_name="get_inventory_candidates",
                    exact_ids=outbound_ids,
                    derive_from_previous_results=True,
                    depends_on=["ORDER_FACTS"],
                    purpose="Load every positive-quantity rack-level stock candidate for the orders.",
                )
            )

        if outbound_ids or inbound_ids:
            subgraph_dependencies = ["ROBOT_RUNTIME"]
            if outbound_ids:
                subgraph_dependencies.extend(["ORDER_FACTS", "INVENTORY_CANDIDATES"])
            if inbound_ids:
                subgraph_dependencies.append("INBOUND_FACTS")
            requests.extend([
                RetrievalToolRequest(
                    request_id="CONNECTING_SUBGRAPH",
                    tool_name="get_connecting_subgraph",
                    derive_from_previous_results=True,
                    depends_on=list(dict.fromkeys(subgraph_dependencies)),
                    purpose=(
                        "Build directed robot-to-pickup and pickup-to-delivery/station "
                        "path evidence for every actionable operation."
                    ),
                ),
                RetrievalToolRequest(
                    request_id="PATH_RUNTIME",
                    tool_name="get_runtime_constraints",
                    derive_from_previous_results=True,
                    depends_on=["CONNECTING_SUBGRAPH"],
                    purpose="Load occupancy, reservation, congestion, and blockage for relevant paths.",
                ),
            ])

        if recovery_ids:
            requests.append(
                RetrievalToolRequest(
                    request_id="ACTIVE_OPERATIONS",
                    tool_name="get_active_operations",
                    exact_ids=recovery_ids,
                    purpose="Load authoritative active/loaded robot operations for recovery.",
                )
            )

        return ParallelRetrievalPlan(
            requests=requests,
            planning_summary=(
                f"Canonical key builder created {len(requests)} required read request(s). "
                "Direct keys are fixed now; derived keys are materialized per dependency wave."
            ),
        )

    def should_invoke_optional_planner(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        canonical_plan: ParallelRetrievalPlan,
        mode: str,
    ) -> bool:
        """Return whether one optional LLM retrieval-plan call adds real value."""

        normalized_mode = str(mode or "auto").casefold()
        if normalized_mode == "always":
            return True
        if normalized_mode == "off":
            return False
        if not canonical_plan.requests:
            return True

        constraints = normalized_request.constraints
        semantic_references = [
            *constraints.excluded_robot_references,
            *constraints.excluded_robot_status_references,
            *constraints.soft_avoid_edge_references,
            *constraints.hard_block_edge_references,
        ]
        if any(str(value).strip() for value in semantic_references):
            return True
        if normalized_request.incidents:
            return True
        supported = {"OUTBOUND_ORDER", "INBOUND_ITEM", "RECOVERY"}
        if any(value.operation_type not in supported for value in normalized_request.operations):
            return True
        return False

    def compile(
        self,
        *,
        normalized_request: NormalizedWarehouseRequest,
        proposed: ParallelRetrievalPlan,
        canonical_plan: ParallelRetrievalPlan | None = None,
    ) -> ParallelRetrievalPlan:
        """Merge the deterministic base DAG with non-redundant safe LLM extras."""

        base = canonical_plan or self.build_canonical_plan(
            normalized_request=normalized_request
        )
        requests: list[RetrievalToolRequest] = [
            value.model_copy(deep=True) for value in base.requests
        ]
        used_ids = {value.request_id for value in requests}
        used_fingerprints: set[tuple] = set()

        def fingerprint(value: RetrievalToolRequest) -> tuple:
            return (
                value.tool_name,
                tuple(sorted(value.exact_ids)),
                tuple(sorted(value.item_ids)),
                tuple(sorted(value.include_statuses)),
                tuple(sorted(value.exclude_statuses)),
                tuple(sorted(value.depends_on)),
                tuple(sorted((ref.raw_text, ref.exact_id_hint or "") for ref in value.raw_references)),
            )

        for value in requests:
            used_fingerprints.add(fingerprint(value))

        canonical_by_tool: dict[str, str] = {}
        for value in requests:
            canonical_by_tool.setdefault(value.tool_name, value.request_id)
        proposed_by_id = {value.request_id: value for value in proposed.requests}
        represented_tools = {value.tool_name for value in requests}
        dropped: list[str] = []

        for proposed_request in proposed.requests:
            if proposed_request.tool_name == "find_orders" and any(
                value.operation_type == "OUTBOUND_ORDER"
                for value in normalized_request.operations
            ):
                dropped.append(f"{proposed_request.request_id}:find_orders_redundant")
                continue
            if proposed_request.tool_name in represented_tools:
                dropped.append(f"{proposed_request.request_id}:tool_already_canonical")
                continue

            mapped_dependencies: list[str] = []
            for dependency in proposed_request.depends_on:
                if dependency in used_ids:
                    mapped_dependencies.append(dependency)
                    continue
                dependency_request = proposed_by_id.get(dependency)
                if dependency_request is not None:
                    canonical_id = canonical_by_tool.get(dependency_request.tool_name)
                    if canonical_id:
                        mapped_dependencies.append(canonical_id)

            extra = proposed_request.model_copy(
                update={
                    "request_id": f"LLM_{proposed_request.request_id}",
                    "depends_on": list(dict.fromkeys(mapped_dependencies)),
                }
            )
            has_direct_reference = bool(
                extra.exact_ids
                or extra.raw_references
                or extra.item_ids
                or extra.item_text
            )
            if extra.derive_from_previous_results and not extra.depends_on:
                dropped.append(f"{extra.request_id}:derived_request_without_dependency")
                continue
            if extra.tool_name == "resolve_map_entities" and not (
                has_direct_reference
                or (extra.derive_from_previous_results and extra.depends_on)
            ):
                dropped.append(f"{extra.request_id}:map_request_without_reference_source")
                continue

            request_id = extra.request_id
            suffix = 2
            while request_id in used_ids:
                request_id = f"{extra.request_id}-{suffix}"
                suffix += 1
            extra = extra.model_copy(update={"request_id": request_id})
            value_fingerprint = fingerprint(extra)
            if value_fingerprint in used_fingerprints:
                dropped.append(f"{extra.request_id}:duplicate_fingerprint")
                continue
            requests.append(extra)
            used_ids.add(extra.request_id)
            used_fingerprints.add(value_fingerprint)
            represented_tools.add(extra.tool_name)

        return ParallelRetrievalPlan(
            requests=requests,
            planning_summary=(
                f"Merged {len(base.requests)} canonical request(s) with "
                f"{len(requests) - len(base.requests)} optional LLM request(s)."
                + (f" Dropped {len(dropped)} redundant/invalid optional request(s): " + ", ".join(dropped) if dropped else "")
            ),
        )


class ParallelRetrievalPlanValidator:
    """Validate request IDs, dependency semantics, cycles, and safe tool contracts."""

    _MAP_DERIVATION_TOOLS = {
        "get_order_facts",
        "get_inbound_facts",
        "find_orders",
        "get_inventory_candidates",
        "get_robot_candidates",
        "get_connecting_subgraph",
    }

    def validate(self, plan: ParallelRetrievalPlan) -> RetrievalToolCallValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if not plan.requests:
            errors.append("EMPTY_RETRIEVAL_PLAN")
            return RetrievalToolCallValidationResult(valid=False, errors=errors, warnings=warnings)

        request_by_id = {value.request_id: value for value in plan.requests}
        if len(request_by_id) != len(plan.requests):
            errors.append("DUPLICATE_RETRIEVAL_REQUEST_ID")
        for request in plan.requests:
            unknown = set(request.depends_on) - set(request_by_id)
            if unknown:
                errors.append(
                    f"UNKNOWN_RETRIEVAL_DEPENDENCY:{request.request_id}:{','.join(sorted(unknown))}"
                )
            if request.request_id in request.depends_on:
                errors.append(f"SELF_RETRIEVAL_DEPENDENCY:{request.request_id}")
            if request.derive_from_previous_results and not request.depends_on:
                errors.append(f"DERIVED_REQUEST_REQUIRES_DEPENDENCY:{request.request_id}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(request_id: str) -> None:
            if request_id in visited:
                return
            if request_id in visiting:
                errors.append(f"CYCLIC_RETRIEVAL_PLAN:{request_id}")
                return
            request = request_by_id.get(request_id)
            if request is None:
                return
            visiting.add(request_id)
            for dependency in request.depends_on:
                if dependency in request_by_id:
                    visit(dependency)
            visiting.remove(request_id)
            visited.add(request_id)

        for request_id in request_by_id:
            visit(request_id)

        tool_by_request = {value.request_id: value.tool_name for value in plan.requests}
        for request in plan.requests:
            dependency_tools = {
                tool_by_request[value]
                for value in request.depends_on
                if value in tool_by_request
            }
            direct_reference = bool(
                request.exact_ids
                or request.raw_references
                or request.item_ids
                or request.item_text
            )
            if request.tool_name == "get_inventory_candidates" and not (
                request.exact_ids
                or request.item_ids
                or {"get_order_facts", "find_orders"} & dependency_tools
            ):
                errors.append(f"INVENTORY_PLAN_REQUIRES_ORDER_FACTS:{request.request_id}")
            if request.tool_name == "resolve_map_entities":
                valid_derived_source = bool(
                    request.derive_from_previous_results
                    and dependency_tools & self._MAP_DERIVATION_TOOLS
                )
                if not direct_reference and not valid_derived_source:
                    errors.append(f"MAP_PLAN_REQUIRES_REFERENCE_SOURCE:{request.request_id}")
            if request.tool_name == "get_inbound_facts" and not (
                request.exact_ids or request.raw_references
            ):
                errors.append(f"INBOUND_PLAN_REQUIRES_INBOUND_REFERENCE:{request.request_id}")
            if request.tool_name == "get_connecting_subgraph":
                has_robot = "get_robot_candidates" in dependency_tools
                has_outbound_pair = {
                    "get_order_facts", "get_inventory_candidates"
                }.issubset(dependency_tools)
                has_inbound = "get_inbound_facts" in dependency_tools
                partial_outbound = bool(
                    {"get_order_facts", "get_inventory_candidates"} & dependency_tools
                ) and not has_outbound_pair
                if not has_robot or not (has_outbound_pair or has_inbound) or partial_outbound:
                    errors.append(f"SUBGRAPH_PLAN_DEPENDENCIES_INCOMPLETE:{request.request_id}")
            if request.tool_name == "get_runtime_constraints" and not (
                request.exact_ids or "get_connecting_subgraph" in dependency_tools
            ):
                errors.append(f"RUNTIME_PLAN_REQUIRES_EDGE_OR_SUBGRAPH:{request.request_id}")

            result = RetrievalToolCallValidator().validate(request=request, observations=[])
            dependency_only_prefixes = (
                "INVENTORY_LOOKUP_REQUIRES_ORDER_FACTS",
                "SUBGRAPH_REQUIRES_",
                "RUNTIME_CONSTRAINTS_REQUIRE_",
            )
            for value in result.errors:
                if value.startswith(dependency_only_prefixes):
                    continue
                if (
                    value == "MAP_ENTITY_RESOLUTION_REQUIRES_REFERENCE"
                    and request.derive_from_previous_results
                    and dependency_tools & self._MAP_DERIVATION_TOOLS
                ):
                    continue
                errors.append(f"{request.request_id}:{value}")

        return RetrievalToolCallValidationResult(
            valid=not errors,
            errors=list(dict.fromkeys(errors)),
            warnings=warnings,
        )

    def complete_and_validate(
        self,
        *,
        plan: ParallelRetrievalPlan,
        request: NormalizedWarehouseRequest,
        canonical_plan: ParallelRetrievalPlan | None = None,
    ) -> tuple[ParallelRetrievalPlan, list[WorkflowValidationIssue]]:
        """Merge canonical reads and optional reads, then return typed issues."""

        completed = ParallelRetrievalPlanCompiler().compile(
            normalized_request=request,
            proposed=plan,
            canonical_plan=canonical_plan,
        )
        result = self.validate(completed)
        issues = [
            WorkflowValidationIssue(
                code=value.split(":", 1)[0],
                node_name="parallel_retrieval_plan_validator",
                message=value,
            )
            for value in result.errors
        ]
        return completed, issues


class ParallelRetrievalExecutor:
    """Execute independent requests concurrently while honoring dependencies."""

    def __init__(self) -> None:
        self.settings = get_settings()
        # Capture the request-scoped repository before worker threads start.
        # Context variables do not automatically flow into ThreadPoolExecutor.
        self.repository = get_repository()

    def execute(
        self,
        *,
        plan: ParallelRetrievalPlan,
        normalized_request: NormalizedWarehouseRequest,
        selected_entity_ids: list[str] | None = None,
        llm_planning_call_count: int = 1,
    ) -> ParallelRetrievalOutcome:
        started = time.perf_counter()
        remaining = {value.request_id: value for value in plan.requests}
        completed: set[str] = set()
        observations: list[RetrievalObservation] = []
        resolved_request_history: list[ResolvedToolRequest] = []
        resolutions: list[EntityResolutionResult] = []
        ambiguous_references: list[str] = []
        not_found_references: list[str] = []
        user_not_found_references: list[str] = []
        issues: list[WorkflowValidationIssue] = []
        warnings: list[str] = []
        waves: list[ParallelRetrievalWaveRecord] = []
        wave_index = 0

        while remaining:
            ready = [
                value
                for value in remaining.values()
                if set(value.depends_on).issubset(completed)
            ]
            if not ready:
                issues.append(
                    WorkflowValidationIssue(
                        code="PARALLEL_RETRIEVAL_DEADLOCK",
                        node_name="parallel_retrieval_executor",
                        message="No retrieval request can advance; dependencies are unresolved.",
                    )
                )
                break
            ready.sort(key=lambda value: value.request_id)
            wave_index += 1
            wave_started_at = _now()
            wave_started = time.perf_counter()
            snapshot_observations = list(observations)
            resolved_requests: list[ResolvedToolRequest] = []

            for request in ready:
                outcome = StepwiseQueryKeyResolver(self.repository).resolve(
                    tool_request=request,
                    normalized_request=normalized_request,
                    observations=snapshot_observations,
                    selected_entity_ids=selected_entity_ids or [],
                )
                resolutions.extend(outcome.entity_resolutions)
                ambiguous_references.extend(outcome.ambiguous_references)
                not_found_references.extend(outcome.not_found_references)
                user_not_found_references.extend(outcome.user_owned_not_found_references)
                if outcome.ambiguous_references:
                    for value in outcome.ambiguous_references:
                        issues.append(
                            WorkflowValidationIssue(
                                code="ENTITY_REFERENCE_AMBIGUOUS",
                                node_name="parallel_retrieval_executor",
                                message=f"Multiple authoritative entities matched {value!r}.",
                                entity_ids=[value],
                                requires_user_clarification=True,
                            )
                        )
                if outcome.not_found_references:
                    for value in outcome.not_found_references:
                        issues.append(
                            WorkflowValidationIssue(
                                code="ENTITY_REFERENCE_NOT_FOUND",
                                node_name="parallel_retrieval_executor",
                                message=f"No authoritative entity matched {value!r}.",
                                entity_ids=[value],
                                requires_user_clarification=(
                                    value in outcome.user_owned_not_found_references
                                ),
                            )
                        )
                if outcome.request is not None:
                    validation = ResolvedRetrievalToolCallValidator().validate(
                        request=outcome.request,
                        observations=snapshot_observations,
                    )
                    if validation.errors:
                        for value in validation.errors:
                            issues.append(
                                WorkflowValidationIssue(
                                    code=value.split(":", 1)[0],
                                    node_name="parallel_retrieval_executor",
                                    message=f"{request.request_id}:{value}",
                                )
                            )
                    else:
                        resolved_requests.append(outcome.request)
                        resolved_request_history.append(outcome.request)
                elif not outcome.ambiguous_references and not outcome.not_found_references:
                    if request.request_id.startswith("LLM_"):
                        warnings.append(
                            f"Skipped optional request {request.request_id}; dependency materialization produced no keys."
                        )
                        completed.add(request.request_id)
                        remaining.pop(request.request_id, None)
                    else:
                        issues.append(
                            WorkflowValidationIssue(
                                code="RESOLVED_REQUEST_EMPTY",
                                node_name="parallel_retrieval_executor",
                                message=f"{request.request_id}: no executable canonical keys were materialized.",
                            )
                        )

            wave_observations: dict[str, RetrievalObservation] = {}
            if resolved_requests and not issues:
                def run(resolved: ResolvedToolRequest) -> RetrievalObservation:
                    request_model = next(
                        value for value in ready if value.request_id == resolved.request_id
                    )
                    return WarehouseReadToolExecutor(self.repository).execute(
                        request=resolved,
                        observations=snapshot_observations,
                        request_fingerprint=RetrievalToolCallValidator.fingerprint(request_model),
                    )

                with ThreadPoolExecutor(
                    max_workers=min(
                        self.settings.parallel_retrieval_max_workers,
                        len(resolved_requests),
                    )
                ) as pool:
                    future_by_id = {
                        pool.submit(run, value): value.request_id
                        for value in resolved_requests
                    }
                    for future in as_completed(future_by_id):
                        request_id = future_by_id[future]
                        try:
                            wave_observations[request_id] = future.result()
                        except Exception as exc:  # pragma: no cover - adapter boundary
                            issues.append(
                                WorkflowValidationIssue(
                                    code="RETRIEVAL_ADAPTER_FAILURE",
                                    node_name="parallel_retrieval_executor",
                                    message=f"{request_id}: {exc}",
                                    retryable=True,
                                    repair_target="TOOL_EXECUTOR",
                                )
                            )
            for request in ready:
                observation = wave_observations.get(request.request_id)
                if observation is not None:
                    observations.append(observation)
                    completed.add(request.request_id)
                    remaining.pop(request.request_id, None)
            wave_ended_at = _now()
            executed_ids = [value.request_id for value in resolved_requests]
            executed_models = [
                value for value in ready if value.request_id in set(executed_ids)
            ]
            waves.append(
                ParallelRetrievalWaveRecord(
                    wave_index=wave_index,
                    request_ids=executed_ids or [value.request_id for value in ready],
                    tool_names=[value.tool_name for value in executed_models] or [value.tool_name for value in ready],
                    data_sources=[
                        _TOOL_DATA_SOURCE[value.tool_name]
                        for value in (executed_models or ready)
                    ],
                    started_at=wave_started_at,
                    ended_at=wave_ended_at,
                    duration_ms=round((time.perf_counter() - wave_started) * 1000, 3),
                    parallel_width=max(1, len(resolved_requests)),
                )
            )
            if issues:
                break

        sufficiency = StepwiseRetrievalSufficiencyValidator().validate(
            request=normalized_request,
            observations=observations,
        )
        issues.extend(sufficiency.errors)
        result = ParallelRetrievalExecutionResult(
            valid=not issues and sufficiency.ready,
            plan_request_count=len(plan.requests),
            completed_request_ids=[
                value.request_id for value in plan.requests if value.request_id in completed
            ],
            wave_records=waves,
            peak_parallel_width=max((value.parallel_width for value in waves), default=0),
            llm_planning_call_count=max(0, int(llm_planning_call_count)),
            errors=list({(value.code, value.message): value for value in issues}.values()),
            warnings=warnings,
            total_duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return ParallelRetrievalOutcome(
            observations=observations,
            resolved_requests=resolved_request_history,
            entity_resolutions=resolutions,
            ambiguous_references=list(dict.fromkeys(ambiguous_references)),
            not_found_references=list(dict.fromkeys(not_found_references)),
            user_not_found_references=list(dict.fromkeys(user_not_found_references)),
            sufficiency=sufficiency,
            execution=result,
        )

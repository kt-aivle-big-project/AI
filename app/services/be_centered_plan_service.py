"""Planning facade for the existing Spring BE simulation model."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.core.config import get_settings
from app.domain.be_centered import (
    BeCenteredPreflightResponse,
    BeHumanInteractionResumeResponse,
    BeSimulationReplanRequest,
    BeSimulationPlanRequest,
    BeSimulationPlanResponse,
)
from app.domain.schemas import (
    AutoMissionRequest,
    EventInput,
    HumanInteractionResumeRequest,
    OrchestrationResult,
    PlanningMode,
    RuntimePlanningOverrides,
    ReplanMissionRequest,
    ReplanReason,
    SimulationPlan,
    SimulationPlanResponse,
    StructuredMissionInput,
    infer_request_mode,
)
from app.infrastructure.be_centered_postgres import (
    BeCenteredDataError,
    BeCenteredPostgresAdapter,
)
from app.infrastructure.manager import get_infrastructure_manager
from app.repositories.be_runtime_repository import BeSpringRuntimeRepository
from app.repositories.be_shared_repository import (
    BeSharedWarehouseRepository,
    rack_access_map_from_neo4j,
    resolve_runtime_route_node,
)
from app.services.orchestration_service import OrchestrationService
from app.services.hitl_service import HumanInteractionService
from app.services.simulation_plan_service import (
    RollingHorizonReplanService,
    SimulationPlanStore,
)
from app.services.be_route_projection import build_projection


def _router_llm_executed(result: Any) -> bool:
    return any(
        value.node_name == "request_router_llm" and value.llm_used
        for value in result.node_execution_log
    )


def _trusted_replan_planning_mode(reason: ReplanReason) -> PlanningMode | None:
    """System battery recovery is deterministic and must not invoke the router."""

    return "force_rule" if reason in {"LOW_BATTERY", "EDGE_BLOCKED"} else None


def _merge_replan_operation_overlay(
    current: StructuredMissionInput,
    prior_request: dict[str, Any] | None,
) -> StructuredMissionInput:
    """Keep old operation facts available without treating them all as new work.

    ``events`` remain sourced from the current command batch.  The rolling
    horizon service later adds only unfinished/unlocked operation IDs.  This
    merged structured input is solely the request-scoped lookup overlay that
    lets those retained IDs resolve without an orders table.
    """

    raw_prior = (prior_request or {}).get("structured_input")
    if not isinstance(raw_prior, dict):
        return current
    try:
        prior = StructuredMissionInput.model_validate(raw_prior)
    except (TypeError, ValueError):
        return current

    merged_by_id = {value.operation_id: value for value in prior.operations}
    merged_by_id.update(
        {value.operation_id: value for value in current.operations}
    )
    return current.model_copy(
        update={"operations": list(merged_by_id.values())}
    )


class BeCenteredPlanService:
    """Create plans without a native orders or handling_units master table."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.manager = get_infrastructure_manager()
        self.postgres = BeCenteredPostgresAdapter(self.settings, self.manager)
        self.runtime = BeSpringRuntimeRepository(self.settings, self.manager)

    def preflight(self, simulation_run_id: int) -> BeCenteredPreflightResponse:
        problems: list[str] = []
        counts: dict[str, int] = {}
        projected_nodes: list[dict[str, Any]] = []
        projected_edges: list[dict[str, Any]] = []
        projection_loaded = False
        node_code_by_id: dict[int, str] = {}
        runtime: Any | None = None
        sources = {
            "operations": "request.structured_input",
            "inventory": "public.warehouse_items rack_level 1..3",
            "robot_runtime": "redis simulation:run:{id}:*",
            "route_graph": (
                "shared neo4j RouteNode/TRAVERSES written by Spring BE"
            ),
            "orders_table": "not_used",
            "handling_units_table": "not_used",
        }
        try:
            context = self.postgres.resolve_simulation_run(simulation_run_id)
        except Exception as exc:
            return BeCenteredPreflightResponse(
                status="NOT_READY",
                ready=False,
                simulation_run_id=simulation_run_id,
                sources=sources,
                problems=[f"POSTGRES_SIMULATION_RUN:{type(exc).__name__}:{exc}"],
            )

        warehouse_id = int(context["warehouse_id"])
        warehouse_code = str(context["warehouse_code"])
        try:
            nodes = self.postgres.route_nodes(warehouse_id)
            edges = self.postgres.route_edges(warehouse_id)
            inventory = self.postgres.inventory_units(warehouse_id)
            slot_counts = self.postgres.rack_slot_counts(warehouse_id)
            robots = self.postgres.robot_master(warehouse_id)
            projected_nodes, projected_edges = build_projection(nodes, edges)
            projection_loaded = True
            node_code_by_id = {
                int(value["node_id"]): str(value["node_code"])
                for value in nodes
            }
            counts.update(
                {
                    "be_warehouse_nodes": len(nodes),
                    "be_warehouse_edges": len(edges),
                    "be_route_nodes": len(projected_nodes),
                    "be_route_edges": len(projected_edges),
                    "be_inventory_rows": len(inventory),
                    "be_storage_locations": slot_counts["storage_locations"],
                    "be_rack_slots": slot_counts["rack_slots"],
                    "be_occupied_rack_slots": slot_counts["occupied_rack_slots"],
                    "be_empty_rack_slots": slot_counts["empty_rack_slots"],
                    "be_robot_master_rows": len(robots),
                }
            )
        except Exception as exc:
            problems.append(f"POSTGRES_READ_VIEWS:{type(exc).__name__}:{exc}")

        runtime_mode: str | None = None
        try:
            runtime = self.runtime.snapshot(simulation_run_id)
            runtime_mode = runtime.mode
            counts["redis_robot_runtime_rows"] = len(runtime.robots)
            counts["redis_blocked_edges"] = len(runtime.blocked_edge_ids)
            if not runtime.robots:
                problems.append("REDIS_RUNTIME_NOT_INITIALIZED")
        except Exception as exc:
            problems.append(f"REDIS_RUNTIME:{type(exc).__name__}:{exc}")

        try:
            graph = self.manager.neo4j.fetch_route_graph(warehouse_code)
            counts["neo4j_route_nodes"] = len(graph.nodes)
            counts["neo4j_route_edges"] = len(graph.edges)
            if not graph.nodes:
                problems.append("NEO4J_BE_ROUTE_PROJECTION_EMPTY")
            expected_rack_ids = set(
                self.postgres.storage_rack_codes(warehouse_id)
            )
            rack_access_map = rack_access_map_from_neo4j(graph.nodes)
            actual_rack_ids = set(rack_access_map)
            missing_rack_ids = expected_rack_ids - actual_rack_ids
            extra_rack_ids = actual_rack_ids - expected_rack_ids
            invalid_rack_ids = {
                rack_id
                for rack_id, access_node_ids in rack_access_map.items()
                if not access_node_ids
            }
            counts["be_storage_racks"] = len(expected_rack_ids)
            counts["neo4j_rack_access_racks"] = len(actual_rack_ids)
            counts["neo4j_missing_rack_access_racks"] = len(missing_rack_ids)
            counts["neo4j_extra_rack_access_racks"] = len(extra_rack_ids)
            counts["neo4j_invalid_rack_access_racks"] = len(invalid_rack_ids)
            if missing_rack_ids or extra_rack_ids or invalid_rack_ids:
                problems.append(
                    "NEO4J_RACK_ACCESS_CONTRACT_MISMATCH:"
                    f"expected={len(expected_rack_ids)}:"
                    f"actual={len(actual_rack_ids)}:"
                    f"missing={len(missing_rack_ids)}:"
                    f"extra={len(extra_rack_ids)}:"
                    f"invalid={len(invalid_rack_ids)}"
                )
            if projection_loaded:
                expected_node_ids = {
                    str(value["id"]) for value in projected_nodes
                }
                actual_node_ids = {
                    str(value.get("id")) for value in graph.nodes if value.get("id")
                }
                missing_node_ids = expected_node_ids - actual_node_ids
                extra_node_ids = actual_node_ids - expected_node_ids
                counts["neo4j_missing_route_nodes"] = len(missing_node_ids)
                counts["neo4j_extra_route_nodes"] = len(extra_node_ids)
                if missing_node_ids or extra_node_ids:
                    problems.append(
                        "NEO4J_ROUTE_NODE_CONTRACT_MISMATCH:"
                        f"expected={len(expected_node_ids)}:actual={len(actual_node_ids)}:"
                        f"missing={len(missing_node_ids)}:extra={len(extra_node_ids)}"
                    )
            if projection_loaded:
                expected_edge_ids = {
                    str(value["id"]) for value in projected_edges
                }
                actual_edge_ids = {
                    str(value.get("id")) for value in graph.edges if value.get("id")
                }
                missing_edge_ids = expected_edge_ids - actual_edge_ids
                extra_edge_ids = actual_edge_ids - expected_edge_ids
                counts["neo4j_missing_route_edges"] = len(missing_edge_ids)
                counts["neo4j_extra_route_edges"] = len(extra_edge_ids)
                if missing_edge_ids or extra_edge_ids:
                    problems.append(
                        "NEO4J_ROUTE_EDGE_CONTRACT_MISMATCH:"
                        f"expected={len(expected_edge_ids)}:actual={len(actual_edge_ids)}:"
                        f"missing={len(missing_edge_ids)}:extra={len(extra_edge_ids)}"
                    )
            if runtime is not None and graph.nodes:
                route_node_ids = {
                    str(value.get("id")) for value in graph.nodes if value.get("id")
                }
                missing_robot_nodes: set[str] = set()
                for robot in runtime.robots:
                    code = resolve_runtime_route_node(
                        robot,
                        route_node_ids,
                        node_code_by_id,
                        rack_access_map,
                    )
                    if code and code not in route_node_ids:
                        missing_robot_nodes.add(str(code))
                counts["redis_robot_nodes_missing_in_neo4j"] = len(
                    missing_robot_nodes
                )
                if missing_robot_nodes:
                    problems.append(
                        "REDIS_ROBOT_NODE_NOT_IN_ROUTE_GRAPH:"
                        f"count={len(missing_robot_nodes)}"
                    )
        except Exception as exc:
            problems.append(f"NEO4J_ROUTE_GRAPH:{type(exc).__name__}:{exc}")

        # Counts alone cannot detect stale facility metadata that points at a
        # node removed or collapsed by the layout editor. Build the exact
        # repository used by /plan so READY also guarantees that its full data
        # contract can be initialized.
        try:
            planning_repository = BeSharedWarehouseRepository(
                simulation_run_id=simulation_run_id,
            )
            counts["planning_inbound_handoffs"] = len(
                planning_repository.inbound_handoffs
            )
            counts["planning_outbound_stations"] = len(
                planning_repository.outbound_stations
            )
        except Exception as exc:
            problems.append(
                f"PLANNING_REPOSITORY_CONTRACT:{type(exc).__name__}:{exc}"
            )

        return BeCenteredPreflightResponse(
            status="READY" if not problems else "NOT_READY",
            ready=not problems,
            simulation_run_id=simulation_run_id,
            warehouse_id=warehouse_code,
            warehouse_numeric_id=warehouse_id,
            sources=sources,
            counts=counts,
            runtime_mode=runtime_mode,
            problems=problems,
        )

    def plan(
        self,
        simulation_run_id: int,
        request: BeSimulationPlanRequest,
    ) -> BeSimulationPlanResponse:
        context = self.postgres.resolve_simulation_run(simulation_run_id)
        request_id = request.structured_input.request_id or f"REQ-BE-{simulation_run_id}-{uuid4().hex[:12].upper()}"
        prior = self.postgres.load_request_response(request_id, simulation_run_id)
        if prior is not None:
            return BeSimulationPlanResponse.model_validate(prior)

        warehouse_id = str(context["warehouse_code"])
        numeric_warehouse_id = int(context["warehouse_id"])
        structured_input = request.structured_input
        if structured_input.request_id is None:
            structured_input = structured_input.model_copy(
                update={"request_id": request_id}
            )
        events = structured_input.to_events()
        if request.runtime_snapshot is not None and not self.settings.allow_api_runtime_snapshot:
            raise BeCenteredDataError(
                "runtime_snapshot is disabled for this deployment. The Spring BE Redis "
                "namespace is authoritative; omit runtime_snapshot or set "
                "ALLOW_API_RUNTIME_SNAPSHOT=true only for deterministic tests."
            )
        runtime_overrides = (
            request.runtime_snapshot.to_internal()
            if request.runtime_snapshot is not None
            else RuntimePlanningOverrides()
        )
        internal = AutoMissionRequest(
            warehouse_id=warehouse_id,
            simulation_id=f"BE-RUN-{simulation_run_id}",
            request_mode=infer_request_mode(
                events=events,
                user_command=request.user_command,
            ),
            optimization_backend=request.optimization_backend,
            events=events,
            structured_input=structured_input,
            user_command=request.user_command,
            runtime_overrides=runtime_overrides,
        )
        repository = BeSharedWarehouseRepository(
            simulation_run_id=simulation_run_id,
        )
        result = OrchestrationService().run(
            internal,
            repository=repository,
            persist_simulation_plan=False,
        )
        if result.simulation_plan is not None:
            next_version = self.postgres.next_plan_version(simulation_run_id)
            plan = result.simulation_plan.model_copy(
                update={
                    "plan_id": (
                        f"PLAN-{warehouse_id}-BE-RUN-{simulation_run_id}-"
                        f"{next_version}-{uuid4().hex[:10].upper()}"
                    ),
                    "plan_version": next_version,
                }
            )
            result = result.model_copy(update={"simulation_plan": plan})

        compact = SimulationPlanResponse(
            status=result.status,
            warehouse_id=result.warehouse_id,
            simulation_id=result.simulation_id,
            request_mode=result.request_mode,
            final_route=(
                result.orchestration_plan.formulation_route
                if result.orchestration_plan
                else None
            ),
            effective_planning_mode=result.effective_planning_mode,
            planning_mode_source=result.planning_mode_source,
            router_llm_executed=_router_llm_executed(result),
            plan=result.simulation_plan,
            frontend_summary=result.frontend_summary,
            pending_human_interaction=result.pending_human_interaction,
            input_rejection=result.input_rejection,
            workflow_hold=result.workflow_hold,
            errors=result.errors,
        )
        if result.simulation_plan is not None:
            plan = result.simulation_plan
            self.postgres.save_plan(
                plan_id=plan.plan_id,
                simulation_run_id=simulation_run_id,
                warehouse_id=numeric_warehouse_id,
                plan_version=plan.plan_version,
                status=plan.status,
                request_json=request.model_dump(mode="json", exclude_none=True),
                plan_json=plan.model_dump(mode="json"),
                trace_json=result.model_dump(mode="json", exclude_none=True),
                planning_mode=result.effective_planning_mode,
                optimization_backend=result.optimization_backend,
                map_version=plan.map_version,
                runtime_version=(
                    result.context_snapshot.runtime_version
                    if result.context_snapshot is not None
                    else None
                ),
                makespan_ms=plan.makespan_ms,
                base_plan_id=plan.base_plan_id,
                supersedes_plan_id=plan.supersedes_plan_id,
                plan_kind=plan.plan_kind,
            )
            # Keep the existing trace/debug endpoints working with the final BE
            # plan ID and version. The database remains the durable authority.
            self.postgres.save_inventory_reservations(
                plan_id=plan.plan_id,
                simulation_run_id=simulation_run_id,
                batches=(
                    list(result.goods_to_person_compilation.batches)
                    if result.goods_to_person_compilation is not None
                    else []
                ),
            )
            SimulationPlanStore().save(plan, result)
        plan_id = result.simulation_plan.plan_id if result.simulation_plan else None
        response = BeSimulationPlanResponse(
            simulation_run_id=simulation_run_id,
            warehouse_id=warehouse_id,
            warehouse_numeric_id=numeric_warehouse_id,
            request_id=request_id,
            result=compact,
            trace_url=(
                f"/api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/trace"
                if plan_id
                else None
            ),
            debug_url=(
                f"/api/v1/warehouses/{warehouse_id}/missions/plans/{plan_id}/debug"
                if plan_id
                else None
            ),
        )
        self.postgres.save_request_log(
            request_id=request_id,
            simulation_run_id=simulation_run_id,
            request_type="PLAN",
            status=result.status,
            request_json=request.model_dump(mode="json", exclude_none=True),
            response_json=response.model_dump(mode="json", exclude_none=True),
        )
        return response

    def replan(
        self,
        simulation_run_id: int,
        request: BeSimulationReplanRequest,
    ) -> BeSimulationPlanResponse:
        context = self.postgres.resolve_simulation_run(simulation_run_id)
        warehouse_id = str(context["warehouse_code"])
        numeric_warehouse_id = int(context["warehouse_id"])
        persisted = self.postgres.load_plan(
            request.active_plan_id, simulation_run_id
        )
        if persisted is None:
            raise BeCenteredDataError(
                f"active_plan_id={request.active_plan_id} does not belong to "
                f"simulation_run_id={simulation_run_id}."
            )

        store = SimulationPlanStore()
        active = SimulationPlan.model_validate(persisted)
        if active.warehouse_id != warehouse_id:
            raise BeCenteredDataError("Active plan warehouse does not match the simulation run.")
        try:
            store.load(active.plan_id)
        except FileNotFoundError:
            store.save(active, None)

        if request.runtime_snapshot is not None and not self.settings.allow_api_runtime_snapshot:
            raise BeCenteredDataError(
                "runtime_snapshot is disabled for this deployment. The Spring BE Redis "
                "namespace is authoritative."
            )
        current_structured_input = request.structured_input
        repository = BeSharedWarehouseRepository(
            simulation_run_id=simulation_run_id,
            replanning_from_plan_id=active.plan_id,
        )
        if request.reason == "LOW_BATTERY":
            events = [
                EventInput(
                    type="low_battery",
                    payload={"replan_reason": "LOW_BATTERY"},
                )
            ]
        else:
            events = current_structured_input.to_events()
            if request.reason == "EDGE_BLOCKED":
                blocked_edge_ids = sorted(
                    {
                        str(value["edge_id"])
                        for value in repository.runtime_edge_records()
                        if str(value.get("status", "")).casefold() == "blocked"
                    }
                )
                events.extend(
                    EventInput(
                        type="edge_blocked",
                        edge_id=edge_id,
                        payload={
                            "status": "blocked",
                            "reason_code": "EDGE_BLOCKED_REPLAN",
                        },
                    )
                    for edge_id in blocked_edge_ids
                )
        structured_input = _merge_replan_operation_overlay(
            current_structured_input,
            self.postgres.load_plan_request(
                request.active_plan_id, simulation_run_id
            ),
        )
        effective_request = request.model_copy(
            update={"structured_input": structured_input}
        )
        internal = AutoMissionRequest(
            warehouse_id=warehouse_id,
            simulation_id=f"BE-RUN-{simulation_run_id}",
            request_mode=infer_request_mode(
                events=events,
                user_command=request.user_command,
            ),
            optimization_backend=request.optimization_backend,
            events=events,
            structured_input=structured_input,
            user_command=request.user_command,
            runtime_overrides=(
                request.runtime_snapshot.to_internal()
                if request.runtime_snapshot is not None
                else RuntimePlanningOverrides()
            ),
        )
        trusted_planning_mode = _trusted_replan_planning_mode(request.reason)

        def run_replan(combined: AutoMissionRequest) -> OrchestrationResult:
            return OrchestrationService().run(
                combined,
                trusted_planning_mode=trusted_planning_mode,
                persist_simulation_plan=False,
                repository=repository,
            )

        compact = RollingHorizonReplanService(
            store=store,
            runner=(run_replan if trusted_planning_mode is not None else None),
            repository=repository,
        ).replan(
            ReplanMissionRequest(
                active_plan_id=request.active_plan_id,
                active_plan_version=request.active_plan_version,
                replan_at_sim_time_ms=request.replan_at_sim_time_ms,
                mission=internal,
                reason=request.reason,
                activation_policy=request.activation_policy,
            )
        )
        plan = compact.plan
        request_id = (
            structured_input.request_id
            or f"REQ-BE-REPLAN-{simulation_run_id}-{uuid4().hex[:12].upper()}"
        )
        if plan is not None:
            _saved_plan, result = store.load(plan.plan_id)
            self.postgres.save_plan(
                plan_id=plan.plan_id,
                simulation_run_id=simulation_run_id,
                warehouse_id=numeric_warehouse_id,
                plan_version=plan.plan_version,
                status=plan.status,
                request_json=effective_request.model_dump(
                    mode="json", exclude_none=True
                ),
                plan_json=plan.model_dump(mode="json"),
                trace_json=(
                    result.model_dump(mode="json", exclude_none=True)
                    if result is not None
                    else None
                ),
                planning_mode=compact.effective_planning_mode,
                optimization_backend=(
                    getattr(result, "optimization_backend", None)
                    if result is not None
                    else request.optimization_backend
                ),
                map_version=plan.map_version,
                runtime_version=(
                    result.context_snapshot.runtime_version
                    if result is not None and result.context_snapshot is not None
                    else None
                ),
                makespan_ms=plan.makespan_ms,
                base_plan_id=plan.base_plan_id,
                supersedes_plan_id=plan.supersedes_plan_id,
                plan_kind=plan.plan_kind,
            )
            self.postgres.save_inventory_reservations(
                plan_id=plan.plan_id,
                simulation_run_id=simulation_run_id,
                batches=(
                    list(result.goods_to_person_compilation.batches)
                    if result is not None
                    and result.goods_to_person_compilation is not None
                    else []
                ),
            )

        response = BeSimulationPlanResponse(
            simulation_run_id=simulation_run_id,
            warehouse_id=warehouse_id,
            warehouse_numeric_id=numeric_warehouse_id,
            request_id=request_id,
            result=compact,
            trace_url=(
                f"/api/v1/warehouses/{warehouse_id}/missions/plans/{plan.plan_id}/trace"
                if plan is not None
                else None
            ),
            debug_url=(
                f"/api/v1/warehouses/{warehouse_id}/missions/plans/{plan.plan_id}/debug"
                if plan is not None
                else None
            ),
        )
        self.postgres.save_request_log(
            request_id=request_id,
            simulation_run_id=simulation_run_id,
            request_type="REPLAN",
            status=compact.status,
            request_json=effective_request.model_dump(
                mode="json", exclude_none=True
            ),
            response_json=response.model_dump(mode="json", exclude_none=True),
        )
        return response

    def respond_to_human_interaction(
        self,
        simulation_run_id: int,
        interaction_id: str,
        payload: HumanInteractionResumeRequest,
    ) -> BeHumanInteractionResumeResponse:
        """Resume one HITL checkpoint through the BE-shared planning contract."""

        context = self.postgres.resolve_simulation_run(simulation_run_id)
        warehouse_id = str(context["warehouse_code"])
        numeric_warehouse_id = int(context["warehouse_id"])
        hitl = HumanInteractionService()
        record = hitl.get(interaction_id)
        expected_simulation_id = f"BE-RUN-{simulation_run_id}"
        if str(record.original_request.get("simulation_id")) != expected_simulation_id:
            raise BeCenteredDataError(
                f"interaction_id={interaction_id} does not belong to "
                f"simulation_run_id={simulation_run_id}."
            )
        if str(record.original_request.get("warehouse_id")) != warehouse_id:
            raise BeCenteredDataError(
                f"interaction_id={interaction_id} warehouse does not match the simulation run."
            )

        captured: dict[str, Any] = {}
        store = SimulationPlanStore()

        def run_resumed(
            request: AutoMissionRequest,
            trusted_planning_mode: str | None,
        ) -> OrchestrationResult:
            source_plan_id = request.runtime_overrides.source_plan_id
            if source_plan_id:
                persisted = self.postgres.load_plan(
                    source_plan_id, simulation_run_id
                )
                if persisted is None:
                    raise BeCenteredDataError(
                        f"source_plan_id={source_plan_id} does not belong to "
                        f"simulation_run_id={simulation_run_id}."
                    )
                active = SimulationPlan.model_validate(persisted)
                try:
                    store.load(active.plan_id)
                except FileNotFoundError:
                    store.save(active, None)
                repository = BeSharedWarehouseRepository(
                    simulation_run_id=simulation_run_id,
                    replanning_from_plan_id=active.plan_id,
                )
                observed: dict[str, OrchestrationResult] = {}

                def run_combined(combined: AutoMissionRequest) -> OrchestrationResult:
                    value = OrchestrationService().run(
                        combined,
                        trusted_planning_mode=trusted_planning_mode,
                        persist_simulation_plan=False,
                        repository=repository,
                    )
                    observed["result"] = value
                    return value

                compact = RollingHorizonReplanService(
                    store=store,
                    runner=run_combined,
                    repository=repository,
                ).replan(
                    ReplanMissionRequest(
                        active_plan_id=active.plan_id,
                        active_plan_version=active.plan_version,
                        replan_at_sim_time_ms=(
                            request.runtime_overrides.planning_horizon_start_ms
                        ),
                        mission=request,
                        reason="NEW_ORDER",
                        activation_policy="ALL_ROBOTS_READY",
                    )
                )
                result = observed["result"]
                if compact.plan is not None:
                    result = result.model_copy(
                        update={"simulation_plan": compact.plan}
                    )
                captured.update(
                    compact=compact,
                    result=result,
                    request=request,
                    request_type="HITL_REPLAN",
                )
                return result

            repository = BeSharedWarehouseRepository(
                simulation_run_id=simulation_run_id
            )
            result = OrchestrationService().run(
                request,
                trusted_planning_mode=trusted_planning_mode,
                persist_simulation_plan=False,
                repository=repository,
            )
            if result.simulation_plan is not None:
                next_version = self.postgres.next_plan_version(simulation_run_id)
                plan = result.simulation_plan.model_copy(
                    update={
                        "plan_id": (
                            f"PLAN-{warehouse_id}-BE-RUN-{simulation_run_id}-"
                            f"{next_version}-{uuid4().hex[:10].upper()}"
                        ),
                        "plan_version": next_version,
                    }
                )
                result = result.model_copy(update={"simulation_plan": plan})
            compact = self._compact_result(result)
            captured.update(
                compact=compact,
                result=result,
                request=request,
                request_type="HITL_PLAN",
            )
            return result

        resumed = hitl.respond(
            interaction_id,
            payload,
            runner=run_resumed,
        )
        compact = captured.get("compact")
        result = captured.get("result")
        plan_response = None
        if isinstance(compact, SimulationPlanResponse) and isinstance(
            result, OrchestrationResult
        ):
            structured = record.original_request.get("structured_input") or {}
            request_id = (
                structured.get("request_id")
                if isinstance(structured, dict)
                else None
            ) or f"REQ-BE-HITL-{simulation_run_id}-{uuid4().hex[:12].upper()}"
            plan = compact.plan
            if plan is not None:
                request_json = record.original_request
                self.postgres.save_plan(
                    plan_id=plan.plan_id,
                    simulation_run_id=simulation_run_id,
                    warehouse_id=numeric_warehouse_id,
                    plan_version=plan.plan_version,
                    status=plan.status,
                    request_json=request_json,
                    plan_json=plan.model_dump(mode="json"),
                    trace_json=result.model_dump(mode="json", exclude_none=True),
                    planning_mode=result.effective_planning_mode,
                    optimization_backend=result.optimization_backend,
                    map_version=plan.map_version,
                    runtime_version=(
                        result.context_snapshot.runtime_version
                        if result.context_snapshot is not None
                        else None
                    ),
                    makespan_ms=plan.makespan_ms,
                    base_plan_id=plan.base_plan_id,
                    supersedes_plan_id=plan.supersedes_plan_id,
                    plan_kind=plan.plan_kind,
                )
                self.postgres.save_inventory_reservations(
                    plan_id=plan.plan_id,
                    simulation_run_id=simulation_run_id,
                    batches=(
                        list(result.goods_to_person_compilation.batches)
                        if result.goods_to_person_compilation is not None
                        else []
                    ),
                )
                store.save(plan, result)
            plan_response = BeSimulationPlanResponse(
                simulation_run_id=simulation_run_id,
                warehouse_id=warehouse_id,
                warehouse_numeric_id=numeric_warehouse_id,
                request_id=request_id,
                result=compact,
                trace_url=(
                    f"/api/v1/warehouses/{warehouse_id}/missions/plans/{plan.plan_id}/trace"
                    if plan is not None
                    else None
                ),
                debug_url=(
                    f"/api/v1/warehouses/{warehouse_id}/missions/plans/{plan.plan_id}/debug"
                    if plan is not None
                    else None
                ),
            )
            self.postgres.save_request_log(
                request_id=request_id,
                simulation_run_id=simulation_run_id,
                request_type=str(captured.get("request_type") or "HITL"),
                status=compact.status,
                request_json=record.original_request,
                response_json=plan_response.model_dump(
                    mode="json", exclude_none=True
                ),
            )

        return BeHumanInteractionResumeResponse(
            interaction_id=resumed.interaction_id,
            interaction_status=resumed.interaction_status,
            resume_outcome=resumed.resume_outcome,
            message=resumed.message,
            terminal_status=resumed.terminal_status,
            workflow_hold=resumed.workflow_hold,
            plan_response=plan_response,
        )

    @staticmethod
    def _compact_result(result: OrchestrationResult) -> SimulationPlanResponse:
        return SimulationPlanResponse(
            status=result.status,
            warehouse_id=result.warehouse_id,
            simulation_id=result.simulation_id,
            request_mode=result.request_mode,
            final_route=(
                result.orchestration_plan.formulation_route
                if result.orchestration_plan
                else None
            ),
            effective_planning_mode=result.effective_planning_mode,
            planning_mode_source=result.planning_mode_source,
            router_llm_executed=_router_llm_executed(result),
            plan=result.simulation_plan,
            frontend_summary=result.frontend_summary,
            pending_human_interaction=result.pending_human_interaction,
            input_rejection=result.input_rejection,
            workflow_hold=result.workflow_hold,
            errors=result.errors,
        )

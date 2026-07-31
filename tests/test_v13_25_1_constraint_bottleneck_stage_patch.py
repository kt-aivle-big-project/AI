from __future__ import annotations

import json
from pathlib import Path

from app.domain.schemas import (
    ConditionalEdgePolicy,
    FormulationRecommendation,
    NodeReservation,
    NormalizedOperation,
    NormalizedRequestConstraints,
    NormalizedWarehouseRequest,
    PublicMissionRequest,
    PublicRuntimeSnapshot,
)
from app.services.cuopt_formulation_service import _apply_emergency_reserve
from app.services.mapf_service import MAPFPlanValidator, PrioritizedSIPPPlanner
from app.services.request_gate_service import resolve_request_gate
from scripts.v12_solver_mapf_support import build_fixture_problem, reference_multitask_result
from scripts.native_plan_complex_support_v41 import (
    build_stage_contract,
    load_scenario,
    write_stage_contract_artifacts,
)


def _policy_request() -> NormalizedWarehouseRequest:
    return NormalizedWarehouseRequest(
        source="natural_language",
        operations=[
            NormalizedOperation(
                operation_id="ORD-001",
                operation_type="OUTBOUND_ORDER",
                raw_reference="ORD-001",
            ),
            NormalizedOperation(
                operation_id="IN-001",
                operation_type="INBOUND_ITEM",
                raw_reference="IN-001",
            ),
        ],
        constraints=NormalizedRequestConstraints(
            conditional_edge_policies=[
                ConditionalEdgePolicy(
                    edge_id="H3_7",
                    threshold_ms=8000,
                    when_true="HARD_AVOID",
                    when_false="SOFT_AVOID",
                )
            ],
            objective_profile="BALANCED",
            objective_terms=["MIN_COMPLETION_TIME", "MIN_BATTERY_RISK"],
            reserve_robot_count=1,
        ),
        raw_user_command=(
            "ORD-001을 출고하고 IN-001도 입고해. H3_7 예상 대기가 8초를 넘으면 "
            "hard avoid, 아니면 soft avoid. 완료시간과 배터리 위험을 최소화하고 "
            "로봇 1대는 비상 예비로 남겨."
        ),
        normalization_summary="typed policy stack",
    )


def test_typed_policy_stack_is_deterministically_routed_to_agent() -> None:
    request = _policy_request()
    decision = resolve_request_gate(
        simulation_id="SIM-POLICY-STACK",
        request=request,
        recommendation=FormulationRecommendation(
            route="RULE_FORMULATION",
            gate_action="PROCEED",
            reasons=["model suggested rule"],
        ),
        original_user_command=request.raw_user_command,
        has_structured_events=False,
        planning_mode="llm_router",
        requires_agent_guard=False,
        human_responses=[],
    )
    assert decision.action == "ROUTE_AGENT"
    assert decision.final_route == "AGENT_FORMULATION"
    assert decision.route_locked is True


def test_emergency_reserve_selects_highest_battery_robot() -> None:
    request = _policy_request()
    included, reserved = _apply_emergency_reserve(
        candidate_robot_ids=["R002", "R003"],
        battery_by_robot={"R002": 72.0, "R003": 91.0},
        request=request,
    )
    assert included == ["R002"]
    assert reserved == ["R003"]


def test_public_runtime_snapshot_carries_node_reservations_to_mapf() -> None:
    snapshot = PublicRuntimeSnapshot(
        preserved_node_reservations=[
            NodeReservation(
                reservation_id="TEST-STATION-1-A",
                node_id="OUT_STATION_1_ACCESS_A",
                robot_id="STATION-MAINT",
                start_at_ms=0,
                end_at_ms=60000,
                reason="forced contention",
            )
        ]
    )
    internal = snapshot.to_internal()
    assert len(internal.preserved_node_reservations) == 1
    assert internal.preserved_node_reservations[0].node_id == "OUT_STATION_1_ACCESS_A"


def test_forced_contention_scenario_is_public_api_valid() -> None:
    scenario = load_scenario("P17_FORCED_STATION_NODE_CONTENTION")
    request = PublicMissionRequest.model_validate(scenario["request"])
    assert len(request.runtime_snapshot.preserved_node_reservations) == 4
    assert scenario["expected"]["min_wait_steps"] == 1
    assert scenario["expected"]["require_positive_mapf_delay"] is True


def test_preserved_node_reservation_forces_real_mapf_wait() -> None:
    """A request-scoped node reservation must create an actual WAIT interval."""

    root = Path(__file__).resolve().parents[1]
    problem = build_fixture_problem(root / "scenarios" / "fixtures" / "V9_ten_orders_multitask")
    result = reference_multitask_result(problem.payload)
    reverse_index = {index: node_id for node_id, index in problem.payload.location_index_map.items()}
    task_location = {
        task_id: reverse_index[location]
        for task_id, location in zip(
            problem.payload.task_data.task_ids,
            problem.payload.task_data.task_locations,
            strict=True,
        )
    }
    blocked_goal = task_location[result.routes[0].task_sequence[0]]

    expansion, schedule = PrioritizedSIPPPlanner().plan(
        payload=problem.payload,
        result=result,
        map_context=problem.map_context,
        node_types=problem.node_types,
        preserved_node_reservations=[
            NodeReservation(
                reservation_id="TEST-FORCED-NODE-CONTENTION",
                node_id=blocked_goal,
                robot_id="STATION-MAINT",
                start_at_ms=0,
                end_at_ms=60000,
                reason="Regression test forces a safe-interval wait.",
            )
        ],
    )

    assert expansion.status == "expanded"
    assert schedule.valid is True
    assert schedule.total_wait_ms > 0
    assert any(
        step.step_type == "WAIT"
        for route in schedule.routes
        for step in route.steps
    )
    validation = MAPFPlanValidator().validate(
        schedule=schedule,
        map_context=problem.map_context,
        node_types=problem.node_types,
        max_edge_wait_ms=problem.mission.max_edge_wait_ms,
        payload=problem.payload,
    )
    assert validation.valid is True


def test_stage_contract_validates_semantics_and_writes_review_files(tmp_path: Path) -> None:
    """The scenario runner must verify business objects, not only node success flags."""

    scenario = {
        "scenario_id": "TEST-STAGE-CONTRACT",
        "expected": {
            "expected_operations": ["ORD-001", "IN-001"],
            "require_live_repository": True,
            "require_router_llm": True,
            "allowed_final_routes": ["RULE_FORMULATION"],
        },
    }
    response = {
        "status": "plan_validated",
        "final_route": "RULE_FORMULATION",
        "router_llm_executed": True,
        "plan": {
            "plan_id": "PLAN-TEST",
            "plan_version": 1,
            "makespan_ms": 10000,
            "robots": [
                {
                    "robot_id": "R002",
                    "steps": [
                        {
                            "step_type": "SERVICE",
                            "task_id": "G2P-ORD-001_PICK",
                        },
                        {
                            "step_type": "SERVICE",
                            "task_id": "IN-001_PICK",
                        },
                    ],
                }
            ],
            "logical_operations": [
                {
                    "operation_id": "ORD-001",
                    "assigned_robot_id": "R002",
                    "task_ids": ["G2P-ORD-001_PICK"],
                },
                {
                    "operation_id": "IN-001",
                    "assigned_robot_id": "R002",
                    "task_ids": ["IN-001_PICK"],
                },
            ],
        },
    }
    trace = {
        "repository": {"repository_type": "LiveWarehouseRepository"},
        "nodes": [
            {"node_name": "request_router_llm"},
            {"node_name": "rule_cuopt_formulator_direct"},
            {"node_name": "optimizer"},
            {"node_name": "mapf_plan_validator"},
        ],
    }
    debug = {
        "normalized_request": {
            "operations": [
                {"operation_id": "ORD-001"},
                {"operation_id": "IN-001"},
            ],
            "constraints": {},
        },
        "context_snapshot": {"snapshot_id": "SNAP-TEST"},
        "inventory_context": {"task_needs": [{}], "inbound_needs": [{}]},
        "map_context": {"node_count": 220, "edge_count": 356},
        "robot_context": {"candidate_robot_ids": ["R002"]},
        "cuopt_dynamic_input_draft": {
            "formulation_source": "rule",
            "formulation_mode": "GOODS_TO_PERSON",
            "g2p_order_ids": ["ORD-001"],
            "tasks": [{"order_id": "IN-001"}],
            "deferred_order_ids": [],
            "fleet": {
                "included_robot_ids": ["R002"],
                "excluded_robot_ids": [],
                "reserved_robot_ids": [],
            },
        },
        "cuopt_dynamic_input_validation": {"valid": True, "errors": []},
        "cuopt_payload": {
            "location_index_map": {"R1_5": 0, "K1_7_ACCESS_A": 1},
            "task_data": {
                "task_ids": ["ORD-001_PICK", "ORD-001_DROP", "IN-001_PICK", "IN-001_DROP"],
                "pickup_and_delivery_pairs": [[0, 1], [2, 3]],
            },
            "fleet_data": {"vehicle_ids": ["R002"], "min_vehicles": 1},
            "waypoint_graph_data": {"edge_ids": ["E1"]},
        },
        "payload_validation": {"valid": True},
        "candidate_space_validation": {"valid": True},
        "optimizer_result": {
            "backend": "ortools",
            "status": "success",
            "optimizer": "ortools-routing",
            "routes": [{"vehicle_id": "R002"}],
            "unassigned_task_ids": [],
            "estimated_makespan_ms": 9000,
        },
        "optimizer_assignment_validation": {"valid": True},
        "traffic_schedule": {
            "valid": True,
            "planner": "prioritized_sipp",
            "routes": [{}],
            "reservations": [],
            "station_reservations": [],
            "conflicts": [],
            "total_wait_ms": 0,
            "total_service_ms": 2000,
            "makespan_ms": 10000,
        },
        "route_validation": {"valid": True},
        "mapf_validation": {"valid": True},
        "logical_operation_coverage_validation": {"valid": True},
        "simulation_plan": response["plan"],
    }

    stage_contract = build_stage_contract(
        scenario, response, trace, debug, backend="ortools"
    )
    assert stage_contract["status"] == "PASS"
    assert [value["stage"] for value in stage_contract["stages"]] == [
        "01_ROUTER_NORMALIZATION",
        "02_AGENT_RETRIEVAL",
        "03_CONTEXT_SNAPSHOT",
        "04_FORMULATION",
        "05_SOLVER_PAYLOAD",
        "06_OPTIMIZER",
        "07_MAPF_TRAFFIC",
        "08_SIMULATION_PLAN",
    ]

    write_stage_contract_artifacts(tmp_path, stage_contract, debug)
    assert (tmp_path / "stage_contract.json").is_file()
    assert (tmp_path / "stage_contract.csv").is_file()
    assert (tmp_path / "stage_contract.md").is_file()
    assert (tmp_path / "stage_outputs" / "04_formulation.json").is_file()
    assert (tmp_path / "stage_outputs" / "07_mapf.json").is_file()


def test_emergency_reserve_helper_exposes_infeasible_count_for_validator() -> None:
    """The helper must never reserve every robot; validators reject the mismatch."""

    request = _policy_request().model_copy(
        update={
            "constraints": _policy_request().constraints.model_copy(
                update={"reserve_robot_count": 2}
            )
        }
    )
    included, reserved = _apply_emergency_reserve(
        candidate_robot_ids=["R002", "R003"],
        battery_by_robot={"R002": 72.0, "R003": 91.0},
        request=request,
    )
    assert included == ["R002"]
    assert reserved == ["R003"]
    assert len(reserved) != request.constraints.reserve_robot_count

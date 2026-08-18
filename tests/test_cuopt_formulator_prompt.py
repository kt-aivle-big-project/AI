from types import SimpleNamespace

from app.domain.schemas import (
    CuOptDynamicInputDraft,
    CuOptFleetDraft,
    CuOptTaskDraft,
    SituationGraphCompleteness,
    SituationNode,
    SituationPathEvidence,
    SituationRelation,
    WarehouseSituationGraph,
)
from app.graph.cuopt_formulation import _enforce_authoritative_inbound_contract
from app.prompts.cuopt_formulator import CUOPT_FORMULATOR_SYSTEM, PROMPT_VERSION
from app.services.cuopt_formulation_service import _bounded_rule_vehicle_floor
from app.services.simulation_plan_service import RollingHorizonReplanService


def test_agent_reassesses_implicit_cost_default_from_live_context() -> None:
    assert PROMPT_VERSION == "13.35-contextual-objective-selection"
    assert "implicit MIN_TOTAL_COST value is a neutral request default" in CUOPT_FORMULATOR_SYSTEM
    assert "large independent wave" in CUOPT_FORMULATOR_SYSTEM
    assert "BALANCED, not implicit MIN_TOTAL_COST" in CUOPT_FORMULATOR_SYSTEM


def test_agent_coordinates_parallelism_with_soft_route_balance() -> None:
    assert "Keep objective_profile and minimum_vehicle_count coherent" in CUOPT_FORMULATOR_SYSTEM
    assert "Do not use minimum_vehicle_count as a substitute for workload balance" in CUOPT_FORMULATOR_SYSTEM
    assert "Never output raw" in CUOPT_FORMULATOR_SYSTEM
    assert "solver weights" in CUOPT_FORMULATOR_SYSTEM


def test_low_battery_rule_replan_preserves_only_replannable_task_capacity() -> None:
    active = SimpleNamespace(
        logical_operations=[
            SimpleNamespace(task_ids=["TASK-001"]),
            SimpleNamespace(task_ids=["TASK-002"]),
            SimpleNamespace(task_ids=["TASK-003"]),
        ],
        robots=[
            SimpleNamespace(
                robot_id=f"R00{index}",
                steps=[
                    SimpleNamespace(
                        step_type="SERVICE",
                        end_at_ms=4000,
                        task_id=f"TASK-00{index}_PICK",
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )
    snapshot = SimpleNamespace(
        completed_task_bases=["TASK-003"],
        locked_task_bases=[],
        replan_at_sim_time_ms=2500,
    )

    prior_task_vehicles = (
        RollingHorizonReplanService._remaining_task_vehicle_count(active, snapshot)
    )

    assert prior_task_vehicles == 2
    assert _bounded_rule_vehicle_floor(
        requested=prior_task_vehicles,
        eligible_vehicle_count=5,
        actionable_cycle_count=25,
    ) == 2
    assert _bounded_rule_vehicle_floor(
        requested=4,
        eligible_vehicle_count=3,
        actionable_cycle_count=2,
    ) == 2


def test_agent_invalid_and_duplicate_putaway_slots_are_grounded_without_retry() -> None:
    def path(path_id: str, purpose: str, source: str, target: str, time_ms: int):
        return SituationPathEvidence(
            path_id=path_id,
            purpose=purpose,
            source_node_id=source,
            target_node_id=target,
            node_sequence=[source, target],
            edge_sequence=[f"E-{path_id}"],
            cost=float(time_ms),
            travel_time_ms=time_ms,
        )

    inbound_nodes = [
        SituationNode(
            node_id=f"inbound:{inbound_id}",
            node_type="inbound",
            attributes={
                "inbound_id": inbound_id,
                "item_id": f"ITEM-{index}",
                "handling_unit_id": f"HU-{index}",
                "transport_unit_count": 1,
                "priority": "medium",
            },
        )
        for index, inbound_id in enumerate(("IN-001", "IN-002"), start=1)
    ]
    relations = []
    for inbound_id in ("IN-001", "IN-002"):
        relations.extend(
            [
                SituationRelation(
                    relation_id=f"{inbound_id}-pickup",
                    source_node_id=f"inbound:{inbound_id}",
                    target_node_id="map:N160",
                    relation_type="PICKUP_FROM",
                ),
                SituationRelation(
                    relation_id=f"{inbound_id}-slot-1",
                    source_node_id=f"inbound:{inbound_id}",
                    target_node_id="rack_slot:K0_1:L1",
                    relation_type="PUTAWAY_TO",
                ),
                SituationRelation(
                    relation_id=f"{inbound_id}-slot-2",
                    source_node_id=f"inbound:{inbound_id}",
                    target_node_id="rack_slot:K0_2:L1",
                    relation_type="PUTAWAY_TO",
                ),
            ]
        )
    relations.extend(
        [
            SituationRelation(
                relation_id="slot-1-access",
                source_node_id="rack_slot:K0_1:L1",
                target_node_id="map:R0_1",
                relation_type="HAS_ACCESS_POINT",
            ),
            SituationRelation(
                relation_id="slot-2-access",
                source_node_id="rack_slot:K0_2:L1",
                target_node_id="map:R0_2",
                relation_type="HAS_ACCESS_POINT",
            ),
        ]
    )
    graph = WarehouseSituationGraph(
        snapshot_id="SNAP-1",
        captured_at="2026-08-18T00:00:00Z",
        graph_version="GRAPH-1",
        inventory_version="INV-1",
        runtime_version="RUN-1",
        nodes=inbound_nodes,
        relations=relations,
        path_evidence=[
            path("robot-pickup", "ROBOT_TO_PICKUP", "C01", "N160", 10),
            path("pickup-slot-1", "PICKUP_TO_DELIVERY", "N160", "R0_1", 20),
            path("pickup-slot-2", "PICKUP_TO_DELIVERY", "N160", "R0_2", 30),
        ],
        completeness=SituationGraphCompleteness(
            order_facts_complete=True,
            inventory_candidates_complete=True,
            robot_candidates_complete=True,
            map_paths_complete=True,
            runtime_constraints_complete=True,
            ready_for_formulation=True,
        ),
        summary="putaway grounding",
    )
    draft = CuOptDynamicInputDraft(
        snapshot_id="SNAP-1",
        graph_version="GRAPH-1",
        formulation_source="llm",
        objective_profile="MIN_TOTAL_COST",
        tasks=[
            CuOptTaskDraft(
                task_id="TASK-001",
                operation_type="INBOUND_ITEM",
                order_id="IN-001",
                item_id="wrong",
                stock_id="wrong",
                rack_id="K9_9",
                rack_level=3,
                pickup_node="N999",
                delivery_node="R9_9",
                demand=99,
                priority="low",
            ),
            CuOptTaskDraft(
                task_id="TASK-002",
                operation_type="INBOUND_ITEM",
                order_id="IN-002",
                item_id="wrong",
                stock_id="wrong",
                rack_id="K0_1",
                rack_level=1,
                pickup_node="N999",
                delivery_node="R0_1",
                demand=99,
                priority="low",
            ),
        ],
        fleet=CuOptFleetDraft(included_robot_ids=["R1"]),
        formulation_summary="invalid agent putaway",
    )

    grounded = _enforce_authoritative_inbound_contract(draft=draft, graph=graph)

    assert [
        (task.rack_id, task.rack_level, task.delivery_node, task.pickup_node)
        for task in grounded.tasks
    ] == [
        ("K0_1", 1, "R0_1", "N160"),
        ("K0_2", 1, "R0_2", "N160"),
    ]

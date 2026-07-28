import pytest

from app.models import OptimizationWeights
from app.services.command_language import (
    optimization_weights_for_priority,
    parse_deterministic_command,
)


def test_explicit_single_work_exclusion_sets_strict_task_scope() -> None:
    parsed = parse_deterministic_command(
        "창고 2의 출고 작업 W-003 하나만 지금 실행해줘. 다른 작업은 포함하지 마."
    )

    assert parsed.target_task_ids == ["W-003"]
    assert "EXPLICIT_TASK_SCOPE_ONLY" in parsed.hard_constraints


def test_named_work_id_is_preserved_for_strict_task_scope() -> None:
    parsed = parse_deterministic_command(
        "창고 2의 출고 작업 DEMO-W-OUT-2-A 하나만 지금 실행해줘. "
        "다른 작업은 포함하지 마."
    )

    assert parsed.target_task_ids == ["DEMO-W-OUT-2-A"]
    assert parsed.extracted_task_ids == ["DEMO-W-OUT-2-A"]
    assert "EXPLICIT_TASK_SCOPE_ONLY" in parsed.hard_constraints


@pytest.mark.parametrize(
    ("text", "target", "action", "filter_name"),
    [
        ("현재 사용 가능한 로봇을 조회해줘", "ROBOT", "STATUS", "AVAILABLE"),
        ("전체 로봇 상태를 보여줘", "ROBOT", "STATUS", None),
        ("배터리 부족 로봇을 알려줘", "ROBOT", "STATUS", "LOW_BATTERY"),
        ("고장 또는 지연 로봇 상태를 조회해줘", "ROBOT", "STATUS", "FAILED_OR_DELAYED"),
        ("전체 작업 목록을 보여줘", "WORK", "LIST", None),
        ("실행 중 작업을 조회해줘", "WORK", "STATUS", "EXECUTING"),
        ("계획된 작업을 보여줘", "WORK", "STATUS", "PLANNED"),
        ("지연 작업 상태를 알려줘", "WORK", "STATUS", "DELAYED"),
        ("미배정 작업을 조회해줘", "WORK", "STATUS", "UNASSIGNED"),
        ("작업 W-003 상태를 보여줘", "WORK", "DETAIL", None),
        ("로봇 R-02 상태를 알려줘", "ROBOT", "DETAIL", None),
        ("현재 활성 계획을 조회해줘", "PLAN", "LIST", None),
        ("현재 계획 버전을 알려줘", "PLAN", "LIST", None),
        ("시뮬레이션 이력을 조회해줘", "SIMULATION", "HISTORY", None),
        ("재계획 이력을 보여줘", "REPLAN", "HISTORY", None),
        ("Verification 검증 결과를 조회해줘", "VERIFICATION", "LIST", None),
        ("초기화 이력을 알려줘", "RESET", "HISTORY", None),
        ("최적화 근거를 조회해줘", "EVIDENCE", "LIST", None),
        ("경로 근거를 보여줘", "EVIDENCE", "LIST", None),
    ],
)
def test_query_command_catalog(text, target, action, filter_name) -> None:
    parsed = parse_deterministic_command(text)
    assert parsed.command_kind == "QUERY"
    assert parsed.query_target == target
    assert parsed.query_action == action
    if filter_name:
        assert filter_name in parsed.query_filters


@pytest.mark.parametrize(
    "text",
    [
        "A상품 현재 가용 재고를 알려줘",
        "A 상품 현재 가용 재고를 알려줘",
        "A품목 현재 가용 재고를 알려줘",
        "A 품목 현재 가용 재고를 알려줘",
        "상품 A 현재 가용 재고를 알려줘",
        "품목 A 현재 가용 재고를 알려줘",
    ],
)
def test_labeled_single_inventory_item_is_extracted(text: str) -> None:
    parsed = parse_deterministic_command(text)

    assert parsed.command_kind == "QUERY"
    assert parsed.query_target == "INVENTORY"
    assert parsed.item_ids == ["A"]




def test_new_inventory_work_can_be_fixed_to_named_robot() -> None:
    parsed = parse_deterministic_command(
        "R2-02에게 E상품 15 BOX 출고 작업을 배정해서 시뮬레이션해줘."
    )

    assert parsed.target_robot_ids == ["R2-02"]
    assert len(parsed.inventory_operations) == 1
    operation_id = parsed.inventory_operations[0].operation_id
    assert [(row.task_id, row.robot_id) for row in parsed.fixed_robot_assignments] == [
        (operation_id, "R2-02")
    ]

def test_labeled_multiple_inventory_items_are_extracted() -> None:
    parsed = parse_deterministic_command("A와 B 상품 재고를 조회해줘")

    assert parsed.item_ids == ["A", "B"]


@pytest.mark.parametrize(
    ("text", "field", "expected"),
    [
        ("전체 작업을 계획해줘", "intent", "DAILY_PLAN"),
        ("작업 W-003만 계획해줘", "target_task_ids", ["W-003"]),
        ("긴급 작업 W-003을 삽입해줘", "intent", "INSERT_TASK"),
        ("R-02를 제외하고 계획해줘", "excluded_robot_ids", ["R-02"]),
        ("작업 W-003을 로봇 R-02에 고정해서 계획해줘", "fixed_robot_assignments", [("W-003", "R-02")]),
        ("로봇 2대로 계획해줘", "robot_limit", 2),
        ("기존 작업 배정 유지해서 계획해줘", "hard_constraints", "PRESERVE_ASSIGNMENTS"),
        ("기존 계획 변경 최소화로 계획해줘", "optimization_priority", "MINIMIZE_PLAN_CHANGE"),
        ("실행 중 작업 보호하고 재계획해줘", "hard_constraints", "PROTECT_EXECUTING_TASKS"),
        ("노드 6을 제외하고 계획해줘", "excluded_node_ids", [6]),
        ("10->11 통로를 제외하고 계획해줘", "excluded_edge_ids", ["10->11"]),
        ("배터리 임계치 이하 로봇 제외하고 계획해줘", "hard_constraints", "EXCLUDE_LOW_BATTERY_ROBOTS"),
        ("이동거리 최소화로 계획해줘", "optimization_priority", "MINIMIZE_DISTANCE"),
        ("완료시간 최소화로 계획해줘", "optimization_priority", "MINIMIZE_MAKESPAN"),
        ("마감 준수와 지연 최소화로 계획해줘", "optimization_priority", "MINIMIZE_TARDINESS"),
        ("에너지 최소화로 계획해줘", "optimization_priority", "MINIMIZE_ENERGY"),
        ("최소 로봇으로 계획해줘", "optimization_priority", "MINIMIZE_ROBOTS"),
        ("거리와 완료시간 균형으로 계획해줘", "optimization_priority", "BALANCE_DISTANCE_MAKESPAN"),
        ("전체 작업 완료시간을 최소화", "optimization_priority", "MINIMIZE_MAKESPAN"),
        ("총 소요시간을 줄여줘", "optimization_priority", "MINIMIZE_MAKESPAN"),
        ("이동거리를 최소화", "optimization_priority", "MINIMIZE_DISTANCE"),
        ("납기 지연을 최소화", "optimization_priority", "MINIMIZE_TARDINESS"),
        ("에너지 사용을 줄여줘", "optimization_priority", "MINIMIZE_ENERGY"),
        ("로봇을 가장 적게 사용", "optimization_priority", "MINIMIZE_ROBOTS"),
        ("기존 계획을 최대한 유지", "optimization_priority", "MINIMIZE_PLAN_CHANGE"),
    ],
)
def test_planning_command_catalog(text, field, expected) -> None:
    parsed = parse_deterministic_command(text)
    assert parsed.command_kind in {"PLAN", "EXECUTE"}
    value = getattr(parsed, field)
    if field == "fixed_robot_assignments":
        value = [(row.task_id, row.robot_id) for row in value]
    if field == "hard_constraints":
        assert expected in value
    else:
        assert value == expected


@pytest.mark.parametrize(
    ("priority", "focused_field"),
    [
        ("MINIMIZE_MAKESPAN", "makespan"),
        ("MINIMIZE_DISTANCE", "total_distance"),
        ("MINIMIZE_TARDINESS", "tardiness"),
        ("MINIMIZE_ENERGY", "energy"),
        ("MINIMIZE_ROBOTS", "robot_activation"),
        ("MINIMIZE_PLAN_CHANGE", "plan_change"),
    ],
)
def test_named_optimization_priority_changes_its_optimizer_weight(
    priority, focused_field
) -> None:
    defaults = OptimizationWeights()
    focused = optimization_weights_for_priority(priority)

    assert getattr(focused, focused_field) > getattr(defaults, focused_field)


@pytest.mark.parametrize(
    ("text", "event_type"),
    [
        ("R-02가 고장이라고 가정해서 시뮬레이션해줘", "ROBOT_FAILURE"),
        ("R-02 배터리 부족을 가정해서 돌려줘", "LOW_BATTERY"),
        ("노드 6 폐쇄를 가정해서 시뮬레이션해줘", "NODE_CLOSURE"),
        ("10->11 통로 폐쇄를 가정해줘", "EDGE_CLOSURE"),
        ("충전소 사용 불가를 가정해줘", "CHARGER_UNAVAILABLE"),
        ("긴급 주문 추가를 가정해줘", "URGENT_ORDER"),
        ("작업 W-003 지연을 가정해줘", "TASK_DELAY"),
        ("재고 부족을 가정해서 시뮬레이션해줘", "INVENTORY_SHORTAGE"),
    ],
)
def test_hypothetical_commands_are_simulation_only(text, event_type) -> None:
    parsed = parse_deterministic_command(text)
    assert parsed.intent == "HYPOTHETICAL_SCENARIO"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert event_type in {row.event_type for row in parsed.hypothetical_events}


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("실행하지 말고 계획만 보여줘", "PLAN_ONLY"),
        ("경로까지만 계산해줘", "PLAN_ONLY"),
        ("시뮬레이션해줘", "SIMULATE_ONLY"),
        ("실제 반영하지 말고 미리 검증해줘", "SIMULATE_ONLY"),
        ("실제 실행해줘", "EXECUTE"),
        ("검증 후 로봇에 전송해줘", "EXECUTE"),
    ],
)
def test_explicit_execution_modes(text, mode) -> None:
    assert parse_deterministic_command(text).execution_mode == mode


@pytest.mark.parametrize(
    ("text", "dimension"),
    [
        ("로봇 2대와 3대를 비교해줘", "ROBOT_COUNT"),
        ("거리 우선과 시간 우선 계획을 비교해줘", "TOTAL_DISTANCE"),
        ("이전 계획과 현재 계획을 비교해줘", "PLAN_VERSION"),
        ("R-02와 R-03을 비교해줘", "ROBOT"),
        ("에너지 기준 계획을 비교해줘", "ENERGY"),
        ("어느 게 좋은지 골라줘", None),
    ],
)
def test_comparison_is_classified_for_what_if_execution(text, dimension) -> None:
    parsed = parse_deterministic_command(text)
    assert parsed.intent == "SCENARIO_COMPARISON"
    assert parsed.comparison_requested is True
    assert parsed.requires_future_feature is False
    assert parsed.execution_mode == "SIMULATE_ONLY"
    if dimension:
        assert dimension in parsed.comparison_dimensions
    else:
        assert "comparison_dimensions" in parsed.missing_information


def test_identifier_normalization_and_ambiguous_execute_safety() -> None:
    parsed = parse_deterministic_command("로봇 2번과 로봇02, R-3 상태를 보여줘")
    assert parsed.extracted_robot_ids == ["R-02", "R-03"]
    ambiguous = parse_deterministic_command("효율적으로 처리해줘")
    assert ambiguous.execution_mode != "EXECUTE"
    assert "requested_execution_mode" in ambiguous.missing_information


def test_specific_simulation_identifier_is_extracted() -> None:
    parsed = parse_deterministic_command("시뮬레이션 sim-2026-01 상태를 조회해줘")
    assert parsed.query_target == "SIMULATION"
    assert parsed.target_simulation_ids == ["sim-2026-01"]


@pytest.mark.parametrize(
    "text",
    [
        "W-003을 시뮬레이션해줘",
        "W-003을 가상 시뮬레이션해줘",
        "W-003 시뮬레이션을 돌려줘",
        "W-003을 시뮬레이션하고 상세히 보여줘",
        "W-003 작업을 테스트해줘",
        "W-003 계획을 검증해줘",
        "W-003 작업을 수행해줘",
        "W-003 실행 결과를 보여줘",
    ],
)
def test_new_simulation_execution_verbs_take_priority_over_show_modifier(
    text: str,
) -> None:
    parsed = parse_deterministic_command(text)
    assert parsed.command_kind == "PLAN"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert parsed.target_task_ids == ["W-003"]
    assert parsed.intent != "SIMULATION_QUERY"


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("기존 시뮬레이션 결과를 조회해줘", "LIST"),
        ("지난 W-003 시뮬레이션 결과를 보여줘", "DETAIL"),
        ("시뮬레이션 이력을 보여줘", "HISTORY"),
        ("최근 시뮬레이션 목록", "LIST"),
        ("저장된 시뮬레이션 상세 조회", "DETAIL"),
        ("이전 결과를 다시 보여줘", "LIST"),
    ],
)
def test_only_explicit_stored_simulation_language_is_a_query(
    text: str,
    action: str,
) -> None:
    parsed = parse_deterministic_command(text)
    assert parsed.command_kind == "QUERY"
    assert parsed.intent == "SIMULATION_QUERY"
    assert parsed.execution_mode == "PLAN_ONLY"
    assert parsed.query_action == action


def test_simulation_id_detail_is_query_and_identifier_is_preserved() -> None:
    parsed = parse_deterministic_command(
        "simulation_id sim-2026-01 결과를 상세 조회해줘"
    )
    assert parsed.command_kind == "QUERY"
    assert parsed.intent == "SIMULATION_QUERY"
    assert parsed.query_action == "DETAIL"
    assert parsed.target_simulation_ids == ["sim-2026-01"]


def test_new_execution_wins_when_previous_result_and_rerun_are_both_present() -> None:
    parsed = parse_deterministic_command(
        "이전 결과도 보여주고 W-003을 새로 다시 시뮬레이션해줘"
    )
    assert parsed.command_kind == "PLAN"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert parsed.target_task_ids == ["W-003"]


def test_warehouse_qualified_robot_ids_remain_distinct() -> None:
    parsed = parse_deterministic_command(
        "R2-01, R2-02, R2-03 상태를 알려줘"
    )
    assert parsed.extracted_robot_ids == ["R2-01", "R2-02", "R2-03"]


def test_warehouse_qualified_robot_id_is_not_parsed_as_closed_edge() -> None:
    parsed = parse_deterministic_command(
        "R2-03의 배터리가 21%라고 가정하고 E 30 BOX 출고를 시뮬레이션해줘"
    )
    assert parsed.extracted_robot_ids == ["R2-03"]
    assert parsed.assumed_closed_edges == []
    assert parsed.excluded_edge_ids == []


def test_explicit_closed_edge_is_still_extracted_next_to_robot_id() -> None:
    parsed = parse_deterministic_command(
        "R2-03은 제외하고 통로 2013-2014를 폐쇄했다고 가정해줘"
    )
    assert parsed.extracted_robot_ids == ["R2-03"]
    assert [(edge.from_node, edge.to_node) for edge in parsed.assumed_closed_edges] == [
        (2013, 2014)
    ]


def test_korean_natural_closed_edge_is_extracted_next_to_robot_query() -> None:
    parsed = parse_deterministic_command(
        "R2-03의 상태를 조회하고, 2013번 노드와 2014번 노드 사이 통로는 폐쇄된 것으로 가정해줘."
    )
    assert parsed.extracted_robot_ids == ["R2-03"]
    assert [(edge.from_node, edge.to_node) for edge in parsed.assumed_closed_edges] == [
        (2013, 2014)
    ]
    assert parsed.assumed_closed_node_ids == []
    assert any(
        event.event_type == "EDGE_CLOSURE"
        and event.target_ids == ["2013->2014"]
        for event in parsed.hypothetical_events
    )


@pytest.mark.parametrize(
    "text",
    [
        "A 5 BOX 출고해줘",
        "A상품 5 BOX 출고해줘",
        "A 품목 5 BOX 출고해줘",
        "상품 A 5 BOX 출고해줘",
        "품목 A 5 BOX 출고해줘",
    ],
)
def test_labeled_inventory_operation_quantity_is_preserved(text: str) -> None:
    parsed = parse_deterministic_command(text)

    assert parsed.intent == "OUTBOUND"
    assert parsed.item_ids == ["A"]
    assert len(parsed.inventory_operations) == 1
    operation = parsed.inventory_operations[0]
    assert operation.operation_type == "OUTBOUND"
    assert operation.item_id == "A"
    assert operation.quantity_boxes == 5


def test_hypothetical_node_closure_preserves_outbound_operation() -> None:
    parsed = parse_deterministic_command(
        "노드 2013을 폐쇄했다고 가정하고 E상품 15 BOX 출고를 시뮬레이션해줘."
    )

    assert parsed.intent == "HYPOTHETICAL_SCENARIO"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert parsed.assumed_closed_node_ids == [2013]
    assert len(parsed.inventory_operations) == 1
    operation = parsed.inventory_operations[0]
    assert operation.operation_type == "OUTBOUND"
    assert operation.item_id == "E"
    assert operation.quantity_boxes == 15


def test_korean_number_first_node_closure_preserves_outbound_operation() -> None:
    parsed = parse_deterministic_command(
        "2013번 노드를 폐쇄했다고 가정하고 E상품 15 BOX 출고를 시뮬레이션해줘."
    )

    assert parsed.intent == "HYPOTHETICAL_SCENARIO"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert parsed.assumed_closed_node_ids == [2013]
    assert any(
        event.event_type == "NODE_CLOSURE" and event.target_ids == ["2013"]
        for event in parsed.hypothetical_events
    )
    assert len(parsed.inventory_operations) == 1
    operation = parsed.inventory_operations[0]
    assert operation.operation_type == "OUTBOUND"
    assert operation.item_id == "E"
    assert operation.quantity_boxes == 15


def test_conditional_partial_outbound_is_not_misclassified_as_hypothetical() -> None:
    parsed = parse_deterministic_command(
        "E상품 재고가 부족하면 가능한 수량만 부분 출고하도록 "
        "150 BOX 출고를 시뮬레이션해줘."
    )

    assert parsed.intent == "OUTBOUND"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert parsed.item_ids == ["E"]
    assert parsed.quantity == 150
    assert parsed.hypothetical_events == []
    assert len(parsed.inventory_operations) == 1
    operation = parsed.inventory_operations[0]
    assert operation.operation_type == "OUTBOUND"
    assert operation.item_id == "E"
    assert operation.quantity_boxes == 150
    assert operation.allow_partial_fulfillment is True


def test_partial_fulfillment_negation_is_preserved() -> None:
    parsed = parse_deterministic_command(
        "E상품 재고가 부족해도 부분 출고하지 말고 150 BOX 출고를 "
        "시뮬레이션해줘."
    )

    assert len(parsed.inventory_operations) == 1
    assert parsed.inventory_operations[0].allow_partial_fulfillment is False


def test_hypothetical_node_closure_preserves_robot_exclusion() -> None:
    parsed = parse_deterministic_command(
        "R2-03을 제외하고 2013번 노드를 폐쇄했다고 가정한 뒤 "
        "E상품 15 BOX 출고를 시뮬레이션해줘."
    )

    assert parsed.intent == "HYPOTHETICAL_SCENARIO"
    assert parsed.execution_mode == "SIMULATE_ONLY"
    assert parsed.excluded_robot_ids == ["R2-03"]
    assert parsed.excluded_node_ids == [2013]
    assert parsed.assumed_closed_node_ids == [2013]
    assert parsed.item_ids == ["E"]
    assert parsed.quantity == 15


def test_hypothetical_battery_override_and_outbound_node_are_structured():
    result = parse_deterministic_command(
        "R2-03의 배터리가 현재 21%라고 가정하고 E상품 30 BOX를 "
        "R2-03에 고정 배정해. 최소 배터리를 유지하지 못하면 active "
        "CHARGER 노드 중 비용이 가장 낮은 충전소에서 필요한 만큼 "
        "충전한 뒤 출고 노드 2146으로 이동해. 실제 Redis 배터리는 "
        "변경하지 말고 시뮬레이션만 해."
    )
    assert result.intent == "HYPOTHETICAL_SCENARIO"
    assert result.execution_mode == "SIMULATE_ONLY"
    assert result.target_node_ids == [2146]
    assert result.target_node_type == "OUTBOUND"
    assert result.hard_constraints == [
        "MINIMUM_REQUIRED_CHARGE",
        "MINIMUM_BATTERY_AT_ALL_TIMES",
    ]
    assert len(result.hypothetical_events) == 1
    event = result.hypothetical_events[0]
    assert event.event_type == "LOW_BATTERY"
    assert event.target_ids == ["R2-03"]
    assert event.parameters.battery_percent == 21
    assert result.fixed_robot_assignments[0].robot_id == "R2-03"
    assert result.inventory_operations[0].item_id == "E"
    assert result.inventory_operations[0].quantity_boxes == 30

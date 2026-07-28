from datetime import UTC, datetime
from types import SimpleNamespace

from app.models import AtomicTask, ScopeDecision
from app.planning import nodes
from app.services.command_language import parse_deterministic_command
from app.services.local_optimizer import LocalOptimizer
from app.services.scheduling import parse_explicit_time_windows


FUTURE_SCHEDULE_COMMAND = (
    "2026년 7월 27일 오전 9시부터 10시까지 C상품 10 BOX를 출고 노드 2146으로 "
    "이동하고, 오전 10시부터 11시까지 D상품 5 BOX를 출고 노드 2146으로 이동하는 "
    "계획을 가상 시뮬레이션해줘. 계획 시작 전에는 로봇이 일반 통로나 저장·출고 "
    "작업 노드를 장시간 점유하지 않도록 해줘."
)

CHARGE_COMMAND = (
    "2026년 7월 27일 오전 8시를 기준으로 R2-02의 배터리가 21%라고 가정해. "
    "C상품 10 BOX를 저장 노드 2088에서 출고 노드 2146으로 이동하기 전에 "
    "R2-02를 안전하게 도달 가능한 active CHARGER로 보내 배터리를 80%까지 "
    "충전한 뒤 출고 작업을 수행해줘. 충전 이동시간, 충전시간, 충전 슬롯 점유시간, "
    "작업 후 최종 배터리와 MOVE, WAIT, CHARGE, PICKUP, DROPOFF 명령을 포함해 "
    "가상 시뮬레이션해줘. 실제 Redis 배터리는 변경하지 마."
)

REFERENCE = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


def test_absolute_schedule_date_is_preserved_and_inherited_by_following_window() -> None:
    windows = parse_explicit_time_windows(
        FUTURE_SCHEDULE_COMMAND,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )

    assert [(row["earliest_start"], row["latest_finish"]) for row in windows] == [
        (
            datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 2, 0, tzinfo=UTC),
        ),
    ]

    parsed = parse_deterministic_command(
        FUTURE_SCHEDULE_COMMAND,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    assert [(row.earliest_start, row.latest_finish) for row in parsed.scheduled_task_constraints] == [
        (
            datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 2, 0, tzinfo=UTC),
        ),
    ]


def test_explicit_robot_charge_workflow_creates_fixed_assignment() -> None:
    parsed = parse_deterministic_command(
        CHARGE_COMMAND,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    operation_id = parsed.inventory_operations[0].operation_id

    assert parsed.target_robot_ids == ["R2-02"]
    assert [(row.task_id, row.robot_id) for row in parsed.fixed_robot_assignments] == [
        (operation_id, "R2-02")
    ]
    assert parsed.hypothetical_events[0].parameters.battery_percent == 21


def test_explicit_robot_charge_workflow_applies_override_and_generates_charge(monkeypatch) -> None:
    parsed = parse_deterministic_command(
        CHARGE_COMMAND,
        reference_time=REFERENCE,
        warehouse_timezone="Asia/Seoul",
    )
    operation_id = parsed.inventory_operations[0].operation_id
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            time_step_seconds=1,
            min_robot_battery=20.0,
            energy_per_distance=0.1,
            charge_target_battery=80.0,
            charge_rate_percent_per_minute=60.0,
            battery_safety_margin_percent=0.5,
            opportunity_charging_enabled=False,
            opportunity_charge_min_idle_minutes=15.0,
            opportunity_charge_target_battery=95.0,
            opportunity_charge_min_gain_percent=2.0,
        ),
    )
    snapshot = {
        "captured_at": REFERENCE.isoformat(),
        "sql": {
            "robots": [
                {
                    "robot_id": "R2-02",
                    "node_id": 1,
                    "battery": 90,
                    "status": "IDLE",
                    "max_load": 100,
                },
                {
                    "robot_id": "R2-01",
                    "node_id": 1,
                    "battery": 100,
                    "status": "IDLE",
                    "max_load": 100,
                },
            ],
            "inventory": [],
        },
        "redis": {
            "robots": [
                {
                    "robot_id": "R2-02",
                    "node_id": 1,
                    "battery": 90,
                    "last_event": "IDLE",
                },
                {
                    "robot_id": "R2-01",
                    "node_id": 1,
                    "battery": 100,
                    "last_event": "IDLE",
                },
            ],
            "temporary_closures": [],
            "active_plan": None,
        },
        "graph": {
            "nodes": [
                {"node_id": 1, "node_type": "AISLE", "active": True},
                {
                    "node_id": 2,
                    "node_type": "CHARGER",
                    "active": True,
                    "charging_cost": 1,
                },
                {"node_id": 3, "node_type": "STORAGE", "active": True},
                {"node_id": 2146, "node_type": "OUTBOUND", "active": True},
            ],
            "edges": [
                {
                    "from_node": 1,
                    "to_node": 2,
                    "distance": 1,
                    "travel_seconds": 1,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 2,
                    "to_node": 3,
                    "distance": 4,
                    "travel_seconds": 4,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 1,
                    "to_node": 3,
                    "distance": 10,
                    "travel_seconds": 10,
                    "direction": "BOTH",
                    "active": True,
                },
                {
                    "from_node": 3,
                    "to_node": 2146,
                    "distance": 6,
                    "travel_seconds": 6,
                    "direction": "BOTH",
                    "active": True,
                },
            ],
        },
    }
    state = {
        "command": {"warehouse_id": 2, "text": CHARGE_COMMAND},
        "interpretation": parsed.model_dump(mode="json"),
        "scope": ScopeDecision(
            plan_mode="INITIAL_PLAN",
            optimization_goal="Swagger charge regression",
            reason_summary="test",
        ).model_dump(mode="json"),
        "snapshot": snapshot,
        "required_tasks": [
            AtomicTask(
                task_id=f"{operation_id}:drop",
                work_id=operation_id,
                action="DROP",
                item_id="C",
                quantity=10,
                source_candidates=[3],
                target_candidates=[2146],
            ).model_dump(mode="json")
        ],
    }

    problem = nodes.build_optimization_problem_node(state)["optimization_problem"]
    assert next(row for row in problem["robots"] if row["robot_id"] == "R2-02")["battery"] == 21
    assert problem["tasks"][0]["assigned_robot_id"] == "R2-02"

    plan = LocalOptimizer(
        time_step_seconds=1,
        min_robot_battery=20,
        energy_per_distance=0.1,
        charge_target_battery=80,
        charge_rate_percent_per_minute=60,
    ).optimize(problem)
    assert any(row.action == "CHARGE" and row.robot_id == "R2-02" for row in plan.scheduled_tasks)
    assert all(
        row.robot_id == "R2-02"
        for row in plan.scheduled_tasks
        if row.work_id == operation_id
    )

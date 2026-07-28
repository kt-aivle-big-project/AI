"""Run representative HTTP flows against a configured development stack.

The script intentionally uses the public API only.  It expects PostgreSQL,
Neo4j, Redis, the Supervisor API, and the mock robot gateway to be running.
It is an integration smoke test, not a replacement for pytest.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

import httpx


class SmokeFailure(RuntimeError):
    """Raised when a representative flow violates its public contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _work_id(response: dict[str, Any]) -> str:
    assignments = response.get("data", {}).get("task_assignments", [])
    for row in assignments:
        value = row.get("work_id")
        if value:
            return str(value)
        task_id = str(row.get("task_id") or "")
        if task_id:
            return task_id.split(":", 1)[0]
    raise SmokeFailure("계획 응답에서 대화 상속 검증에 사용할 work_id를 찾지 못했습니다.")


def _assignment(response: dict[str, Any]) -> dict[str, Any]:
    assignments = response.get("data", {}).get("task_assignments", [])
    if not assignments:
        raise SmokeFailure("EXECUTE 응답에 작업 배정이 없습니다.")
    return dict(assignments[0])


@dataclass
class SmokeRunner:
    base_url: str
    gateway_url: str
    warehouse_id: int
    timeout: float
    inventory_smoke: bool = False
    client: httpx.Client = field(init=False)
    gateway: httpx.Client = field(init=False)
    results: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout,
        )
        self.gateway = httpx.Client(
            base_url=self.gateway_url.rstrip("/"),
            timeout=self.timeout,
        )

    def close(self) -> None:
        self.client.close()
        self.gateway.close()

    def step(self, name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            payload = operation()
        except Exception as exc:
            print(f"[FAIL] {name}: {exc}")
            raise
        self.results.append({"name": name, "status": "PASS"})
        print(f"[PASS] {name}")
        return payload

    @staticmethod
    def json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmokeFailure(
                f"{response.request.method} {response.request.url} 응답이 JSON이 아닙니다."
            ) from exc
        if response.is_error:
            raise SmokeFailure(
                f"{response.request.method} {response.request.url} -> "
                f"{response.status_code}: {payload}"
            )
        if not isinstance(payload, dict):
            raise SmokeFailure("API 응답 최상위 값은 object여야 합니다.")
        return payload

    def command(
        self,
        text: str,
        mode: str,
        *,
        conversation_id: str | None = None,
        parent_command_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "text": text,
            "requested_execution_mode": mode,
            # The integration smoke test validates the complete legacy/public
            # contract. End-user clients can omit this and receive AUTO view.
            "response_view": "FULL",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if parent_command_id:
            body["parent_command_id"] = parent_command_id
        return self.json(self.client.post("/v1/planning/commands", json=body))

    def health(self) -> dict[str, Any]:
        supervisor = self.json(self.client.get("/health"))
        gateway = self.json(self.gateway.get("/health"))
        _require(supervisor.get("status") == "ok", f"Supervisor health 실패: {supervisor}")
        _require(gateway.get("status") == "ok", f"Mock Gateway health 실패: {gateway}")
        return {"supervisor": supervisor, "gateway": gateway}

    def query(self) -> dict[str, Any]:
        result = self.command("현재 로봇 상태를 조회해줘", "PLAN_ONLY")
        _require(result.get("status") == "COMPLETED", f"QUERY 상태 오류: {result}")
        _require(result.get("intent") == "ROBOT_QUERY", f"QUERY intent 오류: {result}")
        _require("robot_count" in result.get("data", {}), "QUERY 실제 집계값이 없습니다.")
        return result

    def clarification(self) -> dict[str, Any]:
        result = self.command("효율적으로 처리해줘", "AUTO")
        _require(
            result.get("status") == "CLARIFICATION_REQUIRED",
            f"Clarification 상태 오류: {result}",
        )
        _require(result.get("clarification", {}).get("clarification_id"), "clarification_id 누락")
        forbidden = {"local_optimize", "build_routes", "simulation", "dispatch_plan"}
        trace_nodes = {row.get("node") for row in result.get("trace", [])}
        _require(not trace_nodes.intersection(forbidden), "Clarification 이후 계산 단계가 실행됐습니다.")
        return result

    def plan_only(self) -> dict[str, Any]:
        result = self.command("현재 미완료 작업의 계획만 만들어줘", "PLAN_ONLY")
        _require(result.get("status") == "PLAN_READY", f"PLAN_ONLY 상태 오류: {result}")
        _require(result.get("plan_version"), "PLAN_ONLY plan_version 누락")
        _require(result.get("collision_plan"), "PLAN_ONLY 충돌 방지 경로 누락")
        return result

    def simulate_only(self) -> dict[str, Any]:
        result = self.command(
            "현재 미완료 작업을 실제 반영하지 말고 가상 시뮬레이션해줘",
            "SIMULATE_ONLY",
        )
        _require(
            result.get("status") == "SIMULATION_SUCCESS",
            f"SIMULATE_ONLY 상태 오류: {result}",
        )
        _require(result.get("simulation_id"), "SIMULATE_ONLY simulation_id 누락")
        _require(result.get("simulation", {}).get("valid") is True, "시뮬레이션 검증 실패")
        return result

    def what_if(self) -> dict[str, Any]:
        body = {
            "warehouse_id": self.warehouse_id,
            "optimization_priority": "MINIMIZE_MAKESPAN",
            "scenarios": [
                {
                    "name": "이동거리 우선",
                    "description": "동일 업무의 이동거리 우선 가상 시나리오",
                    "optimization_priority": "MINIMIZE_DISTANCE",
                },
                {
                    "name": "완료시간 우선",
                    "description": "동일 업무의 완료시간 우선 가상 시나리오",
                    "optimization_priority": "MINIMIZE_MAKESPAN",
                },
            ],
        }
        result = self.json(self.client.post("/v1/scenario-comparisons", json=body))
        _require(result.get("status") in {"COMPLETED", "PARTIAL_SUCCESS"}, f"What-if 실패: {result}")
        _require(len(result.get("scenarios", [])) == 2, "What-if 시나리오 수가 2가 아닙니다.")
        return result

    def conversation(self, work_id: str) -> dict[str, Any]:
        conversation_id = f"smoke-{uuid4()}"
        first = self.command(
            f"{work_id} 작업만 로봇 최대 2대로 전체 완료시간 우선 가상 시뮬레이션해줘",
            "SIMULATE_ONLY",
            conversation_id=conversation_id,
        )
        second = self.command(
            "이번에는 같은 조건에서 이동거리를 최소화하는 가상 시뮬레이션을 실행해줘",
            "SIMULATE_ONLY",
            conversation_id=conversation_id,
            parent_command_id=first.get("command_id"),
        )
        first_targets = first.get("interpretation", {}).get("target_task_ids", [])
        second_targets = second.get("interpretation", {}).get("target_task_ids", [])
        _require(first_targets and second_targets == first_targets, "target_task_ids 상속 실패")
        _require(second.get("parent_command_id") == first.get("command_id"), "parent_command_id 연결 실패")
        _require(second.get("optimization_profile") == "MINIMIZE_DISTANCE", "최적화 목표 override 실패")
        _require(second.get("simulation_id") != first.get("simulation_id"), "후속 simulation_id가 재사용됐습니다.")
        return {"first": first, "second": second, "conversation_id": conversation_id}

    def execute_mock(self) -> dict[str, Any]:
        self.json(self.gateway.delete("/received-plans"))
        result = self.command("현재 미완료 작업을 실제 실행해줘", "EXECUTE")
        _require(result.get("status") == "DISPATCHED", f"EXECUTE 상태 오류: {result}")
        received = self.json(self.gateway.get("/received-plans"))
        _require(received.get("count", 0) >= 1, "Mock Gateway가 계획을 수신하지 못했습니다.")
        latest = received.get("plans", [])[-1]
        _require(latest.get("plan_version") == result.get("plan_version"), "Gateway plan_version 불일치")
        return {"planning": result, "gateway": received}

    def event_replan(self, execute_result: dict[str, Any]) -> dict[str, Any]:
        planning = execute_result["planning"]
        assignment = _assignment(planning)
        payload = {
            "warehouse_id": self.warehouse_id,
            "robot_id": str(assignment["robot_id"]),
            "work_id": assignment.get("work_id"),
            "task_id": assignment.get("task_id"),
            "event_type": "ROBOT_DELAYED",
            "payload": {"delay_seconds": 30},
            "execution_context": "REAL",
            "simulation_id": None,
        }
        result = self.json(self.client.post("/v1/execution/events", json=payload))
        if result.get("failure_reason") == "REPEATED_FAILURE_DETECTED":
            _require(result.get("status") == "FAILED", "반복 이벤트 차단 상태가 올바르지 않습니다.")
            return result
        _require(result.get("auto_replan_requested") is True, f"자동 재계획 미요청: {result}")
        _require(result.get("replan_request_id"), "event replan request_id 누락")
        _require(
            result.get("status") in {"APPROVAL_REQUIRED", "REPLAN_VERIFIED"},
            f"event replan 상태 오류: {result}",
        )
        return result

    def history_and_correlation(self, response: dict[str, Any]) -> dict[str, Any]:
        command_id = str(response["command_id"])
        history = self.json(self.client.get(f"/v1/commands/{command_id}"))
        stages = self.json(self.client.get(f"/v1/commands/{command_id}/stages"))
        stored = history.get("command_history", {})
        summary_ids = (stored.get("result_summary") or {}).get("correlation_ids", {})
        expected = {
            "command_id": response.get("command_id"),
            "conversation_id": response.get("conversation_id"),
            "parent_command_id": response.get("parent_command_id"),
            "plan_version": response.get("plan_version"),
            "simulation_id": response.get("simulation_id"),
        }
        _require(summary_ids == expected, f"command_history correlation 불일치: {summary_ids} != {expected}")
        _require(stages.get("stages"), "planning_stage_log가 비어 있습니다.")
        for row in stages["stages"]:
            stage_ids = (row.get("details") or {}).get("correlation_ids")
            _require(stage_ids == expected, f"stage correlation 불일치: {row.get('node_name')}")
        return {"history": history, "stages": stages}

    def reset_and_history(self, simulation_id: str) -> dict[str, Any]:
        reset = self.json(
            self.client.post(
                f"/v1/simulations/{simulation_id}/reset",
                json={
                    "warehouse_id": self.warehouse_id,
                    "actor_id": "scripts/smoke_test.py",
                    "reason": "대표 API smoke test 종료 후 가상 상태 초기화",
                },
            )
        )
        _require(reset.get("status") in {"RESET", "ALREADY_RESET"}, f"RESET 실패: {reset}")
        logs = self.json(
            self.client.get(
                f"/v1/warehouses/{self.warehouse_id}/simulation-reset-logs"
            )
        )
        matching = [
            row for row in logs.get("reset_logs", [])
            if row.get("target_simulation_id") == simulation_id
        ]
        _require(matching, "RESET 이력이 조회되지 않습니다.")
        return {"reset": reset, "history": logs}

    def inventory_sufficient_multi_outbound(self) -> dict[str, Any]:
        result = self.command(
            "A 10박스와 B 10박스를 출고장으로 보내는 가상 시뮬레이션을 실행해줘",
            "SIMULATE_ONLY",
        )
        _require(result.get("status") == "SIMULATION_SUCCESS", f"다중 출고 실패: {result}")
        feasibility = result.get("inventory_feasibility", {})
        _require(feasibility.get("status") == "PASS", f"재고 검증 실패: {feasibility}")
        _require(result.get("simulation_id"), "재고 시뮬레이션 ID 누락")
        return result

    def inventory_shortage_emergency(self) -> dict[str, Any]:
        result = self.command(
            "오늘 주문과 입고 예정 데이터를 기준으로 가상 시뮬레이션해줘",
            "SIMULATE_ONLY",
        )
        feasibility = result.get("inventory_feasibility", {})
        _require(
            feasibility.get("status") in {"FAILED", "PARTIAL_SUCCESS"},
            f"부족 재고가 정상 작업으로 처리됐습니다: {feasibility}",
        )
        _require(result.get("emergency_review_items"), "비상 검토 구조가 없습니다.")
        _require(feasibility.get("blocked_work_ids"), "부족 작업이 차단되지 않았습니다.")
        _require(
            feasibility.get("independent_work_ids"),
            "재고가 충분한 독립 작업이 계속 진행되지 않았습니다.",
        )
        return result

    def inventory_open_orders(self) -> dict[str, Any]:
        result = self.command(
            "오늘 주문과 입고 예정 데이터를 기준으로 가상 시뮬레이션해줘",
            "SIMULATE_ONLY",
        )
        interpretation = result.get("interpretation", {})
        _require(
            interpretation.get("load_open_inventory_orders") is True,
            "PostgreSQL open order 조회 의도가 반영되지 않았습니다.",
        )
        _require(result.get("inventory_feasibility"), "시간축 재고 결과가 없습니다.")
        return result

    def inventory_simulation_isolation(self) -> dict[str, Any]:
        first = self.command("A 1박스 출고 가상 시뮬레이션해줘", "SIMULATE_ONLY")
        second = self.command("A 1박스 출고 가상 시뮬레이션해줘", "SIMULATE_ONLY")
        _require(first.get("simulation_id") != second.get("simulation_id"), "simulation 격리 실패")
        _require(
            all(row.get("scope") == "SIMULATION" for row in first.get("inventory_reservations", [])),
            "SIMULATE_ONLY 예약 scope 오류",
        )
        first_items = first.get("inventory_feasibility", {}).get("item_results", [])
        second_items = second.get("inventory_feasibility", {}).get("item_results", [])
        _require(first_items == second_items, "SIMULATE_ONLY가 실제 PostgreSQL 재고를 변경했습니다.")
        return {"first": first, "second": second}

    def inventory_execute_and_complete(
        self, execute_result: dict[str, Any]
    ) -> dict[str, Any]:
        planning = execute_result["planning"]
        active_reservations = [
            row
            for row in planning.get("inventory_reservations", [])
            if row.get("scope") == "ACTIVE_PLAN" and row.get("status") == "RESERVED"
        ]
        _require(active_reservations, "EXECUTE가 ACTIVE_PLAN 재고 예약을 만들지 않았습니다.")
        assignments = planning.get("data", {}).get("task_assignments", [])
        assignment = next(
            (row for row in assignments if row.get("inventory_allocations")),
            None,
        )
        _require(assignment is not None, "실제 완료 검증용 재고 작업 배정이 없습니다.")
        event_id = f"smoke-outbound-{uuid4()}"
        event = {
            "event_id": event_id,
            "warehouse_id": self.warehouse_id,
            "robot_id": str(assignment["robot_id"]),
            "work_id": assignment.get("work_id"),
            "task_id": assignment.get("task_id"),
            "event_type": "TASK_COMPLETED",
            "inventory_deltas": [
                {
                    "warehouse_item_id": row["warehouse_item_id"],
                    "quantity_delta": -int(
                        row.get("quantity") or row.get("quantity_boxes") or 0
                    ),
                }
                for row in assignment.get("inventory_allocations", [])
            ],
            "payload": {"plan_version": planning.get("plan_version")},
            "execution_context": "REAL",
            "simulation_id": None,
        }
        completed = self.json(self.client.post("/v1/execution/events", json=event))
        _require(completed.get("sql_committed") is True, f"OUTBOUND SQL 차감 실패: {completed}")
        replayed = self.json(self.client.post("/v1/execution/events", json=event))
        _require(
            replayed.get("commit_result", {}).get("idempotent_replay") is True,
            f"OUTBOUND 중복 완료 event가 차단되지 않았습니다: {replayed}",
        )
        inbound_event = {
            "event_id": f"smoke-inbound-{uuid4()}",
            "warehouse_id": self.warehouse_id,
            "robot_id": "INBOUND-DOCK",
            "event_type": "INBOUND_AVAILABLE",
            "payload": {
                "inbound_id": f"DEMO-IN-{self.warehouse_id}-F",
                "item_id": "F",
                "quantity_boxes": 20,
                "lot_id": "DEMO-LOT-F-02",
                "storage_node_id": 2,
            },
            "execution_context": "REAL",
            "simulation_id": None,
        }
        inbound = self.json(
            self.client.post("/v1/execution/events", json=inbound_event)
        )
        _require(inbound.get("sql_committed") is True, f"INBOUND SQL 증가 실패: {inbound}")
        inbound_replay = self.json(
            self.client.post("/v1/execution/events", json=inbound_event)
        )
        _require(
            inbound_replay.get("commit_result", {}).get("idempotent_replay") is True,
            "INBOUND 중복 완료 event가 차단되지 않았습니다.",
        )
        return {
            "active_reservations": active_reservations,
            "outbound": completed,
            "outbound_replay": replayed,
            "inbound": inbound,
            "inbound_replay": inbound_replay,
        }

    def inventory_capacity_contract(self) -> dict[str, Any]:
        result = self.command(
            "오전 7시에 A 1박스가 입고되고 검수 완료는 오전 7시 10분이야. 가상 시뮬레이션해줘",
            "SIMULATE_ONLY",
        )
        capacity = result.get("capacity_feasibility", {})
        _require(
            capacity.get("status") in {"PASS", "NOT_CONFIGURED"},
            f"capacity 계약 오류: {capacity}",
        )
        return result

    def run_inventory_flows(self) -> None:
        self.step("inventory: sufficient multi-outbound", self.inventory_sufficient_multi_outbound)
        self.step("inventory: AVAILABLE/open-order timeline", self.inventory_open_orders)
        self.step("inventory: shortage emergency and independent flow", self.inventory_shortage_emergency)
        self.step("inventory: simulation isolation", self.inventory_simulation_isolation)
        self.step("inventory: optional capacity", self.inventory_capacity_contract)

    def run(self) -> None:
        self.step("health and dependencies", self.health)
        self.step("QUERY", self.query)
        self.step("CLARIFICATION_REQUIRED", self.clarification)
        plan = self.step("PLAN_ONLY", self.plan_only)
        simulation = self.step("SIMULATE_ONLY", self.simulate_only)
        self.step("What-if comparison", self.what_if)
        conversation = self.step(
            "conversation inheritance/override",
            lambda: self.conversation(_work_id(plan)),
        )
        self.step(
            "correlation identifiers in logs",
            lambda: self.history_and_correlation(conversation["second"]),
        )
        if self.inventory_smoke:
            self.run_inventory_flows()
        executed = self.step("EXECUTE with mock gateway", self.execute_mock)
        if self.inventory_smoke:
            self.step(
                "inventory: EXECUTE reservation and completion events",
                lambda: self.inventory_execute_and_complete(executed),
            )
        self.step("event-driven replan", lambda: self.event_replan(executed))
        self.step(
            "reset and reset history",
            lambda: self.reset_and_history(str(simulation["simulation_id"])),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Warehouse API integration smoke test")
    parser.add_argument("--warehouse-id", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mock-gateway-url", default="http://127.0.0.1:9000")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--inventory-smoke",
        action="store_true",
        help="Migration 010과 seed_inventory_demo_data.py 적용 후 재고 시나리오를 추가 실행합니다.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner = SmokeRunner(
        base_url=args.base_url,
        gateway_url=args.mock_gateway_url,
        warehouse_id=args.warehouse_id,
        timeout=args.timeout,
        inventory_smoke=args.inventory_smoke,
    )
    try:
        runner.run()
    except (SmokeFailure, httpx.HTTPError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        runner.close()
    print(
        json.dumps(
            {"status": "PASSED", "steps": runner.results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

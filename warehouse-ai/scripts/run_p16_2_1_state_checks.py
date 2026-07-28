from __future__ import annotations

import json

from app.planning.nodes import _sanitize_base_plan_terminal_tasks


def main() -> int:
    base = {
        "plan_version": "demo-v1",
        "cuopt_plan": {
            "scheduled_tasks": [
                {"task_id": "DEMO-W-OUT-2-A:pick", "work_id": "DEMO-W-OUT-2-A"},
                {"task_id": "DEMO-W-OUT-2-A:drop", "work_id": "DEMO-W-OUT-2-A"},
                {"task_id": "DEMO-W-OUT-2-F:pick", "work_id": "DEMO-W-OUT-2-F"},
            ]
        },
    }
    snapshot = {
        "sql": {
            "work_statuses": [
                {"work_id": "DEMO-W-OUT-2-A", "status": "COMPLETED"},
                {"work_id": "DEMO-W-OUT-2-F", "status": "NEW"},
            ]
        }
    }
    cleaned, dropped = _sanitize_base_plan_terminal_tasks(base, snapshot)
    remaining = [
        row.get("work_id")
        for row in ((cleaned or {}).get("cuopt_plan") or {}).get("scheduled_tasks", [])
    ]
    checks = {
        "terminal_a_tasks_dropped": dropped == [
            "DEMO-W-OUT-2-A:drop",
            "DEMO-W-OUT-2-A:pick",
        ],
        "open_f_task_preserved": remaining == ["DEMO-W-OUT-2-F"],
    }
    result = {"all_passed": all(checks.values()), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

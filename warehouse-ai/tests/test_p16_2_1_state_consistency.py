from app.planning.nodes import _sanitize_base_plan_terminal_tasks


def _plan(*rows):
    return {"plan_version": "v1", "cuopt_plan": {"scheduled_tasks": list(rows)}}


def test_completed_work_is_removed_from_active_plan_base():
    base = _plan(
        {"task_id": "DEMO-W-OUT-2-A:pick", "work_id": "DEMO-W-OUT-2-A"},
        {"task_id": "DEMO-W-OUT-2-A:drop", "work_id": "DEMO-W-OUT-2-A"},
        {"task_id": "DEMO-W-OUT-2-F:pick", "work_id": "DEMO-W-OUT-2-F"},
    )
    snapshot = {
        "sql": {
            "work_statuses": [
                {"work_id": "DEMO-W-OUT-2-A", "status": "COMPLETED"},
                {"work_id": "DEMO-W-OUT-2-F", "status": "NEW"},
            ]
        }
    }

    cleaned, dropped = _sanitize_base_plan_terminal_tasks(base, snapshot)

    assert dropped == [
        "DEMO-W-OUT-2-A:drop",
        "DEMO-W-OUT-2-A:pick",
    ]
    assert [
        row["work_id"] for row in cleaned["cuopt_plan"]["scheduled_tasks"]
    ] == ["DEMO-W-OUT-2-F"]


def test_unknown_candidate_work_is_preserved():
    base = _plan({"task_id": "COMMAND-1:pick", "work_id": "COMMAND-1"})
    snapshot = {
        "sql": {
            "work_statuses": [
                {"work_id": "DEMO-W-OUT-2-A", "status": "COMPLETED"}
            ]
        }
    }

    cleaned, dropped = _sanitize_base_plan_terminal_tasks(base, snapshot)

    assert dropped == []
    assert cleaned == base


def test_all_terminal_tasks_make_active_plan_unusable():
    base = _plan(
        {"task_id": "A:pick", "work_id": "A"},
        {"task_id": "A:drop", "work_id": "A"},
    )
    snapshot = {"sql": {"work_statuses": [{"work_id": "A", "status": "CANCELLED"}]}}

    cleaned, dropped = _sanitize_base_plan_terminal_tasks(base, snapshot)

    assert cleaned is None
    assert dropped == ["A:drop", "A:pick"]

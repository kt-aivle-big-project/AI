"""Run deterministic P16.3.4 charge-target and replan-state checks.

P16.3.4 keeps all P16.3.3 acceptance rules and adds exact post-charge
operation target preservation, fixed-robot rescheduling during LOCAL_REPLAN,
stale route-state clearing, and candidate-route task-id deduplication.
"""

from __future__ import annotations

import json

from scripts.run_p16_3_3_final_checks import main as _p16_3_3_main
from tests.test_p16_3_4_charge_replan_state import (
    test_changed_candidate_route_does_not_duplicate_old_task_ids,
    test_full_policy_command_records_all_battery_hard_constraints,
    test_local_replan_reschedules_fixed_robot_task_and_regenerates_charge,
    test_prepare_replan_clears_all_route_derived_state,
    test_route_reconciliation_never_reduces_80_percent_post_charge_target,
)


def main() -> int:
    if _p16_3_3_main() != 0:
        return 1

    checks = {
        "battery_policy_hard_constraints_recorded": (
            test_full_policy_command_records_all_battery_hard_constraints
        ),
        "post_charge_target_remains_exactly_80_percent": (
            test_route_reconciliation_never_reduces_80_percent_post_charge_target
        ),
        "local_replan_regenerates_mandatory_charge": (
            test_local_replan_reschedules_fixed_robot_task_and_regenerates_charge
        ),
        "candidate_route_task_ids_not_duplicated": (
            test_changed_candidate_route_does_not_duplicate_old_task_ids
        ),
        "local_replan_clears_stale_derived_state": (
            test_prepare_replan_clears_all_route_derived_state
        ),
    }
    results: dict[str, bool] = {}
    for name, function in checks.items():
        function()
        results[name] = True

    output = {
        "all_passed": all(results.values()),
        "base_p16_3_3_checks_passed": True,
        "checks": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

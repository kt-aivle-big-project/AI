"""Run deterministic P16.3.3 battery-safe charger acceptance checks.

The checks keep every P16.3.2 acceptance rule and add path-wide reserve,
operation-ready 80% charging, safe-before-cost charger selection, recoverable
local replan classification, and CHARGE timeline compression.
"""

from __future__ import annotations

import json

from scripts.run_p16_3_2_final_checks import main as _p16_3_2_main
from tests.test_battery_charging import (
    test_no_safe_charger_leaves_task_unassigned_for_local_replan,
    test_required_charge_uses_operation_ready_target,
    test_unsafe_cheapest_charger_is_rejected_before_cost_comparison,
)
from tests.test_p11_battery_override import (
    test_p11_verification_blocks_missing_charge_false_pass,
)
from tests.test_p16_3_3_battery_safe_charger import (
    test_command_parser_marks_path_wide_battery_constraint,
    test_full_view_compresses_repeated_charge_timeline,
    test_real_failure_shape_rejects_cheap_unsafe_charger_and_selects_safe_fallback,
)


def main() -> int:
    if _p16_3_2_main() != 0:
        return 1

    checks = {
        "path_wide_battery_constraint_parsed": (
            test_command_parser_marks_path_wide_battery_constraint
        ),
        "unsafe_cheapest_charger_rejected": (
            test_unsafe_cheapest_charger_is_rejected_before_cost_comparison
        ),
        "real_failure_shape_selects_safe_fallback": (
            test_real_failure_shape_rejects_cheap_unsafe_charger_and_selects_safe_fallback
        ),
        "charge_target_is_operation_ready_80_percent": (
            test_required_charge_uses_operation_ready_target
        ),
        "no_safe_charger_requests_replan": (
            test_no_safe_charger_leaves_task_unassigned_for_local_replan
        ),
        "battery_failure_classified_local_replan": (
            test_p11_verification_blocks_missing_charge_false_pass
        ),
        "charge_timeline_range_compressed": (
            test_full_view_compresses_repeated_charge_timeline
        ),
    }
    results: dict[str, bool] = {}
    for name, function in checks.items():
        function()
        results[name] = True

    output = {
        "all_passed": all(results.values()),
        "base_p16_3_2_checks_passed": True,
        "checks": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

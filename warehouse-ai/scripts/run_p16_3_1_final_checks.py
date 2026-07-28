"""Run deterministic P16.3.1 final acceptance checks.

No external PostgreSQL, Redis, Neo4j, OpenAI, or robot gateway connection is
used.  The checks cover the P16.3 mixed daily plan plus the partial-plan target
verification semantics added after the real Swagger integration run.
"""

from __future__ import annotations

import json

from tests.test_p16_3_final_daily_plan_integration import (
    test_combined_command_is_real_daily_plan_not_hypothetical_shortage,
    test_combined_optimizer_route_and_robot_commands_include_only_b_and_c,
    test_final_report_marks_combined_result_as_partial_success,
    test_inventory_shortage_blocks_only_a_and_preserves_b_and_c,
    test_mixed_task_generation_keeps_operation_specific_destinations,
)
from tests.test_p16_3_1_partial_target_verification import (
    test_target_check_is_skipped_when_all_outbound_operations_are_inventory_blocked,
    test_target_check_passes_when_planned_outbound_uses_requested_destination,
    test_target_check_still_blocks_when_planned_outbound_uses_wrong_destination,
)


def main() -> int:
    checks = {
        "combined_command_is_daily_plan": test_combined_command_is_real_daily_plan_not_hypothetical_shortage,
        "a_shortage_blocks_only_a": test_inventory_shortage_blocks_only_a_and_preserves_b_and_c,
        "operation_specific_destinations": test_mixed_task_generation_keeps_operation_specific_destinations,
        "b_and_c_commands_generated": test_combined_optimizer_route_and_robot_commands_include_only_b_and_c,
        "partial_success_reported": test_final_report_marks_combined_result_as_partial_success,
        "blocked_outbound_target_check_skipped": test_target_check_is_skipped_when_all_outbound_operations_are_inventory_blocked,
        "planned_outbound_wrong_target_blocked": test_target_check_still_blocks_when_planned_outbound_uses_wrong_destination,
        "planned_outbound_correct_target_passed": test_target_check_passes_when_planned_outbound_uses_requested_destination,
    }
    results: dict[str, bool] = {}
    for name, function in checks.items():
        function()
        results[name] = True
    output = {"all_passed": all(results.values()), "checks": results}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run deterministic P16.3 final mixed daily-plan checks.

This script does not connect to PostgreSQL, Redis, Neo4j, or a robot gateway.
It exercises the parser, inventory projection, mixed task generation, local
optimizer, time-expanded routing, robot adapter, and final report policy with
an in-memory deterministic warehouse fixture.
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


def main() -> int:
    checks = {
        "combined_command_is_daily_plan": test_combined_command_is_real_daily_plan_not_hypothetical_shortage,
        "a_shortage_blocks_only_a": test_inventory_shortage_blocks_only_a_and_preserves_b_and_c,
        "operation_specific_destinations": test_mixed_task_generation_keeps_operation_specific_destinations,
        "b_and_c_commands_generated": test_combined_optimizer_route_and_robot_commands_include_only_b_and_c,
        "partial_success_reported": test_final_report_marks_combined_result_as_partial_success,
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

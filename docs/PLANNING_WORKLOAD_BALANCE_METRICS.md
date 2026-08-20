# Planning workload-balance metrics

Rule/Agent comparison reports expose workload concentration in addition to
makespan, distance, fleet effort, and MAPF wait.

## Counting contract

- One physical cycle is one optimizer pickup-delivery pair, not one PICK/DROP row.
- Every candidate robot is included. An unused candidate has zero cycles and zero
  scheduled work, so a one-robot route cannot look perfectly balanced merely
  because unused robots were omitted.
- Auxiliary unpaired route steps do not increase the physical-cycle count.
- When MAPF output exists, scheduled work is the sum of MOVE, WAIT, and SERVICE
  step durations. Otherwise optimizer completion time minus vehicle availability
  is used as the solve-depth estimate.

## Per-branch fields

- `physical_cycle_count_by_robot`
- `min_physical_cycles_per_robot`
- `max_physical_cycles_per_robot`
- `physical_cycle_count_range`
- `physical_cycle_count_standard_deviation`
- `physical_cycle_count_coefficient_of_variation`
- `physical_cycle_count_gini_coefficient`
- `scheduled_work_ms_by_robot`
- `scheduled_work_time_range_ms`
- `scheduled_work_time_standard_deviation_ms`
- `scheduled_work_time_coefficient_of_variation`
- `route_finish_at_ms_by_robot`
- `max_robot_finish_at_ms`

Lower range, standard deviation, coefficient of variation, Gini coefficient, and
maximum finish time indicate less concentration. Raw maps remain in each branch
result so a distribution such as `19 / 1 / 1 / 0 / 0 / 0` is directly auditable.

## Rule/Agent comparison fields

`operational_comparison` reports the Rule value, median valid Agent value, and
lower-is-better improvement percentage for:

- physical-cycle count range;
- physical-cycle count standard deviation;
- physical-cycle count coefficient of variation;
- physical-cycle count Gini coefficient;
- scheduled-work time range;
- scheduled-work time standard deviation;
- scheduled-work time coefficient of variation; and
- maximum robot finish time.

These metrics are diagnostics and do not yet change the calibrated operational
verdict or resource guardrails. Scenario-specific acceptance thresholds should be
chosen after PC01-PC05 captures establish stable baselines.

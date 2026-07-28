# P16.5.5 Multi-Robot Rebalance and Congestion Hotfix

## Problem

A live complex-day run completed successfully, but all fourteen atomic tasks were assigned to `R2-03`. The managed cuOpt response was converted into hard `assigned_robot_id` and `frozen` constraints before the local warehouse scheduler ran. Consequently, all other robots appeared as `FROZEN_ASSIGNMENT_MISMATCH`, even though the user had not fixed a robot.

The route evidence also reported one-step `RESERVATION_CONFLICT_WAIT` rows with no blocking robot. Those rows were same-robot service continuity, not real multi-robot conflicts.

## Fix

- Daily `INITIAL_PLAN` and `GLOBAL_REPLAN` requests may enable local multi-robot rebalance.
- cuOpt keeps responsibility for the global visit order.
- Explicit user robot assignments, frozen work and fixed scope remain hard constraints.
- Unconstrained PICK/DROP pairs are redistributed by the local scheduler.
- Each `same_robot_group` still keeps its PICK and DROP on one robot.
- A work-group load penalty balances independent missions across available robots.
- Reservations owned by the same robot are ignored during continuation path search.
- Same-robot service dwell is no longer reported as a reservation conflict.
- A command-named congestion node receives a soft routing penalty, allowing a reasonable alternative path to be selected.

## Observable output

`data.optimizer_postprocessing.parallel_robot_rebalance`

`data.optimizer_postprocessing.cuopt_assignment_application`

`data.congestion_avoidance`

## Version

- API version: `2.5.5`
- Response schema: `p16.5.5`

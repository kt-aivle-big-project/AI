# P16.5.13 Gate 1.3 — Relocation Window Fallback Hotfix

## Problem

A hypothetical low-battery Swagger scenario correctly applied the fixed robot and
battery override, but the managed charge-visit second pass omitted MOVE_TO_NEXT,
PICK, and DROP. The bounded CPU fallback also failed with
`SECOND_PASS_TASK_MISSING`.

## Root cause

The post-charge relocation copied the successor business task's `earliest_start`
into its own `latest_finish`. In live snapshots that value can be the inventory
lot's historical `available_at`, earlier than the planning reference. The
relocation therefore received an already-expired HARD_WINDOW.

## Fix

- Use a successor start as relocation `latest_finish` only when it is later than
  the charge completion time.
- Otherwise schedule the relocation as ASAP with no stale upper bound.
- Keep the CHARGE -> MOVE_TO_NEXT -> PICK -> DROP dependency chain and fixed
  robot binding unchanged.
- API version: `2.5.13.3`.

## Regression

Added a live-shaped regression where the planning reference is 2026-07-27 while
inventory availability is 2026-07-24. The CPU contract fallback must preserve the
entire chain with no unassigned tasks.

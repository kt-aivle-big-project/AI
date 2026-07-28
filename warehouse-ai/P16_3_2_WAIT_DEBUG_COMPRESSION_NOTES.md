# P16.3.2 WAIT / DEBUG Compression Final Patch

## Purpose

Long idle gaps between scheduled tasks remain time-expanded inside the planner
for deterministic vertex/edge reservation and collision checking. P16.3.2
prevents those internal rows from being copied verbatim into LLM prompts and
public DEBUG/FULL responses.

## Changes

- Consecutive same-node WAIT route evidence is represented as one range with
  `depart_step`, `arrive_step`, and `travel_steps`.
- Verification LLM payloads omit raw `wait_evidence` and `resolution_events`;
  bounded range summaries and counts are sent instead.
- Final-report LLM DEBUG payloads compress waits and cap route/candidate detail.
- Deterministic DEBUG reports compress waits without truncating non-WAIT route
  evidence.
- FULL API views compress collision-plan waypoints, simulation waypoints, and
  timeline WAIT events while preserving raw/compressed counts.
- Internal persisted planner/simulator state and robot commands remain
  unchanged. RobotAdapter continues to merge WAIT commands into duration-based
  commands.

## Real Swagger sample reduction

Using the P16.3.1 integration response uploaded on 2026-07-24:

- collision route waypoints: 2,178 -> 45
- collision wait evidence rows: 2,135 -> 2 ranges
- simulation timeline rows: 2,178 -> 45

The source response was not mutated during shaping.

## Acceptance

```powershell
python -m scripts.run_p16_3_2_final_checks
python -m pytest -q
```

Expected response schema: `p16.3.2`.

# P16.5.13 Gate 1 — Scenario Consistency Baseline

## Goal

Remove the 10 previously accepted regression failures before adding any new
runtime-event features. No API request or response contract is changed in this
gate, so `response_schema_version` remains `p16.5.12.1`.

## Fixed contracts

1. **Future plan activation versus operational idle**
   - A bounded MAPF route does not reserve a robot before a far-future plan is
     activated.
   - When a designated holding node exists, the planner still emits an explicit
     initial relocation.
   - Once a route is active, long idle remains subject to the strict whitelist.

2. **Cross-robot reservation order**
   - Robots are processed by earliest task start, then numeric task priority,
     then deterministic tie-breakers.
   - Input list order and robot identifier no longer allow a normal task to
     reserve a bottleneck before an emergency task.

3. **Standalone DROP continuation**
   - A DROP-only plan can represent a robot that is already carrying a load.
   - PICK-before-DROP validation is still mandatory when the same transfer has
     an explicit PICK or legacy MOVE task in the plan.

4. **Verification evidence contract**
   - Missing shared-resource evidence is blocking only when the planning state
     declares that the shared-resource scheduler stage was present.
   - Direct verification of a lower-level optimizer/routing unit no longer
     receives a false missing-stage blocker.

## Validation

```text
Focused regression: 40 passed
Full suite: 713 passed, 0 failed
compileall: PASS
```

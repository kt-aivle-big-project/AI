# P16.5.13 Gate 1.2 Activation / Second-pass Hotfix

## Triggered Swagger failures

### Future daily schedule

The absolute date was parsed correctly after Gate 1.1, but a plan for the next
warehouse operating date still activated robot movement immediately. The route
created opportunity charging and holding-area waits thousands of time steps
before the first business task. Shared-resource validation then rejected the
plan with `MAXIMUM_IDLE_DURATION_EXCEEDED`.

### Explicit low-battery charge workflow

The fixed assignment and 21% hypothetical battery override were applied, but a
managed cuOpt second pass omitted business/relocation members of the explicit
charge chain. The post-pass contract correctly detected
`SECOND_PASS_TASK_MISSING`, but the pipeline terminated instead of using the
existing CPU fallback policy.

## Changes

1. `build_optimization_problem_node` calculates a warehouse-local activation
   boundary. When the first scheduled task belongs to a later local date than
   the planning reference, `defer_initial_pre_activation=true` is recorded.
2. Initial opportunity charging is not generated outside that activation
   boundary.
3. Time-expanded routing does not reserve route, charger, or holding occupancy
   before deferred activation. Same-day initial idle behavior is unchanged.
4. Managed cuOpt second-pass results are validated for complete robot-bound
   charge chains. A missing task triggers one deterministic CPU second-pass
   fallback on the same enriched problem, followed by the same contract check.
5. The fallback is observable through
   `optimizer_execution.charge_visit_two_pass.contract_fallback_used` and the
   optimizer warning/attempt history.

## Focused validation in the artifact build environment

- `compileall app`: PASS
- activation + charge fallback regression: 2 passed
- idle/opportunity/shared-resource/two-pass regression: 24 passed
- date/language regression: 161 passed
- combined executed tests: 185 passed, 0 failed

The full suite requires the project's LangGraph, Neo4j, Redis and related
runtime dependencies. Run `python -m scripts.run_p16_5_13_gate1_2_checks` in the
project virtual environment to execute the focused gate followed by the full
suite.

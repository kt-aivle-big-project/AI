# P16.5.13 Gate 1.1 Swagger E2E Hotfix

## Observed failures

1. An explicit schedule date such as `2026년 7월 27일 오전 9시부터 10시까지` lost its date prefix. The window parser matched only `오전 9시부터 10시까지` and resolved it against the current warehouse-local date.
2. A following window without a repeated date, such as `오전 10시부터 11시까지`, did not inherit the preceding explicit date.
3. The charge scenario `R2-02를 ... CHARGER로 보내 ... 충전한 뒤 출고 작업을 수행` expressed an explicit robot workflow, but fixed assignment detection recognized only the words `고정`, `배정`, or `담당`.

## Changes

- Added absolute Korean and ISO schedule-date expressions:
  - `YYYY년 M월 D일`
  - `M월 D일`
  - `YYYY-M-D`
- Preserved the latest explicit date across following undated windows in the same command.
- Added conservative single-robot workflow assignment detection for explicit `send/charge then perform outbound/inbound work` commands.
- Kept API response schema at `p16.5.12.1`.

## Validation

- New Swagger sentence regressions: 3 passed
- Scheduling/language/charging focused regression: 173 passed
- Full suite: 716 passed, 0 failed
- compileall: PASS

# P16.2.3 Unknown Item Execution Termination

## Problem

A planning request for an item absent from the PostgreSQL item master was routed to
`CLARIFICATION_REQUIRED` even when no similar registered item candidate existed. The
final response could also repeat the same warehouse-timezone warning.

## Behavior

- No similar candidate:
  - stop without asking the user again;
  - return `EMERGENCY_REVIEW_REQUIRED`;
  - generate zero tasks and zero robot commands;
  - keep `earliest_full_fulfillment_at` as `null`;
  - explain that the item is not registered.
- Similar candidate after punctuation/spacing normalization:
  - return `CLARIFICATION_REQUIRED`;
  - expose only matching registered item options.
- Final top-level warning strings are deduplicated while preserving order.

## Safety

An unknown item is never silently substituted, registered, stocked, scheduled, or
dispatched. The system changes no PostgreSQL, Redis, Neo4j, simulation, or gateway
state for the rejected request.

# Changelog

## v13.27.0 — BE-Centered Structured Input

- Uses numeric Spring `simulationRunId` to select warehouse/runtime scope.
- Makes request `structured_input.operations` the complete business-operation authority.
- Keeps `user_command` policy-only; command-invented operations are discarded.
- Reads inventory from `public.warehouse_items`; no active LARO `orders` or `handling_units` table.
- Adds only missing planning profiles, facility metadata, inventory reservations, Plan storage, and request logs under `laro_ext`.
- Adds an isolated `com.aivle.be.laro` Spring package without changing existing BE files.
- Keeps legacy `/optimize` and `/reoptimize` compatibility.
- Persists idempotent request results, per-run Plan versions, and selected BE inventory reservations.

## v13.25.1 — Native Trace and Canonical Router Hotfix

- Exposed the final logical-operation coverage result through the LangGraph output schema.
- Prevented explicit canonical mixed commands from being rejected by LLM-only `ASK_CLARIFICATION` recommendations.
- Prevented invented A-E/cross-dock choice menus from changing a clear mission request.
- Added exact live-regression tests matching the user-observed OR-Tools trace and natural LLM+cuOpt failures.

## v13.25.0 — Mixed Operation and Live-Source Hardening

- Fixed silent `INBOUND_ITEM` loss in mixed natural-language Agent requests.
- Rewrote the LLM operation-coverage prompt and mixed example.
- Added canonical inbound retrieval and mixed connecting-subgraph evidence.
- Preserved direct inbound/recovery tasks in G2P postprocessing.
- Added exact-once Rule/Agent dynamic-input validation and one repair retry.
- Added final SimulationPlan operation/task/robot/SERVICE coverage validation.
- Made plan persistence conditional on final `plan_validated` status.
- Removed local JSON dependency from `LiveWarehouseRepository`.
- Made live repositories request-scoped and exposed source manifests in trace.
- Added focused mixed-operation and live-source regression tests.

## v13.24.0 — Native Plan API Bridge

- Preserved `POST /optimize` and `POST /reoptimize` exactly for the unmodified Spring BE.
- Added native-plan preflight and compact trace endpoints (removed from the current public API).
- Added shared-stack native fixture bootstrap for PostgreSQL, Redis, and Neo4j.
- Added automated HTTP plan contract verification and example requests.

## v13.23.0 — Unmodified Spring BE Shared-DB Compatibility v2

- Added the additive `laro_contract` schema and Spring-first compatibility graph/runtime adapters.

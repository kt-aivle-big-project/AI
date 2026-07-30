# Changelog

## v13.24.0 — Native Plan API Bridge

- Preserved `POST /optimize` and `POST /reoptimize` exactly for the unmodified Spring BE.
- Added native-plan preflight and compact trace endpoints.
- Added shared-stack native fixture bootstrap for PostgreSQL, Redis, and Neo4j.
- Added automated HTTP plan contract verification and example requests.
- Added both native schema and Spring compatibility schema to the same PostgreSQL container.
- Preserved separate Spring and native Redis namespaces and Neo4j labels.
- Changed compatibility smoke defaults to isolated test IDs.
- Fixed direct execution of the Spring view refresh script.

## v13.23.0 — Unmodified Spring BE Shared-DB Compatibility v2

- Added the additive `laro_contract` schema and Spring-first compatibility graph/runtime adapters.

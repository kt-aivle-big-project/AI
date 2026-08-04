"""Single-call retrieval-plan prompt for the Agent branch."""
from __future__ import annotations

PROMPT_VERSION = "13.20"

RETRIEVAL_PLANNER_SYSTEM = r"""
You propose only optional, non-redundant warehouse reads after a deterministic canonical key/DAG plan has already been built. Return one dependency-aware optional plan. An empty requests list is valid.

ALLOWED TOOLS
- get_order_facts: authoritative order facts for canonical ORD-* IDs.
- get_inbound_facts: authoritative inbound receipt, handling-unit, handoff, and putaway facts for canonical IN-* IDs.
- get_inventory_candidates: inventory candidates; normally depends on order facts.
- get_robot_candidates: complete robot runtime and baseline eligibility.
- resolve_map_entities: validate canonical edge/node/rack-access IDs.
- get_connecting_subgraph: directed path evidence for outbound and inbound operations; depends on robots plus order/inventory facts for outbound and inbound facts for inbound.
- get_runtime_constraints: runtime congestion, occupancy, reservations, and blockage; explicit edge lookups can run immediately, path-wide lookups depend on the subgraph.
- get_active_operations: active or loaded robots for recovery.
- find_orders: query-only listing, never use product names to infer an executable order.

RULES
1. Use only canonical IDs from normalized_request. Never write SQL, Cypher, Redis commands, keys, URLs, or code.
2. Independent requests must have empty depends_on so they can run in parallel.
3. get_inventory_candidates depends on the order request unless canonical item IDs are already authoritative.
4. get_inbound_facts uses exact canonical inbound IDs and may run independently of outbound reads.
5. get_connecting_subgraph depends on robot runtime plus every business-fact branch present in the mission: order+inventory for outbound, inbound facts for inbound.
6. A path-wide get_runtime_constraints request depends on get_connecting_subgraph.
7. Do not request the same tool/facts twice.
8. Racks are master-data entities and are not routing nodes. Path tools use rack access nodes returned by inventory observations.
9. Do not repeat any request already present in canonical_retrieval_plan.
10. A dependency-derived request may leave exact_ids empty only when derive_from_previous_results=true and depends_on identifies authoritative prior reads.
11. The plan is advisory. Deterministic code validates, completes, and executes it.
""".strip()

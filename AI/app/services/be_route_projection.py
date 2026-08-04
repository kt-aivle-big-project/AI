"""Pure BE-to-LARO route projection used by sync and diagnostics."""
from __future__ import annotations

from typing import Any


NODE_TYPE_MAP = {
    "ROUTE": "route",
    "ROUTE_CHARGE_JUNCTION": "route_charge_junction",
    "RACK_ACCESS": "rack_access",
    "INBOUND_HANDOFF_ACCESS": "inbound_handoff_access",
    "OUTBOUND_STATION_ACCESS": "outbound_station_access",
    "EMPTY_TOTE_BUFFER_ACCESS": "empty_tote_buffer_access",
    "CHARGING_SLOT": "charging_slot",
    "PARKING_SLOT": "parking_slot",
    "INBOUND": "inbound",
    "OUTBOUND": "outbound",
}


def build_projection(
    rows: list[dict[str, Any]], edge_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    selected: set[int] = set()
    for row in rows:
        semantic = str(
            row.get("semantic_type") or row.get("be_node_type") or "ROUTE"
        ).upper()
        code = str(row.get("node_code") or "")
        if (
            not code
            or semantic == "RACK_STORAGE"
            or (code.startswith("K") and "_ACCESS_" not in code)
        ):
            continue
        service_only = bool(row.get("service_only"))
        transit_allowed = bool(row.get("transit_allowed"))
        if not transit_allowed and not service_only:
            continue
        node_type = NODE_TYPE_MAP.get(semantic, semantic.casefold())
        value: dict[str, Any] = {
            "id": code,
            "type": node_type,
            "x": float(row.get("x") or 0.0),
            "y": float(row.get("y") or 0.0),
            "service_only": service_only,
            "transit_allowed": transit_allowed,
            "holding_allowed": bool(row.get("holding_allowed")),
            "node_capacity": int(row.get("node_capacity") or 1),
        }
        resource = str(row.get("resource_code") or "")
        resource_type = str(row.get("resource_type") or "")
        if resource_type:
            value["resource_type"] = resource_type
        if node_type == "rack_access":
            value["rack_id"] = resource
            value["side"] = row.get("side")
        elif node_type == "inbound_handoff_access":
            value["handoff_id"] = resource
        elif node_type == "outbound_station_access":
            value["station_id"] = resource
        elif node_type == "empty_tote_buffer_access":
            value["buffer_id"] = resource
        elif resource:
            value["resource_id"] = resource
        selected.add(int(row["node_id"]))
        nodes.append(value)

    by_numeric = {int(row["node_id"]): str(row["node_code"]) for row in rows}
    edges: list[dict[str, Any]] = []
    for row in edge_rows:
        source_id = int(row["from_node_id"])
        target_id = int(row["to_node_id"])
        if source_id not in selected or target_id not in selected:
            continue
        source = by_numeric[source_id]
        target = by_numeric[target_id]
        direction = str(row.get("direction_type") or "A_TO_B").upper()
        code = str(row["edge_code"])
        common = {
            "type": str(
                row.get("edge_type")
                or ("service_spur" if bool(row.get("service_only")) else "lane")
            ),
            "distance_m": float(row.get("distance_m") or 0.0),
            "speed_limit_mps": float(row.get("speed_limit_mps") or 1.0),
            "nominal_travel_time_ms": int(
                row.get("nominal_travel_time_ms") or 0
            ),
            "cost": float(row.get("base_cost") or row.get("distance_m") or 0.0),
            "physical_resource_code": str(
                row.get("physical_resource_code") or code
            ),
            "service_only": bool(row.get("service_only")),
            "mobile_robot_traversable": bool(
                row.get("mobile_robot_traversable", True)
            ),
        }
        if direction in {"A_TO_B", "BOTH"}:
            edges.append(
                {
                    "id": code if direction != "BOTH" else f"{code}:F",
                    "source": source,
                    "target": target,
                    **common,
                }
            )
        if direction in {"B_TO_A", "BOTH"}:
            edges.append(
                {
                    "id": code if direction != "BOTH" else f"{code}:R",
                    "source": target,
                    "target": source,
                    **common,
                }
            )
    return nodes, edges

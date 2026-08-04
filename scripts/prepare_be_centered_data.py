"""Merge the LARO access-node map and planning metadata into existing BE tables.

This is a data migration/bootstrap utility. After it runs, public.warehouse_node,
public.warehouse_edge, public.warehouse_items, robot, and simulation_runs remain
the authority. No orders or handling_units table is created.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.infrastructure.be_centered_postgres import BeCenteredPostgresAdapter
from app.infrastructure.manager import get_infrastructure_manager
from scripts.sync_be_graph_to_neo4j import sync as sync_neo4j


BE_NODE_TYPE = {
    "route": "ROUTE",
    "route_charge_junction": "ROUTE_CHARGE_JUNCTION",
    "rack_access": "RACK_ACCESS",
    "inbound_handoff_access": "INBOUND_HANDOFF_ACCESS",
    "outbound_station_access": "OUTBOUND_STATION_ACCESS",
    "empty_tote_buffer_access": "EMPTY_TOTE_BUFFER_ACCESS",
    "charging_slot": "CHARGING_SLOT",
    "parking_slot": "PARKING_SLOT",
    "inbound": "INBOUND",
    "outbound": "OUTBOUND",
}
def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _facility_rows(document: dict[str, Any]):
    for value in document.get("inbound_handoffs", []):
        yield "INBOUND_HANDOFF", value["handoff_id"], None, value.get("access_node_ids", []), [], value.get("buffer_capacity"), value
    for value in document.get("inbound_ports", []):
        yield "INBOUND_PORT", value["port_id"], None, [], [], None, value
    for value in document.get("outbound_chutes", []):
        yield "OUTBOUND_CHUTE", value["chute_id"], value["chute_id"], [], [], None, value
    for value in document.get("outbound_stations", []):
        yield "OUTBOUND_STATION", value["station_id"], None, value.get("access_node_ids", []), value.get("served_chute_ids", []), value.get("tote_buffer_capacity"), value
    for value in document.get("station_robots", []):
        yield "STATION_ROBOT", value["station_robot_id"], None, [], [], value.get("max_orders_per_wave"), value
    for value in document.get("empty_tote_buffers", []):
        yield "EMPTY_TOTE_BUFFER", value["buffer_id"], None, value.get("access_node_ids", []), [], value.get("capacity"), value


def prepare(
    warehouse_id: int,
    *,
    graph_path: Path,
    inventory_path: Path,
    facility_path: Path,
    scenario_path: Path | None,
    sync_graph: bool,
) -> dict[str, Any]:
    manager = get_infrastructure_manager()
    manager.start()
    adapter = BeCenteredPostgresAdapter(manager=manager)
    adapter.require_views()
    graph = _read(graph_path)
    inventory = _read(inventory_path)
    facilities = _read(facility_path)
    scenario = _read(scenario_path) if scenario_path and scenario_path.exists() else {}

    with manager.postgres._connection() as conn:
        warehouse = conn.execute(
            "SELECT id FROM public.warehouse_layout WHERE id=%s", (warehouse_id,)
        ).fetchone()
        if warehouse is None:
            raise ValueError(f"warehouse_id={warehouse_id} does not exist")

        existing_nodes = {
            str(row["node_code"]): int(row["node_id"])
            for row in conn.execute(
                "SELECT node_id,node_code FROM public.warehouse_node WHERE warehouse_id=%s AND node_code IS NOT NULL",
                (warehouse_id,),
            ).fetchall()
        }
        node_ids: dict[str, int] = dict(existing_nodes)
        for node in graph.get("nodes", []):
            code = str(node["id"])
            node_type = str(node.get("type") or "route")
            be_type = BE_NODE_TYPE.get(node_type, "ROUTE")
            service_only = bool(
                node.get(
                    "service_only",
                    node_type
                    in {
                        "rack_access",
                        "inbound_handoff_access",
                        "outbound_station_access",
                        "empty_tote_buffer_access",
                    },
                )
            )
            transit_allowed = bool(
                node.get("transit_allowed", not service_only)
            )
            holding_allowed = bool(node.get("holding_allowed", True))
            node_capacity = int(node.get("node_capacity", 1))
            resource_type = None
            resource_code = None
            if node_type == "rack_access":
                resource_type, resource_code = "RACK", node.get("rack_id")
            elif node_type == "inbound_handoff_access":
                resource_type, resource_code = "INBOUND_HANDOFF", node.get("handoff_id")
            elif node_type == "outbound_station_access":
                resource_type, resource_code = "OUTBOUND_STATION", node.get("station_id")
            elif node_type == "empty_tote_buffer_access":
                resource_type, resource_code = "EMPTY_TOTE_BUFFER", node.get("buffer_id")
            elif node.get("resource_id") is not None:
                resource_type, resource_code = "RESOURCE", node.get("resource_id")
            route_attributes = {
                key: value
                for key, value in node.items()
                if key
                not in {
                    "id",
                    "type",
                    "x",
                    "y",
                    "service_only",
                    "transit_allowed",
                    "holding_allowed",
                    "node_capacity",
                    "resource_type",
                    "resource_code",
                    "resource_id",
                    "rack_id",
                    "handoff_id",
                    "station_id",
                    "buffer_id",
                    "side",
                }
            }
            if code in node_ids:
                conn.execute(
                    """
                    UPDATE public.warehouse_node
                    SET x=%s,y=%s,node_type=%s,
                        service_only=%s,transit_allowed=%s,holding_allowed=%s,
                        node_capacity=%s,resource_type=%s,resource_code=%s,side=%s,
                        route_attributes=%s::jsonb
                    WHERE node_id=%s
                    """,
                    (
                        float(node.get("x") or 0.0),
                        float(node.get("y") or 0.0),
                        be_type,
                        service_only,
                        transit_allowed,
                        holding_allowed,
                        node_capacity,
                        resource_type,
                        resource_code,
                        node.get("side"),
                        json.dumps(route_attributes, ensure_ascii=False),
                        node_ids[code],
                    ),
                )
            else:
                row = conn.execute(
                    """
                    INSERT INTO public.warehouse_node(
                        warehouse_id,zone_id,node_code,node_type,x,y,
                        service_only,transit_allowed,holding_allowed,node_capacity,
                        resource_type,resource_code,side,route_attributes
                    )
                    VALUES (
                        %s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    ) RETURNING node_id
                    """,
                    (
                        warehouse_id,
                        code,
                        be_type,
                        float(node.get("x") or 0.0),
                        float(node.get("y") or 0.0),
                        service_only,
                        transit_allowed,
                        holding_allowed,
                        node_capacity,
                        resource_type,
                        resource_code,
                        node.get("side"),
                        json.dumps(route_attributes, ensure_ascii=False),
                    ),
                ).fetchone()
                node_ids[code] = int(row["node_id"])

        # Explicitly keep old rack master nodes out of the route projection.
        conn.execute(
            """
            UPDATE public.warehouse_node
            SET service_only=false,
                transit_allowed=false,
                holding_allowed=false,
                node_capacity=COALESCE(node_capacity, 1),
                resource_type='RACK',
                resource_code=node_code
            WHERE warehouse_id=%s AND node_type='RACK_STORAGE'
            """,
            (warehouse_id,),
        )

        existing_edges = {
            str(row["edge_code"]): int(row["edge_id"])
            for row in conn.execute(
                """
                SELECT e.edge_id,e.edge_code FROM public.warehouse_edge e
                JOIN public.warehouse_node n ON n.node_id=e.from_node_id
                WHERE n.warehouse_id=%s AND e.edge_code IS NOT NULL
                """,
                (warehouse_id,),
            ).fetchall()
        }
        for edge in graph.get("edges", []):
            code = str(edge["id"])
            source = node_ids[str(edge["source"])]
            target = node_ids[str(edge["target"])]
            distance = float(edge.get("distance_m") or edge.get("cost") or 0.0)
            edge_type = str(
                edge.get("type")
                or ("service_spur" if edge.get("service_only") else "lane")
            )
            raw_direction = str(edge.get("direction") or "").strip().upper()
            direction_type = {
                "BOTH": "BOTH",
                "BIDIRECTIONAL": "BOTH",
                "A_TO_B": "A_TO_B",
                "B_TO_A": "B_TO_A",
            }.get(
                raw_direction,
                "BOTH" if edge_type == "charging_connector" else "A_TO_B",
            )
            service_only = bool(
                edge.get("service_only", edge_type == "service_spur")
            )
            speed_limit_mps = float(edge.get("speed_limit_mps") or 1.0)
            nominal_travel_time_ms = int(
                edge.get("nominal_travel_time_ms")
                or round(distance / speed_limit_mps * 1000)
            )
            cost = float(edge.get("cost") or distance)
            physical_resource_code = str(
                edge.get("physical_resource_code")
                or edge.get("resource_id")
                or "PHY::" + "::".join(
                    sorted((str(edge["source"]), str(edge["target"])))
                )
            )
            mobile_robot_traversable = bool(
                edge.get("mobile_robot_traversable", True)
            )
            route_attributes = {
                key: value
                for key, value in edge.items()
                if key
                not in {
                    "id",
                    "source",
                    "target",
                    "distance_m",
                    "type",
                    "direction",
                    "speed_limit_mps",
                    "nominal_travel_time_ms",
                    "cost",
                    "base_cost",
                    "resource_id",
                    "physical_resource_code",
                    "service_only",
                    "mobile_robot_traversable",
                }
            }
            if code in existing_edges:
                edge_id = existing_edges[code]
                conn.execute(
                    """
                    UPDATE public.warehouse_edge
                    SET from_node_id=%s,to_node_id=%s,distance=%s,
                        direction_type=%s,edge_type=%s,speed_limit_mps=%s,
                        nominal_travel_time_ms=%s,cost=%s,physical_resource_code=%s,
                        service_only=%s,mobile_robot_traversable=%s,
                        route_attributes=%s::jsonb
                    WHERE edge_id=%s
                    """,
                    (
                        source,
                        target,
                        distance,
                        direction_type,
                        edge_type,
                        speed_limit_mps,
                        nominal_travel_time_ms,
                        cost,
                        physical_resource_code,
                        service_only,
                        mobile_robot_traversable,
                        json.dumps(route_attributes, ensure_ascii=False),
                        edge_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO public.warehouse_edge(
                        edge_code,distance,from_node_id,to_node_id,
                        direction_type,edge_type,speed_limit_mps,
                        nominal_travel_time_ms,cost,physical_resource_code,
                        service_only,mobile_robot_traversable,route_attributes
                    )
                    VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    )
                    """,
                    (
                        code,
                        distance,
                        source,
                        target,
                        direction_type,
                        edge_type,
                        speed_limit_mps,
                        nominal_travel_time_ms,
                        cost,
                        physical_resource_code,
                        service_only,
                        mobile_robot_traversable,
                        json.dumps(route_attributes, ensure_ascii=False),
                    ),
                )

        # Old edges to rack master nodes are retained for BE compatibility but
        # excluded from the mobile-robot projection.
        conn.execute(
            """
            UPDATE public.warehouse_edge e
            SET mobile_robot_traversable=false
            FROM public.warehouse_node fn, public.warehouse_node tn
            WHERE fn.warehouse_id=%s
              AND fn.node_id=e.from_node_id
              AND tn.node_id=e.to_node_id
              AND (fn.node_type='RACK_STORAGE' OR tn.node_type='RACK_STORAGE')
            """,
            (warehouse_id,),
        )

        # Spring warehouse_items.rack_level is authoritative. The JSON document
        # may still supply rack identities for compatibility, but never assigns
        # inventory or occupancy to a level.
        for rack in inventory.get("racks", []):
            rack_code = str(rack["rack_id"])
            rack_node_id = node_ids.get(rack_code)
            if rack_node_id is None:
                continue
            storage = conn.execute(
                "SELECT storage_location_id FROM public.storage_location WHERE warehouse_id=%s AND node_id=%s",
                (warehouse_id, rack_node_id),
            ).fetchone()
            if storage is None:
                conn.execute(
                    """
                    INSERT INTO public.storage_location(
                        warehouse_id,node_id,max_quantity,max_weight,max_volume,created_at,updated_at,status
                    ) VALUES (%s,%s,100,NULL,NULL,now(),now(),'AVAILABLE')
                    """,
                    (warehouse_id, rack_node_id),
                )

        # rack_slot is keyed by a physical rack master. Earlier bootstrap
        # versions accidentally materialized three levels for RACK_ACCESS
        # service nodes as well. They are route endpoints, not shelves, so
        # remove only those stale planning rows before rebuilding the 48 x 3
        # physical rack slots.
        conn.execute(
            """
            DELETE FROM laro_ext.rack_slot rs
            USING public.warehouse_node n
            WHERE rs.warehouse_id=%s
              AND n.node_id=rs.rack_node_id
              AND n.node_type::text <> 'RACK_STORAGE'
            """,
            (warehouse_id,),
        )
        conn.execute(
            """
            INSERT INTO laro_ext.rack_slot(
                warehouse_id,rack_node_id,rack_level,storage_location_id,capacity,status,version
            )
            SELECT sl.warehouse_id,sl.node_id,levels.rack_level,
                   sl.storage_location_id,1,
                   CASE WHEN wi.warehouse_item_id IS NULL THEN 'EMPTY' ELSE 'OCCUPIED' END,
                   1
            FROM public.storage_location sl
            CROSS JOIN generate_series(1,3) AS levels(rack_level)
            LEFT JOIN public.warehouse_items wi
              ON wi.storage_location_id=sl.storage_location_id
             AND wi.rack_level=levels.rack_level
            WHERE sl.warehouse_id=%s
            ON CONFLICT (warehouse_id,rack_node_id,rack_level) DO UPDATE SET
                storage_location_id=EXCLUDED.storage_location_id,
                capacity=EXCLUDED.capacity,status=EXCLUDED.status,
                version=laro_ext.rack_slot.version+1,updated_at=now()
            """,
            (warehouse_id,),
        )
        rack_slot_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM laro_ext.rack_slot WHERE warehouse_id=%s",
                (warehouse_id,),
            ).fetchone()["count"]
        )
        conn.execute(
            """
            INSERT INTO laro_ext.warehouse_item_profile(
                warehouse_item_id,rack_level,capacity,planning_status,version
            )
            SELECT wi.warehouse_item_id,wi.rack_level,p.units_per_box,'STORED',1
            FROM public.warehouse_items wi
            JOIN public.product p ON p.product_id=wi.product_id
            WHERE wi.warehouse_id=%s
            ON CONFLICT (warehouse_item_id) DO UPDATE SET
                rack_level=EXCLUDED.rack_level,
                capacity=EXCLUDED.capacity,
                version=laro_ext.warehouse_item_profile.version+1,
                updated_at=now()
            """,
            (warehouse_id,),
        )
        unmatched_inventory_profiles: list[str] = []

        # Treat the facility document as the complete active catalog for this
        # warehouse. Without this step, facilities removed by a map revision
        # would remain active beside the new handoffs/stations.
        facility_rows = list(_facility_rows(facilities))
        conn.execute(
            """
            UPDATE laro_ext.facility
            SET active=false,updated_at=now()
            WHERE warehouse_id=%s
            """,
            (warehouse_id,),
        )
        facility_count = 0
        for ftype, code, node_code, access_codes, destinations, capacity, metadata in facility_rows:
            node_id = node_ids.get(str(node_code)) if node_code else None
            conn.execute(
                """
                INSERT INTO laro_ext.facility(
                    warehouse_id,facility_code,facility_type,node_id,
                    access_node_codes,served_destination_codes,capacity,status,metadata,active
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,true)
                ON CONFLICT (warehouse_id,facility_code) DO UPDATE SET
                    facility_type=EXCLUDED.facility_type,node_id=EXCLUDED.node_id,
                    access_node_codes=EXCLUDED.access_node_codes,
                    served_destination_codes=EXCLUDED.served_destination_codes,
                    capacity=EXCLUDED.capacity,status=EXCLUDED.status,
                    metadata=EXCLUDED.metadata,active=true,updated_at=now()
                """,
                (
                    warehouse_id, code, ftype, node_id,
                    json.dumps(access_codes), json.dumps(destinations), capacity,
                    str(metadata.get("status") or "AVAILABLE").upper(),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            facility_count += 1

        robot_defaults = {
            str(value.get("robot_id")): value for value in scenario.get("robots", [])
        }
        specs = conn.execute(
            "SELECT id,robot_code FROM public.robot_specs ORDER BY id"
        ).fetchall()
        for spec in specs:
            default = robot_defaults.get(str(spec["robot_code"]), {})
            conn.execute(
                """
                INSERT INTO laro_ext.robot_profile(
                    robot_spec_id,capacity_units,nominal_speed_mps,
                    minimum_operating_battery_pct,active
                ) VALUES (%s,%s,%s,%s,true)
                ON CONFLICT (robot_spec_id) DO UPDATE SET
                    capacity_units=EXCLUDED.capacity_units,
                    nominal_speed_mps=EXCLUDED.nominal_speed_mps,
                    minimum_operating_battery_pct=EXCLUDED.minimum_operating_battery_pct,
                    active=true,updated_at=now()
                """,
                (
                    int(spec["id"]), 1,
                    float(default.get("nominal_speed_mps") or 1.0), 30.0,
                ),
            )

        conn.execute(
            "UPDATE laro_ext.warehouse_profile SET map_version=map_version+1,facility_version=facility_version+1,updated_at=now() WHERE warehouse_id=%s",
            (warehouse_id,),
        )
        conn.commit()

    adapter.refresh_views(force=True)
    neo4j_result = sync_neo4j(warehouse_id) if sync_graph else None
    return {
        "status": "PASS",
        "warehouse_id": warehouse_id,
        "imported_route_nodes": len(graph.get("nodes", [])),
        "imported_route_edges": len(graph.get("edges", [])),
        "rack_slot_count": rack_slot_count,
        "facility_count": facility_count,
        "unmatched_inventory_profiles": unmatched_inventory_profiles,
        "neo4j": neo4j_result,
        "orders_table_used": False,
        "handling_units_table_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse-id", type=int, required=True)
    parser.add_argument("--graph", type=Path, default=Path("data/warehouse_graph.json"))
    parser.add_argument("--inventory", type=Path, default=Path("data/rack_inventory.json"))
    parser.add_argument("--facilities", type=Path, default=Path("data/facility_resources.json"))
    parser.add_argument("--scenario", type=Path, default=Path("data/scenario_state.json"))
    parser.add_argument("--skip-neo4j", action="store_true")
    args = parser.parse_args()
    result = prepare(
        args.warehouse_id,
        graph_path=args.graph,
        inventory_path=args.inventory,
        facility_path=args.facilities,
        scenario_path=args.scenario,
        sync_graph=not args.skip_neo4j,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

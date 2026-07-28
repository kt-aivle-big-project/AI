// P16.5.7 dedicated idle bays for warehouse 2.
// Run in Neo4j Aura Query if you prefer not to use the Python seed script.
MATCH (w:Warehouse {warehouse_id: 2})
MERGE (z:Zone {warehouse_id: 2, zone_id: 'PARKING'})
MERGE (w)-[:HAS_ZONE]->(z)
WITH z
UNWIND [
  {node_id: 2160, x: 8.2,  y: 10.68, priority: 1},
  {node_id: 2161, x: 10.0, y: 10.68, priority: 2},
  {node_id: 2162, x: 11.24,y: 10.68, priority: 3}
] AS row
MERGE (n:MapNode {warehouse_id: 2, node_id: row.node_id})
SET n.node_type = 'PARKING',
    n.x = row.x,
    n.y = row.y,
    n.active = true,
    n.idle_allowed = true,
    n.idle_capacity = 1,
    n.parking_priority = row.priority
MERGE (z)-[:HAS_NODE]->(n);

UNWIND [
  {edge_id: 'PK1', from_node: 2078, to_node: 2160},
  {edge_id: 'PK2', from_node: 2079, to_node: 2161},
  {edge_id: 'PK3', from_node: 2080, to_node: 2162}
] AS row
MATCH (a:MapNode {warehouse_id: 2, node_id: row.from_node})
MATCH (b:MapNode {warehouse_id: 2, node_id: row.to_node})
MERGE (a)-[r:CONNECTED_TO]->(b)
SET r.edge_id = row.edge_id,
    r.distance = 0.6,
    r.travel_seconds = 1,
    r.direction = 'BOTH',
    r.active = true;

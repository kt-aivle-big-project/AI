// P16.5.8: convert dedicated idle bays into charger-linked waiting areas.
MATCH (n:MapNode {warehouse_id: 2})
WHERE n.node_id IN [2160, 2161, 2162]
SET n.node_type = 'CHARGER_WAITING_AREA',
    n.idle_allowed = true,
    n.idle_capacity = 1,
    n.linked_charger_node_id = CASE n.node_id
        WHEN 2160 THEN 2150
        WHEN 2161 THEN 2151
        WHEN 2162 THEN 2152
    END,
    n.parking_priority = CASE n.node_id
        WHEN 2160 THEN 1
        WHEN 2161 THEN 2
        WHEN 2162 THEN 3
    END;

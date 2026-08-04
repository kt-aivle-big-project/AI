CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id text PRIMARY KEY,
    label text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS warehouse_meta (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, key)
);

CREATE TABLE IF NOT EXISTS racks (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    rack_id text NOT NULL,
    access_node_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (warehouse_id, rack_id)
);

CREATE TABLE IF NOT EXISTS rack_slots (
    warehouse_id text NOT NULL,
    rack_id text NOT NULL,
    level integer NOT NULL CHECK (level BETWEEN 1 AND 3),
    status text NOT NULL CHECK (status IN ('EMPTY','PARTIAL','FULL')),
    capacity integer NOT NULL DEFAULT 0 CHECK (capacity >= 0),
    PRIMARY KEY (warehouse_id, rack_id, level),
    FOREIGN KEY (warehouse_id, rack_id)
      REFERENCES racks(warehouse_id, rack_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS handling_units (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    handling_unit_id text NOT NULL,
    stock_id text NOT NULL,
    item_id text NOT NULL,
    item_name text,
    category text,
    quantity integer NOT NULL CHECK (quantity >= 0),
    capacity integer NOT NULL CHECK (capacity >= 0),
    unit text NOT NULL DEFAULT 'EA',
    home_rack_id text NOT NULL,
    home_rack_level integer NOT NULL CHECK (home_rack_level BETWEEN 1 AND 3),
    status text NOT NULL CHECK (
      status IN ('stored','reserved','in_transit','at_station','returning','empty_in_transit','empty_buffered')
    ),
    version integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, handling_unit_id),
    UNIQUE (warehouse_id, stock_id),
    FOREIGN KEY (warehouse_id, home_rack_id)
      REFERENCES racks(warehouse_id, rack_id)
);
CREATE INDEX IF NOT EXISTS idx_handling_units_warehouse_item_status
    ON handling_units(warehouse_id, item_id, status, quantity);

-- Facility masters are created before orders/receipts that reference them.
CREATE TABLE IF NOT EXISTS inbound_handoffs (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    handoff_id text NOT NULL,
    access_node_ids jsonb NOT NULL,
    buffer_capacity integer NOT NULL CHECK (buffer_capacity > 0),
    PRIMARY KEY (warehouse_id, handoff_id)
);

CREATE TABLE IF NOT EXISTS inbound_ports (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    port_id text NOT NULL,
    label text NOT NULL,
    handoff_id text NOT NULL,
    PRIMARY KEY (warehouse_id, port_id),
    FOREIGN KEY (warehouse_id, handoff_id)
      REFERENCES inbound_handoffs(warehouse_id, handoff_id)
);

CREATE TABLE IF NOT EXISTS outbound_chutes (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    chute_id text NOT NULL,
    label text NOT NULL,
    PRIMARY KEY (warehouse_id, chute_id)
);

CREATE TABLE IF NOT EXISTS outbound_stations (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    station_id text NOT NULL,
    station_robot_id text NOT NULL,
    access_node_ids jsonb NOT NULL,
    served_chute_ids jsonb NOT NULL,
    tote_buffer_capacity integer NOT NULL CHECK (tote_buffer_capacity > 0),
    status text NOT NULL,
    PRIMARY KEY (warehouse_id, station_id),
    UNIQUE (warehouse_id, station_robot_id)
);

CREATE TABLE IF NOT EXISTS station_robots (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    station_robot_id text NOT NULL,
    station_id text NOT NULL,
    status text NOT NULL,
    max_orders_per_wave integer NOT NULL CHECK (max_orders_per_wave > 0),
    items_per_tick integer NOT NULL DEFAULT 1 CHECK (items_per_tick > 0),
    PRIMARY KEY (warehouse_id, station_robot_id),
    UNIQUE (warehouse_id, station_id),
    FOREIGN KEY (warehouse_id, station_id)
      REFERENCES outbound_stations(warehouse_id, station_id)
      DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS empty_tote_buffers (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    buffer_id text NOT NULL,
    access_node_ids jsonb NOT NULL,
    capacity integer NOT NULL CHECK (capacity > 0),
    status text NOT NULL,
    PRIMARY KEY (warehouse_id, buffer_id)
);

CREATE TABLE IF NOT EXISTS orders (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    order_id text NOT NULL,
    status text NOT NULL,
    priority text NOT NULL CHECK (priority IN ('low','medium','high')),
    outbound_chute_id text NOT NULL,
    preferred_station_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, order_id),
    FOREIGN KEY (warehouse_id, outbound_chute_id)
      REFERENCES outbound_chutes(warehouse_id, chute_id)
);

CREATE TABLE IF NOT EXISTS order_lines (
    warehouse_id text NOT NULL,
    order_id text NOT NULL,
    line_no integer NOT NULL,
    item_id text NOT NULL,
    required_qty integer NOT NULL CHECK (required_qty > 0),
    PRIMARY KEY (warehouse_id, order_id, line_no),
    FOREIGN KEY (warehouse_id, order_id)
      REFERENCES orders(warehouse_id, order_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_order_lines_warehouse_item
    ON order_lines(warehouse_id, item_id);

CREATE TABLE IF NOT EXISTS inbound_receipts (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    inbound_id text NOT NULL,
    handling_unit_id text NOT NULL,
    item_id text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    source_port_id text NOT NULL,
    target_rack_id text,
    target_rack_level integer CHECK (target_rack_level BETWEEN 1 AND 3),
    status text NOT NULL CHECK (status IN ('pending','planned','in_transit','stored','held','cancelled')),
    priority text NOT NULL CHECK (priority IN ('low','medium','high')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, inbound_id),
    UNIQUE (warehouse_id, handling_unit_id),
    FOREIGN KEY (warehouse_id, source_port_id)
      REFERENCES inbound_ports(warehouse_id, port_id),
    FOREIGN KEY (warehouse_id, target_rack_id)
      REFERENCES racks(warehouse_id, rack_id)
);
CREATE INDEX IF NOT EXISTS idx_inbound_receipts_warehouse_status
    ON inbound_receipts(warehouse_id, status, priority);

-- Existing installations originally required BE-selected putaway targets.
-- LARO may select the destination, so keep this migration idempotent when the
-- schema is reapplied to an existing database.
ALTER TABLE inbound_receipts ALTER COLUMN target_rack_id DROP NOT NULL;
ALTER TABLE inbound_receipts ALTER COLUMN target_rack_level DROP NOT NULL;

CREATE TABLE IF NOT EXISTS outbound_batches (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    batch_id text NOT NULL,
    simulation_id text NOT NULL,
    item_id text NOT NULL,
    handling_unit_id text NOT NULL,
    station_id text NOT NULL,
    mobile_robot_id text,
    post_station_node text NOT NULL,
    post_station_action text NOT NULL CHECK (
      post_station_action IN ('RETURN_TO_SOURCE','MOVE_TO_EMPTY_TOTE_BUFFER')
    ),
    requested_quantity integer NOT NULL CHECK (requested_quantity > 0),
    quantity_before integer NOT NULL CHECK (quantity_before >= 0),
    quantity_after integer NOT NULL CHECK (quantity_after >= 0),
    return_required boolean NOT NULL,
    status text NOT NULL CHECK (
      status IN ('planned','reserved','at_station','returning','empty_repositioning','completed','held','cancelled')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, batch_id),
    FOREIGN KEY (warehouse_id, handling_unit_id)
      REFERENCES handling_units(warehouse_id, handling_unit_id),
    FOREIGN KEY (warehouse_id, station_id)
      REFERENCES outbound_stations(warehouse_id, station_id)
);

CREATE TABLE IF NOT EXISTS outbound_batch_orders (
    warehouse_id text NOT NULL,
    batch_id text NOT NULL,
    order_id text NOT NULL,
    chute_id text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (warehouse_id, batch_id, order_id, chute_id),
    FOREIGN KEY (warehouse_id, batch_id)
      REFERENCES outbound_batches(warehouse_id, batch_id) ON DELETE CASCADE,
    FOREIGN KEY (warehouse_id, order_id)
      REFERENCES orders(warehouse_id, order_id),
    FOREIGN KEY (warehouse_id, chute_id)
      REFERENCES outbound_chutes(warehouse_id, chute_id)
);

CREATE TABLE IF NOT EXISTS inventory_reservations (
    warehouse_id text NOT NULL,
    reservation_id text NOT NULL,
    batch_id text NOT NULL,
    handling_unit_id text NOT NULL,
    reserved_quantity integer NOT NULL CHECK (reserved_quantity > 0),
    expected_handling_unit_version integer NOT NULL,
    status text NOT NULL CHECK (status IN ('active','committed','released','cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, reservation_id),
    FOREIGN KEY (warehouse_id, batch_id)
      REFERENCES outbound_batches(warehouse_id, batch_id) ON DELETE CASCADE,
    FOREIGN KEY (warehouse_id, handling_unit_id)
      REFERENCES handling_units(warehouse_id, handling_unit_id)
);

CREATE TABLE IF NOT EXISTS infrastructure_roundtrip (
    warehouse_id text NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
    probe_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, probe_id)
);

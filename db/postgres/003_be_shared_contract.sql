-- Additive Spring BE -> LARO compatibility schema.
--
-- This file deliberately does not ALTER or DROP the unmodified Spring tables.
-- It can be applied before Spring starts because every contract table stores
-- Spring numeric identifiers without foreign keys to public.*.  When the
-- Spring tables exist, LARO prefers them directly and uses these tables only as
-- a normalized fallback for the graph received by POST /optimize.

CREATE SCHEMA IF NOT EXISTS laro_contract;

CREATE TABLE IF NOT EXISTS laro_contract.contract_meta (
    contract_name text PRIMARY KEY,
    contract_version integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO laro_contract.contract_meta(contract_name, contract_version)
VALUES ('spring_be_unmodified', 2)
ON CONFLICT (contract_name) DO UPDATE SET
    contract_version = EXCLUDED.contract_version,
    updated_at = now();

CREATE TABLE IF NOT EXISTS laro_contract.warehouse_binding (
    warehouse_id bigint PRIMARY KEY,
    warehouse_code text NOT NULL UNIQUE,
    graph_version text,
    map_version bigint NOT NULL DEFAULT 1,
    inventory_version bigint NOT NULL DEFAULT 1,
    facility_version bigint NOT NULL DEFAULT 1,
    graph_source text NOT NULL DEFAULT 'request_snapshot'
        CHECK (graph_source IN ('spring_db', 'contract', 'request_snapshot')),
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS laro_contract.route_node (
    warehouse_id bigint NOT NULL,
    node_id bigint NOT NULL,
    node_code text NOT NULL,
    x double precision,
    y double precision,
    semantic_type text NOT NULL DEFAULT 'ROUTE',
    service_only boolean NOT NULL DEFAULT false,
    transit_allowed boolean NOT NULL DEFAULT true,
    resource_type text,
    resource_code text,
    adjacent_route_node_id bigint,
    side text,
    active boolean NOT NULL DEFAULT true,
    graph_version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, node_id),
    UNIQUE (warehouse_id, node_code)
);

CREATE TABLE IF NOT EXISTS laro_contract.route_edge (
    warehouse_id bigint NOT NULL,
    edge_id bigint NOT NULL,
    from_node_id bigint NOT NULL,
    to_node_id bigint NOT NULL,
    direction_type text NOT NULL
        CHECK (direction_type IN ('BOTH', 'A_TO_B', 'B_TO_A')),
    edge_code text NOT NULL,
    edge_type text NOT NULL DEFAULT 'ROUTE',
    distance_m double precision NOT NULL CHECK (distance_m >= 0),
    speed_limit_mps double precision NOT NULL DEFAULT 1.0
        CHECK (speed_limit_mps > 0),
    nominal_travel_time_ms bigint NOT NULL CHECK (nominal_travel_time_ms >= 0),
    base_cost double precision NOT NULL CHECK (base_cost >= 0),
    physical_resource_code text,
    service_only boolean NOT NULL DEFAULT false,
    mobile_robot_traversable boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1,
    graph_version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, edge_id)
);

CREATE INDEX IF NOT EXISTS idx_laro_contract_route_edge_from
    ON laro_contract.route_edge(warehouse_id, from_node_id);
CREATE INDEX IF NOT EXISTS idx_laro_contract_route_edge_to
    ON laro_contract.route_edge(warehouse_id, to_node_id);

-- Rack / inventory extension.  These tables are intentionally independent of
-- the Spring JPA entities so BE-main can remain byte-for-byte unchanged.
CREATE TABLE IF NOT EXISTS laro_contract.rack (
    rack_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    rack_code text NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    active boolean NOT NULL DEFAULT true,
    UNIQUE (warehouse_id, rack_code)
);

CREATE TABLE IF NOT EXISTS laro_contract.rack_access (
    rack_id bigint NOT NULL REFERENCES laro_contract.rack(rack_id) ON DELETE CASCADE,
    warehouse_id bigint NOT NULL,
    node_id bigint NOT NULL,
    side text,
    priority integer NOT NULL DEFAULT 0,
    PRIMARY KEY (rack_id, node_id)
);

CREATE TABLE IF NOT EXISTS laro_contract.rack_slot (
    rack_slot_id bigserial PRIMARY KEY,
    rack_id bigint NOT NULL REFERENCES laro_contract.rack(rack_id) ON DELETE CASCADE,
    spring_storage_location_id bigint,
    rack_level integer NOT NULL CHECK (rack_level > 0),
    status text NOT NULL DEFAULT 'EMPTY',
    capacity integer NOT NULL DEFAULT 0 CHECK (capacity >= 0),
    version bigint NOT NULL DEFAULT 1,
    UNIQUE (rack_id, rack_level)
);

CREATE TABLE IF NOT EXISTS laro_contract.handling_unit (
    handling_unit_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    handling_unit_code text NOT NULL,
    stock_code text NOT NULL,
    spring_warehouse_item_id bigint,
    spring_item_id bigint,
    item_code text,
    rack_slot_id bigint REFERENCES laro_contract.rack_slot(rack_slot_id),
    home_rack_slot_id bigint REFERENCES laro_contract.rack_slot(rack_slot_id),
    quantity integer NOT NULL CHECK (quantity >= 0),
    capacity integer NOT NULL CHECK (capacity >= 0),
    unit text NOT NULL DEFAULT 'EA',
    status text NOT NULL DEFAULT 'STORED',
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warehouse_id, handling_unit_code),
    UNIQUE (warehouse_id, stock_code)
);

-- Business request layer used when the project later enables native G2P.
CREATE TABLE IF NOT EXISTS laro_contract.outbound_order (
    order_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    order_code text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING',
    priority text NOT NULL DEFAULT 'MEDIUM',
    outbound_chute_code text,
    version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warehouse_id, order_code)
);

CREATE TABLE IF NOT EXISTS laro_contract.outbound_order_line (
    order_line_id bigserial PRIMARY KEY,
    order_id bigint NOT NULL
        REFERENCES laro_contract.outbound_order(order_id) ON DELETE CASCADE,
    line_no integer NOT NULL DEFAULT 1,
    spring_item_id bigint,
    item_code text,
    required_quantity integer NOT NULL CHECK (required_quantity > 0),
    fulfilled_quantity integer NOT NULL DEFAULT 0 CHECK (fulfilled_quantity >= 0),
    status text NOT NULL DEFAULT 'PENDING',
    UNIQUE (order_id, line_no)
);

CREATE TABLE IF NOT EXISTS laro_contract.inbound_receipt (
    inbound_receipt_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    inbound_code text NOT NULL,
    handling_unit_code text,
    spring_item_id bigint,
    item_code text,
    quantity integer NOT NULL CHECK (quantity > 0),
    source_port_code text,
    target_rack_code text,
    target_rack_level integer,
    status text NOT NULL DEFAULT 'PENDING',
    priority text NOT NULL DEFAULT 'MEDIUM',
    version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warehouse_id, inbound_code)
);

-- Facility master records.  Runtime occupancy belongs in Redis, not here.
CREATE TABLE IF NOT EXISTS laro_contract.facility (
    facility_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    facility_code text NOT NULL,
    facility_type text NOT NULL CHECK (
        facility_type IN (
            'INBOUND_HANDOFF', 'INBOUND_PORT', 'OUTBOUND_CHUTE',
            'OUTBOUND_STATION', 'STATION_ROBOT', 'EMPTY_TOTE_BUFFER',
            'CHARGING_STATION', 'PARKING_SLOT'
        )
    ),
    access_node_id bigint,
    capacity integer,
    status text NOT NULL DEFAULT 'AVAILABLE',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (warehouse_id, facility_code)
);

CREATE TABLE IF NOT EXISTS laro_contract.inventory_reservation (
    reservation_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    handling_unit_code text NOT NULL,
    reserved_quantity integer NOT NULL CHECK (reserved_quantity > 0),
    expected_version bigint NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS laro_contract.request_log (
    request_id text PRIMARY KEY,
    request_type text NOT NULL CHECK (request_type IN ('optimize', 'reoptimize')),
    warehouse_id bigint NOT NULL,
    simulation_run_id bigint,
    graph_source text,
    runtime_source text,
    status text NOT NULL,
    request_json jsonb NOT NULL,
    response_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_laro_contract_request_log_warehouse_created
    ON laro_contract.request_log(warehouse_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_laro_contract_request_log_simulation_created
    ON laro_contract.request_log(simulation_run_id, created_at DESC)
    WHERE simulation_run_id IS NOT NULL;

-- Stable normalized views consumed by the compatibility repository.
CREATE OR REPLACE VIEW laro_contract.laro_route_nodes_v AS
SELECT
    warehouse_id,
    node_id,
    node_code,
    x,
    y,
    semantic_type,
    service_only,
    transit_allowed,
    resource_type,
    resource_code,
    adjacent_route_node_id,
    side,
    active,
    graph_version
FROM laro_contract.route_node;

CREATE OR REPLACE VIEW laro_contract.laro_route_edges_v AS
SELECT
    warehouse_id,
    edge_id,
    from_node_id,
    to_node_id,
    direction_type,
    edge_code,
    edge_type,
    distance_m,
    speed_limit_mps,
    nominal_travel_time_ms,
    base_cost,
    physical_resource_code,
    service_only,
    mobile_robot_traversable,
    active,
    version,
    graph_version
FROM laro_contract.route_edge;

-- This function creates read-only views over the unmodified Spring tables only
-- after Hibernate has created them.  Calling it before Spring startup is safe:
-- it returns false instead of failing the PostgreSQL container initialization.
CREATE OR REPLACE FUNCTION laro_contract.refresh_spring_views()
RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('public.warehouse_node') IS NULL
       OR to_regclass('public.warehouse_edge') IS NULL
       OR to_regclass('public.warehouse_layout') IS NULL THEN
        RETURN false;
    END IF;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_contract.spring_warehouses_v AS
        SELECT id AS warehouse_id,
               ('WH-' || lpad(id::text, 3, '0')) AS warehouse_code,
               name, width, height
        FROM public.warehouse_layout
    $view$;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_contract.spring_route_nodes_v AS
        SELECT warehouse_id,
               node_id,
               COALESCE(node_code, 'N' || node_id::text) AS node_code,
               x, y,
               COALESCE(node_type::text, 'ROUTE') AS spring_node_type
        FROM public.warehouse_node
    $view$;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_contract.spring_route_edges_v AS
        SELECT fn.warehouse_id,
               e.edge_id,
               e.from_node_id,
               e.to_node_id,
               e.direction_type::text AS direction_type,
               COALESCE(e.distance, 0)::double precision AS distance_m
        FROM public.warehouse_edge e
        JOIN public.warehouse_node fn ON fn.node_id = e.from_node_id
        JOIN public.warehouse_node tn ON tn.node_id = e.to_node_id
        WHERE fn.warehouse_id = tn.warehouse_id
    $view$;

    RETURN true;
END;
$$;

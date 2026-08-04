-- Minimal compatibility schema for the unchanged Spring OptimizationClient.
--
-- The compatibility endpoint needs only a warehouse binding, an optional
-- request-snapshot graph fallback, and an audit log. Business orders and
-- inventory units are not duplicated here; the BE-centered native plan path
-- receives operations in request.structured_input and reads public.warehouse_items.

CREATE SCHEMA IF NOT EXISTS laro_contract;

CREATE TABLE IF NOT EXISTS laro_contract.contract_meta (
    contract_name text PRIMARY KEY,
    contract_version integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO laro_contract.contract_meta(contract_name, contract_version)
VALUES ('spring_be_compat_minimal', 3)
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
    speed_limit_mps double precision NOT NULL DEFAULT 1.0 CHECK (speed_limit_mps > 0),
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

CREATE OR REPLACE VIEW laro_contract.laro_route_nodes_v AS
SELECT warehouse_id, node_id, node_code, x, y, semantic_type,
       service_only, transit_allowed, resource_type, resource_code,
       adjacent_route_node_id, side, active, graph_version
FROM laro_contract.route_node;

CREATE OR REPLACE VIEW laro_contract.laro_route_edges_v AS
SELECT warehouse_id, edge_id, from_node_id, to_node_id, direction_type,
       edge_code, edge_type, distance_m, speed_limit_mps,
       nominal_travel_time_ms, base_cost, physical_resource_code,
       service_only, mobile_robot_traversable, active, version, graph_version
FROM laro_contract.route_edge;

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
        SELECT warehouse_id, node_id,
               COALESCE(node_code, 'N' || node_id::text) AS node_code,
               x, y, COALESCE(node_type::text, 'ROUTE') AS spring_node_type
        FROM public.warehouse_node
    $view$;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_contract.spring_route_edges_v AS
        SELECT fn.warehouse_id, e.edge_id, e.from_node_id, e.to_node_id,
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

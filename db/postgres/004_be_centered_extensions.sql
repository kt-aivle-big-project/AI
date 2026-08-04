-- LARO extensions for the existing Spring BE schema.
--
-- Design rules:
-- 1. public.* Spring tables remain authoritative and are never altered here.
-- 2. There is no LARO orders table and no LARO handling_units table.
-- 3. Business operations arrive in the plan request as structured_input.
-- 4. Existing warehouse_items rows are exposed as request-time inventory units.
-- 5. Only fields/concepts missing from the BE schema live in laro_ext.

CREATE SCHEMA IF NOT EXISTS laro_ext;

CREATE TABLE IF NOT EXISTS laro_ext.contract_meta (
    contract_key text PRIMARY KEY,
    contract_value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO laro_ext.contract_meta(contract_key, contract_value)
VALUES
    ('schema_version', '1.0.0'),
    ('business_input_mode', 'REQUEST_STRUCTURED_INPUT'),
    ('inventory_source', 'PUBLIC_WAREHOUSE_ITEMS'),
    ('orders_table_used', 'false'),
    ('handling_units_table_used', 'false')
ON CONFLICT (contract_key) DO UPDATE SET
    contract_value = EXCLUDED.contract_value,
    updated_at = now();

CREATE TABLE IF NOT EXISTS laro_ext.warehouse_profile (
    warehouse_id bigint PRIMARY KEY,
    warehouse_code text NOT NULL UNIQUE,
    map_version bigint NOT NULL DEFAULT 1,
    inventory_version bigint NOT NULL DEFAULT 1,
    facility_version bigint NOT NULL DEFAULT 1,
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Legacy compatibility fallback. New route-planning writes use typed columns
-- on public.warehouse_node; the view below only consults this table when those
-- columns (and legacy JSONB values) are absent.
CREATE TABLE IF NOT EXISTS laro_ext.node_profile (
    node_id bigint PRIMARY KEY,
    semantic_type text NOT NULL DEFAULT 'ROUTE',
    service_only boolean NOT NULL DEFAULT false,
    transit_allowed boolean NOT NULL DEFAULT true,
    holding_allowed boolean NOT NULL DEFAULT true,
    node_capacity integer NOT NULL DEFAULT 1 CHECK (node_capacity > 0),
    resource_type text,
    resource_code text,
    side text,
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Legacy compatibility fallback for pre-promotion edge metadata.
CREATE TABLE IF NOT EXISTS laro_ext.edge_profile (
    edge_id bigint PRIMARY KEY,
    speed_limit_mps double precision NOT NULL DEFAULT 1.0 CHECK (speed_limit_mps > 0),
    nominal_travel_time_ms bigint CHECK (nominal_travel_time_ms >= 0),
    base_cost double precision CHECK (base_cost >= 0),
    physical_resource_code text,
    service_only boolean NOT NULL DEFAULT false,
    mobile_robot_traversable boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS laro_ext.rack_slot (
    rack_slot_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    rack_node_id bigint NOT NULL,
    rack_level integer NOT NULL CHECK (rack_level > 0),
    storage_location_id bigint,
    capacity integer NOT NULL DEFAULT 0 CHECK (capacity >= 0),
    status text NOT NULL DEFAULT 'EMPTY',
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warehouse_id, rack_node_id, rack_level)
);

CREATE TABLE IF NOT EXISTS laro_ext.warehouse_item_profile (
    warehouse_item_id bigint PRIMARY KEY,
    rack_level integer NOT NULL DEFAULT 1 CHECK (rack_level > 0),
    capacity integer CHECK (capacity >= 0),
    planning_status text NOT NULL DEFAULT 'STORED',
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS laro_ext.robot_profile (
    robot_spec_id bigint PRIMARY KEY,
    capacity_units integer NOT NULL DEFAULT 1 CHECK (capacity_units > 0),
    nominal_speed_mps double precision NOT NULL DEFAULT 1.0 CHECK (nominal_speed_mps > 0),
    minimum_operating_battery_pct double precision NOT NULL DEFAULT 30.0
        CHECK (minimum_operating_battery_pct BETWEEN 0 AND 100),
    max_load_weight double precision,
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS laro_ext.facility (
    facility_id bigserial PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    facility_code text NOT NULL,
    facility_type text NOT NULL CHECK (
        facility_type IN (
            'INBOUND_HANDOFF', 'INBOUND_PORT', 'OUTBOUND_CHUTE',
            'OUTBOUND_STATION', 'STATION_ROBOT', 'EMPTY_TOTE_BUFFER',
            'PARKING_SLOT'
        )
    ),
    node_id bigint,
    access_node_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    served_destination_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    capacity integer,
    status text NOT NULL DEFAULT 'AVAILABLE',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warehouse_id, facility_code)
);

CREATE TABLE IF NOT EXISTS laro_ext.inventory_reservation (
    reservation_id text PRIMARY KEY,
    plan_id text NOT NULL,
    simulation_run_id bigint NOT NULL,
    warehouse_item_id bigint NOT NULL,
    operation_id text NOT NULL,
    reserved_quantity integer NOT NULL CHECK (reserved_quantity > 0),
    expected_item_version bigint,
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN ('ACTIVE', 'COMMITTED', 'RELEASED', 'CANCELLED')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, warehouse_item_id, operation_id)
);
CREATE INDEX IF NOT EXISTS idx_laro_ext_inventory_reservation_run
    ON laro_ext.inventory_reservation(simulation_run_id, status);
CREATE INDEX IF NOT EXISTS idx_laro_ext_inventory_reservation_item
    ON laro_ext.inventory_reservation(warehouse_item_id, status);

CREATE TABLE IF NOT EXISTS laro_ext.simulation_plan (
    plan_id text PRIMARY KEY,
    simulation_run_id bigint NOT NULL,
    warehouse_id bigint NOT NULL,
    plan_version integer NOT NULL CHECK (plan_version > 0),
    base_plan_id text,
    supersedes_plan_id text,
    plan_kind text NOT NULL DEFAULT 'INITIAL',
    status text NOT NULL,
    planning_mode text,
    optimization_backend text,
    map_version text,
    runtime_version text,
    makespan_ms bigint,
    request_json jsonb NOT NULL,
    plan_json jsonb NOT NULL,
    trace_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    superseded_at timestamptz,
    UNIQUE (simulation_run_id, plan_version)
);
CREATE INDEX IF NOT EXISTS idx_laro_ext_simulation_plan_run_created
    ON laro_ext.simulation_plan(simulation_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS laro_ext.request_log (
    request_id text PRIMARY KEY,
    simulation_run_id bigint NOT NULL,
    request_type text NOT NULL CHECK (request_type IN ('PLAN', 'REPLAN')),
    status text NOT NULL,
    request_json jsonb NOT NULL,
    response_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Create/refresh read-only views only after Hibernate has created Spring tables.
CREATE OR REPLACE FUNCTION laro_ext.refresh_be_views()
RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('public.warehouse_layout') IS NULL
       OR to_regclass('public.warehouse_node') IS NULL
       OR to_regclass('public.warehouse_edge') IS NULL
       OR to_regclass('public.warehouse_items') IS NULL
       OR to_regclass('public.storage_location') IS NULL
       OR to_regclass('public.product') IS NULL
       OR to_regclass('public.robot') IS NULL
       OR to_regclass('public.robot_specs') IS NULL
       OR to_regclass('public.simulation_runs') IS NULL THEN
        RETURN false;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'warehouse_items'
          AND column_name = 'rack_level'
    ) THEN
        RETURN false;
    END IF;

    -- Add foreign keys only after Hibernate has created the BE tables. This
    -- keeps container initialization order-independent while still making the
    -- extension schema relationally connected to the BE authority model.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_warehouse_profile_warehouse') THEN
        ALTER TABLE laro_ext.warehouse_profile
            ADD CONSTRAINT fk_laro_warehouse_profile_warehouse
            FOREIGN KEY (warehouse_id) REFERENCES public.warehouse_layout(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_node_profile_node') THEN
        ALTER TABLE laro_ext.node_profile
            ADD CONSTRAINT fk_laro_node_profile_node
            FOREIGN KEY (node_id) REFERENCES public.warehouse_node(node_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_edge_profile_edge') THEN
        ALTER TABLE laro_ext.edge_profile
            ADD CONSTRAINT fk_laro_edge_profile_edge
            FOREIGN KEY (edge_id) REFERENCES public.warehouse_edge(edge_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_rack_slot_warehouse') THEN
        ALTER TABLE laro_ext.rack_slot
            ADD CONSTRAINT fk_laro_rack_slot_warehouse
            FOREIGN KEY (warehouse_id) REFERENCES public.warehouse_layout(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_rack_slot_node') THEN
        ALTER TABLE laro_ext.rack_slot
            ADD CONSTRAINT fk_laro_rack_slot_node
            FOREIGN KEY (rack_node_id) REFERENCES public.warehouse_node(node_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_rack_slot_storage') THEN
        ALTER TABLE laro_ext.rack_slot
            ADD CONSTRAINT fk_laro_rack_slot_storage
            FOREIGN KEY (storage_location_id) REFERENCES public.storage_location(storage_location_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_item_profile_item') THEN
        ALTER TABLE laro_ext.warehouse_item_profile
            ADD CONSTRAINT fk_laro_item_profile_item
            FOREIGN KEY (warehouse_item_id) REFERENCES public.warehouse_items(warehouse_item_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_robot_profile_spec') THEN
        ALTER TABLE laro_ext.robot_profile
            ADD CONSTRAINT fk_laro_robot_profile_spec
            FOREIGN KEY (robot_spec_id) REFERENCES public.robot_specs(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_facility_warehouse') THEN
        ALTER TABLE laro_ext.facility
            ADD CONSTRAINT fk_laro_facility_warehouse
            FOREIGN KEY (warehouse_id) REFERENCES public.warehouse_layout(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_facility_node') THEN
        ALTER TABLE laro_ext.facility
            ADD CONSTRAINT fk_laro_facility_node
            FOREIGN KEY (node_id) REFERENCES public.warehouse_node(node_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_inventory_reservation_run') THEN
        ALTER TABLE laro_ext.inventory_reservation
            ADD CONSTRAINT fk_laro_inventory_reservation_run
            FOREIGN KEY (simulation_run_id) REFERENCES public.simulation_runs(simulation_run_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_inventory_reservation_item') THEN
        ALTER TABLE laro_ext.inventory_reservation
            ADD CONSTRAINT fk_laro_inventory_reservation_item
            FOREIGN KEY (warehouse_item_id) REFERENCES public.warehouse_items(warehouse_item_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_inventory_reservation_plan') THEN
        ALTER TABLE laro_ext.inventory_reservation
            ADD CONSTRAINT fk_laro_inventory_reservation_plan
            FOREIGN KEY (plan_id) REFERENCES laro_ext.simulation_plan(plan_id) ON DELETE CASCADE;
    END IF;

    -- Spring owns the three-level rack model. Keep this extension table as a
    -- compatibility catalogue only; live occupancy is always derived from
    -- public.warehouse_items below.
    INSERT INTO laro_ext.rack_slot (
        warehouse_id,
        rack_node_id,
        rack_level,
        storage_location_id,
        capacity,
        status,
        version,
        updated_at
    )
    SELECT sl.warehouse_id,
           sl.node_id,
           levels.rack_level,
           sl.storage_location_id,
           1,
           CASE WHEN wi.warehouse_item_id IS NULL THEN 'EMPTY' ELSE 'OCCUPIED' END,
           1,
           now()
    FROM public.storage_location sl
    CROSS JOIN generate_series(1, 3) AS levels(rack_level)
    LEFT JOIN public.warehouse_items wi
           ON wi.storage_location_id = sl.storage_location_id
          AND wi.rack_level = levels.rack_level
    ON CONFLICT (warehouse_id, rack_node_id, rack_level) DO UPDATE SET
        storage_location_id = EXCLUDED.storage_location_id,
        capacity = EXCLUDED.capacity,
        status = EXCLUDED.status,
        version = laro_ext.rack_slot.version + 1,
        updated_at = now();
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_simulation_plan_run') THEN
        ALTER TABLE laro_ext.simulation_plan
            ADD CONSTRAINT fk_laro_simulation_plan_run
            FOREIGN KEY (simulation_run_id) REFERENCES public.simulation_runs(simulation_run_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_simulation_plan_warehouse') THEN
        ALTER TABLE laro_ext.simulation_plan
            ADD CONSTRAINT fk_laro_simulation_plan_warehouse
            FOREIGN KEY (warehouse_id) REFERENCES public.warehouse_layout(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_laro_request_log_run') THEN
        ALTER TABLE laro_ext.request_log
            ADD CONSTRAINT fk_laro_request_log_run
            FOREIGN KEY (simulation_run_id) REFERENCES public.simulation_runs(simulation_run_id) ON DELETE CASCADE;
    END IF;

    INSERT INTO laro_ext.warehouse_profile(warehouse_id, warehouse_code)
    SELECT id, 'WH-' || lpad(id::text, 3, '0')
    FROM public.warehouse_layout
    ON CONFLICT (warehouse_id) DO NOTHING;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_ext.be_simulation_run_v AS
        SELECT sr.simulation_run_id,
               sr.warehouse_id,
               wp.warehouse_code,
               sr.status::text AS run_status,
               sr.version AS run_version,
               sr.simulation_speed,
               sr.charging_threshold,
               sr.auto_replan,
               sr.obstacle_enabled
        FROM public.simulation_runs sr
        JOIN laro_ext.warehouse_profile wp ON wp.warehouse_id = sr.warehouse_id
    $view$;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_ext.be_route_node_v AS
        SELECT n.warehouse_id,
               wp.warehouse_code,
               n.node_id,
               COALESCE(n.node_code, 'N' || n.node_id::text) AS node_code,
               n.node_type::text AS be_node_type,
               n.x,
               n.y,
               COALESCE(
                   n.node_type::text,
                   upper(NULLIF(na.row_data->'route_attributes'->>'type', '')),
                   p.semantic_type,
                   'ROUTE'
               ) AS semantic_type,
               COALESCE(
                   NULLIF(na.row_data->>'service_only', '')::boolean,
                   NULLIF(na.row_data->'route_attributes'->>'service_only', '')::boolean,
                   p.service_only,
                   n.node_type::text IN (
                       'RACK_ACCESS', 'INBOUND_HANDOFF_ACCESS',
                       'OUTBOUND_STATION_ACCESS', 'EMPTY_TOTE_BUFFER_ACCESS'
                   )
               ) AS service_only,
               COALESCE(
                   NULLIF(na.row_data->>'transit_allowed', '')::boolean,
                   NULLIF(na.row_data->'route_attributes'->>'transit_allowed', '')::boolean,
                   p.transit_allowed,
                   n.node_type::text NOT IN (
                       'RACK_STORAGE', 'RACK_ACCESS', 'INBOUND_HANDOFF_ACCESS',
                       'OUTBOUND_STATION_ACCESS', 'EMPTY_TOTE_BUFFER_ACCESS'
                   )
               ) AS transit_allowed,
               COALESCE(
                   NULLIF(na.row_data->>'holding_allowed', '')::boolean,
                   NULLIF(na.row_data->'route_attributes'->>'holding_allowed', '')::boolean,
                   p.holding_allowed,
                   true
               ) AS holding_allowed,
               COALESCE(
                   NULLIF(na.row_data->>'node_capacity', '')::integer,
                   NULLIF(na.row_data->'route_attributes'->>'node_capacity', '')::integer,
                   p.node_capacity,
                   1
               ) AS node_capacity,
               COALESCE(
                   NULLIF(na.row_data->>'resource_type', ''),
                   NULLIF(na.row_data->'route_attributes'->>'resource_type', ''),
                   p.resource_type,
                   CASE n.node_type::text
                       WHEN 'RACK_ACCESS' THEN 'RACK'
                       WHEN 'INBOUND_HANDOFF_ACCESS' THEN 'INBOUND_HANDOFF'
                       WHEN 'OUTBOUND_STATION_ACCESS' THEN 'OUTBOUND_STATION'
                       WHEN 'EMPTY_TOTE_BUFFER_ACCESS' THEN 'EMPTY_TOTE_BUFFER'
                       ELSE NULL
                   END
               ) AS resource_type,
               COALESCE(
                   NULLIF(na.row_data->>'resource_code', ''),
                   na.row_data->'route_attributes'->>'rack_id',
                   na.row_data->'route_attributes'->>'handoff_id',
                   na.row_data->'route_attributes'->>'station_id',
                   na.row_data->'route_attributes'->>'buffer_id',
                   na.row_data->'route_attributes'->>'resource_id',
                   p.resource_code
               ) AS resource_code,
               COALESCE(
                   NULLIF(na.row_data->>'side', ''),
                   na.row_data->'route_attributes'->>'side',
                   p.side
               ) AS side,
               COALESCE(
                   NULLIF(na.row_data->>'is_active', '')::boolean,
                   p.active,
                   true
               ) AS active
        FROM public.warehouse_node n
        JOIN laro_ext.warehouse_profile wp ON wp.warehouse_id = n.warehouse_id
        LEFT JOIN laro_ext.node_profile p ON p.node_id = n.node_id
        LEFT JOIN LATERAL (
            SELECT to_jsonb(n) AS row_data
        ) na ON true
    $view$;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_ext.be_route_edge_v AS
        SELECT fn.warehouse_id,
               wp.warehouse_code,
               e.edge_id,
               COALESCE(e.edge_code, 'E' || e.edge_id::text) AS edge_code,
               e.from_node_id,
               e.to_node_id,
               COALESCE(fn.node_code, 'N' || fn.node_id::text) AS from_node_code,
               COALESCE(tn.node_code, 'N' || tn.node_id::text) AS to_node_code,
               e.direction_type::text AS direction_type,
               COALESCE(e.distance, 0)::double precision AS distance_m,
               COALESCE(
                   NULLIF(ea.row_data->>'speed_limit_mps', '')::double precision,
                   NULLIF(ea.row_data->'route_attributes'->>'speed_limit_mps', '')::double precision,
                   p.speed_limit_mps,
                   1.0
               ) AS speed_limit_mps,
               COALESCE(
                   NULLIF(ea.row_data->>'nominal_travel_time_ms', '')::bigint,
                   NULLIF(ea.row_data->'route_attributes'->>'nominal_travel_time_ms', '')::bigint,
                   p.nominal_travel_time_ms,
                   round((COALESCE(e.distance, 0) / NULLIF(COALESCE(
                       NULLIF(ea.row_data->>'speed_limit_mps', '')::double precision,
                       NULLIF(ea.row_data->'route_attributes'->>'speed_limit_mps', '')::double precision,
                       p.speed_limit_mps,
                       1.0
                   ), 0)) * 1000)::bigint
               ) AS nominal_travel_time_ms,
               COALESCE(
                   NULLIF(ea.row_data->>'cost', '')::double precision,
                   NULLIF(ea.row_data->'route_attributes'->>'cost', '')::double precision,
                   p.base_cost,
                   e.distance,
                   0
               )::double precision AS base_cost,
               COALESCE(
                   NULLIF(ea.row_data->>'physical_resource_code', ''),
                   ea.row_data->'route_attributes'->>'physical_resource_code',
                   ea.row_data->'route_attributes'->>'resource_id',
                   p.physical_resource_code,
                   e.edge_code,
                   'E' || e.edge_id::text
               ) AS physical_resource_code,
               COALESCE(
                   NULLIF(ea.row_data->>'service_only', '')::boolean,
                   NULLIF(ea.row_data->'route_attributes'->>'service_only', '')::boolean,
                   p.service_only,
                   false
               ) AS service_only,
               COALESCE(
                   NULLIF(ea.row_data->>'mobile_robot_traversable', '')::boolean,
                   NULLIF(ea.row_data->'route_attributes'->>'mobile_robot_traversable', '')::boolean,
                   p.mobile_robot_traversable,
                   true
               ) AS mobile_robot_traversable,
               (
                   COALESCE(p.active, true)
                   AND COALESCE(
                       NULLIF(to_jsonb(fn)->>'is_active', '')::boolean,
                       true
                   )
                   AND COALESCE(
                       NULLIF(to_jsonb(tn)->>'is_active', '')::boolean,
                       true
                   )
               ) AS active,
               COALESCE(p.version, 1) AS version,
               COALESCE(
                   NULLIF(ea.row_data->>'edge_type', ''),
                   ea.row_data->'route_attributes'->>'type',
                   'lane'
               ) AS edge_type
        FROM public.warehouse_edge e
        JOIN public.warehouse_node fn ON fn.node_id = e.from_node_id
        JOIN public.warehouse_node tn ON tn.node_id = e.to_node_id
        JOIN laro_ext.warehouse_profile wp ON wp.warehouse_id = fn.warehouse_id
        LEFT JOIN laro_ext.edge_profile p ON p.edge_id = e.edge_id
        LEFT JOIN LATERAL (
            SELECT to_jsonb(e) AS row_data
        ) ea ON true
        WHERE fn.warehouse_id = tn.warehouse_id
    $view$;

    -- PostgreSQL CREATE OR REPLACE VIEW cannot remove legacy columns, so drop
    -- the old inventory view before recreating the current no-expiry shape.
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'laro_ext'
          AND table_name = 'be_inventory_unit_v'
          AND column_name IN ('box_capacity', 'expiry_date', 'expiry_managed')
    ) THEN
        EXECUTE 'DROP VIEW laro_ext.be_inventory_unit_v';
    END IF;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_ext.be_inventory_unit_v AS
        SELECT wi.warehouse_item_id,
               wi.warehouse_id,
               wp.warehouse_code,
               wi.storage_location_id,
               wi.node_id AS rack_node_id,
               COALESCE(n.node_code, 'N' || n.node_id::text) AS rack_code,
               wi.rack_level,
               wi.product_id AS item_id,
               p.product_code,
               p.product_name,
               wi.quantity,
               p.units_per_box AS capacity,
               COALESCE(wip.planning_status, 'STORED') AS planning_status,
               COALESCE(wip.version, 1) AS version,
               wi.received_at,
               p.category,
               'EA'::varchar(20) AS unit,
               NULL::numeric(10,3) AS unit_weight_kg,
               NULL::numeric(10,3) AS unit_volume_liter,
               p.barcode,
               p.temperature_zone,
               p.fragile,
               p.units_per_box AS quantity_capacity,
               NULL::integer AS weight_capacity,
               NULL::integer AS volume_capacity,
               p.units_per_box,
               1 AS transport_box_count
        FROM public.warehouse_items wi
        JOIN public.warehouse_node n ON n.node_id = wi.node_id
        JOIN public.storage_location sl ON sl.storage_location_id = wi.storage_location_id
        JOIN public.product p ON p.product_id = wi.product_id
        JOIN laro_ext.warehouse_profile wp ON wp.warehouse_id = wi.warehouse_id
        LEFT JOIN laro_ext.warehouse_item_profile wip
               ON wip.warehouse_item_id = wi.warehouse_item_id
    $view$;

    EXECUTE $view$
        CREATE OR REPLACE VIEW laro_ext.be_robot_master_v AS
        SELECT r.robot_id,
               r.warehouse_id,
               wp.warehouse_code,
               r.node_id AS initial_node_id,
               r.battery AS initial_battery_pct,
               r.status::text AS availability_status,
               rs.id AS robot_spec_id,
               rs.robot_code,
               rs.task_code,
               COALESCE(rp.capacity_units, 1) AS capacity_units,
               COALESCE(rp.nominal_speed_mps, 1.0) AS nominal_speed_mps,
               COALESCE(rp.minimum_operating_battery_pct, 30.0) AS minimum_operating_battery_pct
        FROM public.robot r
        JOIN public.robot_specs rs ON rs.id = r.robot_spec_id
        JOIN laro_ext.warehouse_profile wp ON wp.warehouse_id = r.warehouse_id
        LEFT JOIN laro_ext.robot_profile rp ON rp.robot_spec_id = rs.id
    $view$;

    RETURN true;
END;
$$;

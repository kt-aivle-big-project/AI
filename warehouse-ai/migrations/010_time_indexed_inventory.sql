-- Time-indexed inventory and inbound/outbound order data.
-- Review and apply manually after migrations 002-009. This file never runs
-- automatically and never deletes or replays existing inventory.

BEGIN;

CREATE TABLE IF NOT EXISTS inventory_item (
    item_id text PRIMARY KEY,
    item_name text NOT NULL,
    base_unit text NOT NULL DEFAULT 'BOX',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_inventory_item_base_unit CHECK (base_unit = 'BOX')
);

INSERT INTO inventory_item (item_id, item_name, base_unit)
SELECT DISTINCT item_id, item_id, 'BOX'
FROM warehouse_items
WHERE item_id IS NOT NULL
ON CONFLICT (item_id) DO NOTHING;

ALTER TABLE warehouse_items
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'AVAILABLE',
    ADD COLUMN IF NOT EXISTS received_at timestamptz,
    ADD COLUMN IF NOT EXISTS available_at timestamptz,
    ADD COLUMN IF NOT EXISTS expiration_at timestamptz,
    ADD COLUMN IF NOT EXISTS base_unit text NOT NULL DEFAULT 'BOX';

CREATE INDEX IF NOT EXISTS idx_warehouse_items_projection
    ON warehouse_items (warehouse_id, item_id, status, available_at);
CREATE INDEX IF NOT EXISTS idx_warehouse_items_fefo
    ON warehouse_items (
        warehouse_id, item_id, expiration_at, available_at, lot_id
    );

CREATE TABLE IF NOT EXISTS inbound_order_line (
    inbound_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    item_id text NOT NULL,
    quantity_boxes integer NOT NULL,
    expected_arrival_at timestamptz,
    expected_available_at timestamptz,
    actual_arrival_at timestamptz,
    actual_available_at timestamptz,
    status text NOT NULL DEFAULT 'SCHEDULED',
    storage_node_id bigint,
    lot_id text,
    warehouse_item_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_inbound_quantity_boxes CHECK (quantity_boxes > 0)
);

CREATE INDEX IF NOT EXISTS idx_inbound_order_open
    ON inbound_order_line (warehouse_id, status, expected_available_at);
CREATE INDEX IF NOT EXISTS idx_inbound_order_item_available
    ON inbound_order_line (warehouse_id, item_id, expected_available_at);

CREATE TABLE IF NOT EXISTS outbound_order_line (
    outbound_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    item_id text NOT NULL,
    requested_quantity_boxes integer NOT NULL,
    required_by timestamptz,
    priority text NOT NULL DEFAULT 'NORMAL',
    allow_partial_fulfillment boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'OPEN',
    work_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_outbound_quantity_boxes CHECK (requested_quantity_boxes > 0)
);

CREATE INDEX IF NOT EXISTS idx_outbound_order_open
    ON outbound_order_line (warehouse_id, status, required_by, priority);
CREATE INDEX IF NOT EXISTS idx_outbound_order_item_required
    ON outbound_order_line (warehouse_id, item_id, required_by);

ALTER TABLE works
    ADD COLUMN IF NOT EXISTS operation_type text,
    ADD COLUMN IF NOT EXISTS quantity_boxes integer,
    ADD COLUMN IF NOT EXISTS required_at timestamptz,
    ADD COLUMN IF NOT EXISTS allow_partial_fulfillment boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS inventory_order_id text;

CREATE INDEX IF NOT EXISTS idx_works_inventory_requirement
    ON works (warehouse_id, operation_type, item_id, required_at)
    WHERE operation_type IN ('INBOUND', 'OUTBOUND');

CREATE TABLE IF NOT EXISTS inventory_movement (
    movement_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    item_id text NOT NULL,
    lot_id text,
    warehouse_item_id text,
    work_id text,
    order_id text,
    plan_version text,
    movement_type text NOT NULL,
    quantity_delta_boxes integer NOT NULL,
    occurred_at timestamptz NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inventory_movement_item_time
    ON inventory_movement (warehouse_id, item_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_inventory_movement_work
    ON inventory_movement (work_id, occurred_at DESC)
    WHERE work_id IS NOT NULL;

DO $$
BEGIN
    ALTER TABLE warehouse_items
        ADD CONSTRAINT fk_warehouse_items_inventory_item
        FOREIGN KEY (item_id) REFERENCES inventory_item(item_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE inbound_order_line
        ADD CONSTRAINT fk_inbound_order_inventory_item
        FOREIGN KEY (item_id) REFERENCES inventory_item(item_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE outbound_order_line
        ADD CONSTRAINT fk_outbound_order_inventory_item
        FOREIGN KEY (item_id) REFERENCES inventory_item(item_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE inventory_movement
        ADD CONSTRAINT fk_inventory_movement_inventory_item
        FOREIGN KEY (item_id) REFERENCES inventory_item(item_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE inventory_movement
        ADD CONSTRAINT fk_inventory_movement_warehouse_item
        FOREIGN KEY (warehouse_item_id)
        REFERENCES warehouse_items(warehouse_item_id) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;

-- Safety check before applying:
-- SELECT count(*) FROM warehouse_items WHERE quantity < 0;
-- SELECT base_unit, count(*) FROM inventory_item GROUP BY base_unit;
--
-- Rollback (only after confirming no application version depends on 010):
-- BEGIN;
-- DROP TABLE IF EXISTS inventory_movement;
-- DROP TABLE IF EXISTS outbound_order_line;
-- DROP TABLE IF EXISTS inbound_order_line;
-- DROP TABLE IF EXISTS inventory_item;
-- ALTER TABLE works DROP COLUMN IF EXISTS inventory_order_id;
-- ALTER TABLE works DROP COLUMN IF EXISTS allow_partial_fulfillment;
-- ALTER TABLE works DROP COLUMN IF EXISTS required_at;
-- ALTER TABLE works DROP COLUMN IF EXISTS quantity_boxes;
-- ALTER TABLE works DROP COLUMN IF EXISTS operation_type;
-- ALTER TABLE warehouse_items DROP COLUMN IF EXISTS base_unit;
-- ALTER TABLE warehouse_items DROP COLUMN IF EXISTS expiration_at;
-- ALTER TABLE warehouse_items DROP COLUMN IF EXISTS available_at;
-- ALTER TABLE warehouse_items DROP COLUMN IF EXISTS received_at;
-- ALTER TABLE warehouse_items DROP COLUMN IF EXISTS status;
-- COMMIT;

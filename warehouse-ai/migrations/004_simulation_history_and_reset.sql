CREATE TABLE IF NOT EXISTS simulation_session (
    simulation_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    status varchar(30) NOT NULL,
    generation integer NOT NULL DEFAULT 1,
    base_state jsonb NOT NULL,
    current_state jsonb NOT NULL,
    checkpoint text,
    created_by_command_id text,
    last_command_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reset_at timestamptz,
    reset_by text,
    reset_reason text
);

CREATE INDEX IF NOT EXISTS idx_simulation_session_warehouse_updated
    ON simulation_session (warehouse_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_simulation_session_warehouse_status
    ON simulation_session (warehouse_id, status);

CREATE INDEX IF NOT EXISTS idx_simulation_session_created_command
    ON simulation_session (created_by_command_id)
    WHERE created_by_command_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_simulation_session_last_command
    ON simulation_session (last_command_id)
    WHERE last_command_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS simulation_reset_audit (
    reset_id text PRIMARY KEY,
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    target_type varchar(30) NOT NULL,
    target_simulation_id text,
    actor_id text,
    reason text NOT NULL,
    status varchar(30) NOT NULL,
    affected_simulation_count integer NOT NULL DEFAULT 0,
    before_summary jsonb,
    after_summary jsonb,
    failure_summary jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_simulation_reset_audit_warehouse_created
    ON simulation_reset_audit (warehouse_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_simulation_reset_audit_target_created
    ON simulation_reset_audit (target_simulation_id, created_at DESC)
    WHERE target_simulation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_simulation_reset_audit_command
    ON simulation_reset_audit (command_id);

DROP INDEX IF EXISTS ux_simulation_run_simulation_id;

CREATE INDEX IF NOT EXISTS idx_simulation_run_simulation_created
    ON simulation_run (simulation_id, created_at DESC)
    WHERE simulation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_simulation_run_warehouse_created
    ON simulation_run (warehouse_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_simulation_run_command
    ON simulation_run (command_id);

WITH latest_run AS (
    SELECT DISTINCT ON (simulation_id)
        simulation_id,
        warehouse_id,
        command_id,
        current_state,
        checkpoint,
        created_at
    FROM simulation_run
    WHERE simulation_id IS NOT NULL
      AND current_state IS NOT NULL
    ORDER BY simulation_id, created_at DESC, run_id DESC
)
INSERT INTO simulation_session (
    simulation_id,
    warehouse_id,
    status,
    generation,
    base_state,
    current_state,
    checkpoint,
    created_by_command_id,
    last_command_id,
    created_at,
    updated_at
)
SELECT
    simulation_id,
    warehouse_id,
    'ACTIVE',
    1,
    current_state,
    current_state,
    checkpoint,
    command_id,
    command_id,
    created_at,
    created_at
FROM latest_run
ON CONFLICT (simulation_id) DO NOTHING;

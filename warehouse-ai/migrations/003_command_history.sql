CREATE TABLE IF NOT EXISTS command_history (
    command_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    command_type varchar(30),
    requested_execution_mode varchar(30),
    resolved_execution_mode varchar(30),
    source varchar(30),
    original_text text,
    actor_id text,
    status varchar(40) NOT NULL,
    simulation_id text,
    plan_version text,
    parent_command_id text,
    received_at timestamptz NOT NULL,
    completed_at timestamptz,
    result_summary jsonb,
    error_summary jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_command_history_warehouse_received
    ON command_history (warehouse_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_command_history_simulation
    ON command_history (simulation_id)
    WHERE simulation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_command_history_plan_version
    ON command_history (plan_version)
    WHERE plan_version IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_command_history_actor_received
    ON command_history (actor_id, received_at DESC)
    WHERE actor_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_command_history_status_received
    ON command_history (status, received_at DESC);

CREATE TABLE IF NOT EXISTS planning_stage_log (
    stage_log_id bigserial PRIMARY KEY,
    command_id text NOT NULL,
    sequence integer NOT NULL,
    node_name varchar(80) NOT NULL,
    attempt integer NOT NULL DEFAULT 1,
    status varchar(30) NOT NULL,
    message text,
    details jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_planning_stage_command
        FOREIGN KEY (command_id)
        REFERENCES command_history(command_id),
    CONSTRAINT uq_planning_stage_command_sequence_attempt
        UNIQUE (command_id, sequence, attempt)
);

CREATE INDEX IF NOT EXISTS idx_planning_stage_command_order
    ON planning_stage_log (command_id, sequence ASC, attempt ASC);

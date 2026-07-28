CREATE TABLE IF NOT EXISTS conversation_session (
    conversation_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    active_command_id text,
    active_plan_version text,
    active_simulation_id text,
    active_clarification_id text,
    resolved_constraints jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_warehouse_updated
    ON conversation_session (warehouse_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_command_link (
    conversation_id text NOT NULL,
    command_id text NOT NULL,
    parent_command_id text,
    sequence_number integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, command_id),
    CONSTRAINT ux_conversation_command UNIQUE (command_id),
    CONSTRAINT fk_conversation_link_session
        FOREIGN KEY (conversation_id) REFERENCES conversation_session(conversation_id),
    CONSTRAINT fk_conversation_link_command
        FOREIGN KEY (command_id) REFERENCES command_history(command_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_sequence
    ON conversation_command_link (conversation_id, sequence_number);

CREATE INDEX IF NOT EXISTS idx_conversation_link_parent
    ON conversation_command_link (parent_command_id);

-- Existing command_history rows are intentionally not backfilled.
-- Rollback (only after exporting conversation metadata):
-- DROP TABLE IF EXISTS conversation_command_link;
-- DROP TABLE IF EXISTS conversation_session;

CREATE TABLE IF NOT EXISTS clarification_request (
    clarification_id text PRIMARY KEY,
    conversation_id text,
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    status text NOT NULL,
    reason_code text NOT NULL,
    question text NOT NULL,
    missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
    ambiguous_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
    options jsonb NOT NULL DEFAULT '[]'::jsonb,
    original_text text NOT NULL,
    response jsonb,
    resolved_command_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    resolved_at timestamptz,
    CONSTRAINT fk_clarification_command
        FOREIGN KEY (command_id) REFERENCES command_history(command_id)
);

CREATE INDEX IF NOT EXISTS idx_clarification_conversation_created
    ON clarification_request (conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_clarification_command
    ON clarification_request (command_id);

CREATE INDEX IF NOT EXISTS idx_clarification_status_created
    ON clarification_request (status, created_at DESC);

-- Rollback (only after exporting audit data):
-- DROP TABLE IF EXISTS clarification_request;

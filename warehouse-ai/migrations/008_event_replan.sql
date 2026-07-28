CREATE TABLE IF NOT EXISTS execution_event_processing (
    event_id text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    event_type text NOT NULL,
    event_source text NOT NULL,
    status text NOT NULL,
    event_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    impact_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_signature text,
    generated_command_id text,
    generated_plan_version text,
    replan_request_id text,
    approval_required boolean NOT NULL DEFAULT false,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_execution_event_warehouse_created
    ON execution_event_processing (warehouse_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_execution_event_signature_created
    ON execution_event_processing (
        warehouse_id, failure_signature, created_at DESC
    );

CREATE INDEX IF NOT EXISTS idx_execution_event_status
    ON execution_event_processing (status, created_at DESC);

CREATE TABLE IF NOT EXISTS automatic_replan_request (
    request_id text PRIMARY KEY,
    event_id text NOT NULL UNIQUE,
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    scope text NOT NULL,
    status text NOT NULL,
    execution_context text NOT NULL,
    affected_robot_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    affected_task_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    expected_active_plan_version text,
    generated_plan_version text,
    simulation_id text,
    verification_decision text,
    approval_required boolean NOT NULL DEFAULT false,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    approved_by text,
    approval_reason text,
    approved_at timestamptz,
    rejected_by text,
    rejection_reason text,
    rejected_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT fk_auto_replan_event
        FOREIGN KEY (event_id) REFERENCES execution_event_processing(event_id)
);

CREATE INDEX IF NOT EXISTS idx_auto_replan_warehouse_created
    ON automatic_replan_request (warehouse_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auto_replan_status
    ON automatic_replan_request (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auto_replan_command
    ON automatic_replan_request (command_id);

-- Rollback (export event/replan audit data first):
-- DROP TABLE IF EXISTS automatic_replan_request;
-- DROP TABLE IF EXISTS execution_event_processing;

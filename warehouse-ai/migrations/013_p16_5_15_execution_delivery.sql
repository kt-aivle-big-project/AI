-- P16.5.15 approved plan and durable idempotent robot command delivery.

CREATE TABLE IF NOT EXISTS execution_plan_approval (
    plan_version text PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    command_id text,
    verification_decision text NOT NULL,
    status text NOT NULL,
    plan_fingerprint text NOT NULL,
    expected_active_plan_version text,
    approved_by text NOT NULL,
    approval_reason text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    revoked_by text,
    revocation_reason text,
    revoked_at timestamptz,
    CONSTRAINT ck_execution_plan_approval_status
        CHECK (status IN ('APPROVED', 'REVOKED'))
);

CREATE INDEX IF NOT EXISTS idx_execution_plan_approval_warehouse
    ON execution_plan_approval (warehouse_id, approved_at DESC);

CREATE TABLE IF NOT EXISTS robot_execution_dispatch (
    dispatch_id text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    warehouse_id bigint NOT NULL,
    command_id text,
    plan_version text NOT NULL,
    approved_plan_fingerprint text NOT NULL,
    payload_fingerprint text NOT NULL,
    previous_active_plan_version text,
    status text NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    command_batches jsonb NOT NULL DEFAULT '[]'::jsonb,
    command_states jsonb NOT NULL DEFAULT '[]'::jsonb,
    gateway_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT fk_robot_execution_plan_approval
        FOREIGN KEY (plan_version)
        REFERENCES execution_plan_approval(plan_version),
    CONSTRAINT ck_robot_execution_dispatch_status CHECK (
        status IN (
            'PREPARED', 'DISPATCHING', 'AWAITING_ACK', 'PARTIAL_ACK',
            'COMPLETED', 'DISPATCH_TIMEOUT', 'RETRY_EXHAUSTED',
            'PARTIAL_FAILURE', 'CANCELED', 'CANCELED_PARTIAL_EXECUTION',
            'ROLLED_BACK'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_robot_execution_dispatch_plan
    ON robot_execution_dispatch (plan_version, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_robot_execution_dispatch_warehouse_status
    ON robot_execution_dispatch (warehouse_id, status, updated_at DESC);

-- Rollback only after exporting execution delivery audit:
-- DROP TABLE IF EXISTS robot_execution_dispatch;
-- DROP TABLE IF EXISTS execution_plan_approval;

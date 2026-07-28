CREATE TABLE IF NOT EXISTS scenario_comparison (
    comparison_id text PRIMARY KEY,
    request_key text NOT NULL UNIQUE,
    conversation_id text,
    warehouse_id bigint NOT NULL,
    command_id text NOT NULL,
    status text NOT NULL,
    request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    recommendation_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT fk_scenario_comparison_command
        FOREIGN KEY (command_id) REFERENCES command_history(command_id)
);

CREATE INDEX IF NOT EXISTS idx_scenario_comparison_warehouse_created
    ON scenario_comparison (warehouse_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_scenario_comparison_conversation
    ON scenario_comparison (conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_scenario_comparison_status
    ON scenario_comparison (status, created_at DESC);

CREATE TABLE IF NOT EXISTS scenario_comparison_run (
    comparison_id text NOT NULL,
    scenario_id text NOT NULL,
    simulation_id text,
    command_id text NOT NULL,
    status text NOT NULL,
    scenario_definition jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (comparison_id, scenario_id),
    CONSTRAINT fk_scenario_run_comparison
        FOREIGN KEY (comparison_id) REFERENCES scenario_comparison(comparison_id),
    CONSTRAINT fk_scenario_run_command
        FOREIGN KEY (command_id) REFERENCES command_history(command_id)
);

CREATE INDEX IF NOT EXISTS idx_scenario_run_scenario
    ON scenario_comparison_run (scenario_id);

CREATE INDEX IF NOT EXISTS idx_scenario_run_simulation
    ON scenario_comparison_run (simulation_id);

-- Rollback (export comparison audit data first):
-- DROP TABLE IF EXISTS scenario_comparison_run;
-- DROP TABLE IF EXISTS scenario_comparison;

ALTER TABLE simulation_run
    ADD COLUMN IF NOT EXISTS simulation_id text,
    ADD COLUMN IF NOT EXISTS current_state jsonb,
    ADD COLUMN IF NOT EXISTS checkpoint text;

CREATE UNIQUE INDEX IF NOT EXISTS ux_simulation_run_simulation_id
    ON simulation_run (simulation_id)
    WHERE simulation_id IS NOT NULL;

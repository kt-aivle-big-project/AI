-- Daily scheduling input constraints and execution timestamps.
-- Apply manually after reviewing the existing works/work_event schema.

ALTER TABLE works
    ADD COLUMN IF NOT EXISTS actual_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS actual_completed_at timestamptz;

CREATE TABLE IF NOT EXISTS work_dependencies (
    predecessor_work_id text NOT NULL,
    successor_work_id text NOT NULL,
    dependency_type text NOT NULL DEFAULT 'FINISH_TO_START',
    lag_seconds integer NOT NULL DEFAULT 0 CHECK (lag_seconds >= 0),
    source_command_id text,
    plan_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (predecessor_work_id, successor_work_id),
    CONSTRAINT fk_work_dependency_predecessor
        FOREIGN KEY (predecessor_work_id) REFERENCES works(work_id),
    CONSTRAINT fk_work_dependency_successor
        FOREIGN KEY (successor_work_id) REFERENCES works(work_id),
    CONSTRAINT ck_work_dependency_not_self
        CHECK (predecessor_work_id <> successor_work_id)
);

CREATE INDEX IF NOT EXISTS idx_work_dependencies_successor
    ON work_dependencies (successor_work_id);

CREATE TABLE IF NOT EXISTS work_schedule_constraints (
    work_id text PRIMARY KEY,
    earliest_start timestamptz,
    latest_finish timestamptz,
    time_constraint_type text NOT NULL DEFAULT 'ASAP',
    fixed_robot_id text,
    same_robot_group text,
    sequence_group text,
    sequence_order integer CHECK (sequence_order IS NULL OR sequence_order > 0),
    source_command_id text,
    plan_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_work_schedule_constraint_work
        FOREIGN KEY (work_id) REFERENCES works(work_id),
    CONSTRAINT ck_work_schedule_window
        CHECK (
            earliest_start IS NULL OR latest_finish IS NULL
            OR earliest_start <= latest_finish
        )
);

CREATE INDEX IF NOT EXISTS idx_work_schedule_earliest
    ON work_schedule_constraints (earliest_start);
CREATE INDEX IF NOT EXISTS idx_work_schedule_latest
    ON work_schedule_constraints (latest_finish);

-- Rollback only after exporting schedule evidence:
-- DROP TABLE IF EXISTS work_schedule_constraints;
-- DROP TABLE IF EXISTS work_dependencies;
-- ALTER TABLE works DROP COLUMN IF EXISTS actual_started_at;
-- ALTER TABLE works DROP COLUMN IF EXISTS actual_completed_at;

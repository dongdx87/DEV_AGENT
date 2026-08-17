-- One row per issue handed to a BAM agent, so a later pass can find the job
-- again and write its result back to Twenty.
--
-- The Twenty link table maps a record to a task. This maps a record to a *run*,
-- which is a different lifetime: an issue can be run more than once.
CREATE TABLE IF NOT EXISTS plugin_bloy_agent_run (
    id             VARCHAR(64) PRIMARY KEY,
    issue_id       VARCHAR(64) NOT NULL,
    issue_key      VARCHAR(64) NOT NULL,
    project_id     VARCHAR(64) NOT NULL,
    job_id         INTEGER NOT NULL,
    agent_alias    VARCHAR(128) NOT NULL,
    mode           VARCHAR(16) NOT NULL,
    -- pending while the job runs, reported once the outcome reached Twenty
    state          VARCHAR(16) NOT NULL,
    done_status    VARCHAR(64),
    error_status   VARCHAR(64),
    created_at     TIMESTAMP,
    reported_at    TIMESTAMP,
    detail         TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_bloy_agent_run_job
    ON plugin_bloy_agent_run (job_id);

CREATE INDEX IF NOT EXISTS ix_bloy_agent_run_state
    ON plugin_bloy_agent_run (state);

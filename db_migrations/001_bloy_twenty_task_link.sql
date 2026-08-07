-- Link between a Twenty record and the Agent Team task created from it.
--
-- Created here rather than relying only on model create_all so the schema is
-- explicit and versioned. The ORM model in models.py maps the same table;
-- core's create_all runs with checkfirst=True, so this file wins on first boot.
CREATE TABLE IF NOT EXISTS plugin_bloy_twenty_task_link (
    id                VARCHAR(64) PRIMARY KEY,
    board_id          VARCHAR(64) NOT NULL,
    twenty_id         VARCHAR(64) NOT NULL,
    twenty_url        VARCHAR(1024),
    task_id           VARCHAR(64) NOT NULL,
    last_payload_hash VARCHAR(64),
    last_synced_at    TIMESTAMP,
    last_pushed_state VARCHAR(32),
    orphaned_at       TIMESTAMP,
    note              TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_bloy_link_board_record
    ON plugin_bloy_twenty_task_link (board_id, twenty_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_bloy_link_task
    ON plugin_bloy_twenty_task_link (task_id);

CREATE INDEX IF NOT EXISTS ix_bloy_link_board
    ON plugin_bloy_twenty_task_link (board_id);

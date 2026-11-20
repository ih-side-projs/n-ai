-- WP8: allow the control/approval actions used by the web UI.
-- SQLite cannot ALTER a CHECK constraint, so recreate the commands table.
CREATE TABLE commands_new (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    error TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id, project_id) REFERENCES nodes(id, project_id) ON DELETE RESTRICT
);
INSERT INTO commands_new SELECT * FROM commands;
DROP TABLE commands;
ALTER TABLE commands_new RENAME TO commands;
CREATE INDEX IF NOT EXISTS idx_commands_project_status ON commands(project_id, status);
CREATE INDEX IF NOT EXISTS idx_commands_status_created ON commands(status, created_at);

ALTER TABLE tool_defs ADD COLUMN input_schema_json TEXT;
UPDATE tool_defs SET input_schema_json = parameters_json;

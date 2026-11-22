-- Amended spec pipeline: add clarify/select_map/lint job kinds.
-- SQLite requires table recreation to widen a CHECK constraint.
CREATE TABLE jobs_new (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'clarify','decompose','select_map','interfaces','check_interfaces',
        'component_spec','lint','reconcile','contract_stub','author_tests',
        'implement','verify','integrate','diagnose','review'
    )),
    model_role TEXT NOT NULL CHECK (model_role IN ('reasoner', 'coder')),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    attempt INTEGER NOT NULL DEFAULT 1,
    input_ref_json TEXT NOT NULL DEFAULT '{}',
    result_ref_json TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    started_at TEXT,
    ended_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
    , FOREIGN KEY (node_id, project_id) REFERENCES nodes(id, project_id) ON DELETE RESTRICT
);
INSERT INTO jobs_new SELECT * FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX IF NOT EXISTS idx_jobs_project_status ON jobs(project_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status_role ON jobs(status, model_role);
CREATE INDEX IF NOT EXISTS idx_jobs_node ON jobs(node_id);
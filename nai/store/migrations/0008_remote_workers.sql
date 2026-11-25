-- Pull-based remote workers (Prompt 5).
--
-- Linux stays the only orchestrator: it writes the store, calls the model and
-- parses results.  A worker only executes named recipes, so these tables hold
-- no free-form command strings.  Tokens are stored hashed; the plaintext is
-- shown once by ``nai worker add`` and never persisted.

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    toolchains_json TEXT NOT NULL DEFAULT '{}',
    last_seen TEXT,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workers_last_seen ON workers(last_seen);

CREATE TABLE IF NOT EXISTS remote_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT,
    recipe TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    snapshot_sha TEXT NOT NULL,
    required_caps TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'claimed', 'done', 'failed', 'cancelled')),
    lease_expires_at TEXT,
    worker_id TEXT,
    redeliveries INTEGER NOT NULL DEFAULT 0 CHECK (redeliveries >= 0),
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL,
    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_remote_tasks_status ON remote_tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_remote_tasks_project ON remote_tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_remote_tasks_worker ON remote_tasks(worker_id);

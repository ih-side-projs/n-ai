CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    workspace_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'done', 'archived')),
    created_at TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('system', 'subsystem', 'component', 'leaf', 'integration')),
    name TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT '',
    spec_text TEXT NOT NULL DEFAULT '',
    contract_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'awaiting_approval', 'approved', 'ready', 'running',
                         'verifying', 'done', 'failed', 'escalated', 'blocked', 'stale')),
    content_hash TEXT,
    built_hash TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    test_command TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id, project_id) REFERENCES nodes(id, project_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_nodes_one_root_per_project
    ON nodes(project_id) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX uq_nodes_sibling_name
    ON nodes(project_id, parent_id, name) WHERE parent_id IS NOT NULL;
CREATE INDEX idx_nodes_project_status ON nodes(project_id, status);
CREATE INDEX idx_nodes_parent ON nodes(parent_id);

-- Dependencies are normalized so the database can enforce uniqueness and ordering.
-- Their public representation is the ordered depends_on list defined in section 4.
CREATE TABLE node_dependencies (
    node_id TEXT NOT NULL,
    dependency_node_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (node_id, dependency_node_id),
    UNIQUE (node_id, ordinal),
    CHECK (node_id <> dependency_node_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (dependency_node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX idx_node_dependencies_dependency ON node_dependencies(dependency_node_id);

CREATE TABLE requirements (
    node_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    text TEXT NOT NULL,
    testable INTEGER NOT NULL CHECK (testable IN (0, 1)),
    PRIMARY KEY (node_id, requirement_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX idx_requirements_node ON requirements(node_id);

CREATE TABLE test_records (
    node_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    requirement_ids_json TEXT NOT NULL DEFAULT '[]',
    frozen_hash TEXT NOT NULL,
    last_result TEXT NOT NULL DEFAULT 'none'
        CHECK (last_result IN ('pass', 'fail', 'error', 'none')),
    last_run_at TEXT,
    PRIMARY KEY (node_id, file_path),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX idx_test_records_node ON test_records(node_id);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('decompose', 'interfaces', 'check_interfaces', 'component_spec',
        'reconcile', 'contract_stub', 'author_tests', 'implement', 'verify', 'integrate', 'diagnose', 'review')),
    model_role TEXT NOT NULL CHECK (model_role IN ('reasoner', 'coder')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    input_ref_json TEXT NOT NULL DEFAULT '{}',
    result_ref_json TEXT NOT NULL DEFAULT '{}',
    tokens_in INTEGER,
    tokens_out INTEGER,
    started_at TEXT,
    ended_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id, project_id) REFERENCES nodes(id, project_id) ON DELETE RESTRICT
);
CREATE INDEX idx_jobs_status_role ON jobs(status, model_role);
CREATE INDEX idx_jobs_project_status ON jobs(project_id, status);
CREATE INDEX idx_jobs_node ON jobs(node_id);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT,
    job_id TEXT,
    time TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (node_id, project_id) REFERENCES nodes(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (job_id, project_id) REFERENCES jobs(id, project_id) ON DELETE RESTRICT
);
CREATE INDEX idx_events_project_time ON events(project_id, time);
CREATE INDEX idx_events_node ON events(node_id);
CREATE INDEX idx_events_job ON events(job_id);

CREATE TRIGGER events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('spec', 'contract', 'code', 'test', 'output-file', 'report', 'log')),
    path TEXT NOT NULL,
    sha1 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id, project_id) REFERENCES nodes(id, project_id) ON DELETE RESTRICT
);
CREATE INDEX idx_artifacts_project ON artifacts(project_id);
CREATE INDEX idx_artifacts_node ON artifacts(node_id);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('component-map', 'interfaces', 'component-spec', 'escalation')),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'edited')),
    note TEXT NOT NULL DEFAULT '',
    time TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX idx_approvals_node ON approvals(node_id);

CREATE TABLE model_profiles (
    name TEXT PRIMARY KEY,
    reasoner_json TEXT NOT NULL,
    coder_json TEXT NOT NULL,
    swap_cost_seconds REAL CHECK (swap_cost_seconds IS NULL OR swap_cost_seconds >= 0)
);

CREATE TABLE tool_defs (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    family TEXT NOT NULL,
    side_effects TEXT NOT NULL CHECK (side_effects IN ('none', 'writes-outputs')),
    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds > 0)
);

CREATE TABLE commands (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT,
    action TEXT NOT NULL CHECK (action IN ('pause', 'resume', 'cancel_job', 'retry_node', 'approve', 'reject')),
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
CREATE INDEX idx_commands_project_status ON commands(project_id, status);
CREATE INDEX idx_commands_status_created ON commands(status, created_at);

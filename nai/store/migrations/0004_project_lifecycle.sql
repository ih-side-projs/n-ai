-- Add `cancelled` as a project status. SQLite cannot widen a CHECK in place,
-- so rebuild the table. The migration runner disables FK enforcement for this
-- script (see the PRAGMA below) so child rows are preserved when projects is
-- dropped and recreated under the same name.
PRAGMA foreign_keys=OFF;

CREATE TABLE projects_new (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    workspace_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'done', 'cancelled', 'archived')),
    created_at TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO projects_new SELECT * FROM projects;
DROP TABLE projects;
ALTER TABLE projects_new RENAME TO projects;
PRAGMA foreign_key_check;

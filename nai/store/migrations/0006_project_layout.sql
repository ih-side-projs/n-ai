-- Deterministic project layout: stable project/node slugs and an output root.
-- Values are backfilled in Python after the migration (see Store._backfill_layout).
ALTER TABLE projects ADD COLUMN slug TEXT;
ALTER TABLE projects ADD COLUMN output_root TEXT;
ALTER TABLE nodes ADD COLUMN slug TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_output_slug
    ON projects(output_root, slug) WHERE slug IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_sibling_slug
    ON nodes(project_id, parent_id, slug) WHERE parent_id IS NOT NULL AND slug IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_project_slug ON nodes(project_id, slug);

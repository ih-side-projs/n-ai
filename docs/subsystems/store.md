# SS-STORE

SQLite persistence for projects, nodes, jobs, events, artifacts and registry
metadata. Uses only the Python standard library.

## Connections

```python
from nai.store import Store

store = Store(".nai/nai.sqlite3")                  # runner: WAL, migrations, writes
web = Store(".nai/nai.sqlite3", read_only=True)    # web: read-only queries
```

Only the runner holds a writable connection. The web process may call
`enqueue_command()` (a narrow insert) but never mutates nodes or jobs directly.

## Schema and migrations

Migrations live in `nai/store/migrations/` and are applied in numeric order on
open:

- `0001_initial.sql` — projects, nodes, jobs, events, commands, artifacts,
  requirements, tests, approvals, tool_defs, model_profiles, backups.
- `0002_extend_web_actions.sql` — extends command actions for web controls and
  adds tool schema columns.
- `0003_extend_job_kinds.sql` — adds spec-pipeline job kinds.
- `0004_project_lifecycle.sql` — adds `cancelled` project status (rebuilds the
  `projects` table with FK enforcement temporarily disabled so child rows survive).

`schema_migrations` records applied versions. `Store.backup_now()`,
`backup_if_due()` and `backup_keep` provide UTC-daily snapshots.

## Status machine

```
draft → awaiting_approval → approved → ready → running → verifying → done
running|verifying → failed → ready|escalated → ready
any → stale → draft|ready
```

`transition_node_status()` is the only status API and raises `InvalidTransition`
for illegal edges after recording a rejection event.

Project lifecycle statuses are `active`, `paused`, `done`, `cancelled` and
`archived`. `set_project_status()` validates writes; a project-wide cancel moves
`active`/`paused` to `cancelled`, and archive/restore moves between `archived`
and `active`. The runner treats `cancelled` and `archived` as inactive.

## Content hashing

```
content_hash = sha1(spec_text + canonical_json(contract)
                    + hashes of depends_on + parent contract hash)
```

`recompute_project_hashes()` updates every node transactionally and marks
hash-changed nodes and dependents `stale`.

## Useful methods

```python
store.create_project(name, brief="", workspace_path=None, settings=None)
store.get_project(pid) / list_projects(status=None)
store.create_node(pid, name, kind, parent_id=None, objective="", spec_text="", ...)
store.update_node_spec(nid, text) / update_node_contract(nid, contract)
store.replace_requirements(nid, reqs) / upsert_test_record(...)
store.add_approval(nid, stage, decision, note="")
store.create_job(pid, kind, role, node_id=None, attempt=1, input_ref=None)
store.update_job(jid, status=..., tokens_in=..., result_ref=...)
store.enqueue_command(pid, action, node_id=None, payload=None)
store.claim_commands(limit=20) / complete_command(cid, error=None)
store.record_event(pid, type, message, node_id=None, job_id=None, ...)
store.export_project(pid, dest) / import_project(src)
```

Approval stages: `component-map`, `interfaces`, `component-spec`, `escalation`.
Command actions include `pause`, `resume`, `cancel_job`, `retry_node`, `approve`,
`reject`, `cancel`, `rerun_stage`, `reorder`.

Tests: `tests/store/` plus store contract tests in `tests/sched/`.

# SS-WEB

A server-rendered FastAPI dashboard for tracking and steering local projects.
All state changes go through the store's command queue; the web process keeps
read-only store connections (STO-3).

## Running

```bash
NAI_DB_PATH=.nai/nai.sqlite3 \
NAI_WEB_TOKEN=secret \
NAI_WEB_HOST=127.0.0.1 NAI_WEB_PORT=8080 \
nai-web
```

Open <http://127.0.0.1:8080>. OpenAPI docs are at `/api/docs`.

## Pages

| Route | Contents |
|-------|----------|
| `/`, `/projects` | Project cards, status counts, progress, current job, token use |
| `/projects/{id}` | Spec tree, budget/runner summary, pause/resume/cancel |
| `/projects/{id}/nodes/{nid}` | Contract, requirements↔tests traceability, jobs, artifacts, actions |
| `/projects/{id}/nodes/{nid}/edit` | Spec editor plus unified diff against last approval |
| `/approvals` | Pending approvals with approve/reject forms |
| `/escalations` | Escalated and failed nodes with retry / mark-stale |
| `/tools` | Registered tool catalog with schemas and try-it forms |
| `/models` | Model profiles, `/v1/models` + `/running` status, smoke test |
| `/projects/{id}/logs` | Stored events and live SSE stream |
| `/projects/{id}/metrics` | Tokens, wall time, success rate, retry stats, daily trend |
| `/projects/{id}/artifacts` | Registered artifacts and safe workspace browser |

## Controls and approvals

- Approve/reject node specs (records an approval, transitions status).
- Retry failed nodes, re-run a stage, mark a node stale.
- Pause, resume, cancel a project or job, reorder node priorities.
- All state-changing routes are POST-only and enqueue commands where appropriate.

## Editing with diff

The editor loads the last approved spec snapshot from the approval records and
shows a unified diff (`meta`/`delete`/`insert`/`equal` lines) next to the textarea.
Saving recomputes project hashes and records a `node.spec_edited` event.

## Security (WEB-13 / WEB-14)

- CSRF token issued as a cookie and required in form/header for all POSTs.
- Same-origin check on state-changing requests.
- Optional bearer token via `NAI_WEB_TOKEN` (header or cookie).
- Strict Content-Security-Policy, `X-Frame-Options: DENY`, no external CDN assets.
- Workspace file browser rejects absolute paths, `..`, hidden files and symlinks.

## JSON API

`GET /api/projects`, `/api/projects/{id}`, `/api/projects/{id}/nodes/{nid}`,
`logs`, `logs/stream`, `metrics`, `models`, `tools`, `approvals`, `escalations`,
`runner`, `artifacts`, `files`.

Tests: `tests/web/` (read-only v1 behaviour) and `tests/web2/` (v2 controls).

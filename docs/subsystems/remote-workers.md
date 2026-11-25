# Remote workers

`nai/remote/` lets build, test and integration steps run on another machine —
for example a MacBook on the LAN — while Linux stays the only orchestrator. It
writes the store, calls the model and parses results; the worker only executes
*named recipes* from its own `worker.toml` and never sees a model-supplied
command. The remote copy is disposable: it is never edited by the pipeline and
never written back.

## Modules

| Module | Responsibility |
|--------|----------------|
| `recipes.py` | shared recipe registry, parameter kinds, argv rendering, template checks |
| `snapshots.py` | deterministic content-addressed `tar.gz`, pruning, safe extraction |
| `server.py` | worker API (own listener), bearer auth, long-poll claim, TLS/LAN guard |
| `worker.py` | `nai-worker init\|run\|doctor`: config, process control, heartbeat/cancel |
| `target.py` | `RemoteTarget`, an `ExecutionTarget` |
| `routing.py` | `target_for`, `route_for`, `node_platform`, `subtree_has_platform` |
| `cli.py` | `nai worker add\|list\|revoke`, `nai remote serve\|status` |

## Tables

Migration `0008_remote_workers.sql` adds:

| Table | Columns |
|-------|---------|
| `workers` | `id`, `name` (unique), `token_hash`, `capabilities_json`, `toolchains_json`, `last_seen`, `revoked`, `created_at` |
| `remote_tasks` | `id`, `project_id`, `job_id`, `recipe`, `params_json`, `snapshot_sha`, `required_caps`, `status`, `lease_expires_at`, `worker_id`, `redeliveries`, `result_json`, `error`, `created_at`, `updated_at` |

`nai worker add NAME` prints a token **once**, stores only its sha256 digest and
compares with `hmac.compare_digest` over every row, so a revoked worker and a
near-miss token look the same from the outside. `revoke` sets `revoked=1`; the
row is kept for the audit trail.

The worker API is a second writer process, but it only touches these two tables.
A claim is one `BEGIN IMMEDIATE` transaction (lease expiry included), so two
workers polling at once can never receive the same task.

## Worker API

`nai remote serve` runs it on its own listener, separate from the dashboard.
It binds `127.0.0.1` by default; a non-loopback bind needs `--certfile/--keyfile`
or the explicit `--insecure-lan` opt-in (checked before uvicorn starts).

| Endpoint | Purpose |
|----------|---------|
| `POST /worker/v1/heartbeat` | refresh `last_seen`/capabilities, extend the lease, return the cancel flag |
| `POST /worker/v1/claim` | long-poll for a queued task the worker's capabilities satisfy (204 when none) |
| `GET /worker/v1/snapshot/<sha>` | download the content-addressed archive |
| `POST /worker/v1/result` | record exit code, output, duration, `timed_out`, toolchain versions |
| `GET /worker/v1/health` | unauthenticated liveness for `nai-worker doctor` |

Every other endpoint requires `Authorization: Bearer <token>`. The server
re-validates a claimed task's recipe and parameters before handing it out; an
invalid task is failed as an infrastructure error instead of being executed.
Because FastAPI serves sync endpoints from a thread pool, the server owns a
`Store(..., allow_thread_sharing=True)` and serializes calls behind a lock.

## Lifecycle

1. `RemoteTarget.run()` snapshots the workspace, queues a task and waits.
2. A worker claims it, downloads and verifies the snapshot, and runs the recipe.
3. The worker posts the raw result; **parsing stays on Linux** via the adapter.
4. Leases last 60s and are extended by heartbeats. An expired lease requeues the
   task; the third expiry fails it with an `infrastructure:` error.
5. Cancellation is real: `RemoteTarget.cancel()` marks the task cancelled, the
   next heartbeat carries `cancel: true`, and the worker kills the process group.
   A worker that is shut down never posts a "done" result, so the lease expires
   and the task is retried elsewhere.

## Snapshots

A snapshot is a `tar.gz` of the project tree minus the adapter's
`snapshot_ignore` patterns (`.nai`, `.git`, `.venv`, `__pycache__`, `*.pyc`, …).
Identical trees produce byte-identical archives — sorted entries, `mtime=0`,
`uid/gid=0`, normalised modes — so the sha256 of the archive is a content
address. Archives live in `<workspace>/.nai/snapshots` and the last
`keep_snapshots` (default 5) are retained.

Extraction refuses absolute paths, `..` segments, symbolic and hard links,
devices and FIFOs, and enforces member/byte limits, so a hostile archive is a
protocol violation rather than a best-effort case. The worker replaces
`work/projects/<project>` per snapshot but carries the adapter's `cache_dirs`
across, so a virtualenv or pytest cache survives.

## Recipes

`worker.toml` maps a recipe id to an argv template; there is no shell anywhere.
The registry (`recipes.py`) ships with both sides and declares each recipe's
parameters:

| Recipe | Parameters | Capabilities |
|--------|------------|--------------|
| `echo` | `message` (token) | — |
| `toolchain` | — | `python` |
| `build` | `targets` (relpaths) | `python` |
| `lint` | `targets` (relpaths) | `python` |
| `verify` | `targets` (relpaths), `args` (tokens) | `python` |
| `integrate` | `targets` (relpaths) | `python` |
| `swift-build` | — | `swift` |
| `swift-test` | `filter` (token) | `swift` |
| `xcodebuild-test` | `xcode_project` (relpath), `scheme` (token), `destination` (choice) | `swift`, `macos` |

Parameter kinds are `token` (argv-safe characters only), `relpath` (relative,
no `..`), `relpaths`/`tokens` (lists spliced as separate arguments), `int` and
`choice`. Unknown recipes and unknown parameters are rejected on both sides; a
template that starts with a shell is a configuration error. A worker's
`params = [...]` line narrows the shared allowlist (it can never widen it or drop
a required parameter), so a machine can expose less than the registry permits.
The child environment is scrubbed with `nai.exec.scrub_env`, so no secret reaches
a recipe.

```toml
[worker]
name = "macbook"
server = "http://192.168.1.10:8765"
token_file = "/Users/you/.nai-worker/token"
work_dir = "~/.nai-worker"
capabilities = ["macos", "python"]
python = "python3"

[recipes.verify]
params = ["targets"]
argv = ["{python}", "-m", "pytest", "{targets}", "-q"]
timeout = 1800
```

`{python}` and `{workdir}` come from the worker's own configuration, never from
the orchestrator. `nai-worker init` writes a starter file with mode `0600`.

## Routing

`target_for(project, node, job_kind, recipe)` returns the local target or builds
a `RemoteTarget`:

| Condition | Result |
|-----------|--------|
| `settings.remote.enabled` is false | local (a macos node records `remote.disabled`) |
| node platform is `macos` | remote |
| `integrate` job whose subtree has macos | remote |
| `integration` node's commands (its tests exercise the parent's subtree, which has macos) | remote |
| anything else | local |

The platform comes from the node's contract or from the parent contract's
component entry with the same name. For macos components `implement` stays local
but the coder's `run_command` tool is disabled; the handler runs the adapter's
syntax pre-check instead and relies on verify feedback from the worker.

## Running a worker

```bash
# orchestrator
nai worker add macbook --capabilities macos,python   # token shown once
nai remote serve --host 0.0.0.0 --port 8765 --insecure-lan

# macOS
nai-worker init --name macbook --server http://<orchestrator>:8765 --token <token>
nai-worker doctor
nai-worker run
```

`deploy/nai-worker.plist` is a launchd user agent. SIGTERM stops claiming and
kills the running process group; the task's lease then expires and it is
requeued.

## Failure handling

Waiting for a capable worker raises `WorkerUnavailable`; a lost lease, a missing
snapshot or a vanished worker raises `InfrastructureError`. The runner requeues
both **without counting an attempt** (`remote.waiting` / `remote.infrastructure`
events) up to `runner.remote_infra_retries` (default 10), after which the job
fails with an explicit `infrastructure:` error. A remote command that exceeds its
timeout is cancelled and reported as `timed_out` (exit 124).


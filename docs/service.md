# Service / daemon

nai ships a thin supervisor that starts and watches the existing programs. It
does not reimplement their logic — it spawns them as child processes.

## What it starts

| Child | Command | Purpose |
|-------|---------|---------|
| `web` | `python -m nai.web --host … --port …` | local dashboard |
| `runner` | `python -m nai run --db …` | scheduler / runner |
| jobs | `python -m nai <args>` | on-demand instances submitted via the CLI |

The supervisor also opens a Unix-domain control socket at `.nai/service.sock`
and writes logs to `.nai/logs/<name>.log`.

## Quick start

```bash
nai service start                  # detached: web + runner
nai service status                 # children, health, submitted jobs
nai service logs web -n 50         # tail the dashboard log
nai service logs runner            # tail the runner log
nai service stop                   # graceful shutdown of all children
```

To run in the foreground (for systemd, tmux or manual supervision):

```bash
nai service start --foreground
```

## Submitting new nai instances

The service includes a CLI interface for spawning fresh `nai` processes:

```bash
# one-shot submission
nai service exec brief.md -w ./workspace --no-stream

# inspect / cancel
nai service jobs
nai service job job-20261120-120000-abc123
nai service cancel job-20261120-120000-abc123

# interactive shell
nai service shell
nai> status
nai> jobs
nai> brief2.md -w ./ws2      # any non-command line is submitted as `nai ...`
nai> logs job-...            # tail that job's log
nai> exit
```

Submitted jobs get their own log file and are tracked by id. They are not
auto-restarted (a failed build should not silently repeat); use `nai service
exec` again to retry.

## Control protocol

The CLI talks to the supervisor over a newline-delimited JSON protocol on the
local socket. Commands: `status`, `logs`, `stop`, `restart`, `exec`, `jobs`,
`job`, `cancel`. You can use the Python client directly:

```python
from nai.service.protocol import request
from pathlib import Path

print(request(Path(".nai/service.sock"), "status"))
```

Only local processes can connect; the socket is removed on shutdown.

## Configuration

Environment variables (also accepted as CLI flags):

| Variable | Purpose |
|----------|---------|
| `NAI_STATE_DIR` | State/log/socket directory (default `.nai`) |
| `NAI_DB_PATH` | Store path |
| `NAI_WEB_HOST`, `NAI_WEB_PORT` | Dashboard bind address |
| `NAI_PYTHON` | Python interpreter for children (default current) |
| `NAI_SERVICE_NO_RESTART=1` | Disable auto-restart of crashed children |

Restart policy: up to 3 restarts per child with linear backoff (2s, 4s, 6s).
A child that exhausts its budget is retired and reported in `status`.

## Native service integration

See [`deploy/README.md`](../deploy/README.md) for a systemd unit and a launchd
example. When systemd/launchd supervises the daemon itself, run it with
`--foreground` (module entry point `python -m nai.service.daemon`).

## Safety notes

- The supervisor uses `start_new_session=True` and signals the whole process
  group on stop, so command children of the runner are stopped too.
- It never runs arbitrary model-generated code itself; it only spawns the
  approved entry points with the configured environment.
- A lock file (`.nai/service.lock`, `flock`) prevents two supervisors sharing a
  state directory.

Tests: `tests/service/` (uses short-lived fake child processes; no model or
network needed).

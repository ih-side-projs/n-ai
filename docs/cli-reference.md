# CLI reference

## Console scripts

| Script | Module | Purpose |
|--------|--------|---------|
| `nai` | `nai.cli:main` | v1 build CLI + subcommand dispatch |
| `nai run` | `nai.sched.cli:main` | long-running scheduler |
| `nai mcp` | `nai.tools.cli:mcp_main` | MCP server (stdio) |
| `nai-web` | `nai.web.server:main` | local FastAPI dashboard |
| `nai-bench` | `nai.eval.cli:bench_main` | benchmark runner |
| `nai-service` | `nai.service.cli:service_main` | service supervisor control CLI |
| `nai-doctor` | `nai.doctor:main` | offline integration self-check |

Subcommands are registered through `nai.cli.register_subcommand()`; `python -m nai`
uses the same dispatcher as the `nai` script. `python devswarm.py` is the original
v1 entry point.

## `nai doctor` — integration self-check

```bash
nai doctor
nai doctor --json
nai --doctor
python -m nai.doctor
```

Runs offline (no model server required) and checks subsystem imports, CLI
subcommand registration, job-handler/planner coverage against the real
`PlanContext`, artifact kinds, schema artifacts, package data, a temporary
SQLite store migration, and the tool registry. Exits non-zero when a check
fails. Run this first when subsystems pass their own tests but integration
behaves unexpectedly.

## `nai where` / `nai relocate` — project layout

```
nai where <project> [--db PATH] [--json]
nai relocate <project> <output-root> [--db PATH] [--force]
```

`where` resolves a project by id, slug or name and prints its stable slug,
`output_root`, project root and manifest status. `relocate` verifies every
tracked file against `.nai-manifest.json`, then moves the project to
`<output-root>/<slug>` and updates the store. Hand-edited files make
relocation fail unless `--force` explicitly accepts their current hashes.

New projects are created below an output root (precedence: `--output-dir`,
`NAI_OUTPUT_ROOT`, project settings, then the default `outputs/` directory next
to the store). The web form can only choose roots listed in
`NAI_OUTPUT_ROOTS`. Project files live at `<output-root>/<slug>/`; `.nai/`
holds only the database, logs, artifacts and snapshots.

## `nai project create` — Store-backed project

```
nai project create <name> [--brief TEXT|--brief-file FILE] [--output-dir DIR]
                          [--language ID] [--db PATH] [--json]
```

`--language` accepts only registered adapters (run `nai languages` for the
list); one language per project. Omitting it uses `python`.

Creates a project in the store with the deterministic layout
(`<output-root>/<slug>/`, `.nai-project`, `.nai-manifest.json`). The output
root follows the same precedence as `where`/`relocate`; `--brief -` reads the
brief from stdin. Exported projects reconstruct their tree on import:
`SPEC.md`, `contract.json`, `specs/<node-path>/SPEC.md`, source/test files and
a fresh manifest.

## `nai languages` — supported languages and toolchains

```
nai languages [--json] [--no-probe] [--worker NAME=PATH ...]
```

Prints the languages the registry supports (adapter, knowledge pack and
toolchain probe), then probes each declared tool (`python3 --version`,
`pytest --version`, `ruff --version` for Python) on this machine and on every
named worker root. Worker roots can also come from
`NAI_LANGUAGE_WORKERS=name=path,name=path`. A worker root's `.venv/bin` and
`bin` are searched before `PATH`, so a worker checkout's own tools win. The
command exits non-zero when a language is incomplete or a required tool is
missing, which makes it usable as a pre-flight check.

## `nai service` — background service

```
nai service start [--foreground] [--no-web] [--no-runner]
nai service status [--json]
nai service stop
nai service restart [web|runner|job-id]
nai service logs [web|runner|service|job-id] [-n N]
nai service exec [--name ID] <nai arguments>
nai service jobs
nai service cancel ID
nai service shell
```

`start` is detached by default; `--foreground` runs the supervisor in the
current terminal (use this under systemd/launchd). `exec` submits a new
`python -m nai <args>` instance as a tracked job. `shell` is an interactive
submission shell. See [Service / daemon](service.md).

## `nai` — build from a spec

```
nai [SPEC.md|-] [options]
```

Key options:

| Option | Meaning |
|--------|---------|
| `-w, --workspace DIR` | Workspace directory (default `./workspace`) |
| `--config FILE` | TOML config |
| `--profile NAME` | Model profile |
| `--base-url`, `--api-key` | Local model server |
| `--reasoner`, `--coder` | Model id overrides |
| `--ctx`, `--coder-ctx`, `--max-tokens` | Context/token overrides |
| `--docker IMAGE` | Run commands in a persistent, hardened container |
| `--coder-bash` | Give the coder the `run_command` tool |
| `--test-cmd CMD` | Required verification command (repeatable) |
| `--no-gate`, `--resume` | Disable verification / resume saved state |
| `--call-log FILE` | Append every model call as JSONL |
| `--list-models` | Print `/v1/models` and exit |

Exit codes: 0 success, 1 blocked/partial or configuration error, 130 interrupted.

## `nai run` — scheduler

```
nai run [--db PATH] [options]
```

| Option | Meaning |
|--------|---------|
| `--db PATH` | SQLite store (default `.nai/nai.sqlite3`) |
| `--state-dir DIR` | Where `runner.json` and `runner.lock` live |
| `--drain` | Exit when the queue is empty |
| `--until WHEN` | Stop starting jobs after local time `HH:MM` or ISO datetime |
| `--window HH:MM-HH:MM` | Only start jobs in a local-time window (repeatable) |
| `--concurrency N` | Jobs at once (default 1) |
| `--max-consecutive N` | Cap jobs per model before switching |
| `--max-batch-seconds N` | Cap seconds on one model before switching |
| `--poll N` | Idle poll interval |
| `--plugin MODULE` | Plugin module with `register(runner)` (default `nai.spec`, `nai.build`) |
| `--no-call-artifacts` | Do not store full prompts/replies |
| `--backup-dir`, `--backup-keep` | Daily SQLite backups |

Ctrl-C once: finish running jobs then stop. Ctrl-C twice: cancel and requeue.

## `nai mcp` — MCP server

```
nai mcp -w DIR
```

Serves the tool registry over stdio using MCP `initialize`, `tools/list` and
`tools/call`. HTTP transport is not implemented. Tools are sandbox-confined to
the workspace; writes default to `outputs/`.

## `nai-web` — dashboard

Configured entirely through environment variables:

```bash
NAI_DB_PATH=.nai/nai.sqlite3 \
NAI_WEB_TOKEN=secret \
NAI_WEB_HOST=127.0.0.1 NAI_WEB_PORT=8080 \
nai-web
```

Pages: `/`, `/projects/{id}`, node detail and `/nodes/{id}/edit`, `/approvals`,
`/escalations`, `/tools`, `/models`, `/projects/{id}/logs`, `/metrics`,
`/artifacts`. JSON API under `/api/...`; OpenAPI docs at `/api/docs`.

## `nai-bench` — benchmarks

```
nai-bench [SUITE] --profile NAME [options]
```

| Option | Meaning |
|--------|---------|
| `SUITE` | `builtin` (default) or directory of `*.md` specs |
| `--list` | List benchmarks and exit |
| `--reasoner`, `--coder` | Model id overrides |
| `--quantization TEXT` | Recorded in the report |
| `--timeout N` | Seconds per benchmark (default 3600) |
| `--archetype`, `--size` | Filter the suite |
| `--workspace DIR` | Output directory (default `./bench_output`) |
| `--output-format json|markdown|both` | Report format |

Reports include model ids, quantization notes, prompt versions, git commit and
per-benchmark timings.

## Library entry points

```python
from nai.store import Store
from nai.sched import Runner, JobResult
from nai.spec import register as register_spec
from nai.build import register as register_build
from nai.tools import get_registry, Sandbox
from nai.web import create_app, WebSettings
from nai.eval import BenchmarkHarness, generate_report
```

See [API reference](api-reference.md).

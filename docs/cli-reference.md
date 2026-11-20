# CLI reference

## Console scripts

| Script | Module | Purpose |
|--------|--------|---------|
| `nai` | `nai.cli:main` | v1 build CLI + subcommand dispatch |
| `nai run` | `nai.sched.cli:main` | long-running scheduler |
| `nai mcp` | `nai.tools.cli:mcp_main` | MCP server (stdio) |
| `nai-web` | `nai.web.server:main` | local FastAPI dashboard |
| `nai-bench` | `nai.eval.cli:bench_main` | benchmark runner |

Subcommands are registered through `nai.cli.register_subcommand()`; `python -m nai`
uses the same dispatcher as the `nai` script. `python devswarm.py` is the original
v1 entry point.

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
| `--docker IMAGE` | Run commands in a persistent container |
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

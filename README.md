# nai (N-AI)

> Local-first, spec-to-code system powered by locally served LLMs.

[![CI](https://github.com/ih-side-projs/n-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/ih-side-projs/n-ai/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-485%20passed-brightgreen.svg)](#testing)

`nai` turns a rough project brief into a layered specification and then into a
tested codebase, using only local models. Python owns control flow, checks and
context assembly; models are used only for narrow generation and judgment jobs.
No project content ever leaves the machine.

## Contents

- [Why nai?](#why-nai)
- [Features](#features)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Testing](#testing)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Why nai?

- **Build beyond one context window** — specs are decomposed recursively into
  small, frozen interface contracts that a small local coder model can implement.
- **Private by design** — the only runtime network calls are to the configured
  local model server (llama-swap / llama-server).
- **Resumable and inspectable** — every job, event, artifact and model call is
  stored, so a run can be resumed and audited.
- **Deterministic control** — hashing, dependency ordering, size checks and
  traceability are plain Python with unit tests.

See the [full specification](docs/specification.md) for the guiding principles
and requirement IDs (CORE-1, STO-3, SCH-7, SPEC-3, ...).

## Features

| Subsystem | What it provides |
|-----------|------------------|
| **SS-CORE** | LLM client with constrained output, swap detection, path policies, verification gate, git checkpoints, v1 CLI |
| **SS-STORE** | SQLite store with migrations, status machine, content hashing, export/import, backups |
| **SS-SCHED** | Job runner with batching, budgets, crash recovery, scheduling windows, plugin registry |
| **SS-SPEC** | Recursive spec pipeline: decompose, interface checks, component specs, lint, reconcile |
| **SS-BUILD** | Context packs, contract stubs, test authoring, verification, escalation, target environments |
| **SS-TOOLS** | One registry feeding both OpenAI-style tool schemas and an MCP server over stdio |
| **SS-WEB** | Local FastAPI dashboard: project tree, approvals, editing, controls, logs (SSE), metrics, tools |
| **SS-EVAL** | Benchmark suites, scripted stub model server, `nai bench`, JSON/Markdown reports |
| **SS-LANGS** | Language adapters: layout, stubs, commands, result parsing and path policy behind one protocol (Python, Swift, TypeScript, Go, Rust, C, C++) |
| **SS-EXEC** | One execution seam for every command: scrubbed environment, setup/run network phases, resource limits, capped streaming output, real cancellation (local or Docker) |
| **SS-REMOTE** | Pull-based remote workers: named recipes, hashed tokens, deterministic snapshots, leases, real cancellation; macos components build on a Mac over the LAN |
| **Service** | Thin supervisor for web + runner + on-demand `nai` instances, with a CLI and local control socket |

## Architecture

```
Brief → SS-SPEC (approved tree of contracts)
      → SS-BUILD (stubs → tests → code → integration)
      → SS-STORE (all state, events, artifacts)
      → SS-WEB (tracking, approvals, controls)
      → SS-TOOLS (available to jobs via allowlists)
      → SS-EVAL (measures safe unit sizes and regressions)

SS-SCHED runs every model job; SS-CORE supplies the shared client and workspace;
SS-EXEC runs every child process, local or in Docker; SS-REMOTE sends macos
build/test recipes to a worker over the LAN.
```

Control principle: Python decides *which* file, *which* job and *whether* tests
passed. The model never does. Full details: [Architecture](docs/architecture.md).

## Quick start

Requires Python 3.10+ and a local OpenAI-compatible model server (llama-swap).

```bash
git clone https://github.com/ih-side-projs/n-ai.git
cd n-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,web,eval]'

# No model or network needed: run the test suite
pytest -q

# Inspect the CLI
nai --help
nai run --help
nai bench --list

# Point at your local model server, then build from a brief
export LLAMA_SWAP_URL=http://localhost:8080/v1
printf 'Build a small task tracker CLI.\n' > brief.md
nai brief.md -w ./workspace --no-stream
```

See [Quick start](docs/quickstart.md) for a guided walkthrough.

## Installation

```bash
# Core + all extras
pip install -e '.[dev,web,eval]'

# Minimal (core library only)
pip install -e .
```

Optional extras: `dev` (pytest), `web` (FastAPI dashboard), `eval`
(benchmarks), `markdown`, `excel`, `word`, `pdf`, `charts` (tool families).

See [Installation](docs/installation.md) for environment setup and llama-swap
configuration.

## Usage

Console scripts after installation:

| Command | Purpose |
|---------|---------|
| `nai SPEC.md -w DIR` | v1 build from a spec file (still fully supported) |
| `nai run --db PATH` | long-running scheduler / runner |
| `nai mcp -w DIR` | MCP server over stdio for the tool registry |
| `nai-web` | local web dashboard (or `NAI_WEB_PORT=... python -m nai.web`) |
| `nai-bench --profile NAME` | run benchmark suites and write reports |
| `nai service start\|status\|stop` | manage the background service (web + runner) |
| `nai service exec SPEC.md` | submit a new `nai` instance to the service |
| `nai where PROJECT` | show a project's stable slug, output root and manifest status |
| `nai relocate PROJECT DIR` | verify the manifest and move a project to a new output root |
| `nai project create NAME` | create a Store-backed project (`--output-dir`, `--brief`, `--language`, `--json`) |
| `nai languages` | supported languages plus toolchain probes on this machine, a Docker image and named workers |
| `nai doctor` / `nai --doctor` | operability self-check: DB + migrations, model server, output root, per-language toolchains, package data (`--offline`, `--strict`, `--worker NAME=PATH`) |
| `python devswarm.py SPEC.md` | original v1 entry point |

Detailed flags: [CLI reference](docs/cli-reference.md).

## Project layout

State lives in one SQLite store; generated code lives in one directory per
project, so a project can be moved or archived without the store.

```
.nai/nai.sqlite3                  # store: projects, nodes, jobs, events, artifacts
outputs/<slug>/                   # default output root (next to .nai/)
  .nai-project                    # marker: format, version, project_id, slug
  src/ tests/ ...                 # language-specific layout (see Languages)
  .git/                           # job-tagged checkpoints
```

Precedence for the output root is `--output-dir` > `NAI_OUTPUT_ROOT` > project
`settings.output_root` > `<state-dir>/../outputs`. `nai where PROJECT` prints the
resolved root and `nai relocate PROJECT DIR` moves a project after verifying its
marker. Per-language layouts are in [Language adapters](docs/subsystems/languages.md);
the C/C++ adapters also generate `CMakeLists.txt` and vendor their test harnesses.

## Languages

Seven adapters ship today, and the list is derived from the registry rather than
hard-coded (`nai languages` prints it with toolchain versions):

| id | Stack | Tests | Stub |
|----|-------|-------|------|
| `python` | Python 3.12 | pytest | `raise NotImplementedError` |
| `swift` | Swift 6 / SwiftPM | XCTest | `fatalError("not implemented")` |
| `typescript` | Node 20 / TypeScript 5 | vitest | `throw new Error("not implemented")` |
| `go` | Go 1.22 | `go test -json` | `panic("not implemented")` |
| `rust` | Rust 1.75 / Cargo | libtest | `unimplemented!()` |
| `c` | C11 / CMake + CTest | vendored `nai_assert.h` | `NOT_IMPLEMENTED(...)` |
| `cpp` | C++17 / CMake + CTest | doctest | `std::logic_error("not implemented")` |

`project.settings.language` holds the adapter id (one language per project).
Contract components may declare `platform: any | linux | macos` (default `any`);
an `any`/`linux` component may not consume a `macos` component, and portable
components are linted for UI frameworks and Apple-only imports. A `macos`
component builds on a worker instead of the orchestrator.

Toolchains are probed, not assumed: `nai languages` shows `registered` vs
`buildable here`, `nai languages --worker mac=~/w` probes a worker root,
`NAI_DOCKER_IMAGE` moves execution into a container, and `nai doctor` summarises
which languages are ready (a missing toolchain is a warning unless `--strict`).

## Configuration

Configuration precedence: **CLI options > environment > TOML file > built-in defaults**.

```toml
# nai.toml (or $NAI_CONFIG)
default_profile = "default"

[server]
base_url = "http://localhost:8080/v1"
api_key = "none"
timeout = 3600
stream = true

[profiles.default.reasoner]
model_id = "qwen3.6-reasoning"
ctx = 32768
max_tokens = 16384

[profiles.default.coder]
model_id = "qwen3-coder"
ctx = 32768
max_tokens = 16384
repeat_penalty = 1.05

[limits]
max_requirements_per_component = 8
max_public_functions_per_component = 10
max_components_per_level = 7
max_depth = 4

[runner]
concurrency = 1
poll_seconds = 1.0
```

Environment: `LLAMA_SWAP_URL`, `LLAMA_SWAP_KEY`, `REASONER_MODEL`,
`CODER_MODEL`, `NAI_CONFIG`, `NAI_DB_PATH`, `NAI_WEB_TOKEN`, `NAI_WEB_HOST`,
`NAI_WEB_PORT`, `NAI_RUNNER_STATUS_FILE`.

Full reference: [Configuration](docs/configuration.md).

## Remote workers

Build, test and integration steps can run on another machine (a Mac for `macos`
components, or a beefier box) while the orchestrator stays on Linux. The worker
is pull-based: it long-polls the orchestrator, executes only the recipes named in
its own `worker.toml` (argv arrays, never a shell), and reports results back.

```bash
# orchestrator: create a token (printed once; only its sha256 is stored)
nai worker add macbook
nai remote serve --host 127.0.0.1 --port 8765      # loopback by default

# worker machine
nai-worker init --name macbook --server http://orchestrator:8765 --token ...
nai-worker doctor
nai-worker run
```

Every worker call is authenticated with that bearer token, so the transport must
be private. Recommended, in order:

1. **Tailscale / WireGuard** — bind the orchestrator to its tailnet address
   (`--host 100.x.y.z`) and reach it by tailnet name; nothing is exposed to the
   LAN or the internet.
2. **SSH tunnel** — keep the default loopback bind and forward the port from the
   worker: `ssh -N -L 8765:127.0.0.1:8765 orchestrator`.
3. **Plain LAN as a last resort** — a non-loopback bind without TLS is refused
   unless `--insecure-lan` is passed; treat that as a temporary, trusted-network
   only mode.

Snapshots are content-addressed tarballs under `.nai/snapshots` (the last N are
kept) and extraction rejects absolute paths, `..`, links and devices. See
[Remote workers](docs/subsystems/remote-workers.md).

## Testing

The repository ships the integrated test suites of every work package. Tests use
a scripted stub model server — no GPU, no real model and no network are needed.

```bash
pytest -q                      # all tests (485 passed, 1 skipped)
pytest tests/spec/ -q          # one subsystem
pytest -k 'CORE2 or STO3' -q   # by requirement ID
```

Layout: `tests/core`, `tests/store`, `tests/sched`, `tests/spec`, `tests/build`,
`tests/tools`, `tests/web`, `tests/web2`, `tests/test_eval.py`.

See [Testing](docs/testing.md).

## Documentation

Full documentation is in [`docs/`](docs/index.md):

- [Installation](docs/installation.md) · [Quick start](docs/quickstart.md)
- [Architecture](docs/architecture.md) · [Workflow phases](docs/workflow-phases.md) · [Configuration](docs/configuration.md)
- [CLI reference](docs/cli-reference.md) · [API reference](docs/api-reference.md)
- [Service / daemon](docs/service.md) · [Subsystems](docs/subsystems/index.md) · [Testing](docs/testing.md)
- [Troubleshooting](docs/troubleshooting.md) · [FAQ](docs/faq.md)
- [Overall specification](docs/specification.md)

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for development
setup, test expectations and the pull-request process, and the
[Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## Security

This project is local-first and must not make outbound calls except to the
configured local model server and to workers you configure.

Security model, in short:

- **One execution seam.** Every command goes through `nai.exec` as an argv list:
  there is no `shell=True`, no `bash -lc`, and a model-supplied string is never
  interpolated into a shell. The environment is scrubbed to an allowlist (and of
  `*_KEY`/`*_TOKEN` secrets), output is capped, timeouts are enforced and
  cancellation kills the process group.
- **Two network phases.** `setup` (dependency installation) may reach the
  network; `run` (builds and tests) may not — on Linux the run phase gets a
  private network namespace, and when `unshare` is unavailable the weaker mode is
  recorded in the event log and reported by `nai doctor`.
- **Path policy.** File tools are limited to a per-job writable set minus frozen
  globs (`tests/**`). Adapter-owned manifests (`Package.swift`, `Cargo.toml`,
  `CMakeLists.txt`, `package.json`, `go.mod`) are regenerated, not edited, and
  read-only hashes catch shell-side edits of frozen tests.
- **No implicit trust in models.** Structured output is schema-validated with a
  bounded number of repairs; the supported-language list, a project's language
  and the recipes a worker may run are chosen by the operator, never the model.
- **Worker API.** Bearer tokens (printed once, stored as sha256, compared in
  constant time), loopback bind by default, TLS or an explicit `--insecure-lan`
  opt-in for any other interface, allowlisted recipe parameters validated on both
  sides, and snapshots that cannot escape the work directory.
- **Secrets.** Model keys come from the environment or `nai.toml` and are not
  written to the store; artifacts and events record prompts, replies and
  diagnostics, not credentials.

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

Distributed under the MIT License. See [LICENSE](LICENSE).

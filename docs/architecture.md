# Architecture

## System view

```
                       ┌───────────────────────┐
   owner brief ───────► │ SS-WEB / CLI (v1)     │
                       └──────────┬────────────┘
                                  │ commands / spec
                       ┌──────────▼────────────┐
                       │   SS-STORE (SQLite)   │◄────── project state,
                       └──────────┬────────────┘       jobs, events, artifacts
                                  │
                       ┌──────────▼────────────┐
                       │  SS-SCHED (runner)    │
                       │  budgets, plugins     │
                       └───────┬───────┬───────┘
                               │       │
              ┌────────────────▼──┐  ┌─▼────────────────┐
              │ SS-SPEC jobs      │  │ SS-BUILD jobs    │
              │ decompose/lint/…  │  │ stubs/tests/…    │
              └────────────────┬──┘  └─┬────────────────┘
                               └──────┬┘
                       ┌──────────────▼────────────┐
                       │   SS-CORE (LLM client,    │
                       │   workspace, gate, git)   │
                       └──────────────┬────────────┘
                                      │ allowlisted tools
                       ┌──────────────▼────────────┐
                       │ SS-TOOLS (registry, MCP)  │
                       └───────────────────────────┘

                       ┌───────────────────────────┐
                       │ SS-EVAL (benchmarks)      │
                       └───────────────────────────┘
```

## Control principle

Python owns control flow and state. A model never decides which file to read,
which job runs next, or whether tests passed. Models are called only for narrow
jobs with schemas and validation (X1, X2).

## Service supervisor

For unattended operation, `nai service` runs a thin supervisor that spawns and
watches the existing entry points as child processes — it contains no pipeline
logic of its own:

```
nai service start
├── python -m nai.web --host ... --port ...     (dashboard)
├── python -m nai run --db ...                  (runner)
└── python -m nai <args>                        (submitted jobs, on demand)
    ↑ control: Unix socket .nai/service.sock (JSON lines)
```

It provides process lifecycle (start/stop/restart with backoff), per-child logs,
graceful shutdown of process groups, a single-instance lock, and a CLI plus
interactive shell for submitting new `nai` instances. See [Service](service.md).

## Lifecycle

1. **Intake** — a brief is stored as a project; the root spec node is created.
2. **Specification** — stages 1–11 produce a component map, interfaces and leaf
   specs. Each level is approved by the owner before recursion (WEB-4).
3. **Build** — each leaf gets a contract stub, frozen verification tests, an
   implementation, integration and a verification gate.
4. **Escalation** — repeated failures produce an escalation packet for the owner.
5. **Measurement** — benchmarks tune X9 limits and catch regressions.

## Cross-cutting rules

- **X1 Structured output**: JSON schema → grammar → validate-and-repair; at most
  three repair rounds; every job records prompt version and call metadata.
- **X3 Tool results** are strings; errors start with `ERROR:`.
- **X4 Resumability**: jobs left `running` after a crash go back to `queued` on
  runner startup (`SCH-7`); orphan nodes are reported, not silently changed.
- **X5 Budgets**: attempts, wall-clock and tokens; exceeding them fails or
  escalates a node rather than looping.
- **X6 Logging**: full prompts/responses stored as call artifacts; `/running`
  endpoint used for model swap detection with a latency fallback.
- **X7 Config**: CLI > environment > TOML > built-in defaults; no secrets in the store.
- **X8 Testing**: every subsystem has tests, all runnable offline with a stub.
- **X9 Size limits**: configured, not hard-coded; EVAL-4 tunes them.
- **X10 Prompts**: versioned text files under `nai/spec/prompts/` and
  `nai/build/prompts/`, recorded on each job.

## Store ownership

Only the runner uses a writable `Store`. The web process opens read-only
connections; its only mutation is `enqueue_command()`, a narrow insert into the
`commands` table (STO-3). The runner claims and executes commands, then records
events. This keeps a single writer and makes every state change auditable.

## Status machine

```
draft → awaiting_approval → approved → ready → running → verifying → done
                                         ↑         │          │
                                         └──── failed ────────┘
                                                    ↓
                                                escalated → ready
any status → stale → draft | ready
```

`done` snapshots the current content hash into `built_hash`. A changed hash
marks the node and its dependents `stale`.

Next: [Subsystems](subsystems/index.md).

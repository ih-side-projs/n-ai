# SS-BUILD

Turns an approved leaf component spec into tested code, with context packs,
contract stubs, frozen tests, verification, integration and a failure ladder.

## Modules

| Module | Responsibility |
|--------|----------------|
| `context_pack.py` | Assemble deterministic, bounded context for a job |
| `stubs.py` | Generate contract stubs from the interface contract |
| `jobs.py` | Build job handlers (author tests, implement, verify, integrate, diagnose) |
| `tenv.py` | Target environment (TENV-1..7), lint gate, API notes, stale API detection |
| `traceability.py` | Requirement → frozen test traceability |
| `escalation.py` | Escalation packets, redaction, change requests |
| `tools_bridge.py` | Allowlist enforcement for tool calls from build jobs |
| `plugin.py` | `register(runner)` and `plan(ctx)` |

## Flow

```
context pack → contract stub → frozen verification tests → implementation
             → run tests → verify gate → integration → parent node
                    │
                    └─ failures → diagnose → retry (attempts/budgets)
                                  → escalation packet after the ladder
```

## Target environment

`tenv.py` builds a target-environment block from project settings (language,
dependencies, pinned versions, test commands) and enforces:

- warnings as errors,
- a lint gate,
- locally available packages only (no runtime network access),
- API notes injected into prompts to reduce stale API usage.

Escalation packets are redacted before storage and can raise a change request
when the spec, contract or tests are wrong rather than the implementation.

## Tools in build jobs

`tools_bridge.py` reads the allowlist from the job's context pack. Tools outside
the list are not sent to the model and are refused if called (TLS-3). The two
meta-tools `list_tools` and `describe_tool` are available only when explicitly
allowlisted (TLS-4).

## Prompts

Versioned files live in `nai/build/prompts/`. Each job records the prompt id
(e.g. `implement@1`) in its metadata.

Tests: `tests/build/` (context pack, stubs, tenv, traceability, escalation,
failure ladder, structured output).

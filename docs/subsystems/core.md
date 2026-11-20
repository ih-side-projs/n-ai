# SS-CORE

The reusable base of nai: a refactor of the original `devswarm` program into
modules, plus the CORE-1..8 extensions.

## Modules

| Module | Responsibility |
|--------|----------------|
| `llm.py` | One chat-completion call; `response_format`/`grammar`, usage, swap detection, call observers, truncation by finish reason |
| `models.py` | `ModelProfile`, `RoleConfig`, `ModelClient` — model ids and sampling chosen by role, never hard-coded |
| `config.py` | TOML + environment configuration and X9 size limits |
| `terminal.py` | Colored log lines, transcript, thread-aware `log_context()` and sinks, streaming renderer |
| `workspace.py` | Workspace file operations through a per-job path policy |
| `policy.py` | `PathPolicy` and `JobContext` |
| `gate.py` | Per-node verification gate |
| `gitcp.py` | Job-tagged git checkpoints with find/restore |
| `runner.py` | Local and Docker command execution with timeout/cancel |
| `coder.py`, `consult.py`, `orchestrator.py`, `session.py` | v1 agents, now via `ModelClient` |
| `promptstore.py`, `prompts/` | Versioned prompt files (X10) |
| `testing/stub_server.py` | Scripted OpenAI-compatible stub for tests |
| `cli.py` | v1 CLI + `register_subcommand()` hook |

## Model calls

```python
from nai.llm import LLM, json_schema_format, JsonlCallLog
from nai.models import ModelClient, default_profile

llm = LLM("http://127.0.0.1:8080/v1", "none", stream=True)
llm.observers.append(JsonlCallLog(Path("calls.jsonl")))
client = ModelClient(llm, default_profile())
res = client.chat("reasoner", messages, response_format=json_schema_format(schema),
                  tag="decompose", meta={"job_id": "j1", "prompts": ["decompose@1"]})
```

`res.message.content` is the reply text; `res.message.reasoning` holds streamed
reasoning when llama-server separates it. Swap detection prefers llama-swap's
`GET /running` and falls back to first-token latency.

## Path policy

```python
policy = PathPolicy(writable=("src/**",), readonly=("tests/**",))
with ws.job_context(JobContext(node_id="n1", job_kind="implement", policy=policy)):
    ws.call("write_file", {"path": "tests/x.py", "content": "..."})
    # ERROR: path not writable for this job: tests/x.py
```

Reads are unrestricted; a write needs a writable match and no read-only match.
The policy guards file tools only — shell commands can still write anywhere, so
`protected_hashes()` is used to detect changes to frozen tests.

## Verification gate

A node's pass is recorded against its test command and the workspace fingerprint.
Changing any file invalidates the gate (conservative). `python devswarm.py SPEC.md`
still runs the whole v1 flow unchanged.

Tests: `tests/core/` (201+ tests, including S1–S6 scenarios through both entry points).

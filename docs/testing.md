# Testing

nai follows X8: every subsystem ships tests that run offline with no real model.
The suite uses scripted stub servers (`nai.testing.stub_server` and
`nai.eval.stub_server`).

## Run everything

```bash
source .venv/bin/activate
pytest -q
# All offline suites, including CLI and web end-to-end workflows
```

## Layout

| Path | Covers |
|------|--------|
| `tests/core/` | LLM client, config, workspace, gate, git checkpoints, CLI, S1–S6 scenarios |
| `tests/store/` | Schema/migrations, status machine, commands/events, hashing, export/import |
| `tests/sched/` | Runner, budgets, commands, lifecycle, crash recovery, store contract |
| `tests/spec/` | Checkers, schemas, structured-output fallbacks, scenario step fields |
| `tests/build/` | Context packs, stubs, tenv, traceability, escalation, failure ladder |
| `tests/tools/` | Registry, sandbox, data-files and project-introspection tools |
| `tests/exec/` | Execution targets: env scrubbing, phases, limits, streaming, cancellation (fake docker daemon) |
| `tests/remote/` | Remote workers: store leases/claims, snapshots, recipes, API, `RemoteTarget`, routing, CLI, `/workers`, and one real `nai-worker` process |
| `tests/web/` | Dashboard v1 and v2 (TestClient + httpx ASGITransport) |
| `tests/e2e/` | Full CLI and web workflows against the real store, runner, command processor and stub LLM |
| `tests/test_end_to_end.py` | In-process brief → spec → build → done pipeline |
| `tests/test_eval.py` | Benchmark harness, stub server, reports |

Each directory has its own `conftest.py` with fixtures and environment isolation,
so test order does not leak configuration between subsystems.

Run the suite from an activated virtualenv (or with `.venv/bin` on `PATH`):
scenario tests invoke `python` by name through the execution target, so a bare
`.venv/bin/python -m pytest` without the venv on `PATH` fails those steps with
`python: command not found`.

## Useful invocations

```bash
pytest tests/spec/ -q                       # one subsystem
pytest tests/e2e/ -q                        # CLI + web end-to-end workflows
pytest -k 'CORE2 or STO3' -q                # by requirement id in test names
pytest -x -q                                # stop at first failure
pytest --tb=short -q                        # compact failures
python -m compileall -q nai                 # syntax check
```

## Stub server pattern

```python
from nai.testing.stub_server import StubServer, reply, call

with StubServer() as s:
    s.orch.reply(reply(calls=[call("finish", status="success", summary="done")]))
    # run code with LLAMA_SWAP_URL=s.url
    assert s.orch.calls[0]["messages"] ...
```

`StubModelServer` in `nai.eval` adds scripted responses, configurable delay and
error injection, and a call history for MCP/benchmark tests.

## Conventions for new tests

- No network, no GPU, no real model.
- Use `tmp_path` for files and databases; set explicit UTF-8 when reading/writing.
- Name tests after requirement IDs where relevant (`test_CORE2_*`, `test_STO3_*`).
- Test both success and failure paths; tools must return `ERROR:` strings, not raise.
- For structured output, test the D9 fallback chain with a fake client.
- For web, use `httpx.AsyncClient(transport=ASGITransport(app=app))` and CSRF tokens.

## Continuous integration

`.github/workflows/ci.yml` runs on Python 3.10, 3.11 and 3.12 for pushes and
pull requests:

1. install `.[dev,web,eval]`,
2. `python -m compileall -q nai`,
3. `pytest -q`,
4. regenerate schemas and verify no diff (single source of truth).

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the development workflow.

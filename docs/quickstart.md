# Quick start

This walkthrough uses no real model until the last step.

## 1. Run the tests

```bash
cd n-ai
source .venv/bin/activate
pytest -q
```

## 2. Inspect the available commands

```bash
nai --help                 # v1 build CLI
nai run --help             # scheduler
nai bench --list           # built-in benchmarks
nai-web --help             # dashboard (or use NAI_WEB_PORT env)
```

## 3. Explore the store without a model

```python
from nai.store import Store
from pathlib import Path

store = Store(Path("/tmp/demo.sqlite3"))
pid = store.create_project("Demo", "A tiny task tracker")
nid = store.create_node(pid, "tasks", "component", objective="Track tasks with add/list/done")
print(store.get_project(pid))
print(store.list_nodes(pid))
store.close()
```

The store applies numbered migrations on open and enforces the node status
machine (draft → awaiting_approval → approved → ready → running → …).

## 4. Run the scheduler against the stub server

The tests contain a ready-made pattern; in your own code:

```python
from nai.eval.stub_server import StubModelServer, StubServerConfig
from nai.sched import Runner

server = StubModelServer(StubServerConfig(models=["reasoner", "coder"]))
app = server.create_app()
# Serve `app` on 127.0.0.1 with uvicorn, then point LLAMA_SWAP_URL at it.
```

`StubModelServer` supports scripted responses and records every call, so job
behaviour can be verified deterministically.

## 5. Build from a real brief (needs a local model)

```bash
export LLAMA_SWAP_URL=http://127.0.0.1:8080/v1
export REASONER_MODEL=qwen3.6-reasoning
export CODER_MODEL=qwen3-coder

printf 'Build a small task tracker CLI with add/list/done.\n' > brief.md
nai brief.md -w ./workspace --call-log calls.jsonl
```

The v1 CLI writes `SPEC.md` into the workspace and streams orchestrator
activity. `--call-log` stores full prompts and responses for replay.

For the pipeline form, start the runner and use the web dashboard to approve
each specification level:

```bash
nai run --db .nai/nai.sqlite3
NAI_DB_PATH=.nai/nai.sqlite3 nai-web
```

Open <http://127.0.0.1:8080>. The runner is the only writer to the store;
the web UI enqueues commands (approve, reject, retry, pause, …).

## 6. Measure

```bash
nai-bench --profile default --list
nai-bench --profile default --archetype cli --size small \
  --workspace ./bench_output --quantization q4
```

Reports land in `bench_output/reports/` as JSON and Markdown, including the
git commit, model ids and prompt versions.

Next: [Architecture](architecture.md), [Configuration](configuration.md),
[CLI reference](cli-reference.md).

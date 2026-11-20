# API reference

Public interfaces by subsystem. Each subsystem page has more detail and examples.

## Core (`nai`)

```python
from nai.llm import LLM, ChatResult, Usage, CallRecord, JsonlCallLog, json_schema_format
from nai.models import ModelProfile, RoleConfig, ModelClient, default_profile
from nai.config import Config, ServerConfig, SizeLimits, load_config
from nai.workspace import Workspace
from nai.policy import PathPolicy, JobContext
from nai.gate import VerificationGate
from nai.gitcp import GitCheckpoint
from nai.promptstore import get_prompt

llm = LLM(base_url, api_key, stream=True, show_thinking=True, swap_threshold=10.0)
client = ModelClient(llm, profile)
res = client.chat("reasoner", messages, response_format=json_schema_format(schema))
msg, usage, seconds = res          # v1-style unpacking still works
```

`ChatResult` carries `swapped`, `swap_method`, `first_token_seconds` and
`finish_reason`. Streamed reasoning is captured in `msg.reasoning`.

## Store (`nai.store`)

```python
from nai.store import Store, InvalidTransition, content_hash, canonical_json

store = Store(".nai/nai.sqlite3")                 # writable (runner)
web   = Store(".nai/nai.sqlite3", read_only=True)  # read-only (dashboard)

pid = store.create_project("Demo", "brief", workspace_path="/tmp/demo")
nid = store.create_node(pid, "leaf", "component", objective="...")
store.transition_node_status(nid, "awaiting_approval")
j = store.create_job(pid, "implement", "coder", node_id=nid)
store.update_job(j, status="running")
store.enqueue_command(pid, "approve", node_id=nid, payload={"stage": "interfaces"})
```

See [Store subsystem](subsystems/store.md).

## Scheduler (`nai.sched`)

```python
from nai.sched import Runner, SchedConfig, HandlerRegistry, JobResult, read_health

class Handler:
    def run(self, ctx):
        ctx.check()
        return JobResult.success(text="done")

runner = Runner(store, llm, sched=SchedConfig(concurrency=1))
runner.registry.register("custom", Handler(), uses_model=False)
stats = runner.run(drain=True)
```

`RunContext` provides `check()`, `event()`, `artifact()` and access to
`node`, `project`, `job`, `store`, `client`, `ws`.

## Spec pipeline (`nai.spec`)

```python
from nai.spec import register
from nai.spec.schemas import get_schema, write_schemas_to_disk
from nai.spec.checkers import check_interfaces, lint_spec

findings = check_interfaces(contract)   # explicit caller/callee/call/fields steps
```

Schemas live in `schemas.py` (source of truth); regenerate JSON files with
`python -m nai.spec.schemas`.

## Build pipeline (`nai.build`)

```python
from nai.build import (
    assemble_context_pack, generate_contract_stubs, check_traceability,
    build_escalation_packet, plan, register,
)
```

## Tools (`nai.tools`)

```python
from nai.tools import get_registry, Sandbox, MCPServer

registry = get_registry()
schemas = registry.get_openai_schemas(allowlist=["read_csv"])
result = registry.call_tool("read_csv", {"path": "data.csv"})
```

Register a tool:

```python
from nai.tools.registry import register_tool

@register_tool(name="add", family="math", description="Add two numbers")
def add(x: int, y: int) -> str:
    return str(x + y)
```

## Web (`nai.web`)

```python
from nai.web import create_app, WebSettings

settings = WebSettings.from_env(".nai/nai.sqlite3")
app = create_app(settings=settings)
```

The app is a standard FastAPI application; use `httpx.AsyncClient` with
`ASGITransport` in tests.

## Evaluation (`nai.eval`)

```python
from nai.eval import (
    BenchmarkHarness, BenchmarkSuite, load_benchmark_suite,
    StubModelServer, StubServerConfig, generate_report, write_report,
)

server = StubModelServer(StubServerConfig(models=["r", "c"]))
app = server.create_app()
report = generate_report(results, "default", "qwen", "q4", "builtin")
write_report(report, Path("reports"))
```

## Requirement IDs

Requirement IDs (`CORE-2`, `STO-3`, `SCH-7`, `SPEC-3`, `BLD-1`, `TLS-1`,
`WEB-4`, `EVAL-6`, `X9`, …) are stable. See the
[overall specification](specification.md) and grep the source/tests for an ID
to find its implementation and coverage.

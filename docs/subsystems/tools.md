# SS-TOOLS

One tool definition feeds both the orchestration code and an MCP server.

## Registry

```python
from nai.tools import get_registry, Sandbox
from nai.tools.registry import register_tool

@register_tool(
    name="read_csv",
    family="data_files",
    description="Read a CSV file and return its contents as JSON.",
    side_effects=False,
    timeout_seconds=10.0,
    returns_description="JSON array of rows or ERROR:",
)
def read_csv(path: str, max_rows: int = 1000) -> str:
    ...

registry = get_registry()
openai_tools = registry.get_openai_schemas(allowlist=["read_csv"])
mcp_tools = registry.get_mcp_schemas()
result = registry.call_tool("read_csv", {"path": "data.csv"})
```

The decorator builds a JSON Schema from type hints and docstrings. The registry
also provides `list_tools(family=None)` and `describe_tool(name)` meta-tools for
small-model discovery.

## Error format (X3)

Every tool returns a short human-readable string. Failures start with `ERROR:`
and never raise out of the tool layer. File-producing tools return the relative
path and byte count.

## Sandbox (TLS-5)

```python
sandbox = Sandbox(Path("/project"), writable_dirs=[], max_output_bytes=1_000_000)
path = sandbox.validate_path("outputs/result.csv", write=True)
sandbox.write_file("outputs/result.csv", content)
sandbox.run_with_timeout(fn, timeout=5)
sandbox.limit_output(text)
```

- Writes are confined to allowed directories; `outputs/` is always writable.
- Path escapes are refused with `ERROR: path not writable: ...`.
- Per-tool timeouts and output-size limits are enforced; no network access is provided.

## Families

Built-in stdlib families:

| Family | Tools |
|--------|-------|
| `data_files` | `read_csv`, `read_json`, `write_json`, `diff_text`, `zip_files`, `unzip_files` |
| `project_introspection` | `list_spec_nodes`, `read_contract_stub`, `read_requirements` |
| `meta` | `list_tools`, `describe_tool` |

Optional families (markdown, Excel, Word, PDF, charts) are declared as extras and
report `ERROR: dependency not installed: <name>` when missing.

## MCP server

```bash
nai mcp -w /path/to/workspace
```

Implements `initialize`, `tools/list` and `tools/call` over stdio. The web tools
page reads tool definitions registered in the store (`tool_defs`) and offers a
try-it form that proxies to the MCP layer.

Adding a tool requires one module plus tests — no other code changes (TLS-8).

Tests: `tests/tools/`.

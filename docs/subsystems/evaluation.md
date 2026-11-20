# SS-EVAL

Measures whether changes to prompts, models or limits actually help, and finds
safe unit sizes (X9 / EVAL-4).

## Components

| Module | Purpose |
|--------|---------|
| `benchmarks.py` | `BenchmarkSpec`, `BenchmarkSuite`, suite loading from `*.md`, archetypes and size grades |
| `stub_server.py` | Scripted OpenAI-compatible FastAPI server with call history and error injection |
| `harness.py` | `BenchmarkHarness` runs specs against a profile and collects metrics |
| `reports.py` | JSON and Markdown reports with model/quantization/prompt/git metadata |
| `cli.py` | `nai-bench` command |

## Built-in benchmarks

| Id | Archetype | Size |
|----|-----------|------|
| `devswarm_self_hosting` | cli | large |
| `simple_library` | library | small |
| `cli_tool` | cli | medium |
| `web_api` | web_service | medium |
| `data_pipeline` | data_pipeline | medium |
| `ai_feature` | ai_feature | large |

Custom suites are directories of Markdown files:

```markdown
# My benchmark
Description: A small CLI with three commands
Archetype: cli
Size: small
```

## Running benchmarks

```bash
nai-bench --list
nai-bench --profile default --archetype cli --size small --quantization q4 \
  --workspace ./bench_output --output-format both
```

The harness invokes `nai BENCH.md -w WORKSPACE --profile ...` per spec, records
timing and success, collects `.nai/metrics.json` and artifacts, then writes
`bench_output/reports/benchmark_report_*.{json,md}`.

## Stub server

```python
from nai.eval import StubModelServer, StubServerConfig

config = StubServerConfig(models=["r", "c"], delay_range=(0.01, 0.05), error_rate=0.0)
server = StubModelServer(config)
server.add_scripted_response({...OpenAI completion...})
app = server.create_app()
```

Endpoints: `/v1/models`, `/v1/chat/completions`, `/running`, `/health`.
`server.get_call_history()` allows assertions about prompts, tools and response
formats. This is what makes all subsystem tests model-free.

## Reports (EVAL-7)

Reports include:

- generated timestamp, nai version and git commit,
- model profile, model id, quantization notes,
- benchmark suite name and per-benchmark results (success, error, duration),
- aggregate totals and success rate.

## Size-ceiling experiments

Run graded sizes (e.g. 3/6/10 requirements) and compare first-pass success and
average retries across profiles. Store the recommended X9 limits with the
profile in your config; EVAL-4 closes the loop.

Tests: `tests/test_eval.py`.

# nai.eval: SS-EVAL (Work Package WP9)

The evaluation harness and benchmark specs for nai (N-AI).

## Overview

This package implements **SS-EVAL** (Section 13 of the overall specification):

- **EVAL-1**: Benchmark specs — graded sizes, archetypes (library, cli, web_service, data_pipeline, ai_feature)
- **EVAL-2**: Stub model server — deterministic OpenAI-compatible server for testing
- **EVAL-3**: Hold-out tests — separate test set for validation
- **EVAL-4**: Size-ceiling experiments — measure performance vs. spec size
- **EVAL-5**: Regression runs — compare against baseline
- **EVAL-6**: `nai bench` command — run benchmark suites
- **EVAL-7**: Reports — JSON and Markdown with model info, prompt versions, git commit

## Installation

```bash
pip install -e .[dev]          # with test dependencies
pip install -e .[eval]         # with evaluation dependencies (fastapi, pydantic)
```

## Usage

### As a Library

```python
from nai.eval import BenchmarkHarness, BenchmarkResult, run_benchmark
from nai.eval.benchmarks import load_benchmark_suite
from nai.eval.stub_server import StubModelServer, StubServerConfig
from nai.eval.reports import generate_report, write_report

# Run a single benchmark
result = run_benchmark(
    benchmark_path="benchmarks/my_spec.md",
    model_profile="default",
    output_dir="./bench_output",
    model_id="qwen3-coder",
    quantization="q4"
)

# Run a benchmark suite
harness = BenchmarkHarness(Path("benchmarks"), Path("./bench_output"))
results = harness.run_suite(
    benchmark_paths, 
    model_profile="default",
    model_id="qwen3-coder"
)

# Generate report
report = generate_report(results, ...)
write_report(report, Path("./reports"))
```

### CLI Command

```bash
# Run built-in benchmarks
nai bench --profile default

# Run custom benchmark suite
nai bench ./my_benchmarks --profile default

# Filter benchmarks
nai bench --archetype cli --size small --profile default

# List available benchmarks
nai bench --list
```

## Benchmark Specifications (EVAL-1)

Benchmarks are Markdown files with metadata:

```markdown
# Benchmark Name
Description: A brief description
Archetype: cli          # library, cli, web_service, data_pipeline, ai_feature
Size: small             # small, medium, large
```

The harness infers archetype and size from content if not specified.

### Built-in Benchmarks

| ID | Name | Archetype | Size | Description |
|---|---|---|---|---|
| devswarm_self_hosting | DevSwarm Self-Hosting | cli | large | nai building nai |
| simple_library | Simple Library | library | small | Small Python library |
| cli_tool | CLI Tool | cli | medium | Command-line tool |
| web_api | Web API Service | web_service | medium | FastAPI service |
| data_pipeline | Data Pipeline | data_pipeline | medium | ETL pipeline |
| ai_feature | AI Feature | ai_feature | large | LLM-powered feature |

## Stub Model Server (EVAL-2)

Deterministic OpenAI-compatible server for testing:

```python
from nai.eval.stub_server import StubModelServer, StubServerConfig

config = StubServerConfig(
    models=["test-reasoner", "test-coder"],
    delay_range=(0.01, 0.1),
    error_rate=0.0
)

server = StubModelServer(config)
server.add_scripted_response({
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "scripted response"},
        "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
})

app = server.create_app()  # FastAPI app
```

## Reports (EVAL-6, EVAL-7)

```python
from nai.eval.reports import generate_report, write_report

report = generate_report(
    results=results,
    model_profile="default",
    model_id="qwen3-coder",
    quantization="q4",
    benchmark_suite="my_suite",
    nai_version="2.0.0",
    git_commit="abc123"
)

json_path, md_path = write_report(report, Path("./reports"), "both")
```

Reports include:
- Model profile, ID, quantization notes
- Git commit of nai
- Prompt versions (X10)
- Per-benchmark results with timing
- Aggregate statistics (success rate, duration)

## Testing

```bash
pytest tests/test_eval.py -v
```

## Requirements

- Python 3.10+
- fastapi, pydantic (for stub server)
- nai (core package)
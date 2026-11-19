"""Benchmark specifications for WP9 (SS-EVAL)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BenchmarkSpec:
    """Specification for a single benchmark."""
    id: str
    name: str
    description: str
    archetype: str  # library, cli, web_service, data_pipeline, ai_feature
    size_grade: str  # small, medium, large
    spec_path: Path
    expected_files: list[str] = field(default_factory=list)
    expected_tests: list[str] = field(default_factory=list)
    complexity_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkSuite:
    """A collection of benchmarks."""
    name: str
    description: str
    benchmarks: list[BenchmarkSpec] = field(default_factory=list)
    
    def add_benchmark(self, spec: BenchmarkSpec) -> None:
        self.benchmarks.append(spec)
    
    def filter(self, archetype: str | None = None, size_grade: str | None = None) -> "BenchmarkSuite":
        filtered = BenchmarkSuite(name=self.name, description=self.description)
        for b in self.benchmarks:
            if archetype and b.archetype != archetype:
                continue
            if size_grade and b.size_grade != size_grade:
                continue
            filtered.add_benchmark(b)
        return filtered
    
    def __len__(self) -> int:
        return len(self.benchmarks)
    
    def __iter__(self):
        return iter(self.benchmarks)


def load_benchmark_suite(suite_dir: str | Path) -> BenchmarkSuite:
    """Load a benchmark suite from a directory of spec files."""
    suite_dir = Path(suite_dir)
    suite = BenchmarkSuite(
        name=suite_dir.name,
        description=f"Benchmarks from {suite_dir}"
    )
    
    for spec_file in sorted(suite_dir.glob("*.md")):
        # Parse metadata from spec file
        content = spec_file.read_text()
        benchmark = _parse_benchmark_spec(spec_file, content)
        suite.add_benchmark(benchmark)
    
    return suite


def _parse_benchmark_spec(spec_path: Path, content: str) -> BenchmarkSpec:
    """Parse benchmark metadata from spec file."""
    lines = content.split("\n")
    
    # Extract basic metadata
    name = spec_path.stem
    description = ""
    archetype = "library"
    size_grade = "medium"
    
    for line in lines[:20]:  # Check first 20 lines for metadata
        if line.startswith("# "):
            name = line[2:].strip()
        elif line.startswith("Archetype:"):
            archetype = line.split(":", 1)[1].strip()
        elif line.startswith("Size:"):
            size_grade = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            description = line.split(":", 1)[1].strip()
    
    # Infer size from spec length
    if len(content) < 2000:
        size_grade = "small"
    elif len(content) > 10000:
        size_grade = "large"
    
    # Infer archetype from keywords
    archetype_keywords = {
        "cli": ["cli", "command", "argument", "terminal"],
        "web_service": ["web", "http", "api", "server", "fastapi", "flask"],
        "data_pipeline": ["pipeline", "etl", "data", "transform", "csv", "json"],
        "ai_feature": ["llm", "model", "prompt", "embedding", "vector", "rag"],
        "library": ["class", "function", "module", "api", "library"],
    }
    
    content_lower = content.lower()
    for arch, keywords in archetype_keywords.items():
        if any(k in content_lower for k in keywords):
            archetype = arch
            break
    
    # Calculate complexity score
    complexity = len(content) / 1000  # base complexity
    complexity += content_lower.count("class") * 0.5
    complexity += content_lower.count("function") * 0.3
    complexity += content_lower.count("async") * 0.2
    complexity += content_lower.count("test") * 0.1
    
    return BenchmarkSpec(
        id=spec_path.stem,
        name=name,
        description=description,
        archetype=archetype,
        size_grade=size_grade,
        spec_path=spec_path,
        complexity_score=round(complexity, 2),
    )


# Built-in benchmark suite (EVAL-1)
BUILTIN_BENCHMARKS = [
    {
        "id": "devswarm_self_hosting",
        "name": "DevSwarm Self-Hosting",
        "description": "The original devswarm spec - nai building nai",
        "archetype": "cli",
        "size_grade": "large",
    },
    {
        "id": "simple_library",
        "name": "Simple Library",
        "description": "A small Python library with a few classes",
        "archetype": "library",
        "size_grade": "small",
    },
    {
        "id": "cli_tool",
        "name": "CLI Tool",
        "description": "A command-line tool with argument parsing",
        "archetype": "cli",
        "size_grade": "medium",
    },
    {
        "id": "web_api",
        "name": "Web API Service",
        "description": "A FastAPI web service with endpoints",
        "archetype": "web_service",
        "size_grade": "medium",
    },
    {
        "id": "data_pipeline",
        "name": "Data Pipeline",
        "description": "An ETL pipeline for CSV/JSON data",
        "archetype": "data_pipeline",
        "size_grade": "medium",
    },
    {
        "id": "ai_feature",
        "name": "AI-Powered Feature",
        "description": "A feature using LLM calls and embeddings",
        "archetype": "ai_feature",
        "size_grade": "large",
    },
]
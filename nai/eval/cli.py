"""CLI for `nai bench` command (WP9 / EVAL-6)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from .benchmarks import BUILTIN_BENCHMARKS, load_benchmark_suite
from .harness import BenchmarkHarness
from .reports import generate_report, get_git_commit, write_report


def bench_main(argv: list[str]) -> int:
    """Entry point for `nai bench` command."""
    parser = argparse.ArgumentParser(
        prog="nai bench",
        description="Run benchmark suite against a model profile"
    )
    parser.add_argument("suite", nargs="?", default="builtin",
                        help="Benchmark suite directory or 'builtin'")
    parser.add_argument("-w", "--workspace", default="./bench_output",
                        help="Output workspace directory")
    parser.add_argument("--profile", help="Model profile name (required unless --list)")
    parser.add_argument("--reasoner", help="Reasoner model ID override")
    parser.add_argument("--coder", help="Coder model ID override")
    parser.add_argument("--quantization", help="Quantization notes")
    parser.add_argument("--timeout", type=int, default=3600,
                        help="Timeout per benchmark (seconds)")
    parser.add_argument("--archetype", help="Filter by archetype")
    parser.add_argument("--size", help="Filter by size grade")
    parser.add_argument("--output-format", choices=["json", "markdown", "both"],
                        default="both", help="Report output format")
    parser.add_argument("--list", action="store_true",
                        help="List available benchmarks and exit")
    parser.add_argument("--mode", choices=["v1", "v2"], default="v2",
                        help="Pipeline mode: v2 (store/scheduler, default) or v1 (legacy CLI)")
    
    args = parser.parse_args(argv)
    
    if not args.list and not args.profile:
        parser.error("--profile is required unless --list is used")

    # Handle --list
    if args.list:
        if args.suite == "builtin":
            for b in BUILTIN_BENCHMARKS:
                print(f"{b['id']:30} {b['archetype']:15} {b['size_grade']:8} {b['name']}")
        else:
            suite = load_benchmark_suite(args.suite)
            for b in suite:
                print(f"{b.id:30} {b.archetype:15} {b.size_grade:8} {b.name}")
        return 0
    
    # Load benchmark suite
    if args.suite == "builtin":
        # Create temporary spec files for builtin benchmarks
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            for b in BUILTIN_BENCHMARKS:
                spec_file = tmpdir / f"{b['id']}.md"
                spec_file.write_text(f"# {b['name']}\n\n{b['description']}\n\nArchetype: {b['archetype']}\nSize: {b['size_grade']}\n")
            suite = load_benchmark_suite(tmpdir)
    else:
        suite = load_benchmark_suite(args.suite)
    
    # Apply filters
    if args.archetype:
        suite = suite.filter(archetype=args.archetype)
    if args.size:
        suite = suite.filter(size_grade=args.size)
    
    if len(suite) == 0:
        print("No benchmarks match the filters", file=sys.stderr)
        return 1
    
    print(f"Running {len(suite)} benchmarks from {args.suite}")
    
    # Run benchmarks
    harness = BenchmarkHarness(
        benchmarks_dir=Path(args.suite) if args.suite != "builtin" else Path("."),
        output_dir=Path(args.workspace),
        pipeline_mode=args.mode,
    )
    
    model_id = args.reasoner or args.coder or "auto"
    
    results = harness.run_suite(
        [b.spec_path for b in suite],
        model_profile=args.profile,
        model_id=model_id,
        quantization=args.quantization,
        timeout=args.timeout
    )
    
    # Generate report
    git_commit = get_git_commit()
    report = generate_report(
        results=results,
        model_profile=args.profile,
        model_id=model_id,
        quantization=args.quantization or "unknown",
        benchmark_suite=args.suite,
        nai_version="2.0.0.dev0",
        git_commit=git_commit
    )
    
    # Write report
    json_path, md_path = write_report(report, Path(args.workspace) / "reports", args.output_format)
    
    # Print summary
    print(f"\n{'='*50}")
    print(f"Benchmark Suite: {args.suite}")
    print(f"Model Profile: {args.profile}")
    print(f"Model: {model_id}")
    print(f"Git Commit: {git_commit}")
    print(f"{'='*50}")
    print(f"Total: {report.summary['total']}")
    print(f"Passed: {report.summary['passed']}")
    print(f"Failed: {report.summary['failed']}")
    print(f"Success Rate: {report.summary['success_rate']:.1%}")
    print(f"Total Duration: {report.summary['total_duration']:.1f}s")
    print("\nReports written to:")
    print(f"  JSON: {json_path}")
    print(f"  Markdown: {md_path}")
    
    # Exit with failure if any benchmarks failed
    return 0 if report.summary['failed'] == 0 else 1


def register_bench_subcommand(register_fn: Callable[[str, Callable], None]) -> None:
    register_fn("bench", bench_main)
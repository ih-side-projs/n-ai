"""Deterministic requirement-to-test traceability checks (BLD-4, X2)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceabilityResult:
    passed: bool
    missing: list[str]
    covered: dict[str, list[str]]
    test_functions: list[str]
    syntax_errors: list[str]


def extract_test_functions(test_sources: dict[str, str]) -> tuple[list[str], list[str]]:
    """Parse Python test files with AST and return (test_function_names, syntax_errors)."""
    funcs: list[str] = []
    syntax_errors: list[str] = []

    for path, source in sorted(test_sources.items()):
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            syntax_errors.append(f"{path}:{exc.lineno}: {exc.msg}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    funcs.append(node.name)

    return funcs, syntax_errors


def _id_in_test_name(req_id: str, func_name: str) -> bool:
    pattern = re.compile(rf"(?:^|_){re.escape(req_id)}(?:_|$)", re.IGNORECASE)
    return pattern.search(func_name) is not None


def check_traceability(
    requirements: list[dict],
    test_sources: dict[str, str],
) -> TraceabilityResult:
    """Verify every testable requirement id appears in at least one test function name (BLD-4)."""
    funcs, syntax_errors = extract_test_functions(test_sources)
    covered: dict[str, list[str]] = {}
    missing: list[str] = []

    for req in requirements:
        if not req.get("testable", True):
            continue
        req_id = str(req["id"]).strip()
        matches = [fn for fn in funcs if _id_in_test_name(req_id, fn)]
        if matches:
            covered[req_id] = matches
        else:
            missing.append(req_id)

    passed = (len(missing) == 0) and (len(syntax_errors) == 0)
    return TraceabilityResult(
        passed=passed,
        missing=missing,
        covered=covered,
        test_functions=funcs,
        syntax_errors=syntax_errors,
    )

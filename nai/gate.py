"""verification-gate: has a recognised test command exited 0 since the last change? (v1 + CORE-4)

CORE-4: besides the default (run-wide) scope, which behaves exactly like v1, the gate keeps
named scopes, one per node. A node scope requires the node's own test command(s); a pass is
recorded against that command and the workspace fingerprint at the time.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv", ".agent", ".pytest_cache", ".mypy_cache"}
FP_SKIP = IGNORE | {"dist", "build", "target", "coverage", "htmlcov", ".next", ".tox", ".nox", ".cache", ".ruff_cache"}

# Commands that count as a "test run" in auto mode (no explicit commands).
TEST_RE = re.compile(
    r"(?:^|[\s;&|(])(?:pytest|py\.test|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest|bats)\b"
    r"|python3?\s+-m\s+(?:pytest|unittest)"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|t)\b"
    r"|\bgo\s+test\b|\bcargo\s+(?:test|nextest)\b|\bdotnet\s+test\b|\bmake\s+(?:test|check)\b"
    r"|\bdeno\s+test\b|\bswift\s+test\b|\bbundle\s+exec\s+(?:rspec|rake\s+test)\b"
    r"|(?:mvn|mvnw|gradlew?)\b.*\btest\b|npx\s+(?:jest|vitest|mocha)\b")
NOT_TEST_RE = re.compile(r"--collect-only|--co\b|--help\b|--version\b|--dry-run|--listTests|\s-h\b")
MASK_RE = re.compile(r"\|\|\s*(?:true|:|exit\s+0)(?!\w)")  # '|| true' / '|| :' hide failures
ZERO_RE = re.compile(
    r"collected 0 items|Ran 0 tests|running 0 tests|no tests ran|No tests found|Executed 0 tests", re.I
)


@dataclass
class _Scope:
    required: list[str]
    passed: set[str] = field(default_factory=set)
    fp_at_pass: str | None = None


class VerificationGate:
    def __init__(self, root: Path, test_cmds: list[str] | None = None, enabled: bool = True):
        self.root = Path(root)
        self.test_cmds: list[str] = test_cmds if test_cmds is not None else []
        self.enabled = enabled
        self._default = _Scope([])
        self._scopes: dict[str, _Scope] = {}

    # -- scopes --------------------------------------------------------------
    def set_required(self, scope: str, commands: str | list[str]) -> None:
        """Declare the command(s) a node scope needs (CORE-4). Re-declaring keeps recorded passes."""
        cmds = [commands] if isinstance(commands, str) else list(commands)
        self._scopes.setdefault(scope, _Scope(cmds)).required = cmds

    def _scope(self, scope: str | None) -> _Scope:
        if scope is None:
            self._default.required = self.test_cmds
            return self._default
        return self._scopes.setdefault(scope, _Scope([]))

    @property
    def passed(self) -> set[str]:
        return self._default.passed

    @property
    def fp_at_pass(self) -> str | None:
        return self._default.fp_at_pass

    # -- recognition ---------------------------------------------------------
    def counts_as_test(self, command: str, rc: int, out: str, scope: str | None = None) -> list[str] | None:
        if rc != 0 or NOT_TEST_RE.search(command) or MASK_RE.search(command) or ZERO_RE.search(out):
            return None
        required = self._scope(scope).required
        if required:
            return [c for c in required if c in command] or None
        return ["auto"] if TEST_RE.search(command) else None

    def record_pass(self, keys: list[str], scope: str | None = None) -> str:
        sc = self._scope(scope)
        sc.passed.update(keys)
        sc.fp_at_pass = self.fingerprint()
        return sc.fp_at_pass

    def invalidate(self, scope: str | None = None) -> None:
        self._scope(scope).passed.clear()

    def invalidate_all(self) -> None:
        self._default.passed.clear()
        for sc in self._scopes.values():
            sc.passed.clear()

    # -- fingerprint / verdict -----------------------------------------------
    def fingerprint(self) -> str:
        h = hashlib.sha1()
        for dp, dns, fns in os.walk(self.root):
            dns[:] = sorted(d for d in dns if d not in FP_SKIP and not d.endswith(".egg-info"))
            for f in sorted(fns):
                if f.endswith((".pyc", ".log")):
                    continue
                fp = os.path.join(dp, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                h.update(f"{os.path.relpath(fp, self.root)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
        return h.hexdigest()

    def problem(self, scope: str | None = None) -> str | None:
        """None if the work counts as verified; otherwise a message saying what is missing."""
        if not self.enabled:
            return None
        sc = self._scope(scope)
        need = set(sc.required) or {"auto"}
        missing = need - sc.passed
        if missing:
            what = ("these commands (each must exit 0): " + "; ".join(sorted(missing))) if sc.required \
                else "a test command (pytest, npm test, go test, cargo test, make test, ...) that exits 0"
            return f"No passing verification since your last file change. Run {what}."
        if self.fingerprint() != sc.fp_at_pass:
            return "Files changed (e.g. via shell commands) since the last passing test run. Re-run the tests."
        return None

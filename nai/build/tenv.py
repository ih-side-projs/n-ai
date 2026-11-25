"""Target environment management, warnings-as-errors, lint gate, API notes, and staleness tracking."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..exec import (
    PHASE_RUN,
    PHASE_SETUP,
    ExecutionTarget,
    LocalTarget,
    RunResult,
    needs_shell,
    shell_argv,
)


class TargetEnvError(RuntimeError):
    """Raised when target environment creation or validation fails."""


@dataclass(frozen=True)
class TargetEnv:
    language: str = "python"
    python_version: str = field(
        default_factory=lambda: f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    os_family: str = field(default_factory=lambda: platform.system().lower())
    packages: tuple[str, ...] = ()
    lockfile_path: str | None = None
    offline: bool = False
    wheelhouse: str | None = None
    deprecation_allowlist: tuple[str, ...] = ()
    lint_rules: tuple[str, ...] = ("UP", "B")
    lint_max_findings: int = 20
    type_checker: str = "auto"  # "auto" | "mypy" | "pyright" | "none"
    api_notes_max_tokens: int = 1500
    venv_dir: str = ".nai/venv"


@dataclass(frozen=True)
class LintResult:
    passed: bool
    findings: list[str]
    raw_output: str
    tools_run: list[str]


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate without external tokenizers (X2)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def is_pinned_requirement(spec: str) -> bool:
    """Return True if requirement specifies an exact version or direct archive."""
    s = spec.strip()
    if not s or s.startswith("#"):
        return False
    return ("==" in s) or (" @ " in s) or s.endswith(".whl")


def canonical_package_name(spec: str) -> str:
    """Extract normalized package name from a requirement string."""
    s = spec.strip()
    for sep in ("==", ">=", "<=", "~=", "!=", "@", "[", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
    return re.sub(r"[-_.]+", "-", s).lower().strip()


def _parse_lockfile_lines(text: str) -> list[str]:
    pkgs: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        pkgs.append(line)
    return pkgs


def parse_target_env(project: dict[str, Any], workspace_root: Path | None = None) -> TargetEnv:
    """Extract TENV-1 target environment settings from a Project record."""
    settings = project.get("settings") or {}
    cfg = settings.get("target_env") or {}

    default_py = f"{sys.version_info.major}.{sys.version_info.minor}"
    default_os = platform.system().lower()

    raw_pkgs = list(cfg.get("packages") or cfg.get("requirements") or [])
    lockfile_path = cfg.get("lockfile_path")
    if lockfile_path and workspace_root is not None:
        lf = workspace_root / lockfile_path
        if lf.is_file():
            raw_pkgs.extend(_parse_lockfile_lines(lf.read_text(encoding="utf-8")))

    # Deduplicate preserving order
    seen: set[str] = set()
    pkgs: list[str] = []
    for p in raw_pkgs:
        ps = str(p).strip()
        if ps and ps not in seen:
            seen.add(ps)
            pkgs.append(ps)

    return TargetEnv(
        language=str(settings.get("language") or "python"),
        python_version=str(cfg.get("python_version") or default_py),
        os_family=str(cfg.get("os_family") or default_os).lower(),
        packages=tuple(pkgs),
        lockfile_path=lockfile_path,
        offline=bool(cfg.get("offline", False)),
        wheelhouse=cfg.get("wheelhouse"),
        deprecation_allowlist=tuple(str(x) for x in (cfg.get("deprecation_allowlist") or ())),
        lint_rules=tuple(str(x) for x in (cfg.get("lint_rules") or ("UP", "B"))),
        lint_max_findings=int(cfg.get("lint_max_findings", 20)),
        type_checker=str(cfg.get("type_checker", "auto")),
        api_notes_max_tokens=int(cfg.get("api_notes_max_tokens", 1500)),
        venv_dir=str(cfg.get("venv_dir", ".nai/venv")),
    )


def format_env_block(tenv: TargetEnv) -> str:
    """Fixed environment facts header for every context pack (TENV-4)."""
    pkg_lines = (
        "\n".join(f"  - {pkg}" for pkg in tenv.packages)
        if tenv.packages
        else "  - (none; standard library only)"
    )
    return (
        "## Target Environment (TENV-4)\n"
        f"- Language: {tenv.language.capitalize()} {tenv.python_version}\n"
        f"- OS Family: {tenv.os_family}\n"
        f"- Pinned Packages:\n{pkg_lines}\n"
        "- Rule: Use ONLY the standard library and these pinned packages, written for these "
        "exact versions. Never silently add unpinned dependencies; raise a contract/environment "
        "change request if another package is required."
    )


def load_api_notes(
    workspace_root: Path,
    tenv: TargetEnv,
    spec_text: str,
    contract: dict[str, Any] | None = None,
) -> str:
    """Load `.nai/apinotes/<package>.md` for mentioned packages up to token cap (TENV-5)."""
    notes_dir = workspace_root / ".nai" / "apinotes"
    if not notes_dir.is_dir():
        return ""

    haystack = (spec_text or "") + "\n" + json.dumps(contract or {}, sort_keys=True)
    haystack_norm = haystack.lower().replace("_", "-")

    candidate_names: list[str] = []
    for pkg in tenv.packages:
        name = canonical_package_name(pkg)
        if name and name not in candidate_names:
            candidate_names.append(name)

    for md_file in sorted(notes_dir.glob("*.md")):
        stem_norm = re.sub(r"[-_.]+", "-", md_file.stem).lower()
        if stem_norm not in candidate_names:
            candidate_names.append(stem_norm)

    selected_chunks: list[str] = []
    used_tokens = 0
    budget = max(0, tenv.api_notes_max_tokens)

    for pkg_norm in candidate_names:
        if used_tokens >= budget:
            break
        # Check whether spec or contract mentions package
        variants = {pkg_norm, pkg_norm.replace("-", "_")}
        if not any(re.search(rf"(?<![a-z0-9_]){re.escape(v)}(?![a-z0-9_])", haystack_norm) for v in variants):
            continue

        note_file = None
        for md_file in sorted(notes_dir.glob("*.md")):
            if re.sub(r"[-_.]+", "-", md_file.stem).lower() == pkg_norm:
                note_file = md_file
                break
        if note_file is None:
            continue

        content = note_file.read_text(encoding="utf-8").strip()
        if not content:
            continue

        header = f"### API Notes: {note_file.stem}\n"
        chunk = header + content
        chunk_tokens = estimate_tokens(chunk)
        if used_tokens + chunk_tokens <= budget:
            selected_chunks.append(chunk)
            used_tokens += chunk_tokens
        else:
            remaining_chars = max(0, (budget - used_tokens) * 4 - len(header) - 24)
            if remaining_chars > 40:
                truncated = content[:remaining_chars].rsplit("\n", 1)[0].strip()
                if truncated:
                    selected_chunks.append(header + truncated + "\n[...truncated...]")
            break

    if not selected_chunks:
        return ""
    return "## Pinned Package API Notes (TENV-5)\n\n" + "\n\n".join(selected_chunks)


class TargetEnvManager:
    """Manages per-project virtualenvs, deprecation warning flags, and lint gates (TENV-2, TENV-3).

    *target* is the :class:`nai.exec.ExecutionTarget` setup commands run through.
    When omitted, a local target is created lazily so direct callers (tests and
    library users) keep working.
    """

    def __init__(self, target: ExecutionTarget | None = None):
        self.target = target

    def _target_for(self, workspace_root: Path) -> ExecutionTarget:
        if self.target is None:
            self.target = LocalTarget(workspace_root, default_timeout=600)
        return self.target

    def _run(
        self,
        workspace_root: Path,
        argv: list[str],
        *,
        phase: str = PHASE_SETUP,
        timeout: float = 600,
        env_overrides: dict[str, str] | None = None,
    ) -> RunResult:
        """Run one setup command through the execution target."""
        return self._target_for(workspace_root).run(
            argv,
            cwd=str(workspace_root),
            env_overrides=env_overrides,
            timeout=timeout,
            phase=phase,
        )

    def venv_path(self, workspace_root: Path, tenv: TargetEnv) -> Path:
        p = Path(tenv.venv_dir)
        return p if p.is_absolute() else (workspace_root / p)

    def venv_bin_dir(self, venv_path: Path) -> Path:
        win_scripts = venv_path / "Scripts"
        if win_scripts.is_dir():
            return win_scripts
        return venv_path / "bin"

    def venv_python(self, workspace_root: Path, tenv: TargetEnv) -> Path:
        vp = self.venv_path(workspace_root, tenv)
        b = self.venv_bin_dir(vp)
        exe = b / "python.exe" if (b / "python.exe").exists() else b / "python"
        return exe

    def _env_fingerprint(self, tenv: TargetEnv) -> str:
        payload = json.dumps(
            {
                "python_version": tenv.python_version,
                "packages": list(tenv.packages),
                "offline": tenv.offline,
                "wheelhouse": tenv.wheelhouse,
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _select_base_python(self, tenv: TargetEnv) -> str:
        host_py = f"{sys.version_info.major}.{sys.version_info.minor}"
        if tenv.python_version == host_py:
            return sys.executable
        candidate = shutil.which(f"python{tenv.python_version}")
        if candidate:
            return candidate
        raise TargetEnvError(
            f"Requested Python {tenv.python_version} not found on PATH (host is {host_py})"
        )

    def ensure_env(self, workspace_root: Path, tenv: TargetEnv) -> Path:
        """Create or update the project virtualenv from pinned packages (TENV-2)."""
        for spec in tenv.packages:
            if not is_pinned_requirement(spec):
                raise TargetEnvError(
                    f"Unpinned dependency {spec!r} in target_env (TENV-1 requires exact pins)"
                )

        vp = self.venv_path(workspace_root, tenv)
        marker = vp / ".nai_env_hash"
        expected_hash = self._env_fingerprint(tenv)
        py_exe = self.venv_python(workspace_root, tenv)

        if py_exe.exists() and marker.is_file() and marker.read_text().strip() == expected_hash:
            return vp

        base_python = self._select_base_python(tenv)
        vp.parent.mkdir(parents=True, exist_ok=True)
        if not py_exe.exists():
            # setup phase: network allowed (venv creation itself does not use it)
            res = self._run(workspace_root, [base_python, "-m", "venv", str(vp)], phase=PHASE_SETUP)
            if res.exit_code != 0:
                raise TargetEnvError(f"venv creation failed: {res.output or res.error}")
            py_exe = self.venv_python(workspace_root, tenv)

        if tenv.packages:
            self._install_packages(py_exe, workspace_root, tenv)

        marker.write_text(expected_hash, encoding="utf-8")
        return vp

    @staticmethod
    def _is_hash_pin(spec: str) -> bool:
        return "--hash=" in spec or "# sha256:" in spec

    def _install_packages(self, py_exe: Path, workspace_root: Path, tenv: TargetEnv) -> None:
        cmd = [str(py_exe), "-m", "pip", "install", "--disable-pip-version-check"]
        wheelhouse_path: Path | None = None
        if tenv.wheelhouse:
            wp = Path(tenv.wheelhouse)
            wheelhouse_path = wp if wp.is_absolute() else (workspace_root / wp)

        if tenv.offline:
            if wheelhouse_path is None or not wheelhouse_path.is_dir():
                raise TargetEnvError(
                    f"Offline install requested but wheelhouse directory does not exist: {tenv.wheelhouse}"
                )
            cmd.extend(["--no-index", "--find-links", str(wheelhouse_path)])
        elif wheelhouse_path is not None and wheelhouse_path.is_dir():
            cmd.extend(["--find-links", str(wheelhouse_path)])

        # TENV-2: if every pin is a hash pin, force pip to verify every package
        # (including transitive dependencies) rather than trusting resolved names.
        if all(self._is_hash_pin(p) for p in tenv.packages):
            cmd.append("--require-hashes")
        cmd.extend(tenv.packages)
        # setup phase: the network is allowed so pip can reach the index
        res = self._run(workspace_root, cmd, phase=PHASE_SETUP, timeout=1200)
        if res.exit_code != 0:
            raise TargetEnvError(
                f"Package installation failed (offline={tenv.offline}): "
                f"{res.output.strip() or res.error}"
            )

    def deprecation_warning_filters(self, tenv: TargetEnv) -> list[str]:
        filters = ["error::DeprecationWarning"]
        for pat in tenv.deprecation_allowlist:
            # Allowlist entries suppress error escalation back to default for matching module/message
            cleaned = pat.strip()
            if cleaned:
                # -W / PYTHONWARNINGS format is action:message:category:module:lineno.
                # The allowlist entry is a message prefix, so it belongs in the
                # message slot: `default:<message>::`.
                filters.append(f"default:{cleaned}::")
        return filters

    def wrap_test_command(
        self,
        workspace_root: Path,
        tenv: TargetEnv,
        test_command: str,
    ) -> str:
        """Run test_command inside target venv with -W error::DeprecationWarning (TENV-2)."""
        py_exe = self.venv_python(workspace_root, tenv)
        filters = self.deprecation_warning_filters(tenv)
        w_flags = " ".join(f"-W {shlex.quote(f)}" for f in filters)
        tokens = shlex.split(test_command)
        if not tokens:
            return test_command

        if tokens[0] in ("pytest", "py.test"):
            rest = " ".join(shlex.quote(t) for t in tokens[1:])
            return f"{shlex.quote(str(py_exe))} {w_flags} -m pytest {rest}".strip()

        if tokens[0] in ("python", "python3") or tokens[0].startswith("python3."):
            rest = " ".join(shlex.quote(t) for t in tokens[1:])
            return f"{shlex.quote(str(py_exe))} {w_flags} {rest}".strip()

        venv_bin = self.venv_bin_dir(self.venv_path(workspace_root, tenv))
        new_path = f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        py_warn = ",".join(filters)
        return (
            f"env PATH={shlex.quote(new_path)} PYTHONWARNINGS={shlex.quote(py_warn)} "
            f"{test_command}"
        )

    def wrap_adapter_command(
        self,
        workspace_root: Path,
        tenv: TargetEnv,
        command: Any,
    ) -> str:
        """Wrap an adapter ``Command`` in the target environment.

        The adapter owns the argv; this method only applies the Python venv and
        warning-as-error policy the current pipeline already uses.  A
        non-Python adapter would wrap its own commands instead.
        """
        argv = getattr(command, "argv", command)
        return self.wrap_test_command(workspace_root, tenv, shlex.join(argv))

    def command_argv(
        self,
        workspace_root: Path,
        tenv: TargetEnv,
        command: Any,
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, env_overrides)`` for an adapter ``Command``.

        The adapter owns the argv; this method only applies the Python venv and
        the warnings-as-errors policy.  A non-Python adapter wraps its own
        commands in its own ``command_argv``.
        """
        argv = [str(part) for part in getattr(command, "argv", command)]
        venv_bin = self.venv_bin_dir(self.venv_path(workspace_root, tenv))
        filters = self.deprecation_warning_filters(tenv)
        overrides = {"PATH": f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
        if not argv:
            return argv, overrides
        if getattr(command, "shell", False):
            # The adapter explicitly marked this step as a shell step.  It owns
            # the command string, so the venv python substitution does not apply.
            overrides["PYTHONWARNINGS"] = ",".join(filters)
            return shell_argv(shlex.join(argv)), overrides
        py_exe = str(self.venv_python(workspace_root, tenv))
        warn_flags = [flag for f in filters for flag in ("-W", f)]
        head = argv[0]
        if head in ("pytest", "py.test"):
            # The -W flags reach the interpreter directly; PYTHONWARNINGS would
            # also leak into child processes, so it is only set for commands
            # that do not take python flags.
            return [py_exe, *warn_flags, "-m", "pytest", *argv[1:]], overrides
        if head in ("python", "python3") or head.startswith("python3."):
            return [py_exe, *warn_flags, *argv[1:]], overrides
        overrides["PYTHONWARNINGS"] = ",".join(filters)
        return argv, overrides

    def command_argv_for_string(
        self,
        workspace_root: Path,
        tenv: TargetEnv,
        command: str,
    ) -> tuple[list[str], dict[str, str]]:
        """Like :meth:`command_argv` for a user-supplied command string.

        A string that uses shell syntax becomes an explicit shell step; a plain
        command is split into argv without ever invoking a shell.
        """
        argv = shell_argv(command) if needs_shell(command) else shlex.split(command)
        return self.command_argv(workspace_root, tenv, argv)

    def run_lint_gate(
        self,
        workspace_root: Path,
        tenv: TargetEnv,
        target_paths: list[str],
        run_cmd: Callable[[str], tuple[int, str]] | None = None,
        run_argv: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> LintResult:
        """Run Ruff (UP, B) and optional type checker inside target environment (TENV-3)."""
        if not target_paths:
            return LintResult(passed=True, findings=[], raw_output="", tools_run=[])

        venv_bin = self.venv_bin_dir(self.venv_path(workspace_root, tenv))
        findings: list[str] = []
        raw_parts: list[str] = []
        tools_run: list[str] = []
        passed = True

        def _exec(cmd_str: str) -> tuple[int, str]:
            if run_argv is not None:
                return run_argv(shlex.split(cmd_str))
            if run_cmd is not None:
                return run_cmd(cmd_str)
            # lint runs during verify: no network
            res = self._run(workspace_root, shlex.split(cmd_str), phase=PHASE_RUN)
            return res.exit_code, res.output.strip()

        def _find_tool(name: str) -> str | None:
            local = venv_bin / name
            if local.exists():
                return str(local)
            return shutil.which(name)

        quoted_paths = " ".join(shlex.quote(p) for p in target_paths)

        ruff_bin = _find_tool("ruff")
        if ruff_bin:
            rules = ",".join(tenv.lint_rules) if tenv.lint_rules else "UP,B"
            cmd = f"{shlex.quote(ruff_bin)} check --select {shlex.quote(rules)} {quoted_paths}"
            rc, out = _exec(cmd)
            tools_run.append("ruff")
            if out:
                raw_parts.append(f"[ruff]\n{out}")
            if rc != 0:
                passed = False
                for line in out.splitlines():
                    s = line.strip()
                    if s and not s.startswith("Found ") and not s.startswith("[*]"):
                        findings.append(f"ruff: {s}")

        tc_choice = tenv.type_checker
        if tc_choice != "none":
            tc_order = ["mypy", "pyright"] if tc_choice == "auto" else [tc_choice]
            for tc in tc_order:
                tc_bin = _find_tool(tc)
                if tc_bin:
                    cmd = f"{shlex.quote(tc_bin)} {quoted_paths}"
                    rc, out = _exec(cmd)
                    tools_run.append(tc)
                    if out:
                        raw_parts.append(f"[{tc}]\n{out}")
                    if rc != 0:
                        passed = False
                        for line in out.splitlines():
                            s = line.strip()
                            if s:
                                findings.append(f"{tc}: {s}")
                    break

        capped = findings[: max(1, tenv.lint_max_findings)]
        return LintResult(
            passed=passed,
            findings=capped,
            raw_output="\n\n".join(raw_parts),
            tools_run=tools_run,
        )


def detect_stale_api(output_text: str, tenv: TargetEnv) -> list[dict[str, str]]:
    """Identify ImportError, AttributeError, or DeprecationWarning per package (TENV-6)."""
    if not output_text:
        return []

    pinned_map = {
        canonical_package_name(p).replace("-", "_"): canonical_package_name(p)
        for p in tenv.packages
    }
    hits: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _record(pkg_raw: str, kind: str, line: str) -> None:
        mod_root = pkg_raw.split(".", 1)[0].strip().lower().replace("-", "_")
        pkg = pinned_map.get(mod_root, mod_root.replace("_", "-"))
        key = (pkg, kind)
        if pkg and key not in seen:
            seen.add(key)
            hits.append({"package": pkg, "kind": kind, "detail": line.strip()})

    for line in output_text.splitlines():
        m = re.search(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", line)
        if m:
            _record(m.group(1), "ModuleNotFoundError", line)
            continue

        m = re.search(r"ImportError:\s+cannot import name .* from ['\"]([^'\"]+)['\"]", line)
        if m:
            _record(m.group(1), "ImportError", line)
            continue

        m = re.search(r"AttributeError:\s+module ['\"]([^'\"]+)['\"] has no attribute", line)
        if m:
            _record(m.group(1), "AttributeError", line)
            continue

        m = re.search(r"AttributeError:\s+['\"]([^'\"]+)['\"] object has no attribute", line)
        if m:
            obj_type = m.group(1).lower()
            for mod_key in pinned_map:
                if mod_key in obj_type:
                    _record(mod_key, "AttributeError", line)
                    break
            continue

        if "DeprecationWarning" in line:
            matched = False
            lower_line = line.lower().replace("-", "_")
            for mod_key in pinned_map:
                if re.search(rf"(?<![a-z0-9_]){re.escape(mod_key)}(?![a-z0-9_])", lower_line):
                    _record(mod_key, "DeprecationWarning", line)
                    matched = True
            if not matched:
                m_site = re.search(r"site-packages[/\\]([a-zA-Z0-9_]+)", line)
                if m_site:
                    _record(m_site.group(1), "DeprecationWarning", line)

    return hits

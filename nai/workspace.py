"""workspace-tools: confined file tools, tool dispatch and the workspace facade (v1 + CORE-3/4/5/6)."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from .gate import IGNORE, VerificationGate
from .gitcp import GitCheckpoint
from .policy import JobContext, PathPolicy
from .runner import CommandRunner
from .textutil import truncate

FILE_TOOL_NAMES = {"read_file", "write_file", "edit_file", "list_files", "run_command"}


class Workspace:
    def __init__(self, root: Path, cmd_timeout: int = 600, docker_image: str | None = None, git: bool = True,
                 test_cmds: list[str] | None = None, gate: bool = True, extra_env: dict[str, str] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".agent").mkdir(exist_ok=True)
        self.cmd_timeout = cmd_timeout
        self.touched: set[str] = set()
        self.test_cmds: list[str] = test_cmds or []   # must pass before finish(success); empty = auto-detect
        self.gate = VerificationGate(self.root, self.test_cmds, enabled=gate)
        self.gitcp = GitCheckpoint(self.root, enabled=git)
        self.runner = CommandRunner(self.root, cmd_timeout, docker_image, self.gate, extra_env=extra_env)
        self.job: JobContext | None = None

    @property
    def container(self) -> str | None:
        return self.runner.container

    # -- per-job context (CORE-3/4/5) ------------------------------------------
    @contextmanager
    def job_context(self, ctx: JobContext):
        """Apply a path policy, node test command and checkpoint identity for the duration of one job."""
        prev, self.job = self.job, ctx
        cmds = ctx.test_commands()
        if ctx.node_id and cmds:
            self.gate.set_required(ctx.node_id, cmds)
        try:
            yield ctx
        finally:
            self.job = prev

    @property
    def policy(self) -> PathPolicy:
        return (self.job.policy if self.job and self.job.policy else None) or PathPolicy.allow_all()

    def _scope(self) -> str | None:
        """Gate scope of the current job: its node id when the job declared its own test command."""
        return self.job.node_id if self.job and self.job.node_id and self.job.test_commands() else None

    def protected_hashes(self, policy: PathPolicy | None = None) -> dict[str, str]:
        """sha1 of every existing file matching the policy's read-only globs (to detect shell-side edits of frozen files)."""
        pol = policy or self.policy
        out: dict[str, str] = {}
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in IGNORE]
            for f in fns:
                rel = Path(dp, f).relative_to(self.root).as_posix()
                if pol.is_protected(rel):
                    out[rel] = hashlib.sha1(Path(dp, f).read_bytes()).hexdigest()
        return out

    # -- paths -----------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        rel = str(rel)
        if self.container:   # CORE-6: map /work and /work/<x> only, not /workspace/<x>
            if rel == "/work":
                rel = "."
            elif rel.startswith("/work/"):
                rel = rel[len("/work/"):].lstrip("/") or "."
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path escapes workspace: {rel}")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _not_writable(self, p: Path, path) -> str | None:
        if self.policy.allows_write(self._rel(p)):
            return None
        return f"ERROR: path not writable for this job: {path}"

    # -- file tools ------------------------------------------------------------
    def read_file(self, path, start_line=None, end_line=None):
        p = self.resolve(path)
        if not p.is_file():
            return f"ERROR: {path} does not exist or is not a file"
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        n = len(lines)
        if start_line or end_line:
            a, b = max(1, int(start_line or 1)), min(n, int(end_line or n))
            return f"[{path} lines {a}-{b} of {n}]\n" + "\n".join(lines[a - 1:b])
        if len(text) > 20000:
            return truncate(text, 20000) + f"\n[file has {n} lines; use start_line/end_line for specific parts]"
        return text

    def write_file(self, path, content):
        p = self.resolve(path)
        if (err := self._not_writable(p, path)):
            return err
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.touched.add(self._rel(p))
        self.gate.invalidate_all()
        return f"wrote {path} ({len(content.encode('utf-8'))} bytes, {content.count(chr(10)) + 1} lines)"

    def edit_file(self, path, old_str, new_str):
        p = self.resolve(path)
        if (err := self._not_writable(p, path)):
            return err
        if not p.is_file():
            return f"ERROR: {path} does not exist"
        text = p.read_text(encoding="utf-8", errors="replace")
        c = text.count(old_str)
        if c == 0:
            return "ERROR: old_str not found (must match exactly, including whitespace/indentation)"
        if c > 1:
            return f"ERROR: old_str occurs {c} times; include more surrounding context to make it unique"
        p.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        self.touched.add(self._rel(p))
        self.gate.invalidate_all()
        return f"edited {path}"

    def list_files(self, path=".", max_depth=3):
        base = self.resolve(path)
        if not base.is_dir():
            return f"ERROR: {path} is not a directory"
        out: list[str] = []

        def walk(d: Path, depth: int):
            if depth > int(max_depth) or len(out) >= 300:
                return
            for e in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
                if e.name in IGNORE:
                    continue
                out.append("  " * depth + e.name + ("/" if e.is_dir() else ""))
                if e.is_dir():
                    walk(e, depth + 1)

        walk(base, 0)
        if len(out) >= 300:
            out.append("... (listing truncated)")
        return "\n".join(out) or "(empty)"

    def run_command(self, command, timeout_seconds=None):
        # `scope` is deliberately not a parameter: tool arguments come from the model.
        return self.runner.run_command(command, timeout_seconds, scope=self._scope())

    def files_block(self, files, missing: str = "(file does not exist)") -> str:
        """Context-file text for prompts: `### path` plus a fenced block, each cut at 20000 characters."""
        out = ""
        for f in files or []:
            try:
                p = self.resolve(f)
                body = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else missing
            except Exception as e:
                body = f"(cannot read: {e})"
            out += f"\n### {f}\n```\n{truncate(body, 20000)}\n```\n"
        return out

    # -- verification / dispatch / git ----------------------------------------
    def verification_problem(self, scope: str | None = None) -> str | None:
        return self.gate.problem(scope if scope is not None else self._scope())

    def call(self, name: str, args: dict) -> str:
        if name not in FILE_TOOL_NAMES:
            return f"ERROR: unknown tool '{name}'"
        try:
            return str(getattr(self, name)(**args))
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def checkpoint(self, msg: str) -> None:
        self.gitcp.checkpoint(msg)

    def checkpoint_job(self, node_id: str | None = None, job_kind: str | None = None, attempt: int | None = None) -> None:
        """CORE-5: commit `<node>: <job kind> attempt <n>`; arguments default to the current job context."""
        j = self.job
        node_id = node_id or (j.node_id if j else None)
        job_kind = job_kind or (j.job_kind if j else None)
        attempt = attempt or (j.attempt if j else 1)
        if not node_id or not job_kind:
            raise ValueError("checkpoint_job needs a node id and a job kind (pass them or use job_context)")
        self.gitcp.checkpoint_job(node_id, job_kind, attempt)

    def close(self) -> None:
        self.runner.close()

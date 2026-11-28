"""Shared deterministic test doubles matching WP1/WP2/WP3 interfaces."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class FakeInvalidTransition(RuntimeError):
    pass


ALLOWED_EDGES = {
    "draft": {"awaiting_approval", "stale"},
    "awaiting_approval": {"approved", "stale"},
    "approved": {"ready", "stale"},
    "ready": {"running", "stale"},
    "running": {"verifying", "failed", "stale"},
    "verifying": {"done", "failed", "stale"},
    "failed": {"ready", "escalated", "stale"},
    "escalated": {"ready", "stale"},
    "stale": {"draft", "ready"},
    "done": {"stale"},
}


class FakeStore:
    """In-memory Store faithfully enforcing WP2 STO-4 transitions and APIs."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._seq = itertools.count(1)
        self.workspace_root = workspace_root
        self.projects: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.requirements: dict[str, list[dict[str, Any]]] = {}
        self.test_records: dict[str, list[dict[str, Any]]] = {}
        self.approvals: dict[str, list[dict[str, Any]]] = {}
        self.tool_defs: list[dict[str, Any]] = []

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._seq)}"

    def create_project(self, name: str, brief: str = "", workspace_path: str | None = None, *, settings=None) -> str:
        pid = self._id("p")
        self.projects[pid] = {
            "id": pid,
            "name": name,
            "brief": brief,
            "workspace_path": workspace_path,
            "status": "active",
            "settings": settings or {},
        }
        return pid

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.projects.get(project_id)

    def update_project_status(self, project_id: str, status: str) -> None:
        self.projects[project_id]["status"] = status

    def create_node(
        self,
        project_id: str,
        name: str,
        kind: str,
        *,
        parent_id: str | None = None,
        objective: str = "",
        spec_text: str = "",
        contract: dict[str, Any] | None = None,
        depends_on: tuple[str, ...] = (),
        test_command: str | None = None,
    ) -> str:
        nid = self._id("n")
        chash = hashlib.sha1((spec_text + json.dumps(contract or {}, sort_keys=True)).encode()).hexdigest()
        self.nodes[nid] = {
            "id": nid,
            "project_id": project_id,
            "parent_id": parent_id,
            "kind": kind,
            "name": name,
            "objective": objective,
            "spec_text": spec_text,
            "contract": contract,
            "depends_on": list(depends_on),
            "status": "draft",
            "content_hash": chash,
            "built_hash": None,
            "attempts": 0,
            "test_command": test_command,
        }
        return nid

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(node_id)

    def list_nodes(self, project_id: str, parent_id: str | None = None) -> list[dict[str, Any]]:
        return [
            n for n in self.nodes.values()
            if n["project_id"] == project_id and n["parent_id"] == parent_id
        ]

    def set_node_test_command(self, node_id: str, test_command: str | None) -> None:
        self.nodes[node_id]["test_command"] = test_command

    def increment_node_attempts(self, node_id: str, amount: int = 1) -> None:
        self.nodes[node_id]["attempts"] += amount

    def transition_node_status(self, node_id: str, new_status: str, note: str = "", data: Any = None) -> None:
        node = self.nodes[node_id]
        cur = node["status"]
        if new_status not in ALLOWED_EDGES.get(cur, set()):
            raise FakeInvalidTransition(f"Illegal transition {cur} -> {new_status}")
        node["status"] = new_status
        if new_status == "done":
            node["built_hash"] = node["content_hash"]
        self.record_event(node["project_id"], "node.status", f"{cur} -> {new_status}: {note}", node_id=node_id, data=data)

    def create_job(self, project_id: str, kind: str, model_role: str, *, node_id: str | None = None, attempt: int = 1, input_ref=None) -> str:
        jid = self._id("j")
        self.jobs[jid] = {
            "id": jid,
            "project_id": project_id,
            "node_id": node_id,
            "kind": kind,
            "model_role": model_role,
            "status": "queued",
            "attempt": attempt,
            "input_ref": input_ref or {},
            "result_ref": {},
            "started_at": f"2026-01-01T00:00:{next(self._seq):02d}Z",
            "ended_at": None,
            "error": None,
        }
        return jid

    def list_jobs(self, project_id: str, status: str | None = None, model_role: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        res = [j for j in self.jobs.values() if j["project_id"] == project_id]
        if status:
            res = [j for j in res if j["status"] == status]
        if model_role:
            res = [j for j in res if j["model_role"] == model_role]
        return res[:limit]

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        self.jobs[job_id].update({k: v for k, v in kwargs.items() if v is not None})

    def record_event(self, project_id: str, event_type: str, message: str, *, node_id: str | None = None, job_id: str | None = None, level: str = "info", data=None) -> None:
        self.events.append(
            {
                "id": self._id("e"),
                "project_id": project_id,
                "node_id": node_id,
                "job_id": job_id,
                "time": f"2026-01-01T00:00:{next(self._seq):02d}Z",
                "level": level,
                "type": event_type,
                "message": message,
                "data": data or {},
            }
        )

    def list_events(self, project_id: str, node_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        res = [e for e in self.events if e["project_id"] == project_id]
        if node_id:
            res = [e for e in res if e["node_id"] == node_id]
        return res[:limit]

    def replace_requirements(self, node_id: str, requirements: list[dict[str, Any]]) -> None:
        self.requirements[node_id] = list(requirements)

    def list_requirements(self, node_id: str) -> list[dict[str, Any]]:
        return list(self.requirements.get(node_id, []))

    def upsert_test_record(self, node_id: str, file_path: str, requirement_ids: list[str], frozen_hash: str, *, last_result: str = "none", last_run_at: str | None = None) -> None:
        recs = self.test_records.setdefault(node_id, [])
        for r in recs:
            if r["file_path"] == file_path:
                r.update({"requirement_ids": list(requirement_ids), "frozen_hash": frozen_hash, "last_result": last_result, "last_run_at": last_run_at})
                return
        recs.append({"node_id": node_id, "file_path": file_path, "requirement_ids": list(requirement_ids), "frozen_hash": frozen_hash, "last_result": last_result, "last_run_at": last_run_at})

    def list_test_records(self, node_id: str) -> list[dict[str, Any]]:
        return list(self.test_records.get(node_id, []))

    def add_approval(self, node_id: str, stage: str, decision: str, *, note: str = "") -> None:
        self.approvals.setdefault(node_id, []).append({"node_id": node_id, "stage": stage, "decision": decision, "note": note})

    def list_approvals(self, node_id: str) -> list[dict[str, Any]]:
        return list(self.approvals.get(node_id, []))

    def add_artifact(self, project_id: str, kind: str, content: str, *, node_id: str | None = None, filename: str = "artifact.txt") -> str:
        aid = self._id("a")
        self.artifacts.append({"id": aid, "project_id": project_id, "node_id": node_id, "kind": kind, "content": content, "filename": filename})
        return aid

    def list_artifacts(self, project_id: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [a for a in self.artifacts if a["project_id"] == project_id and (node_id is None or a["node_id"] == node_id)]

    def register_tool_def(self, name: str, description: str, parameters: dict, family: str, side_effects: str, timeout_seconds: int) -> None:
        self.tool_defs.append({"name": name, "description": description, "parameters": parameters, "family": family, "side_effects": side_effects, "timeout_seconds": timeout_seconds})

    def list_tool_defs(self, family: str | None = None) -> list[dict[str, Any]]:
        if family is None:
            return list(self.tool_defs)
        return [t for t in self.tool_defs if t["family"] == family]


class FakeWorkspace:
    """Local Workspace test double implementing WP1 Workspace & CORE-3/4/5 semantics."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ctx: Any = None
        self._verified_cmds: set[str] = set()
        self.checkpoints: list[str] = []

    class _CtxMgr:
        def __init__(self, ws: FakeWorkspace, job_ctx: Any) -> None:
            self.ws = ws
            self.job_ctx = job_ctx

        def __enter__(self) -> FakeWorkspace:
            self.ws._ctx = self.job_ctx
            return self.ws

        def __exit__(self, *args: Any) -> None:
            self.ws._ctx = None

    def job_context(self, job_ctx: Any) -> _CtxMgr:
        return self._CtxMgr(self, job_ctx)

    def _matches(self, rel_path: str, globs: tuple[str, ...]) -> bool:
        from fnmatch import fnmatch

        norm = rel_path.replace("\\", "/").lstrip("./")
        for g in globs:
            if fnmatch(norm, g) or (g.endswith("/**") and norm.startswith(g[:-3])):
                return True
        return False

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name == "write_file":
            rel = str(args["path"]).replace("\\", "/").lstrip("/")
            if self._ctx and self._ctx.policy:
                ro = tuple(getattr(self._ctx.policy, "readonly", ()))
                wr = tuple(getattr(self._ctx.policy, "writable", ()))
                if self._matches(rel, ro) or (wr and not self._matches(rel, wr)):
                    return f"ERROR: path not writable for this job: {rel}"
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(args["content"]), encoding="utf-8")
            return f"Wrote {rel}"
        return f"ERROR: unknown tool {name}"

    def run_command(self, cmd: str) -> tuple[int, str]:
        proc = subprocess.run(cmd, shell=True, cwd=str(self.root), capture_output=True, text=True)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode == 0 and self._ctx and getattr(self._ctx, "test_command", None) == cmd:
            self._verified_cmds.add(cmd)
        return proc.returncode, out

    def run_argv(self, argv: Any, *, phase: str = "run", env_overrides: dict | None = None,
                 network: bool | None = None) -> tuple[int, str]:
        """Run argv without a shell, mirroring the real workspace's run_argv."""
        import shlex

        argv = [str(a) for a in argv]
        text = shlex.join(argv)
        env = {**os.environ, **(env_overrides or {})}
        proc = subprocess.run(argv, cwd=str(self.root), capture_output=True, text=True, env=env)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        if proc.returncode == 0 and self._ctx and getattr(self._ctx, "test_command", None) == text:
            self._verified_cmds.add(text)
        return proc.returncode, out

    def verification_problem(self) -> str | None:
        if self._ctx and getattr(self._ctx, "test_command", None):
            if self._ctx.test_command not in self._verified_cmds:
                return f"Command did not pass: {self._ctx.test_command}"
        return None

    def protected_hashes(self, policy: Any) -> dict[str, str]:
        ro = tuple(getattr(policy, "readonly", ()))
        hashes: dict[str, str] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                if self._matches(rel, ro):
                    hashes[rel] = hashlib.sha1(p.read_bytes()).hexdigest()
        return hashes

    def checkpoint_job(self) -> None:
        if self._ctx:
            self.checkpoints.append(f"{self._ctx.node_id}: {self._ctx.job_kind} attempt {self._ctx.attempt}")


class ScriptedClient:
    """Deterministic ModelClient test double returning scripted replies."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat(self, role: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"role": role, "messages": messages, **kwargs})
        text = self.replies.pop(0) if self.replies else "{}"
        msg = SimpleNamespace(content=text, tool_calls=[])
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=10, estimated=True)
        return SimpleNamespace(message=msg, usage=usage, seconds=0.01, swapped=False)


@dataclass
class FakeContext:
    job: dict[str, Any]
    node: dict[str, Any]
    project: dict[str, Any]
    store: FakeStore
    ws: FakeWorkspace
    client: Any
    profile: Any = field(default_factory=lambda: SimpleNamespace(
        coder=SimpleNamespace(ctx=32768, max_tokens=8192),
        reasoner=SimpleNamespace(ctx=32768, max_tokens=16384),
    ))
    budget: Any = None
    enqueued: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def check(self) -> None:
        pass

    def event(self, event_type: str, msg: str, level: str = "info", data=None) -> None:
        self.store.record_event(self.project["id"], event_type, msg, node_id=self.node["id"], job_id=self.job["id"], level=level, data=data)

    def artifact(self, kind: str, content: str, filename: str) -> str:
        return self.store.add_artifact(self.project["id"], kind, content, node_id=self.node["id"], filename=filename)

    def enqueue(self, kind: str, role: str, input_ref=None) -> str:
        self.enqueued.append((kind, role, input_ref or {}))
        return self.store.create_job(self.project["id"], kind, role, node_id=self.node["id"], input_ref=input_ref)

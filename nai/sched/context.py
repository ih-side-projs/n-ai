"""What a job handler receives: a RunContext with the job's node, project, store, guarded model client and workspace."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from .budget import Budget, Usage
from .registry import BudgetExceeded, JobCancelled

if TYPE_CHECKING:  # pragma: no cover
    from .runner import Runner


class GuardedClient:
    """A ModelClient whose every call first checks cancellation and budget, tags the call with the job (so the runner
    can record it, X6) and adds its tokens to the job's totals. Same API as nai.models.ModelClient."""

    def __init__(self, ctx: "RunContext", client):
        self._ctx, self._client = ctx, client

    def chat(self, role: str, messages, **kw):
        self._ctx.check()
        j = self._ctx.job
        kw["meta"] = {**(kw.get("meta") or {}), "job_id": j["id"], "node_id": j.get("node_id"), "job_kind": j.get("kind")}
        res = self._client.chat(role, messages, **kw)
        self._ctx._add_usage(res)
        self._ctx.check()
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class RunContext:
    def __init__(self, runner: "Runner", job: dict, node: dict | None, project: dict, profile, budget: Budget):
        self._runner = runner
        self.job, self.node, self.project, self.profile, self.budget = job, node, project, profile, budget
        self.store = runner.store_proxy                       # use this, never the runner's own Store, from a handler
        self._client: GuardedClient | None = None
        self.config = runner.sched
        self.cancel_event = threading.Event()
        self.cancel_reason: str | None = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()

    @property
    def client(self) -> GuardedClient:
        """The model client for this job's project profile (created on first use, so jobs that never call a model need no LLM)."""
        if self._client is None:
            self._client = GuardedClient(self, self._runner.model_client(self.profile))
        return self._client

    # -- cooperation points ---------------------------------------------------------------------------------
    def check(self) -> None:
        """Raise JobCancelled or BudgetExceeded when the job must stop. Called automatically around every model call;
        long handlers should also call it between steps."""
        if self.cancel_event.is_set():
            raise JobCancelled(self.cancel_reason or "cancel")
        reason = self._runner.budget_problem(self)
        if reason:
            raise BudgetExceeded(reason)

    def _add_usage(self, res) -> None:
        with self._lock:
            self.calls += 1
            self.tokens_in += int(res.usage.prompt_tokens or 0)
            self.tokens_out += int(res.usage.completion_tokens or 0)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def usage(self) -> Usage:
        """The node's usage so far, including this running job."""
        return self._runner.node_usage(self.node, extra_tokens=self.tokens, extra_seconds=self.elapsed)

    # -- helpers --------------------------------------------------------------------------------------------
    @property
    def ws(self):
        """The project's Workspace (shared by its jobs), or None when the project has no workspace_path."""
        return self._runner.workspace_for(self.project)

    def event(self, event_type: str, message: str, *, level: str = "info", data: dict | None = None) -> None:
        self.store.record_event(self.project["id"], event_type, message, node_id=self.job.get("node_id"),
                                job_id=self.job["id"], level=level, data=data)

    def artifact(self, kind: str, content: str, filename: str = "artifact.txt"):
        return self.store.add_artifact(self.project["id"], kind, content, node_id=self.job.get("node_id"), filename=filename)

    def enqueue(self, kind: str, model_role: str, *, node_id: str | None = None, attempt: int = 1, input_ref: dict | None = None):
        """Create a follow-up job in this job's project (defaults to this job's node)."""
        return self.store.create_job(self.project["id"], kind, model_role,
                                     node_id=node_id if node_id is not None else self.job.get("node_id"),
                                     attempt=attempt, input_ref=input_ref)

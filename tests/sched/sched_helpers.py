"""Shared builders for the scheduler tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nai.config import Config
from nai.llm import LLM
from nai.models import ModelProfile, RoleConfig
from nai.sched import HandlerRegistry, JobResult, Runner, SchedConfig

ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, local=datetime(2026, 11, 16, 12, 0)):
        self.t = 1000.0
        self._local = local

    def now(self):
        return datetime(2026, 11, 16, tzinfo=timezone.utc) + timedelta(seconds=self.t - 1000.0)

    def local(self):
        return self._local + timedelta(seconds=self.t - 1000.0)

    def mono(self):
        return self.t

    def advance(self, s):
        self.t += s


def profile(r="r", c="c", ctx=32768):
    return ModelProfile("default", RoleConfig(r, ctx=ctx, max_tokens=1000), RoleConfig(c, ctx=ctx, max_tokens=1000))


def sched_cfg(**kw):
    base = dict(poll_seconds=0.01, plan_interval_seconds=0.0, heartbeat_seconds=0.0, threaded=False, plugins=[])
    base.update(kw)
    return SchedConfig(**base)


def make_runner(store, stub=None, tmp_path=None, *, threaded=False, clock=None, registry=None, **cfg):
    llm = LLM(stub.url, "none", stream=False, show_thinking=False) if stub is not None else None
    app = Config()
    app.profiles["default"] = profile()
    r = Runner(store, llm, app_config=app, sched=sched_cfg(threaded=threaded, **cfg), state_dir=Path(tmp_path or ".") / "state",
               clock=clock, registry=registry or HandlerRegistry())
    return r


def chat_handler(ctx):
    res = ctx.client.chat(ctx.job["model_role"], [{"role": "user", "content": f"job {ctx.job['id']}"}])
    return JobResult.success(text=res.message.content)


def noop(ctx):
    return JobResult.success()


def make_node(store, pid, name="n", status="ready", *, kind="leaf", parent_id=None, depends_on=(), attempts=0):
    """Create a node and walk it along legal edges to `status`.

    WP2 enforces one root per project; attach additional nodes under the
    existing root so tests may create several nodes per project.
    """
    if parent_id is None:
        roots = store.list_nodes(pid, parent_id=None)
        if roots:
            parent_id = roots[0]["id"]
            # walk_nodes only recurses into non-leaf parents
            root = store.get_node(parent_id)
            if root and root.get("kind") == "leaf":
                root["kind"] = "component"
                if hasattr(store, "_transaction"):
                    with store._transaction() as conn:
                        conn.execute("UPDATE nodes SET kind='component' WHERE id=?", (parent_id,))
                elif hasattr(store, "nodes") and parent_id in store.nodes:
                    store.nodes[parent_id]["kind"] = "component"
    nid = store.create_node(pid, name, kind, parent_id=parent_id, depends_on=depends_on)
    path = {"draft": [], "awaiting_approval": ["awaiting_approval"], "approved": ["awaiting_approval", "approved"],
            "ready": ["awaiting_approval", "approved", "ready"], "running": ["awaiting_approval", "approved", "ready", "running"],
            "verifying": ["awaiting_approval", "approved", "ready", "running", "verifying"],
            "done": ["awaiting_approval", "approved", "ready", "running", "verifying", "done"],
            "failed": ["awaiting_approval", "approved", "ready", "running", "failed"]}[status]
    for s in path:
        store.transition_node_status(nid, s)
    if attempts:
        store.increment_node_attempts(nid, attempts)
    return nid

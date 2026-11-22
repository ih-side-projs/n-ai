"""In-memory stand-in for nai.store.Store for scheduler tests.

Constants (status edges, command actions, job kinds) are imported from the real
store module so the fake can never drift from production behaviour. Only the
persistence mechanism is faked; the rules are shared.
"""
from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta, timezone

from nai.store.store import (
    _ALLOWED_TRANSITIONS as EDGES,
)
from nai.store.store import (
    COMMAND_ACTIONS as ACTIONS,
)
from nai.store.store import (
    JOB_KINDS,
)


class InvalidTransition(Exception):
    pass
T0 = datetime(2026, 11, 16, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, *, events_newest_first=False, missing_profile="none"):
        self.projects, self.nodes, self.jobs, self.commands, self.events = {}, {}, {}, {}, []
        self.approvals, self.artifacts, self.profiles = [], [], {}
        self.events_newest_first, self.missing_profile = events_newest_first, missing_profile
        self._n = itertools.count(1)
        self._t = itertools.count(0)
        self.calls = []                         # (method, args) log so tests can assert what the runner did
        self.closed = False

    # -- helpers ---------------------------------------------------------------------------------
    def _id(self, p):
        return f"{p}{next(self._n)}"

    def _now(self):
        return (T0 + timedelta(seconds=next(self._t))).isoformat(timespec="seconds")

    # -- projects ----------------------------------------------------------------------------------
    def create_project(self, name, brief="", workspace_path=None, *, settings=None):
        pid = self._id("p")
        self.projects[pid] = {"id": pid, "name": name, "brief": brief, "workspace_path": workspace_path, "status": "active",
                              "created_at": self._now(), "settings": dict(settings or {})}
        return pid

    def get_project(self, project_id):
        return copy.deepcopy(self.projects.get(project_id))

    def list_projects(self, status=None):
        return [copy.deepcopy(p) for p in self.projects.values() if status in (None, p["status"])]

    def set_project_status(self, project_id, status):
        self.projects[project_id]["status"] = status

    # -- nodes -----------------------------------------------------------------------------------------
    def create_node(self, project_id, name, kind, *, parent_id=None, objective="", spec_text="", contract=None, depends_on=(),
                    test_command=None):
        nid = self._id("n")
        self.nodes[nid] = {"id": nid, "project_id": project_id, "parent_id": parent_id, "kind": kind, "name": name,
                           "objective": objective, "spec_text": spec_text, "contract": contract, "depends_on": list(depends_on),
                           "status": "draft", "content_hash": "h", "attempts": 0, "test_command": test_command,
                           "created_at": self._now(), "updated_at": self._now()}
        return nid

    def get_node(self, node_id):
        return copy.deepcopy(self.nodes.get(node_id))

    def list_nodes(self, project_id, parent_id=None):
        return [copy.deepcopy(n) for n in self.nodes.values() if n["project_id"] == project_id and n["parent_id"] == parent_id]

    def transition_node_status(self, node_id, new_status, note="", data=None):
        n = self.nodes[node_id]
        old = n["status"]
        if not (new_status == "stale" or new_status in EDGES.get(old, ())):
            self.record_event(n["project_id"], "node.transition_rejected", f"{old} -> {new_status}", node_id=node_id, level="warning")
            raise InvalidTransition(f"{old} -> {new_status}")
        n["status"] = new_status
        self.record_event(n["project_id"], "node.transition", f"{old} -> {new_status} {note}".strip(), node_id=node_id,
                          data={"from": old, "to": new_status, "note": note, **(data or {})})

    def increment_node_attempts(self, node_id, amount=1):
        self.nodes[node_id]["attempts"] += amount

    # -- jobs --------------------------------------------------------------------------------------------
    def create_job(self, project_id, kind, model_role, *, node_id=None, attempt=1, input_ref=None):
        if kind not in JOB_KINDS or model_role not in ("reasoner", "coder"):
            raise ValueError(f"bad job kind or role: {kind} {model_role}")
        jid = self._id("j")
        self.jobs[jid] = {"id": jid, "project_id": project_id, "node_id": node_id, "kind": kind, "model_role": model_role,
                          "status": "queued", "attempt": attempt, "input_ref": input_ref, "result_ref": None, "tokens_in": 0,
                          "tokens_out": 0, "started_at": None, "ended_at": None, "error": None}
        return jid

    def get_job(self, job_id):
        return copy.deepcopy(self.jobs.get(job_id))

    def list_jobs(self, project_id, status=None, model_role=None, limit=500):
        out = [copy.deepcopy(j) for j in self.jobs.values()
               if j["project_id"] == project_id and status in (None, j["status"]) and model_role in (None, j["model_role"])]
        return out[:limit]

    def update_job(self, job_id, **fields):
        allowed = {"status", "started_at", "ended_at", "tokens_in", "tokens_out", "result_ref", "error", "input_ref"}
        bad = set(fields) - allowed
        if bad:
            raise TypeError(f"update_job got unexpected fields {bad}")
        self.calls.append(("update_job", job_id, dict(fields)))
        self.jobs[job_id].update(fields)

    # -- commands ----------------------------------------------------------------------------------------
    def enqueue_command(self, project_id, action, *, node_id=None, payload=None):
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action}")
        cid = self._id("c")
        self.commands[cid] = {"id": cid, "project_id": project_id, "node_id": node_id, "action": action,
                              "payload": dict(payload or {}), "status": "pending", "error": None}
        return cid

    def claim_commands(self, limit=20):
        out = []
        for c in self.commands.values():
            if c["status"] == "pending" and len(out) < limit:
                c["status"] = "claimed"
                out.append(copy.deepcopy(c))
        return out

    def complete_command(self, command_id, error=None):
        self.commands[command_id].update(status="done", error=error)

    # -- events / approvals / artifacts / profiles ---------------------------------------------------------
    def record_event(self, project_id, event_type, message, *, node_id=None, job_id=None, level="info", data=None):
        self.events.append({"id": self._id("e"), "project_id": project_id, "node_id": node_id, "job_id": job_id,
                            "time": self._now(), "level": level, "type": event_type, "message": message, "data": data})

    def list_events(self, project_id, node_id=None, limit=500):
        out = [copy.deepcopy(e) for e in self.events if e["project_id"] == project_id and node_id in (None, e["node_id"])]
        out = out[::-1] if self.events_newest_first else out
        return out[:limit]

    def add_approval(self, node_id, stage, decision, *, note=""):
        self.approvals.append({"id": self._id("a"), "node_id": node_id, "stage": stage, "decision": decision, "note": note,
                               "time": self._now()})

    def list_approvals(self, node_id):
        return [copy.deepcopy(a) for a in self.approvals if a["node_id"] == node_id]

    def add_artifact(self, project_id, kind, content, *, node_id=None, filename="artifact.txt"):
        aid = self._id("art")
        self.artifacts.append({"id": aid, "project_id": project_id, "node_id": node_id, "kind": kind, "filename": filename,
                               "content": content})
        return aid

    def list_artifacts(self, project_id, node_id=None):
        return [a for a in self.artifacts if a["project_id"] == project_id and node_id in (None, a["node_id"])]

    def save_model_profile(self, name, reasoner, coder, *, swap_cost_seconds=None):
        self.profiles[name] = {"name": name, "reasoner": reasoner, "coder": coder, "swap_cost_seconds": swap_cost_seconds}

    def get_model_profile(self, name):
        if name in self.profiles:
            return copy.deepcopy(self.profiles[name])
        if self.missing_profile == "raise":
            raise KeyError(name)
        return None

    def close(self):
        self.closed = True

    # -- test conveniences (not part of the Store interface) ---------------------------------------------------
    def events_of(self, etype, project_id=None):
        return [e for e in self.events if e["type"] == etype and project_id in (None, e["project_id"])]


class FakeStoreWithStatus(FakeStore):
    """A store that also has set_project_status (WP3 proposal 1)."""

    def set_project_status(self, project_id, status):
        self.projects[project_id]["status"] = status

"""SCH-6: processing rows of the `commands` table (written by the web process, STO-3).

Actions (spaces, dashes and case are ignored, so "cancel job" == "cancel_job"):

  pause / resume    project (command's project_id); payload {"scope": "runner"} pauses the whole runner
  cancel_job        payload {"job_id": ...}, or the command's node_id (cancels every active job of the node)
  retry_node        node_id: failed | escalated | stale -> ready, with a fresh budget
  approve / reject  node_id, payload {"stage": "component-map"|"interfaces"|"component-spec"|"escalation", "note": ...,
                    "cascade": bool}. Records an Approval; see `_approve` for the node status effects.
"""
from __future__ import annotations

from .util import as_obj, is_invalid_transition

STAGES = ("component-map", "interfaces", "component-spec", "escalation")


def normalize(action: str) -> str:
    return str(action).strip().lower().replace(" ", "_").replace("-", "_")


class CommandProcessor:
    def __init__(self, runner):
        self.r = runner

    def process(self, cmd: dict) -> str | None:
        """Run one command. Returns None on success or an error string; never raises."""
        action = normalize(cmd.get("action", ""))
        payload = as_obj(cmd.get("payload"), {}) or {}
        pid, nid = cmd.get("project_id"), cmd.get("node_id")
        try:
            fn = getattr(self, f"_{action}", None)
            if fn is None:
                return f"unsupported action {cmd.get('action')!r}"
            return fn(pid, nid, payload)
        except Exception as e:
            self.r.store.record_event(pid, "command.error", f"{action} failed: {type(e).__name__}: {e}",
                                      node_id=nid, level="error", data={"command": cmd.get("id")})
            return f"{type(e).__name__}: {e}"

    # -- pause / resume ---------------------------------------------------------------------------------------
    def _pause(self, pid, nid, payload):
        if payload.get("scope") == "runner":
            self.r.set_runner_paused(True, pid)
        else:
            self.r.set_project_paused(pid, True)

    def _resume(self, pid, nid, payload):
        if payload.get("scope") == "runner":
            self.r.set_runner_paused(False, pid)
        else:
            self.r.set_project_paused(pid, False)

    # -- cancel ---------------------------------------------------------------------------------------------------
    def _cancel_job(self, pid, nid, payload):
        if payload.get("job_id"):
            return None if self.r.cancel_job(payload["job_id"], "cancel") else f"job {payload['job_id']} is not active"
        if nid:
            n = 0
            for st in ("queued", "running"):
                for j in self.r.store.list_jobs(pid, status=st, limit=5000):
                    if j.get("node_id") == nid:
                        n += bool(self.r.cancel_job(j["id"], "cancel"))
            return None if n else f"node {nid} has no active job"
        return "cancel_job needs payload.job_id or a node_id"

    # -- retry ----------------------------------------------------------------------------------------------------
    def _retry_node(self, pid, nid, payload):
        if not nid:
            return "retry_node needs a node_id"
        node = self.r.store.get_node(nid)
        if node is None:
            return f"node {nid} not found"
        if node["status"] not in ("failed", "escalated", "stale"):
            return f"node {nid} is {node['status']}; only failed, escalated or stale nodes can be retried"
        base = self.r.tracker.reset(nid, int(node.get("attempts") or 0))
        self.r.store.transition_node_status(nid, "ready", note="owner retry",
                                            data={"budget_reset": True, "attempts_base": base.attempts})
        self.r.store.record_event(pid, "node.retry", f"retry requested for {node.get('name', nid)}; budget reset",
                                  node_id=nid, data={"attempts_base": base.attempts, "tokens_base": base.tokens,
                                                     "seconds_base": base.seconds})
        self.r.mark_dirty()

    # -- approvals ------------------------------------------------------------------------------------------------
    def _approve(self, pid, nid, payload):
        return self._decide(pid, nid, payload, "approved")

    def _reject(self, pid, nid, payload):
        return self._decide(pid, nid, payload, "rejected")

    def _decide(self, pid, nid, payload, decision):
        """Always records an Approval row. Status effects, so the pipelines need no private channel:

        approve, stage component-spec, node awaiting_approval -> approved (cascade: every awaiting child too)
        approve, stage escalation,     node escalated         -> ready (fresh budget)
        reject,  stage component-spec, node awaiting_approval -> stale -> draft (STO-4 has no direct edge); the note is in
                 the event so the spec pipeline can feed it back into the rewrite
        any other stage: the Approval row (and an `approval.recorded` event) is the whole effect; SS-SPEC reads it.
        """
        stage, note = payload.get("stage"), payload.get("note", "")
        if not nid:
            return f"{decision} needs a node_id"
        if stage not in STAGES:
            return f"{decision} needs payload.stage, one of {', '.join(STAGES)}"
        s = self.r.store
        targets = [nid]
        if payload.get("cascade"):
            targets += [k["id"] for k in s.list_nodes(pid, parent_id=nid) if k["status"] == "awaiting_approval"]
        for t in targets:
            s.add_approval(t, stage, decision, note=note)
            node = s.get_node(t)
            s.record_event(pid, "approval.recorded", f"{decision} {stage}" + (f": {note}" if note else ""), node_id=t,
                           data={"stage": stage, "decision": decision, "note": note})
            if node is None:
                continue
            if stage == "component-spec" and node["status"] == "awaiting_approval":
                if decision == "approved":
                    self._move(t, "approved", note or "owner approved")
                else:
                    self._move(t, "stale", note or "owner rejected")
                    self._move(t, "draft", note or "owner rejected")
            elif stage == "escalation" and decision == "approved" and node["status"] == "escalated":
                self.r.tracker.reset(t, int(node.get("attempts") or 0))
                self._move(t, "ready", note or "escalation resolved")
        self.r.mark_dirty()

    def _move(self, nid, status, note):
        try:
            self.r.store.transition_node_status(nid, status, note=note)
        except Exception as e:
            if not is_invalid_transition(e):
                raise

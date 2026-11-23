"""SCH-6: processing rows of the `commands` table (written by the web process, STO-3).

Actions (spaces, dashes and case are ignored, so "cancel job" == "cancel_job"):

  pause / resume    project (command's project_id); payload {"scope": "runner"} pauses the whole runner
  cancel_job        payload {"job_id": ...}, or the command's node_id (cancels every active job of the node)
  cancel            cancel all active jobs and mark the project cancelled (or cancel one node's jobs)
  archive/unarchive move the project to archived/active (owner hides test projects from the default list)
  answer_intake     payload {"answers": {"Q1": "...", ...}}: owner answers Info Gathering questions
  begin_design      force the project out of Info Gathering into the spec pipeline
  retry_node        node_id: failed | escalated | stale -> ready, with a fresh budget
  rerun_stage       node_id, payload {"stage": "spec"|"build"|"test"|"verify"}: enqueue that job again
  reorder           payload {"order": [node_id, ...]}: persist the owner's priority order
  approve / reject  node_id, payload {"stage": "component-map"|"interfaces"|"component-spec"|"escalation", "note": ...,
                    "cascade": bool}. Records an Approval; see `_approve` for the node status effects.
"""
from __future__ import annotations

from .util import as_obj, is_invalid_transition

STAGES = ("component-map", "interfaces", "component-spec", "escalation")
STAGE_JOBS = {
    "spec": ("component_spec", "reasoner"),
    "build": ("implement", "coder"),
    "test": ("author_tests", "reasoner"),
    "verify": ("verify", "coder"),
}


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

    def _cancel(self, pid, nid, payload):
        """Cancel active jobs; a project-wide cancel also leaves `active` state."""
        jobs = []
        for status in ("queued", "running"):
            jobs.extend(self.r.store.list_jobs(pid, status=status, limit=5000))
        if nid:
            jobs = [j for j in jobs if j.get("node_id") == nid]
        for job in jobs:
            self.r.cancel_job(job["id"], "cancel")
        if not nid:
            self.r.store.set_project_status(pid, "cancelled")
            self.r.store.record_event(pid, "project.cancelled", "Project work cancelled by owner")
        self.r.mark_dirty()

    def _archive(self, pid, nid, payload):
        self.r.store.set_project_status(pid, "archived")
        self.r.store.record_event(pid, "project.archived", "Project archived by owner")
        self.r.mark_dirty()

    def _unarchive(self, pid, nid, payload):
        self.r.store.set_project_status(pid, "active")
        self.r.store.record_event(pid, "project.unarchived", "Project restored to active by owner")
        self.r.mark_dirty()

    def _update_settings(self, pid, mutate):
        project = self.r.store.get_project(pid)
        if project is None:
            return f"project {pid} not found"
        settings = dict(project.get("settings") or {})
        mutate(settings)
        self.r.store.update_project(pid, settings=settings)
        self.r.mark_dirty()
        return None

    def _answer_intake(self, pid, nid, payload):
        answers = as_obj(payload.get("answers"), {}) or {}
        if not answers:
            return "answer_intake needs payload.answers"

        def mutate(settings):
            intake = dict(settings.get("intake") or {})
            saved = dict(intake.get("answers") or {})
            saved.update({str(k): str(v).strip() for k, v in answers.items() if str(v).strip()})
            intake["answers"] = saved
            settings["intake"] = intake
            settings["intake_waiting"] = False

        error = self._update_settings(pid, mutate)
        if error:
            return error
        self.r.store.record_event(pid, "project.intake_answers", "Owner answered Info Gathering questions",
                                  data={"question_ids": sorted(answers)})

    def _begin_design(self, pid, nid, payload):
        project = self.r.store.get_project(pid)
        if project is None:
            return f"project {pid} not found"
        settings_before = dict(project.get("settings") or {})
        intake = dict(settings_before.get("intake") or {})
        answers = {str(k): str(v).strip() for k, v in (intake.get("answers") or {}).items() if str(v).strip()}

        def mutate(settings):
            settings["intake_complete"] = True
            settings["intake_waiting"] = False
            settings["intake_forced"] = True
            settings["clarified"] = True

        error = self._update_settings(pid, mutate)
        if error:
            return error
        self.r.store.record_event(pid, "project.design_forced", "Owner started design/spec-ing",
                                  data={"force_refine": bool(answers)})
        if not answers:
            return None

        # The owner answered questions, so make the intake model fold those
        # answers into the root brief before any design job runs.
        roots = self.r.store.list_nodes(pid, parent_id=None) or []
        if not roots:
            return None
        active = [j for status in ("queued", "running")
                  for j in self.r.store.list_jobs(pid, status=status, limit=5000)
                  if j.get("kind") == "intake"]
        if not active:
            self.r.store.create_job(
                pid, "intake", "reasoner", node_id=roots[0]["id"],
                input_ref={"force_refine": True, "force_design": True},
            )

    def _rerun_stage(self, pid, nid, payload):
        stage = str(payload.get("stage") or "")
        if not nid:
            return "rerun_stage needs a node_id"
        if stage not in STAGE_JOBS:
            return f"unknown stage {stage!r}; expected one of {', '.join(STAGE_JOBS)}"
        if self.r.store.get_node(nid) is None:
            return f"node {nid} not found"
        kind, role = STAGE_JOBS[stage]
        active = [j for s in ("queued", "running")
                  for j in self.r.store.list_jobs(pid, status=s, limit=5000)
                  if j.get("node_id") == nid and j.get("kind") == kind]
        if not active:
            self.r.store.create_job(pid, kind, role, node_id=nid, input_ref={"stage": stage, "rerun": True})
            self.r.mark_dirty()

    def _reorder(self, pid, nid, payload):
        order = payload.get("order")
        if not isinstance(order, list):
            return "reorder needs payload.order as a list"
        project = self.r.store.get_project(pid)
        if project is None:
            return f"project {pid} not found"
        settings = dict(project.get("settings") or {})
        settings["node_order"] = [str(x) for x in order]
        self.r.store.update_project(pid, settings=settings)
        self.r.mark_dirty()

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

        approve, stage component-map|interfaces|component-spec,
                                        node awaiting_approval -> approved (cascade: every awaiting child too)
        approve, stage escalation,      node escalated|blocked -> ready (fresh budget)
        reject,  any spec stage,        node awaiting_approval -> stale -> draft (STO-4 has no direct edge);
                 the note is in the event so the spec pipeline can feed it back into the rewrite
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
            if stage in ("component-map", "interfaces", "component-spec") and node["status"] == "awaiting_approval":
                if decision == "approved":
                    self._move(t, "approved", note or f"owner approved {stage}")
                    base = self.r.tracker.reset(t, int(node.get("attempts") or 0))
                    self.r.store.record_event(
                        pid, "node.budget_reset_on_approval", "Owner approval grants a fresh budget",
                        node_id=t, data={"stage": stage, "attempts_base": base.attempts,
                                         "tokens_base": base.tokens, "seconds_base": base.seconds},
                    )
                else:
                    self._move(t, "stale", note or "owner rejected")
                    self._move(t, "draft", note or "owner rejected")
            elif stage == "escalation" and decision == "approved" and node["status"] in ("escalated", "blocked"):
                # Amended SCH-5: owner retry also unblocks a budget-blocked node.
                self.r.tracker.reset(t, int(node.get("attempts") or 0))
                self._move(t, "ready", note or "escalation resolved / budget retry")
        self.r.mark_dirty()

    def _move(self, nid, status, note):
        try:
            self.r.store.transition_node_status(nid, status, note=note)
        except Exception as e:
            if not is_invalid_transition(e):
                raise

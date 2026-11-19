"""Metrics computation for WP8."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any


def build_project_metrics(store: Any, project: dict[str, Any]) -> dict[str, Any]:
    """Build metrics for a project."""
    jobs = store.list_jobs(project["id"], limit=10000)
    nodes = store.list_nodes(project["id"])

    if not jobs:
        return _empty_metrics()

    total_tokens = sum((j.get("tokens_in") or 0) + (j.get("tokens_out") or 0) for j in jobs)
    completed = [j for j in jobs if j["status"] == "done"]
    failed = [j for j in jobs if j["status"] == "failed"]

    total_duration = 0.0
    for j in jobs:
        if j.get("started_at") and j.get("ended_at"):
            try:
                start = datetime.fromisoformat(j["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(j["ended_at"].replace("Z", "+00:00"))
                total_duration += (end - start).total_seconds()
            except (ValueError, TypeError):
                pass

    by_kind = {}
    for node in nodes:
        kind = node.get("kind", "unknown")
        node_jobs = [j for j in jobs if j.get("node_id") == node["id"]]
        if kind not in by_kind:
            by_kind[kind] = {"total": 0, "done": 0, "failed": 0, "running": 0, "tokens": 0, "durations": []}
        k = by_kind[kind]
        k["total"] += len(node_jobs)
        for j in node_jobs:
            if j["status"] == "done":
                k["done"] += 1
            elif j["status"] == "failed":
                k["failed"] += 1
            elif j["status"] == "running":
                k["running"] += 1
            k["tokens"] += (j.get("tokens_in") or 0) + (j.get("tokens_out") or 0)
            if j.get("started_at") and j.get("ended_at"):
                try:
                    start = datetime.fromisoformat(j["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(j["ended_at"].replace("Z", "+00:00"))
                    k["durations"].append((end - start).total_seconds())
                except (ValueError, TypeError):
                    pass

    by_kind_result = []
    for kind, data in by_kind.items():
        success_rate = round(data["done"] * 100 / data["total"], 1) if data["total"] else 0
        avg_dur = round(sum(data["durations"]) / len(data["durations"]), 1) if data["durations"] else 0
        by_kind_result.append({
            "kind": kind,
            "total": data["total"],
            "done": data["done"],
            "failed": data["failed"],
            "running": data["running"],
            "tokens": data["tokens"],
            "avg_duration": avg_dur,
            "success_rate": success_rate,
        })

    by_role = {}
    for j in jobs:
        role = j.get("model_role", "unknown")
        if role not in by_role:
            by_role[role] = {"jobs": 0, "tokens_in": 0, "tokens_out": 0, "durations": []}
        r = by_role[role]
        r["jobs"] += 1
        r["tokens_in"] += j.get("tokens_in") or 0
        r["tokens_out"] += j.get("tokens_out") or 0
        if j.get("started_at") and j.get("ended_at"):
            try:
                start = datetime.fromisoformat(j["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(j["ended_at"].replace("Z", "+00:00"))
                r["durations"].append((end - start).total_seconds())
            except (ValueError, TypeError):
                pass

    by_role_result = []
    for role, data in by_role.items():
        avg_dur = round(sum(data["durations"]) / len(data["durations"]), 1) if data["durations"] else 0
        by_role_result.append({
            "role": role,
            "jobs": data["jobs"],
            "tokens_in": data["tokens_in"],
            "tokens_out": data["tokens_out"],
            "avg_duration": avg_dur,
        })

    daily = {}
    for j in jobs:
        if j.get("ended_at"):
            try:
                d = datetime.fromisoformat(j["ended_at"].replace("Z", "+00:00")).date().isoformat()
            except (ValueError, TypeError):
                continue
            if d not in daily:
                daily[d] = {"jobs": 0, "tokens": 0, "durations": []}
            daily[d]["jobs"] += 1
            daily[d]["tokens"] += (j.get("tokens_in") or 0) + (j.get("tokens_out") or 0)
            if j.get("started_at"):
                try:
                    start = datetime.fromisoformat(j["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(j["ended_at"].replace("Z", "+00:00"))
                    daily[d]["durations"].append((end - start).total_seconds())
                except (ValueError, TypeError):
                    pass

    daily_result = []
    for d in sorted(daily.keys()):
        data = daily[d]
        avg_dur = round(sum(data["durations"]) / len(data["durations"]), 1) if data["durations"] else 0
        daily_result.append({
            "date": d,
            "jobs": data["jobs"],
            "tokens": data["tokens"],
            "duration": avg_dur,
        })

    return {
        "total_tokens": total_tokens,
        "total_duration": total_duration,
        "total": len(jobs),
        "completed": len(completed),
        "failed": len(failed),
        "success_rate": round(len(completed) * 100 / len(jobs), 1) if jobs else 0,
        "avg_retries": round(sum(n.get("attempts", 1) - 1 for n in nodes) / len(nodes), 2) if nodes else 0,
        "by_kind": by_kind_result,
        "by_role": by_role_result,
        "daily": daily_result,
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "total_tokens": 0,
        "total_duration": 0.0,
        "total": 0,
        "completed": 0,
        "failed": 0,
        "success_rate": 0,
        "avg_retries": 0,
        "by_kind": [],
        "by_role": [],
        "daily": [],
    }


# For compatibility with WP7 imports
_all_nodes = []  # Placeholder
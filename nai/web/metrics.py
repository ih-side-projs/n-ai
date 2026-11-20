"""Deterministic presentation metrics derived from SS-STORE records."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(start: str | None, end: str | None) -> float | None:
    a, b = _parse_time(start), _parse_time(end)
    if not a or not b:
        return None
    return max(0.0, (b - a).total_seconds())


def _model_id_for_role(profile: dict[str, Any] | None, role: str) -> str:
    block = (profile or {}).get(role) or {}
    return str(block.get("model_id") or f"unattributed ({role})")


def build_project_metrics(store, project: dict[str, Any]) -> dict[str, Any]:
    project_id = project["id"]
    nodes = _all_nodes(store, project_id)
    node_kind = {n["id"]: n["kind"] for n in nodes}
    settings = project.get("settings") or {}
    profile_name = settings.get("model_profile") or settings.get("model_profile_name")
    profile = _read_model_profile(store, profile_name) if profile_name else None

    jobs = store.list_jobs(project_id, limit=10000)
    total_in = sum(int(j.get("tokens_in") or 0) for j in jobs)
    total_out = sum(int(j.get("tokens_out") or 0) for j in jobs)
    durations = [v for j in jobs if (v := _elapsed_seconds(j.get("started_at"), j.get("ended_at"))) is not None]
    completed = [j for j in jobs if j.get("status") in {"succeeded", "failed", "cancelled"}]
    succeeded = [j for j in completed if j.get("status") == "succeeded"]

    by_node: dict[str, dict[str, Any]] = defaultdict(lambda: {"tokens_in": 0, "tokens_out": 0, "wall_seconds": 0.0, "jobs": 0})
    by_model: dict[str, dict[str, Any]] = defaultdict(lambda: {"tokens_in": 0, "tokens_out": 0, "wall_seconds": 0.0, "jobs": 0, "role": None,
                                                                    "swap_count": 0, "swap_seconds": 0.0, "swap_metadata_available": False})
    by_kind: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "succeeded": 0, "failed": 0, "retries": 0})
    trend: dict[str, dict[str, int]] = defaultdict(lambda: {"tokens_in": 0, "tokens_out": 0, "jobs": 0})
    swap_count = 0
    swap_seconds = 0.0
    swap_data_seen = False

    for job in jobs:
        nid = job.get("node_id") or "(project)"
        node_bucket = by_node[nid]
        node_bucket["tokens_in"] += int(job.get("tokens_in") or 0)
        node_bucket["tokens_out"] += int(job.get("tokens_out") or 0)
        node_bucket["jobs"] += 1
        elapsed = _elapsed_seconds(job.get("started_at"), job.get("ended_at"))
        if elapsed is not None:
            node_bucket["wall_seconds"] += elapsed

        role = str(job.get("model_role") or "unknown")
        model_id = _model_id_for_role(profile, role)
        # Some newer runners may include the concrete model id in result_ref.
        result_ref = job.get("result_ref") or {}
        input_ref = job.get("input_ref") or {}
        model_id = str(result_ref.get("model_id") or input_ref.get("model_id") or model_id)
        model_bucket = by_model[model_id]
        model_bucket["role"] = role
        model_bucket["tokens_in"] += int(job.get("tokens_in") or 0)
        model_bucket["tokens_out"] += int(job.get("tokens_out") or 0)
        model_bucket["jobs"] += 1
        if elapsed is not None:
            model_bucket["wall_seconds"] += elapsed

        kind = node_kind.get(job.get("node_id"), "project")
        kind_bucket = by_kind[kind]
        kind_bucket["total"] += 1
        kind_bucket["succeeded"] += int(job.get("status") == "succeeded")
        kind_bucket["failed"] += int(job.get("status") == "failed")
        kind_bucket["retries"] += max(0, int(job.get("attempt") or 1) - 1)

        stamp = _parse_time(job.get("ended_at") or job.get("created_at"))
        if stamp:
            day = stamp.astimezone(timezone.utc).date().isoformat()
            trend[day]["tokens_in"] += int(job.get("tokens_in") or 0)
            trend[day]["tokens_out"] += int(job.get("tokens_out") or 0)
            trend[day]["jobs"] += 1

        # Accept explicit swap metadata if/when the runner records it.
        swap_marker = result_ref.get("model_swap", result_ref.get("swap_before"))
        if swap_marker is not None:
            swap_data_seen = True
            model_bucket["swap_metadata_available"] = True
            if bool(swap_marker):
                swap_count += 1
                model_bucket["swap_count"] += 1
        swap_duration = result_ref.get("swap_seconds")
        if isinstance(swap_duration, (int, float)):
            swap_data_seen = True
            duration = max(0.0, float(swap_duration))
            swap_seconds += duration
            model_bucket["swap_metadata_available"] = True
            model_bucket["swap_seconds"] += duration

    for bucket in by_node.values():
        bucket["wall_seconds"] = round(bucket["wall_seconds"], 3)
    for bucket in by_model.values():
        bucket["wall_seconds"] = round(bucket["wall_seconds"], 3)
        bucket["swap_seconds"] = round(bucket["swap_seconds"], 3)
    for kind, bucket in by_kind.items():
        bucket["success_rate"] = round(bucket["succeeded"] / bucket["total"], 4) if bucket["total"] else None

    return {
        "project_id": project_id,
        "job_count": len(jobs),
        "completed_jobs": len(completed),
        "successful_jobs": len(succeeded),
        "success_rate": round(len(succeeded) / len(completed), 4) if completed else None,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "wall_seconds": round(sum(durations), 3),
        "retries": sum(max(0, int(j.get("attempt") or 1) - 1) for j in jobs),
        "by_node": dict(sorted(by_node.items())),
        "by_model": dict(sorted(by_model.items())),
        "by_node_kind": dict(sorted(by_kind.items())),
        "trend": [{"date": day, **values} for day, values in sorted(trend.items())],
        "model_swaps": {"count": swap_count if swap_data_seen else None,
                        "seconds": round(swap_seconds, 3) if swap_data_seen else None,
                        "available": swap_data_seen},
        "model_profile": profile_name,
        "note": "Swap metrics remain unavailable until the runner records swap metadata in job result_ref.",
        # WP8-compatible aliases used by the metrics template
        "total_tokens": total_in + total_out,
        "total_duration": round(sum(durations), 3),
        "total": len(jobs),
        "completed": len(completed),
        "avg_retries": round((sum(max(0, int(j.get("attempt") or 1) - 1) for j in jobs) / len(jobs)) if jobs else 0, 2),
        "by_kind": [
            {"kind": k, **v} for k, v in sorted(by_kind.items())
        ],
        "by_role": [
            {"role": k, **v} for k, v in sorted(by_model.items())
        ],
        "daily": [
            {"date": date, "jobs": t.get("jobs", 0),
             "tokens": t.get("tokens_in", 0) + t.get("tokens_out", 0),
             "duration": t.get("wall_seconds", 0)}
            for date, t in sorted(trend.items())
        ] if trend else [],
    }


def _all_nodes(store, project_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(parent_id: str | None) -> None:
        for node in store.list_nodes(project_id, parent_id=parent_id):
            if not node or node["id"] in seen:
                continue
            seen.add(node["id"])
            out.append(node)
            visit(node["id"])

    visit(None)
    return out


def _read_model_profile(store, name: str) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT name, reasoner_json, coder_json, swap_cost_seconds FROM model_profiles WHERE name=?", (name,)
    ).fetchone()
    if not row:
        return None
    import json
    return {
        "name": row["name"],
        "reasoner": json.loads(row["reasoner_json"]),
        "coder": json.loads(row["coder_json"]),
        "swap_cost_seconds": row["swap_cost_seconds"],
    }

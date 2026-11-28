"""CLI helpers for the deterministic project layout: ``nai where`` / ``nai relocate``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import manifest, paths
from .store import Store, StoreError


def _store(args: argparse.Namespace, *, read_only: bool) -> Store:
    return Store(Path(args.db).expanduser(), read_only=read_only)


def where_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nai where", description="Show where a project lives.")
    ap.add_argument("project", help="Project id, slug or name")
    ap.add_argument("--db", default=".nai/nai.sqlite3", help="SQLite store (default .nai/nai.sqlite3)")
    ap.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    # Writable so an upgraded database is migrated/backfilled before lookup.
    with _store(args, read_only=False) as store:
        project = store.find_project(args.project)
        if project is None:
            print(f"error: project not found: {args.project}", file=sys.stderr)
            return 2
        mismatches = manifest.verify(project["workspace_path"])
        payload = {
            "id": project["id"],
            "name": project["name"],
            "slug": project.get("slug"),
            "output_root": project.get("output_root"),
            "project_root": project["workspace_path"],
            "manifest_mismatches": [m.relpath for m in mismatches],
        }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"project:      {payload['name']} ({payload['id']})")
        print(f"slug:         {payload['slug']}")
        print(f"output root:  {payload['output_root']}")
        print(f"project root: {payload['project_root']}")
        if payload["manifest_mismatches"]:
            print("manifest:     hand edits detected: " + ", ".join(payload["manifest_mismatches"]))
        else:
            print("manifest:     clean")
    return 0


def relocate_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nai relocate", description="Move a project to a new output root.")
    ap.add_argument("project", help="Project id, slug or name")
    ap.add_argument("directory", help="New output root")
    ap.add_argument("--db", default=".nai/nai.sqlite3", help="SQLite store (default .nai/nai.sqlite3)")
    ap.add_argument("--force", action="store_true", help="Accept current file hashes when the manifest disagrees")
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    try:
        with _store(args, read_only=False) as store:
            project = store.find_project(args.project)
            if project is None:
                print(f"error: project not found: {args.project}", file=sys.stderr)
                return 2
            target = store.relocate_project(project["id"], args.directory, force=args.force)
    except (paths.LayoutError, StoreError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"relocated {args.project} to {target}")
    return 0

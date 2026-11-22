"""SQLite-backed persistence for N-AI (SS-STORE).

The API is intentionally independent of the core library so WP2 can be used
before WP1 is merged. It uses only Python's standard library.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import (
    DependencyCycleError,
    DependencyError,
    ExportImportError,
    InvalidTransition,
    StoreError,
)
from .hashing import canonical_json, content_hash, sha1_text

NODE_STATUSES = {
    "draft", "awaiting_approval", "approved", "ready", "running", "verifying",
    "done", "failed", "escalated", "blocked", "stale",
}
NODE_KINDS = {"system", "subsystem", "component", "leaf", "integration"}
JOB_KINDS = {
    "clarify", "decompose", "select_map", "interfaces", "check_interfaces",
    "component_spec", "lint", "reconcile",
    "contract_stub", "author_tests", "implement", "verify", "integrate", "diagnose", "review",
}
ARTIFACT_KINDS = {"spec", "contract", "code", "test", "output-file", "report", "log"}
APPROVAL_STAGES = {"component-map", "interfaces", "component-spec", "escalation"}
COMMAND_ACTIONS = {
    "pause", "resume", "cancel_job", "cancel", "retry_node", "approve", "reject",
    "rerun_stage", "reorder",
}

# STO-4. A transition to stale is permitted from every state, including done.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"awaiting_approval", "stale"},
    "awaiting_approval": {"approved", "stale"},
    "approved": {"ready", "stale"},
    "ready": {"running", "blocked", "stale"},
    "running": {"verifying", "failed", "blocked", "stale"},
    "verifying": {"done", "failed", "blocked", "stale"},
    "done": {"stale"},
    "failed": {"ready", "escalated", "stale"},
    "escalated": {"ready", "stale"},
    "blocked": {"failed", "ready", "stale"},
    "stale": {"draft", "ready"},
}


def utc_now() -> str:
    """UTC ISO-8601 timestamp with a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> str:
    """Short globally-unique identifier suitable for SQLite primary keys."""
    return uuid.uuid4().hex[:12]


def _json_object(value: Any, label: str) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return canonical_json(value)


def _decode_json(row: dict[str, Any], source: str, target: str) -> None:
    raw = row.pop(source, "{}")
    row[target] = json.loads(raw) if raw else {}


def _safe_relative_path(value: str) -> Path:
    """Validate a relative POSIX-style workspace path."""
    p = PurePosixPath(value.replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(part in ("", ".", "..") for part in p.parts):
        raise ExportImportError(f"unsafe artifact path: {value!r}")
    return Path(*p.parts)


def _safe_filename(value: str) -> str:
    name = Path(value).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "artifact.bin"


class Store:
    """SQLite store with migrations, status transitions, hashes and project bundles.

    Args:
        db_path: SQLite database path.
        read_only: Open the main connection in SQLite read-only mode. The only
            write exception is :meth:`enqueue_command`, which opens a short-lived
            connection restricted to appending a control command. This supports
            STO-3's read-only web query connection and command inbox.
        backup_dir: Optional directory for daily database snapshots.
        backup_keep: Number of daily snapshots to retain (oldest removed first).
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = False,
        backup_dir: str | Path | None = None,
        backup_keep: int = 7,
        migration_dir: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.read_only = bool(read_only)
        self.backup_dir = Path(backup_dir).expanduser().resolve() if backup_dir else None
        self.backup_keep = int(backup_keep)
        self._backup_lock = threading.RLock()
        self._backup_stop: threading.Event | None = None
        self._backup_thread: threading.Thread | None = None
        self.last_backup_error: str | None = None
        if self.backup_keep < 1:
            raise ValueError("backup_keep must be at least 1")
        if self.read_only and not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        if not self.read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if self.read_only:
            uri = self.db_path.as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
        else:
            self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        if self.read_only:
            self.conn.execute("PRAGMA query_only = ON")
        else:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate(Path(migration_dir) if migration_dir else Path(__file__).with_name("migrations"))
            if self.backup_dir:
                self.backup_if_due()
                self._start_backup_scheduler()

    # ------------------------------------------------------------------
    # Lifecycle, transactions, migrations
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._backup_stop is not None:
            self._backup_stop.set()
        if self._backup_thread is not None and self._backup_thread.is_alive():
            self._backup_thread.join(timeout=5)
        self.conn.close()

    def _start_backup_scheduler(self) -> None:
        """Keep daily backups automatic even when the runner stays open."""
        if self.read_only or not self.backup_dir or self._backup_thread is not None:
            return
        self._backup_stop = threading.Event()

        def run() -> None:
            # Check periodically rather than sleeping until midnight so a
            # transient backup error is retried without requiring a restart.
            while self._backup_stop is not None and not self._backup_stop.wait(60):
                try:
                    self.backup_if_due()
                    self.last_backup_error = None
                except Exception as exc:  # backup failure must not kill the runner
                    self.last_backup_error = f"{type(exc).__name__}: {exc}"

        self._backup_thread = threading.Thread(target=run, name="nai-daily-backup", daemon=True)
        self._backup_thread.start()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise StoreError("store is read-only")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _migrate(self, migration_dir: Path) -> None:
        migration_dir = migration_dir.resolve()
        if not migration_dir.is_dir():
            raise StoreError(f"migration directory not found: {migration_dir}")
        files = sorted(migration_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if not files:
            raise StoreError(f"no numbered SQL migrations in {migration_dir}")
        current_exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        current = 0
        if current_exists:
            row = self.conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
            current = int(row["v"])
        available = max(int(re.match(r"^(\d{4})_", path.name).group(1)) for path in files)
        if current > available:
            raise StoreError(f"database schema version {current} is newer than this package ({available})")
        expected = current + 1
        for path in files:
            match = re.match(r"^(\d{4})_", path.name)
            version = int(match.group(1)) if match else -1
            if version <= current:
                continue
            if version != expected:
                raise StoreError(f"migration gap: expected {expected:04d}, found {path.name}")
            script = path.read_text(encoding="utf-8")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._execute_sql_script(script)
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, path.name, utc_now()),
                )
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()
            current = version
            expected = current + 1

    def _execute_sql_script(self, script: str) -> None:
        """Execute a migration without executescript's implicit transaction commit."""
        statement = ""
        for line in script.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                statement = ""
                if sql:
                    self.conn.execute(sql)
        if statement.strip():
            raise StoreError("incomplete SQL statement at end of migration")

    @property
    def schema_version(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
        return int(row["v"])

    # ------------------------------------------------------------------
    # Events (append-only)
    # ------------------------------------------------------------------
    def _insert_event(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        *,
        node_id: str | None = None,
        job_id: str | None = None,
        level: str = "info",
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
        time: str | None = None,
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or new_id()
        conn.execute(
            """INSERT INTO events(id, project_id, node_id, job_id, time, level, type, message, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, project_id, node_id, job_id, time or utc_now(), level, event_type,
             message, _json_object(data if data is not None else {}, "event data")),
        )
        return event_id

    def record_event(
        self,
        project_id: str,
        event_type: str,
        message: str,
        *,
        node_id: str | None = None,
        job_id: str | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> str:
        with self._transaction() as conn:
            return self._insert_event(conn, project_id, node_id=node_id, job_id=job_id,
                                      level=level, event_type=event_type, message=message, data=data)

    def list_events(
        self, project_id: str, *, node_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return the most recent `limit` events, in chronological order.

        Newest-first selection matters on long projects: callers restoring pause
        state or budget bases must see recent events, not the oldest ones.
        """
        if node_id:
            rows = self.conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE project_id=? AND node_id=? "
                "ORDER BY time DESC, id DESC LIMIT ?) ORDER BY time, id",
                (project_id, node_id, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE project_id=? "
                "ORDER BY time DESC, id DESC LIMIT ?) ORDER BY time, id",
                (project_id, int(limit)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            _decode_json(item, "data_json", "data")
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Projects and nodes
    # ------------------------------------------------------------------
    def create_project(
        self,
        name: str,
        brief: str = "",
        workspace_path: str | Path | None = None,
        *,
        settings: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> str:
        project_id = project_id or new_id()
        workspace = Path(workspace_path).expanduser().resolve() if workspace_path else (
            self.db_path.parent / "workspaces" / project_id
        )
        workspace.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO projects(id, name, brief, workspace_path, status, created_at, settings_json)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (project_id, name, brief, str(workspace), utc_now(), _json_object(settings if settings is not None else {}, "settings")),
            )
            self._insert_event(conn, project_id, event_type="project.created", message=f"Project '{name}' created")
        return project_id

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        _decode_json(item, "settings_json", "settings")
        return item

    def list_projects(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute("SELECT * FROM projects WHERE status=? ORDER BY created_at, id", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            _decode_json(item, "settings_json", "settings")
            out.append(item)
        return out

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        brief: str | None = None,
        workspace_path: str | Path | None = None,
        status: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        current = self.get_project(project_id)
        if current is None:
            raise KeyError(f"unknown project: {project_id}")
        values = {
            "name": name if name is not None else current["name"],
            "brief": brief if brief is not None else current["brief"],
            "workspace_path": str(Path(workspace_path).expanduser().resolve()) if workspace_path is not None else current["workspace_path"],
            "status": status if status is not None else current["status"],
            "settings_json": _json_object(settings, "settings") if settings is not None else canonical_json(current["settings"]),
        }
        if values["status"] not in {"active", "paused", "done", "archived"}:
            raise ValueError(f"invalid project status: {values['status']}")
        with self._transaction() as conn:
            conn.execute(
                "UPDATE projects SET name=?, brief=?, workspace_path=?, status=?, settings_json=? WHERE id=?",
                (values["name"], values["brief"], values["workspace_path"], values["status"], values["settings_json"], project_id),
            )
            self._insert_event(conn, project_id, event_type="project.updated", message="Project details updated")

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
        depends_on: Sequence[str] = (),
        test_command: str | None = None,
        node_id: str | None = None,
    ) -> str:
        if kind not in NODE_KINDS:
            raise ValueError(f"invalid node kind: {kind}")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError("node name must be lowercase-hyphenated")
        node_id = node_id or new_id()
        now = utc_now()
        contract_json = _json_object(contract if contract is not None else {}, "contract")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(f"unknown project: {project_id}")
            self._validate_dependencies(conn, project_id, parent_id, node_id, depends_on)
            conn.execute(
                """INSERT INTO nodes(id, project_id, parent_id, kind, name, objective, spec_text,
                       contract_json, status, attempts, test_command, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, ?)""",
                (node_id, project_id, parent_id, kind, name, objective, spec_text, contract_json,
                 test_command, now, now),
            )
            for ordinal, dependency_id in enumerate(depends_on):
                conn.execute(
                    "INSERT INTO node_dependencies(node_id, dependency_node_id, ordinal) VALUES (?, ?, ?)",
                    (node_id, dependency_id, ordinal),
                )
            self._recompute_project_hashes(conn, project_id)
            self._insert_event(conn, project_id, node_id=node_id, event_type="node.created",
                               message=f"Node '{name}' created", data={"kind": kind, "parent_id": parent_id})
        return node_id

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        _decode_json(item, "contract_json", "contract")
        item["depends_on"] = [r["dependency_node_id"] for r in self.conn.execute(
            "SELECT dependency_node_id FROM node_dependencies WHERE node_id=? ORDER BY ordinal", (node_id,)
        ).fetchall()]
        return item

    def list_nodes(self, project_id: str, *, parent_id: str | None = None) -> list[dict[str, Any]]:
        if parent_id is None:
            rows = self.conn.execute(
                "SELECT id FROM nodes WHERE project_id=? AND parent_id IS NULL ORDER BY created_at, id", (project_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM nodes WHERE project_id=? AND parent_id=? ORDER BY created_at, id", (project_id, parent_id)
            ).fetchall()
        return [self.get_node(row["id"]) for row in rows]

    def update_node_spec(self, node_id: str, spec_text: str) -> None:
        self._update_node_field(node_id, "spec_text", spec_text)

    def update_node_contract(self, node_id: str, contract: dict[str, Any]) -> None:
        self._update_node_field(node_id, "contract_json", _json_object(contract, "contract"))

    def _update_node_field(self, node_id: str, field: str, value: str) -> None:
        if field not in {"spec_text", "contract_json"}:
            raise ValueError("unsupported node field")
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            conn.execute(f"UPDATE nodes SET {field}=?, updated_at=? WHERE id=?", (value, utc_now(), node_id))
            self._recompute_project_hashes(conn, row["project_id"])
            self._insert_event(conn, row["project_id"], node_id=node_id, event_type="node.spec_changed" if field == "spec_text" else "node.contract_changed",
                               message="Node specification updated" if field == "spec_text" else "Node contract updated")

    def set_node_dependencies(self, node_id: str, depends_on: Sequence[str]) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id, parent_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            self._validate_dependencies(conn, row["project_id"], row["parent_id"], node_id, depends_on)
            conn.execute("DELETE FROM node_dependencies WHERE node_id=?", (node_id,))
            for ordinal, dependency_id in enumerate(depends_on):
                conn.execute(
                    "INSERT INTO node_dependencies(node_id, dependency_node_id, ordinal) VALUES (?, ?, ?)",
                    (node_id, dependency_id, ordinal),
                )
            self._recompute_project_hashes(conn, row["project_id"])
            self._insert_event(conn, row["project_id"], node_id=node_id, event_type="node.dependencies_changed",
                               message="Node dependencies updated", data={"depends_on": list(depends_on)})

    def set_node_test_command(self, node_id: str, test_command: str | None) -> None:
        """Set the node's verification command (the command is not part of STO-5's hash)."""
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            conn.execute("UPDATE nodes SET test_command=?, updated_at=? WHERE id=?", (test_command, utc_now(), node_id))
            self._insert_event(conn, row["project_id"], node_id=node_id, event_type="node.test_command_changed",
                               message="Node test command updated", data={"test_command": test_command})

    def increment_node_attempts(self, node_id: str, amount: int = 1) -> int:
        """Increment the persisted node attempt counter and return its new value."""
        amount = int(amount)
        if amount < 1:
            raise ValueError("attempt increment must be positive")
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id, attempts FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            attempts = int(row["attempts"]) + amount
            conn.execute("UPDATE nodes SET attempts=?, updated_at=? WHERE id=?", (attempts, utc_now(), node_id))
            self._insert_event(conn, row["project_id"], node_id=node_id, event_type="node.attempts_updated",
                               message=f"Node attempt count is now {attempts}", data={"attempts": attempts})
            return attempts

    def _validate_dependencies(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        parent_id: str | None,
        node_id: str,
        depends_on: Sequence[str],
    ) -> None:
        if len(set(depends_on)) != len(depends_on):
            raise DependencyError("dependency list contains duplicates")
        if node_id in depends_on:
            raise DependencyError("a node cannot depend on itself")
        for dependency_id in depends_on:
            row = conn.execute(
                "SELECT project_id, parent_id FROM nodes WHERE id=?", (dependency_id,)
            ).fetchone()
            if row is None:
                raise DependencyError(f"unknown dependency node: {dependency_id}")
            if row["project_id"] != project_id or row["parent_id"] != parent_id:
                raise DependencyError("depends_on entries must be siblings in the same project")

    # ------------------------------------------------------------------
    # Content hashing and stale propagation (STO-5)
    # ------------------------------------------------------------------
    def recompute_project_hashes(self, project_id: str) -> dict[str, str]:
        with self._transaction() as conn:
            return self._recompute_project_hashes(conn, project_id)

    def _recompute_project_hashes(self, conn: sqlite3.Connection, project_id: str) -> dict[str, str]:
        rows = conn.execute("SELECT * FROM nodes WHERE project_id=?", (project_id,)).fetchall()
        by_id = {row["id"]: row for row in rows}
        if not rows:
            return {}
        dep_map: dict[str, list[str]] = {}
        for node_id in by_id:
            dep_map[node_id] = [r["dependency_node_id"] for r in conn.execute(
                "SELECT dependency_node_id FROM node_dependencies WHERE node_id=? ORDER BY ordinal", (node_id,)
            ).fetchall()]

        hashes: dict[str, str] = {}
        visiting: set[str] = set()

        def calculate(node_id: str) -> str:
            if node_id in hashes:
                return hashes[node_id]
            if node_id in visiting:
                raise DependencyCycleError(f"dependency cycle includes node {node_id}")
            visiting.add(node_id)
            row = by_id[node_id]
            parent_contract = None
            parent_id = row["parent_id"]
            if parent_id:
                if parent_id not in by_id:
                    raise StoreError(f"node {node_id} has missing parent {parent_id}")
                parent_contract = json.loads(by_id[parent_id]["contract_json"] or "{}")
                # Calculate the parent first to guarantee a stable hierarchy traversal.
                calculate(parent_id)
            dependency_hashes = []
            for dependency_id in dep_map[node_id]:
                if dependency_id not in by_id:
                    raise DependencyError(f"node {node_id} references missing dependency {dependency_id}")
                calculate(dependency_id)  # cycle detection + update the dependency itself
                # Only the dependency's interface participates (amended §4).
                dep_contract = json.loads(by_id[dependency_id]["contract_json"] or "{}")
                dependency_hashes.append(sha1_text(canonical_json(dep_contract)))
            new_hash = content_hash(
                row["spec_text"],
                json.loads(row["contract_json"] or "{}"),
                dependency_hashes,
                parent_contract,
            )
            hashes[node_id] = new_hash
            old_hash = row["content_hash"]
            if old_hash != new_hash:
                conn.execute("UPDATE nodes SET content_hash=?, updated_at=? WHERE id=?", (new_hash, utc_now(), node_id))
                if old_hash is not None:
                    self._mark_node_stale(conn, row, old_hash, new_hash)
                    self._insert_event(
                        conn, project_id, node_id=node_id, event_type="node.content_hash_changed",
                        message=f"Content hash changed for node '{row['name']}'",
                        data={"old_hash": old_hash, "new_hash": new_hash},
                    )
            visiting.remove(node_id)
            return new_hash

        # Parent-first recursion plus dependency recursion handles arbitrary row order.
        for node_id in sorted(by_id):
            calculate(node_id)
        return hashes

    def _mark_node_stale(self, conn: sqlite3.Connection, old_row: sqlite3.Row, old_hash: str, new_hash: str) -> None:
        # Route hash invalidation through the same authoritative status transition
        # function used by scheduler/owner actions (STO-4).
        rejected = self._apply_node_status_transition(
            conn,
            old_row["id"],
            "stale",
            note="content hash changed",
            data={"old_hash": old_hash, "new_hash": new_hash},
        )
        if rejected:
            raise StoreError(rejected)

    # ------------------------------------------------------------------
    # Node status machine (STO-4)
    # ------------------------------------------------------------------
    def _apply_node_status_transition(
        self,
        conn: sqlite3.Connection,
        node_id: str,
        new_status: str,
        *,
        note: str = "",
        data: dict[str, Any] | None = None,
    ) -> str | None:
        """The single function that validates and applies every node transition.

        Returns a rejection message rather than raising so its caller can commit
        the required audit event before surfacing :class:`InvalidTransition`.
        """
        if new_status not in NODE_STATUSES:
            return f"Unknown node status: {new_status}"
        row = conn.execute("SELECT project_id, name, status, content_hash FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown node: {node_id}")
        old_status = row["status"]
        if new_status == old_status:
            return None
        if new_status not in _ALLOWED_TRANSITIONS[old_status]:
            message = f"Invalid node status transition: {old_status} -> {new_status}"
            self._insert_event(
                conn, row["project_id"], node_id=node_id, level="warning",
                event_type="node.transition_rejected", message=message,
                data={"from": old_status, "to": new_status, "note": note},
            )
            return message
        now = utc_now()
        if new_status == "done":
            conn.execute(
                "UPDATE nodes SET status=?, built_hash=content_hash, updated_at=? WHERE id=?",
                (new_status, now, node_id),
            )
        else:
            conn.execute("UPDATE nodes SET status=?, updated_at=? WHERE id=?", (new_status, now, node_id))
        event_data = {"from": old_status, "to": new_status}
        if note:
            event_data["note"] = note
        if data:
            event_data.update(data)
        self._insert_event(
            conn, row["project_id"], node_id=node_id,
            event_type="node.status_changed", message=f"Node '{row['name']}' status: {old_status} -> {new_status}",
            data=event_data,
        )
        return None

    def transition_node_status(
        self,
        node_id: str,
        new_status: str,
        *,
        note: str = "",
        data: dict[str, Any] | None = None,
    ) -> str:
        """Apply the authoritative node-status transition function.

        Rejected transitions are committed as warning events before the
        :class:`InvalidTransition` is raised, so the audit row survives.
        """
        if new_status not in NODE_STATUSES:
            raise ValueError(f"unknown node status: {new_status}")
        rejected = None
        with self._transaction() as conn:
            row = conn.execute("SELECT status FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            current = row["status"]
            rejected = self._apply_node_status_transition(conn, node_id, new_status, note=note, data=data)
        if rejected:
            raise InvalidTransition(rejected)
        return new_status if current != new_status else current

    # ------------------------------------------------------------------
    # Requirements and test records
    # ------------------------------------------------------------------
    def replace_requirements(self, node_id: str, requirements: Sequence[dict[str, Any]]) -> None:
        seen: set[str] = set()
        normalized = []
        for item in requirements:
            requirement_id = str(item["id"])
            if requirement_id in seen:
                raise ValueError(f"duplicate requirement id: {requirement_id}")
            seen.add(requirement_id)
            normalized.append((requirement_id, str(item["text"]), int(bool(item.get("testable", True)))))
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            conn.execute("DELETE FROM requirements WHERE node_id=?", (node_id,))
            conn.executemany(
                "INSERT INTO requirements(node_id, requirement_id, text, testable) VALUES (?, ?, ?, ?)",
                [(node_id, rid, text, testable) for rid, text, testable in normalized],
            )
            self._insert_event(conn, row["project_id"], node_id=node_id, event_type="node.requirements_updated",
                               message=f"Stored {len(normalized)} requirements")

    def list_requirements(self, node_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT requirement_id AS id, node_id, text, testable FROM requirements WHERE node_id=? ORDER BY requirement_id",
            (node_id,),
        ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["testable"] = bool(item["testable"])
        return result

    def upsert_test_record(
        self,
        node_id: str,
        file_path: str,
        requirement_ids: Sequence[str],
        frozen_hash: str,
        *,
        last_result: str = "none",
        last_run_at: str | None = None,
    ) -> None:
        if last_result not in {"pass", "fail", "error", "none"}:
            raise ValueError(f"invalid test result: {last_result}")
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO test_records(node_id, file_path, requirement_ids_json, frozen_hash, last_result, last_run_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(node_id, file_path) DO UPDATE SET
                     requirement_ids_json=excluded.requirement_ids_json,
                     frozen_hash=excluded.frozen_hash,
                     last_result=excluded.last_result,
                     last_run_at=excluded.last_run_at""",
                (node_id, file_path, canonical_json(list(requirement_ids)), frozen_hash, last_result, last_run_at),
            )

    def list_test_records(self, node_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM test_records WHERE node_id=? ORDER BY file_path", (node_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            _decode_json(item, "requirement_ids_json", "requirement_ids")
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def create_job(
        self,
        project_id: str,
        kind: str,
        model_role: str,
        *,
        node_id: str | None = None,
        attempt: int = 1,
        input_ref: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> str:
        if kind not in JOB_KINDS:
            raise ValueError(f"invalid job kind: {kind}")
        if model_role not in {"reasoner", "coder"}:
            raise ValueError(f"invalid model role: {model_role}")
        job_id = job_id or new_id()
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO jobs(id, project_id, node_id, kind, model_role, status, attempt,
                       input_ref_json, result_ref_json, created_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?)""",
                (job_id, project_id, node_id, kind, model_role, int(attempt), _json_object(input_ref if input_ref is not None else {}, "input_ref"), utc_now()),
            )
            self._insert_event(conn, project_id, node_id=node_id, job_id=job_id,
                               event_type="job.queued", message=f"Queued {kind} job for {model_role}")
        return job_id

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {"status", "attempt", "result_ref", "input_ref", "tokens_in", "tokens_out", "started_at", "ended_at", "error"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {', '.join(sorted(unknown))}")
        if not changes:
            return
        values = dict(changes)
        if "result_ref" in values:
            values["result_ref_json"] = _json_object(values.pop("result_ref"), "result_ref")
        if "input_ref" in values:
            values["input_ref_json"] = _json_object(values.pop("input_ref"), "input_ref")
        if "status" in values and values["status"] not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid job status: {values['status']}")
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id, node_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown job: {job_id}")
            assignments = ", ".join(f"{field}=?" for field in values)
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
            self._insert_event(conn, row["project_id"], node_id=row["node_id"], job_id=job_id,
                               event_type="job.updated", message="Job updated", data={k: v for k, v in changes.items() if k != "result_ref"})

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        _decode_json(item, "input_ref_json", "input_ref")
        _decode_json(item, "result_ref_json", "result_ref")
        return item

    def list_jobs(
        self,
        project_id: str,
        *,
        status: str | None = None,
        model_role: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return the most recent `limit` matching jobs, in chronological order."""
        clauses, args = ["project_id=?"], [project_id]
        if status:
            clauses.append("status=?")
            args.append(status)
        if model_role:
            clauses.append("model_role=?")
            args.append(model_role)
        args.append(int(limit))
        rows = self.conn.execute(
            f"SELECT * FROM (SELECT * FROM jobs WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at, id", args
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            _decode_json(item, "input_ref_json", "input_ref")
            _decode_json(item, "result_ref_json", "result_ref")
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Artifacts and approvals
    # ------------------------------------------------------------------
    def add_artifact(
        self,
        project_id: str,
        kind: str,
        content: str | bytes,
        *,
        node_id: str | None = None,
        filename: str = "artifact.txt",
    ) -> str:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"invalid artifact kind: {kind}")
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        if node_id is not None:
            node = self.conn.execute("SELECT project_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if node is None or node["project_id"] != project_id:
                raise ValueError("artifact node_id must belong to the specified project")
        artifact_id = new_id()
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        relative = Path(".nai") / "artifacts" / _safe_filename(node_id or "project") / f"{artifact_id}-{_safe_filename(filename)}"
        workspace = Path(project["workspace_path"]).resolve()
        destination = (workspace / relative).resolve()
        if workspace != destination and workspace not in destination.parents:
            raise StoreError("artifact path escapes project workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(destination)
        digest = hashlib.sha1(data).hexdigest()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts(id, project_id, node_id, kind, path, sha1, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, project_id, node_id, kind, relative.as_posix(), digest, utc_now()),
            )
            self._insert_event(conn, project_id, node_id=node_id, event_type="artifact.created",
                               message=f"Stored {kind} artifact ({len(data)} bytes)", data={"artifact_id": artifact_id, "path": relative.as_posix(), "sha1": digest})
        return artifact_id

    def list_artifacts(self, project_id: str, *, node_id: str | None = None) -> list[dict[str, Any]]:
        if node_id:
            rows = self.conn.execute("SELECT * FROM artifacts WHERE project_id=? AND node_id=? ORDER BY created_at, id", (project_id, node_id)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at, id", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_approval(
        self,
        node_id: str,
        stage: str,
        decision: str,
        *,
        note: str = "",
        approval_id: str | None = None,
    ) -> str:
        if stage not in APPROVAL_STAGES:
            raise ValueError(f"invalid approval stage: {stage}")
        if decision not in {"approved", "rejected", "edited"}:
            raise ValueError(f"invalid approval decision: {decision}")
        approval_id = approval_id or new_id()
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown node: {node_id}")
            timestamp = utc_now()
            conn.execute(
                "INSERT INTO approvals(id, node_id, stage, decision, note, time) VALUES (?, ?, ?, ?, ?, ?)",
                (approval_id, node_id, stage, decision, note, timestamp),
            )
            self._insert_event(conn, row["project_id"], node_id=node_id, event_type="approval.recorded",
                               message=f"{stage} approval {decision}", data={"approval_id": approval_id, "stage": stage, "decision": decision, "note": note}, time=timestamp)
        return approval_id

    def list_approvals(self, node_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM approvals WHERE node_id=? ORDER BY time, id", (node_id,)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Model profiles and tool definitions
    # ------------------------------------------------------------------
    def save_model_profile(
        self,
        name: str,
        reasoner: dict[str, Any],
        coder: dict[str, Any],
        *,
        swap_cost_seconds: float | None = None,
    ) -> None:
        if swap_cost_seconds is not None and swap_cost_seconds < 0:
            raise ValueError("swap_cost_seconds cannot be negative")
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO model_profiles(name, reasoner_json, coder_json, swap_cost_seconds)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET reasoner_json=excluded.reasoner_json,
                     coder_json=excluded.coder_json, swap_cost_seconds=excluded.swap_cost_seconds""",
                (name, _json_object(reasoner, "reasoner profile"), _json_object(coder, "coder profile"), swap_cost_seconds),
            )

    def get_model_profile(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM model_profiles WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        _decode_json(item, "reasoner_json", "reasoner")
        _decode_json(item, "coder_json", "coder")
        return item

    def register_tool_def(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        family: str,
        side_effects: str,
        timeout_seconds: int,
    ) -> None:
        if side_effects not in {"none", "writes-outputs"}:
            raise ValueError(f"invalid tool side effects: {side_effects}")
        if int(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO tool_defs(name, description, parameters_json, family, side_effects, timeout_seconds)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                     parameters_json=excluded.parameters_json, family=excluded.family,
                     side_effects=excluded.side_effects, timeout_seconds=excluded.timeout_seconds""",
                (name, description, _json_object(parameters, "tool parameters"), family, side_effects, int(timeout_seconds)),
            )

    def list_tool_defs(self, *, family: str | None = None) -> list[dict[str, Any]]:
        if family:
            rows = self.conn.execute("SELECT * FROM tool_defs WHERE family=? ORDER BY name", (family,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM tool_defs ORDER BY name").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            _decode_json(item, "parameters_json", "parameters")
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Commands inbox (STO-3)
    # ------------------------------------------------------------------
    def enqueue_command(
        self,
        project_id: str,
        action: str,
        *,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
        command_id: str | None = None,
    ) -> str:
        """Append a control command; valid on the read-only web query facade.

        When this Store was opened read-only, the command is inserted using a
        temporary connection. No other write operation is permitted on that
        facade; the runner remains responsible for processing the queue.
        """
        if action not in COMMAND_ACTIONS:
            raise ValueError(f"invalid command action: {action}")
        command_id = command_id or new_id()
        timestamp = utc_now()
        args = (command_id, project_id, node_id, action, _json_object(payload if payload is not None else {}, "command payload"), timestamp)
        if self.read_only:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 10000")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO commands(id, project_id, node_id, action, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                    args,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            with self._transaction() as conn:
                conn.execute(
                    "INSERT INTO commands(id, project_id, node_id, action, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                    args,
                )
        return command_id

    def claim_commands(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Claim queued commands for the single runner/writer process."""
        now = utc_now()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM commands WHERE status='queued' ORDER BY created_at, id LIMIT ?", (int(limit),)
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.executemany("UPDATE commands SET status='running', claimed_at=? WHERE id=?", [(now, cid) for cid in ids])
            result = []
            for row in rows:
                item = dict(row)
                _decode_json(item, "payload_json", "payload")
                item["status"] = "running"
                item["claimed_at"] = now
                result.append(item)
            return result

    def complete_command(self, command_id: str, *, error: str | None = None) -> None:
        status = "failed" if error else "succeeded"
        with self._transaction() as conn:
            row = conn.execute("SELECT project_id, node_id FROM commands WHERE id=?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown command: {command_id}")
            conn.execute(
                "UPDATE commands SET status=?, completed_at=?, error=? WHERE id=?",
                (status, utc_now(), error, command_id),
            )
            self._insert_event(conn, row["project_id"], node_id=row["node_id"], event_type="command.completed",
                               level="error" if error else "info", message=f"Command {status}",
                               data={"command_id": command_id, "error": error})

    def list_commands(self, project_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute("SELECT * FROM commands WHERE project_id=? AND status=? ORDER BY created_at, id", (project_id, status)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM commands WHERE project_id=? ORDER BY created_at, id", (project_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            _decode_json(item, "payload_json", "payload")
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Backups (STO-8)
    # ------------------------------------------------------------------
    def backup_now(self, *, today: str | None = None) -> Path:
        if self.read_only:
            raise StoreError("cannot create backup from a read-only store")
        if not self.backup_dir:
            raise StoreError("backup_dir was not configured")
        day = today or datetime.now(timezone.utc).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", day):
            raise ValueError("today must be YYYYMMDD")
        with self._backup_lock:
            return self._write_backup(day)

    def backup_if_due(self, *, today: str | None = None) -> Path | None:
        if self.read_only or not self.backup_dir:
            return None
        day = today or datetime.now(timezone.utc).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", day):
            raise ValueError("today must be YYYYMMDD")
        with self._backup_lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            daily = self.backup_dir / f"nai-{day}.sqlite3"
            if daily.exists():
                metadata_path = Path(str(daily) + ".meta.json")
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    previous_signature = metadata.get("source_signature")
                except (OSError, ValueError, AttributeError):
                    previous_signature = None
                if previous_signature == self._database_signature():
                    self._prune_backups()
                    return None
                # Refresh today's snapshot after writes so a startup backup does
                # not remain an empty/pre-change copy for the rest of the day.
                return self._write_backup(day, replace_daily=True)
            return self._write_backup(day)

    def _database_signature(self) -> dict[str, dict[str, int] | None]:
        signature: dict[str, dict[str, int] | None] = {}
        for label, path in (("database", self.db_path), ("wal", Path(str(self.db_path) + "-wal"))):
            try:
                stat = path.stat()
                signature[label] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
            except FileNotFoundError:
                signature[label] = None
        return signature

    def _write_backup(self, day: str, *, replace_daily: bool = False) -> Path:
        assert self.backup_dir is not None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        daily = self.backup_dir / f"nai-{day}.sqlite3"
        if replace_daily and daily.exists():
            destination = self.backup_dir / f".{daily.name}.{new_id()}.tmp"
        elif daily.exists():
            destination = self.backup_dir / f"nai-{day}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}.sqlite3"
        else:
            destination = daily
        source = sqlite3.connect(self.db_path, timeout=10)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.commit()
        except Exception:
            target.close()
            source.close()
            destination.unlink(missing_ok=True)
            raise
        else:
            target.close()
            source.close()
        if replace_daily:
            destination.replace(daily)
            destination = daily
        if destination == daily:
            metadata_path = Path(str(daily) + ".meta.json")
            temporary_metadata = metadata_path.with_name(metadata_path.name + ".tmp")
            temporary_metadata.write_text(
                canonical_json({"source_signature": self._database_signature()}) + "\n",
                encoding="utf-8",
            )
            temporary_metadata.replace(metadata_path)
        self._prune_backups()
        self.last_backup_error = None
        return destination

    def _prune_backups(self) -> None:
        if not self.backup_dir:
            return
        backups = sorted(self.backup_dir.glob("nai-*.sqlite3"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for path in backups[self.backup_keep:]:
            path.unlink(missing_ok=True)
            Path(str(path) + ".meta.json").unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Project export/import (STO-7)
    # ------------------------------------------------------------------
    def _bundle_tables(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        project = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        nodes = [dict(r) for r in self.conn.execute("SELECT * FROM nodes WHERE project_id=? ORDER BY created_at, id", (project_id,)).fetchall()]
        node_ids = [r["id"] for r in nodes]
        def in_nodes(table: str, column: str = "node_id") -> list[dict[str, Any]]:
            if not node_ids:
                return []
            marks = ",".join("?" for _ in node_ids)
            return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} WHERE {column} IN ({marks})", node_ids).fetchall()]
        jobs = [dict(r) for r in self.conn.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at, id", (project_id,)).fetchall()]
        events = [dict(r) for r in self.conn.execute("SELECT * FROM events WHERE project_id=? ORDER BY time, id", (project_id,)).fetchall()]
        artifacts = [dict(r) for r in self.conn.execute("SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at, id", (project_id,)).fetchall()]
        commands = [dict(r) for r in self.conn.execute(
            "SELECT * FROM commands WHERE project_id=? AND status IN ('succeeded','failed','cancelled') ORDER BY created_at, id", (project_id,)
        ).fetchall()]
        # Keep only event/job/node links belonging to the exported project.
        dependencies = []
        if node_ids:
            marks = ",".join("?" for _ in node_ids)
            dependencies = [dict(r) for r in self.conn.execute(
                f"SELECT * FROM node_dependencies WHERE node_id IN ({marks}) ORDER BY node_id, ordinal", node_ids
            ).fetchall()]
        return {
            "projects": [dict(project)],
            "nodes": nodes,
            "node_dependencies": dependencies,
            "requirements": in_nodes("requirements"),
            "test_records": in_nodes("test_records"),
            "jobs": jobs,
            "events": events,
            "artifacts": artifacts,
            "approvals": in_nodes("approvals"),
            "commands": commands,
        }

    def export_project(self, project_id: str, destination: str | Path) -> Path:
        """Export one project to a directory or `.zip` archive."""
        destination = Path(destination).expanduser().resolve()
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        tables = self._bundle_tables(project_id)
        artifact_files: dict[str, str] = {}
        workspace = Path(project["workspace_path"]).resolve()
        is_zip = destination.suffix.lower() == ".zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage: Path | None = Path(tempfile.mkdtemp(prefix="nai-export-", dir=destination.parent))
        try:
            assert stage is not None
            artifact_dir = stage / "artifacts"
            artifact_dir.mkdir()
            for artifact in tables["artifacts"]:
                relative = _safe_relative_path(artifact["path"])
                source = (workspace / relative).resolve()
                if workspace != source and workspace not in source.parents:
                    raise ExportImportError(f"artifact escapes workspace: {artifact['path']}")
                if not source.is_file():
                    raise ExportImportError(f"artifact file missing: {artifact['path']}")
                data = source.read_bytes()
                if hashlib.sha1(data).hexdigest() != artifact["sha1"]:
                    raise ExportImportError(f"artifact hash mismatch: {artifact['path']}")
                bundle_name = f"artifacts/{artifact['id']}.bin"
                (stage / bundle_name).write_bytes(data)
                artifact_files[artifact["id"]] = bundle_name
            manifest = {
                "format": "nai-project-export",
                "format_version": 1,
                "exported_at": utc_now(),
                "project_id": project_id,
                "tables": tables,
                "artifact_files": artifact_files,
            }
            (stage / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            if is_zip:
                temp_zip = destination.with_name(destination.name + ".tmp")
                with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(stage.rglob("*")):
                        if path.is_file():
                            archive.write(path, path.relative_to(stage).as_posix())
                temp_zip.replace(destination)
            else:
                if destination.exists():
                    if not destination.is_dir() or any(destination.iterdir()):
                        raise FileExistsError(f"export destination exists and is not empty: {destination}")
                    destination.rmdir()
                stage.replace(destination)
                stage = None  # staged directory was moved
            return destination
        finally:
            if stage is not None and stage.exists() and stage.is_dir():
                shutil.rmtree(stage, ignore_errors=True)

    def import_project(
        self,
        source: str | Path,
        *,
        workspace_path: str | Path | None = None,
        project_id: str | None = None,
    ) -> str:
        """Import a directory or zip project bundle, remapping all primary ids."""
        if self.read_only:
            raise StoreError("cannot import into a read-only store")
        source = Path(source).expanduser().resolve()
        with _BundleReader(source) as bundle:
            try:
                manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            except Exception as exc:
                raise ExportImportError(f"cannot read manifest: {exc}") from exc
            if manifest.get("format") != "nai-project-export" or manifest.get("format_version") != 1:
                raise ExportImportError("unsupported project export format/version")
            tables = manifest.get("tables")
            if not isinstance(tables, dict) or not isinstance(tables.get("projects"), list) or len(tables["projects"]) != 1:
                raise ExportImportError("manifest must contain exactly one project")
            original_project = tables["projects"][0]
            old_project_id = original_project["id"]
            new_project_id = project_id or new_id()
            if self.conn.execute("SELECT 1 FROM projects WHERE id=?", (new_project_id,)).fetchone():
                raise ExportImportError(f"project id already exists: {new_project_id}")
            if workspace_path is None:
                base_name = re.sub(r"[^A-Za-z0-9._-]+", "-", original_project.get("name", "project")).strip(".-") or "project"
                workspace = (self.db_path.parent / "workspaces" / f"{base_name}-{new_project_id}").resolve()
            else:
                workspace = Path(workspace_path).expanduser().resolve()
            workspace_existed = workspace.exists()
            workspace.mkdir(parents=True, exist_ok=True)

            # Generate new globally unique IDs for every primary-keyed project row.
            id_maps: dict[str, dict[str, str]] = {}
            for table in ("nodes", "jobs", "events", "artifacts", "approvals", "commands"):
                id_maps[table] = {row["id"]: new_id() for row in tables.get(table, [])}
            id_maps["projects"] = {old_project_id: new_project_id}

            try:
                with self._transaction() as conn:
                    p = dict(original_project)
                    p.update(id=new_project_id, workspace_path=str(workspace))
                    conn.execute(
                        "INSERT INTO projects(id,name,brief,workspace_path,status,created_at,settings_json) VALUES (?,?,?,?,?,?,?)",
                        tuple(p[k] for k in ("id", "name", "brief", "workspace_path", "status", "created_at", "settings_json")),
                    )
                    for row in tables.get("nodes", []):
                        conn.execute(
                            """INSERT INTO nodes(id,project_id,parent_id,kind,name,objective,spec_text,contract_json,status,
                               content_hash,built_hash,attempts,test_command,created_at,updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (id_maps["nodes"][row["id"]], new_project_id,
                             id_maps["nodes"].get(row.get("parent_id")) if row.get("parent_id") else None,
                             row["kind"], row["name"], row["objective"], row["spec_text"], row["contract_json"], row["status"],
                             row.get("content_hash"), row.get("built_hash"), row["attempts"], row.get("test_command"), row["created_at"], row["updated_at"]),
                        )
                    for row in tables.get("node_dependencies", []):
                        conn.execute(
                            "INSERT INTO node_dependencies(node_id,dependency_node_id,ordinal) VALUES (?,?,?)",
                            (id_maps["nodes"][row["node_id"]], id_maps["nodes"][row["dependency_node_id"]], row["ordinal"]),
                        )
                    for row in tables.get("requirements", []):
                        conn.execute(
                            "INSERT INTO requirements(node_id,requirement_id,text,testable) VALUES (?,?,?,?)",
                            (id_maps["nodes"][row["node_id"]], row["requirement_id"], row["text"], row["testable"]),
                        )
                    for row in tables.get("test_records", []):
                        conn.execute(
                            "INSERT INTO test_records(node_id,file_path,requirement_ids_json,frozen_hash,last_result,last_run_at) VALUES (?,?,?,?,?,?)",
                            (id_maps["nodes"][row["node_id"]], row["file_path"], row["requirement_ids_json"], row["frozen_hash"], row["last_result"], row.get("last_run_at")),
                        )
                    for row in tables.get("jobs", []):
                        conn.execute(
                            """INSERT INTO jobs(id,project_id,node_id,kind,model_role,status,attempt,input_ref_json,result_ref_json,
                               tokens_in,tokens_out,started_at,ended_at,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (id_maps["jobs"][row["id"]], new_project_id,
                             id_maps["nodes"].get(row.get("node_id")) if row.get("node_id") else None,
                             row["kind"], row["model_role"], row["status"], row["attempt"], row["input_ref_json"], row["result_ref_json"],
                             row.get("tokens_in"), row.get("tokens_out"), row.get("started_at"), row.get("ended_at"), row.get("error"), row["created_at"]),
                        )
                    for row in tables.get("events", []):
                        conn.execute(
                            "INSERT INTO events(id,project_id,node_id,job_id,time,level,type,message,data_json) VALUES (?,?,?,?,?,?,?,?,?)",
                            (id_maps["events"][row["id"]], new_project_id,
                             id_maps["nodes"].get(row.get("node_id")) if row.get("node_id") else None,
                             id_maps["jobs"].get(row.get("job_id")) if row.get("job_id") else None,
                             row["time"], row["level"], row["type"], row["message"], row["data_json"]),
                        )
                    for row in tables.get("artifacts", []):
                        bundle_name = manifest.get("artifact_files", {}).get(row["id"])
                        if not bundle_name:
                            raise ExportImportError(f"artifact file mapping missing for {row['id']}")
                        bundle_relative = _safe_relative_path(bundle_name).as_posix()
                        data = bundle.read(bundle_relative)
                        if hashlib.sha1(data).hexdigest() != row["sha1"]:
                            raise ExportImportError(f"artifact hash mismatch for {row['path']}")
                        relative = _safe_relative_path(row["path"])
                        if not relative.parts[:2] == (".nai", "artifacts"):
                            raise ExportImportError(f"artifact is outside .nai/artifacts: {row['path']}")
                        target = (workspace / relative).resolve()
                        if workspace != target and workspace not in target.parents:
                            raise ExportImportError(f"artifact escapes target workspace: {row['path']}")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        conn.execute(
                            "INSERT INTO artifacts(id,project_id,node_id,kind,path,sha1,created_at) VALUES (?,?,?,?,?,?,?)",
                            (id_maps["artifacts"][row["id"]], new_project_id,
                             id_maps["nodes"].get(row.get("node_id")) if row.get("node_id") else None,
                             row["kind"], row["path"], row["sha1"], row["created_at"]),
                        )
                    for row in tables.get("approvals", []):
                        conn.execute(
                            "INSERT INTO approvals(id,node_id,stage,decision,note,time) VALUES (?,?,?,?,?,?)",
                            (id_maps["approvals"][row["id"]], id_maps["nodes"][row["node_id"]], row["stage"], row["decision"], row["note"], row["time"]),
                        )
                    for row in tables.get("commands", []):
                        conn.execute(
                            "INSERT INTO commands(id,project_id,node_id,action,payload_json,status,created_at,claimed_at,completed_at,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (id_maps["commands"][row["id"]], new_project_id,
                             id_maps["nodes"].get(row.get("node_id")) if row.get("node_id") else None,
                             row["action"], row["payload_json"], row["status"], row["created_at"], row.get("claimed_at"), row.get("completed_at"), row.get("error")),
                        )
                    self._insert_event(conn, new_project_id, event_type="project.imported", message="Project imported from export bundle",
                                       data={"source_project_id": old_project_id})
            except Exception:
                if not workspace_existed:
                    shutil.rmtree(workspace, ignore_errors=True)
                raise
            return new_project_id


class _BundleReader:
    """Read files from an export directory or zip without extracting untrusted paths."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.archive: zipfile.ZipFile | None = None

    def __enter__(self) -> _BundleReader:
        if self.source.is_dir():
            self.archive = None
        elif self.source.is_file() and zipfile.is_zipfile(self.source):
            self.archive = zipfile.ZipFile(self.source, "r")
        else:
            raise ExportImportError(f"not a project export directory or zip: {self.source}")
        return self

    def read(self, name: str) -> bytes:
        relative = _safe_relative_path(name).as_posix()
        if self.archive:
            try:
                info = self.archive.getinfo(relative)
            except KeyError as exc:
                raise ExportImportError(f"bundle member missing: {relative}") from exc
            if info.is_dir():
                raise ExportImportError(f"bundle member is a directory: {relative}")
            return self.archive.read(info)
        root = self.source.resolve()
        path = (root / relative).resolve()
        if root != path and root not in path.parents:
            raise ExportImportError(f"bundle path escapes source directory: {relative}")
        if not path.is_file():
            raise ExportImportError(f"bundle member missing: {relative}")
        return path.read_bytes()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.archive:
            self.archive.close()

"""git-checkpoint: workspace snapshots as local git commits (v1 + CORE-5)."""
from __future__ import annotations

import subprocess
from pathlib import Path

GITIGNORE = ".agent/\nnode_modules/\n.venv/\nvenv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.nai/artifacts/\n"


class GitCheckpoint:
    def __init__(self, root: Path, enabled: bool = True, identity: tuple[str, str] = ("nai", "nai@localhost")):
        self.root = Path(root)
        self.enabled = enabled
        self.identity = identity

    def _git(self, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", f"user.name={self.identity[0]}", "-c", f"user.email={self.identity[1]}", *a],
                              cwd=self.root, capture_output=True, text=True)

    def checkpoint(self, msg: str) -> None:
        """R1-R5: init on first use, `git add -A`, commit; failures are ignored."""
        if not self.enabled:
            return
        try:
            if not (self.root / ".git").exists():
                self._git("init", "-q")
                gi = self.root / ".gitignore"
                if not gi.exists():
                    gi.write_text(GITIGNORE)
            self._git("add", "-A")
            self._git("commit", "-q", "-m", msg[:200])
        except FileNotFoundError:  # git is not installed
            self.enabled = False

    # -- CORE-5: node/job tagged checkpoints -----------------------------------
    @staticmethod
    def job_message(node_id: str, job_kind: str, attempt: int = 1) -> str:
        return f"{node_id}: {job_kind} attempt {attempt}"

    def checkpoint_job(self, node_id: str, job_kind: str, attempt: int = 1) -> None:
        self.checkpoint(self.job_message(node_id, job_kind, attempt))

    def find(self, node_id: str, job_kind: str | None = None) -> list[tuple[str, str]]:
        """Commits made by checkpoint_job for a node (optionally one job kind), newest first: (sha, subject)."""
        if not self.enabled or not (self.root / ".git").exists():
            return []
        prefix = f"{node_id}: " + (f"{job_kind} attempt " if job_kind else "")
        try:
            r = self._git("log", "--format=%H%x1f%s")
        except FileNotFoundError:
            return []
        out = []
        for line in r.stdout.splitlines():
            sha, _, subject = line.partition("\x1f")
            if subject.startswith(prefix):
                out.append((sha, subject))
        return out

    def restore(self, sha: str) -> bool:
        """Make the working tree and index equal to commit `sha` (tracked files added/changed/removed).

        HEAD does not move; the next checkpoint records the restore as a new commit, so no history is lost.
        Untracked, non-ignored files are left alone.
        """
        if not self.enabled:
            return False
        try:
            return self._git("read-tree", "--reset", "-u", sha).returncode == 0
        except FileNotFoundError:
            return False

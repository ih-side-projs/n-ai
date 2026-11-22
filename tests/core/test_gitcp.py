"""v1 git-checkpoint R1-R5, E1-E2, example; CORE-5 job-tagged checkpoints and restore."""
import subprocess

from nai.gitcp import GitCheckpoint
from tests.core.conftest import needs_git

pytestmark = needs_git


def commits(root):
    return subprocess.run(["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True).stdout.split("\n")[:-1]


def test_R1_disabled_does_nothing(tmp_path):
    GitCheckpoint(tmp_path, enabled=False).checkpoint("x")
    assert not (tmp_path / ".git").exists()


def test_R2_example_init_and_gitignore(tmp_path):
    (tmp_path / "a").write_text("1")
    GitCheckpoint(tmp_path).checkpoint("initial")
    assert commits(tmp_path) == ["initial"]
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[:7] == [".agent/", "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".pytest_cache/"]


def test_R3_identity_and_message_cut(tmp_path):
    (tmp_path / "a").write_text("1")
    GitCheckpoint(tmp_path).checkpoint("m" * 300)
    r = subprocess.run(["git", "log", "--format=%an <%ae>|%s"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    assert r.startswith("nai <nai@localhost>|") and len(r.split("|", 1)[1]) == 200


def test_E1_existing_git_and_gitignore_untouched(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("mine\n")
    (tmp_path / "a").write_text("1")
    GitCheckpoint(tmp_path).checkpoint("c1")
    assert (tmp_path / ".gitignore").read_text() == "mine\n"


def test_E2_R4_no_changes_no_commit_no_raise(tmp_path):
    (tmp_path / "a").write_text("1")
    g = GitCheckpoint(tmp_path)
    g.checkpoint("one")
    g.checkpoint("two")        # nothing to commit: non-zero exit ignored
    assert commits(tmp_path) == ["one"]


def test_R5_git_missing_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    g = GitCheckpoint(tmp_path)
    g.checkpoint("x")
    assert g.enabled is False


def test_CORE5_job_messages_and_find(tmp_path):
    g = GitCheckpoint(tmp_path)
    for i, (node, kind, att) in enumerate([("n1", "implement", 1), ("n2", "implement", 1), ("n1", "implement", 2), ("n1", "verify", 1)]):
        (tmp_path / "f").write_text(str(i))
        g.checkpoint_job(node, kind, att)
    assert commits(tmp_path)[0] == "n1: verify attempt 1"
    assert [s for _, s in g.find("n1")] == ["n1: verify attempt 1", "n1: implement attempt 2", "n1: implement attempt 1"]
    assert [s for _, s in g.find("n1", "implement")] == ["n1: implement attempt 2", "n1: implement attempt 1"]
    assert [s for _, s in g.find("n2")] == ["n2: implement attempt 1"]
    assert g.find("n") == []      # no prefix confusion between ids


def test_CORE5_restore_node_state(tmp_path):
    g = GitCheckpoint(tmp_path)
    (tmp_path / "a.txt").write_text("v1")
    g.checkpoint_job("n1", "implement", 1)
    (tmp_path / "a.txt").write_text("v2")
    (tmp_path / "b.txt").write_text("new")
    g.checkpoint_job("n1", "implement", 2)
    first = g.find("n1")[-1][0]
    assert g.restore(first) is True
    assert (tmp_path / "a.txt").read_text() == "v1" and not (tmp_path / "b.txt").exists()
    g.checkpoint("restored")                         # history is preserved, restore becomes a commit
    assert len(commits(tmp_path)) == 3

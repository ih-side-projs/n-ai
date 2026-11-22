"""v1 workspace-tools R1-R7, E1-E4, example; CORE-3 path policy; CORE-4/5 job context; CORE-6 /work fix."""
import subprocess

import pytest

from nai.policy import JobContext, PathPolicy
from nai.workspace import Workspace
from tests.core.conftest import needs_git


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "w", git=False)


def test_R1_construction(tmp_path):
    w = Workspace(tmp_path / "new" / "w", test_cmds=["pytest"], gate=False)
    assert (w.root / ".agent").is_dir() and w.test_cmds == ["pytest"] and w.gate.enabled is False
    assert w.gate.test_cmds is w.test_cmds


def test_R2_resolve_and_E1(ws):
    assert ws.resolve("a/../b.txt") == ws.root / "b.txt" and ws.resolve(".") == ws.root
    with pytest.raises(ValueError, match="path escapes workspace: ../x"):
        ws.resolve("../x")
    (ws.root / "link").symlink_to("/tmp")
    with pytest.raises(ValueError):
        ws.resolve("link/anything")
    assert ws.call("read_file", {"path": "../../etc/passwd"}) == "ERROR: ValueError: path escapes workspace: ../../etc/passwd"


def test_R3_read_file(ws):
    assert ws.read_file("nope") == "ERROR: nope does not exist or is not a file"
    ws.write_file("f.txt", "l1\nl2\nl3\nl4\n")
    assert ws.read_file("f.txt") == "l1\nl2\nl3\nl4\n"
    assert ws.read_file("f.txt", 2, 3) == "[f.txt lines 2-3 of 4]\nl2\nl3"
    assert ws.read_file("f.txt", start_line=0, end_line=99) == "[f.txt lines 1-4 of 4]\nl1\nl2\nl3\nl4"
    ws.write_file("big.txt", "x\n" * 15000)
    out = ws.read_file("big.txt")
    assert "chars truncated" in out and out.endswith("[file has 15000 lines; use start_line/end_line for specific parts]")


def test_E4_invalid_utf8_replaced(ws):
    (ws.root / "b.bin").write_bytes(b"ok\xff\xfe")
    assert ws.read_file("b.bin").startswith("ok") and "\ufffd" in ws.read_file("b.bin")


def test_R4_example_E2_E3_write(ws):
    assert ws.write_file("a/b.py", "x=1\n") == "wrote a/b.py (4 bytes, 2 lines)"
    assert ws.touched == {"a/b.py"}
    assert ws.write_file("a.txt", "") == "wrote a.txt (0 bytes, 1 lines)"
    ws.run_command("echo hi > shell.txt")
    assert "shell.txt" not in ws.touched
    assert ws.write_file("u.txt", "é") == "wrote u.txt (2 bytes, 1 lines)"      # real bytes (deliberate fix of v1's character count)


def test_R4_write_invalidates_gate(ws):
    ws.run_command("echo '1 passed' # pytest")
    assert ws.verification_problem() is None
    ws.write_file("a", "1")
    assert ws.verification_problem().startswith("No passing verification")


def test_R5_edit_file(ws):
    assert ws.edit_file("x", "a", "b") == "ERROR: x does not exist"
    ws.write_file("x", "aa bb")
    assert ws.edit_file("x", "zz", "y") == "ERROR: old_str not found (must match exactly, including whitespace/indentation)"
    assert ws.edit_file("x", "a", "y") == "ERROR: old_str occurs 2 times; include more surrounding context to make it unique"
    assert ws.edit_file("x", "bb", "cc") == "edited x" and ws.read_file("x") == "aa cc"


def test_R6_list_files(ws):
    assert ws.list_files() == "(empty)"
    for f in ("b.txt", "a.txt", "d/z.txt", "d/e/deep.txt", "c/y.txt", "node_modules/skip.js", ".git/x"):
        ws.write_file(f, "1")
    out = ws.list_files(".", 1).splitlines()
    assert out == ["c/", "  y.txt", "d/", "  e/", "  z.txt", "a.txt", "b.txt"]
    assert "deep.txt" in ws.list_files(".", 3) and "deep.txt" not in ws.list_files(".", 1)
    assert ws.list_files("a.txt") == "ERROR: a.txt is not a directory"


def test_R6_list_truncation(ws):
    for i in range(400):
        ws.write_file(f"d{i:03}/f.txt", "1")
    out = ws.list_files(".", 3).splitlines()
    assert out[-1] == "... (listing truncated)"
    assert out.count("  f.txt") == 150          # no new directory is entered once 300 lines exist
    assert "d399/" in out                       # but the directory being listed is finished


def test_R7_call_dispatch(ws):
    assert ws.call("nope", {}) == "ERROR: unknown tool 'nope'"
    assert ws.call("write_file", {"path": "a"}).startswith("ERROR: bad arguments for write_file:")
    assert ws.call("write_file", {"path": "a", "content": "x"}) == "wrote a (1 bytes, 1 lines)"
    assert ws.call("run_command", {"command": "echo hi"}) == "exit code: 0\nhi\n"
    assert ws.call("run_command", {"command": "echo", "scope": "evil"}).startswith("ERROR: bad arguments")   # model can't pick a scope
    assert ws.call("list_files", {"path": "a"}).startswith("ERROR: a is not a directory")


# --- CORE-3 -----------------------------------------------------------------------------
def test_CORE3_policy_blocks_writes_not_reads(ws):
    ws.write_file("tests/test_R1.py", "frozen")
    ws.touched.clear()
    pol = PathPolicy(writable=("src/**",), readonly=("tests/**",))
    with ws.job_context(JobContext("n1", "implement", policy=pol)):
        assert ws.write_file("src/a.py", "ok") == "wrote src/a.py (2 bytes, 1 lines)"
        assert ws.write_file("tests/test_R1.py", "x") == "ERROR: path not writable for this job: tests/test_R1.py"
        assert ws.call("edit_file", {"path": "tests/test_R1.py", "old_str": "frozen", "new_str": "x"}) == \
            "ERROR: path not writable for this job: tests/test_R1.py"
        assert ws.write_file("./other/../README.md", "x") == "ERROR: path not writable for this job: ./other/../README.md"
        assert ws.read_file("tests/test_R1.py") == "frozen"
        assert ws.touched == {"src/a.py"}
    assert ws.read_file("tests/test_R1.py") == "frozen"
    assert ws.write_file("tests/test_R1.py", "free again").startswith("wrote")      # policy ends with the job


def test_CORE3_denied_write_changes_nothing(ws):
    ws.run_command("echo '1 passed' # pytest")
    with ws.job_context(JobContext(policy=PathPolicy(writable=()))):
        ws.write_file("a", "x")
    assert not (ws.root / "a").exists() and ws.verification_problem() is None      # gate not invalidated


def test_CORE3_escape_still_wins_and_nested_contexts_restore(ws):
    outer, inner = PathPolicy(writable=("a/**",)), PathPolicy(writable=("b/**",))
    with ws.job_context(JobContext(policy=outer)):
        with ws.job_context(JobContext(policy=inner)):
            assert ws.write_file("a/x", "1").startswith("ERROR: path not writable")
            assert ws.call("write_file", {"path": "../x", "content": "1"}).startswith("ERROR: ValueError: path escapes")
        assert ws.write_file("a/x", "1").startswith("wrote")
    assert ws.job is None


def test_CORE3_protected_hashes_detect_shell_edits(ws):
    ws.write_file("tests/t.py", "v1")
    ws.write_file("src/a.py", "v1")
    pol = PathPolicy(writable=("src/**",), readonly=("tests/**",))
    snap = ws.protected_hashes(pol)
    assert list(snap) == ["tests/t.py"]
    ws.run_command("echo changed > tests/t.py")                                     # shell bypasses the policy ...
    assert ws.protected_hashes(pol) != snap                                         # ... but is detectable


# --- CORE-4 / CORE-5 ----------------------------------------------------------------------
def test_CORE4_node_test_command_scope(ws):
    with ws.job_context(JobContext("n1", "verify", test_command="python3 -c 'print(1)' # unit")):
        assert "n1" in ws.gate._scopes
        assert ws.verification_problem().endswith("(each must exit 0): python3 -c 'print(1)' # unit.")
        assert "[verification" in ws.run_command("python3 -c 'print(1)' # unit")
        assert ws.verification_problem() is None
    assert ws.verification_problem() is not None       # run-wide scope was never satisfied


@needs_git
def test_CORE5_checkpoint_job_defaults_from_context(tmp_path):
    w = Workspace(tmp_path / "w")
    w.write_file("a", "1")
    with w.job_context(JobContext("node-a", "implement", attempt=2)):
        w.checkpoint_job()
    w.write_file("a", "2")
    w.checkpoint_job("node-b", "author_tests")
    log = subprocess.run(["git", "log", "--format=%s"], cwd=w.root, capture_output=True, text=True).stdout.split("\n")
    assert log[:2] == ["node-b: author_tests attempt 1", "node-a: implement attempt 2"]
    with pytest.raises(ValueError):
        w.checkpoint_job()


# --- CORE-6 --------------------------------------------------------------------------------
def test_CORE6_work_prefix_stripping_fixed(tmp_path, fake_docker):
    w = Workspace(tmp_path / "w", docker_image="img", git=False)
    try:
        assert w.resolve("/work") == w.root and w.resolve("/work/") == w.root
        assert w.resolve("/work/a/b.py") == w.root / "a" / "b.py" and w.resolve("/work//a.py") == w.root / "a.py"
        # v1 turned /workspace/x into space/x; now it is an absolute path outside the workspace
        with pytest.raises(ValueError, match="path escapes workspace: /workspace/x"):
            w.resolve("/workspace/x")
        with pytest.raises(ValueError):
            w.resolve("/working/x")
        assert w.resolve("workspace/x") == w.root / "workspace" / "x"
        assert w.run_command("echo in-container") == "exit code: 0\nin-container\n"
    finally:
        w.close()


def test_CORE6_local_mode_does_not_strip_work(ws):
    with pytest.raises(ValueError):
        ws.resolve("/work/a.py")

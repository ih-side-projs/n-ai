"""v1 verification-gate R1-R8, E1-E4, example; CORE-4 node scopes."""
import time

import pytest

from nai.gate import VerificationGate


@pytest.fixture
def g(tmp_path):
    return VerificationGate(tmp_path, [])


@pytest.mark.parametrize("cmd", ["pytest --collect-only", "pytest --co", "pytest --help", "tool --version", "x --dry-run",
                                 "jest --listTests", "pytest -h", "pytest || true", "pytest || :", "pytest || exit 0"])
def test_R1_rejected_commands(g, cmd):
    assert g.counts_as_test(cmd, 0, "ok") is None


@pytest.mark.parametrize("out", ["collected 0 items", "Ran 0 tests in 0.0s", "running 0 tests", "NO TESTS RAN", "No tests found"])
def test_R1_zero_tests(g, out):
    assert g.counts_as_test("pytest", 0, out) is None


def test_R1_nonzero(g):
    assert g.counts_as_test("pytest", 1, "") is None


def test_E1_E2(g):
    assert g.counts_as_test("pytest -q || true", 0, "") is None
    assert g.counts_as_test("python -m pytest -q | tail -3", 0, "5 passed") == ["auto"]


@pytest.mark.parametrize("cmd", ["pytest -q", "cd x && pytest", "python3 -m unittest", "npm test", "npm run test", "yarn t", "go test ./...",
                                 "cargo nextest run", "make check", "bundle exec rake test", "mvn -q verify test", "npx vitest", "tox"])
def test_R3_auto_recognised(g, cmd):
    assert g.counts_as_test(cmd, 0, "1 passed") == ["auto"]


@pytest.mark.parametrize("cmd", ["ls", "echo hello", "python app.py", "npm install"])
def test_R3_auto_not_recognised(g, cmd):
    assert g.counts_as_test(cmd, 0, "") is None


def test_R2_E4_explicit_commands_substring(tmp_path):
    g = VerificationGate(tmp_path, ["pytest", "pytest -q", "npm run build"])
    assert g.counts_as_test("pytest -q tests/", 0, "") == ["pytest", "pytest -q"]
    assert g.counts_as_test("go test", 0, "") is None


def test_R4_R5_record_and_invalidate(tmp_path):
    g = VerificationGate(tmp_path, [])
    (tmp_path / "a.txt").write_text("1")
    g.record_pass(["auto"])
    assert g.passed == {"auto"} and g.fp_at_pass == g.fingerprint()
    fp = g.fp_at_pass
    g.invalidate()
    assert g.passed == set() and g.fp_at_pass == fp


def test_R6_fingerprint_skips_and_changes(tmp_path):
    g = VerificationGate(tmp_path, [])
    (tmp_path / "a.py").write_text("x")
    base = g.fingerprint()
    for d in (".git", "node_modules", "__pycache__", ".venv", "venv", ".agent", ".pytest_cache", ".mypy_cache", "dist", "build",
              "target", "coverage", "htmlcov", ".next", ".tox", ".nox", ".cache", ".ruff_cache", "x.egg-info"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f").write_text("noise")
    (tmp_path / "m.pyc").write_text("c")
    (tmp_path / "run.log").write_text("l")
    assert g.fingerprint() == base
    (tmp_path / "a.py").write_text("xy")
    assert g.fingerprint() != base


def test_R7_missing_messages(tmp_path):
    g = VerificationGate(tmp_path, [])
    assert g.problem() == ("No passing verification since your last file change. Run a test command "
                           "(pytest, npm test, go test, cargo test, make test, ...) that exits 0.")
    assert VerificationGate(tmp_path, [], enabled=False).problem() is None


def test_R7_example(tmp_path):
    g = VerificationGate(tmp_path, ["pytest", "npm run build"])
    g.record_pass(g.counts_as_test("pytest -q", 0, "1 passed"))
    assert g.problem() == "No passing verification since your last file change. Run these commands (each must exit 0): npm run build."


def test_E3_never_passed_reports_missing_first(tmp_path):
    g = VerificationGate(tmp_path, [])
    assert g.problem().startswith("No passing verification")


def test_R8_files_changed(tmp_path):
    g = VerificationGate(tmp_path, [])
    (tmp_path / "a").write_text("1")
    g.record_pass(["auto"])
    assert g.problem() is None
    time.sleep(0.01)
    (tmp_path / "a").write_text("22")
    assert g.problem() == "Files changed (e.g. via shell commands) since the last passing test run. Re-run the tests."


# --- CORE-4 ---------------------------------------------------------------------
def test_CORE4_node_scope_uses_node_command(tmp_path):
    g = VerificationGate(tmp_path, [])
    g.set_required("n1", "pytest tests/test_n1.py")
    assert g.counts_as_test("pytest tests/test_other.py", 0, "1 passed", scope="n1") is None
    keys = g.counts_as_test("pytest tests/test_n1.py -q", 0, "1 passed", scope="n1")
    assert keys == ["pytest tests/test_n1.py"]
    assert "pytest tests/test_n1.py" in g.problem("n1")
    g.record_pass(keys, "n1")
    assert g.problem("n1") is None
    assert g.problem() is not None            # default scope untouched


def test_CORE4_node_pass_is_tied_to_fingerprint(tmp_path):
    g = VerificationGate(tmp_path, [])
    g.set_required("n1", "pytest")
    (tmp_path / "a").write_text("1")
    g.record_pass(["pytest"], "n1")
    assert g.problem("n1") is None
    time.sleep(0.01)
    (tmp_path / "a").write_text("22")
    assert g.problem("n1").startswith("Files changed")


def test_CORE4_scopes_are_independent_and_invalidate_all(tmp_path):
    g = VerificationGate(tmp_path, [])
    g.set_required("a", "pytest a")
    g.set_required("b", "pytest b")
    g.record_pass(["pytest a"], "a")
    assert g.problem("a") is None and g.problem("b") is not None
    g.invalidate_all()
    assert g.problem("a") is not None


def test_CORE4_auto_mode_in_scope_keeps_v1_rules(tmp_path):
    g = VerificationGate(tmp_path, [])
    assert g.counts_as_test("pytest -q", 0, "ok", scope="auto-node") == ["auto"]
    assert g.counts_as_test("pytest -q", 0, "collected 0 items", scope="auto-node") is None
